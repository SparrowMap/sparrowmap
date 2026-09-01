"""Focused behavioral characterization for the Stage 2E1 reviewer read/session routes.

This suite exercises the inherited implementation before the reviewer read/session
route extraction. It intentionally ignores the production code layout and asserts
only externally observable behavior against an isolated temporary environment.

Run directly:
    python tools\test_reviewer_read_characterization.py
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

SCRATCH = Path(tempfile.mkdtemp(prefix="sparrow_reviewer_read_"))
for sub in ("snaps", "evidence", "held", "review", "inbox", "tiles"):
    (SCRATCH / sub).mkdir(parents=True, exist_ok=True)

import core  # noqa: E402
import db  # noqa: E402
import mirror  # noqa: E402
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

PORT = 8802
BASE = f"http://127.0.0.1:{PORT}"
UA = "SparrowMap-reviewer-read-test/1.0"
FAIL = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"  [{detail}]" if detail else ""))
    if not ok:
        FAIL.append(name)


def call(path: str, body=None, token: str = "", method: str = "GET", cookie: str = ""):
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


def ensure_reviewer_token(label: str = "tester"):
    tok = review_auth.issue(label, scope="pool")
    return tok


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

    review_tok = ensure_reviewer_token("tester")
    bad_tok = "not-a-reviewer-token"
    op_tok = "operator-token" if not core.CONFIG.get("operator_requires_auth") else None
    if op_tok is None:
        op_tok = "operator-token"
    # The operator credential is intentionally not a reviewer credential.
    try:
        operator_auth = __import__("operator_auth")
        op_tok = operator_auth.token() or "operator-token"
    except Exception:
        pass

    # /api/rv/me: no credential, bad credential, bearer, cookie, operator token, loopback no bypass.
    st, body = call("/api/rv/me", method="GET")
    check("GET /api/rv/me without reviewer credential -> 401",
          st == 401 and body.get("error") == "not signed in",
          f"{st} {body}")
    st, body = call("/api/rv/me", method="GET", token=bad_tok)
    check("GET /api/rv/me with invalid reviewer bearer -> 401",
          st == 401 and body.get("error") == "not signed in",
          f"{st} {body}")
    st, body = call("/api/rv/me", method="GET", token=review_tok)
    check("GET /api/rv/me with valid reviewer bearer -> 200",
          st == 200 and body.get("ok") is True and body.get("label") == "tester",
          f"{st} {body}")
    st, body = call("/api/rv/me", method="GET", cookie=f"sparrow_rv={review_tok}")
    check("GET /api/rv/me with valid reviewer cookie -> 200",
          st == 200 and body.get("ok") is True,
          f"{st} {body}")
    st, body = call("/api/rv/me", method="GET", token=op_tok)
    check("GET /api/rv/me with operator token alone -> 401",
          st == 401 and body.get("error") == "not signed in",
          f"{st} {body}")

    # /api/rv/queue is reviewer-authenticated; /api/review/queue is a separate
    # local-only operator surface that ignores reviewer bearer/cookie auth.
    for path in ("/api/rv/queue", "/api/review/queue"):
        st, body = call(path, method="GET")
        want_unauth = (path == "/api/rv/queue")
        check(f"GET {path} without reviewer credential -> {'401' if want_unauth else '200'}",
              (st == 401 and body.get("error") == "not signed in") if want_unauth
              else (st == 200 and isinstance(body, dict)),
              f"{st} {body}")
        st, body = call(path, method="GET", token=bad_tok)
        check(f"GET {path} with invalid reviewer bearer -> {'401' if want_unauth else '200'}",
              (st == 401 and body.get("error") == "not signed in") if want_unauth
              else (st == 200 and isinstance(body, dict)),
              f"{st} {body}")
        st, body = call(path, method="GET", token=review_tok)
        check(f"GET {path} with valid reviewer bearer -> 200",
              st == 200 and isinstance(body, dict),
              f"{st} {body}")
        st, body = call(path, method="GET", cookie=f"sparrow_rv={review_tok}")
        check(f"GET {path} with valid reviewer cookie -> 200",
              st == 200 and isinstance(body, dict),
              f"{st} {body}")

    # /api/rv/contributed: read-only counts, reviewer-only auth.
    st, body = call("/api/rv/contributed", method="GET")
    check("GET /api/rv/contributed without reviewer credential -> 401",
          st == 401 and body.get("error") == "not signed in",
          f"{st} {body}")
    st, body = call("/api/rv/contributed", method="GET", token=review_tok)
    check("GET /api/rv/contributed with valid reviewer bearer -> 200",
          st == 200 and isinstance(body, dict),
          f"{st} {body}")

    # /api/rv/progress: read-only status metadata; no operator bypass.
    st, body = call("/api/rv/progress", method="GET")
    check("GET /api/rv/progress without reviewer credential -> 401",
          st == 401 and body.get("error") == "not signed in",
          f"{st} {body}")
    st, body = call("/api/rv/progress", method="GET", token=review_tok)
    check("GET /api/rv/progress with valid reviewer bearer -> 200",
          st == 200 and isinstance(body, dict),
          f"{st} {body}")

    # /api/rv/login and /api/rv/logout: session establishment only.
    st, body = call("/api/rv/login", body={"token": "bad"}, method="POST")
    check("POST /api/rv/login with invalid token -> 401",
          st == 401 and body.get("error") == "invalid token",
          f"{st} {body}")
    st, body = call("/api/rv/login", body={"token": review_tok}, method="POST")
    check("POST /api/rv/login with valid reviewer token -> 200",
          st == 200 and body.get("ok") is True,
          f"{st} {body}")
    st, body = call("/api/rv/logout", method="POST")
    check("POST /api/rv/logout without JSON content-type -> 415",
          st == 415 and body.get("error") == "state-changing requests must be application/json",
          f"{st} {body}")

    # Mirror exclusion: private review surfaces are not available on a mirror.
    old = core.CONFIG.get("public_mirror")
    core.CONFIG["public_mirror"] = True
    st, body = call("/api/review/queue", method="GET", token=review_tok)
    check("GET /api/review/queue on public mirror -> route unavailable/404",
          st == 404 or (isinstance(body, dict) and body.get("error") == "not found"),
          f"{st} {body}")
    core.CONFIG["public_mirror"] = old

    srv.shutdown()
    srv.server_close()
    print()
    if FAIL:
        print(f"{len(FAIL)} FAILED: " + ", ".join(FAIL))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        shutil.rmtree(SCRATCH, ignore_errors=True)
    sys.exit(code)
