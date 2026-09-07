"""reviewer_mutation.py - Stage 2E2 reviewer mutation/evidence route adapters.

Thin HTTP adapters for the reviewer mutation routes that remain in the hub
boundary for this stage: review verdict submission and evidence/photo actions.
The domain logic stays in review_api.py and related storage modules.
"""

from __future__ import annotations

import review_api
import review_auth
import privacy


def rv_retracted_delete(handler):
    """POST /api/rv/retracted/delete - preserve reviewer auth and retracted-photo deletion."""
    r = review_auth.identify(handler.headers)
    if not r:
        return handler._err(401, "not signed in")
    b = handler._body()
    try:
        sid = int(b.get("id"))
    except (TypeError, ValueError):
        return handler._err(400, "bad id")
    return handler._json(review_api.delete_retracted_photo(
        r, sid, privacy.audit_ip(handler.client_ip)))


def rv_held_fix(handler):
    """POST /api/rv/held/fix - preserve reviewer auth and held-photo actions."""
    r = review_auth.identify(handler.headers)
    if not r:
        return handler._err(401, "not signed in")
    b = handler._body()
    try:
        sid = int(b.get("id"))
    except (TypeError, ValueError):
        return handler._err(400, "bad id")
    crop = b.get("crop")
    return handler._json(review_api.fix_photo(
        r, sid, b.get("action") or "",
        crop if isinstance(crop, dict) else None,
        privacy.audit_ip(handler.client_ip)))


def rv_verdict(handler):
    """POST /api/rv/verdict - preserve reviewer auth and verdict mutation semantics."""
    r = review_auth.identify(handler.headers)
    if not r:
        return handler._err(401, "not signed in")
    b = handler._body()
    try:
        sid = int(b.get("id"))
    except (TypeError, ValueError):
        return handler._err(400, "bad id")
    box = b.get("crop")
    return handler._json(review_api.verdict(
        r, sid, b.get("verdict"), privacy.audit_ip(handler.client_ip),
        crop_box=box if isinstance(box, dict) else None))
