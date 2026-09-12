"""ON THE BOX: dump every published police/gov sighting + its crop for the desktop.

    .venv/bin/python3 tools/reid/export.py                  # everything
    .venv/bin/python3 tools/reid/export.py --since 1789000000   # only newer rows

Writes /tmp/reid_rows.json and /tmp/reid_snaps.tar. Public tier, police/gov,
with a photo - the same rows the map already publishes; nothing here leaves
the box that a visitor could not fetch from /api/sighting/<id> and /snap/.
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
    ap.add_argument("--since", type=float, default=0.0, help="unix ts; rows newer than this")
    ap.add_argument("--out", default="/tmp")
    args = ap.parse_args()
    conn = db.connect()
    rows = [dict(r) for r in conn.execute("""
        SELECT s.id, s.node_id, s.ts, s.lat, s.lon, s.vclass, s.vclass_conf, s.snap,
               s.snap_full, s.reviewed, s.vehicle_tag, s.markings,
               n.kind AS node_kind, n.name AS node_name, n.place, n.road_name
        FROM sightings s JOIN nodes n ON n.id = s.node_id
        WHERE s.tier = 'public' AND s.vclass IN ('police', 'gov')
          AND s.snap IS NOT NULL AND s.ts > ?
        ORDER BY s.ts""", (args.since,)).fetchall()]
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
