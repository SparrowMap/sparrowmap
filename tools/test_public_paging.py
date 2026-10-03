"""The public feed must page past its per-call ceiling, losing nothing.

    python tools\\test_public_paging.py

2026-10-02: the map sat at exactly 5000 public sightings with 5,626 in the
database (the same "stuck at 2000" bug one level up). recent_sightings now takes
a (ts, id) keyset cursor and the map pages until a short page. This builds
12,003 public rows in a SCRATCH db - with a timestamp TIE straddling the first
page boundary, the case a bare `ts < before` would silently drop - and checks
that paging returns every row exactly once.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

SCRATCH = Path(tempfile.mkdtemp(prefix="sparrow_paging_")).resolve()
os.environ["SPARROW_DATA"] = str(SCRATCH)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402

fails = 0


def check(name, ok, detail=""):
    global fails
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not ok else ""))
    fails += 0 if ok else 1


try:
    assert str(db.DB_PATH).startswith(str(SCRATCH))
    db.init()
    c = db.connect()
    N = 12003
    # Rows 4990..5010 (by age) all share one timestamp, so the 5000-row page
    # boundary falls inside a tie.
    rows = []
    for i in range(N):
        ts = 1_000_000.0 - i if not (4990 <= i <= 5010) else 1_000_000.0 - 4990
        rows.append(("n_t", ts, "public", "police", f"{i}.jpg"))
    c.executemany("INSERT INTO sightings(node_id, ts, tier, vclass, snap) VALUES (?,?,?,?,?)", rows)
    c.commit()

    first = db.recent_sightings(0, 5000, "public")
    check("one call is still capped at 5000", len(first) == 5000, str(len(first)))

    got = db.all_sightings(0, "public")
    ids = [r["id"] for r in got]
    check("paging returns every row", len(ids) == N, f"{len(ids)} of {N}")
    check("no row twice", len(set(ids)) == len(ids))

    # The same walk the browser does, through the cursor the API takes.
    seen, before = [], None
    while True:
        page = db.recent_sightings(0, 5000, "public", None, before)
        seen += [r["id"] for r in page]
        if len(page) < 5000:
            break
        before = (page[-1]["ts"], page[-1]["id"])
    check("cursor walk matches", sorted(seen) == sorted(ids), f"{len(seen)}")
finally:
    try:
        db.close_thread()
    except Exception:
        pass
    shutil.rmtree(SCRATCH, ignore_errors=True)

print(f"\n{'ALL PASS' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
