"""Prove the 2026-09-29 fixes fire on their real triggers.

    python tools\\test_discard_honesty.py

Three faults, found when a contributor's AXIS cameras reported "verified police
rejected" (see box_publish.discard_one, box_puller.NEAR_MISS_FLOOR and
mirror._CONTRIBUTOR_RESERVE):

  1. the head's automatic discard was written as a HUMAN rejection;
  2. a crop scored just under the head's bar went to the bin, never a person;
  3. the traffic fleet filled the inbox and a contributor's crops were dropped.

Runs in a SCRATCH data directory (SPARROW_DATA is read at import time, so it is
set before anything from the repo is imported). Touches no live data.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

SCRATCH = Path(tempfile.mkdtemp(prefix="sparrow_discard_")).resolve()
os.environ["SPARROW_DATA"] = str(SCRATCH)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import db           # noqa: E402
import mirror       # noqa: E402
import review_api   # noqa: E402
import box_publish  # noqa: E402

fails = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global fails
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not ok else ""))
    fails += 0 if ok else 1


def new_row(why: str = "no distinguishing signals; treated as private") -> int:
    c = db.connect()
    cur = c.execute("INSERT INTO sightings(node_id, ts, tier, vclass, vclass_why, source) "
                    "VALUES ('n_test', 1, 'private', 'civilian', ?, 'phone_node')", (why,))
    c.commit()
    return cur.lastrowid


def audits(sid: int) -> list:
    return db.connect().execute("SELECT action, actor FROM audit WHERE target=?",
                                (str(sid),)).fetchall()


try:
    assert str(db.DB_PATH).startswith(str(SCRATCH)), f"not scratch: {db.DB_PATH}"
    db.init()

    # ---- 1. an automatic discard is not a human verdict --------------------
    sid = new_row()
    box_publish.discard_one(sid)
    r = db.connect().execute("SELECT reviewed, vclass_why FROM sightings WHERE id=?",
                             (sid,)).fetchone()
    check("discard marks row auto_discarded", r[0] == "auto_discarded", str(r[0]))
    check("discard keeps the hub's reason", "retracted" not in (r[1] or ""), r[1])
    check("discard writes no review:reject audit", not audits(sid), str(audits(sid)))

    # The box_puller path itself: a `discard` list on stdin.
    sid2 = new_row()
    sys.stdin = __import__("io").StringIO(json.dumps({"discard": [sid2]}))
    box_publish.main()
    r2 = db.connect().execute("SELECT reviewed FROM sightings WHERE id=?", (sid2,)).fetchone()
    check("stdin discard list routes to discard_one", r2[0] == "auto_discarded", str(r2[0]))

    # A human's CLI reject still retracts, under the new actor name.
    sid3 = new_row()
    box_publish.reject_one(sid3)
    r3 = db.connect().execute("SELECT reviewed FROM sightings WHERE id=?", (sid3,)).fetchone()
    check("human reject still retracts", r3[0] == "retracted", str(r3[0]))
    check("human reject audited as box_review_cli",
          ("review:reject", "box_review_cli") in [tuple(a) for a in audits(sid3)], str(audits(sid3)))

    # ---- 2. near miss reaches the main queue -------------------------------
    nm = {"head": {"conf": 0.877, "threshold": 0.97915, "near_miss": True}}
    low = {"head": {"conf": 0.30, "threshold": 0.97915}}
    check("near miss is not head_rejected", review_api.head_rejected(nm) is False)
    check("ordinary low score still hidden", review_api.head_rejected(low) is True)

    from detect import box_puller
    from detect.vehicle_id import VehicleIdentifier
    import numpy as np
    import cv2

    inbox = SCRATCH / "fake_inbox"
    inbox.mkdir()
    img = np.full((120, 200, 3), 128, np.uint8)
    for s, head in ((901, 0.877), (902, 0.30), (903, 0.99)):
        cv2.imwrite(str(inbox / f"{s}.jpg"), img)
        (inbox / f"{s}.json").write_text(json.dumps({"sighting_id": s, "node_name": "t"}))
    scores = {901: 0.877, 902: 0.30, 903: 0.99}
    order = iter(sorted(scores))

    class StubVid:
        def classify(self, _img):
            s = next(order)
            return {"vclass": "police", "conf": 0.9, "margin": 0.5, "scores": {}, "_s": scores[s]}

    real = VehicleIdentifier.gov_call
    VehicleIdentifier.gov_call = staticmethod(lambda r: {
        "source": "head", "gov": r["_s"] >= 0.97915, "conf": r["_s"], "threshold": 0.97915})
    captured = {}
    box_puller.apply_verdict = lambda a, p, rv, d, s, l: captured.update(review=rv, discard=d) or {}
    box_puller._seen.clear()
    try:
        box_puller._run_once(StubVid(), SimpleNamespace(limit=0, dry_run=False), inbox, True)
    finally:
        VehicleIdentifier.gov_call = real
    rv = {it["id"]: it for it in captured.get("review", [])}
    check("0.877 goes to review as near miss", 901 in rv and rv[901].get("near_miss") is True, str(rv.keys()))
    check("0.99 goes to review, not near miss", 903 in rv and not rv[903].get("near_miss"))
    check("0.30 is discarded", 902 in captured.get("discard", []), str(captured.get("discard")))

    # ---- 2b. a contributor's crop is scored and sent BEFORE the fleet's ----
    inbox2 = SCRATCH / "fake_inbox2"
    inbox2.mkdir()
    for s, contrib in ((951, False), (952, False), (953, True), (954, False)):
        cv2.imwrite(str(inbox2 / f"{s}.jpg"), img)
        (inbox2 / f"{s}.json").write_text(json.dumps(
            {"sighting_id": s, "node_name": "t", "contributor": contrib}))
    seen_order, calls = [], []

    class OrderVid:
        def classify(self, _img):
            return {"vclass": "civilian", "conf": 0.9, "margin": 0.5, "scores": {}, "_s": 0.1}

    VehicleIdentifier.gov_call = staticmethod(lambda r: {
        "source": "head", "gov": False, "conf": r["_s"], "threshold": 0.97915})
    box_puller.apply_verdict = lambda a, p, rv, d, s, l: calls.append(list(d)) or {"discarded": len(d)}
    box_puller._seen.clear()
    try:
        out = box_puller._run_once(OrderVid(), SimpleNamespace(limit=0, dry_run=False), inbox2, True)
    finally:
        VehicleIdentifier.gov_call = real
    check("contributor verdict sent first, alone", calls[:1] == [[953]], str(calls))
    check("fleet verdict sent after", len(calls) == 2 and sorted(calls[1]) == [951, 952, 954], str(calls))
    check("cycle totals still count everything", out.get("discarded") == 4, str(out))

    # ---- 3. a contributor is not crowded out by the fleet ------------------
    mirror._last_prune = 1e18          # keep the prune from recounting
    mirror._inbox_count = mirror._INBOX_MAX_FILES
    check("fleet crop refused at the bound",
          mirror.quarantine_write(1001, b"x", {}, contributor=False) is None)
    check("contributor crop parked at the bound",
          mirror.quarantine_write(1002, b"x", {}, contributor=True) == "1002")
    mirror._inbox_count = mirror._INBOX_MAX_FILES + mirror._CONTRIBUTOR_RESERVE
    check("contributor bounded by the reserve",
          mirror.quarantine_write(1003, b"x", {}, contributor=True) is None)
finally:
    try:
        db.close_thread()
    except Exception:
        pass
    shutil.rmtree(SCRATCH, ignore_errors=True)

print(f"\n{'ALL PASS' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
