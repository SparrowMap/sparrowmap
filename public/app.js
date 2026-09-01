/* RavenMap — the public map.
 *
 * Every vehicle the network has seen, plotted where it was seen, with the
 * snapshot that proves it. Public-tier vehicles carry their plate. Private
 * vehicles carry a rolling daily alias and nothing else, because the server
 * never had their plate to give.
 */

/* RED for anything publicly owned, because that is the entire subject of this
   map. Police and gov are one thing on it - "a publicly owned vehicle doing
   public work on a public road is a public record" was always the argument,
   and the specific agency is a detail the detail panel can carry. Fleet is
   commercial, not government, so it keeps its own colour. */
const COLOR = {
  police:   '#ff3b47',
  gov:      '#ff3b47',
  emergency:'#ff3b47',
  fleet:    '#ffb547',
  civilian: '#55637a',
  unknown:  '#55637a',
};

/* What the map CALLS each class. The internal classification is unchanged -
   classify.py still decides police vs gov on its own evidence, and the detail
   panel still shows which - but the headline on a dot is the category that
   matters publicly. */
/* 🚨 THE MAP IS POLICE-ONLY (core.py public_tiers). His call: "just police
   type vehicles - no regular city work trucks, no ambulance, no firefighters."
   So the headline says what it now means. `gov` and `emergency` keep their
   labels because historical rows and the reviewer's own vocabulary still use
   them - they simply cannot reach the public tier any more. */
const CLASS_LABEL = {
  police:    'Police vehicle',
  gov:       'Government vehicle',
  emergency: 'Emergency vehicle',
  fleet:     'Fleet vehicle',
  civilian:  'Private vehicle',
  unknown:   'Unidentified',
};
const label_for = (v) => CLASS_LABEL[v] || v;

/* Private traffic gets its own colour rather than the civilian slate, which is
   barely separable from the dark basemap - a dot nobody can see does not
   communicate "the road is busy". Still deliberately colourless next to the
   public tiers: it reads as movement, not as an identity. */
const TRAFFIC = '#93a7c4';

/* The ring drawn around every marker so it reads against ANY backdrop.
   Matches --bg, so on empty land the ring is invisible and the dot looks
   exactly as it always did; over a lit road it is what keeps the dot separate.
   ⚠️ Not black: pure black against the near-black basemap would read as a
   deliberate outline where none is wanted. */
const MARKER_HALO = '#0a0d12';

/* How long a private-tier pass stays on the map.
 *
 * Public-tier sightings persist for the whole selected window, because a
 * publicly owned vehicle on a public road is a record. Private traffic is a
 * LIVE VIEW and nothing else: a dot appears as the car passes, fades, and is
 * gone. It is not clickable, it has no detail panel and it leaves nothing
 * behind, which is the same promise the storage layer already makes - the
 * plate was destroyed at the camera and the row expires in 14 days. The map
 * should not imply a persistence the system deliberately does not have. */
const TRAFFIC_FADE_S = 45;   // live view: a pass shows, then fades quickly

/* 0 = no limit, and it is the DEFAULT.
 *
 * A public sighting is a RECORD, kept indefinitely by policy
 * (public_retention_days: 0). Hiding one behind an hour-long window meant the
 * first patrol car this network ever caught was invisible on the map while the
 * header insisted it existed - and the map is the whole point of keeping them.
 *
 * This control never had much to do with private traffic anyway: that fades
 * after TRAFFIC_FADE_S regardless of what is selected here, because it is a
 * live view rather than a record. So the window is, and now says it is, a
 * filter on the public tier. */
const state = {
  filter: 'all',
  windowS: 0,
  // null until the first refresh answers, so the dot keeps saying "connecting"
  // rather than claiming either state before anything has been tried.
  online: null,
  // 🚦 THE WATCHED-ROAD BANDS DEFAULT TO OFF, AND THAT IS A PRESENTATION CALL,
  // NOT A PRIVACY ONE. Nothing about them leaks - a span is deliberately padded
  // so its midpoint does not localise the camera (road.py / SPAN_MIN_M).
  //
  // The problem is that on a map with few red dots, thirty green bands ARE the
  // map, and a visitor reads them as "thirty things detected here" - which is
  // the exact misreading the corridor shape was drawn to prevent. A band means
  // "somebody is watching this stretch", a dot means "a police vehicle passed".
  // When the dots are sparse the bands drown them out and the map appears to say
  // something it is not saying.
  //
  // ⏭️ TURN THIS BACK ON once the map carries enough sightings that the bands
  // read as context behind the dots instead of as the content. The toggle in the
  // header does it, and the choice is remembered - see SHOWCAMS_KEY.
  showCams: false,
  // Town badges default ON - they are how somebody who has just arrived sees
  // that this is a network rather than one street - but they are the first
  // thing in the way when you zoom into a road, so the answer is remembered.
  showPlaces: true,
  // Public traffic cameras: a different kind of coverage from a volunteer's
  // camera, so it gets its own switch. See the checkbox in index.html.
  //
  // 🚨 DEFAULT OFF SINCE THE FLEET REACHED 4,434. At eighteen cameras this was
  // a handful of squares; at four thousand it is four thousand Leaflet markers
  // built on every load, and he reported the map as laggy the moment it was
  // switched on. The layer is worth having and is one click away - it is just
  // not what the map should be doing before anybody asks. See the viewport
  // bound on /api/nodes, which is the other half of this.
  showPubCams: false,
  showPolice: false,          // OpenStreetMap police-station overlay
  showCameras: false,         // OpenStreetMap Flock/ALPR surveillance-camera overlay
  showRadar: false,           // live radar / speed-trap sources from paired detectors
  showDrones: false,          // drones broadcasting Remote ID (paired ESP32/Pi)
  showRadio: false,           // police-radio activity heard by a paired scanner
  // 🚁 TWO AIRCRAFT SWITCHES, NOT ONE, AND THAT IS THE WHOLE POINT.
  //
  // "Government" is a registration category, and most of what falls in it is
  // universities and agricultural agencies - the FAA's own field, not a claim
  // this project makes. Drawn under one "aircraft" toggle they were
  // indistinguishable from a sheriff's helicopter unless you opened the popup,
  // which is exactly backwards for the one question people come here to ask.
  // Separated, "is that police" has an answer you can read off the map.
  showAircraft: false,        // law-enforcement registrations
  showAircraftGov: false,     // other government registrations, and circling
  sightings: new Map(),   // id -> record  (public tier: the records)
  markers: new Map(),     // id -> leaflet marker
  traffic: new Map(),     // id -> {rec, marker}  (private tier: the live view)
  camLayer: L.layerGroup(),
  placeLayer: L.layerGroup(),
  places: null,
  pingLayer: L.layerGroup(),
  trafficLayer: L.layerGroup(),
  trailLayer: L.layerGroup(),
  reportLayer: L.layerGroup(),   // live driver reports (ephemeral, unverified)
  selected: null,
  trackHash: null,
};

/* ---------------------------------------------------------------- map ---- */

/* This opening view is a PLACEHOLDER and is expected to be replaced within a
 * few hundred ms, by whichever of these answers first:
 *
 *   1. the watched spans, once /api/nodes returns  (loadCameras -> fitBounds)
 *   2. `map_center` / `map_zoom` from /api/policy   (applyConfiguredView)
 *
 * It is rounded and region-level because a hardcoded street-level centre in
 * published source is a real camera's neighbourhood, and this file is public.
 * The deployment's actual centre is CONFIG - served, not compiled in. */
/* 🚨 CANVAS, NOT SVG, FOR THE VECTOR LAYERS.
 *
 * Reported: the map goes laggy zooming in and out over Linden, ON A PHONE
 * ONLY. That last part is the diagnosis. Every sighting is an L.circleMarker,
 * and Leaflet's default renderer gives each one its own SVG element - so a
 * zoom is not "redraw some dots", it is a style recalculation and reflow over
 * hundreds of DOM nodes, which a desktop absorbs and a phone does not.
 *
 * preferCanvas draws all of them into one canvas instead. Identical positions,
 * identical colours, no clustering - clustering would have been the other
 * obvious fix and it is the wrong one here, because merging two sightings into
 * "2" invents a claim the map cannot support. Every dot is still exactly where
 * it was; only the machinery underneath changed.
 *
 * ⚠️ Canvas ignores per-path CSS classes, so the ONE vector that depends on a
 * stylesheet - the dashed trail (.trail{stroke-dasharray}) - is pinned back to
 * SVG explicitly where it is created. Icons and labels are DOM markers and are
 * unaffected either way.
 */
const map = L.map('map', { zoomControl: false, attributionControl: true,
                           preferCanvas: true,
                           // Smoother desktop feel: settle at half-zoom steps
                           // and need more wheel travel per level, so scroll
                           // glides instead of jumping a whole level at a time.
                           zoomSnap: 0.5, zoomDelta: 0.5,
                           wheelPxPerZoomLevel: 120 })
  .setView([42.7, -84.5], 8);
// No zoom buttons at all: pinch and scroll zoom the map, and the buttons only
// got in the way - on a phone they sat over the RavenMap logo. `zoomControl:
// false` above removes them. Keep the map sized to its container, because after
// the mobile layout stacks map-over-panel Leaflet renders short and leaves a
// grey gap otherwise.
// Leaflet locks tile geometry to the container size it saw at construction, so
// re-measure whenever the viewport changes shape (rotate, window resize) or
// after first paint. The map's HEIGHT is pure CSS (a fixed share of the phone
// screen); this only tells Leaflet to re-tile into it.
const fixMapSize = () => map.invalidateSize(false);
['load', 'orientationchange', 'resize'].forEach((ev) => addEventListener(ev, fixMapSize));
[200, 600].forEach((t) => setTimeout(fixMapSize, t));

// Publish the header's real height so the fixed About/Transparency panes start
// below it. On a phone the search box wraps onto its own row, making the header
// ~120px, and a hardcoded top hid each pane's heading behind the sticky bar.
const _bar = document.querySelector('header.bar');
const setHeadH = () =>
  document.documentElement.style.setProperty('--headh', Math.round(_bar.getBoundingClientRect().height) + 'px');
['load', 'orientationchange', 'resize'].forEach((ev) => addEventListener(ev, setHeadH));
setHeadH();

// A snapshot that fails to load must not leave a broken-image icon in the list.
// The CSP forbids inline onerror handlers, so catch the error in the capture
// phase (image errors do not bubble) and hide the element.
addEventListener('error', (e) => {
  const t = e.target;
  if (t && t.tagName === 'IMG' && (t.getAttribute('src') || '').startsWith('/snap/')) {
    t.style.display = 'none';
  }
}, true);

/* Same-origin on purpose - see hub.py TILES. The tiles are CARTO's, fetched
 * and cached by the hub, so a viewer's IP and the streets they chose to look
 * at never reach a third party. Attribution is still required and still
 * shown; proxying the bytes does not proxy the credit. */
L.tileLayer('/api/tile/{z}/{x}/{y}.png', {
  attribution: '&copy; OpenStreetMap contributors &copy; CARTO &middot; RavenMap',
  maxZoom: 20,
}).addTo(map);
/* 🗺️ OUR OWN vector basemap. We serve a full-planet Protomaps archive
 * (planet.pmtiles, on the box via `pmtiles serve`) and render it with MapLibre
 * GL through the Leaflet bridge - keeping every Leaflet marker/layer below,
 * only the basemap changes. This ends the dependency on Carto, whose rate-limit
 * "API KEY REQUIRED" placeholder was poisoning tile caches at three layers
 * (see the tile-cache note). The viewer's IP and the streets they look at still
 * never leave our origin. `?raster=1` falls back to the old Carto proxy as an
 * escape hatch if the vector map ever misbehaves on a device. */
// 🗺️ Self-hosted VECTOR basemap is now the DEFAULT. `?raster=1` is the escape
// hatch to the old Carto proxy.
//
// 🚨 THE BLANK-BASEMAP ROOT CAUSE (finally found, 2026-08-27, and it was NEITHER
// the worker NOR the resize the old notes chased): MapLibre gates its ENTIRE
// initial style load behind ONE requestAnimationFrame -
//   Style.loadJSON -> browser.frameAsync -> requestAnimationFrame -> _load(style)
// A map built while the tab is NOT PAINTING - a backgrounded PWA/standalone
// launch, a link opened in a background tab, the split second before first paint -
// never receives that frame. So the style never loads: no sources are created, no
// tile is ever requested, and CRUCIALLY no error and no event fire. It is silent,
// and non-deterministic (any later repaint/resize delivers the missing frame and
// rescues it - which is exactly why it "rendered the whole planet once"). Every
// past console diagnosis that "proved" a bare direct maplibregl.Map fails too was
// itself run in an occluded automation tab where rAF is paused - same trap.
// PROVEN: shimming requestAnimationFrame to a timer renders the whole planet in
// that same hidden tab, styleLoaded true, 15 tiles, zero errors.
//
// THE CURE: guarantee that one frame fires. startFramePump() wraps rAF so each
// callback ALSO gets a short timer fallback (de-duped, so a healthy foreground
// frame is untouched - native rAF wins the race and the timer is a no-op). It is
// active ONLY from just before the map is built until its first frame is drawn,
// then it restores the native rAF so the render loop keeps real vsync timing.
// A watchdog falls back to raster if the vector map ever fails to load, because
// keeping the map VISIBLE outranks the vector upgrade - it is never left blank.

function startFramePump() {
  const rAF = window.requestAnimationFrame.bind(window);
  const cAF = window.cancelAnimationFrame.bind(window);
  let active = true;
  window.requestAnimationFrame = function (cb) {
    let fired = false;
    const once = (t) => { if (fired) return; fired = true; try { cb(t); } catch (e) { /* keep the loop alive */ } };
    const r = rAF(once);
    // 60ms ~ one very slow frame; only ever matters when the native frame is
    // starved (hidden tab / no paint). When rAF is healthy `once` de-dupes it away.
    const s = setTimeout(() => once(performance.now()), 60);
    return { __pump: 1, r, s };
  };
  window.cancelAnimationFrame = function (h) {
    if (h && h.__pump) { cAF(h.r); clearTimeout(h.s); } else { cAF(h); }
  };
  return function stop() {
    if (!active) return;
    active = false;
    window.requestAnimationFrame = rAF;
    window.cancelAnimationFrame = cAF;
  };
}

function addRasterBasemap() {
  L.tileLayer('/api/tile/{z}/{x}/{y}.png', {
    attribution: '&copy; OpenStreetMap contributors &copy; CARTO &middot; SparrowMap',
    maxZoom: 20,
  }).addTo(map);
}

if (!new URLSearchParams(location.search).has('raster')) {
  try {
    // Pump frames across map construction so the rAF-gated style load always runs.
    const stopPump = startFramePump();
    // Never hold the global rAF override longer than a few seconds, whatever happens.
    const hardStop = setTimeout(stopPump, 8000);

    const glLayer = L.maplibreGL({
      style: '/basemap/style.json?v=5',
      attribution: '&copy; OpenStreetMap contributors &middot; SparrowMap',
    }).addTo(map);

    // The bridge builds its maplibregl.Map synchronously inside addTo(), so the
    // handle is available now. Latch a ONE-SHOT "the basemap came up" flag off the
    // first load/idle - and end the pump there. 'load' and 'idle' each fire once the
    // first full frame is on screen; whichever comes first latches basemapUp. The 8s
    // hardStop is the backstop if neither fires (e.g. a tab that stays hidden).
    // ⚠️ Do NOT use isStyleLoaded() for this - it is false during ANY tile load,
    // including a normal zoom, so sampling it later would wrongly declare failure.
    const glMap = (glLayer.getMaplibreMap && glLayer.getMaplibreMap()) || glLayer._glMap;
    // basemapUp latches TRUE the instant MapLibre's style DEFINITION has loaded - the
    // moment the rAF gate we are fighting clears and the vector map is going to render.
    // It never un-latches, so a later zoom (which reloads tiles and makes
    // isStyleLoaded() briefly false) can never make the watchdog think we failed, and a
    // slow phone still streaming tiles is not mistaken for a failure either.
    let basemapUp = false;
    const endPump = () => { clearTimeout(hardStop); stopPump(); };
    if (glMap) {
      glMap.on('styledata', () => { if (glMap.style && glMap.style._loaded) basemapUp = true; });
      glMap.once('load', endPump);
      glMap.once('idle', endPump);
    }

    // 🚨 THE BRIDGE'S resize GAP: leaflet-maplibre-gl's _resize only re-sizes the
    // container DIV and jumpTo()s - it NEVER calls _glMap.resize(). So a container
    // that lays out a beat after the GL map is built (flex column / first paint)
    // leaves MapLibre's internal size stale. Re-size the container AND the GL map
    // whenever #map actually has a size; a ResizeObserver re-syncs on first layout,
    // rotate and panel toggles too.
    const syncSize = () => {
      try {
        if (glLayer._resizeContainer) glLayer._resizeContainer();
        const g = (glLayer.getMaplibreMap && glLayer.getMaplibreMap()) || glLayer._glMap;
        if (g && g.resize) g.resize();
      } catch (e) { /* never let a basemap hiccup take the whole map down */ }
    };
    const mapEl = document.getElementById('map');
    if (window.ResizeObserver && mapEl) new ResizeObserver(syncSize).observe(mapEl);
    map.whenReady(syncSize);

    // 🛡️ WATCHDOG: keeping the map visible outranks the vector upgrade. If the
    // basemap NEVER came up (basemapUp still false after 8s - some environment the
    // pump did not foresee), drop it and fall back to the Carto raster proxy so it is
    // never blank. Keyed off the latched basemapUp flag, NOT isStyleLoaded(), so a
    // user zooming (which reloads tiles) can never trip it once the map is working.
    setTimeout(() => {
      if (basemapUp) return;
      try {
        if (glLayer && map.hasLayer(glLayer)) map.removeLayer(glLayer);
        addRasterBasemap();
      } catch (e) { try { addRasterBasemap(); } catch (_) { /* give up quietly */ } }
    }, 8000);
  } catch (e) {
    // Anything at all goes wrong standing up the vector map -> raster, immediately.
    addRasterBasemap();
  }
} else {
  addRasterBasemap();
}

state.camLayer.addTo(map);
// The camera layer is the only one whose SHAPE depends on the zoom - a span
// that is a corridor at z17 is a dot at z10 (see drawSpans). Redraw it on
// zoom, not on every move: panning cannot change which side of the legibility
// floor a span falls on, and redrawing on move would rebuild 30 layers per
// frame while dragging.
// showCams is checked here too: without it, zooming would put the cameras back
// on a map the visitor had switched them off on.
map.on('zoomend', () => {
  if (state.showCams && state.spans) drawSpans(state.spans);
  drawPlaces();
});
state.placeLayer.addTo(map);
state.trafficLayer.addTo(map);
state.trailLayer.addTo(map);
state.pingLayer.addTo(map);
state.reportLayer.addTo(map);

