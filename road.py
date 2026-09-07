"""RavenMap - putting a sighting on the ROAD without putting the camera on the map.

🚨 THE PROBLEM THIS SOLVES
Node positions are published jittered by up to 60 m so the map can show where
people are recorded without publishing which house volunteered. Sightings were
then derived from that jittered point - pushed out along the heading by 0.6 x
reach - which sounds reasonable until you compare the two numbers. His node has
a reach of 28 m and a jitter budget of 60 m. **The privacy noise is more than
twice the entire depth of the cone.** So the cone, and every dot inside it,
lands wherever the jitter threw it: a back garden, the middle of a field, the
wrong side of the block. The dots were never on the road.

Raising the accuracy by shrinking the jitter is the wrong trade. The right one
comes from noticing that the two things being protected are not the same thing:

    the ROAD a camera watches is public.  People are entitled to know where
    they are recorded, and that is the whole civic argument for this project.

    the HOUSE the camera sits in is not.  A volunteer should not have to accept
    a target on their door to take part.

So we publish them separately and at different resolutions. Each node gets a
WATCHED SPAN: the stretch of real road its lens actually covers, computed from
the camera's TRUE position and snapped to the road centreline. That span is
published accurately, because it is public information. The camera's own point
stays jittered. A sighting is placed somewhere along the span - which is an
honest statement of what the system actually knows, since a camera watching
30 m of street cannot tell you which metre of it a car occupied.

## Why the span has a MINIMUM length

Publishing a 28 m span next to a published heading and reach would hand an
attacker a back-projection: perpendicular from the span midpoint, 28 m out,
lands on one or two houses. That is the recurring failure in this codebase -
a control applied to one representation and given away by another. Two
countermeasures, both here:

  * the published span is extended to at least ``SPAN_MIN_M``, centred on the
    real watched stretch, so its midpoint no longer localises the camera; and
  * ``hub`` serves the span INSTEAD of a precise heading and reach for nodes
    that have one, so there is nothing left to back-project with.

## Why the Overpass query is deliberately coarse

Asking a public API "what road is at this exact point" tells that API the
exact location of a volunteer's camera - which would be a strange thing for
this project to do to its own contributors. The bbox is therefore snapped to a
~400 m grid before it is sent, and all the geometry happens locally against the
returned tile. Overpass learns a neighbourhood, not a house. The result is
cached on the node record, so the query happens once at enrolment and never
again.

Everything degrades gracefully: no network, no road found, Overpass down - the
span falls back to the camera's own aim axis, which is still on the road for
any node mounted per the project's own aiming rule (aim DOWN the street).
"""

from __future__ import annotations

import json
import http.client
import math
import threading
import time
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

from core import DATA, angle_diff, bearing_deg, haversine_m

# The published span is never shorter than this. See the module docstring:
# it is what stops the span's midpoint from localising the camera. Chosen to
# match the existing 60 m node jitter budget rather than exceed it - the two
# uncertainties compound, and a volunteer should not have to reason about two
# different privacy radii.
SPAN_MIN_M = 80.0

# Roads people drive on. A camera pointed at a footpath is not doing ALPR, and
# snapping a sighting onto a cycleway would be worse than not snapping it.
DRIVEABLE = {
    "motorway", "motorway_link", "trunk", "trunk_link", "primary",
    "primary_link", "secondary", "secondary_link", "tertiary", "tertiary_link",
    "unclassified", "residential", "living_street", "service", "road",
}

# 🚨 ONE ENDPOINT MEANT ONE POINT OF FAILURE, AND IT FAILED SILENTLY.
#
# The box cannot reach overpass-api.de at all: it resolves IPv6-only from there
# and that route is refused, with no usable A record to fall back to. General
# outbound is fine (github answers 200 over IPv4), so nothing looked broken -
# except that EVERY road lookup raised URLError, snap() swallowed it and
# returned None, and /aim told the owner "no road fell inside the view" over a
# map visibly full of roads. The camera could not be aimed at all, and the
# message blamed their distance setting.
#
# So: mirrors, tried in order, with a cooldown on whichever one just failed so a
# dead endpoint is not re-dialled on every lookup. overpass-api.de stays first
# because it is the canonical instance and works from most machines - the home
# hub reaches it fine - it is simply not reachable from this box today.
# ⚠️ MEASURED FROM THE BOX 2026-08-19, not assumed. Every public instance I
# could find was tried with a real highway query from this machine:
#
#   overpass-api.de          TCP :443 CONNECTION REFUSED on v4 and v6.
#                            ICMP to it succeeds at 25 ms, and github,
#                            openstreetmap.org and cloudflare all return 200 -
#                            so egress is fine and this is them refusing us,
#                            almost certainly the Hetzner range.
#   kumi.systems             accepts TCP, completes a TLS 1.3 handshake, then
#   private.coffee           never answers. Same host, 193.219.97.30.
#   osm.ch                   answers correctly but is SWITZERLAND ONLY - valid
#                            JSON, zero elements for Michigan.
#   monicz.dev               answered once, then 0 ways in 40 s. Rate limited.
#   fossgis / osm.se / osm.jp / vlaanderen / pgi.gov.pl / openstreetmap.fr
#   / nchc.org.tw / geocoding.ai / lz4 / z    all failed outright.
#
# 🚨 THE ONLY INSTANCE THAT ANSWERS THIS BOX IS maps.mail.ru, AND THAT IS A
# DELIBERATE TRADE-OFF, NOT AN OVERSIGHT.
#
# What it learns is a GRID-SNAPPED bbox (see GRID_DEG, ~400 m) asking for
# public road geometry - never a camera's true position, which is why that
# snapping exists. What it can still infer, over time, is roughly which areas
# this project is interested in. That is a real disclosure to a large Russian
# service, and it is listed LAST so it is only reached when nothing else will
# answer.
#
# ➡️ To refuse that trade entirely, delete the last line. Road snapping then
# degrades to the documented fallback - the jittered point is used and no
# claim is made about which street - which is safe, just less precise.
OVERPASS_MIRRORS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass.monicz.dev/api/interpreter",
)
# 🚨 maps.mail.ru WAS HERE AND WAS REMOVED ON PURPOSE - DO NOT ADD IT BACK.
#
# It was the only instance that answered the public box, so for a few hours it
# was the only thing making road snapping work there. It is still the only one.
# It is out anyway, because the price was telling a large Russian service which
# ~400 m squares a project that tracks police vehicles keeps asking about, and
# this codebase gives viewers a per-day rolling alias and hashes their IP
# addresses specifically to prevent that class of inference. Buying snapping
# with it would contradict the thing the rest of the file is built to protect.
#
# ➡️ The replacement is tools/road_fill.py: his DESKTOP reaches
# overpass-api.de fine (measured), so it resolves the cells the box needs and
# copies them into the box cache. Same data, same snapping, nothing new
# disclosed - only the machine asking changes.
# Kept as a name because other code and tests refer to it; it is the primary.
OVERPASS = OVERPASS_MIRRORS[0]

