"""Turn-by-turn navigation, computed on our own machine.

    nav.route(a, b, avoid_highways=..., avoid_tolls=..., avoid_hotspots=...)
    nav.speed_limit(lat, lon)            # what the road is posted at, or None
    nav.available()                      # is the routing engine actually up

Valhalla on loopback. No Google, no Mapbox, no third party: a driver asking
this site how to get somewhere must not thereby tell a company where they are
going, which is the entire argument SparrowMap makes about number plates
applied to the one request that would otherwise leak the most.

🚨 NOTHING HERE IS WRITTEN DOWN. Not the origin, not the destination, not the
route, not a counter of how many people asked. The engine's own access log is
off (see deploy/valhalla.service) and this module keeps no state between
calls. A destination is the most revealing thing a person can hand a map -
where they are going, before they have gone - so the rule is stronger than the
one for plate searches, which at least concern a public record.

The claim is checkable rather than asserted: /api/policy publishes
`route_logging: false` and `routing_engine`, deploy/valhalla.service shows the
daemon started with logging disabled, and this file is the only caller.

WHAT "AVOID HOTSPOTS" HONESTLY MEANS. Valhalla is told to treat a small box
around each hot cell as closed road. That is avoidance of PLACES PATROLS HAVE
BEEN, which is history, not a live position - a car is not there because the
map says the area is hot, and the road may be perfectly clear. It also cannot
be absolute: if the only way out of a neighbourhood runs through a hot cell,
excluding it has no route at all, and a navigation app that answers "no route"
is worse than one that answers honestly. So a blocked route falls back to an
unblocked one and SAYS SO, rather than silently routing through the thing the
driver asked to avoid.
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
import threading
import urllib.error
import urllib.request
from pathlib import Path

#: The engine, on loopback. Never reachable from outside this machine.
VALHALLA = os.environ.get("VALHALLA_URL", "http://127.0.0.1:8002")
TIMEOUT_S = float(os.environ.get("VALHALLA_TIMEOUT_S", "20"))

#: A hot cell has to have been seen this many times before it is worth routing
#: around. One old sighting is not a zone - the same threshold the driving
#: page uses for its cone warning, kept in step deliberately so the map and
#: the route cannot disagree about what counts as hot.
HOT_MIN_N = 3
#: Half-width of the box closed around a hot cell, in metres. A city block.
#: Bigger detours further for weaker evidence; smaller and the router simply
#: drives past the corner of it.
HOT_BOX_M = 150.0
#: Valhalla walks every exclusion against every edge it considers, so the list
#: has to be bounded or a long route through a well-covered city gets slow.
#: The nearest cells to the straight line between the two points are the ones
#: that can actually be on the route.
HOT_MAX_POLYS = 60

#: 🚨 VALHALLA REFUSES THE WHOLE REQUEST IF THE EXCLUSION RINGS ADD UP TO MORE
#: THAN 10 km, AND THAT IS A TOTAL, NOT A PER-POLYGON LIMIT.
#:
#: Measured 2026-09-27, the first time the engine ever ran: asking to avoid
#: hotspots on a Columbus corridor built 29 boxes and came back
#:   {"error_code":167,"error":"Exceeded maximum circumference for
#:    exclude_polygons: 10000 meters"}
#: A single box routed fine. So the failure was not "no route exists with the
#: hot areas closed" - it was the request being rejected before any routing
#: happened, and route() caught the 400, fell back, and honestly reported
#: hotspot_fallback=true. The driver asked to avoid patrols and got a plain
#: route with a warning, every single time, on every corridor with more than
#: eight hot cells near it.
#:
#: Each box is 2*HOT_BOX_M a side, so it spends 8*HOT_BOX_M of ring. At the
#: default 150 m that is 1,200 m per hotspot and about seven of them fit.
#: 9,000 leaves a margin under the engine's 10,000 so a rounding difference
#: cannot put us back over the edge.
#:
#: ⚠️ RAISING THIS MEANS RAISING service_limits.max_exclude_polygons_length in
#: valhalla.json to match, and every extra polygon costs routing time on a box
#: that is also running the map.
EXCLUDE_BUDGET_M = 9_000.0

#: Where the ALPR-camera snapshot lives. A separate sqlite file, not sparrow.db
#: - see tools/load_alpr.py for why. Absent until a dump is loaded, and
#: alpr_polygons treats "no file" the same as "no cameras near": nothing to
#: route around.
ALPR_DB = Path(__file__).resolve().parent / "data" / "alpr.db"

#: Half-width of the box closed around an ALPR camera, in metres. Smaller than
#: a hotspot's: a camera watches one spot on one road, so a tight box forces
#: the router off that segment without detouring a whole block for it - and a
#: small box spends less of the 10 km exclusion budget, so more cameras can be
#: avoided on the same route.
ALPR_BOX_M = 55.0

#: How far from the straight line between origin and destination an ALPR camera
#: can be and still plausibly sit on the route. Only the nearest ALPR_MAX of
#: these are kept, and then only as many as the budget allows, so this is a
#: pre-filter for the bounding-box query rather than a promise.
ALPR_CORRIDOR_M = 4_000.0
ALPR_MAX = 80

_ALPR_LOCK = threading.Lock()
_alpr_conn = None


def _alpr():
    """A cached read-only handle to the camera snapshot, or None if unloaded.

    Opened read-only and shared across the hub's threads: this file is only
    ever written by tools/load_alpr.py, which builds a new database and swaps
    it into place, so a reader never sees a half-written one.
    """
    global _alpr_conn
    with _ALPR_LOCK:
        if _alpr_conn is None:
            if not ALPR_DB.exists():
                return None
            _alpr_conn = sqlite3.connect(
                f"file:{ALPR_DB}?mode=ro", uri=True, check_same_thread=False)
        return _alpr_conn


def alpr_available() -> bool:
    """Is a camera snapshot loaded? So the page can offer the toggle only when
    there is data behind it, rather than a switch that silently does nothing."""
    return _alpr() is not None


def alpr_cameras_near(a, b, corridor_m: float = ALPR_CORRIDOR_M) -> list:
    """(distance_to_route, lat, lon) for ALPR cameras near the corridor a->b.

    A bounding-box range scan on the indexed snapshot, then the same
    point-to-segment test the hotspots use, nearest first. Empty when no
    snapshot is loaded - avoiding a camera we do not know about is not
    something to claim.
    """
    conn = _alpr()
    if conn is None:
        return []
    la0, la1 = sorted((a[0], b[0]))
    lo0, lo1 = sorted((a[1], b[1]))
    pad = corridor_m / 111_320.0            # generous; the segment test trims it
    try:
        rows = conn.execute(
            "SELECT lat, lon FROM cams WHERE lat BETWEEN ? AND ? "
            "AND lon BETWEEN ? AND ?",
            (la0 - pad, la1 + pad, lo0 - pad, lo1 + pad)).fetchall()
    except sqlite3.Error:
        return []
    out = []
    for lat, lon in rows:
        d = _point_to_segment_m((lat, lon), a, b)
        if d <= corridor_m:
            out.append((d, lat, lon))
    out.sort()
    return out


def alpr_polygons(a, b) -> list:
    """Small closed boxes around the ALPR cameras nearest the route.

    Nearest first, because an ALPR sits on a specific road and the ones closest
    to the straight line are the ones a route is most likely to pass. Budgeted
    the same way hotspots are; route() enforces the combined ceiling.
    """
    out = []
    for _, lat, lon in alpr_cameras_near(a, b)[:ALPR_MAX]:
        my, mx = _m_per_deg(lat)
        dlat, dlon = ALPR_BOX_M / my, ALPR_BOX_M / mx
        out.append([[lon - dlon, lat - dlat], [lon + dlon, lat - dlat],
                    [lon + dlon, lat + dlat], [lon - dlon, lat + dlat],
                    [lon - dlon, lat - dlat]])
    return out


def _ring_contains(ring, pt) -> bool:
    """Is (lat, lon) inside this axis-aligned [lon,lat] box?

    🚨 A CAMERA AT YOUR ORIGIN OR DESTINATION BREAKS THE WHOLE REQUEST.
    Valhalla answers error 442 "No path could be found" when a start or end
    point sits inside an exclude polygon - and one wall-in box discards every
    other exclusion with it, so a single camera next to where the driver is
    standing would silently cancel all avoidance. You cannot avoid a camera you
    are already beside, so such a box is dropped before the route is asked for.
    """
    lat, lon = pt
    xs = [c[0] for c in ring]
    ys = [c[1] for c in ring]
    return min(xs) <= lon <= max(xs) and min(ys) <= lat <= max(ys)


def _ring_perimeter_m(ring) -> float:
    """Length of a [lon,lat] ring in metres, for budgeting against the engine's
    exclude_polygons circumference cap."""
    total = 0.0
    for (lo1, la1), (lo2, la2) in zip(ring, ring[1:]):
        my, mx = _m_per_deg((la1 + la2) / 2.0)
        total += math.hypot((la2 - la1) * my, (lo2 - lo1) * mx)
    return total




def _post(path: str, body: dict) -> dict:
    req = urllib.request.Request(
        f"{VALHALLA}{path}", data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
        return json.loads(r.read() or b"{}")


def available() -> bool:
    """Is the engine up? Asked so the page can say "navigation is offline"
    instead of showing a driver a dead Go button."""
    try:
        _post("/status", {})
        return True
    except Exception:
        return False


def _m_per_deg(lat: float) -> tuple[float, float]:
    return 111_320.0, 111_320.0 * max(0.05, math.cos(math.radians(lat)))


def _point_to_segment_m(p, a, b) -> float:
    """Distance from a cell to the straight line between origin and
    destination - used only to choose WHICH exclusions are worth sending."""
    my, mx = _m_per_deg(p[0])
    px, py = (p[1] - a[1]) * mx, (p[0] - a[0]) * my
    bx, by = (b[1] - a[1]) * mx, (b[0] - a[0]) * my
    L = bx * bx + by * by
    t = 0.0 if L == 0 else max(0.0, min(1.0, (px * bx + py * by) / L))
    dx, dy = px - bx * t, py - by * t
    return math.hypot(dx, dy)


def hotspot_polygons(cells, a, b, corridor_m: float = 25_000.0) -> list:
    """Closed boxes around the hot cells near the corridor between a and b.

    Valhalla wants rings of [lon, lat] - the opposite order to everything else
    in this codebase, which is a cheap mistake to make and an expensive one to
    debug, because a transposed ring is a valid polygon in the sea and simply
    excludes nothing.
    """
    near = []
    for c in cells or []:
        try:
            n = c.get("n") or 0
            if n < HOT_MIN_N:
                continue
            lat, lon = float(c["lat"]), float(c["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        d = _point_to_segment_m((lat, lon), a, b)
        if d <= corridor_m:
            near.append((d, lat, lon, n))
    # 🚨 SPEND THE BUDGET ON THE WORST HOTSPOTS, NOT THE FIRST ONES.
    # Only a handful of boxes fit (see EXCLUDE_BUDGET_M), so ordering by
    # distance alone would hand the whole allowance to whichever cells happen
    # to sit nearest the line and drop a corridor's busiest one. Hottest first,
    # nearest as the tie-break.
    near.sort(key=lambda t: (-t[3], t[0]))
    out, used = [], 0.0
    # A box is 2*HOT_BOX_M on each side, so its ring is 8*HOT_BOX_M long.
    per = 8.0 * HOT_BOX_M
    for _, lat, lon, _n in near[:HOT_MAX_POLYS]:
        if used + per > EXCLUDE_BUDGET_M:
            break
        used += per
        my, mx = _m_per_deg(lat)
        dlat, dlon = HOT_BOX_M / my, HOT_BOX_M / mx
        out.append([[lon - dlon, lat - dlat], [lon + dlon, lat - dlat],
                    [lon + dlon, lat + dlat], [lon - dlon, lat + dlat],
                    [lon - dlon, lat - dlat]])
    return out


def route(a, b, avoid_highways: bool = False, avoid_tolls: bool = False,
          hot_cells=None, avoid_alpr: bool = False) -> dict:
    """A driving route from a=(lat,lon) to b=(lat,lon).

    Returns the trip plus, for each avoidance the driver asked for, whether it
    was applied or fell back:
        avoided_hotspots / hotspot_fallback
        avoided_alpr     / alpr_fallback
    A `*_fallback` is true when that avoidance was requested, boxes were built
    for it, and the engine could not route with them closed - so this is the
    ordinary route and the page must say the driver is not getting what they
    asked for.

    🚨 HOTSPOTS AND ALPR CAMERAS SHARE ONE BUDGET, because Valhalla caps the
    TOTAL exclusion circumference (see EXCLUDE_BUDGET_M), not each polygon. With
    both toggles on in a dense city the budget runs out, and then a ring that
    did not fit is simply not sent - the toggle still did as much as the engine
    allows, and whichever kind got no ring at all is reported as a fallback so
    the claim on the page stays true.
    """
    auto = {
        # Valhalla reads these as PREFERENCES from 0 to 1, not switches. 0 is
        # "avoid as hard as this costing model can", which is what the toggle
        # means to a driver, and it stops short of impossible - a motorway-only
        # stretch still routes rather than failing.
        "use_highways": 0.0 if avoid_highways else 0.5,
        "use_tolls": 0.0 if avoid_tolls else 0.5,
    }
    body = {
        "locations": [{"lat": a[0], "lon": a[1]}, {"lat": b[0], "lon": b[1]}],
        "costing": "auto",
        "costing_options": {"auto": auto},
        "directions_options": {"units": "miles"},
        # The driver is on a phone in a car: the narrative is what gets spoken.
        "directions_type": "instructions",
    }
    hp = hotspot_polygons(hot_cells, a, b) if hot_cells else []
    ap = alpr_polygons(a, b) if avoid_alpr else []

    # Fit both kinds under the one circumference cap. Hotspots first (a corridor
    # rarely has many, and confirmed patrol history is the stronger signal),
    # then ALPR cameras fill whatever budget is left. `continue`, not `break`,
    # so a small camera box can still fit after a big hotspot box that did not.
    polys, used, kept_hp, kept_ap = [], 0.0, 0, 0
    for ring in hp:
        if _ring_contains(ring, a) or _ring_contains(ring, b):
            continue
        per = _ring_perimeter_m(ring)
        if used + per <= EXCLUDE_BUDGET_M:
            used += per
            polys.append(ring)
            kept_hp += 1
    for ring in ap:
        if _ring_contains(ring, a) or _ring_contains(ring, b):
            continue
        per = _ring_perimeter_m(ring)
        if used + per <= EXCLUDE_BUDGET_M:
            used += per
            polys.append(ring)
            kept_ap += 1

    if polys:
        try:
            trip = _post("/route", dict(body, exclude_polygons=polys))["trip"]
            return {"trip": trip,
                    "avoided_hotspots": kept_hp > 0,
                    "hotspot_fallback": bool(hp) and kept_hp == 0,
                    "avoided_alpr": kept_ap > 0,
                    "alpr_fallback": bool(ap) and kept_ap == 0}
        except Exception:
            # No route with those areas closed. Fall through and say so for
            # everything that was actually attempted.
            pass
    out = _post("/route", body)
    return {"trip": out["trip"],
            "avoided_hotspots": False, "hotspot_fallback": bool(hp),
            "avoided_alpr": False, "alpr_fallback": bool(ap)}


def speed_limit(lat: float, lon: float) -> dict:
    """What the road under a point is POSTED at, in mph, or None.

    🚨 None IS AN ANSWER AND MUST REACH THE SCREEN AS ONE. OpenStreetMap has a
    maxspeed tag on a minority of American roads, so most of the time the
    honest reply is "not known here" - and a navigation display that fills
    that gap with a guess, a default, or the road class's typical speed is
    inventing a number a driver may be fined for trusting. The caller shows a
    dash. See the same rule in privacy.py: a missing value is not a zero.
    """
    body = {"locations": [{"lat": lat, "lon": lon}], "costing": "auto",
            "verbose": True}
    try:
        res = _post("/locate", body)
    except Exception:
        return {"mph": None, "road": None}
    best, road = None, None
    for loc in res or []:
        for e in (loc.get("edges") or []):
            info = e.get("edge_info") or {}
            edge = e.get("edge") or {}
            names = info.get("names") or []
            if road is None and names:
                road = names[0]
            # 🚨 THE KEY IS edge_info.speed_limit, NOT edge.speed_limit.
            # Written from a remembered key name, this read `edge` and returned
            # None for every road on earth - which looks exactly like "OSM has
            # no maxspeed here", the answer that is genuinely correct most of
            # the time. A wrong parser hiding behind a plausible empty result
            # is the failure mode this file was warned about.
            #
            # Measured 2026-09-27 against the live engine, the first time it
            # had ever run: /locate at Times Square returns
            # edge_info.speed_limit = 40 with names ["7th Avenue"], while
            # edge.speed_limit is absent. Before this fix /api/speedlimit
            # answered {"mph": null, "road": "7th Avenue"} - it had found the
            # road and thrown the number away.
            sl = info.get("speed_limit") or edge.get("speed_limit")
            if sl and best is None:
                best = float(sl)
    # ✅ THE UNIT IS km/h, AND THAT IS NOW MEASURED RATHER THAN ASSUMED.
    # Times Square returns 40 where the posted limit is 25 mph (40.2 km/h), and
    # a downtown Los Angeles street returns 40 where California's urban default
    # is 25 mph. Both land on the same conversion, and 40 would be a nonsense
    # POSTED value for either road, so mph is ruled out.
    #   40 km/h * 0.621371 = 24.9 -> 25 mph. Correct.
    return {"mph": int(round(best * 0.621371)) if best else None, "road": road}
