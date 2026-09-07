"""Live aircraft over a box: who they belong to, and which ones are circling.

STAGED. Reached only when `aircraft_preview` is on (see core.py DEFAULTS), so
this file ships everywhere and runs nowhere it has not been switched on.

TWO SIGNALS, AND NEITHER IS ENOUGH ALONE
  registry  who owns it, from the FAA's public database. Finds the county
            sheriff's helicopter. Cannot find federal surveillance, which is
            widely documented as registered to front companies and therefore
            appears as an ordinary corporation.
  orbit     what it is DOING. Sustained circling at low altitude over one
            point, which is the pattern journalists used to identify
            surveillance aircraft that were registered to look like nothing.
            Cannot tell you who: crop dusters, traffic helicopters, news crews
            and training circuits all orbit.

Registration is a claim somebody filed once. An orbit is a thing an aircraft is
doing right now, over your house. Together they are worth looking at; either
one alone is worth looking at less.

🚨 THIS PAGE NAMES NO ONE AS POLICE. It reports ownership as the FAA records
it and behaviour as the track shows it, and lets the reader draw the line -
the same posture as the camera network, where the classifier proposes and a
human decides.
"""

from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.request
from pathlib import Path

from core import DATA

OPENSKY = "https://opensky-network.org/api/states/all"
UA = {"User-Agent": "RavenMap/1.0 (aircraft)"}
REGISTRY = DATA / "faa_registry.json"        # written by tools/gov_aircraft.py

# Tracks accumulate in memory: an orbit is only visible over time, and a single
# snapshot of positions cannot show one. Bounded so a long-running hub cannot
# grow without limit.
_TRACKS: dict = {}
_WINDOW_S = 30 * 60
_MAX_TRACKS = 4000

# Aircraft pushed by LOCAL ADS-B receivers (a Pi running dump1090), keyed by
# icao -> (lat, lon, alt, track, call, ts, node). These augment the OpenSky feed
# with coverage it misses, and expire quickly since a plane out of local range
# stops being pushed. See tools/sensors/adsb_feed.py and /api/aircraft/ingest.
_LOCAL: dict = {}
_LOCAL_TTL_S = 60.0
_MAX_LOCAL = 4000


def ingest_local(craft: list, node: str) -> int:
    """Store aircraft a local receiver reports. Returns how many were accepted.
    Each entry: {icao, lat, lon, alt_m?, track?, call?}. Bad rows are skipped
    rather than failing the batch - a receiver should never be able to 400 the
    whole push over one malformed line."""
    now = time.time()
    if len(_LOCAL) > _MAX_LOCAL:
        _LOCAL.clear()
    n = 0
    for a in craft:
        try:
            icao = str(a.get("icao") or "").strip().lower()
            lat = float(a["lat"]); lon = float(a["lon"])
        except (AttributeError, TypeError, ValueError, KeyError):
            continue
        if not icao or not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        alt = a.get("alt_m")
        try:
            alt = float(alt) if alt is not None else None
        except (TypeError, ValueError):
            alt = None
        _LOCAL[icao] = {"lat": lat, "lon": lon, "alt": alt,
                        "track": a.get("track"), "call": str(a.get("call") or ""),
                        "ts": now, "node": node}
        # Feed the same track store OpenSky uses, so orbit detection works on
        # locally-seen aircraft too.
        t = _TRACKS.setdefault(icao, [])
        if not t or now - t[-1][3] > 5:
            t.append([lat, lon, alt, now])
        _TRACKS[icao] = [p for p in t if now - p[3] <= _WINDOW_S]
        n += 1
    return n

# Same thresholds as tools/orbit_watch.py, and deliberately conservative: a
# missed orbit costs nothing, a false one sends somebody to look at a crop
# duster and teaches them to ignore the next alert.
MIN_POINTS, MIN_PATH_KM, MAX_NET_KM = 8, 8.0, 5.0
MIN_TURN_DEG, MAX_RADIUS_KM, MAX_ALT_M = 540.0, 8.0, 4000.0

_LE_WORDS = ("POLICE", "SHERIFF", "HIGHWAY PATROL", "STATE PATROL",
             "PUBLIC SAFETY", "MARSHAL", "HOMELAND SECURITY", "CUSTOMS",
             "BORDER PROTECTION", "TROOPER", "CONSTABLE", "CORRECTIONS")
# Marshall University is not a US Marshal, and it flies constantly. Education is
# never law enforcement, so it is excluded outright rather than by hoping the
# next pattern is tighter - this exact word has caused two false positives.
_NOT_LE = ("UNIVERSITY", "COLLEGE", "SCHOOL", "REGENTS", "TRUSTEES",
           "ACADEMY", "INSTITUTE")


def _registry() -> dict:
    try:
        d = json.loads(REGISTRY.read_text(encoding="utf-8"))
        return d.get("gov") or {}
    except Exception:
        return {}


def _is_le(owner: str) -> bool:
    o = (owner or "").upper()
    return any(w in o for w in _LE_WORDS) and not any(w in o for w in _NOT_LE)


def _km(a, b) -> float:
    R = 6371.0
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp, dl = math.radians(b[0] - a[0]), math.radians(b[1] - a[1])
    x = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(x))