# endpoint -> the time it may be tried again. A mirror that refused a connection
# is skipped for this long rather than costing every later lookup its timeout.
_MIRROR_COOLDOWN_S = 600.0
_MIRROR_DOWN: dict = {}

# 🚨 WHICHEVER MIRROR LAST ANSWERED IS TRIED FIRST NEXT TIME.
#
# A fixed order cannot be right on both machines: from the home hub
# overpass-api.de answers, and from the public box it refuses outright, so
# whichever order is hardcoded is wrong somewhere. Measured cost of getting it
# wrong: a lookup spent its entire budget on two failing endpoints and never
# reached the one that works.
#
# Remembering the last success makes the steady state optimal on ANY machine
# without hardcoding a preference, and it costs one variable. The cooldown
# still handles the case where the remembered one dies.
_LAST_GOOD: dict = {}

# Grid the bbox onto ~400 m so the request does not carry the camera's precise
# position. 0.004 deg of latitude is ~445 m; longitude is scaled at query time.
GRID_DEG = 0.004


# --------------------------------------------------------------------------
# Local planar helpers
#
# Over a few hundred metres a flat approximation centred on the camera is
# accurate to well under a metre and makes the projection maths readable.
# Longitude is scaled by cos(lat) - forgetting that skews everything by 26% at
# Michigan's latitude, which is the sort of quiet error this project keeps
# finding in itself.
# --------------------------------------------------------------------------

def _to_xy(lat: float, lon: float, lat0: float, lon0: float) -> tuple[float, float]:
    return ((lon - lon0) * 111_320.0 * math.cos(math.radians(lat0)),
            (lat - lat0) * 111_320.0)


def _to_ll(x: float, y: float, lat0: float, lon0: float) -> tuple[float, float]:
    return (lat0 + y / 111_320.0,
            lon0 + x / (111_320.0 * max(math.cos(math.radians(lat0)), 1e-6)))


def _seg_points(a: tuple, b: tuple, step: float = 2.0) -> list[tuple[float, float]]:
    """Sample a segment every `step` metres, endpoints included."""
    d = math.hypot(b[0] - a[0], b[1] - a[1])
    n = max(1, int(d / step))
    return [(a[0] + (b[0] - a[0]) * i / n, a[1] + (b[1] - a[1]) * i / n)
            for i in range(n + 1)]


# --------------------------------------------------------------------------
# Overpass
# --------------------------------------------------------------------------

class OverpassBusy(RuntimeError):
    """Overpass answered, but said it could not do the work.

    Distinct from "no roads here" on purpose: the two are byte-identical in the
    response body apart from a `remark`, and confusing them poisons the cache
    with an empty answer that never expires.
    """


# 🚨 NO urlopen INSIDE AN ADMISSION PERMIT WITHOUT A BOUND.
# The tile proxy learned this the hard way (hub._TILE_FETCH): an unbounded
# upstream call reached from a request handler turns somebody else's slow API
# into this box's outage. Overpass is reached from /api/enroll, /api/sightings
# and /api/drive/report, each holding a permit for up to 25 seconds.
#
# Overpass also rate-limits hard - a sweep of 18 nodes earned a 429 in under two
# minutes - so a small number is right here. Callers that cannot get a slot
# behave exactly as they do when Overpass is down, which is a path that already
# had to work.
_OVERPASS_SLOTS = threading.Semaphore(3)

# Cells Overpass has recently refused, and until when.
_FAIL_UNTIL: dict = {}
_FAIL_TTL_S = 120.0
# 🚨 AND HOW MANY TIMES IN A ROW, SO A PERMANENTLY DEAD CELL BACKS OFF.
# A flat 120 s is right for a blip and wrong for an outage: with Overpass
# unreachable for days, every cell was re-asked every two minutes forever, and
# each attempt costs a bounded slot and a full timeout. Doubling per consecutive
# failure turns that into a handful of attempts an hour, and any success resets
# it, so recovery is still automatic and immediate.
_FAIL_STREAK: dict = {}
_FAIL_TTL_MAX_S = 3600.0


def _fetch_ways_bounded(lat: float, lon: float):
    """fetch_ways, but never more than _OVERPASS_SLOTS at once."""
    if not _OVERPASS_SLOTS.acquire(timeout=1.0):
        raise OverpassBusy("too many road lookups already in flight")
    try:
        # Shorter than the 25s default: this runs inside a request, and a
        # visitor waiting 25 seconds for a road lookup has already been failed.
        #
        # ⚠️ RAISED 8s -> 14s ON MEASUREMENT. The only mirror that answers this
        # box returns in about 12 s, so an 8 s bound guaranteed failure however
        # healthy the endpoint was - the timeout itself was making the feature
        # impossible. 14 s is still well inside the caller's tolerance, and the
        # circuit breaker above means a dead endpoint is now refused instantly
        # rather than costing this timeout on every lookup.
        return fetch_ways(lat, lon, timeout=14.0)
    finally:
        _OVERPASS_SLOTS.release()


