"""Stage 2C2 focused behavioral/authorization characterization for
operator_bugs.py routes (bug-report submission + operator management).

Protects externally observable behavior for the routes moved to
operator_bugs.py: /admin/bugs, /api/bug/list, /api/bug/shot/<id>, /api/bug
(POST), /api/bug/close, /api/bug/delete.

Establishes ACTUAL authorization behavior from executable code/tests rather
than inferring it from route names: /admin/bugs, /api/bug/list,
/api/bug/shot/<id>, /api/bug/close and /api/bug/delete all gate on
Handler._is_local() (operator_auth.check()) - the SAME mechanism every other
operator route in hub.py uses. /api/bug (submission) is deliberately
unauthenticated and only rate-limited.

Reuses the isolated-subprocess harness from test_hub_behavior.py.

Run directly:  python tools\\test_bugs_characterization.py
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_hub_behavior import HubInstance, _get, _post, check
import test_hub_behavior as thb


class _Ctx:
    """Minimal context-manager factory for config-override hub instances,
    matching the pattern used by test_hub_behavior.py's own main()."""
    def __init__(self, overrides):
        self.overrides = overrides
    def __enter__(self):
        self.hub = HubInstance(self.overrides)
        self.hub.start()
        return self.hub
    def __exit__(self, *exc):
        self.hub.stop()
        shutil.rmtree(self.hub.tmp, ignore_errors=True)


def t_admin_bugs_page_loopback(hub: HubInstance) -> None:
    print("\n== /admin/bugs from loopback (operator_requires_auth default off) ==")
    status, headers, body = _get(hub, "/admin/bugs")
    check("GET /admin/bugs from loopback -> 200 (trusts socket addr)",
          status == 200, str(status))
    check("GET /admin/bugs content-type is html",
          "html" in headers.get("Content-Type", ""), headers.get("Content-Type"))


def t_bug_list_loopback(hub: HubInstance) -> None:
    print("\n== /api/bug/list from loopback ==")
    status, headers, body = _get(hub, "/api/bug/list")
    check("GET /api/bug/list from loopback -> 200", status == 200, str(status))
    data = json.loads(body)
    check("/api/bug/list has 'bugs' key and it is a list",
          isinstance(data.get("bugs"), list), str(data))

    status, headers, body = _get(hub, "/api/bug/list?all=1")
    check("GET /api/bug/list?all=1 from loopback -> 200", status == 200, str(status))


def t_bug_shot_missing_loopback(hub: HubInstance) -> None:
    print("\n== /api/bug/shot/<id> missing screenshot, loopback ==")
    status, headers, body = _get(hub, "/api/bug/shot/nosuchid")
    check("GET /api/bug/shot/<unknown> from loopback -> 404",
          status == 404, str(status))


def t_bug_report_unauthenticated(hub: HubInstance):
    print("\n== POST /api/bug unauthenticated success/side-effect ==")
    status, headers, body = _post(hub, "/api/bug", json.dumps({
        "desc": "the map does not load on my phone",
        "page": "/", "shot": ""}).encode())
    check("POST /api/bug (no auth, valid desc) -> 200", status == 200, str(status))
    data = json.loads(body)
    check("POST /api/bug success -> {'ok': True, 'id': ...}",
          data.get("ok") is True and isinstance(data.get("id"), str), str(data))
    bug_id = data.get("id")

    # side effect: the new report is now visible via /api/bug/list (loopback).
    status, headers, body = _get(hub, "/api/bug/list")
    listing = json.loads(body).get("bugs", [])
    check("submitted bug report appears in /api/bug/list",
          any(b.get("id") == bug_id for b in listing), str(listing))
    return bug_id


def t_bug_report_malformed(hub: HubInstance) -> None:
    print("\n== POST /api/bug malformed/empty input ==")
    status, headers, body = _post(hub, "/api/bug", json.dumps({}).encode())
    check("POST /api/bug empty body -> 400 (bugs.save rejects blank desc)",
          status == 400, str(status))


