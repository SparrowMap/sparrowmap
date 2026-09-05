"""Focused behavioral characterization for the Stage 2D3 credential/evidence routes.

Exercises the inherited hub behavior before/after extracting the adapter layer for:
    POST /api/node/key
    POST /api/key/qr
    POST /api/key/rotate
    POST /api/sighting/fullres

This suite asserts externally observable behavior only: status codes, payload
shapes, persistence, and request-side auth semantics. It is intentionally not a
source-level test and does not normalize or fix the preserved defects.
"""

from __future__ import annotations

import base64
import json
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from io import BytesIO
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SCRATCH = Path(tempfile.mkdtemp(prefix="sparrow_node_key_"))
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

PORT = 8916
BASE = f"http://127.0.0.1:{PORT}"
UA = "SparrowMap-node-key-characterization/1.0"
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


def call_raw(path: str, body=None, token: str = "", method: str = "POST"):
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
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def enroll(name: str, status: str = "active"):
    st, node = call("/api/enroll", {"name": name, "lat": 42.5, "lon": -83.7, "kind": "fixed"})
    if st != 200 or not node.get("token"):
        raise RuntimeError(f"enroll failed: {st} {node}")
    conn = db.connect()
    conn.execute("UPDATE nodes SET status=? WHERE id=?", (status, node["id"]))
    conn.commit()
    return node["id"], node["token"]


def valid_pubkey() -> str:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return base64.b64encode(pub).decode("ascii")


def make_jpeg_b64() -> str:
    buf = BytesIO()
    Image.new("RGB", (10, 10), color=(255, 0, 0)).save(buf, format="JPEG")
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def make_sighting(node_id: str, *, tier: str = "public") -> int:
    rec = {
        "node_id": node_id,
        "ts": time.time(),
        "lat": 42.5,
        "lon": -83.7,
        "tier": tier,
        "plate_hash": None,
        "plate_text": None,
        "plate_state": None,
        "plate_conf": 0.0,
        "vclass": "police",
        "vclass_conf": 0.8,
        "vclass_why": "test-sighting",
        "color": None,
        "body": "test",
        "make": None,
        "model": None,
        "heading": 90.0,
        "speed_mph": 0.0,
        "snap": None,
        "source": "camera",
        "sig_ok": 1,
        "bank_ref": None,
        "reviewed": None,
        "decided_by": None,
    }
    return db.insert_sighting(rec)


