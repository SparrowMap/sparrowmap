"""node_lifecycle.py - Stage 2D2 route adapters.

Thin HTTP adapters for the node lifecycle/heartbeat routes, preserving the
existing route-local auth and persistence semantics from hub.py without
introducing a new node-auth abstraction. This stays deliberately in the route
adapter layer: domain logic and DB updates remain the same existing seams.
"""

from __future__ import annotations

import hmac

import db
from core import now
import node_auth


def heartbeat(handler):
    """POST /api/heartbeat - preserve existing heartbeats + fullres request."""
    b = handler._body()
    authenticated = node_auth.authenticate_node_bearer(
        str(b.get("node_id") or ""), handler.headers.get("Authorization")
    )
    if not authenticated.allowed:
        return handler._err(authenticated.status_code, authenticated.error)
    nd = authenticated.node
    if nd["status"] != "active":
        return handler._json({"ok": True, "posting": False,
                              "status": nd["status"],
                              "note": "this camera is not active, so "
                                      "its sightings are refused"})
    db.heartbeat(nd["id"])
    want = []
    try:
        want = db.wants_fullres(nd["id"])
    except Exception:
        want = []
    return handler._json({"ok": True, "posting": True, "ts": now(), "want_full": want})


def heartbeat_bulk(handler):
    """POST /api/heartbeat/bulk - preserve the bespoke batch auth + counts."""
    b = handler._body()
    items = b.get("nodes")
    if not isinstance(items, list):
        return handler._err(400, "nodes must be a list")
    if len(items) > 1000:
        return handler._err(413, "at most 1000 per request")
    ok, bad, inactive = [], 0, 0
    for it in items:
        if not isinstance(it, dict):
            bad += 1
            continue
        nd = db.node(str(it.get("node_id") or ""))
        if not nd:
            bad += 1
            continue
        tok = str(nd.get("token") or "")
        if tok and not hmac.compare_digest(str(it.get("token") or ""), tok):
            bad += 1
            continue
        if nd["status"] != "active":
            inactive += 1
            continue
        ok.append(nd["id"])
    db.heartbeat_many(ok)
    return handler._json({"ok": True,
                          "beat": len(ok),
                          "rejected": bad,
                          "inactive": inactive,
                          "ts": now()})


def node_progress(handler):
    """POST /api/node/progress - preserve the permissive header-only token check."""
    b = handler._body()
    authenticated = node_auth.authenticate_node_bearer(
        str(b.get("node_id") or ""), handler.headers.get("Authorization")
    )
    if not authenticated.allowed:
        return handler._err(authenticated.status_code, authenticated.error)
    nd = authenticated.node
    plat = str(b.get("platform") or "")[:12].lower()
    if plat not in ("ios", "android", "desktop", "other", ""):
        plat = "other"
    db.set_setup_stage(nd["id"], str(b.get("stage") or "")[:24], plat or None)
    return handler._json({"ok": True})
