"""operator_bugs.py - Stage 2C2 route adapters: bug-report submission and
its operator-only management (/admin/bugs, /api/bug/*).

This is a ROUTE ADAPTER module, matching pages.py/mapdata.py/community.py:
it owns path matching, query/body parsing, invocation of the EXISTING
authentication check (Handler._is_local(), unchanged), and response mapping.
It does not hold any bug-report persistence/redaction logic itself - all of
that already lives in bugs.py, which remains the sole behavioral authority
for report storage, screenshot re-encoding, listing, closing and deletion.

Routes moved verbatim (smallest possible semantic change) from hub.py:

  GET  /admin/bugs         - operator page shell.
  GET  /api/bug/list       - operator listing.
  GET  /api/bug/shot/<id>  - operator-only screenshot bytes.
  POST /api/bug            - UNAUTHENTICATED report submission (rate-limited).
  POST /api/bug/close      - operator close.
  POST /api/bug/delete     - operator delete.

AUTHORIZATION, characterized (not inferred from naming) before this move:
  - /admin/bugs, /api/bug/list, /api/bug/shot/<id>, /api/bug/close,
    /api/bug/delete all gate on handler._is_local() - the SAME
    loopback/LAN/Tailscale-or-authenticated-operator-token check used by
    every other operator route in hub.py (operator_auth.check() under the
    hood). This wrapper invokes handler._is_local() exactly as hub.py did;
    it does not reimplement or duplicate that check.
  - /api/bug IS DELIBERATELY UNAUTHENTICATED. See its docstring below - the
    people most likely to report a bug are the ones locked out, so it is
    rate-limited and size/format-bounded instead of gated on identity.

Does not import hub. Stdlib plus bugs/core/ratelimit only.
"""

from __future__ import annotations

import subprocess
import sys
from urllib.parse import parse_qs, urlparse

import bugs
import core
from core import PUBLIC
from ratelimit import rate_ok


def admin_bugs_page(handler) -> None:
    """GET /admin/bugs - moved verbatim from hub.py.

    The way back in for a camera whose browser lost its key. See
    /api/node/whoami for what was actually happening to these people.
    """
    if not handler._is_local():
        return handler._err(403, "operator only")
    return handler._file(PUBLIC / "bugs.html")


def bug_list(handler, path: str) -> None:
    """GET /api/bug/list - moved verbatim from hub.py."""
    if not handler._is_local():
        return handler._err(403, "operator only")
    q = parse_qs(urlparse(path).query)
    return handler._json({"bugs": bugs.listing(
        limit=200, include_closed=(q.get("all", ["0"])[0] == "1"))})


def bug_shot(handler, path: str) -> None:
    """GET /api/bug/shot/<id> - moved verbatim from hub.py.

    🚨 OPERATOR ONLY, AND NOT IN SNAPS. A reporter's screenshot can contain
    their own camera key or the QR that is their key. It is served from
    core.BUGS, which no other route touches, so a leaked filename reaches
    nothing.
    """
    if not handler._is_local():
        return handler._err(403, "operator only")
    raw = bugs.shot_bytes(path.rsplit("/", 1)[-1])
    if not raw:
        return handler._err(404, "no screenshot")
    return handler._send(200, raw, "image/jpeg")


def bug_report(handler) -> None:
    """POST /api/bug - moved verbatim from hub.py.

    🚨 UNAUTHENTICATED ON PURPOSE, AND BOUNDED BECAUSE OF IT. The people
    most likely to report a bug are the ones who cannot get in - a
    volunteer whose key is gone, somebody whose browser will not install
    the app. Requiring a login to report "I cannot log in" is how a report
    never arrives.

    The cost of that is an open upload on a 3 GB box, so: a rate bucket, a
    size cap checked before anything is decoded, a re-encode that strips
    EXIF, a ceiling on how many reports can exist per hour, and a TTL
    sweep. See bugs.py.
    """
    if not rate_ok("/api/bug", handler.client_ip):
        return handler._err(429, "too many reports right now - "
                                 "please try again in a few minutes")
    b = handler._body()
    out = bugs.save(str(b.get("desc") or ""),
                     str(b.get("shot") or ""),
                     page=str(b.get("page") or ""),
                     ua=(handler.headers.get("User-Agent") or ""))
    if out.get("error"):
        return handler._err(400, out["error"])
    # Tell the operator it exists. The alert carries an ID and NOTHING
    # ELSE - the default alert repo is the public one.
    try:
        subprocess.Popen(
            [sys.executable, str(core.DATA.parent / "tools" / "bug_alert.py"),
             out["id"]],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass          # a failed alert must never lose the report
    return handler._json({"ok": True, "id": out["id"]})


def bug_close(handler) -> None:
    """POST /api/bug/close - moved verbatim from hub.py."""
    if not handler._is_local():
        return handler._err(403, "operator only")
    b = handler._body()
    return handler._json({"ok": bugs.close(str(b.get("id") or ""))})


def bug_delete(handler) -> None:
    """POST /api/bug/delete - moved verbatim from hub.py."""
    if not handler._is_local():
        return handler._err(403, "operator only")
    b = handler._body()
    return handler._json({"ok": bugs.delete(str(b.get("id") or ""))})
