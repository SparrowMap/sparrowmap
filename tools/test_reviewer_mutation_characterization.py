"""Focused behavioral characterization for Stage 2E2 reviewer mutation/evidence routes.

This suite exercises the inherited implementation before the Stage 2E2 route
extraction. It is intentionally narrow: it captures the review-auth boundary,
reviewer mutation outcomes, and the evidence deletion paths that Stage 2E2 will
move while leaving the surrounding application/domain logic in place.

Run directly:
    python tools\test_reviewer_mutation_characterization.py
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

SCRATCH = Path(tempfile.mkdtemp(prefix="sparrow_reviewer_mutation_"))
for sub in ("snaps", "evidence", "held", "review", "inbox", "tiles"):
    (SCRATCH / sub).mkdir(parents=True, exist_ok=True)

import core  # noqa: E402
import db  # noqa: E402
import mirror  # noqa: E402
import review_auth  # noqa: E402

REAL_DB = Path(db.DB_PATH)
for _mod, _name, _val in [
    (core, "DATA", SCRATCH),
    (core, "SNAPS", SCRATCH / "snaps"),
    (core, "EVIDENCE", SCRATCH / "evidence"),
    (core, "HELD", SCRATCH / "held"),
    (core, "DB_PATH", SCRATCH / "sparrow.db"),
    (db, "DB_PATH", SCRATCH / "sparrow.db"),
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

PORT = 8815
BASE = f"http://127.0.0.1:{PORT}"
UA = "SparrowMap-reviewer-mutation-test/1.0"
FAIL = []
TOTAL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global TOTAL
    TOTAL += 1
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"  [{detail}]" if detail else ""))
    if not ok:
        FAIL.append(name)


def call(path: str, body=None, token: str = "", method: str = "POST", cookie: str = ""):
    hdrs = {"User-Agent": UA}
    data = None
    if body is not None:
        hdrs["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    if token:
        hdrs["Authorization"] = "Bearer " + token
    if cookie:
        hdrs["Cookie"] = cookie
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


def png_bytes() -> bytes:
    try:
        from PIL import Image
    except Exception:
        return b"not-a-real-jpeg"
    img = Image.new("RGB", (64, 32), color=(255, 0, 0))
    out = __import__("io").BytesIO()
    img.save(out, format="JPEG")
    return out.getvalue()


def add_row(sid: int, node_id: str = "n_demo", snap: str = "demo.jpg") -> None:
    conn = db.connect()
    conn.execute(
        "INSERT OR REPLACE INTO nodes (id, name, token, kind, status, lat, lon, created) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (node_id, "demo camera", "camera-secret", "fixed", "active", 1.0, 2.0, time.time()),
    )
    conn.execute(
        "INSERT OR REPLACE INTO sightings (id, node_id, ts, lat, lon, tier, plate_hash, plate_text, vclass, source, snap, reviewed, snap_held) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (sid, node_id, time.time(), 1.0, 2.0, "public", "aaa", "ABC123", "police", "camera", snap, None, None),
    )
    conn.commit()
    meta = {"id": sid, "ts": time.time(), "node_id": node_id, "node_name": "demo camera", "vclass": "police", "why": "unit test"}
    (mirror.REVIEW / f"{sid}.json").write_text(json.dumps(meta), encoding="utf-8")
    (mirror.REVIEW / f"{sid}.jpg").write_bytes(png_bytes())


def main() -> int:
    srv = hub.ThreadingHTTPServer(("127.0.0.1", PORT), hub.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    for _ in range(50):
        try:
            urllib.request.urlopen(urllib.request.Request(BASE + "/api/health", headers={"User-Agent": UA}), timeout=2)
            break
        except Exception:
            time.sleep(0.1)
    else:
        print("server never came up")
        return 2

    review_tok = review_auth.issue("tester", scope="pool")
    bad_tok = "not-a-reviewer-token"

    sid = 20001
    add_row(sid)

    st, body = call("/api/rv/verdict", body={"id": sid, "verdict": "cop"})
    check("POST /api/rv/verdict without reviewer credential -> 401",
          st == 401 and body.get("error") == "not signed in",
          f"{st} {body}")

    st, body = call("/api/rv/verdict", body={"id": sid, "verdict": "cop"}, token=bad_tok)
    check("POST /api/rv/verdict with invalid reviewer bearer -> 401",
          st == 401 and body.get("error") == "not signed in",
          f"{st} {body}")

    st, body = call("/api/rv/verdict", body={"id": sid, "verdict": "cop"}, token=review_tok)
    check("POST /api/rv/verdict with valid reviewer bearer -> 200 and verdict mutation",
          st == 200 and body.get("ok") is True and body.get("verdict") == "cop",
          f"{st} {body}")

    held_id = 20002
    add_row(held_id, snap="held_demo.jpg")
    db.set_snap_held(held_id, "held_demo.jpg", None)
    (core.HELD / "held_demo.jpg").write_bytes(png_bytes())

    st, body = call("/api/rv/held/fix", body={"id": held_id, "action": "delete"})
    check("POST /api/rv/held/fix without reviewer credential -> 401",
          st == 401 and body.get("error") == "not signed in",
          f"{st} {body}")

    st, body = call("/api/rv/held/fix", body={"id": held_id, "action": "delete"}, token=review_tok)
    check("POST /api/rv/held/fix with valid reviewer bearer -> 200 and deletion action",
          st == 200 and body.get("ok") is True and body.get("action") == "delete",
          f"{st} {body}")

    retracted_id = 20003
    add_row(retracted_id, snap="retract_demo.jpg")
    conn = db.connect()
    conn.execute("UPDATE sightings SET reviewed='retracted', snap='retract_demo.jpg' WHERE id=?", (retracted_id,))
    conn.commit()
    (core.SNAPS / "retract_demo.jpg").write_bytes(png_bytes())

    st, body = call("/api/rv/retracted/delete", body={"id": retracted_id})
    check("POST /api/rv/retracted/delete without reviewer credential -> 401",
          st == 401 and body.get("error") == "not signed in",
          f"{st} {body}")

    st, body = call("/api/rv/retracted/delete", body={"id": retracted_id}, token=review_tok)
    check("POST /api/rv/retracted/delete with valid reviewer bearer -> 200 and row clears photo",
          st == 200 and body.get("ok") is True and body.get("id") == retracted_id,
          f"{st} {body}")

    srv.shutdown()
    srv.server_close()
    print(f"RESULT: {TOTAL - len(FAIL)}/{TOTAL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