/* ------------------------------------------------------------- helpers --- */

const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

function ago(ts) {
  const d = Date.now() / 1000 - ts;
  if (d < 60) return `${Math.max(0, Math.round(d))}s`;
  if (d < 3600) return `${Math.round(d / 60)}m`;
  if (d < 86400) return `${Math.round(d / 3600)}h`;
  return `${Math.round(d / 86400)}d`;
}

/* 🚨 BUCKET `since` SO THE REQUEST URL IS STABLE, OR THE EDGE CACHE NEVER HITS.
   /api/sightings is short-cacheable, but a `since` of Date.now() changes every
   call, so every request would be a unique URL and Cloudflare could cache none
   of it. Rounding down to a fixed bucket means every viewer in the same window
   requests the IDENTICAL url, so the origin serves it once and the edge serves
   the crowd. The bucket matches the server's max-age (15s). */
const CACHE_BUCKET_S = 4;   // live view: new passes appear within a few seconds
const bucketed = (sec) => Math.floor(sec / CACHE_BUCKET_S) * CACHE_BUCKET_S;

/* The oldest timestamp the window admits. 0 means no limit - decided here
   once, because the same rule is needed at four call sites and a constant
   re-derived in four places is a constant that will eventually disagree. */
const windowCut = () => state.windowS ? bucketed(Date.now() / 1000 - state.windowS) : 0;

/* How the window is NAMED wherever a count is printed beside it. Kept next to
   windowCut for the same reason windowCut exists: the header, the panel and the
   hint all describe the same window, and a label re-derived per call site is a
   label that will eventually disagree with the number it sits next to.
   ⚠️ Keys must match the <option value>s in index.html. */
const WINDOW_LABEL = {
  0: 'all time', 300: '5m', 3600: '1h',
  21600: '6h', 86400: '24h', 604800: '7d',
};

/* How many public sightings the map is DRAWING right now.
   state.sightings holds public rows only (load() fills it from the
   vclass=public fetch), so this is the window applied to that set - the same
   filter drawAll and renderList use, and deliberately NOT the chip filter,
   which hides rows rather than changing how many exist. */
const publicInWindow = () => {
  const cut = windowCut();
  return [...state.sightings.values()].filter((s) => s.ts > cut).length;
};

/* The public fetch's row cap, named because loadStats has to know whether a
   count landed on it and is therefore a floor rather than a total. */
const PUBLIC_LIMIT = 2000;

const isPublic = (s) => s.tier === 'public';
const label = (s) => isPublic(s) ? (s.plate_text || '—')
  : `private · ${(s.plate_hash || '').slice(2, 8)}`;

/* Public-tier dots hold their weight for the whole window - they are records.
   The visual hierarchy is the argument: this map is about who is watching, not
   about whoever drove past. */
function pingStyle(s) {
  // With no window there is nothing to fade against, so records stay solid.
  const age = state.windowS
    ? Math.min(1, (Date.now() / 1000 - s.ts) / state.windowS) : 0;
  return {
    radius: 7,
    // 🚨 A DARK RING, NOT THE CLASS COLOUR AGAIN.
    //
    // The stroke used to be the same colour as the fill, which made every dot's
    // legibility depend entirely on what was underneath it. That was survivable
    // while the basemap was uniformly near-black, and it stopped being
    // survivable the moment the basemap was brightened so the roads could be
    // seen at all: measured, a fleet marker on a lit road casing came out at
    // 1.07:1, and a sighting sits ON a road by definition.
    //
    // The two requirements pull against each other - brighter roads, visible
    // dots - and tuning brightness alone cannot satisfy both: the best balance
    // available left BOTH at about 2.3:1, under the 3.0 bar. A ring breaks the
    // tie instead of splitting it, because the dot is then separated from
    // whatever it sits on by a colour of its own.
    //
    // ⚠️ THE FILL STILL CARRIES THE MEANING. Red is still a publicly owned
    // vehicle; nothing about identity moved into the stroke.
    color: MARKER_HALO,
    fillColor: COLOR[s.vclass] || COLOR.unknown,
    fillOpacity: 0.9 * (1 - age * 0.45),
    opacity: 1 - age * 0.35,
    weight: 1.5,
  };
}

/* Whether a PUBLIC-tier record is drawn and listed.
 *
 * Private traffic never reaches here - drawSighting hands it to drawTraffic
 * first - so "traffic" is expressed as "no public record passes", which is
 * exactly what it means: the live road with the records taken off it. */
function passes(s) {
  if (state.filter === 'none') return false;   // hide every vehicle marker
  if (state.filter === 'traffic') return false;  // live passes only
  if (state.filter === 'all') return true;
  return s.vclass === state.filter;
}

/* True when the current filter is capable of showing a public record at all.
 * Used to keep the "N more in the last 24h" nudge quiet when the records are
 * hidden BY CHOICE - offering to widen the window is no help when the window
 * is not what is hiding them, and it reads as the map arguing with itself. */
const filterShowsPublic = () =>
  state.filter !== 'none' && state.filter !== 'traffic';

/* ------------------------------------------------------------- markers --- */

function drawSighting(s) {
  // "None" hides vehicle dots entirely - private live traffic included, since it
  // is a vehicle marker too. The watched-roads tickbox is independent, so None
  // + roads-on shows just the road lines.
  if (state.filter === 'none') return;
  if (!isPublic(s)) return drawTraffic(s);

  state.sightings.set(s.id, s);
  const old = state.markers.get(s.id);
  if (old) state.pingLayer.removeLayer(old);
  if (!passes(s)) { state.markers.delete(s.id); return; }

  const m = L.circleMarker([s.lat, s.lon], pingStyle(s));
  m.on('click', () => snapTo(s.id));
  m.addTo(state.pingLayer);
  state.markers.set(s.id, m);
}

/* ------------------------------------------------------- live traffic ---- */

/* A private pass. Deliberately inert: no click handler, no tooltip, no entry
   in the list, no id anyone can look up. It exists to show that the road is
   busy, which is the honest sum of what the system is allowed to know about
   it, and then it disappears. */
function drawTraffic(s) {
  if (state.traffic.has(s.id)) return;
  const m = L.circleMarker([s.lat, s.lon], {
    // Same ring as the public dots, and for the same reason - these are the
    // markers most likely to be sitting on a lit road, because live traffic is
    // nothing but vehicles on roads.
    radius: 5, color: MARKER_HALO, fillColor: TRAFFIC,
    fillOpacity: 0.8, opacity: 0.95, weight: 1,
    interactive: false,        // unclickable, not just click-does-nothing
  }).addTo(state.trafficLayer);
  state.traffic.set(s.id, { rec: s, marker: m });
}

/* How many vehicles are crossing a camera right now.
 *
 * ⚠️ ONE DEFINITION, THREE READOUTS. This number is printed in the stats row,
 * beside the live dot, and in the panel's traffic bar. They were computed
 * separately - two of them counted the live set, the third filtered it to the
 * last 60 seconds - and a filter at 60s over a set that is reaped at 45s can
 * never remove anything, so the third was the same number written a longer way.
 * Three copies of one figure is three chances for them to drift apart and for
 * the map to be seen disagreeing with itself, which costs more trust than the
 * figure earns. So: one function, and it is the size of the live set by
 * definition. If the fade window changes, every readout follows it. */
const movingNow = () => state.traffic.size;

/* The live dot carries two things at once: whether the last refresh worked, and
 * the same count as everything else. They are decided by different timers -
 * connection state on the 4s refresh, the count on the 1s reap - so writing the
 * text from whichever fired last had the dot reading "live · 25 passing" beside
 * a header saying "24 moving now". One definition was not enough; they also
 * have to be PAINTED from the same tick. This is the only writer, and both
 * timers call it. */
function paintLive() {
  const dot = $('#live');
  if (!dot || state.online === null) return;   // still saying "connecting"
  // The passing count lives in its own #livecount span so the phone header can
  // hide it (it is already shown in the traffic bar) and let LIVE share a row
  // with the What's-new / Sign in / bug controls instead of pushing them down.
  const msg = $('#livemsg'), cnt = $('#livecount');
  if (!state.online) {
    if (msg) msg.textContent = 'reconnecting';
    if (cnt) cnt.textContent = '';
    return;
  }
  if (msg) msg.textContent = 'live';
  const n = movingNow();
  if (cnt) cnt.textContent = n ? ` · ${n} passing` : '';
}

/* One timer fades and reaps every traffic dot. Per-dot timers would mean
   hundreds of them on a busy road, all firing independently. */
function ageTraffic() {
  const t = Date.now() / 1000;
  for (const [id, e] of state.traffic) {
    const a = (t - e.rec.ts) / TRAFFIC_FADE_S;
    if (a >= 1) {
      state.trafficLayer.removeLayer(e.marker);
      state.traffic.delete(id);
      continue;
    }
    // Bright and full-size as it passes, then thinning away to nothing. The
    // curve is deliberately back-loaded so a fresh pass reads as an event
    // rather than as one more faint dot among the dying ones.
    const k = Math.pow(1 - a, 0.6);
    e.marker.setStyle({ fillOpacity: 0.8 * k, opacity: 0.95 * k,
                        radius: 5 * (0.45 + 0.55 * k) });
  }
  const n = movingNow();
  const el = $('#traffic');
  if (el) {
    el.textContent = n ? `${n} passing now` : 'road quiet';
    el.classList.toggle('busy', n > 0);
  }
  // The stats row is rewritten wholesale every 3s by loadStats, which renders
  // this figure as it stands at that moment; this keeps it moving in between.
  // Updating the one element rather than the row means the count ticks without
  // the rest of the header flickering.
  const mv = $('#movingnow');
  if (mv) {
    mv.textContent = n.toLocaleString();
    mv.classList.toggle('on', n > 0);
  }
  paintLive();
  emptyState();
}

function redrawAll() {
  state.pingLayer.clearLayers();
  state.markers.clear();
  // None also clears the live-traffic layer (its dots are added outside this
  // pass and would otherwise linger until they faded on their own).
  // "Traffic" deliberately does NOT clear it - those dots are the view.
  if (state.filter === 'none') {
    state.trafficLayer.clearLayers();
    state.traffic.clear();
  }
  const cut = windowCut();
  [...state.sightings.values()]
    .filter((s) => s.ts > cut)
    .sort((a, b) => a.ts - b.ts)
    .forEach(drawSighting);
  renderList();
}

/* --------------------------------------------------------------- panel --- */

function renderList() {
  const cut = windowCut();
  let rows = [...state.sightings.values()].filter((s) => s.ts > cut && passes(s));
  if (state.trackHash) rows = rows.filter((s) => s.plate_hash === state.trackHash);
  rows.sort((a, b) => b.ts - a.ts);

  // Public tier only. A private pass has no identifier, no detail page and no
  // trail, so a list row for it would be a row you cannot click carrying
  // nothing you can read - and listing them alongside records implies the
  // system holds something on them that it does not.
  $('#listtitle').textContent = state.filter === 'traffic'
    ? 'Live traffic'
    : state.trackHash
    ? `Trail · ${rows.length} sightings`
    // "sightings", not "vehicles" - the list has one row per PASS, and
              // without a plate there is no way to know how many vehicles that
              // is. Calling it vehicles here while the header honestly says
              // "-- distinct vehicles" would put the contradiction back.
    : `Public sightings · ${rows.length}`;
  $('#clearsel').classList.toggle('hidden', !state.trackHash);

  // ⚠️ THE HEADER AND THE PANEL WERE CONTRADICTING EACH OTHER.
  // The header counts public sightings over 24h; the panel and the map only
  // ever show the selected window, which defaults to one hour. So the first
  // patrol car this camera ever caught read as "1 public sightings" up top and
  // "0" in the panel with nothing on the map, and the only way to reconcile
  // that was to know how the window works. A public sighting is rare and it is
  // a record - if one exists and the window is hiding it, the map should say
  // so and offer to widen rather than leave a contradiction on screen.
  const el = $('#windowhint');
  const hidden = (lastStats?.public_24h || 0) - rows.length;
  if (el) {
    const show = !state.trackHash && hidden > 0 && state.windowS !== 0
      && filterShowsPublic();
    el.style.display = show ? '' : 'none';
    if (show) {
      el.innerHTML = `${hidden} more public sighting${hidden === 1 ? '' : 's'}
        in the last 24h &mdash; <button class="ghost" id="widen">show everything</button>`;
      // Same rule as the window dropdown: this changes the window, so the
      // header's count has to be repainted with it rather than waiting on the
      // 30s timer.
      $('#widen').onclick = async () => {
        $('#window').value = '0';
        state.windowS = 0;
        await load();
        loadStats();
      };
    }
  }

  // Under "Traffic" the list is empty BY DESIGN, and an empty list under a
  // map full of moving dots reads as broken. Say why instead: a private pass has
  // no plate, no id and no detail page, so there has never been anything to put
  // in a row - which is the same point the tier itself is making.
  if (state.filter === 'traffic') {
    $('#list').innerHTML = `<li class="note">Private passes are not listed.
      There is no plate, no id and no detail to show &mdash; the dot on the map
      is the whole of what this system is allowed to know about one, and it
      fades after ${TRAFFIC_FADE_S} seconds.</li>`;
    return;
  }

  // 🚨 SKIP the rebuild when the visible rows and selection are unchanged.
  // renderList runs on every ~4s poll; rebuilding innerHTML recreated every
  // <img class="thumb">, so each poll re-fetched EVERY snapshot - a constant
  // thumbnail-reload loop (visible flicker + network spam). The signature
  // covers everything the markup depends on (id, snapshot, class, plate,
  // selection); relative times refresh on the next real change, which is a fine
  // trade for not reloading the whole list four times a minute.
  const shown = rows.slice(0, 300);
  // NOTE: relative time (ago) is deliberately NOT in the signature - including it
  // would rebuild (and reload every thumbnail) every time a "2m" ticked to "3m".
  // Times refresh whenever the row set, class, snapshot or selection changes.
  // selection is NOT in the signature - moving the highlight (highlightSelected)
  // must not trigger a rebuild, or every tap would reset the list scroll.
  const sig = shown.map((s) => s.id + ':' + (s.snap || '') + ':' + s.vclass
    + ':' + (s.plate_text || '')).join(',');
  if (sig === renderList._sig) return;
  renderList._sig = sig;

  // preserve scroll across a rebuild so a new sighting doesn't jump you to the top
  const _list = $('#list');
  const _scroll = _list.scrollTop;
  _list.innerHTML = shown.map((s) => `
    <li data-id="${s.id}" class="${s.id === state.selected ? 'sel' : ''}">
      <i class="sw" style="background:${COLOR[s.vclass] || COLOR.unknown}"></i>
      ${s.snap ? `<img class="thumb" loading="lazy" src="/snap/${encodeURIComponent(s.snap)}" alt="">` : ''}
      <div class="who">
        <b class="${isPublic(s) ? '' : 'priv'}" style="${isPublic(s)
          ? 'color:' + (COLOR[s.vclass] || COLOR.unknown) : ''}">${
          isPublic(s) && !s.plate_text ? esc(label_for(s.vclass)) : esc(label(s))}</b>
        <span>${esc(s.color || '')} ${esc(s.body || '')} · ${esc(s.node_id)}</span>
      </div>
      <div class="when">${ago(s.ts)}</div>
      <button class="li-detail" data-detail="${s.id}" aria-label="Open full details"
        title="Open full details">&#10530;</button>
    </li>`).join('');
  _list.scrollTop = _scroll;
}

$('#list').addEventListener('click', (e) => {
  const li = e.target.closest('li');
  if (!li || li.classList.contains('note')) return;
  const id = Number(li.dataset.id);
  // the ⤢ button opens the full card; tapping the row just snaps the map + bubble
  if (e.target.closest('[data-detail]')) openDetail(id);
  else snapTo(id);
});

// 🚨 Move the selection highlight WITHOUT rebuilding the list. renderList sets
// #list.innerHTML, which resets the scroll to the top - so selecting a sighting
// far down the list (then closing it) used to throw you back to the top and you
// lost your place. Toggling the .sel class in place leaves the scroll untouched.
function highlightSelected() {
  const list = $('#list');
  if (!list) return;
  list.querySelectorAll('li.sel').forEach((li) => li.classList.remove('sel'));
  if (state.selected != null) {
    const li = list.querySelector(`li[data-id="${state.selected}"]`);
    if (li) li.classList.add('sel');
  }
}

function closeDetail() {
  state.selected = null;
  $('#detail').classList.add('hidden');
  const bg = $('#detailbg'); if (bg) bg.hidden = true;
  // A trail drawn from the detail panel belongs to the detail panel; leaving
  // it on the map after closing leaves a line nobody can explain or remove.
  if (state.trackHash) {
    state.trackHash = null;
    state.trailLayer.clearLayers();
  }
  highlightSelected();
}
// Tapping the scrim behind the popup closes it, same as Back.
document.getElementById('detailbg')?.addEventListener('click', closeDetail);

// Escape gets you out of anything.
addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && state.selected != null) closeDetail();
});

/* Public correction. Anyone can flag a published sighting - "that is an SUV, not
   a motorcycle", "that is not a government vehicle". The flag does NOT change the
   map; it drops the sighting into the operator's review queue, where a human
   confirms or retracts it. That is the whole trust model: correctable by anyone,
   editable only after a human agrees.

   🚨 WITH ONE EXCEPTION, AND IT IS THE POINT OF THE FOURTH BUTTON.
   "Shows a person" is not a claim about the vehicle, it is a claim about
   somebody who never asked to be in the photograph - an arm on the wheel of the
   car alongside, a face through a window, a watch. Waiting for review is fine
   when the cost of being wrong is a mislabelled truck; it is not fine when the
   cost accrues to a person for every hour the queue is behind. That reason
   takes the PICTURE down immediately (the sighting stays), and a reviewer then
   crops the person out and puts it back. See review_api.hold_photo. */
