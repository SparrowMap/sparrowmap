"""Tell the operator a bug report arrived. Say NOTHING about what is in it.

    python tools/bug_alert.py <report-id>

🚨 THE ALERT REPO MUST BE EXPLICITLY CONFIGURED.
deploy/sparrowmap-watch.sh files outage alerts as GitHub issues only when a
Raven-configured repository is supplied; it will not default to upstream.

The consequence is absolute: this may carry an ID, a page path and a timestamp,
and nothing else. Not the description, not the screenshot, not the reporter's
user agent. A person reporting a bug is very often holding a screenshot of their
own camera key, and publishing it to help them would be worse than never
answering.

The operator opens /admin/bugs to read the actual report, which is behind
operator authentication and served from a directory no other route touches.

Set RAVEN_ALERT_REPO to a PRIVATE repository if you want more than an id in
the notification. Legacy SPARROW_ALERT_REPO remains accepted. This file still
will not send the image.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import bugs  # noqa: E402

TOKEN_FILE = (os.environ.get("RAVEN_GH_TOKEN_FILE") or
              os.environ.get("SPARROW_GH_TOKEN_FILE") or
              "/etc/sparrowmap/github_token")
REPO = os.environ.get("RAVEN_ALERT_REPO") or os.environ.get("SPARROW_ALERT_REPO")
PRIVATE = (os.environ.get("RAVEN_ALERT_REPO_IS_PRIVATE") or
           os.environ.get("SPARROW_ALERT_REPO_IS_PRIVATE") or "") == "1"


def token() -> str:
    p = Path(TOKEN_FILE)
    if not p.exists():
        return ""
    # A token written from PowerShell arrives with a BOM and CRLF - 45 bytes for
    # a 40-character token. Strip rather than wonder why GitHub says 401.
    return p.read_text(encoding="utf-8-sig").strip()


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: bug_alert.py <report-id>", file=sys.stderr)
        return 2
    bid = sys.argv[1]
    if not REPO:
        print("no alert repository configured; set RAVEN_ALERT_REPO "
              "(legacy SPARROW_ALERT_REPO is also accepted)", file=sys.stderr)
        return 1
    rec = bugs.get(bid)
    if not rec:
        print(f"no such report: {bid}", file=sys.stderr)
        return 1

    tok = token()
    if not tok:
        print(f"[bug] report {bid} stored, but NOBODY WAS TOLD: no token at "
              f"{TOKEN_FILE}", file=sys.stderr)
        return 1

    body = [
        f"A bug report arrived: **{bid}**",
        "",
        f"- screenshot attached: {'yes' if rec.get('shot') else 'no'}",
        f"- page: `{rec.get('page') or 'unknown'}`",
        "",
        "Read it on **/admin/bugs** (operator sign-in required).",
        "",
        "> The description and any screenshot are deliberately not in this "
        "issue. This repository is public, and a screenshot from somebody "
        "having trouble with SparrowMap often contains their own camera key.",
    ]
    if PRIVATE and rec.get("desc"):
        body.insert(2, f"\n> {rec['desc'][:1500]}\n")

    payload = json.dumps({
        "title": f"Bug report {bid}"
                 + (" (with screenshot)" if rec.get("shot") else ""),
        "body": "\n".join(body),
        "labels": ["bug-report"],
    }).encode()
    req = urllib.request.Request(
        f"https://api.github.com/repos/{REPO}/issues", data=payload,
        method="POST",
        headers={"Authorization": f"Bearer {tok}",
                 "Accept": "application/vnd.github+json",
                 "User-Agent": "SparrowMap-bug-alert/1.0",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            out = json.loads(r.read() or b"{}")
        print(f"[bug] alerted: {out.get('html_url')}")
        return 0
    except urllib.error.HTTPError as e:
        print(f"[bug] ALERT FAILED for {bid}: {e.code} {e.read()[:200]}",
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
