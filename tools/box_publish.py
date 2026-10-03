"""Handle head-scored contributor crops on the public mirror.

Runs ON THE BOX, driven over SSH by detect/box_puller.py (which has the GPU and
the trained head) and by tools/box_review.py (the human gate). Three actions,
given as JSON on stdin:

    {"review":  [{"id": 31382, "head_conf": 0.98, "vclass": "police",
                  "node_name": "...", "why": "..."}, ...],
     "discard": [31383, 31384, ...],
     "publish": [{"id": 31382, "vclass": "police", "conf": 0.98,
                  "why": "..."}, ...]}

  review  - a head-positive contribution. The trained head cannot tell a marked
            patrol car from an unmarked black SUV (both read "government"), and
            publishing the second as police is the exact harm this project
            exists to prevent. So a head-positive is NOT published: its crop is
            moved from the inbox into data/review/ and the row is left private
            (an anonymous traffic dot, no government claim) until a human looks.
  discard - the head said ordinary. Delete the crop from the inbox and mark the
            row `auto_discarded` (a model's call - see discard_one).
  publish - a human (box_review.py) confirmed a reviewed crop is a real,
            clearly-identifiable government vehicle. Promote the row to the
            public tier, attach the crop as its photo, and clear the review pen.

The mirror grows no HTTP surface for any of this - it is all driven over the
existing SSH admin channel, so a mirror breach still yields only the public
record.
"""

from __future__ import annotations

import base64
import json
import secrets
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db                       # noqa: E402
import snapshot                 # noqa: E402
from core import SNAPS          # noqa: E402
from mirror import INBOX        # noqa: E402

REVIEW = INBOX.parent / "review"


def _inbox_paths(sid: int) -> tuple[Path, Path]:
    return INBOX / f"{sid}.jpg", INBOX / f"{sid}.json"


def _review_paths(sid: int) -> tuple[Path, Path]:
    return REVIEW / f"{sid}.jpg", REVIEW / f"{sid}.json"


def _delete(*paths: Path) -> None:
    for p in paths:
        p.unlink(missing_ok=True)


def _attach_snap(jpg: bytes, meta: dict) -> str | None:
    """Store a crop as a public snapshot and return its name.

    Prefer the canonical stamped snapshot; if that path refuses for any reason
    (e.g. background stripping cannot isolate the vehicle in a whole-frame
    crop), fall back to writing the already-safe bytes directly. The crop was
    verified plate-illegible when it was parked, so a missing stamp exposes
    nothing - never let a stamp failure drop a confirmed government sighting off
    the map.
    """
    try:
        data_url = "data:image/jpeg;base64," + base64.b64encode(jpg).decode()
        return snapshot.store_subresolution(data_url, meta)
    except Exception as exc:                                  # noqa: BLE001
        try:
            ts = int(meta.get("ts") or time.time())
            name = f"{ts}_{secrets.token_hex(4)}.jpg"
            SNAPS.mkdir(parents=True, exist_ok=True)
            (SNAPS / name).write_bytes(jpg)
            print(f"  snap fallback for stamp failure: {exc}", file=sys.stderr)
            return name
        except Exception as exc2:                             # noqa: BLE001
            print(f"  snap FAILED: {exc2}", file=sys.stderr)
            return None


def review_one(item: dict) -> None:
    """Move a head-positive crop into the review pen, row stays private."""
    sid = int(item["id"])
    REVIEW.mkdir(parents=True, exist_ok=True)
    ij, im = _inbox_paths(sid)
    rj, rm = _review_paths(sid)
    meta = {}
    if im.exists():
        try:
            meta = json.loads(im.read_text(encoding="utf-8"))
        except Exception:
            meta = {}
    # `threshold` makes the item self-describing: a score means nothing without
    # the operating point it was judged against, and this box cannot load the
    # head to look it up. review_api.queue() needs both to hide an item the
    # head already rejected.
    meta["head"] = {"conf": item.get("head_conf"), "vclass": item.get("vclass"),
                    "threshold": item.get("head_threshold"),
                    "near_miss": bool(item.get("near_miss")),
                    "why": item.get("why"), "at": time.time()}
    # The crop the reviewer sees. Prefer the box's own inbox copy; fall back to
    # the crop the puller carried in the verdict. mirror._prune_inbox deletes
    # inbox crops after 12h, so a delayed verdict can arrive to find the inbox
    # copy already gone - and a pen entry with no crop is a black hole the
    # reviewer can never act on (28 such cards is what surfaced this bug).
    crop = None
    if ij.exists():
        crop = ij.read_bytes()
    elif item.get("crop_b64"):
        try:
            crop = base64.b64decode(item["crop_b64"])
        except Exception:
            crop = None
    if not crop:
        # No image from either source: do NOT park a crop-less card. Leave the
        # row private and drop any stale inbox remnant; it re-pulls next cycle
        # while a crop still exists. Silence here beats a permanent black hole.
        _delete(ij, im)
        print(f"  review {sid}: no crop (inbox pruned, none carried) - skipped",
              file=sys.stderr)
        return
    rj.write_bytes(crop)
    rm.write_text(json.dumps(meta, indent=1), encoding="utf-8")
    _delete(ij, im)


