"""node_self.py - Stage 2D1 route adapters: camera self-service routes.

This is a ROUTE ADAPTER module, not a domain/application-service layer.
It owns the HTTP-facing glue for the low-risk camera/self-service routes that
already had a narrow existing seam and whose behavior stays entirely within the
node token + row lookup + response-shaping layer:

    GET  /api/node/me
    POST /api/node/whoami
    POST /api/node/parked
    POST /api/node/span

Deliberately NOT moved in this stage:
    /api/nodes - Stage 2D pre-analysis left it in hub.py because the volunteer
                 public-position projection logic and consent gate are coupled
                 to the route adapter and would require a larger seam than this
                 stage is allowed to introduce.

The handler contract is intentionally the same duck-typed object used in the
other route modules: it has _body(), _json(), _err(), and the existing
_handler._token_ok() method. This keeps the ordered dispatch semantics in
hub.py unchanged while moving only the route-specific logic out.
"""

from __future__ import annotations

import db
import nodes as node_mod
import privacy
import review_api


def node_me(handler, query: dict):
    """GET /api/node/me - moved verbatim from hub.py."""
    nd = db.node((query.get("id") or [""])[0])
    if not nd:
        return handler._err(404, "unknown camera")
    if not nd.get("token") or not handler._token_ok(nd):
        return handler._err(401, "this camera's token is required")
    return handler._json({
        "id": nd["id"], "name": nd.get("name") or nd["id"],
        "kind": nd.get("kind") or "fixed",
        "lat": nd["lat"], "lon": nd["lon"],
        "heading": nd.get("heading") or 0,
        "fov": nd.get("fov") or 60,
        "reach_m": nd.get("reach_m") or 45,
        "road_name": nd.get("road_name"),
        "span_source": nd.get("span_source"),
        "span": node_mod.span_of(nd),
        "publish_span": bool(nd.get("publish_span")),
        "sightings": nd.get("sightings") or 0,
        "last_seen": nd.get("last_seen"),
    })


def node_whoami(handler):
    """POST /api/node/whoami - moved verbatim from hub.py."""
    b = handler._body()
    nd = db.node(str(b.get("node_id") or ""))
    if not nd:
        return handler._err(404, "no camera with that id")
    if not nd.get("token"):
        return handler._err(401, "this node has no token; re-enroll it")
    if not handler._token_ok(nd):
        return handler._err(401, "that key does not match this camera")
    return handler._json({
        "id": nd["id"], "name": nd.get("name") or nd["id"],
        "status": nd.get("status"),
        "kind": nd.get("kind"),
        "sightings": nd.get("sightings") or 0,
        "last_seen": nd.get("last_seen"),
    })


def node_parked(handler):
    """POST /api/node/parked - moved verbatim from hub.py."""
    b = handler._body()
    nd = db.node(str(b.get("node_id") or ""))
    if not nd:
        return handler._err(404, "unknown node")
    if not nd.get("token"):
        return handler._err(401, "this node has no token; re-enroll it")
    if not handler._token_ok(nd):
        return handler._err(401, "bad node token")
    try:
        ids = [int(x) for x in (b.get("ids") or [])][:40]
    except (TypeError, ValueError):
        return handler._err(400, "bad ids")
    out = []
    for sid in ids:
        row = db.sighting(sid)
        if not row or (row.get("node_id") or "") != nd["id"]:
            continue
        meta = review_api._pen_meta(sid)
        if not meta:
            continue
        out.append({"id": sid,
                    "vclass": meta.get("vclass") or row.get("vclass"),
                    "score": meta.get("score")})
    return handler._json({"parked": out})


def node_span(handler):
    """POST /api/node/span - moved verbatim from hub.py."""
    b = handler._body()
    nd = db.node(str(b.get("node_id") or ""))
    if not nd:
        return handler._err(404, "unknown node")
    if not nd.get("token"):
        return handler._err(401, "this node has no token; re-enroll it")
    if not handler._token_ok(nd):
        return handler._err(401, "bad node token")
    on = bool(b.get("publish"))
    db.set_publish_span(nd["id"], on)
    db.audit("node_span:" + ("publish" if on else "hide"),
             nd["id"], actor=f"camera {nd['id']}",
             ip=privacy.audit_ip(handler.client_ip))
    return handler._json({"ok": True, "publish": on,
                        "has_span": node_mod.span_of(db.node(nd["id"])) is not None,
                        "road_name": (db.node(nd["id"]) or {}).get("road_name")})
