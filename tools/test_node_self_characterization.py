"""Focused behavioral characterization for the Stage 2D1 camera self-service routes.

This suite exercises the current implementation of the routes that were moved
into node_self.py: /api/node/me, /api/node/whoami, /api/node/parked and
/api/node/span. It does not modify production code or assert source layout; it
verifies externally observable behavior against a local temporary database.

Run directly:
    python tools\test_node_self_characterization.py
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

SCRATCH = Path(tempfile.mkdtemp(prefix="sparrow_node_self_"))
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

PORT = 8794
BASE = f"http://127.0.0.1:{PORT}"
UA = "SparrowMap-node-self-test/1.0"
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


def enroll(name: str, kind: str = "phone"):
    st, node = call("/api/enroll", {"name": name, "lat": 42.5, "lon": -83.7,
                                    "kind": kind})
    if st != 200 or not node.get("token"):
        raise RuntimeError(f"enroll failed: {st} {node}")
    conn = db.connect()
    conn.execute("UPDATE nodes SET status='active' WHERE id=?", (node["id"],))
    conn.commit()
    return node["id"], node["token"]


def make_sighting(node_id: str, ts: float | None = None) -> int:
    ts = float(ts if ts is not None else time.time())
    rec = {"node_id": node_id, "ts": ts, "lat": 42.5, "lon": -83.7,
           "tier": "private", "plate_hash": None, "plate_text": None,
           "plate_state": None, "plate_conf": 0.0, "vclass": "police",
           "vclass_conf": 0.8, "vclass_why": "test-sighting",
           "color": None, "body": "test", "make": None, "model": None,
           "heading": 90.0, "speed_mph": 0.0, "snap": None, "source": "camera",
           "sig_ok": 1, "bank_ref": None, "reviewed": None, "decided_by": None}
    return db.insert_sighting(rec)


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

    # /api/node/me: unknown, tokenless, wrong token, valid token + field shape
    nid, tok = enroll("node-self-me")
    st, body = call(f"/api/node/me?id={nid}", method="GET")
    check("GET /api/node/me requires the camera token", st == 401, f"{st} {body}")
    st, body = call(f"/api/node/me?id={nid}", method="GET", token="wrong")
    check("GET /api/node/me rejects a wrong token", st == 401, f"{st} {body}")
    st, body = call(f"/api/node/me?id={nid}", method="GET", token=tok)
    check("GET /api/node/me with correct token -> 200", st == 200 and body.get("id") == nid,
          f"{st} {body}")
    for key in ("id", "name", "kind", "lat", "lon", "heading", "fov",
                "reach_m", "publish_span", "sightings", "last_seen"):
        check(f"GET /api/node/me returns field {key}", key in body, str(body.keys()))
    check("GET /api/node/me returns true position only after token validation",
          isinstance(body.get("lat"), (int, float)) and isinstance(body.get("lon"), (int, float)),
          f"lat={body.get('lat')} lon={body.get('lon')}")

    # /api/node/whoami: wrong token, tokenless, valid token
    st, body = call("/api/node/whoami", {"node_id": nid}, method="POST")
    check("POST /api/node/whoami without token -> 401", st == 401, f"{st} {body}")
    st, body = call("/api/node/whoami", {"node_id": nid}, method="POST", token="wrong")
    check("POST /api/node/whoami rejects wrong token", st == 401, f"{st} {body}")
    st, body = call("/api/node/whoami", {"node_id": nid}, method="POST", token=tok)
    check("POST /api/node/whoami with correct token -> 200",
          st == 200 and body.get("id") == nid, f"{st} {body}")

    # /api/node/parked: owner-only filtering and hidden other-node sightings
    nid2, tok2 = enroll("node-self-parked")
    sid1 = make_sighting(nid)
    sid2 = make_sighting(nid2)
    meta = {"node_id": nid, "vclass": "police", "score": 0.91}
    (mirror.REVIEW / f"{sid1}.json").write_text(json.dumps(meta), encoding="utf-8")
    meta2 = {"node_id": nid2, "vclass": "police", "score": 0.73}
    (mirror.REVIEW / f"{sid2}.json").write_text(json.dumps(meta2), encoding="utf-8")

    st, body = call("/api/node/parked", {"node_id": nid, "ids": [sid1, sid2]},
                    method="POST", token=tok)
    check("POST /api/node/parked returns only own sightings",
          st == 200 and len(body.get("parked", [])) == 1 and body["parked"][0]["id"] == sid1,
          f"{st} {body}")

    # /api/node/span: false->true->false; response reports span state
    conn = db.connect()
    conn.execute("UPDATE nodes SET span_lat1=?, span_lon1=?, span_lat2=?, span_lon2=?, road_name=? WHERE id=?",
                 (42.50, -83.70, 42.51, -83.68, "Main St", nid))
    conn.commit()
    st, body = call("/api/node/span", {"node_id": nid, "publish": False}, method="POST", token=tok)
    check("POST /api/node/span false -> returns publish false and has_span",
          st == 200 and body.get("publish") is False and body.get("has_span") is True,
          f"{st} {body}")
    st, body = call("/api/node/span", {"node_id": nid, "publish": True}, method="POST", token=tok)
    check("POST /api/node/span true -> returns publish true",
          st == 200 and body.get("publish") is True, f"{st} {body}")
    st, body = call("/api/node/span", {"node_id": nid, "publish": False}, method="POST", token=tok)
    check("POST /api/node/span false -> returns publish false again",
          st == 200 and body.get("publish") is False, f"{st} {body}")

    srv.shutdown()
    srv.server_close()
    print()
    if FAIL:
        print(f"{len(FAIL)} FAILED: " + ", ".join(FAIL))
        return 1
    print("all node self-service checks passed")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        shutil.rmtree(SCRATCH, ignore_errors=True)
    sys.exit(code)
