"""Draw "these sightings may be the same vehicle" links across published police
sightings, and say why in words.

    python tools/link_vehicles.py                 # dry run, prints what it would link
    python tools/link_vehicles.py --apply
    python tools/link_vehicles.py --clear r1-plate

🚨 THIS IS TRACKING, AND THAT IS WHY IT IS FENCED THE WAY IT IS.

Following one vehicle across cameras over time is precisely the capability
SparrowMap argues against having, and destroys civilian plates at the edge to
avoid. It is defensible for one class of vehicle and one only: a marked patrol
car on a public road, publicly funded, whose plate this system already keeps ON
PURPOSE. Pointed at anyone else it would make every claim on /transparency
untrue.

So the refusal is not in this file. db.tag_sighting checks the STORED row -
tier and class - at the moment of writing, and raises rather than link a
private vehicle. This tool cannot override that and should not try.

⚠️ IT IS A GUESS AND HAS TO READ LIKE ONE.
Nothing here concludes "this is unit 4021". It concludes "these two sightings
are probably the same vehicle, because <reason>", and the reason is stored next
to the link so a reader can disagree with it. A link nobody can check is an
assertion with extra steps.

⏭️ IT IS BUILT TO BE REPLACED.
Today the only evidence good enough is a plate read, which is nearly certain and
nearly useless - it needs a legible plate, and the whole reason the resolution
work exists is that most catches do not have one. The ladder below is written so
a better model drops in as a new rung with its own revision, and
`--clear <rev>` throws away everything the old one believed. The tag is this
version's opinion, never a fact.
"""
from __future__ import annotations

import argparse
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db                                   # noqa: E402

# 🚨 A LINK IS ONLY WORTH DRAWING INSIDE A WINDOW YOU CAN DEFEND.
#
# The same plate a year apart is the same car and says nothing anyone needs.
# The same plate twenty minutes apart on two roads is the observation - a
# patrol moving through an area. Beyond a few days a "link" stops being an
# event and becomes a dossier, which is a different product and not this one.
WINDOW_S = 3 * 86400

# Faster than this between two sightings and one of them is wrong: a vehicle
# that appears 90 miles away in four minutes was not the same vehicle, it was a
# misread plate or a bad camera position. Refusing those is most of what makes
# the rest believable.
MAX_MPH = 100.0


def _miles(a, b) -> float:
    """Great-circle miles between two (lat, lon) pairs."""
    import math
    if None in a or None in b:
        return 0.0
    r = 3958.8
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp = p2 - p1
    dl = math.radians(b[1] - a[1])
    h = (math.sin(dp / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2)
    return 2 * r * math.asin(min(1.0, math.sqrt(h)))


def implausible(a: dict, b: dict) -> str:
    """"" if the pair could be one vehicle, else why it could not."""
    dt = abs((a["ts"] or 0) - (b["ts"] or 0))
    if dt <= 0:
        return ""
    d = _miles((a.get("lat"), a.get("lon")), (b.get("lat"), b.get("lon")))
    if d <= 0:
        return ""
    mph = d / (dt / 3600.0)
    if mph > MAX_MPH:
        return f"{d:.0f} miles in {dt / 60:.0f} min ({mph:.0f} mph)"
    return ""


def by_plate(rows: list) -> dict:
    """Rung 1: the same plate text, close enough in time to mean something.

    Nearly certain and nearly useless, and both halves matter. A plate match is
    the strongest evidence available and needs a LEGIBLE plate, which most
    catches do not have - that is the whole reason the resolution work exists.
    It is here because it is checkable, and because it gives the shape
    something better can be dropped into.
    """
    groups = defaultdict(list)
    for r in rows:
        p = (r.get("plate_text") or "").strip().upper()
        if len(p) >= 4:
            groups[p].append(r)
    return {p: g for p, g in groups.items() if len(g) > 1}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--days", type=float, default=WINDOW_S / 86400)
    ap.add_argument("--clear", metavar="REV",
                    help="forget every link drawn by a revision, or ALL")
    a = ap.parse_args()

    if a.clear:
        if not a.apply:
            print("--clear needs --apply; it deletes links")
            return 1
        n = db.clear_tags(None if a.clear.upper() == "ALL" else a.clear)
        print(f"cleared {n} link(s)")
        return 0

    since = time.time() - a.days * 86400
    # all_sightings + vclass="public": recent_sightings silently capped this at
    # 5000 rows of ALL traffic, i.e. the last ~15 minutes, not `a.days`.
    rows = [r for r in db.all_sightings(since=since, vclass="public")
            if r.get("tier") == "public"
            and (r.get("vclass") or "") in db.TAGGABLE]
    print(f"{len(rows)} published police/government sighting(s) in "
          f"{a.days:.0f} days")

    made = skipped = 0
    for plate, group in sorted(by_plate(rows).items()):
        group.sort(key=lambda r: r["ts"])
        # ⚠️ CHECK THE WHOLE CHAIN, NOT JUST NEIGHBOURS. One bad member makes
        # every link through it wrong, and a group is a claim about all of its
        # members at once.
        bad = ""
        for i in range(len(group) - 1):
            bad = implausible(group[i], group[i + 1])
            if bad:
                break
        if bad:
            skipped += 1
            print(f"  skip  {plate:<10} {len(group)} sightings - {bad}")
            continue
        tag = "v_" + plate.lower().replace(" ", "")
        span = (group[-1]["ts"] - group[0]["ts"]) / 3600.0
        why = (f"same plate read ({plate}) on {len(group)} sightings across "
               f"{span:.1f}h; no impossible travel between them")
        print(f"  link  {plate:<10} {len(group)} sightings over {span:.1f}h")
        if a.apply:
            for r in group:
                try:
                    db.tag_sighting(r["id"], tag, 0.9, why)
                    made += 1
                except db.NotTaggable as exc:
                    # The database refused. That is the guard working, and it
                    # is worth seeing rather than swallowing.
                    print(f"    refused: {exc}")

    print(f"\n{made} sighting(s) linked, {skipped} group(s) refused as "
          f"impossible travel")
    if not a.apply:
        print("DRY RUN - nothing written. Re-run with --apply.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
