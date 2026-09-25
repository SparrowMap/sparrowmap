"""Where a sighting actually happened, cached, for corroborating what is
painted on a vehicle.

    town, state = places.near(lat, lon)        # -> ("Chicago", "Illinois")

🚨 WHY THIS EXISTS: A MOVING CAMERA HAS NO TOWN.

`city_of` in link.py will only publish the town painted on a car when the
CAMERA's own town agrees with it - that is what stops "TTLEPOLICE" publishing
a car belonging to the town of Ttle. It works for a fixed camera, whose town
was resolved once at enrolment, and it cannot work at all for a dashcam, whose
node has no place: measured 2026-09-25, a Chicago patrol car carried a full
position and a `place` of None, so its livery went out as "HICAGO POLICE"
with nothing to check it against.

The coordinates were there the whole time. This turns them into a town and a
state so the same corroboration rule works for a camera that moves.

🚨 CACHED HARD, AND ROUNDED BEFORE IT IS CACHED.
Nominatim asks for one request per second and this is somebody else's free
service. Coordinates are rounded to ~1 km before they become a key, so a
dashcam driving down one street asks once rather than once per sighting, and
the answer is kept for ever - a town does not move. A cache miss costs a
second; a cache hit costs nothing, which is what makes this affordable inside
a pipeline that runs every four hours.

⚠️ ROUNDING IS ALSO A PRIVACY PROPERTY, NOT ONLY A RATE LIMIT. The cache file
would otherwise become a list of the exact places a volunteer's dashcam has
been. At two decimal places it records that somebody was within a kilometre of
a road junction, which is what the map publishes anyway.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO / "tools"))
CACHE = REPO / "data" / "reid" / "places.json"

#: ~1.1 km at two decimal places. Coarse enough to be one lookup per
#: neighbourhood, fine enough that the answer is still the right town.
ROUND = 2

#: Nominatim's published limit is one request a second. This is deliberately
#: slower than that, because nothing here is urgent and the service is free.
DELAY_S = 1.2

_mem: dict = {}
_last = [0.0]


def _load() -> dict:
    if _mem:
        return _mem
    if CACHE.exists():
        try:
            _mem.update(json.loads(CACHE.read_text()))
        except (OSError, ValueError):
            pass
    return _mem


def _save() -> None:
    try:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(_mem, separators=(",", ":")))
    except OSError:
        pass


def near(lat, lon, allow_network: bool = True) -> tuple:
    """(town, state) for a point, either possibly None.

    A cached answer costs nothing. A miss costs a second, and only happens
    with allow_network - so a caller that must not block can ask for what is
    already known and get None rather than a delay.
    """
    try:
        key = f"{round(float(lat), ROUND)},{round(float(lon), ROUND)}"
    except (TypeError, ValueError):
        return (None, None)
    cache = _load()
    if key in cache:
        v = cache[key]
        return (v.get("town"), v.get("state"))
    if not allow_network:
        return (None, None)

    from backfill_where import where_of
    wait = DELAY_S - (time.time() - _last[0])
    if wait > 0:
        time.sleep(wait)
    _last[0] = time.time()
    _road, town, state = where_of(float(lat), float(lon))
    # 🚨 A FAILED LOOKUP IS CACHED TOO, AS A FAILURE.
    # Without this, every run re-asks for the same unanswerable point for
    # ever - the ocean, a private road, a coordinate Nominatim simply has
    # nothing for - and the pipeline spends its whole budget on them. Missing
    # data is not negative data, so it is stored as null and re-asked only if
    # somebody clears the cache on purpose.
    cache[key] = {"town": town, "state": state}
    _save()
    return (town, state)


def known(lat, lon) -> tuple:
    """Cache-only lookup: never touches the network."""
    return near(lat, lon, allow_network=False)


def stats() -> dict:
    c = _load()
    return {"cached": len(c),
            "with_town": sum(1 for v in c.values() if v.get("town"))}
