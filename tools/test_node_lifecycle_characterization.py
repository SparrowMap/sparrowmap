"""Focused behavioral characterization for the Stage 2D2 lifecycle routes.

Covers the current heartbeat and setup telemetry behavior that Stage 2D2 moves:
    POST /api/heartbeat
    POST /api/heartbeat/bulk
    POST /api/node/progress

The suite is deterministic and isolated: it uses a temporary scratch database,
launches the real hub in a local-threaded HTTP server, and keeps assertions to
externally observable behavior. It intentionally preserves documented security
quirks such as permissive _token_ok behavior rather than fixing them.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SCRATCH = Path(tempfile.mkdtemp(prefix="sparrow_node_lifecycle_"))
for sub in ("snaps", "evidence", "held", "review", "inbox", "tiles"):
    (SCRATCH / sub).mkdir(parents=True, exist_ok=True)

import core  # noqa: E402
import db  # noqa: E402
import mirror  # noqa: E402
import review_api  # noqa: E402
import snapshot  # noqa: E402

REAL_DB = Path(db.DB_PATH)
for _mod, _name, _val in [
    (core, "DATA", SCRATCH),
    (core, "SNAPS", SCRATCH / "snaps"),
    (core, "EVIDENCE", SCRATCH / "evidence"),
    (core, "HELD", SCRATCH / "held"),
    (core, "DB_PATH", SCRATCH / "sparrow.db"),
    (db, "DB_PATH", SCRATCH / "sparrow.db"),
    (snapshot, "SNAPS", SCRATCH / "snaps"),
    (review_api, "SNAPS", SCRATCH / "snaps"),
    (review_api, "EVIDENCE", SCRATCH / "evidence"),
    (review_api, "HELD", SCRATCH / "held"),
    (mirror, "DATA", SCRATCH),
    (mirror, "REVIEW", SCRATCH / "review"),
    (mirror, "INBOX", SCRATCH / "inbox"),
]:
    setattr(_mod, _name, _val)

import hub  # noqa: E402

PORT = 8797
BASE = f"http://127.0.0.1:{PORT}"
UA = "SparrowMap-node-lifecycle-test/1.0"
FAIL = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"  [{detail}]" if detail else ""))
    if not ok:
        FAIL.append(name)


def call(path: str, body=None, token: str = "", method: str = "POST"):
    hdrs = {"User-Agent": UA}
    data = None
    if body is not None:
        hdrs["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    if token:
        hdrs["Authorization"] = "Bearer " + token
    req = urllib.request.Request(BASE + path, method=method, data=data, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            raw = r.read()
            try:
                return r.status, json.loads(raw or b"{}")
            except Exception:
                return r.status, {"raw": raw.decode("utf-8", "replace")}
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw or b"{}")
        except Exception:
            return e.code, {"raw": raw.decode("utf-8", "replace")}


def enroll(name: str, status: str = "active"):
    st, node = call("/api/enroll", {"name": name, "lat": 42.5, "lon": -83.7,
                                    "kind": "fixed"})
    if st != 200 or not node.get("token"):
        raise RuntimeError(f"enroll failed: {st} {node}")
    conn = db.connect()
    conn.execute("UPDATE nodes SET status=? WHERE id=?", (status, node["id"]))
    conn.commit()
    return node["id"], node["token"]


def get_node(node_id: str):
    return db.connect().execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()


def main() -> int:
    srv = hub.ThreadingHTTPServer(("127.0.0.1", PORT), hub.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    for _ in range(50):
        try:
            urllib.request.urlopen(urllib.request.Request(BASE + "/api/health",
                                                          headers={"User-Agent": UA}),
                                   timeout=2)
            break
        except Exception:
            time.sleep(0.1)
    else:
        print("server never came up")
        return 2

    # /api/heartbeat: unknown, wrong token, active, pause/revoke inactive, tokenless SEC-02
    nid, tok = enroll("heartbeat-live")
    before = get_node(nid)
    st, body = call("/api/heartbeat", {"node_id": "missing-node"}, method="POST")
    check("POST /api/heartbeat unknown node -> 404", st == 404, f"{st} {body}")
    st, body = call("/api/heartbeat", {"node_id": nid}, method="POST", token="wrong")
    check("POST /api/heartbeat wrong token -> 401", st == 401, f"{st} {body}")
    st, body = call("/api/heartbeat", {"node_id": nid}, method="POST", token=tok)
    after = get_node(nid)
    check("POST /api/heartbeat correct token -> 200", st == 200 and body.get("ok") is True,
          f"{st} {body}")
    check("POST /api/heartbeat updates last_beat on active node",
          after["last_beat"] is not None and after["beats"] == (before["beats"] or 0) + 1,
          f"before={before['beats']} after={after['beats']} last_beat={after['last_beat']}")

    conn = db.connect()
    conn.execute("UPDATE nodes SET status='paused', last_beat=NULL, beats=beats WHERE id=?", (nid,))
    conn.commit()
    before_paused = get_node(nid)
    st, body = call("/api/heartbeat", {"node_id": nid}, method="POST", token=tok)
    after_paused = get_node(nid)
    check("POST /api/heartbeat paused node -> 200 inactive response with no mutation",
          st == 200 and body.get("posting") is False and body.get("status") == "paused"
          and after_paused["last_beat"] == before_paused["last_beat"],
          f"{st} {body} last_beat={after_paused['last_beat']}")
    conn.execute("UPDATE nodes SET status='revoked', last_beat=NULL, beats=beats WHERE id=?", (nid,))
    conn.commit()
    before_revoked = get_node(nid)
    st, body = call("/api/heartbeat", {"node_id": nid}, method="POST", token=tok)
    after_revoked = get_node(nid)
    check("POST /api/heartbeat revoked node -> 200 inactive response with no mutation",
          st == 200 and body.get("posting") is False and body.get("status") == "revoked"
          and after_revoked["last_beat"] == before_revoked["last_beat"],
          f"{st} {body} last_beat={after_revoked['last_beat']}")

    nid2, tok2 = enroll("heartbeat-tokenless", status="active")
    conn = db.connect(); conn.execute("UPDATE nodes SET token=NULL WHERE id=?", (nid2,)); conn.commit()
    st, body = call("/api/heartbeat", {"node_id": nid2}, method="POST")
    check("POST /api/heartbeat tokenless active node preserves SEC-02 permissive behavior",
          st == 200 and body.get("ok") is True, f"{st} {body}")

    # /api/heartbeat/bulk: valid/bad/inactive entries and counts
    nid3, tok3 = enroll("heartbeat-bulk", status="active")
    nid4, tok4 = enroll("heartbeat-bulk-paused", status="paused")
    nid5, tok5 = enroll("heartbeat-bulk-revoked", status="revoked")
    st, body = call("/api/heartbeat/bulk", {"nodes": [
        {"node_id": nid3, "token": tok3},
        {"node_id": nid3, "token": "wrong"},
        {"node_id": nid4, "token": tok4},
        {"node_id": nid5, "token": None},
        {"node_id": "missing"},
        {"node_id": nid2, "token": None},
    ]}, method="POST")
    check("POST /api/heartbeat/bulk returns aggregate counts", st == 200 and body.get("beat") == 2 and body.get("rejected") == 3 and body.get("inactive") == 1,
          f"{st} {body}")

    # /api/node/progress: wrong token, SEC-02 tokenless allowance, and setup stage updates
    nid6, tok6 = enroll("node-progress", status="active")
    st, body = call("/api/node/progress", {"node_id": nid6, "stage": "detecting", "platform": "ios"}, method="POST", token="wrong")
    check("POST /api/node/progress wrong token -> 401", st == 401, f"{st} {body}")
    st, body = call("/api/node/progress", {"node_id": nid6, "stage": "detecting", "platform": "ios"}, method="POST", token=tok6)
    check("POST /api/node/progress correct token -> 200", st == 200 and body.get("ok") is True, f"{st} {body}")
    setup = get_node(nid6)["setup_stage"]
    check("POST /api/node/progress updates setup_stage", setup == "detecting", f"setup_stage={setup}")
    conn = db.connect(); conn.execute("UPDATE nodes SET token=NULL WHERE id=?", (nid6,)); conn.commit()
    st, body = call("/api/node/progress", {"node_id": nid6, "stage": "complete", "platform": "android"}, method="POST")
    check("POST /api/node/progress tokenless node preserves SEC-02 permissive behavior",
          st == 200 and body.get("ok") is True, f"{st} {body}")

    srv.shutdown(); srv.server_close()
    print()
    if FAIL:
        print(f"{len(FAIL)} FAILED: " + ", ".join(FAIL))
        return 1
    print("all node lifecycle checks passed")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        shutil.rmtree(SCRATCH, ignore_errors=True)
    sys.exit(code)
