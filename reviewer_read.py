"""reviewer_read.py - Stage 2E1 reviewer read/session route adapters.

Thin HTTP adapters for the reviewer/session and operator-local read-only routes
that are safe to extract without normalizing the existing reviewer-vs-operator
trust boundaries. The application/domain logic remains in review_api.py and db.
"""

from __future__ import annotations

import json
import traceback

import db
import review_api
import review_auth
from core import DATA


def rv_me(handler):
    """GET /api/rv/me - preserve reviewer token cookie/bearer auth semantics."""
    r = review_auth.identify(handler.headers)
    if not r:
        return handler._err(401, "not signed in")
    return handler._json({
        "ok": True,
        "label": r["label"],
        "scope": r["scope"],
        "nodes": sorted(r["nodes"]) if r["nodes"] else [],
    })


def rv_queue(handler, query):
    """GET /api/rv/queue - preserve reviewer auth and queue filtering."""
    r = review_auth.identify(handler.headers)
    if not r:
        return handler._err(401, "not signed in")
    scope = (query.get("scope") or ["pool"])[0]
    rejected = (query.get("rejected") or ["0"])[0] in ("1", "true", "yes")
    return handler._json(review_api.queue(r, scope, rejected=rejected))


def review_queue(handler):
    """GET /api/review/queue - preserve the operator-local read-only queue."""
    if not handler._is_local():
        return handler._err(403, "local only")
    rows = db.public_review_queue_rows(200)
    report_counts = db.open_report_counts()
    out = []
    for row in rows:
        d = dict(row)
        item = {k: d.get(k) for k in (
            "id", "ts", "vclass", "vclass_conf", "vclass_why",
            "plate_text", "snap", "node_id", "reviewed",
            "reviewed_at", "source", "color", "body",
        )}
        item["reports"] = db.reports_for(d["id"]) if report_counts.get(d["id"]) else []
        out.append(item)
    out.sort(key=lambda x: (x["reviewed"] is not None, 0 if x["reports"] else 1))
    missed = []
    try:
        from detect import bank
        day = __import__("core").now() - 86400 * 3
        rows2 = db.private_unreviewed_since(day, 400)
        from detect import head as _head
        hthr = _head.threshold() if _head.available() else None
        for r in rows2:
            if not r["bank_ref"]:
                continue
            j = bank.sidecar(r["bank_ref"])
            if not j:
                continue
            meta = json.loads(j.read_text(encoding="utf-8"))
            clip = meta.get("clip") or {}
            hc = clip.get("head_conf")
            if hc is not None and hthr is not None:
                if hc < hthr:
                    continue
            elif (clip.get("vclass") != "police"
                    or (clip.get("conf") or 0) < 0.50
                    or (clip.get("margin") or 0) < 0.20):
                continue
            d = dict(r)
            missed.append({
                **{k: d.get(k) for k in ("id", "ts", "vclass", "snap", "node_id")},
                "clip_conf": hc if (hc is not None and hthr is not None) else clip.get("conf"),
                "clip_margin": clip.get("margin"),
                "by_head": hc is not None and hthr is not None,
                "label": meta.get("label"),
            })
        missed.sort(key=lambda m: -(m.get("clip_conf") or 0))
        missed = missed[:40]
    except Exception:
        traceback.print_exc()
    return handler._json({"queue": out, "missed": missed,
                          "missed_pending": len(missed), **db.review_stats()})


def rv_contributed(handler):
    """GET /api/rv/contributed - preserve reviewer-only counts."""
    r = review_auth.identify(handler.headers)
    if not r:
        return handler._err(401, "not signed in")
    return handler._json(review_api.contributed(r))


def rv_progress(handler):
    """GET /api/rv/progress - preserve read-only reviewer progress metadata."""
    r = review_auth.identify(handler.headers)
    if not r:
        return handler._err(401, "not signed in")
    try:
        return handler._json(json.loads((DATA / "label_progress.json").read_text(encoding="utf-8")))
    except Exception:
        return handler._json({"unavailable": True})


def rv_login(handler):
    """POST /api/rv/login - preserve reviewer login cookie issuance."""
    val = review_auth.login_value((handler._body() or {}).get("token", ""))
    if not val:
        return handler._err(401, "invalid token")
    handler._send(200, json.dumps({"ok": True}).encode(),
                  "application/json",
                  {"Set-Cookie": review_auth.cookie_header(val)})
    return


def rv_logout(handler):
    """POST /api/rv/logout - preserve the existing JSON-only state-changing behavior."""
    handler._send(200, json.dumps({"ok": True}).encode(),
                  "application/json",
                  {"Set-Cookie": review_auth.cookie_header("", clear=True)})
    return
