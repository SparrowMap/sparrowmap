"""operator_admin.py - Stage 2E3 operator and reviewer-token route adapters.

Thin HTTP adapters for the operator login/session and token-administration
routes that are already intentionally scoped by the existing operator auth
model. The application/domain logic stays in operator_auth.py, review_api.py
and privacy.py.
"""

from __future__ import annotations

import json

import db
import operator_auth
import privacy
import review_api


def operator_login(handler):
    """POST /api/operator/login - preserve auth-off and auth-on semantics."""
    if not operator_auth.required():
        return handler._json({"ok": True, "note": "auth is off"})
    val = operator_auth.login(handler._body())
    if not val:
        return handler._err(401, "wrong token")
    handler._send(200, json.dumps({"ok": True}).encode(),
                  "application/json",
                  {"Set-Cookie": operator_auth.cookie_header(val)})
    return


def operator_logout(handler):
    """POST /api/operator/logout - preserve cookie-clearing behavior."""
    handler._send(200, json.dumps({"ok": True}).encode(),
                  "application/json",
                  {"Set-Cookie": operator_auth.cookie_header("", clear=True)})
    return


def rv_tokens(handler):
    """GET /api/rv/tokens - preserve operator-local-only visibility."""
    if not handler._is_local():
        return handler._err(403, "operator only")
    return handler._json(review_api.list_tokens())


def rv_tokens_new(handler):
    """POST /api/rv/tokens/new - preserve operator-local-only issuance."""
    if not handler._is_local():
        return handler._err(403, "operator only")
    b = handler._body()
    nodes = b.get("nodes") if isinstance(b.get("nodes"), list) else []
    return handler._json(review_api.issue_token(
        str(b.get("label") or "reviewer")[:60],
        "own" if b.get("scope") == "own" else "pool",
        [str(n)[:32] for n in nodes]))


def rv_tokens_revoke(handler):
    """POST /api/rv/tokens/revoke - preserve operator-local-only revocation."""
    if not handler._is_local():
        return handler._err(403, "operator only")
    b = handler._body()
    try:
        tid = int(b.get("id"))
    except (TypeError, ValueError):
        return handler._err(400, "bad id")
    return handler._json(review_api.revoke_token(tid))


def purge(handler):
    """POST /api/purge - preserve operator-local-only retention purge."""
    if not handler._is_local():
        return handler._err(403, "operator only")
    rep = privacy.purge_expired(db.connect())
    return handler._json(rep)
