"""One-off: copy the police calls made before rv_calls existed (10-17) out of `audit` into rv_calls,
so the approvals page (/rv/approved) and reversal feedback cover them. Label only - audit never kept
the reviewer's id, so a reversal of one of these reaches every reviewer carrying that label.

  python tools/backfill_rv_calls.py [--days 30]

Reads audit by ts (ix_audit_ts), never by action. Idempotent: a sid already in rv_calls is left alone.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db  # noqa: E402

days = float(sys.argv[sys.argv.index("--days") + 1]) if "--days" in sys.argv else 30.0
conn = db.connect()
since = time.time() - days * 86400
n = 0
# not box_review: that is the operator's own command-line confirm (tools/box_publish.py), not a reviewer
for r in conn.execute("SELECT target, actor, ts FROM audit WHERE ts >= ? AND action = 'review:confirm' "
                      "AND actor NOT IN ('box_review')", (since,)).fetchall():
    try:
        sid = int(r["target"])
    except (TypeError, ValueError):
        continue
    n += conn.execute("INSERT OR IGNORE INTO rv_calls(sid, reviewer_id, label, ts) VALUES (?,?,?,?)",
                      (sid, None, r["actor"] or "", r["ts"])).rowcount
conn.commit()
print("rv_calls: %d police calls copied from the last %g days of audit" % (n, days))