def t_bug_close_delete(hub: HubInstance, bug_id: str) -> None:
    print("\n== /api/bug/close and /api/bug/delete (loopback, operator) ==")
    status, headers, body = _post(hub, "/api/bug/close",
                                   json.dumps({"id": bug_id}).encode())
    check("POST /api/bug/close from loopback -> 200", status == 200, str(status))
    data = json.loads(body)
    check("POST /api/bug/close known id -> ok:true", data.get("ok") is True, str(data))

    status, headers, body = _post(hub, "/api/bug/delete",
                                   json.dumps({"id": bug_id}).encode())
    check("POST /api/bug/delete from loopback -> 200", status == 200, str(status))
    data = json.loads(body)
    check("POST /api/bug/delete known id -> ok:true", data.get("ok") is True, str(data))

    # after delete, the id should no longer appear in the listing.
    status, headers, body = _get(hub, "/api/bug/list?all=1")
    listing = json.loads(body).get("bugs", [])
    check("deleted bug no longer in /api/bug/list?all=1",
          not any(b.get("id") == bug_id for b in listing), str(listing))


def t_bug_close_delete_unknown_id(hub: HubInstance) -> None:
    print("\n== /api/bug/close and /api/bug/delete for a nonexistent id ==")
    status, headers, body = _post(hub, "/api/bug/close",
                                   json.dumps({"id": "nosuchid"}).encode())
    check("POST /api/bug/close unknown id -> 200", status == 200, str(status))
    data = json.loads(body)
    check("POST /api/bug/close unknown id -> ok:false",
          data.get("ok") is False, str(data))

    status, headers, body = _post(hub, "/api/bug/delete",
                                   json.dumps({"id": "nosuchid"}).encode())
    check("POST /api/bug/delete unknown id -> 200", status == 200, str(status))
    data = json.loads(body)
    check("POST /api/bug/delete unknown id -> ok:false",
          data.get("ok") is False, str(data))


def t_bug_rate_limit(hub: HubInstance) -> None:
    print("\n== POST /api/bug rate limiting ==")
    # 🔎 FINDING (characterized, not fixed): bugs.py's OWN per-hour ceiling
    # (bugs.MAX_PER_HOUR = 60, checked inside bugs.save()) is stricter than
    # and fires before ratelimit.py's per-IP "/api/bug" bucket (120/hour) -
    # so flooding this route from a single test process is observed to
    # return HTTP 400 with an in-body "too many reports" error well before
    # any HTTP 429 from rate_ok() is reached. Both caps exist; only the
    # tighter one is externally reachable in practice from one IP within an
    # hour. This matches the existing behavior exactly as it was inline in
    # hub.py before this stage - not a regression introduced here.
    saw_400_too_many = False
    saw_429 = False
    last_status = None
    for _ in range(80):
        status, headers, body = _post(hub, "/api/bug", json.dumps({
            "desc": "flooding this on purpose", "page": "/"}).encode())
        last_status = status
        if status == 429:
            saw_429 = True
            break
        if status == 400 and b"too many" in body:
            saw_400_too_many = True
            break
    check("POST /api/bug flood eventually -> 429 or 400 'too many' "
          "(bugs.py's own 60/hour cap fires before ratelimit.py's 120/hour cap)",
          saw_429 or saw_400_too_many, f"last status {last_status}")


