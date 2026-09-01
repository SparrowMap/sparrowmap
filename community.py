"""community.py - Stage 2C1 route adapters: help/community labelling and
the driver-radar "drive" page family.

This is a ROUTE ADAPTER module, not an application-service layer, matching
pages.py (Stage 2A) and mapdata.py (Stage 2B). It owns path/query/body
parsing and HTTP-level response mapping for two small, already-decoupled
route families:

  * community crop-labelling: /help, /api/help/next, /api/help/stats,
    /api/help/img/<id>, /api/help/vote - all of which already delegate to
    help_api.py's existing seams (items/next_for/record/image/stats). No
    domain logic lived inline in hub.py for these; help_api.py already is
    the application-service layer for this feature, so this stage only
    moves the HTTP glue around it.
  * the "drive" radar reads/votes: /drive, /api/drive/reports,
    /api/drive/report (disabled/410), /api/drive/vote - all of which
    already delegate to db.py seams (active_driver_reports/
    vote_driver_report) or are a fixed disabled-route response.

Moved verbatim (smallest possible semantic change) from hub.py's
_do_GET_inner / _do_POST_inner:
  GET  /help, /api/help/next, /api/help/stats, /api/help/img/<id>
  GET  /drive, /api/drive/reports
  POST /api/help/vote
  POST /api/drive/report, /api/drive/vote

Each function takes the same duck-typed handler object every other route
module takes (only handler._file/handler._json/handler._err/handler._body/
handler.client_ip are used here), so hub.py's dispatch chain can call
straight into this module at the exact position each route currently
occupies, preserving first-match-wins ordering across the hub.py/community.py
boundary.

Does not import hub. Stdlib plus db/help_api/ratelimit/core only.
"""

from __future__ import annotations

from pathlib import Path

import db
import help_api
from core import PUBLIC
from ratelimit import rate_ok


# ---------------------------------------------------------------------------
# Community crop-labelling (/help, /api/help/*)
#
# 🚨 COMMUNITY LABELLING. Public on purpose, and safe because of what it
# cannot do rather than who it lets in: a vote lands in a SEPARATE database
# file with no sightings table, it never becomes a label here, and every crop
# in a task is from a PUBLIC traffic camera carrying an opaque id with no
# day, node, time or place. See help_api.py, which exists to hold those
# limits in one place.
# ---------------------------------------------------------------------------

def help_page(handler) -> None:
    """GET /help - moved verbatim from hub.py."""
    return handler._file(PUBLIC / "help.html")


def help_next(handler, query: dict) -> None:
    """GET /api/help/next - moved verbatim from hub.py."""
    voter = (query.get("voter") or [""])[0]
    return handler._json(help_api.next_for(voter))


def help_stats(handler) -> None:
    """GET /api/help/stats - moved verbatim from hub.py."""
    return handler._json(help_api.stats())


def help_img(handler, path: str) -> None:
    """GET /api/help/img/<id> - moved verbatim from hub.py."""
    raw = help_api.image(path[len("/api/help/img/"):])
    if raw is None:
        return handler._err(404, "no such crop")
    return handler._send(200, raw, "image/jpeg")


def help_vote(handler) -> None:
    """POST /api/help/vote - moved verbatim from hub.py.

    A stranger's judgement about one crop. It is written to label_votes.db
    and nowhere else - not to sightings, not to the bank. Consensus and the
    decision happen later, on his machine.

    NOTE (preserved existing behavior): help_api.record() reports validation
    failures via an in-body {"error": ...} value, not a non-200 status - this
    wrapper does not add a status check that was not there before.
    """
    b = handler._body()
    return handler._json(help_api.record(
        str(b.get("item") or ""), str(b.get("label") or ""),
        str(b.get("voter") or "")))


# ---------------------------------------------------------------------------
# "Drive" radar (/drive, /api/drive/*)
#
# A separate, token-less reviewer-adjacent app: live crowd reports of where a
# patrol was recently confirmed, read like the map (public, ephemeral,
# unverified by construction). /api/drive/report (submitting a NEW report
# from a tap) was closed 2026-08-15 - see drive_report()'s docstring, carried
# over unchanged - and reads/votes stay open so reports already on the layer
# age out normally.
# ---------------------------------------------------------------------------

def drive_page(handler) -> None:
    """GET /drive - moved verbatim from hub.py."""
    return handler._file(PUBLIC / "drive.html")


def drive_reports(handler) -> None:
    """GET /api/drive/reports - moved verbatim from hub.py.

    Live crowd reports for the driving radar. Public read, like the map.
    Ephemeral and unverified by construction.
    """
    return handler._json({"reports": db.active_driver_reports()})


def drive_report(handler) -> None:
    """POST /api/drive/report - moved verbatim from hub.py.

    🚨 CLOSED 2026-08-15. His call, and the right one.

    It let anybody POST "there is a patrol at this coordinate" - no camera,
    no photograph, no review, no identity. Every other route onto this map
    puts a human in front of a picture before a police marker appears; this
    one asserted a vehicle from a pair of numbers, and the numbers were
    supplied by the caller. Sending someone a false "police ahead", or
    clearing a street by covering it in fake ones, cost one request.

    ⚠️ THE BUTTON WAS NOT THE VULNERABILITY. drive.html was this route's
    only caller, so deleting the button and leaving the route open would
    have removed the feature from honest users and left it working for
    everybody else - which is worse than doing nothing, because it looks
    fixed.

    Reads and votes stay open so reports already on the layer age out
    normally. Re-enabling means restoring the body; the db helper
    (add_driver_report) is untouched.
    """
    return handler._err(410, "reporting a patrol by tapping has been "
                             "withdrawn - sightings come from cameras")


def drive_vote(handler, path: str) -> None:
    """POST /api/drive/vote - moved verbatim from hub.py."""
    if not rate_ok(path, handler.client_ip):
        return handler._err(429, "voting too fast")
    b = handler._body()
    try:
        rid = int(b.get("id"))
    except (TypeError, ValueError):
        return handler._err(400, "bad id")
    ok = db.vote_driver_report(rid, bool(b.get("still_there")))
    return handler._json({"ok": ok})
