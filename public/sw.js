/* SparrowMap service worker.
 *
 * Two jobs: make the camera app INSTALLABLE (a browser only offers "Install"
 * for a page backed by a service worker, over HTTPS), and make a running node
 * OFFLINE-RESILIENT so a camera keeps working through a network blip.
 *
 * It only ever caches this origin's own files. The vendored detector runtime
 * (the ~36 MB model + wasm) is cached the first time it's fetched, so a node
 * that has run once can start again with no network.
 */
/* TWO caches, deliberately. The app cache is versioned and PURGED on every
 * update; the vendor cache holds the pinned detector runtime (a ~38 MB model
 * plus wasm) and must survive those updates.
 *
 * 🚨 THEY USED TO BE ONE. Every app fix bumped the version, the activate
 * handler deleted every cache that was not the new one, and a phone had to
 * re-download 38 MB before its camera could start again - so shipping a small
 * JS fix quietly knocked every phone camera offline until it finished pulling
 * the model over mobile data. Version the code; never the thing that takes a
 * minute to fetch. */
const CACHE = 'sparrow-app-v11';
// Deliberately the LAST app cache name rather than a fresh one: devices already
// hold the model under it, and renaming would throw away the very download this
// split exists to protect. Bump ONLY when the vendored model itself changes.
const VENDOR_CACHE = 'sparrow-v6';

// 🗺️ Map tiles, in their own cache with a ceiling.
// Without these an offline map is a grey rectangle with some dots on it, which
// is not a map. They are static imagery and cannot mislead anybody about what
// is happening now, unlike the sighting data below - so they are the one thing
// here worth keeping for offline. Capped because a few minutes of panning can
// pull thousands of them and this is a phone.
// ⚠️ BUMPED v1 -> v2 (2026-08-26). Cache-first tiles meant the "API KEY
// REQUIRED" placeholders Carto served during a rate-limit episode got pinned in
// the service worker and re-served forever - a hard refresh clears HTTP cache
// but NOT CacheStorage, so the box/CDN fix could not reach a phone that had
// already cached them. The activate handler drops any tile cache whose name is
// not this one, so bumping the version purges the poisoned tiles on next visit.
// The box now 404s placeholders (res.ok false -> never cached), so v2 stays clean.
const TILE_CACHE = 'sparrow-tiles-v2';
const TILE_MAX = 400;

const SHELL = [
  // The CAMERA app.
  '/app',
  '/static/sparrow-app.js',
  '/static/manifest.webmanifest',
  // 🚨 AND THE MAP, WHICH THIS WORKER NEVER USED TO COVER.
  // Registration lived only in sparrow-app.js, so a visitor who only ever
  // opened the map had no service worker at all - the installed app opened to
  // the browser's error page with no signal. Resilience is now the stated
  // reason the project ships as a web app rather than through a store, and an
  // app that cannot open offline does not deliver it.
  '/',
  '/static/app.js',
  // The shared basemap app.js calls on load: without it cached, an offline map
  // open would die on a ReferenceError before drawing a single marker.
  '/static/basemap.js',
  '/vendor/maplibre-gl.js',
  '/vendor/maplibre-gl.css',
  '/vendor/leaflet-maplibre-gl.js',
  '/static/sitenav.js',
  '/static/install.js',
  '/static/offline.js',
  '/static/transparency.js',
  '/static/manifest-map.webmanifest',
  '/vendor/leaflet.js',
  '/vendor/leaflet.css',
  // Shared by both.
  '/static/style.css',
  // ⚠️ '/static/refresh.js' was here and the file is gone. The install is
  // allSettled so a stale entry cannot break it, which is exactly why one
  // could sit here failing on every install without anyone noticing. Remove
  // the entry when you remove the file.
  '/static/icon-192.png',
];