function openReport(id) {
  const box = $('#reportbox');
  box.classList.remove('hidden');
  box.innerHTML = `
    <div class="rlabel">What looks wrong here?</div>
    <div class="rreasons">
      <button class="btn alt" data-r="not_government">Not a government vehicle</button>
      <button class="btn alt" data-r="wrong_description">Wrong description</button>
      <button class="btn alt" data-r="privacy">Shows a person or private detail</button>
      <button class="btn alt" data-r="other">Something else</button>
    </div>
    <textarea id="rnote" maxlength="500" rows="2"
      placeholder="Add a detail (optional), e.g. this is an SUV, not a motorcycle"></textarea>
    <div class="acts">
      <button class="btn" id="rsend" disabled>Send to review</button>
      <button class="btn alt" id="rcancel">Cancel</button>
    </div>
    <div id="rmsg" class="rmsg"></div>`;

  let reason = null;
  box.querySelectorAll('.rreasons .btn').forEach((b) => {
    b.onclick = () => {
      box.querySelectorAll('.rreasons .btn').forEach((x) => x.classList.remove('on'));
      b.classList.add('on');
      reason = b.dataset.r;
      $('#rsend').disabled = false;
      // Ask for the one detail that makes a privacy flag actionable. A reviewer
      // cropping a person out is looking at a small photo and guessing which
      // part to cut; "the arm on the left" turns that into one drag.
      const note = $('#rnote');
      if (note) {
        note.placeholder = reason === 'privacy'
          ? 'Whereabouts in the picture? e.g. the arm and watch on the left'
          : 'Add a detail (optional), e.g. this is an SUV, not a motorcycle';
      }
    };
  });
  const done = () => { box.classList.add('hidden'); box.innerHTML = ''; };
  $('#rcancel').onclick = done;
  $('#rsend').onclick = async () => {
    if (!reason) return;
    $('#rsend').disabled = true;
    try {
      const res = await fetch('/api/report', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id, reason, note: ($('#rnote').value || '').slice(0, 500) }),
      });
      if (res.ok) {
        // 🚨 THE MESSAGE HAS TO MATCH WHAT ACTUALLY HAPPENED. "Nothing on the
        // map changes until they do" is true of every other reason and FALSE of
        // a privacy flag, which pulls the photograph immediately - and telling
        // somebody who just reported their own arm that nothing has changed yet
        // is the worst possible answer to give them. The server reports what it
        // did (`held`) rather than the page assuming from the reason it sent.
        let held = false;
        try { held = !!(await res.json()).held; } catch { held = false; }
        box.innerHTML = held
          ? '<div class="rmsg ok">Thanks. <b>The photo has been taken off the map '
            + 'straight away</b> while a person looks at it. The sighting itself '
            + 'stays; the picture comes back only cropped, or not at all.</div>'
          : '<div class="rmsg ok">Thanks. This was sent to the review '
            + 'queue for a person to check. Nothing on the map changes until they do.</div>';
        setTimeout(done, 6000);
        // The picture is gone from the server: drop it from the open panel too,
        // rather than leaving the flagged image on screen behind the receipt.
        if (held) { const im = document.querySelector('#detail img'); if (im) im.remove(); }
      } else {
        $('#rmsg').textContent = 'Could not send that. Please try again.';
        $('#rsend').disabled = false;
      }
    } catch {
      $('#rmsg').textContent = 'Could not send that. Please try again.';
      $('#rsend').disabled = false;
    }
  };
}

/* Full-screen view of one published photo.
   Built with DOM calls rather than innerHTML because the CSP forbids inline
   anything, and pinned to the viewport so a phone shows the crop as large as
   the screen allows. image-rendering is left alone deliberately - smoothing a
   200px crop invents detail that is not in the evidence. */
function openLightbox(s) {
  const ov = document.createElement('div');
  ov.className = 'lightbox';
  const img = document.createElement('img');
  img.src = `/snap/${encodeURIComponent(s.snap)}`;
  img.alt = 'snapshot, enlarged';
  const cap = document.createElement('div');
  cap.className = 'lbcap';
  cap.textContent = `${label_for(s.vclass)} · ${new Date(s.ts * 1000).toLocaleString()}`;
  const close = document.createElement('button');
  close.className = 'lbclose';
  close.textContent = '✕';
  close.setAttribute('aria-label', 'Close');
  const shut = () => { ov.remove(); removeEventListener('keydown', esc); };
  const esc = (e) => { if (e.key === 'Escape') shut(); };
  close.onclick = shut;
  // Tapping the backdrop closes; tapping the photo itself must not, or you
  // dismiss the thing you opened while trying to look at it.
  ov.onclick = (e) => { if (e.target === ov) shut(); };
  addEventListener('keydown', esc);
  ov.appendChild(img); ov.appendChild(cap); ov.appendChild(close);
  document.body.appendChild(ov);
}

// 🚨 Clicking a sighting row (or a map dot) SNAPS the map to it and drops a
// thumbnail bubble on the map - it does NOT open the big card. The bubble, or
// the row's ⤢ button, opens the full detail (openDetail). Two levels of intent:
// "show me where" vs "tell me everything".
function snapTo(id) {
  const s = state.sightings.get(id);
  if (!s) { openDetail(id); return; }   // not cached - just open the card
  state.selected = id;
  highlightSelected();
  const m = state.markers.get(id);
  if (m) m.bringToFront();
  // 🚨 CENTRE IT DETERMINISTICALLY. `{animate:true}` here + the bubble popup's
  // own autoPan raced each other: the FIRST tap (which also changes zoom) landed
  // centred, but every SUBSEQUENT same-zoom tap left the sighting ~half a map off
  // to a corner (measured: 663px, then 0px after a plain setView). An instant,
  // non-animated setView is the authoritative centre, and the popup below no
  // longer pans, so nothing shoves it afterwards. Verified centred to the pixel.
  map.setView([s.lat, s.lon], Math.max(map.getZoom(), 14), { animate: false });
  showBubble(s);
  bringMapIntoView();
}

// After tapping a row in the sightings list, make sure the map you just centred
// is actually in front of you. On the phone layout the WHOLE PAGE scrolls (the
// map is a 56vh band at the top, the list flows below it), so tapping a row far
// down the list re-centres a map that has scrolled off the top - the sighting
// moves to the map's centre, but the map isn't on screen, so it "shows but isn't
// centred". Leaflet's own popup auto-pan only pans WITHIN the map div, which does
// not help when that div is above the viewport. Scroll it back under the sticky
// header. A no-op on desktop, where `main` is absolutely positioned and never
// scrolls, so the map is always in view.
function bringMapIntoView() {
  const mapEl = document.getElementById('map');
  if (!mapEl) return;
  const headh = parseInt(getComputedStyle(document.documentElement)
                  .getPropertyValue('--headh')) || 0;
  const b = mapEl.getBoundingClientRect();
  if (b.top >= headh - 1 && b.bottom <= innerHeight + 1) return;  // already fully visible
  // scroll-margin-top so scrollIntoView lands the map BELOW the sticky header
  // instead of behind it (the header covers the top --headh px when scrolled).
  mapEl.style.scrollMarginTop = (headh + 4) + 'px';
  mapEl.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function showBubble(s) {
  const cap = esc(isPublic(s) && !s.plate_text ? label_for(s.vclass) : label(s));
  // The thumbnail is ZOOMED (see .bubble .bub-img in shell.css) so the navy
  // masked border the isolator paints around the vehicle is cropped out - the
  // bubble shows the vehicle, not the frame.
  const img = s.snap
    ? `<span class="bub-img"><img src="/snap/${encodeURIComponent(s.snap)}" alt=""></span>`
    : '';
  const html = `<div class="bubble" data-detail="${s.id}" role="button" tabindex="0"
      title="Open full details">${img}<span class="bub-cap">${cap}<em>tap for details</em></span></div>`;
  // 🚨 autoPan:false — snapTo has ALREADY centred the sighting, and Leaflet's
  // popup autoPan would pan the map again to fit the bubble, knocking the
  // sighting off the centre it was just placed at (this was the "only the first
  // click centres" bug). The bubble opens above an already-centred point, so it
  // is on-screen without any auto-panning.
  const p = L.popup({ className: 'sight-bubble', closeButton: true, maxWidth: 240,
                      autoPan: false, offset: [0, -4] })
    .setLatLng([s.lat, s.lon]).setContent(html).openOn(map);
  const el = p.getElement && p.getElement();
  const b = el && el.querySelector('.bubble[data-detail]');
  if (b) {
    const open = () => openDetail(Number(b.dataset.detail));
    b.addEventListener('click', open);
    b.addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); open(); }
    });
  }
}

