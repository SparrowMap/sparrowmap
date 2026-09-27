#!/usr/bin/env python3
r"""Load a fixed ALPR-camera location dump into data/alpr.db for route avoidance.

    python3 tools/load_alpr.py path/to/alpr.tsv          # rebuild data/alpr.db
    python3 tools/load_alpr.py path/to/alpr.tsv --db data/alpr.db

Input is a TAB-separated file with a header row and at least `lat` and `lon`;
an optional `reads_plates` column (1/0) is kept so the map can later show which
cameras read a number plate rather than only watch. Anything else is ignored.

🚨 THIS IS REFERENCE DATA, NOT LIVE STATE. It is a snapshot of where ALPR
cameras were known to be on a given date, rebuilt wholesale when a newer dump
arrives - so it lives in its OWN sqlite file, not in sparrow.db. Keeping it out
of the main database means it never touches a backup, a WAL checkpoint, or the
janitor, and a bad import can be fixed by re-running this rather than by
surgery on the file everything else depends on.

⚠️ POSITIONS ARE ROUNDED AND DEDUPED. A Flock pole is often listed several
times (one row per direction it faces); routing only cares that a camera is at
a spot, so rows are collapsed to 5 decimal places (~1 m) and the plate-reading
flag is OR-ed across the listings at that spot.

The point of the file is nav.alpr_polygons: a bounding-box query returns the
cameras near a corridor, and the router is asked to avoid a small box around
each. So the only index that matters is on (lat, lon), and this builds it.
"""
from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("src", help="TSV with lat, lon [, reads_plates]")
    ap.add_argument("--db", default=str(ROOT / "data" / "alpr.db"))
    ap.add_argument("--source", default="flock",
                    help="tag stored on every row, e.g. flock / osm")
    a = ap.parse_args()

    src = Path(a.src)
    if not src.exists():
        print(f"no such file: {src}")
        return 1

    rows: dict[tuple[float, float], int] = {}
    with src.open(encoding="utf-8", errors="replace") as f:
        rd = csv.DictReader(f, delimiter="\t")
        if not rd.fieldnames or "lat" not in rd.fieldnames or "lon" not in rd.fieldnames:
            print(f"need a header with lat and lon; got {rd.fieldnames}")
            return 1
        bad = 0
        for r in rd:
            try:
                lat = round(float(r["lat"]), 5)
                lon = round(float(r["lon"]), 5)
            except (TypeError, ValueError):
                bad += 1
                continue
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                bad += 1
                continue
            rp = 1 if str(r.get("reads_plates", "")).strip() in ("1", "true", "True") else 0
            key = (lat, lon)
            rows[key] = rows.get(key, 0) or rp

    db = Path(a.db)
    db.parent.mkdir(parents=True, exist_ok=True)
    tmp = db.with_suffix(".db.new")
    if tmp.exists():
        tmp.unlink()
    conn = sqlite3.connect(str(tmp))
    conn.execute("PRAGMA journal_mode=OFF")
    conn.execute("CREATE TABLE cams (lat REAL, lon REAL, reads_plates INT, source TEXT)")
    conn.executemany(
        "INSERT INTO cams (lat, lon, reads_plates, source) VALUES (?,?,?,?)",
        [(la, lo, rp, a.source) for (la, lo), rp in rows.items()])
    # 🚨 THE INDEX IS THE WHOLE POINT: a per-route bounding-box query against
    # 160k rows has to be a range scan, not a table scan on the box that is
    # also serving the map.
    conn.execute("CREATE INDEX cams_lat ON cams (lat)")
    conn.execute("CREATE INDEX cams_lon ON cams (lon)")
    conn.commit()
    n = conn.execute("SELECT COUNT(*) FROM cams").fetchone()[0]
    reads = conn.execute("SELECT COUNT(*) FROM cams WHERE reads_plates=1").fetchone()[0]
    conn.close()

    # Swap into place only once it is fully built, so a crash mid-load never
    # leaves a half-written database that nav would read as "no cameras".
    tmp.replace(db)
    print(f"loaded {n:,} unique camera positions ({reads:,} read plates), "
          f"{bad} unusable rows skipped -> {db}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
