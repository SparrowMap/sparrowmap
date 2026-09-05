"""Stage 3F3 characterization: ingest_persistence's injectable store seam.

Proves persist_vehicle_sighting() (and its helpers) use a supplied `store`
dependency instead of the global `db` module, and that the call ordering on
both the merge path and the insert path is unchanged from today's behavior.

This does not build a database fake - it records calls and returns the
smallest data needed to drive control flow.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ingest_persistence  # noqa: E402

FAILURES = []


def check(label: str, cond: bool, detail: str = "") -> None:
    status = "ok" if cond else "FAIL"
    print(f"  [{status}] {label}" + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(label)


class FakeFeed:
    def __init__(self):
        self.published = []

    def publish(self, record):
        self.published.append(record)


class FakeStore:
    """Records calls; returns just enough to drive merge-vs-insert branching."""

    def __init__(self, merge_row=None, insert_id=999):
        self._merge_row = merge_row
        self._insert_id = insert_id
        self.calls = []

    def merge_window_row(self, node_id, vclass, ts):
        self.calls.append(("merge_window_row", node_id, vclass, ts))
        return self._merge_row

    def bump_detections(self, sid, ts, conf):
        self.calls.append(("bump_detections", sid, ts, conf))

    def heartbeat(self, node_id):
        self.calls.append(("heartbeat", node_id))

    def insert_sighting(self, record):
        self.calls.append(("insert_sighting", dict(record)))
        return self._insert_id


def _classification():
    return {"vclass": "gov", "conf": 0.91}


def test_merge_path() -> None:
    print("\n[1] merge path: fake store observes merge_window_row -> bump_detections -> heartbeat")
    prior_row = {"id": 4242}
    store = FakeStore(merge_row=prior_row)
    feed = FakeFeed()
    result = ingest_persistence.persist_vehicle_sighting(
        record={"id": None}, event={}, node={"name": "cam1"}, node_id="n1",
        classification=_classification(), timestamp=100.0, candidate=True,
        latitude=1.0, longitude=2.0, banked_stem=None, relay_crop=None,
        review_crop=None, evidence_crop=None, feed=feed, store=store,
    )
    names = [c[0] for c in store.calls]
    check("merge_window_row called", "merge_window_row" in names)
    check("call order is merge_window_row, bump_detections, heartbeat",
          names == ["merge_window_row", "bump_detections", "heartbeat"],
          f"got {names}")
    check("no insert_sighting call occurred on the merge path",
          "insert_sighting" not in names)
    check("no feed publication occurred on the merge path", feed.published == [])
    check("merge result reports merged_into with the prior row id",
          result.merged_into == 4242 and result.record is None,
          f"got {result}")


def test_insert_path() -> None:
    print("\n[2] insert path: fake store observes insert_sighting ... heartbeat")
    store = FakeStore(merge_row=None, insert_id=777)
    feed = FakeFeed()
    record = {"id": None, "node_id": "n1", "vclass": "gov"}
    result = ingest_persistence.persist_vehicle_sighting(
        record=record, event={}, node={"name": "cam1"}, node_id="n1",
        classification=_classification(), timestamp=100.0, candidate=True,
        latitude=1.0, longitude=2.0, banked_stem=None, relay_crop=None,
        review_crop=None, evidence_crop=None, feed=feed, store=store,
    )
    names = [c[0] for c in store.calls]
    check("merge_window_row was still consulted first (candidate=True)",
          names[0] == "merge_window_row")
    check("insert_sighting occurred", "insert_sighting" in names)
    check("heartbeat occurred after insert_sighting on the insert path",
          names.index("heartbeat") > names.index("insert_sighting"),
          f"got {names}")
    check("no bump_detections call occurred on the insert path",
          "bump_detections" not in names)
    check("insert result carries the store-assigned id and no merged_into",
          result.record is not None and result.record["id"] == 777
          and result.merged_into is None,
          f"got {result}")
    check("feed.publish was called exactly once with the persisted record",
          feed.published == [result.record], f"got {feed.published}")


def test_default_store_is_db_module() -> None:
    print("\n[3] no explicit store supplied -> default falls back to the real db module")
    import db
    check("find_vehicle_fragment's default store resolves to the db module",
          ingest_persistence.find_vehicle_fragment.__defaults__[-1] is None,
          "default parameter value should be None, resolved internally to db")
    # Confirm the resolution happens by patching db.merge_window_row and
    # calling with no store argument at all.
    calls = []
    orig = db.merge_window_row

    def spy(node_id, vclass, ts):
        calls.append((node_id, vclass, ts))
        return None
    db.merge_window_row = spy
    try:
        ingest_persistence.find_vehicle_fragment("n9", _classification(), 5.0, True)
    finally:
        db.merge_window_row = orig
    check("global db.merge_window_row was used when no store was passed",
          calls == [("n9", "gov", 5.0)], f"got {calls}")


def main() -> int:
    test_merge_path()
    test_insert_path()
    test_default_store_is_db_module()
    total = 13
    print(f"\n{total - len(FAILURES)}/{total} checks passed"
          if not FAILURES else f"\n{len(FAILURES)} FAILURES: {FAILURES}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