def fetch_ways(lat: float, lon: float, timeout: float = 25.0) -> list[list[tuple]]:
    """Driveable road geometries near a point, as lists of (lat, lon).

    The bbox is snapped to a grid before it leaves this machine. See the module
    docstring - the camera's exact position is not Overpass's business.
    """
    dlat = GRID_DEG
    dlon = GRID_DEG / max(math.cos(math.radians(lat)), 1e-6)
    s = math.floor(lat / dlat) * dlat
    w = math.floor(lon / dlon) * dlon
    bbox = f"{s - dlat:.6f},{w - dlon:.6f},{s + 2 * dlat:.6f},{w + 2 * dlon:.6f}"

    q = f'[out:json][timeout:20];way["highway"]({bbox});out geom;'
    data = urllib.parse.urlencode({"data": q}).encode()

    # Try each mirror that is not in cooldown. A connection-level failure moves
    # to the next; the LAST failure is re-raised so the caller's except path and
    # the negative cache still see a real error rather than an empty result -
    # "no roads here" and "could not ask" must never look the same, which is the
    # lesson the remark check below already encodes.
    now = time.time()
    live = [m for m in OVERPASS_MIRRORS if _MIRROR_DOWN.get(m, 0) <= now]
    # Try the one that worked last time first; it is by far the most likely to
    # work again, and every second spent on a dead endpoint is a second the
    # working one does not get.
    good = _LAST_GOOD.get("url")
    if good in live:
        live = [good] + [m for m in live if m != good]
    if not live:
        # 🚨 THIS USED TO READ `live = list(OVERPASS_MIRRORS)` - "all cooling
        # down: try them all anyway" - WHICH DEFEATED THE COOLDOWN IN THE ONE
        # CASE IT EXISTS FOR.
        #
        # A per-mirror cooldown is meant to stop a dead endpoint costing every
        # later lookup its timeout. Falling back to the full list when they are
        # ALL cooling down means a total outage is the one situation where the
        # protection does nothing. Measured 2026-08-19 with every mirror down:
        # the hub logged a retry every 8 seconds, indefinitely, each one taking
        # a road-lookup slot and burning its full timeout.
        #
        # Fail immediately instead. The caller already handles "could not ask"
        # and the negative cache below already remembers it, so nothing is lost
        # except the wasted work.
        raise urllib.error.URLError(
            "all overpass mirrors are cooling down after recent failures")
    # 🚨 BOUND THE WHOLE ATTEMPT, NOT EACH MIRROR.
    # `timeout` is per-request, so with five mirrors a single lookup could hold
    # a road slot for five times that before giving up - which is exactly the
    # "bound on the wrong noun" shape that has caused every outage here so far.
    # The caller asked for `timeout`; that is the budget for the ANSWER, not
    # per endpoint dialled. Whatever is left is what the next mirror gets, and
    # a mirror is skipped entirely once there is not enough left to be worth
    # dialling.
    deadline = time.time() + max(timeout, 1.0)
    doc = None
    last = None
    for url in live:
        remaining = deadline - time.time()
        if remaining < 1.5:
            last = last or urllib.error.URLError(
                "road lookup budget spent before a mirror answered")
            break
        req = urllib.request.Request(
            url, data=data,
            headers={"User-Agent": "RavenMap/0.1 (citizen ALPR; road snapping)"})
        try:
            with urllib.request.urlopen(req, timeout=remaining) as r:
                doc = json.loads(r.read().decode("utf-8"))
            _MIRROR_DOWN.pop(url, None)
            _LAST_GOOD["url"] = url
            break
        except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
            last = exc
            _MIRROR_DOWN[url] = time.time() + _MIRROR_COOLDOWN_S
            print(f"[road] overpass mirror unreachable ({url}): "
                  f"{exc.__class__.__name__}: {exc}")
    if doc is None:
        raise last or urllib.error.URLError("no overpass mirror answered")

    # 🚨 OVERPASS SIGNALS OVERLOAD AS HTTP 200 WITH AN EMPTY RESULT.
    # The body comes back {"elements": [], "remark": "runtime error: Query timed
    # out..."} - a successful-looking response meaning "ask again later". Read
    # as data it says "there are no roads here", which then gets CACHED, and
    # because sighting positions are frozen at ingest the dots placed during
    # that window never self-heal. That is the "one dot in a park with dozens of
    # passes behind it". A remark is an error; raise so the caller's except path
    # and the negative cache handle it as one.
    remark = doc.get("remark")
    if remark and not doc.get("elements"):
        raise OverpassBusy(str(remark)[:160])

    out: list = []
    fallback: list = []          # service roads and alleys, used only if out is empty
    for el in doc.get("elements", []):
        if el.get("type") != "way":
            continue
        tags = el.get("tags", {})
        if tags.get("highway") not in DRIVEABLE:
            continue
        # 🚨 A DRIVEWAY IS NOT A STREET, AND IT IS USUALLY THE NEAREST THING.
        # `service` covers driveways and parking aisles as well as alleys, and
        # a camera in a window is nearly always closer to its own driveway than
        # to the road it is pointed at. span_nearest takes the CLOSEST driveable
        # way, so the watched span was being drawn down a driveway - which the
        # basemap barely renders, so the span and its dots looked like they were
        # floating in the gap between the streets. Reported as "roads aren't
        # lining up"; the geometry was exact and the road was the wrong one.
        #
        # The sighting is a claim that a vehicle passed on a public road. A
        # private driveway or a supermarket parking aisle cannot carry that
        # claim, so they are excluded from the candidate set entirely rather
        # than merely ranked lower.
        if tags.get("service") in ("driveway", "parking_aisle", "drive-through"):
            continue
        geom = [(g["lat"], g["lon"]) for g in el.get("geometry") or []]
        if len(geom) < 2:
            continue
        entry = (tags.get("name") or "", geom)
        # An untagged `service` way is an alley or an access road. It is a real
        # place a vehicle can drive, so it is not discarded - but it should
        # never win against an actual street merely by being a few metres
        # nearer, which is exactly what "closest driveable way" would do to a
        # camera looking across its own back lane. Held back and used only if
        # nothing better exists in the tile.
        (fallback if tags.get("highway") == "service" else out).append(entry)
    return out or fallback


