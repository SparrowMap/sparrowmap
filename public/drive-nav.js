/* Turn-by-turn navigation for driving mode, on our own routing engine.
 *
 *   window.DriveNav.start({map, pos, heading, speed, toast, beep, haversine,
 *                          bearing, soundOn})
 *
 * Kept out of drive.html's closure on purpose. The driving page's job is to
 * warn a driver about patrols and it must keep doing that whatever happens
 * here, so navigation is a separate file that is handed what it needs and can
 * fail to load without taking the alerts down with it.
 *
 * 🚨 WHAT LEAVES THE PHONE, EXACTLY.
 *   - the destination you searched for, to /api/geocode (proxied here, so no
 *     third party ever sees it or your address)
 *   - two coordinates, to /api/route, when you press Go
 *   - one coordinate every few seconds while moving, to /api/speedlimit
 * None of it is written down at the other end - see nav.py, and /api/policy
 * publishes route_logging:false so the claim can be diffed against the config
 * rather than believed. The cover copy says this in a sentence; this comment
 * is the long version, and the two must not drift apart.
 *
 * WHAT THIS DELIBERATELY DOES NOT DO: speak. A voice needs an audio session
 * that survives the screen locking, ducking against the radio, and a language
 * choice, and half-built voice guidance is worse than none - a driver who
 * expects to be told about the turn stops watching for it. The banner is
 * large, the distance counts down, and the existing alert tone is reused for
 * the turn itself.
 */