async function openDetail(id) {
  state.selected = id;
  const s = state.sightings.get(id) || await (await fetch(`/api/sighting/${id}`)).json();
  state.sightings.set(id, s);

  const pub = isPublic(s);
  const conf = s.vclass_conf != null ? `${Math.round(s.vclass_conf * 100)}%` : '—';

  $('#detail').innerHTML = `
    ${s.snap ? `<div class="shotwrap">
      <img src="/snap/${encodeURIComponent(s.snap)}" alt="snapshot" id="snapImg">
      <button class="viewbtn" id="btnBigger" title="View this photo larger"
        aria-label="View this photo larger">⤢ View</button>
    </div>` : ''}
    <div class="plate ${pub ? '' : 'priv'}">${esc(label(s))}</div>
    <div class="kv">
      <span>class</span><b style="color:${COLOR[s.vclass] || COLOR.unknown}">${
        esc(label_for(s.vclass))} · ${conf}</b>
      <span>classified</span><b>${esc(s.vclass)}</b>
      <span>seen</span><b>${new Date(s.ts * 1000).toLocaleString()}</b>
      <span>camera</span><b>${esc(s.node_id)}</b>
      <span>vehicle</span><b>${esc(s.color || '?')} ${esc(s.body || '')}</b>
      <span>heading</span><b>${s.heading != null ? Math.round(s.heading) + '°' : '—'}</b>
      <span>signed</span><b>${s.sig_ok ? 'yes' : 'no'}</b>
      ${s.detections > 1 ? `<span>detections</span><b title="The tracker saw this
        vehicle as several separate tracks while it crossed the frame. They are
        one pass, folded together.">${s.detections} merged</b>` : ''}
    </div>
    <div class="why"><b>Why this class:</b> ${esc(s.vclass_why || 'no signals recorded')}</div>
    <!-- 🚨 A LINK BETWEEN SIGHTINGS IS A GUESS AND MUST READ AS ONE.
         "Possibly the same vehicle", never "unit 4021" - and the reason is
         printed next to it so a reader can disagree with the machine rather
         than take its word. A link nobody can check is an assertion with
         extra steps. Only published police and government rows can carry one;
         db.tag_sighting refuses everything else. -->
    ${s.vehicle_tag ? `<div class="why"><b>Possibly the same vehicle</b> as
      other sightings tagged <code>${esc(s.vehicle_tag)}</code>.
      ${esc(s.tag_why || '')} <i>This is inferred, not confirmed.</i></div>` : ''}
    ${pub ? '' : `<div class="why">This vehicle is private tier. Its plate was
      hashed at the camera and never stored, so there is no plate to show and
      no way to search for it. The identifier above is a rolling alias that
      changes daily.</div>`}
    <!-- Filled in async by the scanner lookup below; absent until it answers,
         because a dead link is worse than no link. -->
    <div class="why scanner hidden" id="scannerRow"></div>
    <div class="acts">
      <button class="btn" id="btnTrail">${pub ? 'Show trail' : 'Show today’s trail'}</button>
      <button class="btn alt" id="btnCenter">Centre</button>
      ${pub ? '<button class="btn alt" id="btnReport">Report a problem</button>' : ''}
      <button class="btn alt" id="btnClose" title="Back to the list">Back</button>
    </div>
    <div id="reportbox" class="reportbox hidden"></div>`;
  $('#detail').classList.remove('hidden');

  $('#btnCenter').onclick = () => map.setView([s.lat, s.lon], 17);

  /* 🔍 VIEW THE PHOTO LARGER.
     The published crop is at most 200px on its long edge and the panel shows it
     smaller still, so on a phone the vehicle is a smudge - you cannot check the
     claim the map is making, which is the one thing a viewer should always be
     able to do. Opening it full-screen does not add a single pixel of detail;
     it just stops the layout throwing away the ones that are there. */
  const big = $('#btnBigger');
  if (big) big.onclick = () => openLightbox(s);

  /* Where to LISTEN, for the place this sighting is in.
   *
   * 🚨 A LINK, NEVER A STREAM. Broadcastify allows a feed OWNER to embed their
   * OWN feed and forbids becoming a redistribution layer, so we send people to
   * their site rather than proxying their audio. Receiving unencrypted
   * public-safety radio is legal; rebroadcasting someone else's stream is a
   * contract question and the answer is no.
   *
   * State level on purpose: their county ids are opaque internal numbers, so a
   * deep link would sometimes name the wrong county - and being wrong about
   * which county is listening to whom is not a small error on this map.
   * Rendered only once it resolves, so a lookup failure leaves no dead link. */
  fetch(`/api/scanner?lat=${s.lat}&lon=${s.lon}`)
    .then((r) => r.json())
    .then((d) => {
      const row = $('#scannerRow');
      if (!row || !d || !d.ok || !d.url) return;
      const where = d.county ? `${d.county}, ${d.state}` : d.state;
      row.innerHTML = `<b>Listen:</b> public-safety radio for
        ${esc(where)} is carried on
        <a href="${esc(d.url)}" target="_blank" rel="noopener noreferrer">Broadcastify</a>.
        <span class="sub">Someone else's service, not ours. Many agencies are
        encrypted, so a feed may not exist.</span>`;
      row.classList.remove('hidden');
    })
    .catch(() => { /* no link is fine; a broken one is not */ });
  $('#btnTrail').onclick = () => showTrail(s.plate_hash);
  if (pub) $('#btnReport').onclick = () => openReport(s.id);
  // There was no way out of the detail panel once it opened - it covered the
  // list and stayed until another sighting was clicked. A view you can enter
  // and not leave is a dead end.
  $('#btnClose').onclick = closeDetail;

  const m = state.markers.get(id);
  if (m) { m.bringToFront(); map.panTo([s.lat, s.lon]); }
  // The detail is a centered popup over a scrim now (not inline in the sidebar),
  // so the list keeps its scroll and its place. Show the scrim, move the
  // highlight without rebuilding the list.
  const bg = $('#detailbg'); if (bg) bg.hidden = false;
  highlightSelected();
}

/* ---------------------------------------------------------------- trail -- */

async function showTrail(hash) {
  if (!hash) return;
  const rows = await (await fetch(`/api/track/${encodeURIComponent(hash)}`)).json();
  state.trailLayer.clearLayers();
  if (!rows.length) return;

  state.trackHash = hash;
  const pts = rows.map((r) => [r.lat, r.lon]);
  const col = COLOR[rows[0].vclass] || COLOR.unknown;

  // Pinned to SVG: its dashes come from .trail in the stylesheet, and a
  // canvas path has no class for CSS to reach.
  L.polyline(pts, { color: col, weight: 2, opacity: 0.75, className: 'trail',
                    renderer: L.svg() })
    .addTo(state.trailLayer);
  rows.forEach((r, i) => {
    L.circleMarker([r.lat, r.lon], {
      radius: i === rows.length - 1 ? 6 : 3.5, color: col, fillColor: col,
      fillOpacity: 0.9, weight: 1,
    }).on('click', () => openDetail(r.id)).addTo(state.trailLayer);
  });
  map.fitBounds(L.latLngBounds(pts).pad(0.25));

  const ps = rows[0].patrol_score;
  if (ps != null && ps > 0.55) {
    $('#detail').insertAdjacentHTML('beforeend',
      `<div class="why" style="border-color:${COLOR.police}">
         <b>Patrol-shaped movement (${Math.round(ps * 100)}%).</b> Many passes,
         spread across the clock, reversing over the same stretch. Nobody's
         commute looks like this.</div>`);
  }
  renderList();
}

$('#clearsel').onclick = () => {
  state.trackHash = null;
  state.trailLayer.clearLayers();
  renderList();
};

/* -------------------------------------------------------------- cameras -- */

let fittedOnce = false;
let _geoLoc = null, _spanBounds = null, _userMovedMap = false;
map.on('dragstart zoomstart', () => { _userMovedMap = true; });

// Choose the opening view. If there are watched roads AND the visitor is near
// them (<60 km), show the roads - the data is the point. If the visitor is far,
// open on THEIR own city instead of yanking them to another state's cameras.
// With no cameras at all, open on their city. Runs when geolocation resolves or
// the cameras load, whichever is last, and never fights a visitor who has
// already panned. Deliberately NO IP lookup - RavenMap does not send anyone's
// location to a geo service to guess where they are; the browser asks, once.
/* 🚨 A FLORIDA VOLUNTEER OPENED THE MAP AND GOT LANSING, MICHIGAN.
 *
 * Reported, and it is exactly what the code did. The map is created at
 * [42.7, -84.5] - a hardcoded start that is this operator's own state - and
 * chooseView() was the only thing that ever moved it. It could not:
 *
 *   - `_spanBounds` needs PUBLISHED spans, and publishing a span is opt-in
 *     (publish_span defaults off). Measured live: 10 of 255 nodes have one.
 *   - the geolocation callback discarded its own failure - `() => {}` - so a
 *     refused or slow fix left `_geoLoc` null for ever.
 *
 * With neither, chooseView() fell through both branches and did nothing at all,
 * leaving the hardcoded view on screen. Nothing was broken enough to notice:
 * the map worked, it was simply looking at the wrong state, for everybody who
 * does not live near this operator.
 *
 * Their location comes FIRST now when it is available, and the initial fit
 * waits a moment for it rather than committing to somebody else's cameras.
 */
let _geoDone = false;
let _geoDeadline = 0;

function chooseView() {
  if (_userMovedMap) return;

  // A fix may still be seconds away. Committing to a view now and moving the
  // map under someone a moment later is worse than a short wait, so hold the
  // decision until geolocation has answered one way or the other.
  if (!_geoDone && !_geoLoc && Date.now() < _geoDeadline) {
    setTimeout(chooseView, 200);
    return;
  }

  // 🎯 WHERE THEY ARE, WHENEVER WE KNOW IT. This used to be the last resort;
  // it is the first choice. A volunteer opening the map wants their own
  // street, not the centre of everybody else's.
  //
  // ⚠️ `animate: false`, AND THAT IS NOT A PREFERENCE.
  // An animated setView across four zoom levels fires zoomstart and then flies.
  // Measured on the live page: the flight from the hardcoded start to a real
  // fix emitted zoomstart and never landed - the map sat on the start view with
  // an animation in limbo, so the correct branch ran, called setView, and
  // changed nothing anybody could see. Which is the worst shape a bug can take,
  // because every variable reads correct. A hard jump has no flight to lose.
  if (_geoLoc) {
    fittedOnce = true;
    if (_spanBounds &&
        _spanBounds.getCenter().distanceTo(L.latLng(_geoLoc)) < 60000) {
      // Their own area IS the covered area - frame the roads being watched.
      map.fitBounds(_spanBounds.pad(0.35), { maxZoom: 17, animate: false });
    } else {
      map.setView(_geoLoc, 12, { animate: false });
    }
    return;
  }

  // No location. Show the whole network rather than one hardcoded town: it is
  // honestly "here is where this project has cameras", and it is the same view
  // wherever the visitor happens to be.
  if (_spanBounds) {
    fittedOnce = true;
    map.fitBounds(_spanBounds.pad(0.25), { maxZoom: 13, animate: false });
  }
}

if (navigator.geolocation) {
  _geoDeadline = Date.now() + 4000;
  navigator.geolocation.getCurrentPosition(
    (pos) => {
      _geoLoc = [pos.coords.latitude, pos.coords.longitude];
      _geoDone = true;
      chooseView();
    },
    // ⚠️ NOT `() => {}`. Swallowing the failure is what left the view pinned:
    // nothing else ever learned that no fix was coming, so the wait above
    // would have run to its deadline every time and the fallback never got to
    // say why.
    () => { _geoDone = true; chooseView(); },
    { enableHighAccuracy: false, timeout: 8000, maximumAge: 600000 },
  );
} else {
  _geoDone = true;
}

/* A way to get back to yourself, because the automatic choice is made once and
 * a map you have panned is a map that will not re-centre (_userMovedMap). */
/* MeControl removed: it was one of the three floating map buttons. Its
   behaviour lives on in the Layers menu. */
// 🚨 NOT ADDED TO THE MAP ANY MORE. Three floating round buttons collided
// with the Sign in pill on a phone. The behaviour is kept and moved into the
// Layers menu, which is already in normal flow - see [[sparrow-no-floating-controls]].
function goMyArea() {
  if (_geoLoc) { map.setView(_geoLoc, 13, { animate: false }); return; }
  if (!navigator.geolocation) return;
  navigator.geolocation.getCurrentPosition(
    (pos) => {
      _geoLoc = [pos.coords.latitude, pos.coords.longitude];
      _geoDone = true;
      map.setView(_geoLoc, 13, { animate: false });
    },
    () => {},
    { enableHighAccuracy: true, timeout: 10000 });
}

/* 🎯 SHOW THE WHOLE NETWORK.
 *
 * The find-me control answers "what is near me". This answers the other
 * question people ask on arriving - "how much of this is there?" - and it is
 * the one the opening view can no longer answer, because the map now opens on
 * the visitor rather than on everything.
 *
 * It fits whatever is actually plotted: the watched roads if any are published,
 * otherwise the public sightings themselves. Falling back to a hardcoded
 * country view would be another constant that is wrong for every deployment
 * except this one, which is the mistake that opened the map on Lansing for a
 * volunteer in Florida.
 */
/* AllControl removed: it was one of the three floating map buttons. Its
   behaviour lives on in the Layers menu. */
function goEverything() {
  // A deliberate move by a person, so it must stick: mark the map as
  // user-moved or the next automatic choice would pull them back.
  _userMovedMap = true;
  const pts = [];
  if (_spanBounds) { pts.push(_spanBounds.getNorthEast(), _spanBounds.getSouthWest()); }
  state.sightings.forEach((s2) => {
    if (s2.tier === 'public' && s2.lat != null && s2.lon != null) pts.push([s2.lat, s2.lon]);
  });
  if (!pts.length) return;
  map.fitBounds(L.latLngBounds(pts).pad(0.15), { maxZoom: 12, animate: false });
}

/* A camera is drawn as ONE thing: the stretch of road it watches.
 *
 * There used to be a second thing - a dot at the camera's jittered position,
 * with a vague wedge joining it to the span. Both are gone, and the server no
 * longer sends the coordinates to draw them with (see hub.py /api/nodes).
 *
 * The reasoning: the span is drawn ACCURATELY on purpose, because people are
 * entitled to know exactly where they are recorded, and it describes a public
 * road. The camera point described a HOUSE, and 60 m of jitter on a
 * residential street narrows that to a handful of front doors rather than
 * hiding it. Publishing both meant publishing the honest fact and, next to it,
 * a weaker claim about the same camera that could only make the first one
 * sharper. Keep the road. Drop the door. */
/* The viewport, snapped OUTWARD to the server's grid and padded by one cell.
 *
 * ⚠️ THE PADDING IS WHAT STOPS THIS REFETCHING CONSTANTLY. Without it, sitting
 * exactly on a cell boundary makes every small drag flip the key back and
 * forth, so the one thing that was supposed to reuse a cached answer would
 * request twice as often as before.
 *
 * Snapped in the SAME direction as hub._snap_box: outward. A client that
 * rounded to nearest would ask for a box the server then widened, get a
 * superset back, and quietly disagree with the server about which key it was
 * on - correct output, wasted cache. */
const BOX_SNAP = 0.1;
function camBoxKey() {
  const b = map.getBounds();
  const f = (v) => Math.floor(v / BOX_SNAP) * BOX_SNAP - BOX_SNAP;
  const c = (v) => Math.ceil(v / BOX_SNAP) * BOX_SNAP + BOX_SNAP;
  return [f(b.getSouth()), f(b.getWest()), c(b.getNorth()), c(b.getEast())]
    .map((n) => n.toFixed(1)).join(',');
}

async function loadCameras() {
  // 🚨 DO NOT DOWNLOAD 4,800 CAMERAS IN ORDER TO HIDE THEM.
  //
  // This response is ~90 KB without the public traffic cameras and 1.48 MB
  // with them, and the layer that draws them defaults OFF because 4,800
  // markers made the map lag on a phone. Filtering after the download bounded
  // the DRAWING and left the megabyte and its parse exactly where they were -
  // which is most of what "the map is laggy" actually was.
  //
  // With the layer ON it is bounded by VIEWPORT as well, snapped to the same
  // ~11 km grid the server keys its cache on - so panning reuses one cached
  // answer instead of minting a new one per drag, and the answer is always a
  // superset of what is on screen.
  let url = '/api/nodes?public_cams=0';
  if (state.showPubCams) {
    url = `/api/nodes?box=${camBoxKey()}`;
  }
  const cams = await (await fetch(url)).json();
  state.camLayer.clearLayers();

  // Open on the area that actually has cameras, rather than a hardcoded zoom
  // that is wrong for every deployment except the one it was written for.
  // Spans only now - a span-less node contributes no geometry to fit to, and
  // that is correct: it has no published location to open on.
  /* 🎥 PUBLIC TRAFFIC CAMERAS GET A MARKER, because they are the only cameras
   * on this map whose position is already public - the transport department
   * publishes it in the same feed the pictures come from. A volunteer's camera
   * still gets no dot, ever: that one describes a house.
   *
   * Drawn distinctly on purpose. This project's argument is that a VOLUNTEER
   * pointed a camera at their own street, and a viewer has to be able to tell
   * which dots are that and which are a government camera we are reading.
   */
  state.publicCamLayer = state.publicCamLayer || L.layerGroup().addTo(map);
  // Clear FIRST, then bail - returning early would leave the markers drawn
  // after the box is unticked and the toggle would look dead until a reload.
  state.publicCamLayer.clearLayers();
  // A guard, NOT an early return: returning here would abandon loadCameras
  // before it draws the watched-road spans and picks the opening view, so
  // unticking one checkbox would quietly break two unrelated things.
  // \uD83D\uDEA8 circleMarker, NOT marker+divIcon, AND THAT IS THE WHOLE FIX FOR THE
  // PHONE FREEZE.
  //
  // L.marker with a divIcon builds a REAL DOM ELEMENT per camera and ignores
  // preferCanvas entirely - that is what a marker is. At eighteen cameras
  // nobody noticed; at several thousand it is several thousand nodes for the
  // browser to lay out, and a phone stops responding while it does.
  //
  // The sighting dots were moved to canvas for exactly this reason once
  // already ("the phone lag was SVG reflow"); the traffic cameras were left
  // behind and became the bigger layer. A circleMarker honours the map's
  // preferCanvas and is drawn, not built - thousands cost one canvas pass.
  //
  // \u26A0\uFE0F The look changes slightly: a canvas circle instead of a rounded square
  // with a glyph in it. Canvas cannot render a DOM box, and a layer that a
  // phone cannot open is not a nicer icon.
  if (state.showPubCams) cams.filter((c) => c.kind === 'public_cam' && c.lat != null).forEach((c) => {
    L.circleMarker([c.lat, c.lon], {
      radius: 4, weight: 1.5,
      color: '#7fd1ff', fillColor: '#1b2a3d', fillOpacity: 0.9,
    }).bindPopup(
      '<b>' + esc(c.name) + '</b><br>'
      + '<span style="color:#93a3b3">A public traffic camera, read by RavenMap.'
      + ' Not a volunteer\u2019s camera.</span><br>'
      + esc(String(c.sightings || 0)) + ' passes seen'
    ).addTo(state.publicCamLayer);
  });

  const spans = cams.filter((c) => c.span && c.span.length);
  state.spans = spans;          // kept so zoomend can redraw without refetching
  if (spans.length) _spanBounds = L.latLngBounds(spans.flatMap((c) => c.span));
  // chooseView() decides between the watched roads and the visitor's own city.
  // maxZoom in there matters: a single node's span is ~80 m across, and fitting
  // that tightly lands past zoom 20 where the basemap has no tiles.
  chooseView();

  // No count is rendered here on purpose: the stats bar already publishes
  // "<online>/<active> cameras online" from /api/stats, and that figure counts
  // the carried phones and un-snapped windows this layer cannot draw. A second
  // count computed a second way is how two numbers that must agree stop
  // agreeing.
  if (!state.showCams) return;

  // Only nodes with a road snap are drawable. A carried phone, or a window
  // camera Overpass could not snap to a way, contributes NOTHING to this
  // layer - it has no published geometry. It still appears in the count above,
  // and its sightings still appear as dots wherever they were taken.
  drawSpans(spans);
}

/* ---------------------------------------------------- towns, at low zoom -- */

/* 🚨 THIS EXISTS BECAUSE A SPAN BECOMES A MARKER WHEN YOU ZOOM OUT.
 * An 80 m watched span is 91 px at zoom 17 and under one pixel by zoom 12, so a
 * state-wide view drew a scatter of tiny green smears - which read as "a thing
 * is at this spot", the precise impression the corridor shape exists to
 * prevent. Reported as: "it still looks too much like markers."
 *
 * A town badge is the honest unit at that zoom. It says what is actually known
 * from ten miles up - somebody is watching in Brighton, and there are three
 * cameras there - and it cannot be misread as a location, because a town is
 * not a place a car was.
 *
 * It also publishes LESS than the map already does. A span names a stretch of a
 * named street; the town containing it is strictly coarser. Nothing new is
 * exposed by aggregating upward.
 *
 * The two layers never show together: below the threshold you get towns, above
 * it you get the roads themselves. Showing both would put a badge on top of the
 * detail it stands in for. */
const PLACE_MAX_ZOOM = 13;

async function loadPlaces() {
  try {
    const d = await (await fetch('/api/places')).json();
    state.places = d.places || [];
  } catch (e) {
    state.places = [];      // a failed fetch draws nothing, never a guess
  }
  drawPlaces();
}

function drawPlaces() {
  state.placeLayer.clearLayers();
  // 🚨 CLEAR FIRST, THEN BAIL. Returning before clearLayers would leave the
  // badges on screen after the box is unticked and the toggle would look dead
  // until the next reload - the same trap the watched-roads toggle documents.
  if (!state.showPlaces) return;
  if (!state.places || map.getZoom() > PLACE_MAX_ZOOM) return;

  const G = '#3ddc97';

  /* 🚨 THE BADGE HAS TO SHRINK AS YOU ZOOM OUT, OR IT BECOMES THE BLOB IT
   * REPLACED. A radius fixed in pixels is a radius that covers more GROUND the
   * further out you go: at zoom 3 a 20 px circle spans several hundred miles,
   * so the eastern states merged into one green mass and the red sightings
   * underneath disappeared. That is the same "it looks like a marker" failure
   * as the spans, arriving from the opposite direction.
   *
   * So the badge is small enough at continental zoom to read as a scatter of
   * distinct towns, and grows as the view narrows and there is room. */
  const z = map.getZoom();
  const zf = Math.max(0.42, Math.min(1, (z - 1) / 8));
  // Below this the circle is too small to hold a number legibly, and a
  // half-clipped digit reads as damage rather than data.
  const LABEL_MIN_R = 11;

  state.places.forEach((p) => {
    // Area, not radius, tracks the count - a radius proportional to cameras
    // makes three cameras look nine times one. Clamped so a single camera is
    // still findable and a big town does not swallow the county.
    const r = Math.round(Math.max(9, Math.min(26, 7 + Math.sqrt(p.cameras) * 5)) * zf);
    const live = p.online > 0;

    L.circleMarker([p.lat, p.lon], {
      radius: r, color: G, weight: live ? 2 : 1.2,
      opacity: live ? 0.85 : 0.45,
      fillColor: G, fillOpacity: live ? 0.22 : 0.12,
      // The badge stands for a whole town, so it must not behave like a
      // sighting: no click-to-open, and it sits under the red dots.
      interactive: true,
    }).bindTooltip(
      `<b>${esc(p.place)}</b><br>${p.cameras} camera${p.cameras === 1 ? '' : 's'}` +
      (p.online ? ` &middot; <span style="color:${G}">${p.online} online</span>` : '') +
      `<br><i style="opacity:.6">zoom in to see the watched roads</i>`,
      { direction: 'top', offset: [0, -r] }
    ).addTo(state.placeLayer);

    // The count inside the badge, so the map is readable without hovering -
    // which matters most on a phone, where there is no hover at all. Dropped
    // entirely when the circle is too small to hold it: a clipped digit reads
    // as a rendering fault, and at that zoom the dot is the message anyway.
    if (r >= LABEL_MIN_R) {
      L.marker([p.lat, p.lon], {
        interactive: false,
        icon: L.divIcon({
          className: 'placelabel',
          html: `<span>${p.cameras}</span>`,
          iconSize: [r * 2, r * 2], iconAnchor: [r, r],
        }),
      }).addTo(state.placeLayer);
    }
  });
}

/* 🚨 THE SPAN IS DRAWN TO SCALE, SO ZOOMING OUT DELETES IT.
 * An 80 m span is 91 px at zoom 17, 5.7 px at 13, and 0.7 px at zoom 10. The
 * previous rendering only survived down there BY ACCIDENT: `lineCap: 'round'`
 * draws a half-disc at each end however short the line gets, so a collapsed
 * span still left a ~5 px dot at 0.75 opacity. That dot WAS the "pin" the
 * corridor was built to remove - and removing it removed the zoomed-out map
 * with it. Reported as "I can't see the watched roads anymore when zoomed out".
 * The two were never separate properties, which is why the corridor change
 * could not have been tested at one zoom and called done.
 *
 * It bites now because the network went national after the video: the zoom
 * that shows every camera at once is exactly the zoom where every span is
 * sub-pixel.
 *
 * A mark at the span's MIDPOINT is safe here and nowhere else. Below the
 * legibility floor a single pixel covers more ground than the entire span, so
 * the dot cannot localise anything the span was not already publishing. The
 * argument against a centre point is a HIGH-zoom argument - at zoom 12 one
 * pixel is 28 m, already wider than the padding SPAN_MIN_M adds to hide the
 * midpoint. So: the corridor whenever it is legible, a plain dot when it is
 * not, and never both at once. */
const SPAN_LEGIBLE_PX = 14;

function drawSpans(spans) {
  state.camLayer.clearLayers();
  spans.forEach((c) => {
    const live = c.online;
    const G = '#3ddc97';

    // 'quiet' and 'offline' are different facts and used to be the same word,
    // because online was inferred from traffic. A camera can now say it is
    // watching an empty street.
    const seenAgo = c.last_seen ? ago(c.last_seen) + ' ago' : 'never';
    const status = live
      ? `<b style="color:${G}">online</b> · last vehicle ${seenAgo}`
      : `<b style="color:#8794a8">offline</b> · last vehicle ${seenAgo}`;

    // The road being watched, drawn on the road. This is the whole camera
    // layer now. The tooltip hangs off the span because there is no longer a
    // point to hang it off - and that is the honest place for it, since the
    // span is the only thing being claimed.
    // 🚨 A CORRIDOR ALONG THE ROAD, NOT A BLOB, AND NOT A LOZENGE.
    // A 5px round-capped stroke over an 80 m span draws a short fat pill with
    // bulging ends, and that reads as a MARKER - a thing at a place. It is the
    // opposite of what is being claimed: the camera's own position is jittered
    // and deliberately unpublished, and the span is the only honest unit.
    //
    // The obvious alternative, a soft radial haze, would be WORSE. A blob has a
    // visual centre and the eye finds it instantly - and that centre is the span
    // midpoint, which is exactly what SPAN_MIN_M exists to make uninformative.
    // road.py pads the span to a minimum length "so its midpoint no longer
    // localises the camera"; a rendering with a bright middle hands that back.
    //
    // So: a wide, faint band of EVEN intensity along the whole stretch, with a
    // thin brighter line on the road itself. Uniform end to end, no hotspot to
    // read a position out of, and square ends (lineCap 'butt') because round
    // caps are what made it look like a pin in the first place.
    const tip = `${esc(c.name)}${c.road_name ? ' &middot; ' + esc(c.road_name) : ''}
       <br>${status}<br>${c.sightings} sightings
       <br><i style="opacity:.6">this stretch of road is watched; the camera's
       own position is not published</i>`;

    // How long is this span ON SCREEN right now? Measured per span, not by a
    // zoom cutoff: a 28 m stretch of Hamrick and an 80 m stretch of a highway
    // stop being legible at different zooms, and the honest test is whether
    // THIS line can still be seen as a line.
    const a = map.latLngToLayerPoint(L.latLng(c.span[0]));
    const b = map.latLngToLayerPoint(L.latLng(c.span[1]));
    if (a.distanceTo(b) < SPAN_LEGIBLE_PX) {
      // The dot carries more opacity than the corridor because it has far
      // less area to carry it: the band is 18 px wide and can read at 0.14,
      // while a 4 px dot covering roughly a fortieth of that cannot. Offline
      // still reads quieter than live - 1-3 of 31 cameras are online at any
      // moment, so most of this layer is the offline state and it has to be
      // legible without pretending those cameras are live.
      // Verified at z11 on the real basemap; both this and a fainter version
      // render, so this is a legibility margin, not a fix for a blank map.
      L.circleMarker(L.latLng((c.span[0][0] + c.span[1][0]) / 2,
                              (c.span[0][1] + c.span[1][1]) / 2), {
        radius: 4, color: G, weight: 1.5, fillColor: G,
        opacity: live ? 0.95 : 0.7, fillOpacity: live ? 0.75 : 0.35,
      }).bindTooltip(tip, { sticky: true }).addTo(state.camLayer);
      return;
    }

    L.polyline(c.span, {
      color: G, weight: 18, opacity: live ? 0.14 : 0.06,
      lineCap: 'butt', interactive: false,
    }).addTo(state.camLayer);
    L.polyline(c.span, {
      color: G, weight: 2.5, opacity: live ? 0.65 : 0.28, lineCap: 'butt',
    }).bindTooltip(tip, { sticky: true }).addTo(state.camLayer);
  });
}

/* ------------------------------------------------------------- controls -- */

document.querySelectorAll('.chip').forEach((b) => {
  b.onclick = () => {
    document.querySelectorAll('.chip').forEach((x) => x.classList.remove('on'));
    b.classList.add('on');
    state.filter = b.dataset.f;
    redrawAll();
  };
});

// ⚠️ REPAINT THE HEADER TOO, OR THE FIX ONLY HOLDS FOR 30 SECONDS.
// The stats row is on a 30s timer, so changing the window redrew the map
// instantly and left the count reading the OLD window until the timer came
// round - which is the exact contradiction this was fixing, just briefer and
// harder to catch. The count is derived from the rows load() fetches, so it is
// only correct once load() has resolved.
$('#window').onchange = async (e) => {
  state.windowS = Number(e.target.value);
  await load();
  loadStats();
};
/* The watched-roads toggle, and it REMEMBERS.
 *
 * A visitor who turns the bands on, pans, and has them snap back off on the
 * next load will conclude the toggle is broken rather than that it is
 * per-session. The default lives in `state.showCams`; this only overrides it
 * once somebody has expressed a preference, so flipping the default later
 * still reaches everyone who has never touched the control.
 *
 * localStorage can throw outright in private browsing, so every access is
 * guarded - a map that fails to load because it could not remember a checkbox
 * would be a poor trade. */
const SHOWCAMS_KEY = 'sparrow.showCams';
try {
  const saved = localStorage.getItem(SHOWCAMS_KEY);
  if (saved !== null) state.showCams = saved === '1';
} catch (e) { /* private mode: keep the default */ }

const SHOWPLACES_KEY = 'sparrow.showPlaces';
try {
  const saved = localStorage.getItem(SHOWPLACES_KEY);
  if (saved !== null) state.showPlaces = saved === '1';
} catch (err) { /* the default stands */ }
const _showplaces = $('#showplaces');
if (_showplaces) {
  // Agree with state before anyone sees it, for the reason spelled out below.
  _showplaces.checked = state.showPlaces;
  _showplaces.onchange = (e) => {
    state.showPlaces = e.target.checked;
    try { localStorage.setItem(SHOWPLACES_KEY, state.showPlaces ? '1' : '0'); }
    catch (err) { /* not remembering is survivable; not drawing is not */ }
    drawPlaces();
  };
}

const SHOWPUBCAMS_KEY = 'sparrow.showPubCams';
try {
  const saved = localStorage.getItem(SHOWPUBCAMS_KEY);
  if (saved !== null) state.showPubCams = saved === '1';
} catch (err) { /* the default stands */ }
const _showpubcams = $('#showpubcams');
if (_showpubcams) {
  _showpubcams.checked = state.showPubCams;
  _showpubcams.onchange = (e) => {
    state.showPubCams = e.target.checked;
    try { localStorage.setItem(SHOWPUBCAMS_KEY, state.showPubCams ? '1' : '0'); }
    catch (err) { /* not remembering is survivable */ }
    loadCameras();
  };
}

/* 🚔 POLICE-STATION OVERLAY (OpenStreetMap).
 *
 * Context behind the dots, NOT a sighting: fixed buildings a viewer can use to
 * orient, drawn as a distinct navy "PD" badge so it can never be mistaken for a
 * government-vehicle dot. Bounded to the viewport and gated on zoom for the same
 * reason the traffic cameras are - there are ~15k nationwide, and dumping them
 * at country zoom is both a huge draw and useless. The hub serves only the ones
 * in the current box (/api/police?box=), so a phone gets the few dozen on screen.
 */
const POLICE_MIN_ZOOM = 0;       // always show (dots when far out), like cameras
const POLICE_DETAIL_ZOOM = 12;   // navy PD badges at street level
const POLICE_ICON = L.divIcon({
  className: 'polstn-wrap',
  html: '<div style="background:#16305c;color:#cfe3ff;border:1px solid #4f7fc7;'
      + 'border-radius:5px;font:700 10px/16px ui-monospace,Consolas,monospace;'
      + 'text-align:center;box-shadow:0 1px 3px rgba(0,0,0,.55)">PD</div>',
  iconSize: [26, 18], iconAnchor: [13, 9], popupAnchor: [0, -9],
});
let _policeBox = null;
async function loadPolice() {
  state.policeLayer = state.policeLayer || L.layerGroup().addTo(map);
  // Clear FIRST so unticking (or zooming out) removes what is drawn instead of
  // leaving stale badges until the next reload - the trap the pubcam layer names.
  state.policeLayer.clearLayers();
  if (!state.showPolice) { _policeBox = null; return; }
  if (map.getZoom() < POLICE_MIN_ZOOM) return;
  let data;
  try { data = await (await fetch('/api/police?box=' + camBoxKey())).json(); }
  catch (err) { return; }
  // Dots when zoomed out (fast), navy PD badges up close.
  const detail = map.getZoom() >= POLICE_DETAIL_ZOOM;
  (data.stations || []).forEach((p) => {
    if (!detail) {
      L.circleMarker([p.lat, p.lon], { radius: 3.2, weight: 1.4,
        color: '#4f7fc7', fillColor: '#16305c', fillOpacity: 0.9 })
        .addTo(state.policeLayer);
      return;
    }
    L.marker([p.lat, p.lon], { icon: POLICE_ICON, keyboard: false })
      .bindPopup('<b>' + esc(p.name || 'Police station') + '</b><br>'
        + '<span style="color:#93a3b3">A police station (OpenStreetMap). '
        + 'Context, not a sighting.</span>')
      .addTo(state.policeLayer);
  });
}
const SHOWPOLICE_KEY = 'sparrow.showPolice';
try {
  const saved = localStorage.getItem(SHOWPOLICE_KEY);
  if (saved !== null) state.showPolice = saved === '1';
} catch (err) { /* default stands */ }
const _showpolice = $('#showpolice');
if (_showpolice) {
  _showpolice.checked = state.showPolice;
  _showpolice.onchange = (e) => {
    state.showPolice = e.target.checked;
    try { localStorage.setItem(SHOWPOLICE_KEY, state.showPolice ? '1' : '0'); }
    catch (err) { /* not remembering is survivable */ }
    _policeBox = null;
    loadPolice();
  };
}
// Refetch on pan/zoom, but only when the box actually changed - same grid-snap
// discipline as the traffic cameras, so a nudge does not re-ask.
map.on('moveend', () => {
  if (!state.showPolice) return;
  const k = map.getZoom() + ':' + camBoxKey();
  if (k === _policeBox) return;
  _policeBox = k;
  loadPolice();
});

/* 📷 FLOCK / ALPR SURVEILLANCE-CAMERA OVERLAY (OpenStreetMap / DeFlock).
 *
 * The other side of "watching the watchers": where the automated plate readers
 * are. Community-mapped, so it drifts - cities add and remove them - which is
 * why every camera carries "still here" / "removed" buttons that park a report
 * for review, and why the served layer drops the ones a review confirmed gone.
 * A red eye, deliberately unlike the navy police badge and the vehicle dots.
 * Bounded to the viewport and gated on zoom (there are tens of thousands).
 */
const CAMERA_MIN_ZOOM = 0;      // always show (dots when far out); he wants them visible zoomed all the way out
const CAMERA_DETAIL_ZOOM = 14;  // full cones + report buttons at street level
// A red camera VIEW CONE that points the way the camera faces (its OSM
// `direction` bearing). The camera sits at the apex; the wedge fans out toward
// what it is watching. A camera with no mapped direction gets just the red dot.
function cameraIcon(dir) {
  var d = parseFloat(dir);
  var hasDir = !isNaN(d);
  // border-top triangle: apex at the bottom-centre (the camera), base at the top
  // (the view direction) before rotation. translucent red so overlaps still read.
  var cone = hasDir
    ? '<div style="position:absolute;top:0;left:50%;transform:translateX(-50%);width:0;height:0;'
      + 'border-left:13px solid transparent;border-right:13px solid transparent;'
      + 'border-top:24px solid rgba(255,77,94,.38)"></div>'
    : '';
  var dot = '<div style="position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);'
      + 'width:9px;height:9px;border-radius:50%;background:#7a1220;border:2px solid #ff4d5e;'
      + 'box-shadow:0 0 0 2px rgba(0,0,0,.45)"></div>';
  var rot = hasDir ? ('transform:rotate(' + d + 'deg);transform-origin:24px 24px;') : '';
  return L.divIcon({
    className: 'alprcam',
    html: '<div style="width:48px;height:48px;position:relative;' + rot + '">' + cone + dot + '</div>',
    iconSize: [48, 48], iconAnchor: [24, 24], popupAnchor: [0, -12],
  });
}
// Report a camera's status. Exposed globally so the popup buttons can call it.
// 🚨 YOU MUST BE STANDING AT THE CAMERA. The report carries your live GPS, and
// the server refuses it unless you are within a short distance of the camera -
// so nobody can log on from anywhere and mass-report cameras gone (or present).
window.smCamReport = function (id, kind, camLat, camLon, btn) {
  const wrap = btn && btn.parentNode;
  function msg(t, ok) {
    if (!wrap) return;
    let m = wrap.querySelector('.camrep-msg');
    if (!m) { m = document.createElement('div'); m.className = 'camrep-msg';
      m.style.cssText = 'font-size:12px;margin-top:6px'; wrap.appendChild(m); }
    m.style.color = ok ? '#3ddc97' : '#ff8a95'; m.textContent = t;
  }
  if (!navigator.geolocation) { msg('Your device has no location — it is needed to prove you are at the camera.', false); return; }
  if (wrap) wrap.querySelectorAll('button').forEach((b) => { b.disabled = true; });
  msg('Getting your location…', true);
  navigator.geolocation.getCurrentPosition((pos) => {
    fetch('/api/camera/report', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: id, kind: kind, lat: camLat, lon: camLon,
        at_lat: pos.coords.latitude, at_lon: pos.coords.longitude,
        acc: Math.round(pos.coords.accuracy || 0) }),
    }).then((r) => r.json().then((j) => ({ ok: r.ok, j: j })))
      .then((res) => {
        if (res.ok && res.j.ok) msg('Thanks — recorded for review.', true);
        else { msg(res.j.error || 'Could not send.', false);
          if (wrap) wrap.querySelectorAll('button').forEach((b) => { b.disabled = false; }); }
      }).catch(() => { msg('Network error, try again.', false);
        if (wrap) wrap.querySelectorAll('button').forEach((b) => { b.disabled = false; }); });
  }, (err) => {
    msg('Location needed to confirm you are at the camera: ' + err.message, false);
    if (wrap) wrap.querySelectorAll('button').forEach((b) => { b.disabled = false; });
  }, { enableHighAccuracy: true, timeout: 12000, maximumAge: 0 });
};
let _camBox2 = null;
async function loadSurveillance() {
  state.cameraLayer = state.cameraLayer || L.layerGroup().addTo(map);
  state.cameraLayer.clearLayers();
  if (!state.showCameras) { _camBox2 = null; return; }
  if (map.getZoom() < CAMERA_MIN_ZOOM) return;   // below this it's just noise
  let data;
  try { data = await (await fetch('/api/cameras?box=' + camBoxKey())).json(); }
  catch (err) { return; }
  // Zoomed out: light canvas dots (thousands stay smooth). Zoomed in: the full
  // cone + report buttons. So you can SEE the cameras from far out and act on
  // one up close, without a phone-freezing pile of DOM markers at low zoom.
  const detail = map.getZoom() >= CAMERA_DETAIL_ZOOM;
  (data.cameras || []).forEach((c) => {
    if (!detail) {
      L.circleMarker([c.lat, c.lon], { radius: 3.2, weight: 1.4,
        color: c.confirmed ? '#3ddc97' : '#ff4d5e',
        fillColor: '#7a1220', fillOpacity: 0.9 }).addTo(state.cameraLayer);
      return;
    }
    const dir = c.dir ? (' Faces about ' + esc(String(c.dir)) + '°.') : '';
    const badge = c.confirmed
      ? '<span style="color:#3ddc97"> ✓ RF-confirmed present.</span>' : '';
    L.marker([c.lat, c.lon], { icon: cameraIcon(c.dir), keyboard: false })
      .bindPopup(
        '<b>Flock / ALPR camera</b><br>'
        + '<span style="color:#93a3b3">An automated licence-plate reader '
        + '(OpenStreetMap).' + dir + badge + '</span><br>'
        + '<span style="color:#7f8ea0;font-size:11.5px">You must be standing at '
        + 'the camera to report it — the buttons use your location.</span>'
        + '<div style="margin-top:8px;display:flex;gap:6px;flex-wrap:wrap">'
        + '<button type="button" style="flex:1;padding:7px;border:0;border-radius:7px;'
        + 'background:#173a2a;color:#3ddc97;font-weight:700;cursor:pointer" '
        + 'onclick="smCamReport(\'' + esc(c.id) + '\',\'present\',' + c.lat + ',' + c.lon + ',this)">'
        + '✓ Still here</button>'
        + '<button type="button" style="flex:1;padding:7px;border:0;border-radius:7px;'
        + 'background:#3a1720;color:#ff8a95;font-weight:700;cursor:pointer" '
        + 'onclick="smCamReport(\'' + esc(c.id) + '\',\'removed\',' + c.lat + ',' + c.lon + ',this)">'
        + '✗ Removed</button></div>')
      .addTo(state.cameraLayer);
  });
}
const SHOWCAMERAS_KEY = 'sparrow.showCameras';
try {
  const saved = localStorage.getItem(SHOWCAMERAS_KEY);
  if (saved !== null) state.showCameras = saved === '1';
} catch (err) { /* default stands */ }
const _showcameras = $('#showcameras');
if (_showcameras) {
  _showcameras.checked = state.showCameras;
  _showcameras.onchange = (e) => {
    state.showCameras = e.target.checked;
    try { localStorage.setItem(SHOWCAMERAS_KEY, state.showCameras ? '1' : '0'); }
    catch (err) { /* survivable */ }
    _camBox2 = null;
    loadSurveillance();
  };
}
map.on('moveend', () => {
  if (!state.showCameras) return;
  const k = map.getZoom() + ':' + camBoxKey();
  if (k === _camBox2) return;
  _camBox2 = k;
  loadSurveillance();
});

/* 📡 RADAR / SPEED-TRAP LAYER (beta).
 *
 * Live police-radar sources reported by PAIRED DETECTOR HARDWARE (see
 * tools/radar_bridge.py). These are not sightings and not taps: a dot means "a
 * radar emission of this band was detected near here in the last few minutes,"
 * and it fades on its own. Ka band is police-only, so it reads red-hot; K/X are
 * dimmer because a car's blind-spot radar also lives at 24 GHz. Because these
 * are transient, the layer refreshes on a short timer while it is on. It stays
 * empty until a detector is feeding the network - honest, like the RF layer. */
function radarColor(band, conf) {
  if (band === 'laser') return '#c026d3';       // laser/lidar: distinct violet
  if (band === 'ka') return '#ef2b2b';          // police-only: red
  if (band === 'k') return conf >= 0.5 ? '#f59e0b' : '#b8860b';  // amber, cars share it
  return '#8a8a8a';                             // X band: mostly doors, grey
}
async function loadRadar() {
  state.radarLayer = state.radarLayer || L.layerGroup().addTo(map);
  state.radarLayer.clearLayers();
  if (!state.showRadar) return;
  let data;
  try { data = await (await fetch('/api/radar?box=' + camBoxKey())).json(); }
  catch (err) { return; }
  (data.dots || []).forEach((d) => {
    const col = radarColor(d.band, d.conf);
    const r = 10 + Math.round(10 * (d.conf || 0));
    const icon = L.divIcon({ className: 'radar-blip', iconSize: [r * 2, r * 2],
      html: '<span class="radar-ring" style="width:' + (r * 2) + 'px;height:'
        + (r * 2) + 'px;border-color:' + col + '"></span>'
        + '<span class="radar-core" style="background:' + col + '"></span>' });
    L.marker([d.lat, d.lon], { icon, keyboard: false })
      .bindPopup('<b style="color:' + col + '">📡 ' + esc((d.band || '').toUpperCase())
        + ' radar</b><br>' + Math.round((d.conf || 0) * 100) + '% confidence'
        + ' · ' + d.reporters + ' report' + (d.reporters === 1 ? '' : 's')
        + '<br><span style="color:#93a3b3">detected ' + d.age + 's ago, fades on its own.'
        + (d.band === 'ka' ? ' Ka band is police-only.'
          : d.band === 'laser' ? ' Laser = active lidar speed gun.'
          : ' K/X band can also be a car’s own radar.') + '</span>')
      .addTo(state.radarLayer);
  });
}
let _radarBox = null, _radarTimer = null;
const SHOWRADAR_KEY = 'sparrow.showRadar';
try {
  const saved = localStorage.getItem(SHOWRADAR_KEY);
  if (saved !== null) state.showRadar = saved === '1';
} catch (err) { /* default stands */ }
function radarTimerSync() {
  if (state.showRadar && !_radarTimer) {
    _radarTimer = setInterval(loadRadar, 12000);   // transient, so refresh often
  } else if (!state.showRadar && _radarTimer) {
    clearInterval(_radarTimer); _radarTimer = null;
  }
}
const _showradar = $('#showradar');
if (_showradar) {
  _showradar.checked = state.showRadar;
  _showradar.onchange = (e) => {
    state.showRadar = e.target.checked;
    try { localStorage.setItem(SHOWRADAR_KEY, state.showRadar ? '1' : '0'); }
    catch (err) { /* survivable */ }
    _radarBox = null;
    loadRadar();
    radarTimerSync();
  };
}
map.on('moveend', () => {
  if (!state.showRadar) return;
  const k = camBoxKey();
  if (k === _radarBox) return;
  _radarBox = k;
  loadRadar();
});

/* 🛸 LIVE SENSOR LAYERS: drones (Remote ID) and police-radio activity.
 *
 * Both come from paired hardware via /api/sensor and are transient like radar -
 * a drone is a moving point, radio activity is a soft pulse where a scanner is
 * hearing dispatch. One small generic driver runs both: a state flag, a layer,
 * a fetch, a refresh timer, and a draw function per kind. Empty until a feeder
 * is running (tools/sensors/drone_feed.py, p25_feed.py). */
function makeSensorLayer(opts) {
  // opts: {kind, flag, key(storage), checkbox, draw, refreshMs}
  let box = null, timer = null;
  async function load() {
    state[opts.layer] = state[opts.layer] || L.layerGroup().addTo(map);
    state[opts.layer].clearLayers();
    if (!state[opts.flag]) return;
    let data;
    try { data = await (await fetch('/api/sensor?kind=' + opts.kind + '&box=' + camBoxKey())).json(); }
    catch (err) { return; }
    (data.points || []).forEach((pt) => opts.draw(state[opts.layer], pt));
  }
  function timerSync() {
    if (state[opts.flag] && !timer) timer = setInterval(load, opts.refreshMs);
    else if (!state[opts.flag] && timer) { clearInterval(timer); timer = null; }
  }
  try {
    const saved = localStorage.getItem(opts.key);
    if (saved !== null) state[opts.flag] = saved === '1';
  } catch (err) { /* default */ }
  const cb = $(opts.checkbox);
  if (cb) {
    cb.checked = state[opts.flag];
    cb.onchange = (e) => {
      state[opts.flag] = e.target.checked;
      try { localStorage.setItem(opts.key, state[opts.flag] ? '1' : '0'); } catch (err) {}
      box = null; load(); timerSync();
    };
  }
  map.on('moveend', () => {
    if (!state[opts.flag]) return;
    const k = camBoxKey();
    if (k === box) return;
    box = k; load();
  });
  return { load, timerSync };
}
const _droneLayer = makeSensorLayer({
  kind: 'drone', flag: 'showDrones', layer: 'droneLayer',
  key: 'sparrow.showDrones', checkbox: '#showdrones', refreshMs: 10000,
  draw: (lyr, pt) => {
    L.marker([pt.lat, pt.lon], { icon: L.divIcon({ className: 'drone-mk',
      iconSize: [22, 22], html: '<span class="drone-dot">🛸</span>' }), keyboard: false })
      .bindPopup('<b style="color:#38bdf8">🛸 Drone</b><br>'
        + (pt.label ? esc(pt.label) + '<br>' : '')
        + '<span style="color:#93a3b3">Broadcasting Remote ID · seen ' + pt.age + 's ago</span>')
      .addTo(lyr);
  } });
const _radioLayer = makeSensorLayer({
  kind: 'radio', flag: 'showRadio', layer: 'radioLayer',
  key: 'sparrow.showRadio', checkbox: '#showradio', refreshMs: 20000,
  draw: (lyr, pt) => {
    L.circleMarker([pt.lat, pt.lon], { radius: 13, weight: 1.5, color: '#f59e0b',
      fillColor: '#f59e0b', fillOpacity: 0.16 })
      .bindPopup('<b style="color:#f59e0b">📻 Police radio active</b><br>'
        + (pt.label ? esc(pt.label) + '<br>' : '')
        + '<span style="color:#93a3b3">A scanner here is hearing dispatch · '
        + pt.age + 's ago. Area, not a pinpoint.</span>')
      .addTo(lyr);
  } });

/* 🚁 AIRCRAFT ON THE MAP.
 *
 * The detector has existed for a while and only /planes could see it, which is
 * a page nobody visits. The thing worth showing was never "aircraft" - it is
 * an ORBIT: sustained circling at low altitude over one spot, which is what a
 * police helicopter working a scene looks like from above, and which no amount
 * of registration data can tell you because registration is a claim somebody
 * filed once while an orbit is a thing an aircraft is doing right now.
 *
 * So this layer draws three things and nothing else: aircraft that are
 * ORBITING, aircraft on a GOVERNMENT registration, and aircraft flagged as LAW
 * ENFORCEMENT. Everything else in the sky is a light aircraft going somewhere
 * and would bury the signal it is here to show.
 *
 * ⚠️ Bounded to what is on screen and polled slowly. The upstream is OpenSky's
 * free tier - somebody else's service, shared by everyone - and a map that
 * refetched the whole sky on every pan would be both useless and rude.
 */
const AIR_KEY = 'sparrow.showAircraft';
const airLayer = L.layerGroup().addTo(map);
let _airTimer = null, _airBusy = false;

function airIcon(a) {
  // 🚨 COLOUR ANSWERS "IS IT POLICE", BECAUSE THAT IS THE QUESTION.
  //
  // It used to answer "is it circling", which meant a sheriff's helicopter in
  // transit and a university survey plane were the same colour, and a circling
  // crop duster was the reddest thing on the map. The ring still marks an
  // orbit - that is behaviour and worth seeing - but the colour is ownership.
  const orbit = !!a.orbit;
  const col = a.law_enforcement ? '#ff3b47' : (orbit ? '#ffb547' : '#7fb4ff');
  const rot = Math.round(a.track || 0);
  return L.divIcon({
    className: 'airmark',
    iconSize: [30, 30], iconAnchor: [15, 15],
    html: `<div class="airwrap${orbit ? ' orbit' : ''}">`
        + `<svg viewBox="0 0 24 24" width="22" height="22"`
        + ` style="transform:rotate(${rot}deg)">`
        + `<path fill="${col}" stroke="#06090f" stroke-width="1"`
        + ` d="M12 2l1.6 7.2 7.4 3.2v1.6l-7.4-1.6-1 5.2 2.6 1.8v1.2L12 20l-3.2.6v-1.2l2.6-1.8-1-5.2-7.4 1.6v-1.6l7.4-3.2z"/>`
        + `</svg></div>`,
  });
}

function airPopup(a) {
  const esc = (t) => String(t ?? '').replace(/[&<>"']/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const bits = [];
  if (a.orbit) {
    // Say what was MEASURED, not just "circling". This is the claim most worth
    // being able to check, and the number is the whole basis for it.
    const d = a.orbit_detail || {};
    bits.push('<b style="color:#ff3b47">Circling</b>'
      + (d.path_km ? ` — flew ${Math.round(d.path_km)} km and stayed within `
                   + `${(d.net_km || 0).toFixed(1)} km` : ''));
  }
  // ⚠️ SAY "NOT LAW ENFORCEMENT" OUT LOUD. Most government registrations are
  // universities and agricultural agencies, and leaving that implied is how
  // somebody reads a university survey plane as a police aircraft. The map
  // exists to let people check things, so the negative has to be stated.
  if (a.law_enforcement) {
    bits.push('<b style="color:#ff3b47">Law enforcement</b> registration');
  } else if (a.gov) {
    bits.push('<b>Government</b> registration — <b>not</b> law enforcement');
  }
  if (a.owner) bits.push(esc(a.owner));
  if (a.alt_m != null) bits.push(`${Math.round(a.alt_m * 3.28084).toLocaleString()} ft`);
  return `<div class="pop"><h4>${esc(a.call || a.n_number || a.icao)}</h4>`
       + bits.map((b) => `<div class="sub">${b}</div>`).join('')
       + `<div class="sub dim">Live position from ADS-B, which aircraft `
       + `broadcast publicly. RavenMap does not track aircraft.</div></div>`;
}

// 🚨 WHICH SWITCH OWNS AN AIRCRAFT. Law enforcement wins outright, so a
// sheriff's helicopter is never hidden behind the "other government" box no
// matter what else is true of it. Everything else the layer draws - other
// government registrations, and anything CIRCLING regardless of who owns it -
// belongs to the second box. An aircraft that is neither is not drawn at all.
function airIsPolice(a) { return !!a.law_enforcement; }
function airIsOther(a) { return !a.law_enforcement && (!!a.gov || !!a.orbit); }

function drawAircraft(list) {
  airLayer.clearLayers();
  if (!state.showAircraft && !state.showAircraftGov) return;
  (list || []).forEach((a) => {
    if (a.lat == null || a.lon == null) return;
    const show = (state.showAircraft && airIsPolice(a))
              || (state.showAircraftGov && airIsOther(a));
    if (!show) return;
    L.marker([a.lat, a.lon], { icon: airIcon(a), zIndexOffset: 400 })
      .bindPopup(airPopup(a))
      .addTo(airLayer);
  });
}

function loadAircraft() {
  if (!state.showAircraft && !state.showAircraftGov) {
    airLayer.clearLayers();
    return;
  }
  if (_airBusy) return;                 // a slow answer must not stack up
  _airBusy = true;
  const b = map.getBounds();
  const box = [b.getSouth(), b.getWest(), b.getNorth(), b.getEast()]
    .map((n) => n.toFixed(3)).join(',');
  fetch(`/api/aircraft?box=${box}`, { cache: 'no-store' })
    .then((r) => (r.ok ? r.json() : null))
    .then((d) => { if (d && d.aircraft) drawAircraft(d.aircraft); })
    .catch(() => { /* the rest of the map does not depend on this */ })
    .then(() => { _airBusy = false; });
}

const AIRGOV_KEY = 'sparrow.showAircraftGov';
function _wireAir(sel, key, prop) {
  try {
    const saved = localStorage.getItem(key);
    if (saved !== null) state[prop] = saved === '1';
  } catch (err) { /* the default stands */ }
  const el = $(sel);
  if (!el) return;
  el.checked = state[prop];
  el.onchange = (e) => {
    state[prop] = e.target.checked;
    try { localStorage.setItem(key, state[prop] ? '1' : '0'); }
    catch (err) { /* not remembering is survivable */ }
    // ⚠️ Always redraw, never just clear: turning one box off must leave the
    // OTHER box's aircraft on the map. Clearing the layer here is what makes a
    // shared layer with two switches go wrong.
    loadAircraft();
    if (!state.showAircraft && !state.showAircraftGov) airLayer.clearLayers();
  };
}
_wireAir('#showair', AIR_KEY, 'showAircraft');
_wireAir('#showairgov', AIRGOV_KEY, 'showAircraftGov');

// Slow on purpose - see the note above about whose service this is.
if (_airTimer) clearInterval(_airTimer);
_airTimer = setInterval(loadAircraft, 30000);
map.on('moveend', () => {
  if (state.showAircraft || state.showAircraftGov) loadAircraft();
});
if (state.showAircraft || state.showAircraftGov) loadAircraft();

/* Traffic cameras follow the viewport - but ONLY when the snapped box actually
 * changes.
 *
 * ⚠️ A bare `moveend -> loadCameras()` would refetch on every nudge of the map,
 * which is how a fix for downloading too much turns into downloading more
 * often. The whole point of snapping to a grid is that most movement does not
 * change the answer, so most movement must not ask for it again. */
let _camBox = null;
map.on('moveend', () => {
  if (!state.showPubCams) return;
  const k = camBoxKey();
  if (k === _camBox) return;
  _camBox = k;
  loadCameras();
});

/* The View button: open the chips, close them, and keep the label honest.
 *
 * ⚠️ It does NOT own the filter. The chips still do, and their existing click
 * handler still runs - this listens for the same click and follows. Two things
 * deciding what the filter is would be the same "one rule, many owners" bug
 * this codebase keeps finding in itself; the label is a READOUT, never a
 * source of truth. */
(function () {
  const btn = $('#viewbtn'), menu = $('#filters'), now = $('#viewnow');
  if (!btn || !menu || !now) return;
  const open = (on) => {
    menu.hidden = !on;
    btn.setAttribute('aria-expanded', on ? 'true' : 'false');
    // Only one dropdown open at a time. Each button stopPropagation()s its own
    // click, which blocks the OTHER menu's outside-click close handler, so
    // opening one has to close the other explicitly.
    if (on) {
      const lp = $('#layers'), lb = $('#layerbtn');
      if (lp) lp.setAttribute('hidden', '');
      if (lb) lb.setAttribute('aria-expanded', 'false');
    }
  };
  btn.addEventListener('click', (e) => { e.stopPropagation(); open(menu.hidden); });
  menu.addEventListener('click', (e) => {
    const chip = e.target.closest('.chip');
    if (!chip) return;
    now.textContent = chip.textContent.trim();
    open(false);
  });
  // Anywhere else on the page closes it, including on the map.
  document.addEventListener('click', (e) => {
    if (!menu.hidden && !menu.contains(e.target) && e.target !== btn) open(false);
  });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') open(false); });
  // Start agreeing with whatever chip is actually selected.
  const on = menu.querySelector('.chip.on');
  if (on) now.textContent = on.textContent.trim();
})();

const _showcams = $('#showcams');
// The checkbox must be made to AGREE with state before anyone sees it. The
// markup ships checked; if the stored answer or the default is off, a control
// that says "on" over a map with no bands is worse than no control.
_showcams.checked = state.showCams;
_showcams.onchange = (e) => {
  state.showCams = e.target.checked;
  try { localStorage.setItem(SHOWCAMS_KEY, state.showCams ? '1' : '0'); }
  catch (err) { /* not remembering is survivable; not drawing is not */ }
  // Unticking has to CLEAR what is already drawn. loadCameras() returns early
  // when showCams is false, so without this the bands stay on screen and the
  // toggle looks dead until the next reload.
  if (!state.showCams) state.camLayer.clearLayers();
  loadCameras();
};

/* ----------------------------------------------------------------- data -- */

/* Two requests, because the two tiers want opposite things.
 *
 * RECORDS go back as far as the window says - which now defaults to all of
 * them. TRAFFIC only ever wants the last couple of minutes, because that is
 * how long a dot survives. Asking for both in one call meant that selecting
 * "everything" also dragged down every private pass ever recorded, almost all
 * of which would be drawn and immediately reaped. Two bounded queries beat one
 * that grows without limit. */
/* The Layers button: open the switches, close them, and keep the count honest.
 *
 * ⚠️ It OWNS NOTHING. Every checkbox keeps its own existing handler; this only
 * shows and hides the panel and counts what is ticked. Two things deciding
 * whether a layer is on is the "one rule, many owners" bug the View button
 * already had to avoid. */
(function () {
  const btn = $('#layerbtn');
  const panel = $('#layers');
  if (!btn || !panel) return;
  const boxes = () => panel.querySelectorAll('input[type=checkbox]');
  const count = () => {
    const n = [...boxes()].filter((b) => b.checked).length;
    const el = $('#layern');
    if (el) el.textContent = String(n);
  };
  btn.onclick = (e) => {
    e.stopPropagation();
    const open = panel.hasAttribute('hidden');
    panel.toggleAttribute('hidden', !open);
    btn.setAttribute('aria-expanded', open ? 'true' : 'false');
    // Close the View menu when opening this one (see the note in the View
    // handler): stopPropagation blocks the other's outside-click close.
    if (open) {
      const fm = $('#filters'), vb = $('#viewbtn');
      if (fm) fm.setAttribute('hidden', '');
      if (vb) vb.setAttribute('aria-expanded', 'false');
    }
  };
  // Tapping the map closes it, the same as the View menu - a panel that can
  // only be dismissed by the button that opened it traps a phone user.
  document.addEventListener('click', (e) => {
    if (!panel.contains(e.target) && e.target !== btn) {
      panel.setAttribute('hidden', '');
      btn.setAttribute('aria-expanded', 'false');
    }
  });
  boxes().forEach((b) => b.addEventListener('change', count));
  count();
})();

/* 🚨 On a phone, reparent the OPEN View/Layers sheet to <body>. Nested under the
 * fixed, z-indexed <main>, the fixed sheet was painting UNDER the map on some
 * phones (a GPU-composited map layer can beat a nested z-index). As a direct
 * child of <body> it sits in the root stacking context, unambiguously on top.
 * Moved back to its home parent on close (desktop keeps it in place - it is
 * absolutely positioned against its button there). Handlers bind by id and use
 * .contains(), both of which survive the move. */
(function () {
  const isPhone = () => window.matchMedia('(max-width:820px)').matches;
  ['filters', 'layers'].forEach((id) => {
    const el = document.getElementById(id);
    if (!el) return;
    const home = el.parentNode;
    const sync = () => {
      const shown = !el.hasAttribute('hidden');
      if (isPhone() && shown) {
        if (el.parentNode !== document.body) document.body.appendChild(el);
      } else if (el.parentNode !== home) {
        home.appendChild(el);
      }
    };
    new MutationObserver(sync).observe(el, { attributes: true, attributeFilter: ['hidden'] });
  });
})();

/* 🚨 "A POSSIBLE PATROL CAR HERE IS WAITING ON A PERSON."
 *
 * Unreviewed police-classed sightings, drawn as a slow pulse over a ~5 km cell.
 * The point is to say that something is pending and roughly where, WITHOUT
 * asserting a patrol car is there - the classifier runs at about 95% precision,
 * so one in twenty of these is not one, and a confident dot would be the map
 * making a claim no human has checked.
 *
 * ⚠️ IT IS DELIBERATELY UNLIKE A SIGHTING. Sightings are small, sharp and
 * exactly placed because they are records. This is large, soft, dashed and
 * pulsing because it is a question. If the two ever start looking alike,
 * something has gone wrong with the argument this map makes.
 *
 * ⚠️ The coarsening is the SERVER's (db.pending_areas). Rounding here would be
 * decoration - the precise position would already have been sent.
 */
let pendingLayer = null;

async function loadPending() {
  try {
    const d = await (await fetch('/api/pending', { cache: 'no-store' })).json();
    pendingLayer = pendingLayer || L.layerGroup().addTo(map);
    pendingLayer.clearLayers();
    (d.cells || []).forEach((c) => {
      const km = (d.cell_deg || 0.05) * 111;
      L.circle([c.lat, c.lon], {
        radius: km * 500,               // half the cell, in metres
        className: 'pendingPulse',
        color: '#ffb547', weight: 2, dashArray: '6 6',
        fillColor: '#ffb547', fillOpacity: 0.08, interactive: true,
      }).bindPopup(
        `<div class="pop"><h4>Possible patrol car, not yet reviewed</h4>`
        + `<div class="sub">${c.n} sighting${c.n === 1 ? '' : 's'} in this area `
        + `the detector called police or government, waiting on a person.</div>`
        + `<div class="sub dim">The area is deliberately rough and the exact `
        + `position is not published: nobody has checked these yet, and about `
        + `one in twenty will not be a patrol car.</div></div>`
      ).addTo(pendingLayer);
    });
  } catch (err) {
    /* 🚨 A FAILED REFRESH MUST TAKE THE PULSE DOWN, NOT LEAVE IT UP.
     *
     * This used to swallow the error and return, which left whatever was
     * drawn last still on the map. The layer is only cleared AFTER the fetch
     * resolves, so one failed call froze the pulse until a later one
     * succeeded - and on a phone that can be a very long time (see the
     * visibility handler below).
     *
     * Leaving it up is the wrong failure direction for THIS layer in
     * particular. Every other layer shows something a human already
     * confirmed; this one asserts "a machine thinks there is an unreviewed
     * patrol car here", and the queue behind it empties as soon as somebody
     * presses a button. A stale pulse therefore points at a place where
     * nothing is pending, which is the one claim this layer must never make.
     * Reported from the map: a pulse with an empty review queue behind it. */
    if (pendingLayer) pendingLayer.clearLayers();
  }
}

/* 🚨 A BACKGROUNDED PHONE DOES NOT RUN setInterval, SO IT COMES BACK LYING.
 *
 * The pending pulse refreshes on a 60s timer. iOS suspends timers the moment
 * the tab or PWA goes to the background, so a phone that was put in a pocket
 * with the map open shows the pulse it had when it was locked - minutes or
 * hours later - and the timer does not necessarily fire promptly on return.
 * The review queue is usually emptied within a couple of minutes, so the
 * stale state is not an edge case: it is the normal case for a phone user.
 *
 * Refreshing on visibility costs one small request per return to the app, and
 * `/api/pending` answers `no-store` with an empty body in the common case. */
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') loadPending();
});

/* The opening view, drawn before the live data can possibly arrive.
 *
 * 🚨 THE MAP USED TO OPEN EMPTY FOR ABOUT 0.7s ON EVERY VISIT. Two live API
 * calls - measured 57.8 KB and 175.5 KB - and both answer max-age=4 with
 * cf-cache-status DYNAMIC, so the edge caches neither and every visitor is a
 * pair of database queries for an answer identical for all of them.
 *
 * /static/snapshot.json is a plain file, so the edge DOES cache it: the first
 * paint costs one cached fetch and no origin work.
 *
 * ⚠️ IT NEVER OVERWRITES LIVE DATA. If load() has already landed - a fast
 * connection, or a soft refresh - this returns without touching anything. A
 * snapshot arriving late and painting stale dots over fresh ones would be
 * worse than the blank map it replaces.
 *
 * ⚠️ AND IT DOES NOT CLAIM TO BE LIVE. The status pill stays "connecting"
 * until real data arrives; a stale snapshot presented as live would be the
 * project telling a small lie on its own front page every few minutes.
 */
let _liveArrived = false;

async function drawSnapshot() {
  try {
    const d = await (await fetch('/static/snapshot.json')).json();
    if (_liveArrived || !d) return;
    (d.public || []).forEach((r) => state.sightings.set(r.id, r));
    (d.traffic || []).forEach((r) => { if (r.tier !== 'public') drawTraffic(r); });
    redrawAll();
  } catch (err) { /* the live path is the real one; this is a head start */ }
}

/* 🚨 A FETCH WITH NO TIMEOUT CAN HANG FOREVER, AND A HUNG FETCH IS WORSE THAN A
   FAILED ONE. It never resolves and never rejects, so refresh() stays awaiting,
   `state.online` stays null, and the pill sits on "connecting" for as long as
   the tab is open - with no retry, because the next tick is still waiting on
   the last one. Reported from a phone on a VPN, where the snapshot had drawn
   144 sightings and the header still claimed it was connecting.

   A timeout converts that into a normal failure: the dot says "reconnecting",
   which is TRUE, and the next tick tries again. */
const FETCH_TIMEOUT_MS = 12000;

function fetchJSON(url, ms = FETCH_TIMEOUT_MS) {
  // AbortSignal.timeout is not on older iOS Safari, which is exactly the
  // audience here, so drive an AbortController by hand.
  const ac = new AbortController();
  const t = setTimeout(() => ac.abort(), ms);
  return fetch(url, { signal: ac.signal })
    .then((r) => {
      if (!r.ok) throw new Error(url + ' -> ' + r.status);
      return r.json();
    })
    .finally(() => clearTimeout(t));
}

async function load() {
  const trafficCut = bucketed(Date.now() / 1000 - TRAFFIC_FADE_S);
  const [pub, live] = await Promise.all([
    fetchJSON(`/api/sightings?since=${windowCut()}&vclass=public&limit=${PUBLIC_LIMIT}`),
    fetchJSON(`/api/sightings?since=${trafficCut}&limit=400`),
  ]);
  // ⚠️ The clear() is why drawSnapshot must never run after this: live data
  // replaces the snapshot wholesale rather than merging with it.
  _liveArrived = true;
  state.sightings.clear();
  pub.forEach((r) => state.sightings.set(r.id, r));
  live.forEach((r) => { if (r.tier !== 'public') drawTraffic(r); });
  redrawAll();
  ageTraffic();
}

let lastStats = null;

function emptyState(stats) {
  // The empty-state panel was removed at the owner's request - a live map reads
  // as live on its own, and the panel covered it. Keep the cached stats (other
  // code reads lastStats) and clear any panel a previous version left behind.
  lastStats = stats || lastStats;
  const el = document.getElementById('empty');
  if (el) el.remove();
}

/* Why the map has no police on it.
 *
 * An empty public tier and a quiet street look identical from outside, and a
 * visitor who cannot tell them apart will assume the network does not work.
 * Saying it plainly costs nothing and is the same argument as the transparency
 * page: a claim this project cannot support yet is a claim it does not make,
 * and that is a feature worth showing rather than a gap worth hiding. */
/* Move the map to the deployment's configured centre - but only while nothing
 * better has happened. loadCameras() fitting to the real watched spans is
 * strictly better than any configured guess, and it can land first or second
 * depending on which fetch returns quicker, so this defers to `fittedOnce`
 * rather than assuming an order. Without that guard the map visibly jumps back
 * off the cameras a moment after finding them. */
async function applyConfiguredView() {
  let p;
  try { p = await (await fetch('/api/policy')).json(); } catch { return; }
  // 🚨 THREE FUNCTIONS WANTED TO SET THE OPENING VIEW AND `fittedOnce` WAS THE
  // ONLY THING KEEPING THEM APART - a flag read AFTER an await, so which one
  // won depended on which network call returned first. That is how a Florida
  // volunteer ended up looking at Lansing: the configured centre is this
  // deployment's own town, and it only had to arrive at the right moment.
  //
  // It is now the LAST resort rather than a competitor. If a location fix is
  // still plausibly coming, this stays out of the way entirely - chooseView
  // owns the decision and has its own fallback for having no fix at all.
  if (fittedOnce || _userMovedMap) return;
  if (!_geoDone && Date.now() < _geoDeadline) return;
  if (_geoLoc || _spanBounds) return;      // chooseView has something better
  const c = p.map_center;
  if (Array.isArray(c) && c.length === 2 && c.every(Number.isFinite)) {
    map.setView(c, p.map_zoom || 13, { animate: false });
  }
}

async function policyBanner() {
  let p;
  try { p = await (await fetch('/api/policy')).json(); } catch { return; }
  const el = document.getElementById('policybar');
  if (!el) return;
  const main = document.querySelector('main');
  if (p.publishes_public_tier) {
    el.style.display = 'none';
    main.style.top = '52px';
    return;
  }
  el.style.display = '';
  el.innerHTML = `<b>Public-tier reporting is off.</b> RavenMap is not yet
    willing to call a vehicle a police vehicle: the classifier has not been
    validated against locally labelled footage, and an unvalidated one was
    wrong every time it was checked. Traffic is still counted and cameras are
    still live &mdash; nothing is being asserted that cannot be supported.
    <a href="/transparency">How this is decided &rarr;</a>`;
  // main is absolutely positioned under a fixed-height header, so it has to be
  // pushed down by however tall the notice wraps to on this screen.
  const push = () => { main.style.top = (52 + el.offsetHeight) + 'px';
                       map.invalidateSize(); };
  push();
  window.addEventListener('resize', push);
}

async function loadStats() {
  const s = await (await fetch('/api/stats')).json();
  emptyState(s);
  // 'vehicles' counts DISTINCT PUBLIC-TIER vehicles. It used to count distinct
  // plate hashes across every tier, which counted the empty-string hash shared
  // by every plateless pass as one vehicle - so a map identifying nobody
  // reported "1 vehicle". It is also the wrong thing to report even when it
  // works: the system cannot count distinct private vehicles, by design, and a
  // figure that implies it can is a claim this project should never make.
  // ⚠️ A DASH, NOT A ZERO, WHEN THE QUESTION CANNOT BE ASKED.
  //
  // "public vehicles" counts DISTINCT vehicles, which needs a plate to tell
  // them apart. This camera reads none - 22px of plate against the 60 needed -
  // so the figure sat at 0 beside "4 public sightings" and read as a
  // contradiction. It is the difference between counting none and being unable
  // to count, and printing the second as the first is exactly the mistake of
  // treating a gap in the instrument as evidence of absence.
  const countable = s.vehicles_countable ?? 0;
  const vehicles = countable
    ? `<b>${s.vehicles_24h.toLocaleString()}</b> distinct vehicles`
    : `<b title="Distinct vehicles can only be counted when a plate is read.
This camera reads none, so the sightings above cannot be told apart.">&mdash;</b> distinct vehicles`;
  // 🚨 COMMENTS GO HERE, NOT INSIDE THE TEMPLATE LITERAL.
  // HTML comments inside a `...` string are just text, and a BACKTICK inside
  // one closes the string. That is what took the whole map down: app.js failed
  // to parse, so nothing ran and the page sat on "connecting", "everything
  // quiet", no sightings - while the server was perfectly healthy the whole
  // time. Nothing in the markup below is commented; the reasoning lives here.
  //
  // "online / enrolled" stays: 29 enrolled is true and it is the encouraging
  // framing of a network that is growing. nodes_ever_produced rides in the
  // title as its honest companion - enrolling is one tap, contributing is the
  // thing, and most of the enrolled have never sent a sighting.
  //
  // "hours watched" is every heartbeat any camera ever sent, added up. Both
  // node types beat every 30 SECONDS (run_live.py, sparrow-app.js:500), so
  // beats/120 is hours. It is a LOWER BOUND: heartbeats were not always on,
  // dropped beats are never made up, early browser nodes beat at 45s. It
  // undercounts, which is the safe direction for a front-page figure, and it
  // measures patience - most of what this project asks of a volunteer.
  //
  // Every figure with a time window says so ON THE FIGURE. "passes 24h" once
  // carried the qualifier while "public sightings" did not, so the second read
  // as a running total against an all-time count published elsewhere.
  // "Moving now" is the only figure up here that is not the server's - it is
  // the size of the live traffic set this page is already drawing, so the
  // number in the header and the dots on the map are the same fact and cannot
  // disagree. ageTraffic keeps it ticking between these rewrites.
  //
  // The last hour rides in the title because the live figure alone is
  // ambiguous in the direction that matters: 0 is the normal state of a small
  // network on a quiet road, and it looks identical to every camera being off.
  // The hourly count is what tells a visitor which one they are looking at.
  const hour = s.traffic_1h ?? null;
  const movingTitle = hour === null
    ? 'Vehicles crossing a camera in the last 45 seconds.'
    : `Vehicles crossing a camera in the last 45 seconds. ${hour.toLocaleString()} passes in the last hour, so 0 here means a quiet road rather than a network that has stopped.`;
  const everProduced = s.nodes_ever_produced ?? '?';
  // 🚨 THE HEADER WAS ANSWERING A QUESTION NOBODY ASKED IT.
  //
  // Reported: "top of map ui says 88 sightings but there are 188". Measured on
  // the live API: 88 public sightings in the last 24h, 183 all time. BOTH
  // numbers were right. The header printed the 24h figure while the map drew
  // the window the visitor had selected - and `everything` is the DEFAULT, so
  // this was not an edge case somebody wandered into, it was the opening state
  // of the site. A visitor counts the dots, reads the header, and concludes the
  // map is broken. He did, and he was reading it correctly.
  //
  // ⚠️ SAME BUG AS THE PANEL ONE ABOVE (see renderList), FROM THE OTHER SIDE.
  // That time the header showed MORE than the panel; this time it shows fewer.
  // The lesson did not stick because the fix was a hint rather than a shared
  // definition, so fix the definition: the figure is counted from the rows the
  // map is ACTUALLY drawing, which is the rule "moving now" already follows -
  // the number in the header and the dots under it are one fact and cannot
  // disagree.
  //
  // ⚠️ The label has to move WITH the number. Printing 183 under a fixed "24h"
  // would swap a visible contradiction for an invisible lie.
  const wl = WINDOW_LABEL[state.windowS] || `${Math.round(state.windowS / 3600)}h`;
  // Before the first load answers there is nothing drawn to count, and 0 would
  // read as "no government vehicles have ever been seen". Fall back to the
  // server's own 24h figure and say 24h, until the map can speak for itself.
  const shown = _liveArrived ? publicInWindow() : null;
  const pubCount = shown === null ? s.public_24h : shown;
  const pubWindow = shown === null ? '24h' : wl;
  // The fetch asks for at most PUBLIC_LIMIT rows, so a count that lands exactly
  // on it is a floor rather than a total. Say so with a + instead of quietly
  // publishing the cap as if it were the answer.
  const capped = shown !== null && state.sightings.size >= PUBLIC_LIMIT;
  const hours = s.heartbeats_total
    ? `<span title="${s.heartbeats_total.toLocaleString()} heartbeats, one every 30 seconds. A lower bound: heartbeats were not always enabled and dropped ones are never counted."><b>${Math.round(s.heartbeats_total / 120).toLocaleString()}</b> hours watched</span>`
    : '';
  $('#stats').innerHTML = `
    <span title="${everProduced} of these have ever sent a sighting. Enrolling a camera is one tap; keeping one running is the real contribution."><i>${s.nodes_online}</i>/<b>${s.nodes_active}</b> cameras online</span>
    ${hours}
    <span class="movingstat" title="${movingTitle}"><b id="movingnow" class="${movingNow() ? 'on' : ''}">${movingNow().toLocaleString()}</b> moving now</span>
    <span><b>${(s.traffic_24h ?? 0).toLocaleString()}</b> passes 24h</span>
    <span title="Government and police vehicles published with a photo, over the window selected in the panel. Private traffic is counted separately as passes."><i>${pubCount.toLocaleString()}${capped ? '+' : ''}</i> public sightings ${pubWindow}</span>
    <span>${vehicles}</span>`;
}