def _bearing(a, b) -> float:
    y = math.sin(math.radians(b[1] - a[1])) * math.cos(math.radians(b[0]))
    x = (math.cos(math.radians(a[0])) * math.sin(math.radians(b[0]))
         - math.sin(math.radians(a[0])) * math.cos(math.radians(b[0]))
         * math.cos(math.radians(b[1] - a[1])))
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def _orbit(pts: list) -> dict:
    """pts = [(lat, lon, alt, ts), ...] oldest first."""
    if len(pts) < MIN_POINTS:
        return {"orbit": False}
    path = sum(_km(pts[i - 1][:2], pts[i][:2]) for i in range(1, len(pts)))
    net = _km(pts[0][:2], pts[-1][:2])
    clat = sum(p[0] for p in pts) / len(pts)
    clon = sum(p[1] for p in pts) / len(pts)
    radius = max(_km((clat, clon), p[:2]) for p in pts)
    turn = 0.0
    for i in range(2, len(pts)):
        d = abs((_bearing(pts[i - 1][:2], pts[i][:2])
                 - _bearing(pts[i - 2][:2], pts[i - 1][:2]) + 180) % 360 - 180)
        # A single ~180 degree jump is a gap or a glitch, not a reversal.
        if d < 170:
            turn += d
    alts = [p[2] for p in pts if p[2] is not None]
    alt = sum(alts) / len(alts) if alts else None
    return {"orbit": (path >= MIN_PATH_KM and net <= MAX_NET_KM
                      and turn >= MIN_TURN_DEG and radius <= MAX_RADIUS_KM
                      and (alt is None or alt <= MAX_ALT_M)),
            "turn": round(turn), "radius_km": round(radius, 1),
            "path_km": round(path, 1), "net_km": round(net, 1),
            "minutes": round((pts[-1][3] - pts[0][3]) / 60)}


def live(box: list) -> dict:
    lamin, lomin, lamax, lomax = box
    url = (f"{OPENSKY}?lamin={lamin}&lomin={lomin}&lamax={lamax}&lomax={lomax}")
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA),
                                    timeout=25) as r:
            states = (json.loads(r.read()) or {}).get("states") or []
    except (urllib.error.URLError, OSError, ValueError) as exc:
        # Say which upstream failed. "aircraft unavailable" with no cause is
        # how a rate limit gets mistaken for a bug in this file.
        return {"error": f"ADS-B feed unavailable: {exc}", "aircraft": []}

    gov = _registry()
    now = time.time()
    if len(_TRACKS) > _MAX_TRACKS:
        _TRACKS.clear()

    out = []
    for s in states:
        icao = (s[0] or "").strip().lower()
        lat, lon, alt = s[6], s[5], s[7]
        if not icao or lat is None or lon is None:
            continue
        t = _TRACKS.setdefault(icao, [])
        if not t or now - t[-1][3] > 5:
            t.append([lat, lon, alt, now])
        _TRACKS[icao] = [p for p in t if now - p[3] <= _WINDOW_S]

        who = gov.get(icao.upper())
        orb = _orbit(_TRACKS[icao])
        out.append({
            "icao": icao,
            "call": (s[1] or "").strip(),
            "lat": lat, "lon": lon,
            "alt_m": round(alt) if alt else None,
            "speed": round(s[9]) if s[9] else None,
            "track": s[10],
            "owner": who[0] if who else None,
            "n_number": who[1] if who else None,
            "gov": bool(who),
            "law_enforcement": _is_le(who[0]) if who else False,
            "orbit": orb.get("orbit", False),
            "orbit_detail": orb if orb.get("orbit") else None,
            "points": len(_TRACKS[icao]),
        })

    # Merge in locally-received aircraft the OpenSky snapshot did not include,
    # inside the requested box and still fresh. Coverage a Pi caught that the
    # aggregator missed.
    seen = {a["icao"] for a in out}
    lamin, lomin, lamax, lomax = box
    for icao, a in list(_LOCAL.items()):
        if now - a["ts"] > _LOCAL_TTL_S:
            _LOCAL.pop(icao, None)
            continue
        if icao in seen:
            continue
        if not (lamin <= a["lat"] <= lamax and lomin <= a["lon"] <= lomax):
            continue
        who = gov.get(icao.upper())
        orb = _orbit(_TRACKS.get(icao, []))
        out.append({
            "icao": icao, "call": a["call"],
            "lat": a["lat"], "lon": a["lon"],
            "alt_m": round(a["alt"]) if a["alt"] else None,
            "speed": None, "track": a["track"],
            "owner": who[0] if who else None,
            "n_number": who[1] if who else None,
            "gov": bool(who),
            "law_enforcement": _is_le(who[0]) if who else False,
            "orbit": orb.get("orbit", False),
            "orbit_detail": orb if orb.get("orbit") else None,
            "points": len(_TRACKS.get(icao, [])),
            "local": True,
        })

    return {"aircraft": out,
            "counts": {"total": len(out),
                       "government": sum(1 for a in out if a["gov"]),
                       "law_enforcement": sum(1 for a in out if a["law_enforcement"]),
                       "circling": sum(1 for a in out if a["orbit"])},
            # A page that shows nothing must be able to say WHY. Without the
            # registry this still works, it just cannot name an owner.
            "registry_loaded": len(gov),
            "ts": now}
