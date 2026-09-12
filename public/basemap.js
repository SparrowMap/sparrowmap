// 🗺️ THE ONE BASEMAP for every SparrowMap page that draws a Leaflet map.
//
// Self-hosted vector tiles (planet.pmtiles on the box, rendered by MapLibre
// through the Leaflet bridge), with the Carto raster proxy kept ONLY as a
// last-resort fallback. Carto rate-limits our proxy IP and then serves real map
// tiles with "API KEY REQUIRED" stamped across them - the box's placeholder
// filter cannot catch those because they are a genuine map with text on top.
// The main map moved off Carto on 2026-08-27; driving mode, aim, ipcamera and
// planes kept their own L.tileLayer('/api/tile/...') and all four showed the
// watermark on 2026-09-11. One copy here, so a page can never be left behind.
//
// Usage (after /vendor/leaflet.js, /vendor/maplibre-gl.js and
// /vendor/leaflet-maplibre-gl.js):   sparrowBasemap(map)
// `?raster=1` on any page is the escape hatch back to Carto.
//
// 🚨 WHY THERE IS A FRAME PUMP (read before "simplifying" this)
// MapLibre gates its ENTIRE initial style load behind one requestAnimationFrame:
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
(function () {
  'use strict';

  var STYLE_URL = '/basemap/style.json?v=5';

  function startFramePump() {
    var rAF = window.requestAnimationFrame.bind(window);
    var cAF = window.cancelAnimationFrame.bind(window);
    var active = true;
    window.requestAnimationFrame = function (cb) {
      var fired = false;
      var once = function (t) { if (fired) return; fired = true; try { cb(t); } catch (e) { /* keep the loop alive */ } };
      var r = rAF(once);
      // 60ms ~ one very slow frame; only ever matters when the native frame is
      // starved (hidden tab / no paint). When rAF is healthy `once` de-dupes it away.
      var s = setTimeout(function () { once(performance.now()); }, 60);
      return { __pump: 1, r: r, s: s };
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

  function addRasterBasemap(map, maxZoom) {
    return L.tileLayer('/api/tile/{z}/{x}/{y}.png', {
      attribution: '&copy; OpenStreetMap contributors &copy; CARTO &middot; SparrowMap',
      maxZoom: maxZoom,
    }).addTo(map);
  }

  // opts.maxZoom: the raster fallback's ceiling (default 20). Returns the layer
  // that was added, so a caller can remove it if it ever needs to.
  window.sparrowBasemap = function (map, opts) {
    opts = opts || {};
    var maxZoom = opts.maxZoom || 20;
    var wantVector = !new URLSearchParams(location.search).has('raster')
      && typeof L.maplibreGL === 'function' && window.maplibregl;
    if (!wantVector) return addRasterBasemap(map, maxZoom);

    try {
      // Pump frames across map construction so the rAF-gated style load always runs.
      var stopPump = startFramePump();
      // Never hold the global rAF override longer than a few seconds, whatever happens.
      var hardStop = setTimeout(stopPump, 8000);

      var glLayer = L.maplibreGL({
        style: STYLE_URL,
        attribution: '&copy; OpenStreetMap contributors &middot; SparrowMap',
      }).addTo(map);

      // The bridge builds its maplibregl.Map synchronously inside addTo(), so the
      // handle is available now. Latch a ONE-SHOT "the basemap came up" flag off the
      // first load/idle - and end the pump there. 'load' and 'idle' each fire once the
      // first full frame is on screen; whichever comes first latches basemapUp. The 8s
      // hardStop is the backstop if neither fires (e.g. a tab that stays hidden).
      // ⚠️ Do NOT use isStyleLoaded() for this - it is false during ANY tile load,
      // including a normal zoom, so sampling it later would wrongly declare failure.
      var getGl = function () { return (glLayer.getMaplibreMap && glLayer.getMaplibreMap()) || glLayer._glMap; };
      var glMap = getGl();
      // basemapUp latches TRUE the instant MapLibre's style DEFINITION has loaded - the
      // moment the rAF gate we are fighting clears and the vector map is going to render.
      // It never un-latches, so a later zoom (which reloads tiles and makes
      // isStyleLoaded() briefly false) can never make the watchdog think we failed, and a
      // slow phone still streaming tiles is not mistaken for a failure either.
      var basemapUp = false;
      var endPump = function () { clearTimeout(hardStop); stopPump(); };
      if (glMap) {
        glMap.on('styledata', function () { if (glMap.style && glMap.style._loaded) basemapUp = true; });
        glMap.once('load', endPump);
        glMap.once('idle', endPump);
      }

      // 🚨 THE BRIDGE'S resize GAP: leaflet-maplibre-gl's _resize only re-sizes the
      // container DIV and jumpTo()s - it NEVER calls _glMap.resize(). So a container
      // that lays out a beat after the GL map is built (flex column / first paint)
      // leaves MapLibre's internal size stale. Re-size the container AND the GL map
      // whenever the map element actually has a size; a ResizeObserver re-syncs on
      // first layout, rotate and panel toggles too.
      var syncSize = function () {
        try {
          if (glLayer._resizeContainer) glLayer._resizeContainer();
          var g = getGl();
          if (g && g.resize) g.resize();
        } catch (e) { /* never let a basemap hiccup take the whole map down */ }
      };
      var mapEl = map.getContainer();
      if (window.ResizeObserver && mapEl) new ResizeObserver(syncSize).observe(mapEl);
      map.whenReady(syncSize);

      // 🛡️ WATCHDOG: keeping the map visible outranks the vector upgrade. If the
      // basemap NEVER came up (basemapUp still false after 8s - some environment the
      // pump did not foresee), drop it and fall back to the Carto raster proxy so it is
      // never blank. Keyed off the latched basemapUp flag, NOT isStyleLoaded(), so a
      // user zooming (which reloads tiles) can never trip it once the map is working.
      setTimeout(function () {
        if (basemapUp) return;
        try {
          if (glLayer && map.hasLayer(glLayer)) map.removeLayer(glLayer);
          addRasterBasemap(map, maxZoom);
        } catch (e) { try { addRasterBasemap(map, maxZoom); } catch (_) { /* give up quietly */ } }
      }, 8000);
      return glLayer;
    } catch (e) {
      // Anything at all goes wrong standing up the vector map -> raster, immediately.
      return addRasterBasemap(map, maxZoom);
    }
  };
})();