/* 🚨 NO PERSISTENT STREAM. This used to hold an EventSource('/api/live') open,
   which on a threaded server is ONE PINNED THREAD PER OPEN TAB - it does not
   survive thousands of viewers. Instead the map REFRESHES on a timer against
   the briefly edge-cached /api/sightings, so the crowd is served by Cloudflare
   and the origin sees ~one fetch per window. The `#live` dot now reflects
   whether the last refresh succeeded rather than a socket's state. */
let _refreshing = false;

async function refresh() {
  // ⚠️ ONE AT A TIME. The timer fires every cache bucket regardless of whether
  // the last one finished, so on a slow link the ticks stack and each new
  // request competes with the ones already queued - which makes a struggling
  // connection worse rather than recovering it.
  if (_refreshing) return;
  _refreshing = true;
  const dot = $('#live');
  try {
    await load();
    // Make "live" tangible: the dot reads "live" on a quiet road and
    // "live · N passing" when traffic is actually crossing, so the map
    // obviously IS the live view. paintLive owns the text; this owns whether
    // the connection is up.
    state.online = true;
    if (dot) dot.classList.add('on');
  } catch (e) {
    state.online = false;
    if (dot) dot.classList.remove('on');
  } finally {
    _refreshing = false;
  }
  paintLive();
}

