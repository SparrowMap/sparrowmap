"""Stage 2C1 focused behavioral characterization for community.py routes
(help/community labelling + drive radar).

Protects externally observable behavior for the routes moved to community.py:
/help, /api/help/next, /api/help/stats, /api/help/img/<id>, /api/help/vote,
/drive, /api/drive/reports, /api/drive/report (410), /api/drive/vote.

Reuses the isolated-subprocess harness from test_hub_behavior.py.

Run directly:  python tools\\test_community_characterization.py
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_hub_behavior import HubInstance, _get, _post, check
import test_hub_behavior as thb


def t_help_page(hub: HubInstance) -> None:
    print("\n== /help page shell ==")
    status, headers, body = _get(hub, "/help")
    check("GET /help -> 200", status == 200, str(status))
    check("GET /help content-type is html",
          "html" in headers.get("Content-Type", ""), headers.get("Content-Type"))


def t_help_next_schema(hub: HubInstance) -> None:
    print("\n== /api/help/next schema ==")
    status, headers, body = _get(hub, "/api/help/next?voter=chartest")
    check("GET /api/help/next -> 200", status == 200, str(status))
    data = json.loads(body)
    check("/api/help/next returns a dict", isinstance(data, dict), str(data))
    # No task queue is guaranteed present in a fresh empty db; only assert
    # the shape is JSON-serializable and doesn't error.


def t_help_next_no_voter(hub: HubInstance) -> None:
    print("\n== /api/help/next without voter query param ==")
    status, headers, body = _get(hub, "/api/help/next")
    check("GET /api/help/next (no voter) -> 200", status == 200, str(status))
    json.loads(body)  # must still be valid JSON


def t_help_stats_schema(hub: HubInstance) -> None:
    print("\n== /api/help/stats schema ==")
    status, headers, body = _get(hub, "/api/help/stats")
    check("GET /api/help/stats -> 200", status == 200, str(status))
    data = json.loads(body)
    check("/api/help/stats returns a dict", isinstance(data, dict), str(data))


def t_help_img_missing(hub: HubInstance) -> None:
    print("\n== /api/help/img/<id> missing crop ==")
    status, headers, body = _get(hub, "/api/help/img/nosuchcrop")
    check("GET /api/help/img/<unknown> -> 404", status == 404, str(status))


def t_help_vote_malformed(hub: HubInstance) -> None:
    print("\n== /api/help/vote malformed input (preserved existing quirk) ==")
    status, headers, body = _post(hub, "/api/help/vote", json.dumps({}).encode())
    check("POST /api/help/vote empty body -> 200 (not 400)",
          status == 200, str(status))
    data = json.loads(body)
    check("POST /api/help/vote empty body -> in-body error, not HTTP status",
          isinstance(data, dict) and "error" in data, str(data))


def t_help_vote_unknown_item(hub: HubInstance) -> None:
    print("\n== /api/help/vote for a nonexistent item ==")
    status, headers, body = _post(hub, "/api/help/vote", json.dumps({
        "item": "nosuchitem", "label": "y", "voter": "chartest"}).encode())
    check("POST /api/help/vote unknown item -> 200", status == 200, str(status))
    data = json.loads(body)
    check("POST /api/help/vote unknown item -> in-body error",
          isinstance(data, dict) and "error" in data, str(data))


def t_drive_page(hub: HubInstance) -> None:
    print("\n== /drive page shell ==")
    status, headers, body = _get(hub, "/drive")
    check("GET /drive -> 200", status == 200, str(status))
    check("GET /drive content-type is html",
          "html" in headers.get("Content-Type", ""), headers.get("Content-Type"))


def t_drive_reports_schema(hub: HubInstance) -> None:
    print("\n== /api/drive/reports schema ==")
    status, headers, body = _get(hub, "/api/drive/reports")
    check("GET /api/drive/reports -> 200", status == 200, str(status))
    data = json.loads(body)
    check("/api/drive/reports has 'reports' key", "reports" in data, str(data))
    check("/api/drive/reports 'reports' is a list",
          isinstance(data.get("reports"), list), str(data))


def t_drive_report_disabled(hub: HubInstance) -> None:
    print("\n== /api/drive/report disabled/410 ==")
    status, headers, body = _post(hub, "/api/drive/report", json.dumps({
        "lat": 12.34, "lon": 56.78}).encode())
    check("POST /api/drive/report -> 410", status == 410, str(status))
    text = body.decode("utf-8", "replace") if isinstance(body, (bytes, bytearray)) else str(body)
    check("POST /api/drive/report 410 body mentions withdrawal",
          "withdrawn" in text, text)


def t_drive_vote_malformed_id(hub: HubInstance) -> None:
    print("\n== /api/drive/vote malformed id ==")
    status, headers, body = _post(hub, "/api/drive/vote", json.dumps({
        "id": "not-an-int", "still_there": True}).encode())
    check("POST /api/drive/vote bad id -> 400", status == 400, str(status))


def t_drive_vote_unknown_id(hub: HubInstance) -> None:
    print("\n== /api/drive/vote for a nonexistent report id ==")
    status, headers, body = _post(hub, "/api/drive/vote", json.dumps({
        "id": 999999, "still_there": True}).encode())
    check("POST /api/drive/vote unknown id -> 200", status == 200, str(status))
    data = json.loads(body)
    check("POST /api/drive/vote unknown id -> ok:false",
          data.get("ok") is False, str(data))


def t_drive_vote_rate_limit(hub: HubInstance) -> None:
    print("\n== /api/drive/vote rate limiting ==")
    saw_429 = False
    for _ in range(150):
        status, headers, body = _post(hub, "/api/drive/vote", json.dumps({
            "id": 1, "still_there": True}).encode())
        if status == 429:
            saw_429 = True
            break
    check("POST /api/drive/vote flood eventually -> 429", saw_429, "no 429 seen")


def t_mirror_availability(_Ctx) -> None:
    print("\n== mirror availability for community.py routes ==")
    with _Ctx({"public_mirror": True}) as hub:
        for path in ("/help", "/api/help/next", "/api/help/stats",
                     "/drive", "/api/drive/reports"):
            status, headers, body = _get(hub, path)
            check(f"public_mirror=true: {path} still reachable -> 200",
                  status == 200, str(status))
        status, headers, body = _post(hub, "/api/drive/report", json.dumps({}).encode())
        check("public_mirror=true: /api/drive/report still 410",
              status == 410, str(status))


def main() -> int:
    print("Starting isolated hub instance (default config)...")
    with HubInstance() as hub:
        t_help_page(hub)
        t_help_next_schema(hub)
        t_help_next_no_voter(hub)
        t_help_stats_schema(hub)
        t_help_img_missing(hub)
        t_help_vote_malformed(hub)
        t_help_vote_unknown_item(hub)
        t_drive_page(hub)
        t_drive_reports_schema(hub)
        t_drive_report_disabled(hub)
        t_drive_vote_malformed_id(hub)
        t_drive_vote_unknown_id(hub)

    print("Starting a fresh isolated hub instance for rate-limit flood test...")
    with HubInstance() as hub:
        t_drive_vote_rate_limit(hub)

    class _Ctx:
        def __init__(self, overrides):
            self.overrides = overrides
        def __enter__(self):
            self.hub = HubInstance(self.overrides)
            self.hub.start()
            return self.hub
        def __exit__(self, *exc):
            self.hub.stop()
            shutil.rmtree(self.hub.tmp, ignore_errors=True)

    t_mirror_availability(_Ctx)

    print(f"\n{thb.CHECKS} checks run, {len(thb.FAILURES)} failed.")
    if thb.FAILURES:
        print("\nFAILURES:")
        for f in thb.FAILURES:
            print(f"  - {f}")
        return 1
    print("community characterization passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