window.DriveNav = (function () {
  "use strict";
  var $ = function (s) { return document.querySelector(s); };
  var H = null;                 // the hook from drive.html
  var dest = null;              // {lat, lon, label}
  var trip = null;              // the current Valhalla trip
  var line = null, destMarker = null;
  var shape = [];               // decoded route geometry, [[lat,lon],...]
  var manIdx = 0;               // which maneuver we are on
  var offSince = 0;             // when we first looked off-route
  var limit = { mph: null, road: null };
  var limitAt = null;           // where the last speed-limit answer was for
  var opts = { hotspots: true, highways: false, tolls: false, alpr: false, alpr_detour: 8 };
  var camLayer = null;          // ALPR cameras on/around the route
  var overview = false;         // route-overview (zoomed out) or follow

  /* Valhalla returns the route as an encoded polyline at 1e6 precision, not
   * the 1e5 every other polyline library assumes. Decoding at the wrong
   * precision does not throw - it produces a line a few hundred metres long
   * somewhere off the coast of Africa, which is a confusing way to learn this.
   */
  function decode(str, precision) {
    var index = 0, lat = 0, lng = 0, out = [], shift, result, byte, factor;
    factor = Math.pow(10, precision === undefined ? 6 : precision);
    while (index < str.length) {
      shift = 0; result = 0;
      do { byte = str.charCodeAt(index++) - 63; result |= (byte & 0x1f) << shift; shift += 5; }
      while (byte >= 0x20);
      lat += ((result & 1) ? ~(result >> 1) : (result >> 1));
      shift = 0; result = 0;
      do { byte = str.charCodeAt(index++) - 63; result |= (byte & 0x1f) << shift; shift += 5; }
      while (byte >= 0x20);
      lng += ((result & 1) ? ~(result >> 1) : (result >> 1));
      out.push([lat / factor, lng / factor]);
    }
    return out;
  }

  function miles(km) { return km * 0.621371; }
  function fmtDist(m) {
    if (m < 160) return Math.round(m / 10) * 10 + ' m';
    if (m < 1609) return (m / 1609.34).toFixed(1) + ' mi';
    return Math.round(m / 1609.34) + ' mi';
  }
  function fmtMins(s) {
    var m = Math.round(s / 60);
    if (m < 60) return m + ' min';
    return Math.floor(m / 60) + ' h ' + (m % 60) + ' m';
  }

  // ---- the destination sheet -------------------------------------------
  function sheet() {
    var el = $('#navsheet');
    if (el) return el;
    el = document.createElement('div');
    el.id = 'navsheet';
    el.innerHTML =
      '<div class="ns-card" role="dialog" aria-label="Navigate somewhere">' +
        '<form id="nsform" autocomplete="off">' +
          '<input id="nsq" type="search" placeholder="Where to?" aria-label="Destination">' +
          '<button type="submit">Find</button>' +
        '</form>' +
        // Close, Stop and Route options ride at the TOP now, right under the
        // search bar, so the turn list below is the first thing you see when
        // you open the sheet from the card. His call, 2026-09-27.
        '<div class="ns-row">' +
          '<button class="ns-cancel" id="nsCancel">Close</button>' +
          '<button class="ns-stop" id="nsStop">Stop navigating</button>' +
        '</div>' +
        '<details class="ns-opts" id="nsOpts">' +
          '<summary>Route options</summary>' +
          '<label><input type="checkbox" id="nsHot" checked> Avoid police hotspots</label>' +
          '<label><input type="checkbox" id="nsHwy"> Avoid highways</label>' +
          '<label><input type="checkbox" id="nsToll"> Avoid tolls</label>' +
          '<label id="nsAlprRow" style="display:none"><input type="checkbox" id="nsAlpr"> Avoid license-plate cameras</label>' +
          '<label id="nsAlprDetourRow" class="ns-sub" style="display:none">How far you’ll go to avoid them' +
            '<select id="nsAlprDetour">' +
              '<option value="3">A little (+3 mi)</option>' +
              '<option value="8" selected>Balanced (+8 mi)</option>' +
              '<option value="25">Far (+25 mi)</option>' +
              '<option value="none">As far as it takes</option>' +
            '</select></label>' +
        '</details>' +
        '<div id="nsresults"></div>' +
        '<div id="nsturns"></div>' +
        '<div class="ns-note">Routed on SparrowMap’s own machine, never logged, ' +
          'never shared.</div>' +
      '</div>';
    document.body.appendChild(el);
    $('#nsCancel').onclick = function () { closeSheet(); };
    $('#nsStop').onclick = function () { stop(); closeSheet(); };
    $('#nsform').onsubmit = function (e) { e.preventDefault(); search($('#nsq').value); };
    ['nsHot', 'nsHwy', 'nsToll', 'nsAlpr', 'nsAlprDetour'].forEach(function (id) {
      var box = $('#' + id);
      if (!box) return;
      box.onchange = function () {
        opts.hotspots = $('#nsHot').checked;
        opts.highways = $('#nsHwy').checked;
        opts.tolls = $('#nsToll').checked;
        var al = $('#nsAlpr'); opts.alpr = !!(al && al.checked);
        var ad = $('#nsAlprDetour'); if (ad) opts.alpr_detour = ad.value;
        // The detour picker only matters, and only shows, when avoidance is on.
        var dr = $('#nsAlprDetourRow'); if (dr) dr.style.display = opts.alpr ? '' : 'none';
        if (dest) go(dest);        // re-route immediately: the toggle IS the ask
      };
    });
    return el;
  }
  /* The whole turn list, in order, under the search bar. His ask: tapping the
   * turn card should show every turn, not just the next one. Built from the
   * route we already have - no request - and only while navigating; with no
   * route the slot is empty and the sheet is just the destination search. */
  function fillTurns() {
    var box = $('#nsturns');
    if (!box) return;
    var leg = trip && trip.legs && trip.legs[0];
    var mans = leg && leg.maneuvers;
    if (!mans || !mans.length) { box.innerHTML = ''; return; }
    var html = '<div class="ns-turns-h">Turns</div>';
    for (var i = 0; i < mans.length; i++) {
      var m = mans[i];
      var d = m.length ? fmtDist(m.length * 1609.34) : '';
      html += '<div class="ns-turn' + (i === manIdx ? ' now' : '') + '">' +
        '<span class="ns-turn-t">' + (m.instruction || '') + '</span>' +
        '<span class="ns-turn-d">' + d + '</span></div>';
    }
    box.innerHTML = html;
  }

  function openSheet(withTurns) {
    sheet().className = 'show';
    fillTurns();
    // Only steal focus to the search box when the driver opened it to search.
    // Tapping the turn card to read the list should not pop the keyboard up.
    if (H && H.setUiPaused) H.setUiPaused(true);   // stop the heavy loops while typing
    if (!withTurns) setTimeout(function () { $('#nsq').focus(); }, 30);
  }
  function closeSheet() {
    var el = $('#navsheet'); if (el) el.className = '';
    if (H && H.setUiPaused) H.setUiPaused(false);
  }

  function search(q) {
    q = (q || '').trim();
    if (q.length < 3) { H.toast('Type a place to search for'); return; }
    var box = $('#nsresults');
    box.innerHTML = '<div class="ns-empty">Searching…</div>';
    // A SUBMITTED search sends a COARSE position so "closest walmart" works -
    // the server rounds it to ~1 km and only a submitted query carries it,
    // never a keystroke. Without a fix yet, it just searches by name.
    var at = H.pos && H.pos();
    var near = at ? '&near=' + at[0].toFixed(3) + ',' + at[1].toFixed(3) : '';
    fetch('/api/geocode?q=' + encodeURIComponent(q) + near)
      .then(function (r) { return r.json(); })
      .then(function (d) {
        var rows = (d && d.results) || [];
        if (!rows.length) {
          box.innerHTML = '<div class="ns-empty">' +
            (d && d.error ? 'Search is unavailable right now.' : 'Nothing found for that.') +
            '</div>';
          return;
        }
        box.innerHTML = '';
        rows.slice(0, 6).forEach(function (r) {
          var lat = +r.lat, lon = +r.lon;
          if (isNaN(lat) || isNaN(lon)) return;
          var name = r.name || (lat.toFixed(4) + ', ' + lon.toFixed(4));
          var b = document.createElement('button');
          b.className = 'ns-hit'; b.type = 'button'; b.textContent = name;
          b.onclick = function () { $('#nsq').value = ''; $('#nsresults').innerHTML = ''; closeSheet(); go({ lat: lat, lon: lon, label: name }); };
          box.appendChild(b);
        });
      })
      .catch(function () {
        // An error is not an empty result set - the same rule the plate search
        // learned. "Nothing found" would tell a driver the place does not
        // exist when in fact the lookup never happened.
        box.innerHTML = '<div class="ns-empty">Search is unavailable right now.</div>';
      });
  }

  // ---- routing ----------------------------------------------------------
  function go(d) {
    var at = H.pos();
    if (!at) { H.toast('Waiting for a GPS fix…'); return; }
    dest = d;
    H.toast('Finding a route…');
    fetch('/api/route', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ from: at, to: [d.lat, d.lon], avoid: opts })
    }).then(function (r) {
      return r.json().then(function (j) { return { ok: r.ok, j: j }; });
    }).then(function (res) {
      if (!res.ok) { H.toast(res.j && res.j.error ? res.j.error : 'No route found'); return; }
      draw(res.j);
      if (res.j.hotspot_fallback) {
        // 🚨 SAY IT. They asked to avoid hotspots and are not getting that.
        // Silently routing through the thing someone asked to avoid is the
        // worst failure this feature has, because it looks like success.
        H.toast('No route avoids every hotspot — this one goes through some');
      } else if (res.j.avoided_hotspots) {
        H.toast('Routing around known hotspots');
      }
      // ALPR cameras, reported honestly: the ones we could not dodge are
      // almost always on the unavoidable first/last mile, so say that rather
      // than reading as a failure.
      var onr = res.j.alpr_on_route || [];
      var endN = 0, midN = 0;
      onr.forEach(function (c) { if (c[2] && c[2] !== 'mid') endN++; else midN++; });
      if (res.j.avoided_alpr) {
        if (!onr.length) H.toast('Routed around every plate camera');
        else if (!midN) H.toast('Routed around the cameras — ' + endN +
          ' left near your start/finish (unavoidable)');
        else H.toast('Routed around most cameras — ' + onr.length +
          ' could not be avoided');
      } else if (res.j.alpr_fallback) {
        if (onr.length && !midN) H.toast('The plate camera' +
          (onr.length > 1 ? 's here are' : ' here is') +
          ' near your start/finish — unavoidable');
        else H.toast('No route avoids every plate camera — this one passes some');
      }
    }).catch(function () { H.toast('Navigation is unavailable'); });
  }

  function draw(out) {
    trip = out.trip;
    var leg = trip && trip.legs && trip.legs[0];
    if (!leg) { H.toast('No route found'); return; }
    shape = decode(leg.shape, 6);
    manIdx = 0; offSince = 0; lastIdx = -1;
    if (line) H.map.removeLayer(line);
    line = L.polyline(shape, { color: '#3b82f6', weight: 7, opacity: 0.85 }).addTo(H.map);
    if (destMarker) H.map.removeLayer(destMarker);
    destMarker = L.circleMarker([dest.lat, dest.lon], {
      radius: 8, color: '#fff', weight: 2, fillColor: '#3b82f6', fillOpacity: 1
    }).addTo(H.map);
    drawCams(out);
    banner();
  }

  /* A little camera glyph for the map. Built once per marker; Leaflet caches
   * nothing here, so it stays a plain inline SVG in a divIcon - no image
   * request, works offline, and colours to match on-route vs avoided. */
  function camIcon(color, filled) {
    var svg = '<svg width="17" height="17" viewBox="0 0 24 24" fill="' +
      (filled ? color : 'rgba(20,26,34,.55)') + '" stroke="' + color +
      '" stroke-width="2" stroke-linejoin="round">' +
      '<path d="M4 8.5h3L8.5 6h7L17 8.5h3V19H4z"/>' +
      '<circle cx="12" cy="13" r="3" fill="' + (filled ? '#fff' : 'none') +
      '" stroke="' + color + '"/></svg>';
    return L.divIcon({ html: svg, className: 'camic', iconSize: [17, 17],
      iconAnchor: [8, 9] });
  }

  /* The license-plate cameras, so a driver can zoom out and SEE what is being
   * routed around. Red = still on this route (unavoidable here), amber ring =
   * one the plain route would have hit and this one skirts. These are fixed
   * public camera positions, the same the avoidance is computed from. */
  function drawCams(out) {
    if (camLayer) { H.map.removeLayer(camLayer); camLayer = null; }
    var on = (out && out.alpr_on_route) || [], av = (out && out.alpr_avoided) || [];
    if (!on.length && !av.length) return;
    camLayer = L.layerGroup();
    // Draw them AS CAMERAS, not dots (his call): amber outline = one this route
    // avoids, solid red = one it still passes.
    av.forEach(function (c) {
      L.marker([c[0], c[1]], { icon: camIcon('#f59e0b', false),
        interactive: false, keyboard: false }).addTo(camLayer);
    });
    on.forEach(function (c) {
      var tag = c[2] || 'mid';
      var un = (tag !== 'mid');
      var label = tag === 'start' ? 'Camera near your start — unavoidable'
                : tag === 'end' ? 'Camera near your destination — unavoidable'
                : 'Camera on your route — could not route around it';
      L.marker([c[0], c[1]], {
        icon: camIcon(un ? '#9aa7b4' : '#ef4444', !un),
        interactive: true, keyboard: false
      }).bindTooltip(label, { direction: 'top' }).addTo(camLayer);
    });
    camLayer.addTo(H.map);
  }

  /* Pull the map out to the whole trip so the cameras and the route are visible
   * at once, then a tap on the same button drops back to driving. Follow mode
   * is paused for the overview or the next GPS fix would snap back to the car. */
  function toggleOverview() {
    overview = !overview;
    var b = $('#nbOv');
    if (overview && shape.length) {
      if (H.setFollow) H.setFollow(false);
      if (H.markProgZoom) H.markProgZoom();
      H.map.fitBounds(L.latLngBounds(shape), { padding: [50, 50] });
      if (b) b.textContent = 'Driving';
    } else {
      if (H.setFollow) H.setFollow(true);
      if (b) b.textContent = 'Overview';
    }
  }

  function stop() {
    dest = null; trip = null; shape = []; manIdx = 0; lastIdx = -1;
    if (line) { H.map.removeLayer(line); line = null; }
    if (destMarker) { H.map.removeLayer(destMarker); destMarker = null; }
    if (camLayer) { H.map.removeLayer(camLayer); camLayer = null; }
    overview = false; if (H.setFollow) H.setFollow(true);
    var b = $('#navbar'); if (b) b.className = '';
    var sp = $('#splimit'); if (sp) sp.style.display = '';
    H.toast('Navigation stopped');
  }

  /* Which maneuver are we on? Valhalla numbers each maneuver's begin and end
   * shape index, so the answer is "the one whose stretch of road contains the
   * point on the line nearest the car" - not the nearest maneuver by straight
   * line, which on a cloverleaf picks the exit you already took.
   *
   * 🚨 SEARCHED IN A WINDOW, NOT OVER THE WHOLE ROUTE. The routing graph is
   * the entire United States, so a long trip's shape is tens of thousands of
   * points, and scanning all of them once a second is tens of thousands of
   * haversines a second on a phone that is also decoding camera frames. A car
   * moves a few points along the line per second, so only the stretch around
   * where it was last needs looking at.
   *
   * The full scan stays as the fallback for the two cases the window cannot
   * answer: the first fix after a route is drawn, and a driver who has left
   * the corridor entirely - which is exactly when being off-route has to be
   * detected rather than missed. */
  var lastIdx = -1;
  function scan(at, from, to) {
    var bi = from, bd = Infinity;
    for (var i = from; i < to; i++) {
      var d = H.haversine(at[0], at[1], shape[i][0], shape[i][1]);
      if (d < bd) { bd = d; bi = i; }
    }
    return { i: bi, d: bd };
  }
  function nearestIdx(at) {
    var W = 250;                       // points either side; ~1-2 km of road
    if (lastIdx >= 0) {
      var r = scan(at, Math.max(0, lastIdx - W), Math.min(shape.length, lastIdx + W));
      // Trust the window only while the car is plausibly ON the line. Once it
      // is not, the answer may simply be the nearest point of the wrong
      // stretch, so fall through and look at everything.
      if (r.d < 200) { lastIdx = r.i; return r; }
    }
    var full = scan(at, 0, shape.length);
    lastIdx = full.i;
    return full;
  }

  function banner() {
    var b = $('#navbar');
    if (!b) {
      b = document.createElement('div'); b.id = 'navbar';
      b.innerHTML =
        '<div class="nb-top">' +
          '<div class="nb-turn" id="nbTurn"></div>' +
          '<div class="nb-lim unknown" id="nbLim"><div class="l-cap">SPEED<br>LIMIT</div>' +
            '<div class="l-num" id="nbLimNum">—</div></div>' +
        '</div>' +
        '<div class="nb-sub"><span id="nbDist"></span>' +
          '<span><span id="nbEta"></span>' +
          '<button type="button" class="nb-ov" id="nbOv">Overview</button></span></div>';
      document.body.appendChild(b);
      // A tap on the card text opens the destination sheet; the buttons do not.
      $('#nbTurn').onclick = function () { openSheet(true); };
      $('#nbOv').onclick = function (e) { e.stopPropagation(); toggleOverview(); };
      paintLimit();
    }
    var leg = trip.legs[0], m = leg.maneuvers[manIdx];
    if (!m) { b.className = ''; return; }
    $('#nbTurn').textContent = m.instruction || '';
    b.className = 'show';
    // The card now carries the limit, so the standalone plate would be a
    // duplicate while navigating.
    var sp = $('#splimit'); if (sp) sp.style.display = 'none';
  }

  function tick() {
    var at = H.pos();
    if (!at) return;
    speedTick(at);
    if (!trip || !shape.length) return;
    var leg = trip.legs[0];
    var n = nearestIdx(at);

    /* OFF ROUTE. One bad GPS fix in a tunnel or under a bridge must not
     * trigger a reroute, so being off the line has to PERSIST before it
     * counts - otherwise a driver going perfectly straight gets a recalculated
     * route every time the phone loses a satellite. */
    if (n.d > 60) {
      if (!offSince) offSince = Date.now();
      else if (Date.now() - offSince > 12000) {
        offSince = 0;
        H.toast('Off route — recalculating');
        go(dest);
        return;
      }
    } else { offSince = 0; }

    // advance the maneuver pointer to the one whose stretch we are in
    while (manIdx + 1 < leg.maneuvers.length &&
           n.i >= leg.maneuvers[manIdx + 1].begin_shape_index) manIdx++;
    var m = leg.maneuvers[manIdx], nx = leg.maneuvers[manIdx + 1];
    if (!m) return;

    // distance to the END of this maneuver = where the next turn happens
    var endI = Math.min(m.end_shape_index, shape.length - 1);
    var d = 0;
    for (var i = n.i; i < endI; i++) {
      d += H.haversine(shape[i][0], shape[i][1], shape[i + 1][0], shape[i + 1][1]);
    }
    $('#nbTurn').textContent = (nx ? nx.instruction : m.instruction) || '';
    $('#nbDist').textContent = nx ? fmtDist(d) : 'Arriving';

    // time left over the whole remaining trip, not just this turn
    var left = 0;
    for (var k = manIdx; k < leg.maneuvers.length; k++) left += (leg.maneuvers[k].time || 0);
    $('#nbEta').textContent = fmtMins(left);

    // one tone as the turn comes up, reusing the page's own alert sound so a
    // route cue and a patrol cue are the same vocabulary
    if (nx && d < 120 && !nx._cued) { nx._cued = true; if (H.soundOn()) H.beep(); }
    if (nx && d > 400) nx._cued = false;

    if (!nx && d < 40) { H.toast('You have arrived'); stop(); }
  }

  /* SPEED LIMIT. Asked for the car's own position, never for the route, so it
   * works whether or not you are navigating - a driver wants to know the limit
   * on the road they are on more often than they want directions.
   *
   * 🚨 A DASH IS A REAL ANSWER. Most American roads have no maxspeed in
   * OpenStreetMap, so the usual reply is "not known here". Showing the road
   * class's typical speed instead would be inventing a limit a driver could be
   * fined for trusting. */
  function speedTick(at) {
    if (limitAt && H.haversine(at[0], at[1], limitAt[0], limitAt[1]) < 120) return;
    limitAt = at;
    fetch('/api/speedlimit?lat=' + at[0].toFixed(5) + '&lon=' + at[1].toFixed(5))
      .then(function (r) { return r.json(); })
      .then(function (d) { limit = d || { mph: null }; paintLimit(); })
      .catch(function () { /* keep the last known rather than flashing a dash */ });
  }
  function paintLimit() {
    var el = $('#splimit');
    if (!el) {
      el = document.createElement('div'); el.id = 'splimit';
      el.innerHTML = '<div class="sl-cap">SPEED<br>LIMIT</div><div class="sl-num" id="slNum">—</div>';
      document.body.appendChild(el);
    }
    $('#slNum').textContent = limit.mph != null ? limit.mph : '—';
    el.className = 'show' + (limit.mph == null ? ' unknown' : '');
    // The same value in the turn card, when navigating. The standalone plate
    // (#splimit) stays for when the driver is not navigating.
    var cn = $('#nbLimNum'), cl = $('#nbLim');
    if (cn) cn.textContent = limit.mph != null ? limit.mph : '—';
    if (cl) cl.className = 'nb-lim' + (limit.mph == null ? ' unknown' : '');
  }

  function start(hook) {
    H = hook;
    var btn = $('#navBtn');
    if (btn) btn.onclick = openSheet;
    // Is the engine even up? A Go button that silently does nothing is worse
    // than one that says navigation is offline.
    fetch('/api/nav/status').then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d || !d.available) {
          if (btn) { btn.style.opacity = '.45'; btn.title = 'Navigation is offline'; btn.onclick = function () { H.toast('Navigation is offline right now'); }; }
        }
        // Only offer plate-camera avoidance when there is a snapshot
        // behind it; otherwise the toggle would do nothing.
        if (d && d.alpr) { sheet(); var row = $('#nsAlprRow'); if (row) row.style.display = ''; }
      }).catch(function () {});
    setInterval(tick, 1000);
    paintLimit();
  }

  return { start: start, stop: stop, go: go, openSheet: openSheet };
})();