// 🚨 FIRST, AND DELIBERATELY NOT AWAITED. The snapshot is a cached static file
// and usually wins the race against refresh() by a wide margin, so the map has
// dots before the live query has left the building. If it loses, it checks
// _liveArrived and does nothing - see drawSnapshot.
drawSnapshot();
refresh();
loadPending();
loadCameras();
loadPolice();   // draws only if the toggle was left on and we are zoomed in
loadSurveillance();
loadRadar();    // draws only if the toggle was left on; empty until a detector feeds it
radarTimerSync();
_droneLayer.load(); _droneLayer.timerSync();
_radioLayer.load(); _radioLayer.timerSync();
// Towns are independent of the watched-roads toggle: they are what the map
// shows INSTEAD of spans when zoomed out, not a second copy of them, so they
// load whether or not the bands are switched on.
loadPlaces();
loadStats();
applyConfiguredView();
policyBanner();
// Pending is a slow signal - a human deciding takes minutes, not seconds - so
// it is polled far less often than the map data it sits beside.
setInterval(loadPending, 60000);
setInterval(refresh, CACHE_BUCKET_S * 1000);  // new sightings; matches the cache window
// 🚨 30s, HIS CALL, AND IT IS ALSO TEN TIMES LESS LOAD.
// Every open tab was asking for the counters every three seconds. On an
// ordinary day that is invisible; during a viral wave it is the single
// chattiest thing the site does, from the largest number of clients, for a
// figure nobody reads that often. The numbers it drives - cameras online,
// passes today, moving now - do not change meaningfully inside half a minute,
// and nodes_online now has a 5-minute posting window behind it anyway.
setInterval(loadStats, 30000);
setInterval(loadCameras, 5000);   // 'online' reacts within a beat or two

