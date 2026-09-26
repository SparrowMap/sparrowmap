"""ON THE BOX: dump every published police/gov sighting + its crop for the desktop.

    .venv/bin/python3 tools/reid/export.py                  # everything
    .venv/bin/python3 tools/reid/export.py --since 1789000000   # only newer rows

Writes /tmp/reid_rows.json and /tmp/reid_snaps.tar. Public tier, police/gov,
with a photo - the same rows the map already publishes; nothing here leaves
the box that a visitor could not fetch from /api/sighting/<id> and /snap/.

🚨 `--since` IS ON WHEN A ROW BECAME ELIGIBLE, NOT WHEN THE CAR DROVE PAST.
A sighting only becomes public when somebody CONFIRMS it, which can be hours
after the vehicle passed. Filtering on `ts` meant a row confirmed after the
cursor had moved past its pass-time was never exported and never got markings -
silently, for ever.

Measured 2026-09-26, his catch ("still not getting all readables in markings"):
sighting 13339925 passed at 13:52 UTC and was confirmed at 15:49, after the
14:43 run had already pushed the cursor to ~14:43. A Finnish patrol van with
POLIS on the door and 207 on the roof, and the pipeline could not see it.

So the cursor is `max(ts, reviewed_at)` - the moment the row became something
this pipeline is allowed to read - and run.py keeps its cursor the same way.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tarfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
import db          # noqa: E402

SNAPS = Path(os.environ.get("SPARROW_SNAPS", "/opt/sparrowmap/data/snaps"))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", type=float, default=0.0,
                    help="unix ts; rows that BECAME ELIGIBLE after this")
    ap.add_argument("--out", default="/tmp")
    args = ap.parse_args()
    conn = db.connect()
    rows = [dict(r) for r in conn.execute("""
        SELECT s.id, s.node_id, s.ts, s.lat, s.lon, s.vclass, s.vclass_conf, s.snap,
               s.snap_full, s.reviewed, s.reviewed_at, s.vehicle_tag, s.markings,
               max(s.ts, coalesce(s.reviewed_at, 0)) AS eligible_at,
               n.kind AS node_kind, n.name AS node_name, n.place, n.road_name
        FROM sightings s JOIN nodes n ON n.id = s.node_id
        WHERE s.tier = 'public' AND s.vclass IN ('police', 'gov')
          AND s.snap IS NOT NULL
          AND max(s.ts, coalesce(s.reviewed_at, 0)) > ?
        ORDER BY eligible_at""", (args.since,)).fetchall()]
    out = Path(args.out)
    json.dump(rows, open(out / "reid_rows.json", "w"))
    n = 0
    with tarfile.open(out / "reid_snaps.tar", "w") as t:
        for r in rows:
            for f in (r["snap"], r["snap_full"]):
                if f and (SNAPS / f).exists():
                    t.add(SNAPS / f, arcname=f)
                    n += 1
    print(f"{len(rows)} rows, {n} files -> {out}")


if __name__ == "__main__":
    main()
