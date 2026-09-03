"""Re-publish already-published photos from their banked originals.

🚨 WHY THESE PHOTOS NEED FIXING AT ALL.

Until 2026-09-02 `snapshot.store_crop` handed ONE rectangle to two functions
that ask different questions: `isolate.strip` uses it to pick WHICH INSTANCE is
the subject (an 18% inset, correct) and the geometric fallback uses it to decide
WHAT SURVIVES (an 18% inset, destructive). The box has no cv2 on purpose, so the
fallback is the only path a published photo ever takes - and every one came out
as the middle 41% of itself with a flat backdrop painted round the rest.

The blanked top band is exactly where a roof light bar sits: CROP_PAD_TOP is
0.28 and asymmetric precisely so the crop reaches above the roof to catch it.
Measured on a real published patrol SUV, the old path removed the light bar, the
front of the vehicle and half the door livery, leaving an anonymous white car.
His report: "sometimes makes it hard to tell it was even a cop sighting".

The fix only helps photos published AFTER it. The pixels of the older ones were
destroyed at store time and there is no undo - EXCEPT where the crop that was
published from still exists in the training bank, keyed by `sighting_id`. This
re-publishes those through the corrected path.

    # on the box, after tools/recrop_prepare.py has shipped the crops
    python tools/recrop_published.py --dir /tmp/recrop            # dry run
    python tools/recrop_published.py --dir /tmp/recrop --apply

⚠️ THE OLD FILE IS DELETED BY `attach_confirmed_photo`, ON PURPOSE - an unlinked
snapshot that stays on disk is still one URL away, which is a leak this codebase
has shipped three times. So the old file is COPIED ASIDE FIRST, into
`data/snaps_pre_recrop/`, before anything replaces it. That directory is served
by no route; it exists so a bad batch can be undone by hand rather than mourned.

⚠️ ONLY `tier='public'` ROWS. The public tier is the one where a government
vehicle's plate is meant to survive, and every row here has already been vouched
for by a human. A private-tier row must never be re-published from a bank crop -
the bank holds the crop as it was BEFORE redaction decisions were applied.

📌 SOME BANK CROPS ARE BIGGER THAN WHAT WAS PUBLISHED, and that is not a
mistake to correct. 200px is `SUBRES_MAX_EDGE`, the size chosen because it
destroys a plate - right for a candidate nobody has looked at, wrong for a
confirmed patrol car on the public tier, which is the whole argument of
`sparrow_fullres_on_confirm`. `store_crop` still caps at MAX_EDGE.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import db                                                  # noqa: E402
import review_api                                          # noqa: E402
from core import SNAPS                                     # noqa: E402

BACKUP = ROOT / "data" / "snaps_pre_recrop"


def _size(p: Path):
    try:
        from PIL import Image
        with Image.open(p) as im:
            return im.size
    except Exception:
        return None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dir", required=True,
                    help="directory of <sighting_id>.jpg banked originals")
    ap.add_argument("--apply", action="store_true",
                    help="actually re-publish (default is a dry run)")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    src = Path(args.dir)
    crops = sorted(src.glob("*.jpg"))
    if args.limit:
        crops = crops[:args.limit]
    print(f"{len(crops)} banked crops offered\n")

    conn = db.connect()
    done = skipped = failed = bigger = 0
    for p in crops:
        try:
            sid = int(p.stem)
        except ValueError:
            continue
        row = conn.execute(
            "SELECT id, snap, snap_held, tier, vclass, ts FROM sightings "
            "WHERE id=?", (sid,)).fetchone()
        if not row:
            print(f"  {sid}: no such sighting - skipped")
            skipped += 1
            continue
        row = dict(row)
        if row["tier"] != "public":
            print(f"  {sid}: tier={row['tier']} - REFUSED (public tier only)")
            skipped += 1
            continue
        if not row["snap"]:
            print(f"  {sid}: no current photo - skipped")
            skipped += 1
            continue

        cur = SNAPS / Path(str(row["snap"])).name
        was, now = _size(cur), _size(p)
        if now is None:
            print(f"  {sid}: banked crop unreadable - skipped")
            skipped += 1
            continue
        if now[0] * now[1] > (was[0] * was[1] if was else 0):
            bigger += 1
        note = f"{was[0]}x{was[1]}" if was else "missing"
        if not args.apply:
            print(f"  {sid}: published {note} -> would re-publish from "
                  f"{now[0]}x{now[1]} banked original")
            done += 1
            continue

        # 🚨 COPY THE OLD FILE ASIDE BEFORE ANYTHING REPLACES IT.
        if cur.exists():
            BACKUP.mkdir(parents=True, exist_ok=True)
            shutil.copy2(cur, BACKUP / cur.name)
        new = review_api.attach_confirmed_photo(
            sid, row, p.read_bytes(),
            ts=row["ts"], node_name="recrop",
            vclass=row["vclass"] or "police",
            # The banked crop is the pre-publish copy: never stamped, never
            # background-stripped. So it needs both, exactly like a fresh
            # confirmation - this is not the already-processed `reported` path.
            stamp=True, isolate=True)
        if new:
            after = _size(SNAPS / new)
            print(f"  {sid}: {note} -> {after[0]}x{after[1]}  ({new})")
            done += 1
        else:
            print(f"  {sid}: FAILED to attach - photo left as it was")
            failed += 1

    print(f"\n{'re-published' if args.apply else 'would re-publish'}: {done}"
          f"   skipped: {skipped}   failed: {failed}")
    print(f"{bigger} of them gain resolution as well as framing.")
    if args.apply:
        print(f"previous photos kept in {BACKUP}")
    else:
        print("\n[--apply to do it]")


if __name__ == "__main__":
    main()
