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
import urllib.error
import urllib.request

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
            if (c.get("n") or 0) < HOT_MIN_N:
                continue
            lat, lon = float(c["lat"]), float(c["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        d = _point_to_segment_m((lat, lon), a, b)
        if d <= corridor_m:
            near.append((d, lat, lon))
    near.sort()
    out = []
    for _, lat, lon in near[:HOT_MAX_POLYS]:
        my, mx = _m_per_deg(lat)
        dlat, dlon = HOT_BOX_M / my, HOT_BOX_M / mx
        out.append([[lon - dlon, lat - dlat], [lon + dlon, lat - dlat],
                    [lon + dlon, lat + dlat], [lon - dlon, lat + dlat],
                    [lon - dlon, lat - dlat]])
    return out


def route(a, b, avoid_highways: bool = False, avoid_tolls: bool = False,
          hot_cells=None) -> dict:
    """A driving route from a=(lat,lon) to b=(lat,lon).

    Returns {"trip": <valhalla trip>, "avoided_hotspots": bool,
             "hotspot_fallback": bool}. `hotspot_fallback` is true when the
    hotspot-avoiding attempt had no route and this is the ordinary one -
    the page must tell the driver that, because they asked for something
    they are not getting.
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
    polys = hotspot_polygons(hot_cells, a, b) if hot_cells else []
    if polys:
        try:
            body_x = dict(body, exclude_polygons=polys)
            return {"trip": _post("/route", body_x)["trip"],
                    "avoided_hotspots": True, "hotspot_fallback": False}
        except Exception:
            # No route with the hot areas closed. Fall through and say so.
            pass
    out = _post("/route", body)
    return {"trip": out["trip"], "avoided_hotspots": False,
            "hotspot_fallback": bool(polys)}


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
            sl = edge.get("speed_limit")
            if sl and best is None:
                best = float(sl)
    # Valhalla reports speed limits in km/h. The exact key and unit are
    # verified against a live /locate response before this ships - see
    # tools/nav_probe.py, which prints what the engine actually returns rather
    # than what a docstring somewhere believes.
    return {"mph": int(round(best * 0.621371)) if best else None, "road": road}