# --------------------------------------------------------------------------
# The span
# --------------------------------------------------------------------------

def span_from_ways(lat: float, lon: float, heading: float, fov: float,
                   reach_m: float, ways: list) -> Optional[dict]:
    """Which stretch of which road does this camera actually cover?

    Walks every candidate road at 2 m resolution and keeps the points that are
    both inside the cone and within reach. The winning road is the one with the
    most such points, not the nearest one: a camera aimed down a street will
    accumulate a long run of hits on that street and at most a glancing one or
    two on the cross street behind it. Nearest-point matching gets that
    backwards at exactly the junctions where it matters.
    """
    best = None
    for name, geom in ways:
        hits: list[tuple[float, float]] = []
        for i in range(len(geom) - 1):
            a = _to_xy(geom[i][0], geom[i][1], lat, lon)
            b = _to_xy(geom[i + 1][0], geom[i + 1][1], lat, lon)
            for px, py in _seg_points(a, b):
                d = math.hypot(px, py)
                if d > reach_m * 1.25 or d < 1.0:
                    continue
                # atan2(east, north) -> compass bearing.
                brg = (math.degrees(math.atan2(px, py)) + 360.0) % 360.0
                if angle_diff(brg, heading) <= fov / 2.0:
                    hits.append((px, py))
        if hits and (best is None or len(hits) > len(best[1])):
            best = (name, hits)

    if best is None:
        return None

    name, hits = best
    # The span is the extent of the hits along the road's own direction, which
    # is the principal axis of the hit cloud. Taking min/max on x and y
    # separately would give a bounding box, not a line.
    cx = sum(h[0] for h in hits) / len(hits)
    cy = sum(h[1] for h in hits) / len(hits)
    sxx = sum((h[0] - cx) ** 2 for h in hits)
    syy = sum((h[1] - cy) ** 2 for h in hits)
    sxy = sum((h[0] - cx) * (h[1] - cy) for h in hits)
    theta = 0.5 * math.atan2(2 * sxy, sxx - syy)
    ux, uy = math.cos(theta), math.sin(theta)

    ts = [(h[0] - cx) * ux + (h[1] - cy) * uy for h in hits]
    t0, t1 = min(ts), max(ts)

    # Extend to the published minimum, symmetrically about the real midpoint.
    length = t1 - t0
    if length < SPAN_MIN_M:
        pad = (SPAN_MIN_M - length) / 2.0
        t0, t1 = t0 - pad, t1 + pad

    p0 = _to_ll(cx + ux * t0, cy + uy * t0, lat, lon)
    p1 = _to_ll(cx + ux * t1, cy + uy * t1, lat, lon)
    return {"road_name": name, "span": [list(p0), list(p1)],
            "watched_m": round(length, 1), "source": "osm"}


def span_on_named_road(lat: float, lon: float, reach_m: float,
                       name_match: str) -> Optional[dict]:
    """The stretch of a NAMED road nearest this camera. The operator names it.

    🚨 THE CONE IS A HEURISTIC AND AT A JUNCTION IT LOSES TO A PERSON.
    span_from_ways picks whichever road collects the most points inside the
    camera's cone, which is the right guess when nothing better is known. Near a
    junction it is a guess against a person who can see the street: three nodes
    16-91 m apart all snapped to a cross street, and every police vehicle the
    operator has ever seen was on the other one. A camera 16 m from another
    camera was placed on a different road from it.

    This does not fabricate anything - it still requires a real OSM way with
    that name within reach, and returns None when there is none, so the rule
    that an unknown road publishes NO span is intact (see resolve()). What it
    drops is only the assumption that the cone knows best.

    ⚠️ Heading is deliberately ignored. A camera can be aimed across a street
    it watches, or report no heading at all from a browser enrolment, and the
    whole reason this exists is that the aim-based pick was wrong.
    """
    want = (name_match or "").strip().lower()
    if not want:
        return None
    # ⚠️ Overpass rate-limits a sweep hard - 18 nodes earned a 429 in under two
    # minutes - and it did exactly that mid-run here, crashing the tool on the
    # third node after two had already been written. resolve() has always caught
    # this; a second entry point into the same API must too, or a partial sweep
    # leaves half the nodes moved and no clear record of which.
    try:
        # Bounded: see _fetch_ways_bounded. Both of these are reached from a
        # request handler holding an admission permit.
        ways = _fetch_ways_bounded(lat, lon)
    except (urllib.error.URLError, OSError, ValueError, KeyError,
            http.client.HTTPException, OverpassBusy) as exc:
        print(f"[road] named-road lookup unavailable "
              f"({exc.__class__.__name__}: {exc}) - no span; retry later")
        return None
    pts: list[tuple[float, float]] = []
    matched_name = None
    for nm, geom in ways:
        if not nm or want not in nm.lower():
            continue
        for i in range(len(geom) - 1):
            a = _to_xy(geom[i][0], geom[i][1], lat, lon)
            b = _to_xy(geom[i + 1][0], geom[i + 1][1], lat, lon)
            for px, py in _seg_points(a, b):
                # Generous compared with the cone: the road is known, so the
                # only question is which STRETCH of it this camera covers.
                if math.hypot(px, py) <= max(reach_m * 2.0, SPAN_MIN_M):
                    pts.append((px, py))
                    matched_name = matched_name or nm
    if not pts:
        return None

    # Same principal-axis extent as span_from_ways, so a span produced here is
    # indistinguishable in shape from one the cone produced - only the choice
    # of road differs.
    cx = sum(p[0] for p in pts) / len(pts)
    cy = sum(p[1] for p in pts) / len(pts)
    sxx = sum((p[0] - cx) ** 2 for p in pts)
    syy = sum((p[1] - cy) ** 2 for p in pts)
    sxy = sum((p[0] - cx) * (p[1] - cy) for p in pts)
    theta = 0.5 * math.atan2(2 * sxy, sxx - syy)
    ux, uy = math.cos(theta), math.sin(theta)
    ts = [(p[0] - cx) * ux + (p[1] - cy) * uy for p in pts]
    t0, t1 = min(ts), max(ts)

    # Keep the published span near the camera's actual coverage rather than the
    # whole street: trim to reach either side of the closest point, then apply
    # the usual minimum so the midpoint still cannot be back-projected.
    near = min(range(len(pts)), key=lambda i: math.hypot(pts[i][0], pts[i][1]))
    tn = (pts[near][0] - cx) * ux + (pts[near][1] - cy) * uy
    t0 = max(t0, tn - reach_m)
    t1 = min(t1, tn + reach_m)
    length = t1 - t0
    if length < SPAN_MIN_M:
        pad = (SPAN_MIN_M - length) / 2.0
        t0, t1 = t0 - pad, t1 + pad

    p0 = _to_ll(cx + ux * t0, cy + uy * t0, lat, lon)
    p1 = _to_ll(cx + ux * t1, cy + uy * t1, lat, lon)
    return {"road_name": matched_name, "span": [list(p0), list(p1)],
            "watched_m": round(max(length, 0.0), 1), "source": "osm:named"}


