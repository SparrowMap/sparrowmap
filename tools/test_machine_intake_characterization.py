"""Focused behavioral characterization for the Stage 3E5 machine-intake routes.

Covers the current node-authentication behavior of:
    POST /api/rf
    POST /api/radar/hit
    POST /api/sensor/hit
    POST /api/aircraft/ingest

/api/rf uses tokenless-compatible bearer authentication gated by active node
status (no configured-token requirement). /api/radar/hit, /api/sensor/hit and
/api/aircraft/ingest require a configured node token and do not check node
status. This suite is deterministic and isolated: it uses a temporary scratch
database, launches the real hub in a local-threaded HTTP server, and keeps
assertions to externally observable behavior. It intentionally preserves
documented security quirks (tokenless RF authentication, status-before-auth
disclosure) rather than fixing them.
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

SCRATCH = Path(tempfile.mkdtemp(prefix="sparrow_machine_intake_"))
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

if Path(db.DB_PATH) == REAL_DB:
    print("REFUSING TO RUN: database still points at the live path.")
    shutil.rmtree(SCRATCH, ignore_errors=True)
    sys.exit(2)

PORT = 8798
BASE = f"http://127.0.0.1:{PORT}"
UA = "SparrowMap-machine-intake-test/1.0"
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

    # /api/rf: unknown, inactive-before-token, wrong token, tokenless active OK
    nid, tok = enroll("rf-live")
    st, body = call("/api/rf", {"node_id": "missing-node", "candidates": []})
    check("POST /api/rf unknown node -> 404", st == 404, f"{st} {body}")

    nid_paused, tok_paused = enroll("rf-paused", status="paused")
    st, body = call("/api/rf", {"node_id": nid_paused, "candidates": []},
                     token="wrong")
    check("POST /api/rf inactive node rejected before token check -> 403",
          st == 403 and "paused" in str(body.get("error", "")), f"{st} {body}")

    st, body = call("/api/rf", {"node_id": nid, "candidates": []}, token="wrong")
    check("POST /api/rf active node wrong token -> 401", st == 401, f"{st} {body}")

    st, body = call("/api/rf", {"node_id": nid, "candidates": []}, token=tok)
    check("POST /api/rf active node correct token -> 200",
          st == 200 and body.get("reviewed") == "pending", f"{st} {body}")

    conn = db.connect()
    conn.execute("UPDATE nodes SET token=NULL WHERE id=?", (nid,))
    conn.commit()
    st, body = call("/api/rf", {"node_id": nid, "candidates": []})
    check("POST /api/rf tokenless active node preserves permissive behavior",
          st == 200 and body.get("reviewed") == "pending", f"{st} {body}")

    # /api/radar/hit: unknown, missing configured token, wrong token, correct
    nid2, tok2 = enroll("radar-live")
    st, body = call("/api/radar/hit", {"node_id": "missing-node", "band": "ka",
                                        "lat": 42.5, "lon": -83.7})
    check("POST /api/radar/hit unknown node -> 404", st == 404, f"{st} {body}")

    st, body = call("/api/radar/hit", {"node_id": nid2, "band": "ka",
                                        "lat": 42.5, "lon": -83.7}, token="wrong")
    check("POST /api/radar/hit wrong token -> 401", st == 401, f"{st} {body}")

    st, body = call("/api/radar/hit", {"node_id": nid2, "band": "ka",
                                        "lat": 42.5, "lon": -83.7}, token=tok2)
    check("POST /api/radar/hit correct token -> 200",
          st == 200 and body.get("ok") is True, f"{st} {body}")

    conn.execute("UPDATE nodes SET token=NULL WHERE id=?", (nid2,))
    conn.commit()
    st, body = call("/api/radar/hit", {"node_id": nid2, "band": "ka",
                                        "lat": 42.5, "lon": -83.7})
    check("POST /api/radar/hit requires a configured token (tokenless -> 401)",
          st == 401 and "no token" in str(body.get("error", "")), f"{st} {body}")

    # /api/sensor/hit: unknown, missing configured token, wrong token, correct
    nid3, tok3 = enroll("sensor-live")
    st, body = call("/api/sensor/hit", {"node_id": "missing-node", "kind": "drone",
                                         "lat": 42.5, "lon": -83.7})
    check("POST /api/sensor/hit unknown node -> 404", st == 404, f"{st} {body}")

    st, body = call("/api/sensor/hit", {"node_id": nid3, "kind": "drone",
                                         "lat": 42.5, "lon": -83.7}, token="wrong")
    check("POST /api/sensor/hit wrong token -> 401", st == 401, f"{st} {body}")

    st, body = call("/api/sensor/hit", {"node_id": nid3, "kind": "drone",
                                         "lat": 42.5, "lon": -83.7}, token=tok3)
    check("POST /api/sensor/hit correct token -> 200",
          st == 200 and body.get("ok") is True, f"{st} {body}")

    conn.execute("UPDATE nodes SET token=NULL WHERE id=?", (nid3,))
    conn.commit()
    st, body = call("/api/sensor/hit", {"node_id": nid3, "kind": "drone",
                                         "lat": 42.5, "lon": -83.7})
    check("POST /api/sensor/hit requires a configured token (tokenless -> 401)",
          st == 401 and "no token" in str(body.get("error", "")), f"{st} {body}")

    # /api/aircraft/ingest: feature-flag gate precedes auth, then the usual
    # unknown / missing-token / wrong-token / correct-token sequence.
    hub.CONFIG["aircraft_preview"] = False
    st, body = call("/api/aircraft/ingest", {"node_id": "missing-node",
                                              "aircraft": []})
    check("POST /api/aircraft/ingest disabled feature -> 404 before auth",
          st == 404 and "not enabled" in str(body.get("error", "")), f"{st} {body}")

    hub.CONFIG["aircraft_preview"] = True
    nid4, tok4 = enroll("aircraft-live")
    st, body = call("/api/aircraft/ingest", {"node_id": "missing-node",
                                              "aircraft": []})
    check("POST /api/aircraft/ingest unknown node -> 404", st == 404, f"{st} {body}")

    st, body = call("/api/aircraft/ingest", {"node_id": nid4, "aircraft": []},
                     token="wrong")
    check("POST /api/aircraft/ingest wrong token -> 401", st == 401, f"{st} {body}")

    st, body = call("/api/aircraft/ingest", {"node_id": nid4, "aircraft": []},
                     token=tok4)
    check("POST /api/aircraft/ingest correct token -> 200",
          st == 200 and body.get("ok") is True, f"{st} {body}")

    conn.execute("UPDATE nodes SET token=NULL WHERE id=?", (nid4,))
    conn.commit()
    st, body = call("/api/aircraft/ingest", {"node_id": nid4, "aircraft": []})
    check("POST /api/aircraft/ingest requires a configured token (tokenless -> 401)",
          st == 401 and "no token" in str(body.get("error", "")), f"{st} {body}")

    srv.shutdown(); srv.server_close()
    print()
    if FAIL:
        print(f"{len(FAIL)} FAILED: " + ", ".join(FAIL))
        return 1
    print("all machine intake checks passed")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        shutil.rmtree(SCRATCH, ignore_errors=True)
    sys.exit(code)
