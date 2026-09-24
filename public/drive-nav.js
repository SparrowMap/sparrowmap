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
  var opts = { hotspots: true, highways: false, tolls: false };

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
        '<div id="nsresults"></div>' +
        '<div class="ns-opts">' +
          '<label><input type="checkbox" id="nsHot" checked> Avoid police hotspots</label>' +
          '<label><input type="checkbox" id="nsHwy"> Avoid highways</label>' +
          '<label><input type="checkbox" id="nsToll"> Avoid tolls</label>' +
        '</div>' +
        '<div class="ns-note">Your destination is routed on SparrowMap’s own ' +
          'machine and is never logged, and never reaches anyone else.</div>' +
        '<div class="ns-row">' +
          '<button class="ns-cancel" id="nsCancel">Close</button>' +
          '<button class="ns-stop" id="nsStop">Stop navigating</button>' +
        '</div>' +
      '</div>';
    document.body.appendChild(el);
    $('#nsCancel').onclick = function () { el.className = ''; };
    $('#nsStop').onclick = function () { stop(); el.className = ''; };
    $('#nsform').onsubmit = function (e) { e.preventDefault(); search($('#nsq').value); };
    ['nsHot', 'nsHwy', 'nsToll'].forEach(function (id) {
      $('#' + id).onchange = function () {
        opts.hotspots = $('#nsHot').checked;
        opts.highways = $('#nsHwy').checked;
        opts.tolls = $('#nsToll').checked;
        if (dest) go(dest);        // re-route immediately: the toggle IS the ask
      };
    });
    return el;
  }
  function openSheet() { sheet().className = 'show'; setTimeout(function () { $('#nsq').focus(); }, 30); }

  function search(q) {
    q = (q || '').trim();
    if (q.length < 3) { H.toast('Type a place to search for'); return; }
    var box = $('#nsresults');
    box.innerHTML = '<div class="ns-empty">Searching…</div>';
    // /api/geocode takes the term and nothing else - no bias toward where the
    // car is, because that would mean sending the current position on every
    // keystroke of a search that has not been submitted yet.
    fetch('/api/geocode?q=' + encodeURIComponent(q))
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
          b.onclick = function () { $('#navsheet').className = ''; go({ lat: lat, lon: lon, label: name }); };
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
    banner();
  }

  function stop() {
    dest = null; trip = null; shape = []; manIdx = 0; lastIdx = -1;
    if (line) { H.map.removeLayer(line); line = null; }
    if (destMarker) { H.map.removeLayer(destMarker); destMarker = null; }
    var b = $('#navbar'); if (b) b.className = '';
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
      b.innerHTML = '<div class="nb-turn" id="nbTurn"></div>' +
                    '<div class="nb-sub"><span id="nbDist"></span><span id="nbEta"></span></div>';
      document.body.appendChild(b);
      b.onclick = openSheet;
    }
    var leg = trip.legs[0], m = leg.maneuvers[manIdx];
    if (!m) { b.className = ''; return; }
    $('#nbTurn').textContent = m.instruction || '';
    b.className = 'show';
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
      }).catch(function () {});
    setInterval(tick, 1000);
    paintLimit();
  }

  return { start: start, stop: stop, go: go };
})();
