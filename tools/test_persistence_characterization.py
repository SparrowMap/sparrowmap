"""Stage 3F1 characterization of db.py persistence semantics.

This suite exercises the REAL SQLite implementation (no mocking of SQLite
itself) against an isolated scratch database, so that the operation
boundaries, merge-window edge behavior, and safety properties documented in
the Stage 3F persistence-boundary analysis are locked down BEFORE any
persistence seam/adapter is introduced.

Scope is deliberately narrow: this file characterizes db.py functions
directly. It does not start an HTTP server, does not touch developer
`data/`, and does not modify production code. Where a property (single-commit
atomicity of two statements sharing one `conn.commit()`) cannot be safely
fault-injected without fragile monkeypatching of sqlite3 internals, this file
documents that property as CODE-REVIEW-ONLY rather than inventing brittle
test infrastructure for one implementation detail.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SCRATCH = Path(tempfile.mkdtemp(prefix="ravenmap_persistence_"))

import core  # noqa: E402
import db  # noqa: E402

REAL_DB = Path(db.DB_PATH)
for _mod, _name, _val in [
    (core, "DATA", SCRATCH),
    (core, "DB_PATH", SCRATCH / "sparrow.db"),
    (db, "DB_PATH", SCRATCH / "sparrow.db"),
]:
    setattr(_mod, _name, _val)

FAILURES: list[str] = []
CHECKS = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global CHECKS
    CHECKS += 1
    if condition:
        print(f"  [ok] {name}")
    else:
        FAILURES.append(name)
        print(f"  [FAIL] {name}: {detail}")


def add_node(node_id: str, *, lat: float = 41.1, lon: float = -82.2) -> None:
    db.upsert_node({
        "id": node_id, "name": node_id, "lat": lat, "lon": lon,
        "pub_lat": lat + 0.01, "pub_lon": lon - 0.01, "kind": "fixed",
        "status": "active",
    })


def base_record(node_id: str, ts: float, *, vclass: str = "civilian",
                 tier: str = "private", vclass_conf: float | None = None) -> dict:
    return {
        "node_id": node_id, "ts": ts, "lat": 41.1, "lon": -82.2,
        "tier": tier, "vclass": vclass, "vclass_conf": vclass_conf,
        "source": "synthetic",
    }


# ---------------------------------------------------------------------------
# 1. insert_sighting() node-stat pairing
# ---------------------------------------------------------------------------
def test_insert_sighting_node_stat_pairing() -> None:
    print("\n[1] insert_sighting() node-stat pairing")
    add_node("n-stat1")
    before = db.node("n-stat1")
    check("node starts with sightings=0", before["sightings"] == 0, before)
    check("node starts with last_seen unset", before["last_seen"] is None, before)

    ts1 = 1_000_000.0
    sid1 = db.insert_sighting(base_record("n-stat1", ts1))
    row1 = db.sighting(sid1)
    check("first insert_sighting returns a positive id", sid1 > 0, sid1)
    check("first sighting row persisted with matching node_id/ts",
          row1 is not None and row1["node_id"] == "n-stat1" and row1["ts"] == ts1,
          row1)
    n1 = db.node("n-stat1")
    check("first insert_sighting bumps node.sightings to 1", n1["sightings"] == 1, n1)
    check("first insert_sighting sets node.last_seen to the sighting ts",
          n1["last_seen"] == ts1, n1)

    ts2 = 1_000_050.0
    sid2 = db.insert_sighting(base_record("n-stat1", ts2))
    n2 = db.node("n-stat1")
    check("second insert_sighting bumps node.sightings to 2", n2["sightings"] == 2, n2)
    check("second insert_sighting advances node.last_seen to the new ts",
          n2["last_seen"] == ts2, n2)
    check("both sightings persisted as distinct rows", sid1 != sid2, (sid1, sid2))

    # db.insert_sighting() issues the sightings INSERT and the nodes UPDATE on
    # the same connection with exactly one shared conn.commit() (db.py:496-503).
    # There is no natural, non-fragile seam to fault-inject a failure between
    # those two statements without monkeypatching sqlite3 connection internals
    # process-wide (which would leak into every other connection sharing the
    # same thread-local db._local.conn). Per Stage 3F1 instructions, this
    # single-commit atomicity is therefore characterized as CODE-REVIEW-ONLY:
    # the externally observable pairing above (both mutations landed together
    # after one call) is what this test dynamically verifies.
    print("  [info] single-commit atomicity of insert_sighting's two statements "
          "is CODE-REVIEW-ONLY coverage (db.py:496-503); not dynamically "
          "fault-injected, per instructions.")


# ---------------------------------------------------------------------------
# 2. merge_window_row() edge semantics
# ---------------------------------------------------------------------------
def test_merge_window_row_semantics() -> None:
    print("\n[2] merge_window_row() edge semantics")

    # (a) row inside the accepted window
    add_node("n-mw-a")
    sid_a = db.insert_sighting(base_record("n-mw-a", 1000.0, vclass="gov"))
    got = db.merge_window_row("n-mw-a", "gov", 1003.0, window=6.0)
    check("row inside window matches", got is not None and got["id"] == sid_a, got)

    # (b) row exactly at the EXCLUDED lower boundary (ts > lower_bound is strict)
    add_node("n-mw-b")
    db.insert_sighting(base_record("n-mw-b", 994.0, vclass="gov"))
    got = db.merge_window_row("n-mw-b", "gov", 1000.0, window=6.0)
    check("row exactly at lower boundary is EXCLUDED (ts > lower, strict)",
          got is None, got)

    # (c) row exactly at the INCLUDED upper boundary (ts <= upper_bound)
    add_node("n-mw-c")
    sid_c = db.insert_sighting(base_record("n-mw-c", 1000.0, vclass="gov"))
    got = db.merge_window_row("n-mw-c", "gov", 1000.0, window=6.0)
    check("row exactly at upper boundary is INCLUDED (ts <= upper)",
          got is not None and got["id"] == sid_c, got)

    # (d) same node, wrong vclass
    add_node("n-mw-d")
    db.insert_sighting(base_record("n-mw-d", 1000.0, vclass="civilian"))
    got = db.merge_window_row("n-mw-d", "gov", 1003.0, window=6.0)
    check("wrong vclass on the same node does not match", got is None, got)

    # (e) correct class, wrong node
    add_node("n-mw-e-other")
    db.insert_sighting(base_record("n-mw-e-other", 1000.0, vclass="gov"))
    got = db.merge_window_row("n-mw-e-target", "gov", 1003.0, window=6.0)
    check("matching class on a different node does not match", got is None, got)

    # (f) multiple valid rows: newest ts must win
    add_node("n-mw-f")
    sid_older = db.insert_sighting(base_record("n-mw-f", 1000.0, vclass="gov"))
    sid_newer = db.insert_sighting(base_record("n-mw-f", 1002.0, vclass="gov"))
    got = db.merge_window_row("n-mw-f", "gov", 1005.0, window=6.0)
    check("newest of several valid candidates wins (ORDER BY ts DESC LIMIT 1)",
          got is not None and got["id"] == sid_newer and got["ts"] == 1002.0,
          (got, sid_older, sid_newer))


# ---------------------------------------------------------------------------
# 3. bump_detections() MIN(ts)/MAX(conf) behavior
# ---------------------------------------------------------------------------
def test_bump_detections_min_max() -> None:
    print("\n[3] bump_detections() MIN(ts)/MAX(conf) behavior")
    add_node("n-bump")
    sid = db.insert_sighting(base_record("n-bump", 1000.0, vclass="gov",
                                          vclass_conf=0.9))
    row0 = db.sighting(sid)
    check("detections starts unset", row0["detections"] is None, row0)

    # later timestamp, LOWER confidence -> ts stays the earliest, conf stays max
    db.bump_detections(sid, ts=1005.0, conf=0.3)
    row1 = db.sighting(sid)
    check("bump_detections increments detections (1st call, NULL->2)",
          row1["detections"] == 2, row1)
    check("bump_detections keeps the earliest ts when a later ts is folded in",
          row1["ts"] == 1000.0, row1)
    check("bump_detections keeps the higher confidence when a lower one is folded in",
          row1["vclass_conf"] == 0.9, row1)

    # earlier timestamp, HIGHER confidence -> ts decreases, conf increases
    db.bump_detections(sid, ts=995.0, conf=0.99)
    row2 = db.sighting(sid)
    check("bump_detections increments detections (2nd call, 2->3)",
          row2["detections"] == 3, row2)
    check("bump_detections adopts an earlier ts when one is folded in (MIN)",
          row2["ts"] == 995.0, row2)
    check("bump_detections adopts a higher confidence when one is folded in (MAX)",
          row2["vclass_conf"] == 0.99, row2)


# ---------------------------------------------------------------------------
# 4. heartbeat() independence from sighting-insert
# ---------------------------------------------------------------------------
def test_heartbeat_independence() -> None:
    print("\n[4] heartbeat() independence")
    add_node("n-hb")
    before = db.node("n-hb")
    check("node starts with no heartbeat state",
          before["last_beat"] is None and before["beats"] is None, before)

    db.heartbeat("n-hb", ts=5000.0)
    n1 = db.node("n-hb")
    check("first heartbeat sets last_beat", n1["last_beat"] == 5000.0, n1)
    check("first heartbeat sets beats to 1 (COALESCE(beats,0)+1)",
          n1["beats"] == 1, n1)

    db.heartbeat("n-hb", ts=5010.0)
    n2 = db.node("n-hb")
    check("second heartbeat call is independent and increments beats again",
          n2["beats"] == 2, n2)
    check("second heartbeat call advances last_beat independently",
          n2["last_beat"] == 5010.0, n2)
    check("heartbeat calls never touch last_seen/sightings",
          n2["last_seen"] is None and n2["sightings"] == 0, n2)

    # A sighting insert must not move heartbeat/liveness fields, and a
    # heartbeat call must not move sighting-traffic fields - these are two
    # independent operations recording two different questions
    # ("a car drove past" vs "the camera is running"), per db.py:607-612.
    sid = db.insert_sighting(base_record("n-hb", 6000.0))
    n3 = db.node("n-hb")
    check("insert_sighting leaves heartbeat fields untouched",
          n3["last_beat"] == 5010.0 and n3["beats"] == 2, n3)
    check("insert_sighting still bumps last_seen/sightings independently",
          n3["last_seen"] == 6000.0 and n3["sightings"] == 1, n3)

    db.heartbeat("n-hb", ts=6500.0)
    n4 = db.node("n-hb")
    check("a later heartbeat call leaves last_seen/sightings untouched",
          n4["last_seen"] == 6000.0 and n4["sightings"] == 1, n4)
    check("a later heartbeat call still advances liveness independently",
          n4["last_beat"] == 6500.0 and n4["beats"] == 3, n4)
    _ = sid


# ---------------------------------------------------------------------------
# 5. promote_sighting() missing-row safety
# ---------------------------------------------------------------------------
def test_promote_sighting_missing_row() -> None:
    print("\n[5] promote_sighting() missing-row safety")
    add_node("n-promote")
    sid = db.insert_sighting(base_record("n-promote", 1000.0, vclass="civilian",
                                          tier="private"))
    # Simulate the documented race: the retention sweep deletes the row while
    # it still sits in the review pen (db.py:1899-1911).
    conn = db.connect()
    conn.execute("DELETE FROM sightings WHERE id=?", (sid,))
    conn.commit()
    check("row removed out from under the pending review", db.sighting(sid) is None)

    raised: Exception | None = None
    try:
        db.promote_sighting(sid)
    except Exception as exc:  # capture the exact type below
        raised = exc
    check("promote_sighting on a vanished row raises LookupError (not silent ok)",
          isinstance(raised, LookupError), raised)
    check("promote_sighting does not resurrect a deleted row",
          db.sighting(sid) is None, db.sighting(sid))


# ---------------------------------------------------------------------------
# 6. review_sighting() / resolve_reports() sequencing
# ---------------------------------------------------------------------------
def test_review_sighting_report_sequencing() -> None:
    print("\n[6] review_sighting() / resolve_reports() sequencing")

    # Baseline: the normal path resolves both the verdict and the report.
    add_node("n-review-a")
    sid_a = db.insert_sighting(base_record("n-review-a", 1000.0, vclass="gov",
                                            tier="public"))
    db.add_report(sid_a, "not_a_police_car", "looks like a contractor truck", "1.2.3.4")
    db.review_sighting(sid_a, "confirmed")
    row_a = db.sighting(sid_a)
    open_reports_a = db.reports_for(sid_a, open_only=True)
    check("normal-path review_sighting records the verdict",
          row_a["reviewed"] == "confirmed", row_a)
    check("normal-path review_sighting also resolves the open report",
          open_reports_a == [], open_reports_a)

    # Fault injection: db.review_sighting() calls the unqualified name
    # `resolve_reports(sighting_id)` (db.py:1441), which Python resolves via
    # db.py's own module globals at call time. Monkeypatching db.resolve_reports
    # from this test therefore intercepts that same call - the same kind of
    # module-attribute patching convention already used throughout this suite
    # for DB_PATH/DATA, not a fragile sqlite3-internals patch.
    add_node("n-review-b")
    sid_b = db.insert_sighting(base_record("n-review-b", 1000.0, vclass="gov",
                                            tier="public"))
    db.add_report(sid_b, "not_a_police_car", "unmarked contractor van", "1.2.3.4")

    original_resolve_reports = db.resolve_reports

    def _boom(_sighting_id: int) -> None:
        raise RuntimeError("Stage 3F1 injected fault: resolve_reports failed")

    db.resolve_reports = _boom
    raised: Exception | None = None
    try:
        db.review_sighting(sid_b, "confirmed")
    except Exception as exc:
        raised = exc
    finally:
        db.resolve_reports = original_resolve_reports

    check("resolve_reports failure propagates out of review_sighting (uncaught)",
          isinstance(raised, RuntimeError), raised)

    row_b = db.sighting(sid_b)
    open_reports_b = db.reports_for(sid_b, open_only=True)
    check("the verdict UPDATE was ALREADY committed before the injected failure "
          "(review_sighting's own commit precedes the resolve_reports call, "
          "db.py:1436-1441)",
          row_b["reviewed"] == "confirmed", row_b)
    check("the report resolution UPDATE did NOT happen (remains open) because "
          "review_sighting/resolve_reports are two separate commits, not one "
          "transaction",
          len(open_reports_b) == 1, open_reports_b)

    print("  [info] review_sighting()'s verdict UPDATE and resolve_reports()'s "
          "UPDATE are dynamically confirmed here as two SEPARATE commits "
          "(db.py:1406-1441, 1575-1581): a fault between them leaves the "
          "verdict recorded but the report unresolved. This is documented, "
          "pre-existing, non-atomic behavior and is not being fixed.")


# ---------------------------------------------------------------------------
# 7. Raw-SQL leak baseline (static, count-based, not line-number based)
# ---------------------------------------------------------------------------
def test_raw_sql_leak_baseline() -> None:
    print("\n[7] raw-SQL leak baseline (Stage 3F2: closed sites now at 0/expected)")
    expectations = {
        "hub.py": 1,               # only _janitor()'s purge_expired(db.connect()) remains
        "node_credentials.py": 0,  # node_key/key_rotate now use db.set_node_pubkey/set_node_token
        "review_api.py": 0,        # contributed()/attach_confirmed_photo() now use named db fns
        "reviewer_read.py": 0,     # review_queue() now uses db.public_review_queue_rows/
                                   # db.private_unreviewed_since
        "operator_admin.py": 1,    # purge() deferred; still passes db.connect() to privacy.py
    }
    for filename, expected_count in expectations.items():
        text = (ROOT / filename).read_text(encoding="utf-8")
        actual_count = text.count("db.connect(")
        check(f"{filename} has {expected_count} direct db.connect() call(s) "
              f"(Stage 3F2 baseline)",
              actual_count == expected_count,
              f"found {actual_count}")

    # privacy.py never calls db.connect() itself - it receives an externally
    # opened connection/cursor as a parameter and issues raw DELETE statements
    # against it (privacy.py:~390-419). Characterize that shape by substring,
    # not by line number, so harmless reformatting does not break this check.
    privacy_text = (ROOT / "privacy.py").read_text(encoding="utf-8")
    check("privacy.py's purge_expired still receives an external connection "
          "(no internal db.connect() of its own)",
          "def purge_expired(conn)" in privacy_text
          and "db.connect(" not in privacy_text,
          "purge_expired signature or db.connect() usage changed")
    check("privacy.py's purge_expired still issues raw DELETE FROM sightings",
          "DELETE FROM sightings" in privacy_text,
          "raw DELETE FROM sightings no longer found")


def main() -> int:
    try:
        test_insert_sighting_node_stat_pairing()
        test_merge_window_row_semantics()
        test_bump_detections_min_max()
        test_heartbeat_independence()
        test_promote_sighting_missing_row()
        test_review_sighting_report_sequencing()
        test_raw_sql_leak_baseline()
    finally:
        db.close_thread()

    print(f"\n{CHECKS} checks run.")
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: " + ", ".join(FAILURES))
        return 1
    print("all persistence characterization checks passed")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        shutil.rmtree(SCRATCH, ignore_errors=True)
    sys.exit(code)