/* Live driver reports: ephemeral, unverified crowd pins from driving mode. An
 * amber ring, deliberately unlike a verified sighting; cleared and redrawn each
 * poll since the server drops the expired ones. */
async function loadReports() {
  let reports;
  try { reports = (await (await fetch('/api/drive/reports')).json()).reports || []; }
  catch (e) { return; }
  state.reportLayer.clearLayers();
  const now = Date.now() / 1000;
  for (const r of reports) {
    const mins = Math.max(0, Math.round((now - r.ts) / 60));
    L.circleMarker([r.lat, r.lon], {
      radius: 8, color: '#f5a623', weight: 2,
      fillColor: '#f5a623', fillOpacity: 0.3,
    }).bindPopup(`Live driver report — patrol<br>${mins}m ago · `
        + `${r.confirms} confirmation${r.confirms === 1 ? '' : 's'}`)
      .addTo(state.reportLayer);
  }
}
loadReports();
setInterval(loadReports, 10000);

/* Patrol hotspots: every confirmed government sighting ever, as a translucent
 * heat so a driver can see which areas run hot. Off by default, toggled by a
 * control button. No plugin - overlapping low-opacity circles glow where
 * patrols cluster; the server already aggregated them to a grid. */
state.heatLayer = L.layerGroup();
state.heatOn = false;
async function loadHeat() {
  let cells;
  try { cells = (await (await fetch('/api/heat')).json()).cells || []; }
  catch (e) { return; }
  let max = 1;
  for (const c of cells) if (c.n > max) max = c.n;
  state.heatCells = cells;
  state.heatMax = max;
  drawHeat();
}
/* Draw (or redraw) the hotspots. Each cell is a translucent red circle whose
 * size and fill grow with how many patrols were logged there, so overlapping
 * cells still glow into a detailed picture zoomed out. Two things keep a LONE
 * hotspot readable without zooming all the way in: a crisp stroked edge, and a
 * floor on its ON-SCREEN radius so it never shrinks to an invisible dot at low
 * zoom. Redrawn on zoom because that floor is measured in screen pixels. */