def span_from_aim(lat: float, lon: float, heading: float,
                  reach_m: float) -> dict:
    """Fallback span: the camera's own aim axis, no road data required.

    The project's mounting rule is to aim DOWN the street at receding traffic,
    so the aim axis IS the road for any correctly mounted node. This keeps the
    system working with no network, no Overpass and no OSM coverage - it just
    trusts the operator's aim instead of verifying it against a map.
    """
    mid = reach_m * 0.6
    half = max(SPAN_MIN_M, reach_m) / 2.0
    h = math.radians(heading)
    out = []
    for t in (mid - half, mid + half):
        out.append(list(_to_ll(t * math.sin(h), t * math.cos(h), lat, lon)))
    return {"road_name": "", "span": out, "watched_m": round(reach_m, 1),
            "source": "aim"}


def span_nearest(lat: float, lon: float, ways: list) -> Optional[dict]:
    """The nearest driveable road, when nothing is known about where the camera
    is aimed.

    🚨 MOST CONTRIBUTORS NEVER PROVIDE A HEADING, AND THEIR DOTS LANDED ON
    BUILDINGS. A browser only reports a heading while the device is MOVING, so a
    camera enrolled from a window has none, and `span_from_ways` - which picks
    the road by counting hits inside the aim cone - has no cone to work with. The
    old fallback pushed a point out along heading 0, i.e. due north, and put
    every sighting from that node on whatever happened to be north of it.

    A camera watching a street is, in practice, near that street. So with no aim
    to reason about, take the closest road and publish a span centred on the
    nearest point of it. It says less than an aimed span - it does not claim to
    know which stretch is watched - but it is true, and it puts the dot on a
    road instead of somebody's living room.
    """
    px, py = 0.0, 0.0
    best, bestd, bestname = None, 1e9, ""
    bestseg = None
    for name, geom in ways:
        for i in range(len(geom) - 1):
            a = _to_xy(geom[i][0], geom[i][1], lat, lon)
            b = _to_xy(geom[i + 1][0], geom[i + 1][1], lat, lon)
            for qx, qy in _seg_points(a, b):
                d = math.hypot(qx - px, qy - py)
                if d < bestd:
                    # 🚨 REMEMBER WHICH SEGMENT WON, NOT JUST THE POINT.
                    # See the orientation note below - without this the span is
                    # drawn at the wrong angle through the right place.
                    bestd, best, bestname = d, (qx, qy), name
                    bestseg = (a, b)
    if best is None or bestd > 120.0:
        return None            # nothing driveable close enough to be honest about
    # A span, not a point: a bare point would re-publish the camera's own spot.
    half = SPAN_MIN_M / 2.0
    # 🚨 ORIENT ALONG THE SEGMENT THAT WON, NOT THE WAY'S FIRST TWO POINTS.
    #
    # This used to look the road up again by NAME and take the bearing of
    # geom[0] -> geom[1]. On a short straight way those are the same thing. On a
    # long or curving one they are not: East Broad Street in Linden runs most of
    # a mile and bends, so a span centred correctly on the nearest point was
    # drawn at the bearing of a segment hundreds of metres away, and points
    # placed along it landed 16-21 m off the centreline - in the yards and
    # parking lots either side.
    #
    # Matching by NAME made it worse, because a name is not unique: several
    # distinct ways share "East Broad Street" here, and the loop took whichever
    # came first. The winning segment is already known at the point it wins;
    # carrying it is both cheaper and correct.
    if bestseg is not None:
        (sa, sb) = bestseg
        n = math.hypot(sb[0] - sa[0], sb[1] - sa[1]) or 1.0
        ax = ((sb[0] - sa[0]) / n, (sb[1] - sa[1]) / n)
    else:
        ax = (1.0, 0.0)
    p1 = _to_ll(best[0] - ax[0] * half, best[1] - ax[1] * half, lat, lon)
    p2 = _to_ll(best[0] + ax[0] * half, best[1] + ax[1] * half, lat, lon)
    return {"road_name": bestname or "", "span": [list(p1), list(p2)],
            "watched_m": SPAN_MIN_M, "source": "nearest"}