self.addEventListener('install', (e) => {
  e.waitUntil(
    caches.open(CACHE)
      // Best-effort: a missing shell file must not fail the whole install.
      .then((c) => Promise.allSettled(SHELL.map((u) => c.add(u))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      // Never delete Sparrow Send's caches (sparrowsend-*): CacheStorage is
      // shared per-origin, and wiping its shell on every map update made the two
      // service workers thrash each other's caches. Only drop OUR own old ones.
      .then((ks) => Promise.all(ks
        .filter((k) => k !== CACHE && k !== VENDOR_CACHE && k !== TILE_CACHE
                       && k.indexOf('sparrowsend-') !== 0)
        .map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.origin !== location.origin) return;

  // Cache-first ONLY for the vendored runtime: it is pinned and large (the
  // detector model + wasm), so a node that has run once starts again offline
  // and never re-downloads it.
  if (url.pathname.startsWith('/vendor/')) {
    e.respondWith(
      caches.open(VENDOR_CACHE).then((c) =>
        c.match(e.request).then((hit) =>
          hit || fetch(e.request).then((res) => {
            if (res.ok) c.put(e.request, res.clone());
            return res;
          })
        )
      )
    );
    return;
  }

  // Our own app code (/static/) is NETWORK-FIRST: always try the network so a
  // shipped fix lands on the very next refresh, and fall back to the cached copy
  // only when offline. Stale-while-revalidate served the OLD build on the first
  // load after a deploy (the fix landed a load late), which read as "the browser
  // is not loading the new one". Freshness matters more than a few ms here; the
  // large pinned runtime under /vendor/ is what stays cache-first for offline.
  if (url.pathname.startsWith('/static/')) {
    e.respondWith(
      fetch(e.request).then((res) => {
        if (res.ok) caches.open(CACHE).then((c) => c.put(e.request, res.clone()));
        return res;
      }).catch(() => caches.match(e.request))
    );
    return;
  }

  // 🚨 Self-hosted VECTOR basemap (planet.pmtiles): pass straight through, never
  // touch it. The tiles are gzip; a service worker that re-serves them (its own
  // fetch decompresses the body but keeps Content-Encoding: gzip) makes the
  // browser decompress a second time, so MapLibre gets garbage and the map is
  // blank. Same reason /api/ is passed through below.
  if (url.pathname.startsWith('/basemap/')) return;

  // Map tiles: cache-first with a ceiling, so the map has a map under it when
  // there is no signal. Static imagery only - see TILE_CACHE.
  if (url.pathname.startsWith('/api/tile/')) {
    e.respondWith(
      caches.open(TILE_CACHE).then((c) =>
        c.match(e.request).then((hit) =>
          hit || fetch(e.request).then((res) => {
            if (res.ok) {
              c.put(e.request, res.clone());
              // Oldest-first trim. Cache.keys() returns insertion order, so the
              // front of the list is the least recently ADDED - good enough for
              // a bound, and it costs nothing to compute.
              c.keys().then((ks) => {
                if (ks.length > TILE_MAX) {
                  for (let i = 0; i < ks.length - TILE_MAX; i++) c.delete(ks[i]);
                }
              });
            }
            return res;
          })
        )
      )
    );
    return;
  }

  // 🚨 THE SIGHTING API IS NEVER SERVED FROM CACHE, AND THAT IS DELIBERATE.
  // Everything else here is about making the app work without a signal; this is
  // the one place where "works offline" would be a lie with consequences. A
  // cached /api/sightings would draw a patrol car that passed three hours ago
  // onto a live map with no indication it was stale - the map's whole claim is
  // that a dot means a vehicle was seen at that time. Offline, the honest
  // answer is an empty map and a banner saying there is no connection, which is
  // what offline.js shows. Let the request fail.
  if (url.pathname.startsWith('/api/')) return;

  // Network-first for pages; fall back to the cached page offline so the app
  // opens instead of showing the browser's error page.
  e.respondWith(
    fetch(e.request).catch(() =>
      caches.match(e.request).then((hit) => hit || caches.match(appShell(url)))
    )
  );
});

/* 🚨 WHICH SHELL, BECAUSE THERE ARE TWO APPS ON ONE ORIGIN.
 * This used to fall back to '/app' unconditionally, which was harmless while
 * the worker only ever ran for the camera app. The moment the map registers it,
 * that line hands a visitor who opened the MAP with no signal the camera setup
 * page instead - a different app, asking them to point a webcam at a road. A
 * fallback that serves the wrong page is worse than the browser's error page,
 * because the browser's at least says what happened. */
function appShell(url) {
  const p = url.pathname;
  if (p === '/app' || p.startsWith('/app/') || p === '/key' || p === '/aim') {
    return '/app';
  }
  return '/';
}