def publish_one(item: dict) -> None:
    """Promote a human-confirmed row onto the public map with its photo."""
    sid = int(item["id"])
    vclass = item.get("vclass") or "gov"
    conf = item.get("conf")
    why = item.get("why") or "confirmed by review as a government vehicle"

    crop = None
    meta = {}
    for jp, mp in (_review_paths(sid), _inbox_paths(sid)):
        if jp.exists():
            crop = jp.read_bytes()
            if mp.exists():
                try:
                    meta = json.loads(mp.read_text(encoding="utf-8"))
                except Exception:
                    meta = {}
            break

    snap = None
    if crop:
        snap = _attach_snap(crop, {
            "ts": meta.get("ts") or time.time(), "node_id": "box-relay",
            "node_name": meta.get("node_name") or "a phone contributor",
            "tier": "public", "vclass": vclass, "watermark": "CONTRIBUTED"})

    # Who decided, recorded rather than inferred. box_review.py (a human ruling
    # in the reviewer UI) sends 'human'; anything automated must say 'auto' and
    # is counted as such on the public policy endpoint.
    db.promote_sighting(sid, vclass, conf, why,
                        decided_by=item.get("decided_by") or "human")
    if snap:
        conn = db.connect()
        conn.execute("UPDATE sightings SET snap=? WHERE id=?", (snap, sid))
        conn.commit()
    # Same reason as reject_one: sync_review_labels reads the AUDIT table, so a
    # confirmation with no audit row never becomes a training label. The web
    # path has always written one; this path did not, so every CLI verdict -
    # both directions - was invisible to training.
    db.audit("review:confirm", str(sid), actor="box_review", ip="")
    _delete(*_review_paths(sid), *_inbox_paths(sid))


def reject_one(sid: int) -> None:
    """A human said a reviewed crop is not a government vehicle.

    🚨 "LEAVE THE ROW PRIVATE" WAS NOT ENOUGH, AND THE WEB PATH ALREADY KNEW IT.
    This only unlinked the files. review_api.verdict('not') does two more things
    for exactly this case, and they are not decoration:

      * db.review_sighting(sid, 'retracted') - without it the row keeps
        `reviewed IS NULL` AND keeps `vclass='police'`, so a vehicle a human
        has just rejected is still returned by /api/sightings?vclass=police
        until retention deletes it, and backfill_pen re-queues it on the next
        sweep so the reviewer is asked again about a car they already ruled on.
      * db.audit(...) - tools/sync_review_labels.py harvests training labels
        FROM THE AUDIT TABLE, so a verdict cast through the CLI never became a
        training label at all. Every rejection made here was invisible to the
        thing that learns from rejections.

    Two reviewer paths that disagree about what a verdict means is worse than
    one path with a bug, because the disagreement is invisible from either side.
    """
    try:
        db.review_sighting(sid, "retracted")
    except Exception as exc:                                  # noqa: BLE001
        # Never let the bookkeeping stop the crop being removed - but say so,
        # because a silent failure here is what put the row back in the queue.
        print(f"warning: could not record the retraction for {sid}: {exc}",
              file=sys.stderr)
    # `box_review_cli`, not `box_review`: that name is on 13.8M AUTOMATIC
    # discards from before discard_one existed, so sync_review_labels ignores it.
    db.audit("review:reject", str(sid), actor="box_review_cli", ip="")
    _delete(*_review_paths(sid), *_inbox_paths(sid))


def discard_one(sid: int) -> None:
    """The trained head said ordinary. A MODEL's verdict, never a person's.

    🚨 THIS USED TO BE reject_one, AND THAT LIED ABOUT WHO DECIDED.
    box_puller's `discard` list was routed through reject_one ("A human said a
    reviewed crop is not a government vehicle"), so every automatic discard:
      * was audited as `review:reject` by `box_review` - 13,795,024 of them by
        2026-09-29, ~350k/day, outnumbering every real reviewer combined;
      * was stamped "retracted by the camera operator: not a public-tier
        vehicle" on a row no operator ever saw;
      * fed tools/sync_review_labels.py, which turns `review:reject` into a
        human `civilian` training label - the head's own misses coming back as
        ground truth.
    Found when a contributor reported "verified police rejected" and the audit
    pointed at a reviewer who did not exist.

    `reviewed='auto_discarded'` keeps the row out of backfill_pen and the
    possibly-missed queue exactly as before, leaves vclass/why as the hub set
    them, and writes NO audit row: the row itself records the decision, and an
    audit line per passing car is how the table reached 13.8M.
    """
    try:
        db.review_sighting(sid, "auto_discarded")
    except Exception as exc:                                  # noqa: BLE001
        print(f"warning: could not record the discard for {sid}: {exc}",
              file=sys.stderr)
    _delete(*_review_paths(sid), *_inbox_paths(sid))


def main() -> None:
    raw = sys.stdin.read()
    try:
        req = json.loads(raw) if raw.strip() else {}
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"bad json: {exc}"}))
        sys.exit(1)

    counts = {"reviewed": 0, "published": 0, "discarded": 0, "rejected": 0}
    errors = []

    def run(items, fn, key, id_of):
        for it in items:
            try:
                fn(it)
                counts[key] += 1
            except Exception as exc:                          # noqa: BLE001
                errors.append({"id": id_of(it), "error": str(exc)})

    run(req.get("review", []),  review_one,  "reviewed",  lambda it: it.get("id"))
    run(req.get("publish", []), publish_one, "published", lambda it: it.get("id"))
    run(req.get("discard", []), lambda sid: discard_one(int(sid)), "discarded", lambda x: x)
    run(req.get("reject", []),  lambda sid: reject_one(int(sid)), "rejected",  lambda x: x)

    print(json.dumps({"ok": not errors, **counts, "errors": errors}))


if __name__ == "__main__":
    main()
