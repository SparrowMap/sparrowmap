"""Resolve each node's TOWN, for the zoomed-out map.

Zoomed out to a state or a country, an 80 m watched span is a fraction of a
pixel. The map then shows a scatter of tiny green smears that read as markers -
"a thing is at this spot" - which is the exact impression the corridor shape was
drawn to avoid. What is honestly known at that zoom is the town, so that is what
the map should say: "Brighton, 3 cameras".

    python tools/backfill_places.py              # show what would be resolved
    python tools/backfill_places.py --apply      # write it
    python tools/backfill_places.py --apply --all   # re-resolve nodes that have one

🚨 THE JITTERED POSITION IS SENT, NEVER THE TRUE ONE.
A town lookup would survive the 60 m jitter easily, so this costs nothing in
accuracy - and the rule that a camera's real coordinates never reach a third
party is not worth making an exception to for a convenience. road.py already
grid-snaps its Overpass bbox for the same reason. If a node somehow has no
published point, it is skipped rather than falling back to the true one.

⚠️ NOMINATIM ALLOWS ONE REQUEST PER SECOND and will block a client that ignores
it. This sleeps between calls and caches by rounded coordinate, so a town with
several cameras costs one lookup, not one per camera.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db          # noqa: E402

UA = "RavenMap/1.0 (town lookup)"
# zoom=10 is the "city/town" level. Higher gets suburbs and splits one town into
# several names; lower collapses neighbouring towns into a county.
ZOOM = 10


def town_of(lat: float, lon: float) -> tuple[str | None, str | None]:
    """(town, state) for a point, or (None, None). One second per call."""
    url = ("https://nominatim.openstreetmap.org/reverse?"
           + urllib.parse.urlencode({"format": "json", "zoom": ZOOM,
                                     "lat": f"{lat:.4f}", "lon": f"{lon:.4f}"}))
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            addr = (json.loads(r.read()) or {}).get("address") or {}
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"   lookup failed ({exc.__class__.__name__}: {exc})")
        return None, None
    # Nominatim names the same kind of place differently by country and size.
    town = (addr.get("city") or addr.get("town") or addr.get("village")
            or addr.get("municipality") or addr.get("hamlet")
            or addr.get("suburb") or addr.get("county"))
    return town, addr.get("state")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--all", action="store_true",
                    help="re-resolve nodes that already have a place")
    args = ap.parse_args()

    conn = db.connect()
    rows = db.nodes(active_only=False)
    todo = [n for n in rows if args.all or not n.get("place")]
    print(f"{len(rows)} nodes, {len(todo)} to resolve\n")

    cache: dict[tuple, tuple] = {}
    resolved = skipped = 0
    for n in todo:
        lat = n.get("pub_lat")
        lon = n.get("pub_lon")
        if lat is None or lon is None:
            # No published point means nothing may be sent. Deliberately NOT
            # falling back to lat/lon - that is the one value this must not leak.
            print(f"{n['id']}  no published position - skipped")
            skipped += 1
            continue
        # ~1 km bucket: neighbouring cameras in one town share a single lookup.
        key = (round(lat, 2), round(lon, 2))
        if key in cache:
            town, state = cache[key]
        else:
            town, state = town_of(lat, lon)
            cache[key] = (town, state)
            time.sleep(1.1)          # Nominatim: 1 req/sec, and mean it
        label = f"{town}, {state}" if town and state else (town or "")
        print(f"{n['id']}  {str(n['name'])[:24]:26} -> {label or '(unresolved)'}")
        if town and args.apply:
            conn.execute("UPDATE nodes SET place=? WHERE id=?", (label, n["id"]))
            resolved += 1

    if args.apply:
        conn.commit()
        print(f"\nwrote {resolved} places ({skipped} skipped, "
              f"{len(cache)} lookups for {len(todo)} nodes)")
    else:
        print(f"\n{len(cache)} lookups would be made   [--apply to write]")


if __name__ == "__main__":
    main()
