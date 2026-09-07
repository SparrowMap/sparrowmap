"""Focused behavioral characterization for the Stage 2E3 operator/admin routes.

This suite exercises the inherited implementation before the operator/admin
route extraction. It intentionally ignores production code layout and asserts
only externally observable behavior against an isolated temporary environment.

Run directly:
    python tools\test_operator_admin_characterization.py --label PRE-2E3
"""

from __future__ import annotations

import argparse
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

SCRATCH = Path(tempfile.mkdtemp(prefix="sparrow_operator_admin_"))
for sub in ("snaps", "evidence", "held", "review", "inbox", "tiles"):
    (SCRATCH / sub).mkdir(parents=True, exist_ok=True)

import core  # noqa: E402
import db  # noqa: E402
import mirror  # noqa: E402
import operator_auth  # noqa: E402
import review_api  # noqa: E402
import review_auth  # noqa: E402
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

PORT = 8815
BASE = f"http://127.0.0.1:{PORT}"
UA = "SparrowMap-operator-admin-test/1.0"
FAIL = []
TOTAL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global TOTAL
    TOTAL += 1
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"  [{detail}]" if detail else ""))
    if not ok:
        FAIL.append(name)


def call(path: str, *, body=None, token: str = "", cookie: str = "", method: str = "GET"):
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", default="PRE-2E3")
    args = parser.parse_args()

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
        srv.shutdown()
        srv.server_close()
        return 2

    # We intentionally vary deployment auth mode to exercise the inheritance.
    op_tok = operator_auth.token() or "operator-token"
    review_tok = review_auth.issue("tester", scope="pool")

    # Default deployment: operator auth off, loopback local is accepted and login is informational.
    st, body = call("/api/operator/login", method="POST", body={"token": "wrong"})
    check("POST /api/operator/login when auth is off -> 200 informational response",
          st == 200 and body.get("ok") is True and body.get("note") == "auth is off",
          f"{st} {body}")

    st, body = call("/api/rv/tokens")
    check("GET /api/rv/tokens from loopback with auth off -> 200",
          st == 200 and isinstance(body, dict) and isinstance(body.get("tokens"), list),
          f"{st} {body}")

    # operator_requires_auth=true: local loopback must not bypass auth once required.
    core.CONFIG["operator_requires_auth"] = True
    st, body = call("/api/rv/tokens")
    check("GET /api/rv/tokens with operator_requires_auth=true and no token -> 403",
          st == 403 and body.get("error") == "operator only",
          f"{st} {body}")

    st, body = call("/api/rv/tokens", token=review_tok)
    check("GET /api/rv/tokens with reviewer bearer alone when operator auth required -> 403",
          st == 403 and body.get("error") == "operator only",
          f"{st} {body}")

    st, body = call("/api/rv/tokens", token=op_tok)
    check("GET /api/rv/tokens with valid operator bearer when operator auth required -> 200",
          st == 200 and isinstance(body, dict) and isinstance(body.get("tokens"), list),
          f"{st} {body}")

    st, body = call("/api/operator/login", method="POST", body={"token": "wrong"})
    check("POST /api/operator/login with wrong token when auth is required -> 401",
          st == 401 and body.get("error") == "wrong token",
          f"{st} {body}")

    st, body = call("/api/operator/login", method="POST", body={"token": op_tok})
    check("POST /api/operator/login with valid token -> 200 and session cookie",
          st == 200 and body.get("ok") is True,
          f"{st} {body}")

    # The route writes the session cookie in the response headers, not in the JSON body.
    st, headers, body = None, {}, None
    req = urllib.request.Request(BASE + "/api/operator/login", method="POST", data=json.dumps({"token": op_tok}).encode(), headers={"User-Agent": UA, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            st = r.status
            headers = dict(r.headers)
            body = json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        st = e.code
        headers = dict(e.headers)
        body = json.loads(e.read() or b"{}")
    check("POST /api/operator/login sets cookie header on success",
          st == 200 and "sparrow_op=" in (headers.get("Set-Cookie") or ""),
          f"{st} headers={headers}")

    st, body = call("/api/operator/logout", method="POST", body={})
    check("POST /api/operator/logout -> 200 and clears cookie",
          st == 200 and body.get("ok") is True,
          f"{st} {body}")

    # Use raw response headers to confirm the clear cookie is emitted.
    req = urllib.request.Request(BASE + "/api/operator/logout", method="POST", data=json.dumps({}).encode(), headers={"User-Agent": UA, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            st = r.status
            headers = dict(r.headers)
            body = json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        st = e.code
        headers = dict(e.headers)
        body = json.loads(e.read() or b"{}")
    check("POST /api/operator/logout emits clear cookie",
          st == 200 and "Max-Age=0" in (headers.get("Set-Cookie") or ""),
          f"{st} headers={headers}")

    st, body = call("/api/rv/tokens/new", method="POST", body={"label": "test", "scope": "pool", "nodes": []}, token=review_tok)
    check("POST /api/rv/tokens/new with reviewer bearer alone -> 403",
          st == 403 and body.get("error") == "operator only",
          f"{st} {body}")

    st, body = call("/api/rv/tokens/new", method="POST", body={"label": "test", "scope": "pool", "nodes": []}, token=op_tok)
    check("POST /api/rv/tokens/new with valid operator bearer -> 200",
          st == 200 and body.get("ok") is True and body.get("token"),
          f"{st} {body}")

    st, body = call("/api/rv/tokens/revoke", method="POST", body={"id": 1}, token=op_tok)
    check("POST /api/rv/tokens/revoke with valid operator bearer -> 200",
          st == 200 and isinstance(body, dict) and "ok" in body,
          f"{st} {body}")

    # A route that is operator-only and simple enough to be a thin adapter.
    st, body = call("/api/purge", method="POST", body={})
    check("POST /api/purge from loopback with auth required -> 403 when operator auth is required",
          st == 403 and body.get("error") == "operator only",
          f"{st} {body}")

    st, body = call("/api/purge", method="POST", body={}, token=op_tok)
    check("POST /api/purge with valid operator bearer -> 200",
          st == 200 and isinstance(body, dict),
          f"{st} {body}")

    print(f"{args.label}: {len([1 for x in range(TOTAL) if True]) - len(FAIL)}/{TOTAL}")
    srv.shutdown()
    srv.server_close()
    try:
        shutil.rmtree(SCRATCH, ignore_errors=True)
    except Exception:
        pass
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