def main() -> int:
    srv = hub.ThreadingHTTPServer(("127.0.0.1", PORT), hub.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
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

        nid, tok = enroll("node-key-seed")
        bad_pub = "not-a-real-ed25519-key"

        st, body = call("/api/node/key", {"node_id": "missing-node", "pubkey": valid_pubkey()})
        check("POST /api/node/key unknown node -> 404", st == 404, f"{st} {body}")

        st, body = call("/api/node/key", {"node_id": nid, "pubkey": valid_pubkey()})
        check("POST /api/node/key without token -> 401", st == 401, f"{st} {body}")

        st, body = call("/api/node/key", {"node_id": nid, "pubkey": valid_pubkey()}, token="wrong")
        check("POST /api/node/key wrong token -> 401", st == 401, f"{st} {body}")

        st, body = call("/api/node/key", {"node_id": nid, "pubkey": bad_pub}, token=tok)
        check("POST /api/node/key malformed pubkey -> 400", st == 400, f"{st} {body}")

        pub = valid_pubkey()
        count_before = db.connect().execute("SELECT COUNT(*) FROM audit WHERE action='node_key'").fetchone()[0]
        st, body = call("/api/node/key", {"node_id": nid, "pubkey": pub}, token=tok)
        check("POST /api/node/key correct token -> 200", st == 200 and body.get("ok") is True and body.get("id") == nid,
            f"{st} {body}")
        stored = db.connect().execute("SELECT pubkey FROM nodes WHERE id = ?", (nid,)).fetchone()[0]
        check("POST /api/node/key persists the Ed25519 public key", stored == pub, f"stored={stored!r}")
        count_after = db.connect().execute("SELECT COUNT(*) FROM audit WHERE action='node_key'").fetchone()[0]
        check("POST /api/node/key creates an audit event", count_after == count_before + 1, f"before={count_before} after={count_after}")

        st, body = call("/api/key/qr", {"node_id": nid, "token": "", "origin": "https://example.test"})
        check("POST /api/key/qr missing token -> 404", st == 404, f"{st} {body}")

        st, body = call("/api/key/qr", {"node_id": nid, "token": "wrong", "origin": "https://example.test"})
        check("POST /api/key/qr wrong token -> 403", st == 403, f"{st} {body}")

        st, headers, raw = call_raw("/api/key/qr", {"node_id": nid, "token": tok, "origin": "https://example.test"})
        check("POST /api/key/qr correct token -> 200", st == 200 and headers.get_content_type() == "image/png",
            f"{st} {headers.get_content_type()}")
        if st == 200:
          import cv2  # noqa: F401
          import numpy as np
          img = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
          data, points, _ = cv2.QRCodeDetector().detectAndDecode(img)
          check("POST /api/key/qr encodes the node token in its QR payload",
                data == f"https://example.test/node#k={nid}.{tok}",
                f"decoded={data!r}")

        st, body = call("/api/key/rotate", {"node_id": "missing-node"})
        check("POST /api/key/rotate unknown node -> 404", st == 404, f"{st} {body}")

        st, body = call("/api/key/rotate", {"node_id": nid, "token": ""})
        check("POST /api/key/rotate local loopback bypass with empty token -> 200",
            st == 200 and body.get("ok") is True,
            f"{st} {body}")
        old_tok = tok
        new_tok = body.get("token")
        check("POST /api/key/rotate returns a plaintext replacement token", bool(new_tok) and new_tok != old_tok,
            f"old={old_tok!r} new={new_tok!r}")
        stored_tok = db.connect().execute("SELECT token FROM nodes WHERE id=?", (nid,)).fetchone()[0]
        check("POST /api/key/rotate persists the replacement token", stored_tok == new_tok, f"stored={stored_tok!r}")

        st, body = call("/api/node/whoami", {"node_id": nid}, token=old_tok)
        check("POST /api/key/rotate old token is rejected by the standard node-auth path",
            st == 401 and body.get("error") == "that key does not match this camera",
            f"{st} {body}")

        full_nid, full_tok = enroll("node-fullres")
        sid = make_sighting(full_nid, tier="public")
        st, body = call("/api/sighting/fullres", {"node_id": "missing-node", "id": sid, "snap_b64": make_jpeg_b64()})
        check("POST /api/sighting/fullres unknown node -> 404", st == 404, f"{st} {body}")

        st, body = call("/api/sighting/fullres", {"node_id": full_nid, "id": sid, "snap_b64": make_jpeg_b64()}, token="wrong")
        check("POST /api/sighting/fullres wrong token -> 401", st == 401, f"{st} {body}")

        st, body = call("/api/sighting/fullres", {"node_id": full_nid, "id": sid}, token=full_tok)
        check("POST /api/sighting/fullres tokenless SEC-02 acceptance path returns 400 no image",
            st == 400 and body.get("error") == "no image",
            f"{st} {body}")

        st, body = call("/api/sighting/fullres", {"node_id": full_nid, "id": sid, "snap_b64": make_jpeg_b64()}, token=full_tok)
        check("POST /api/sighting/fullres correct token -> 200", st == 200 and body.get("ok") is True,
            f"{st} {body}")
        row = db.sighting(sid)
        check("POST /api/sighting/fullres marks the sighting as full-resolution attached",
            bool(row and row.get("snap_full")), f"snap_full={row.get('snap_full') if row else None}")

        other_nid, other_tok = enroll("node-fullres-other")
        other_sid = make_sighting(other_nid, tier="public")
        st, body = call("/api/sighting/fullres", {"node_id": full_nid, "id": other_sid, "snap_b64": make_jpeg_b64()}, token=full_tok)
        check("POST /api/sighting/fullres other node's sighting -> 404", st == 404, f"{st} {body}")

        db.connect().execute("UPDATE sightings SET tier='private' WHERE id=?", (sid,))
        db.connect().commit()
        st, body = call("/api/sighting/fullres", {"node_id": full_nid, "id": sid, "snap_b64": make_jpeg_b64()}, token=full_tok)
        check("POST /api/sighting/fullres private-tier row -> 403", st == 403, f"{st} {body}")

        db.connect().execute("UPDATE sightings SET tier='public' WHERE id=?", (sid,))
        db.connect().commit()
        st, body = call("/api/sighting/fullres", {"node_id": full_nid, "id": sid, "snap_b64": make_jpeg_b64()}, token=full_tok)
        check("POST /api/sighting/fullres already attached -> 200 already=True", st == 200 and body.get("already") is True,
            f"{st} {body}")

        print(f"\nRESULT: {len(FAIL)} failures")
        if FAIL:
          for name in FAIL:
              print("  -", name)
          return 1
        return 0
    finally:
        srv.shutdown()
        srv.server_close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        shutil.rmtree(SCRATCH, ignore_errors=True)