function drawHeat() {
  const cells = state.heatCells || [], max = state.heatMax || 1;
  state.heatLayer.clearLayers();
  const z = map.getZoom();
  const mpp = 156543.03392 * Math.cos(map.getCenter().lat * Math.PI / 180)
              / Math.pow(2, z);                    // metres per screen pixel
  // Keep the generous on-screen minimum at local/regional zoom (a lone hotspot
  // is easy to spot), but ramp it down past the state level so the dots shrink
  // back into a fine pattern when you pull out to the whole country.
  const zf = Math.max(0, Math.min(1, (z - 5) / 3));  // 0 at USA-wide, 1 at z>=8
  for (const c of cells) {
    const t = c.n / max;
    const meters = 90 + t * 220;                   // physical footprint
    const floorPx = 2 + (7 + t * 9) * zf;          // ~2px zoomed way out, 9-18px local
    L.circle([c.lat, c.lon], {
      radius: Math.max(meters, floorPx * mpp),
      stroke: true, color: '#ff453a', weight: 1.5, opacity: 0.55 + t * 0.35,
      fillColor: '#ff3b30', fillOpacity: 0.18 + t * 0.32,
    }).addTo(state.heatLayer);
  }
}
map.on('zoomend', () => { if (state.heatOn && state.heatCells) drawHeat(); });
/* ⚠️ THE CHECKBOX IS THE STATE NOW, not a button's background colour. This
 * used to tint #heatBtn, which no longer exists - the control moved into the
 * Layers menu. Keeping the box in sync matters because the menu can be closed
 * and reopened, and a switch that shows the wrong position is worse than no
 * switch. */
function toggleHeat() {
  state.heatOn = !state.heatOn;
  if (state.heatOn) {
    loadHeat();
    state.heatLayer.addTo(map);
  } else {
    map.removeLayer(state.heatLayer);
    state.heatLayer.clearLayers();
  }
  const box = document.getElementById('showheat');
  if (box && box.checked !== state.heatOn) box.checked = state.heatOn;
}
/* HeatControl removed: it was one of the three floating map buttons. Its
   behaviour lives on in the Layers menu. */
// 🔥 The heat toggle is a LAYER, so it lives with the layers now.
(function wireMenuControls() {
  const heat = document.getElementById('showheat');
  if (heat) heat.addEventListener('change', () => toggleHeat());
  const mine = document.getElementById('gomine');
  if (mine) mine.addEventListener('click', goMyArea);
  const all = document.getElementById('goall');
  if (all) all.addEventListener('click', goEverything);
})();

/* First-visit nudge: people were not finding the "Add a camera" link, so on a
 * first visit put the invitation front and centre. Shown once per browser
 * (localStorage), dismissible, and built with DOM calls so it survives the CSP.
 */
function showIntro() {
  try { if (localStorage.getItem('sparrow.introSeen')) return; } catch (e) { return; }
  const ov = document.createElement('div');
  Object.assign(ov.style, { position: 'fixed', inset: '0', zIndex: '3000',
    display: 'flex', alignItems: 'center', justifyContent: 'center',
    background: 'rgba(0,0,0,.6)', padding: '20px' });
  const card = document.createElement('div');
  Object.assign(card.style, { maxWidth: '380px', width: '100%',
    background: '#0d1219', border: '1px solid #22303c', borderRadius: '16px',
    padding: '26px', textAlign: 'center', color: '#c7d2dc',
    font: '15px/1.55 system-ui,sans-serif', boxShadow: '0 24px 70px rgba(0,0,0,.6)' });
  const mk = (tag, text, style) => {
    const el = document.createElement(tag);
    if (text) el.textContent = text;
    if (style) Object.assign(el.style, style);
    return el;
  };
  const h = mk('div', 'Watch the watchers',
    { fontSize: '21px', fontWeight: '700', color: '#fff', marginBottom: '10px' });
  // ⚠️ "PRIVATE plates", NOT "plates". A government plate is kept readable and
  // searchable on purpose - that is the entire public tier - and snapshot.py
  // redacts only when tier != "public". The unscoped version of this sentence
  // promised something the system does not do and was never meant to do, which
  // is a worse failure than promising nothing.
  const p = mk('div', 'RavenMap runs on volunteer cameras. Point a spare phone '
    + 'at a street and it maps the patrols that pass. Private plates are '
    + 'destroyed on the device and never uploaded.',
    { color: '#93a3b3', marginBottom: '20px' });
  const add = mk('a', 'Add a camera', { display: 'block', padding: '14px',
    borderRadius: '11px', background: '#3b82f6', color: '#fff', fontWeight: '600',
    textDecoration: 'none', marginBottom: '10px' });
  add.href = '/app';

  /* 🚨 SIGN IN BELONGS HERE MORE THAN ANYWHERE ELSE ON THE SITE, AND THE
   * REASON IS NOT CONVENIENCE.
   *
   * `sparrow.introSeen` lives in the SAME localStorage as the camera key. So
   * the browser eviction that loses somebody their camera - Safari wipes
   * script-writable storage after 7 days for a site not installed to the home
   * screen - also clears this flag. Every volunteer who is about to report
   * that their camera "got deleted" sees THIS CARD FIRST, and until now the
   * only thing it offered them was "Add a camera", which is how they ended up
   * enrolling a second one and orphaning the first. 160 of 262 nodes have
   * never produced anything; one street is enrolled six times.
   *
   * A returning owner and a brand-new visitor are indistinguishable here, so
   * the card has to serve both without pushing either. The camera invitation
   * stays the primary button; the way back is offered beside it, with the one
   * sentence that stops the duplicate being made.
   */
  const row = mk('div', null, { display: 'flex', gap: '8px', marginBottom: '10px' });
  const secondary = {
    flex: '1', display: 'block', padding: '12px 8px', borderRadius: '11px',
    background: '#131c27', border: '1px solid #22303c', color: '#c7d2dc',
    fontWeight: '600', fontSize: '12.5px', textDecoration: 'none',
    textAlign: 'center', cursor: 'pointer', whiteSpace: 'nowrap',
    minWidth: '0' };
  const drive = mk('a', 'Driving', secondary);
  drive.href = '/drive';
  // A shop with a camera already on the street is coverage that costs nobody a
  // spare phone. It was reachable only from a paragraph inside /app.
  const biz = mk('a', 'IP Camera', secondary);
  biz.href = '/IPCamera';
  const signin = mk('a', 'Sign in', secondary);
  signin.href = '/signin';
  row.append(drive, biz, signin);

  const backNote = mk('div',
    'Set up a camera before? Nothing is deleted — sign in with your key rather '
    + 'than adding it again.',
    { color: '#6f8296', fontSize: '12px', lineHeight: '1.5', marginBottom: '14px' });

  const skip = mk('button', 'Just browsing', { display: 'block', width: '100%',
    padding: '12px', borderRadius: '11px', background: 'transparent',
    border: '1px solid #22303c', color: '#7f93a6', cursor: 'pointer',
    font: 'inherit' });
  const done = () => { try { localStorage.setItem('sparrow.introSeen', '1'); } catch (e) {} ov.remove(); };
  skip.addEventListener('click', done);
  // Every route out of this card marks it seen. A link that navigates without
  // doing so brings the card back on the next visit to the map, which reads as
  // the site having forgotten the choice that was just made.
  [add, drive, biz, signin].forEach((el) => el.addEventListener('click', done));
  ov.addEventListener('click', (e) => { if (e.target === ov) done(); });
  card.append(h, p, add, row, backNote, skip);
  ov.appendChild(card);
  document.body.appendChild(ov);
}
setTimeout(showIntro, 700);
setInterval(renderList, 10000);   // keep the "3m ago" column honest
setInterval(ageTraffic, 1000);    // the live traffic view

/* The shared refresh button was removed at his request, and public/refresh.js
 * with it - it was the last `position:fixed` control on the site, which is the
 * thing seven overlap reports in one day were about. The map already refreshes
 * on a timer, so the button was offering to do what was happening anyway.
 *
 * Nothing calls window.sparrowRefresh now, so it is gone too rather than left
 * as a hook with no caller. If a refresh control is ever wanted again it goes
 * IN THE HEADER, in flow - see sitenav.js. */

/* ---- government plate search ------------------------------------------
 *
 * The one control on this map that could be mistaken for the thing RavenMap
 * exists to oppose. So it is built to be honest about its own limits: it can
 * only find a plate on a PUBLIC-tier sighting that a human has confirmed, and
 * the server never scans anything else - not as a display rule, but in the
 * query itself, because a search that scans everything and hides the results
 * still answers the question.
 *
 * The empty state therefore says what the box cannot do, rather than a bare
 * "no results". Somebody typing their own plate in to see whether they are
 * being tracked deserves a straight answer.
 */
(function () {
  const form = document.querySelector('#plateform');
  const box = document.querySelector('#plateresults');
  if (!form || !box) return;
  const input = document.querySelector('#plateq');

  /* The box lives in a dialog now (see index.html). Opening and closing it is
   * all that changed here - the search itself, below, never learns about it.
   *
   * ⚠️ FOCUS AFTER THE UNHIDE, NOT BEFORE. A hidden element cannot take focus,
   * so focusing in the same tick does nothing and the phone keyboard does not
   * appear - which turns a one-tap search into a two-tap one for no visible
   * reason. */
  const modal = document.querySelector('#platemodal');
  const opener = document.querySelector('#plateopen');
  if (modal && opener) {
    const openModal = () => {
      modal.hidden = false;
      /* 🚨 FOCUS IN THE SAME TASK AS THE TAP, NOT IN A FRAME CALLBACK.
       * This used requestAnimationFrame to focus "after the unhide", which
       * works on a desktop and fails on a phone: iOS only opens the keyboard
       * for a focus() that happens inside the user gesture, and a rAF callback
       * is a new task, so the box appeared and the keyboard did not. Measured
       * as document.activeElement staying on <body> after the click.
       * Setting hidden=false takes effect immediately, so the element is
       * already focusable on the very next line - the frame wait bought
       * nothing and cost the keyboard. */
      if (input) { try { input.focus({ preventScroll: true }); } catch (e) { input.focus(); } input.select(); }
    };
    const closeModal = () => {
      modal.hidden = true;
      box.style.display = 'none';
      box.innerHTML = '';
    };
    opener.addEventListener('click', openModal);
    const bg = document.querySelector('#plateclose');
    if (bg) bg.addEventListener('click', closeModal);
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && !modal.hidden) closeModal();
    });
    // Dismissing the keyboard (Done / tap away) closes the search. The short
    // delay lets a tap on Search or a result register first - if focus then
    // landed inside the dialog we keep it open; only a genuine keyboard-away closes.
    if (input) input.addEventListener('blur', () => {
      setTimeout(() => {
        if (!modal.hidden && !modal.contains(document.activeElement)) closeModal();
      }, 200);
    });
    /* Picking a result means "take me there", so the dialog gets out of the
     * way. Bound on the results list rather than inside the click handler
     * below, so a result added by any future code path still closes it. */
    box.addEventListener('click', (e) => {
      if (e.target.closest('.hit')) setTimeout(closeModal, 60);
    });
  }

  const close = () => { box.style.display = 'none'; box.innerHTML = ''; };

  /* 🚨 PLATES ONLY. HIS CALL, and the place half is gone rather than hidden.
   *
   * This box searched plates AND geocoded towns and roads, and the geocode
   * genuinely worked. What did not work was TYPING one: the input is
   * maxlength=12 - a plate's length - so "Grand River Ave" was truncated to
   * "Grand River " before it was ever sent, and autocapitalize=characters
   * shouted every place name back in caps. Then a road search led with "Plate
   * search only finds government vehicles", which reads as a flat refusal even
   * though place results were being rendered above it.
   *
   * His instruction: "just keep it plates on that to keep it simple." So the
   * promise is withdrawn instead of half-kept. One box, one question.
   *
   * ⚠️ /api/geocode STAYS - ipcamera.html uses it to place a camera, and it is
   * a privacy proxy worth keeping. Only this box stopped calling it.
   */
  function render(q, rows) {
    if (!rows.length) {
      box.innerHTML = `<h4>No plate matches ${esc(q)}</h4>
        <div class="none"><b>Only government plates are searchable.</b><br>
        A plate appears here only when a camera published the vehicle as
        a government vehicle <i>and</i> an operator confirmed it.
        Private vehicles are never searched &mdash; their plates are destroyed
        in the image at the camera and never reach this server, so there is
        nothing here to find.</div>`;
      box.style.display = '';
      return;
    }
    box.innerHTML = `<h4>${rows.length} sighting${rows.length === 1 ? '' : 's'}</h4>`
      + rows.map((r) => `
        <div class="hit" data-id="${r.id}" data-lat="${r.lat}" data-lon="${r.lon}">
          ${r.snap ? `<img src="/snap/${encodeURIComponent(r.snap)}" alt="" loading="lazy">` : ''}
          <div>
            <b>${esc(r.plate_text || '')}</b>
            <div class="sub">${esc(r.vclass || '')} &middot; ${ago(r.ts)}</div>
            <div class="sub">${esc(r.node_id || '')}</div>
          </div>
        </div>`).join('');
    box.style.display = '';
  }

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const q = input.value.trim();
    if (q.length < 3) {
      box.innerHTML = `<div class="none">Type at least three characters.</div>`;
      box.style.display = '';
      return;
    }
    /* One box, ONE question - see the note on render(). The place lookup that
     * used to run alongside this was removed at his request; the box asks for
     * a plate and answers about plates. */
    try {
      const plates = await fetch('/api/plate?q=' + encodeURIComponent(q))
        .then((x) => x.json()).catch(() => ({ results: [] }));
      // 🚨 AN ERROR IS NOT AN EMPTY RESULT SET.
      // The server answers a failed lookup with {"error": ...} and no
      // `results`, and this rendered that as "no matches" - so a rate-limit or
      // an outage looked exactly like "that plate is not on the map". Somebody
      // searching a plate would conclude it had never been seen, and nothing
      // anywhere would record a failure. The class of bug this codebase keeps
      // finding: something broke, and the UI reported a confident wrong answer.
      if (plates.error && !(plates.results || []).length) {
        box.innerHTML = `<div class="none">Search is temporarily unavailable`
          + ` &mdash; ${esc(String(plates.error).slice(0, 80))}</div>`;
        box.style.display = '';
        return;
      }
      render(q, plates.results || []);
    } catch (err) {
      box.innerHTML = `<div class="none">Search unavailable.</div>`;
      box.style.display = '';
    }
  });

  // Clicking a hit flies the map to it, which is the only reason to have the
  // result list at all - a plate on its own tells you nothing.
  box.addEventListener('click', (e) => {
    const hit = e.target.closest('.hit');
    if (!hit) return;
    const lat = parseFloat(hit.dataset.lat), lon = parseFloat(hit.dataset.lon);
    // The `.place` branch that used to live here went with the place search
    // itself - nothing emits a .place hit any more.
    // `map` is the module-level Leaflet instance declared above; this IIFE is
    // in the same file and the same scope, so no global is needed.
    if (!isNaN(lat) && !isNaN(lon)) map.setView([lat, lon], 17);
    close();
  });

  document.addEventListener('click', (e) => {
    if (!box.contains(e.target) && !form.contains(e.target)) close();
  });
  input.addEventListener('keydown', (e) => { if (e.key === 'Escape') close(); });
})();

/* ---- the site as one program ------------------------------------------
 *
 * Map, About and Transparency as modes rather than pages. The content for the
 * two text modes is FETCHED from /about and /transparency instead of being
 * copied in here, so the copy has exactly one home. A promise about what this
 * project does with people's data must not be able to say one thing on a page
 * and something slightly different in a panel.
 *
 * /about and /transparency still work as standalone URLs - they are linked
 * from documentation and may be bookmarked - and they are also the source this
 * reads from, so neither can silently drift from the other.
 */
(function modes() {
  const bar = document.querySelector('#modes');
  if (!bar) return;
  const loaded = {};

  async function fill(name) {
    const pane = document.querySelector('#pane-' + name);
    if (loaded[name]) return;
    try {
      const html = await fetch('/' + name).then((r) => r.text());
      // Take the page's body AND its <style>. The standalone pages carry their
      // own styles in <head>; injecting the body alone dropped them, so the
      // panel rendered unstyled - stat numbers glued to their labels, tables
      // and tier boxes unformatted. A <style> IS applied when set via innerHTML
      // (only <script> is inert), so this keeps the panel identical to the
      // standalone page from one source, with no CSS copied into style.css.
      const doc = new DOMParser().parseFromString(html, 'text/html');
      const body = doc.querySelector('.doc') || doc.body;
      const styles = [...doc.querySelectorAll('style')].map((s) => s.outerHTML).join('');
      pane.querySelector('.paneinner').innerHTML = styles + body.innerHTML;
      loaded[name] = true;
      // innerHTML never runs scripts, so the transparency panel has to be
      // started by hand - from the same shared module the standalone page
      // uses, not a second copy of the rendering.
      if (name === 'transparency' && window.sparrowTransparency) {
        window.sparrowTransparency();
      }
    } catch (e) {
      pane.querySelector('.paneinner').innerHTML =
        '<p class="note">Could not load this section.</p>';
    }
  }

  function go(name) {
    document.querySelectorAll('.pane').forEach((p) => { p.hidden = true; });
    bar.querySelectorAll('button').forEach((b) =>
      b.classList.toggle('on', b.dataset.m === name));
    if (name !== 'map') {
      document.querySelector('#pane-' + name).hidden = false;
      fill(name);
    } else {
      // Leaflet mis-sizes itself if the container changed while hidden.
      setTimeout(() => map.invalidateSize(), 50);
    }
    // A shareable URL per mode, without a page load.
    history.replaceState(null, '', name === 'map' ? '/' : '/#' + name);
  }

  bar.addEventListener('click', (e) => {
    const b = e.target.closest('button[data-m]');
    if (b) go(b.dataset.m);
  });

  // Opening /#about lands straight on it, so a link to a section still works.
  const initial = (location.hash || '').replace('#', '');
  if (['about', 'transparency'].includes(initial)) go(initial);
})();