def span_from_travel(lat: float, lon: float, ways: list, axis_deg: float,
                     max_off_axis: float = 25.0,
                     max_dist_m: float = 120.0) -> Optional[dict]:
    """Pick the road the camera actually watches, from how its traffic MOVES.

    🚨 PROXIMITY IS THE WRONG QUESTION AND IT PICKS THE WRONG ROAD.
    span_nearest takes the closest driveable way. On a corner plot the closest
    way can be the cross street, or the alley behind, and "closest" then loses
    by half a metre to a road running at ninety degrees to the traffic. Measured
    on the operator's own camera: vehicles travel on a 93 degree axis with 0.97
    concentration over 49 passes, and the nearest road was North Bridge Street
    at 178 degrees - PERPENDICULAR - winning by 0.7 m over Hamrick Street at
    88.8 degrees, which matches the traffic to within 4 degrees.

    A camera pointed at a street sees vehicles travelling ALONG that street. So
    the road is identifiable from the sightings themselves, with no compass, no
    heading at enrolment, and nothing for a volunteer to get wrong - a browser
    in a window cannot report a bearing anyway, which is why 25 of 28 nodes
    have heading 0 and fall through to proximity in the first place.

    `axis_deg` is the dominant direction of travel folded to 0-180: a road
    carries traffic both ways, so a heading and its reverse are the same axis.
    Candidates are scored on angular agreement first and distance only as a
    tie-break, which is the inversion this function exists to make.

    Returns None when nothing agrees within `max_off_axis`. That matters: the
    caller must fall back rather than be handed a confident wrong answer, which
    is exactly what proximity did.
    """
    px, py = 0.0, 0.0
    best = None
    for name, geom in ways:
        for i in range(len(geom) - 1):
            a = _to_xy(geom[i][0], geom[i][1], lat, lon)
            b = _to_xy(geom[i + 1][0], geom[i + 1][1], lat, lon)
            dx, dy = b[0] - a[0], b[1] - a[1]
            seg = math.hypot(dx, dy)
            if seg < 1e-6:
                continue
            # Bearing of this segment, folded the same way as the travel axis.
            brg = math.degrees(math.atan2(dx, dy)) % 180.0
            off = abs(((brg - axis_deg + 90.0) % 180.0) - 90.0)
            # Closest point on the segment to the camera.
            t = max(0.0, min(1.0, ((px - a[0]) * dx + (py - a[1]) * dy) / (seg * seg)))
            qx, qy = a[0] + dx * t, a[1] + dy * t
            d = math.hypot(qx - px, qy - py)
            if d > max_dist_m or off > max_off_axis:
                continue
            # 🚨 ANGLE IN BUCKETS, THEN DISTANCE - not raw angle.
            # Sorting on the exact angle first lets a road 101 m away beat one
            # at 31 m by being 0.9 degrees better aligned, which is what this
            # function did on its first run: it chose Mill Street over Hamrick
            # Street for a camera with a 30 m reach. That is the original bug
            # inverted - optimising one quantity so hard that the other stops
            # mattering.
            #
            # A 5 degree bucket is well inside the noise of a tracker heading
            # averaged over dozens of passes, so two roads in the same bucket
            # are not meaningfully different in alignment and the nearer one
            # wins. `max_dist_m` is the real guard: a camera cannot watch a
            # road beyond its reach however perfectly aligned it is.
            key = (round(off / 5.0), d)
            if best is None or key < best[0]:
                best = (key, (qx, qy), (dx / seg, dy / seg), name, d, off)
    if best is None:
        return None
    _, point, unit, name, dist, off = best
    half = SPAN_MIN_M / 2.0
    p1 = _to_ll(point[0] - unit[0] * half, point[1] - unit[1] * half, lat, lon)
    p2 = _to_ll(point[0] + unit[0] * half, point[1] + unit[1] * half, lat, lon)
    return {"road_name": name or "", "span": [list(p1), list(p2)],
            "watched_m": SPAN_MIN_M, "source": "traffic",
            "off_axis_deg": round(off, 1), "camera_dist_m": round(dist, 1)}


# A grid cell's road geometry, kept so a MOBILE contributor does not hit
# Overpass once per vehicle. The key is the same ~400 m cell the query is
# already snapped to, so the cache cannot be finer-grained than the request
# and a hit reveals nothing a miss would not have.
_WAYS_CACHE: dict[tuple[int, int], list] = {}
# 🚨 RAISED 64 -> 4000 ON MEASUREMENT, BECAUSE 64 MEANT PERMANENT THRASH.
#
# 64 was sized when this cache only had to stop a MOBILE contributor hitting
# Overpass repeatedly - a handful of cells around one moving phone. It is now on
# the ingest path for the whole fleet: about 6,200 distinct cells are in play
# and 8,001 are on disk, so 64 slots evicted continuously and nearly every
# sighting fell through to a disk read.
#
# That showed up the moment sighting placement stopped going online: a py-spy
# dump had 35 of ~70 threads sitting in pathlib.read_text/open, reading ~20 KB
# of road JSON per sighting. The network wait had simply become a disk wait.
#
# ⚠️ SIZED FROM A MEASUREMENT, NOT A GUESS. Loading 500 real cells on the box
# moved RSS from 12 MB to 71 MB - 0.12 MB per cell - so 4,000 cells costs about
# 470 MB against 25 GB free. This is a CEILING on what gets held, not a
# reservation: it only ever holds cells actually touched.
#
# ⚠️ Memory is what caused the one real outage, on the old 4 GB box. On that
# machine this number could not have been raised. Check free memory before
# raising it further; 8,001 cells - everything on disk - would be about 940 MB.
# ⚠️ RAISED AGAIN 4000 -> 12000, BECAUSE 4000 STILL THRASHED.
# The working set is about 6,200 cells and eviction drops HALF the cache when
# it fills, so 4,000 slots guaranteed a permanent cycle of fill-and-dump.
# Measured after the raise to 4,000: still 27 of ~64 threads sitting in
# read_text/open, and /api/nodes held for 176s during a publish burst.
#
# 12,000 is above everything on disk (8,044 cells), so after warmup the ingest
# path stops touching the disk entirely. At the measured 0.12 MB per cell that
# is about 965 MB for the current disk cache, against 26 GB free on this box -
# and it is still a CEILING, not a reservation, so it only holds what is used.
_WAYS_CACHE_MAX = 12000

