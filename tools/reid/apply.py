"""Write the links and markings that tools/reid/link.py decided, ON THE BOX.

    scp data/reid/tags.json root@BOX:/tmp/tags.json
    ssh root@BOX 'cd /opt/sparrowmap && sudo -u sparrow .venv/bin/python3 tools/reid/apply.py /tmp/tags.json'
    ... --clear-rev r2-markings      # forget everything one revision believed

Every write goes through db.tag_sighting / db.set_markings, which check the
STORED row's tier and class and refuse anything but a published police or
government sighting. This script never bypasses that: a decision file that
names a private row is reported and skipped, not applied. A file is the
output of one revision (tag_rev), so a better model can clear_tags(rev) and
re-apply without leaving two generations of guess in one column.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
import db          # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("file", nargs="?")
    ap.add_argument("--clear-rev", metavar="REV")
    args = ap.parse_args()

    if args.clear_rev:
        n = db.clear_tags(args.clear_rev)
        conn = db.connect()
        m = conn.execute("UPDATE sightings SET markings=NULL, markings_rev=NULL "
                         "WHERE markings_rev=?", (args.clear_rev,)).rowcount
        conn.commit()
        print(f"cleared {n} tags and {m} markings of rev {args.clear_rev}")
        if not args.file:
            return

    d = json.load(open(args.file))
    rev = d["rev"]
    tagged = marked = refused = stale = 0

    # Rows this run LOOKED AT and decided have no marking must lose whatever an
    # earlier revision believed. Without it, retiring a revision depends on
    # remembering to run --clear-rev for every superseded one, and forgetting
    # leaves a row asserting something the current rules would not assert.
    # Measured 2026-09-23: 28 rows still read STATE POLICE at rev r4 after r6
    # had already corrected them, because r6 gives those crops no marking at
    # all and so never names them.
    considered = d.get("considered")
    if considered:
        keep = {int(m["id"]) for m in d.get("markings", [])}
        drop = [i for i in (int(x) for x in considered) if i not in keep]
        conn = db.connect()
        for chunk in (drop[k:k + 400] for k in range(0, len(drop), 400)):
            stale += conn.execute(
                "UPDATE sightings SET markings=NULL, markings_rev=NULL "
                f"WHERE markings IS NOT NULL AND markings_rev IS NOT ? "
                f"AND id IN ({','.join('?' * len(chunk))})",
                (rev, *chunk)).rowcount
        conn.commit()
    for m in d.get("markings", []):
        try:
            db.set_markings(int(m["id"]), m["text"], rev)
            marked += 1
        except db.NotTaggable as e:
            refused += 1
            print("  refused:", e)
    for t in d.get("tags", []):
        try:
            db.tag_sighting(int(t["id"]), t["tag"], float(t["conf"]), t["why"], rev)
            tagged += 1
        except (db.NotTaggable, ValueError) as e:
            refused += 1
            print("  refused:", e)
    print(f"rev {rev}: {marked} markings, {stale} stale cleared, {tagged} tags written, {refused} refused")


if __name__ == "__main__":
    main()
