"""Give every camera a ROAD and a TOWN, so a sighting can say where it was taken.

    python tools/backfill_where.py                 # show what would be resolved
    python tools/backfill_where.py --apply         # write it
    python tools/backfill_where.py --apply --limit 300   # a bounded run

WHY THIS EXISTS
The sighting panel used to print `camera: n_1564f636` and a heading nobody
could use. What a reader actually wants is "7200 block of Elroy Rd, Austin,
Texas, from a public traffic camera" - and the two columns that can say that
(`nodes.road_name`, `nodes.place`) were empty for all 15,218 public cameras and
most volunteer ones. backfill_places.py filled towns for volunteer nodes only,
and nothing ever filled a road.

One reverse lookup at street zoom returns the road AND the town AND the state,
so a camera costs one request, not two.

🚨 WHICH POSITION IS SENT
  * public_cam  - its TRUE coordinates. They are published by the transport
                  department in the same feed the pictures come from; the map
                  already draws them at that exact point.
  * everything else - the JITTERED published point (pub_lat/pub_lon), exactly
                  as backfill_places.py does. The road of a point 60 m from a
                  house is the same road; the house is not disclosed. A node
                  with no published point is skipped, never falled back.

🚨 A SNAPPED ROAD IS NEVER OVERWRITTEN. road.py writes road_name when a
volunteer's span is snapped to OSM; that name came from the actual watched way
and is better than a point lookup. This only fills road_name when it is empty.

⚠️ NOMINATIM ALLOWS ONE REQUEST PER SECOND and blocks clients that ignore it.
Sleeps 1.1 s between calls. 15k cameras is ~4.5 h; run it with nohup on the box
and let it finish, it is idempotent and resumes where it stopped (a node with
both fields filled is not asked again). Cameras that have PUBLISHED police or
government sightings go first, so the panel improves for the rows people are
actually opening within the first quarter hour.
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

UA = "SparrowMap/1.0 (https://sparrowmap.com; camera road+town lookup)"
# zoom=17 is street level: `road` is populated and the town keys are the same
# ones backfill_places.py reads at zoom 10.
ZOOM = 17


def where_of(lat: float, lon: float) -> tuple[str | None, str | None, str | None]:
    """(road, town, state) for a point, any of them None. One second per call."""
    url = ("https://nominatim.openstreetmap.org/reverse?"
           + urllib.parse.urlencode({"format": "json", "zoom": ZOOM,
                                     "lat": f"{lat:.5f}", "lon": f"{lon:.5f}"}))
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            addr = (json.loads(r.read()) or {}).get("address") or {}
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"   lookup failed ({exc.__class__.__name__}: {exc})", flush=True)
        return None, None, None
    road = (addr.get("road") or addr.get("pedestrian") or addr.get("highway")
            or addr.get("residential") or None)
    town = (addr.get("city") or addr.get("town") or addr.get("village")
            or addr.get("municipality") or addr.get("hamlet")
            or addr.get("suburb") or addr.get("county"))
    return road, town, addr.get("state")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="stop after N lookups")
    args = ap.parse_args()

    conn = db.connect()
    rows = db.nodes(active_only=False)
    todo = [n for n in rows
            if not n.get("place") or not (n.get("road_name") or "").strip()]

    # The cameras whose sightings people open come first.
    hot = {r[0] for r in conn.execute(
        "SELECT DISTINCT node_id FROM sightings WHERE tier='public' "
        "AND vclass IN ('police','government')").fetchall()}
    todo.sort(key=lambda n: (0 if n["id"] in hot else 1,
                             -(n.get("sightings") or 0)))
    print(f"{len(rows)} nodes, {len(todo)} to resolve "
          f"({sum(1 for n in todo if n['id'] in hot)} with published gov sightings first)\n",
          flush=True)

    done = wrote = skipped = 0
    for n in todo:
        if args.limit and done >= args.limit:
            break
        if n.get("kind") == "public_cam":
            lat, lon = n.get("lat"), n.get("lon")
        else:
            lat, lon = n.get("pub_lat"), n.get("pub_lon")
        if lat is None or lon is None:
            skipped += 1
            continue
        road, town, state = where_of(float(lat), float(lon))
        done += 1
        time.sleep(1.1)          # Nominatim: 1 req/sec, and mean it
        place = f"{town}, {state}" if town and state else (town or "")
        print(f"{n['id']}  {str(n.get('name'))[:30]:32} -> "
              f"{road or '(no road)'} · {place or '(no town)'}", flush=True)
        if not args.apply:
            continue
        sets, vals = [], []
        if place and not n.get("place"):
            sets.append("place=?"); vals.append(place)
        if road and not (n.get("road_name") or "").strip():
            sets.append("road_name=?"); vals.append(road)
        if sets:
            conn.execute(f"UPDATE nodes SET {', '.join(sets)} WHERE id=?",
                         (*vals, n["id"]))
            conn.commit()        # per row: a killed run keeps what it learned
            wrote += 1

    print(f"\n{done} lookups, {wrote} nodes written, {skipped} skipped"
          + ("" if args.apply else "   [--apply to write]"), flush=True)


if __name__ == "__main__":
    main()
