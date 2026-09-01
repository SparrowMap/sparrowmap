"""node_credentials.py - Stage 2D3 route adapters.

Thin route adapters for the credential and evidence routes that remain in the
HTTP/transport boundary in this stage:

    POST /api/node/key
    POST /api/key/qr
    POST /api/key/rotate
    POST /api/sighting/fullres

The scope is intentionally narrow: parse request values, perform the existing
route-local auth checks, call the same persistence and evidence seams, and map
back to the existing response shapes. No new auth policy or storage abstraction
is introduced, and the inherited status semantics remain unchanged.
"""

from __future__ import annotations

import base64
import secrets as _s

import db
import privacy
import qr
import review_api
import snapshot


def node_key(handler):
    """POST /api/node/key - preserve the existing node-token + Ed25519 validation."""
    b = handler._body()
    nd = db.node(str(b.get("node_id") or ""))
    if not nd:
        return handler._err(404, "unknown node")
    if not nd.get("token"):
        return handler._err(401, "this node has no token; re-enroll it")
    if not handler._token_ok(nd):
        return handler._err(401, "bad node token")
    pub = str(b.get("pubkey") or "").strip()
    if not pub:
        return handler._err(400, "missing pubkey")
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        Ed25519PublicKey.from_public_bytes(base64.b64decode(pub))
    except Exception:
        return handler._err(400, "that is not an ed25519 public key")
    conn = db.connect()
    conn.execute("UPDATE nodes SET pubkey=? WHERE id=?", (pub, nd["id"]))
    conn.commit()
    db.audit("node_key", nd["id"], actor=f"camera {nd['id']}",
             ip=privacy.audit_ip(handler.client_ip))
    return handler._json({"ok": True, "id": nd["id"]})


def key_qr(handler):
    """POST /api/key/qr - preserve the QR payload and route-local token check."""
    b = handler._body()
    nid, tok = str(b.get("node_id") or ""), str(b.get("token") or "")
    nd = db.node(nid) if nid else None
    if not nd or not nd.get("token") or not tok:
        return handler._err(404, "unknown camera")
    if not _s.compare_digest(str(nd["token"]), tok):
        return handler._err(403, "wrong key")
    origin = b.get("origin") or ""
    if not origin.startswith(("http://", "https://")):
        return handler._err(400, "bad origin")
    url = f"{origin.rstrip('/')}/node#k={nid}.{tok}"
    try:
        return handler._send(200, qr.png(url), "image/png")
    except ValueError as exc:
        return handler._err(400, str(exc))


def key_rotate(handler):
    """POST /api/key/rotate - preserve the local-bypass + replacement-token semantics."""
    b = handler._body()
    nid, tok = str(b.get("node_id") or ""), str(b.get("token") or "")
    nd = db.node(nid) if nid else None
    if not nd or not nd.get("token"):
        return handler._err(404, "unknown camera")
    if not (_s.compare_digest(str(nd["token"]), tok) or handler._is_local()):
        return handler._err(403, "wrong key")
    new = _s.token_urlsafe(24)
    c = db.connect()
    c.execute("UPDATE nodes SET token=? WHERE id=?", (new, nid))
    c.commit()
    return handler._json({"ok": True, "node_id": nid, "token": new})


def sighting_fullres(handler):
    """POST /api/sighting/fullres - preserve the permissive node-token and evidence checks."""
    b = handler._body()
    nd = db.node(str(b.get("node_id") or ""))
    if not nd:
        return handler._err(404, "unknown node")
    if not handler._token_ok(nd):
        return handler._err(401, "bad node token")
    try:
        sid = int(b.get("id") or 0)
    except (TypeError, ValueError):
        return handler._err(400, "bad id")
    row = db.sighting(sid)
    if not row or row.get("node_id") != nd["id"]:
        return handler._err(404, "not this camera's sighting")
    if row.get("tier") != "public":
        return handler._err(403, "not published; the small crop stands")
    if row.get("snap_full"):
        return handler._json({"ok": True, "already": True})
    if not b.get("snap_b64"):
        return handler._err(400, "no image")
    try:
        full = snapshot.decode_bytes(str(b["snap_b64"]))
        name = review_api.attach_confirmed_photo(
            sid, row, full, ts=row.get("ts"),
            node_name=nd.get("name") or "a camera",
            vclass=("police" if row.get("vclass") == "police" else "gov"))
        if name:
            db.mark_fullres(sid, name)
        return handler._json({"ok": bool(name), "snap": name})
    except Exception as exc:
        print(f"[fullres] {sid}: {exc}")
        return handler._err(400, "image rejected")