# 🚨 ROADS DO NOT MOVE, SO THIS MUST NOT DIE WITH THE PROCESS.
#
# The cache above is memory only and holds 64 cells. Every restart emptied it,
# and after a restart the FIRST sighting from each area needs a live Overpass
# query - which rate limits, times out, and was measured failing on all three
# mirrors within a minute of succeeding on one.
#
# When that query fails, snap_point returns None and a mobile sighting is
# published wherever the 60 m privacy jitter put it: a park, a back garden, the
# wrong street. Reported from the road - patrol cars scattered east of Bridge
# Street that all belonged on South Main. It looked like a projection bug and it
# was a cold cache plus a flaky third party.
#
# A disk cache makes the failure survivable instead of silent. A cell fetched
# once is answerable for ever, the map stops depending on Overpass being up at
# the exact second a car goes past, and the load on somebody else's free service
# drops to one query per area for the life of the deployment.
_WAYS_DIR = DATA / "roadcache"
_WAYS_DISK_TTL_S = 180 * 24 * 3600     # roads change, just not this week


# 🚨 A WAY IS (road_name, points), NOT A LIST OF POINTS.
#
# The first version of this cache serialised a way as `[tuple(pt) for pt in
# way]`, which is correct for the points and catastrophic for the name: it
# turned the string "Murphy Street" into ('M','u','r','p','h','y',...). That
# corrupted name was written straight into a node's road_name, and SQLite threw
# "Error binding parameter 21: type 'tuple' is not supported" - so EVERY camera
# enrolment 500'd with "internal error".
#
# It hid for a while because it can only bite when the cache HITS, and before
# the cache was persisted Overpass was usually failing anyway. Making snapping
# reliable is what exposed it.
#
# The version suffix means the broken files are ignored rather than needing to
# be found and deleted on every machine that ran the bad build.
_WAYS_FORMAT = "v2"


def _disk_path(key: tuple) -> Path:
    return _WAYS_DIR / f"{key[0]}_{key[1]}.{_WAYS_FORMAT}.json"


def _disk_get(key: tuple) -> Optional[list]:
    p = _disk_path(key)
    try:
        if not p.exists():
            return None
        if time.time() - p.stat().st_mtime > _WAYS_DISK_TTL_S:
            return None
        raw = json.loads(p.read_text(encoding="utf-8"))
        # Round-trip the SHAPE, not just the numbers: (name, [points]).
        out = []
        for name, pts in raw:
            out.append((name, [tuple(pt) for pt in pts]))
        return out
    except Exception:
        return None


def _disk_put(key: tuple, ways: list) -> None:
    try:
        _WAYS_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _disk_path(key).with_suffix(".tmp")
        tmp.write_text(json.dumps(
            [[name, [list(pt) for pt in pts]] for name, pts in ways]),
            encoding="utf-8")
        tmp.replace(_disk_path(key))
    except Exception:
        pass          # a cache that cannot be written is not an error


def _cell(lat: float, lon: float) -> tuple[int, int]:
    dlon = GRID_DEG / max(math.cos(math.radians(lat)), 1e-6)
    return (int(math.floor(lat / GRID_DEG)), int(math.floor(lon / dlon)))


def ways_for_cell(lat: float, lon: float, online: bool = True,
                  respect_backoff: bool = True) -> Optional[list]:
    """Road geometry for this grid cell, from cache when we have it.

    🚨 THE AIM PATH WAS NOT USING THIS CACHE, AND THAT IS WHY A CAMERA COULD NOT
    BE AIMED. `resolve` called `_fetch_ways_bounded` directly, so every press of
    "Save and check the road" was a fresh Overpass query - and Overpass rate
    limits, as the note in `resolve` already records: "a sweep of 18 nodes
    earned a 429 in under two minutes". Measured here: the first query returned
    138 ways in 3.0s and the next three timed out.

    The page asks the owner to adjust the direction and save AGAIN until the
    road falls inside the cone. That workflow is a loop, so the one path that
    most needed a cache was the only one without it, and the second attempt was
    the one guaranteed to fail. Roads do not move; the cell is the same 400 m
    grid the query is snapped to anyway.

    ⚠️ `respect_backoff=False` FOR A HUMAN PRESSING A BUTTON. The negative cache
    exists to stop a stampede of automated sighting lookups, and applying it to
    a deliberate click means the owner gets an instant "no road" that never
    asked anything. One person clicking save is not a stampede.
    """
    key = _cell(lat, lon)
    ways = _WAYS_CACHE.get(key)
    if ways is not None:
        return ways
    # Disk before network. This is the line that stops a restart costing every
    # area its road data, and it costs a single file read.
    ways = _disk_get(key)
    if ways is not None:
        _WAYS_CACHE[key] = ways
        return ways
    if not online:
        return None
    # 🚨 A FAILING CELL MUST NOT BE RETRIED ON EVERY REQUEST.
    # Failures were never remembered, so a cell Overpass is refusing was
    # re-fetched by every sighting from that camera - each one a synchronous
    # call holding an admission permit, all destined to fail the same way. The
    # negative entry is short: long enough to stop the stampede, short enough
    # that recovery is automatic.
    if respect_backoff and _FAIL_UNTIL.get(key, 0.0) > time.time():
        return None
    try:
        ways = _fetch_ways_bounded(lat, lon)
    except Exception:
        streak = _FAIL_STREAK.get(key, 0) + 1
        _FAIL_STREAK[key] = streak
        # 120s, 240s, 480s ... capped at an hour.
        wait = min(_FAIL_TTL_S * (2 ** (streak - 1)), _FAIL_TTL_MAX_S)
        _FAIL_UNTIL[key] = time.time() + wait
        raise
    _FAIL_UNTIL.pop(key, None)
    _FAIL_STREAK.pop(key, None)
    if len(_WAYS_CACHE) >= _WAYS_CACHE_MAX:
        # ⚠️ Evict HALF, not everything. clear() threw away every warm cell the
        # moment the 65th arrived, so a network spread over more than
        # _WAYS_CACHE_MAX cells thrashed to permanently empty and every node
        # re-fetched constantly.
        for k in list(_WAYS_CACHE)[:_WAYS_CACHE_MAX // 2]:
            _WAYS_CACHE.pop(k, None)
    _WAYS_CACHE[key] = ways
    _disk_put(key, ways)
    return ways


def snap_point(lat: float, lon: float, seed: str,
               online: bool = True) -> Optional[tuple[float, float]]:
    """Put a loose point on the nearest road, or None if we cannot.

    🚨 FOR MOBILE CONTRIBUTORS, WHOSE DOTS LANDED IN PARKS AND BUILDINGS.
    A fixed camera gets a watched span at enrolment and its sightings are
    placed along it. A phone has no span - it moves - so its sightings were
    published as its own GPS with the 60 m jitter applied and nothing else, and
    60 m in a random direction routinely lands off the road: a park, a back
    garden, the middle of a block.

    Snapping AFTER the jitter matters and is not the same as snapping before.
    The caller jitters first, so what arrives here is already displaced; this
    finds the nearest road to THAT point and then places the dot at a stable
    position along an 80 m stretch of it. The privacy noise is not undone - it
    is redistributed along the road instead of across it, which is also the
    more honest shape, since what is actually known is "a vehicle passed on
    this street", not "a vehicle was in this garden".

    Returns None when the road lookup is unavailable, and the caller keeps the
    jittered point: a dot slightly off the road beats no dot at all, and a
    third-party API being down must never drop a sighting.
    """
    try:
        ways = ways_for_cell(lat, lon, online=online)
        if not ways:
            return None
        got = span_nearest(lat, lon, ways)
        span = (got or {}).get("span")
        if not span or len(span) < 2:
            return None
        return point_on_span(span, seed)
    except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError,
            http.client.HTTPException, OverpassBusy):
        return None