def t_operator_bug_routes_auth_required() -> None:
    print("\n== operator bug routes with operator_requires_auth=true ==")
    with _Ctx({"operator_requires_auth": True}) as hub:
        status, _, body = _get(hub, "/admin/bugs")
        check("operator_requires_auth=true, no token: GET /admin/bugs -> 403",
              status == 403, f"got {status}: {body[:200]}")
        status, _, body = _get(hub, "/api/bug/list")
        check("operator_requires_auth=true, no token: GET /api/bug/list -> 403",
              status == 403, f"got {status}: {body[:200]}")
        status, _, body = _get(hub, "/api/bug/shot/anything")
        check("operator_requires_auth=true, no token: GET /api/bug/shot/<id> -> 403",
              status == 403, f"got {status}: {body[:200]}")
        status, _, body = _post(hub, "/api/bug/close", json.dumps({"id": "x"}).encode())
        check("operator_requires_auth=true, no token: POST /api/bug/close -> 403",
              status == 403, f"got {status}: {body[:200]}")
        status, _, body = _post(hub, "/api/bug/delete", json.dumps({"id": "x"}).encode())
        check("operator_requires_auth=true, no token: POST /api/bug/delete -> 403",
              status == 403, f"got {status}: {body[:200]}")

        # /api/bug (submission) must remain reachable with NO token at all -
        # it is deliberately unauthenticated even when operator_requires_auth
        # is on, because operator auth only gates operator-facing routes.
        status, _, body = _post(hub, "/api/bug", json.dumps({
            "desc": "still reachable with no token", "page": "/"}).encode())
        check("operator_requires_auth=true, no token: POST /api/bug -> 200 "
              "(submission stays unauthenticated)",
              status == 200, f"got {status}: {body[:200]}")

        # now authenticate as operator and confirm the operator-only routes work.
        token_file = hub.tmp / "data" / "operator.token"
        _post(hub, "/login", json.dumps({"token": "wrong"}).encode())
        check("operator.token file created on first auth attempt",
              token_file.exists(), str(token_file))
        if token_file.exists():
            real_token = token_file.read_text(encoding="utf-8").strip()
            status, _, body = _get(hub, "/admin/bugs",
                                    headers={"Authorization": f"Bearer {real_token}"})
            check("operator_requires_auth=true, correct bearer token: "
                  "GET /admin/bugs -> 200",
                  status == 200, f"got {status}: {body[:200]}")
            status, _, body = _get(hub, "/api/bug/list",
                                    headers={"Authorization": f"Bearer {real_token}"})
            check("operator_requires_auth=true, correct bearer token: "
                  "GET /api/bug/list -> 200",
                  status == 200, f"got {status}: {body[:200]}")


def t_bug_routes_mirror_availability() -> None:
    print("\n== mirror availability for operator_bugs.py routes ==")
    with _Ctx({"public_mirror": True}) as hub:
        # These are NOT in mirror.route_allowed()'s exclusion list (which only
        # covers /review, /api/review, /api/operator, /api/purge, /api/audit),
        # so they remain reachable on a mirror. Characterized as observed, not
        # as might be assumed from "operator-only" framing.
        status, _, body = _get(hub, "/admin/bugs")
        check("public_mirror=true: GET /admin/bugs still reachable -> 200 "
              "(not in mirror's exclusion list, loopback still trusted)",
              status == 200, f"got {status}: {body[:200]}")
        status, _, body = _get(hub, "/api/bug/list")
        check("public_mirror=true: GET /api/bug/list still reachable -> 200",
              status == 200, f"got {status}: {body[:200]}")
        status, _, body = _post(hub, "/api/bug", json.dumps({
            "desc": "reachable on a mirror too", "page": "/"}).encode())
        check("public_mirror=true: POST /api/bug still reachable -> 200",
              status == 200, f"got {status}: {body[:200]}")


def main() -> int:
    print("Starting isolated hub instance (default config)...")
    with HubInstance() as hub:
        t_admin_bugs_page_loopback(hub)
        t_bug_list_loopback(hub)
        t_bug_shot_missing_loopback(hub)
        t_bug_report_malformed(hub)
        bug_id = t_bug_report_unauthenticated(hub)
        t_bug_close_delete_unknown_id(hub)
        t_bug_close_delete(hub, bug_id)

    print("Starting a fresh isolated hub instance for rate-limit flood test...")
    with HubInstance() as hub:
        t_bug_rate_limit(hub)

    # The flood loop above can exhaust local ephemeral ports on Windows
    # (TIME_WAIT); give the OS a moment before opening more connections,
    # matching the retry pattern already established for this suite family.
    import time
    time.sleep(20)

    t_operator_bug_routes_auth_required()
    t_bug_routes_mirror_availability()

    print(f"\n{thb.CHECKS} checks run, {len(thb.FAILURES)} failed.")
    if thb.FAILURES:
        print("\nFAILURES:")
        for f in thb.FAILURES:
            print(f"  - {f}")
        return 1
    print("bugs/operator characterization passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
