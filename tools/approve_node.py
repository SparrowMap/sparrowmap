"""Approve ONE paused camera, by hand, after looking at it.

    python tools/approve_node.py --box root@HOST --key PATH --node n_xxxxxxxx
    python tools/approve_node.py --box ... --key ... --node n_xxxxxxxx --yes

WHY THIS EXISTS

The live box runs with `auto_approve_nodes: false` (config.launch.json): anyone
can reach /api/enroll, so a new camera arrives `paused` and posts nothing until
the operator lets it in. Until 10-27 the only ways to do that were a bulk tool
built for thousands of traffic cameras (bulk_enrol_cams.py) or hand-typed SQL on
the box. Written for the WGXA Dublin GA webcam (bug report 013), the first
single public webcam added since; the opposite of revoke_node.py.

WHAT IT REFUSES

Only `paused` becomes `active`. A `revoked` node had its key taken away on
purpose and a `removed` one was taken down by its owner (/api/node/remove);
neither is this tool's to undo. Preview by default; --yes to do it.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
from pathlib import Path

REMOTE = "/opt/sparrowmap"

REMOTE_OP = r'''
import json, sqlite3, sys
node_id = NODE_JSON
do_it = DO_IT
db = sqlite3.connect("/opt/sparrowmap/data/sparrow.db")
db.row_factory = sqlite3.Row
n = db.execute("SELECT id, name, kind, status, lat, lon, created FROM nodes WHERE id = ?", (node_id,)).fetchone()
if not n:
    print(json.dumps({"error": "no such node: %s" % node_id})); sys.exit(0)
out = dict(n)
if out["status"] != "paused":
    print(json.dumps({"error": "%s is %s, not paused; this tool only approves paused nodes" % (node_id, out["status"])})); sys.exit(0)
if not do_it:
    print(json.dumps({"preview": out})); sys.exit(0)
cur = db.execute("UPDATE nodes SET status = 'active' WHERE id = ? AND status = 'paused'", (node_id,))
db.commit()
out["now_status"] = db.execute("SELECT status FROM nodes WHERE id = ?", (node_id,)).fetchone()[0]
try:
    sys.path.insert(0, "/opt/sparrowmap")
    import db as _db
    _db.audit("node:approved", node_id, actor="operator")
except Exception as exc:
    out["audit_error"] = str(exc)
print(json.dumps({"done": out}))
'''


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--box", required=True)
    ap.add_argument("--key", required=True)
    ap.add_argument("--node", required=True)
    ap.add_argument("--yes", action="store_true", help="actually approve it")
    a = ap.parse_args()

    script = (REMOTE_OP.replace("NODE_JSON", json.dumps(a.node))
                       .replace("DO_IT", "True" if a.yes else "False"))
    tmp = Path(tempfile.mkdtemp()) / "op.py"
    tmp.write_text(script, encoding="utf-8")
    subprocess.run(["scp", "-i", a.key, "-o", "BatchMode=yes", "-q",
                    str(tmp), "%s:/tmp/_approve.py" % a.box], check=True)
    r = subprocess.run(["ssh", "-i", a.key, "-o", "BatchMode=yes", a.box,
                        "cd %s && .venv/bin/python /tmp/_approve.py" % REMOTE],
                       capture_output=True, timeout=120)
    if r.returncode != 0:
        raise SystemExit("ssh failed: %s" % r.stderr.decode()[:300])
    res = json.loads(r.stdout.decode("utf-8", "replace"))
    if "error" in res:
        raise SystemExit("  " + res["error"])
    if "preview" in res:
        print("  WOULD APPROVE:")
        for k, v in res["preview"].items():
            print("    %-8s %s" % (k, v))
        print("\n  Nothing changed. Re-run with --yes to approve.")
        return
    d = res["done"]
    print("  APPROVED %s (%s): %s -> %s" % (d["id"], d["name"], d["status"], d["now_status"]))
    if d.get("audit_error"):
        print("    audit entry failed: %s" % d["audit_error"])


if __name__ == "__main__":
    main()