def resolve(lat: float, lon: float, heading: float, fov: float,
            reach_m: float, online: bool = True) -> Optional[dict]:
    """Best available watched span for a camera, or None if there isn't one.

    A failure here must never block enrolment - a node that failed to enrol
    because a third-party API was down is not a working node.

    🚨 BUT "DID NOT BLOCK ENROLMENT" IS NOT "INVENTED A ROAD".
    This used to end at span_from_aim, which pushes a line out along the
    heading and knows nothing about streets - so when Overpass was down or
    rate-limiting (which it does; a sweep of 18 nodes earned a 429 in under two
    minutes), a camera was given a compass line instead of a road. With the
    usual heading of 0 that is a line pointing DUE NORTH, drawn on the public
    map as a watched road, with an empty road_name because there is no road.

    10 of 52 live spans were made this way, one of them carrying 58 real
    sightings plotted along a street that does not exist. Reported as "the
    green marker is vertical and should be horizontal" - it should have been
    neither, because nothing was ever known about it.

    nodes.py already states the rule this restores: "Publishing nothing is
    recoverable. Publishing a fabricated road is not." An unknown road is now
    None, and the caller stores no span at all - the same treatment a carried
    phone gets, and for the same reason.
    """
    if online:
        try:
            # Bounded: see _fetch_ways_bounded. This is reached from
            # POST /api/enroll while holding an admission permit.
            # Cached per 400 m cell, and NOT subject to the failure backoff:
            # this is reached from a person pressing "save", who is entitled to
            # an actual attempt. See ways_for_cell.
            ways = ways_for_cell(lat, lon, online=True, respect_backoff=False)
            got = span_from_ways(lat, lon, heading, fov, reach_m, ways or [])
            if got:
                return got
            # No aim to work with (a window enrolment reports no heading), so
            # fall back to the road itself rather than to a compass direction
            # nobody supplied.
            got = span_nearest(lat, lon, ways or [])
            if got:
                return got
        except (urllib.error.URLError, OSError, ValueError, KeyError,
            http.client.HTTPException, OverpassBusy) as exc:
            # Worth saying loudly: this is recoverable (re-snap later) but only
            # if somebody knows it happened.
            print(f"[road] snap unavailable ({exc.__class__.__name__}: {exc}) - "
                  f"NO span will be stored; re-snap this node later")
            return None
    return None


# --------------------------------------------------------------------------
# Placing a sighting on the span
# --------------------------------------------------------------------------

def point_on_span(span: list, seed: str) -> tuple[float, float]:
    """A stable point along a published span.

    Deterministic in `seed` for two reasons. A dot that jumps to a new place on
    every page refresh is obviously fake, and re-deriving a position on each
    read would let anyone average many reads of the same sighting back to the
    span midpoint - which is the thing the minimum span length exists to hide.
    One sighting, one position, forever.

    The spread is not a claim of precision. It is the opposite: the camera
    covers this stretch and cannot say which metre of it the vehicle occupied,
    so the honest rendering is a dot somewhere along the stretch rather than a
    pile of dots on a single invented point.
    """
    import hashlib
    (a_lat, a_lon), (b_lat, b_lon) = span[0], span[1]
    h = hashlib.blake2b(seed.encode("utf-8"), digest_size=8).digest()
    t = int.from_bytes(h, "big") / float(1 << 64)
    return (round(a_lat + (b_lat - a_lat) * t, 6),
            round(a_lon + (b_lon - a_lon) * t, 6))


def span_length_m(span: list) -> float:
    return haversine_m(span[0][0], span[0][1], span[1][0], span[1][1])


def span_bearing(span: list) -> float:
    return bearing_deg(span[0][0], span[0][1], span[1][0], span[1][1])
