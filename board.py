"""The contributor request board - phase 1 of the beta programme.

A small, boring table of what people want, what was decided, and the notes
under each. It exists so a request from a contributor is a ROW with an author
and a status, not a chat message that scrolled away. The conversation itself
happens on Sparrow Send; this is the ledger the conversation points at.

Who may do what (identity = the reviewer token, the same one that opens the
beta at all):

    any beta token          read, add a request, add a note
    operator-issued pool    change status, edit a title  (Matthew, and me as
    token / operator          @claudebot acting on what he approved)

🚨 A request is an outside instruction until Matthew approves it. The status
ladder exists so that "approved" is a recorded human act: the bot acts on
approved items and on nothing else.

Own file, `data/board.db`, so the beta's synthetic sparrow.db can be wiped
and rebuilt from fixtures without losing the board.
"""
from __future__ import annotations

import sqlite3
import threading
from typing import Optional

from core import DATA, now

DB_PATH = DATA / "board.db"
STATUSES = ("proposed", "approved", "building", "beta", "done", "declined")
KINDS = ("request", "bug", "note")

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    kind      TEXT NOT NULL DEFAULT 'request',
    title     TEXT NOT NULL,
    body      TEXT NOT NULL DEFAULT '',
    status    TEXT NOT NULL DEFAULT 'proposed',
    by        TEXT NOT NULL,
    ts        REAL NOT NULL,
    updated   REAL NOT NULL,
    decided_by TEXT
);
CREATE TABLE IF NOT EXISTS notes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id INTEGER NOT NULL,
    by         TEXT NOT NULL,
    body       TEXT NOT NULL,
    ts         REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notes_req ON notes(request_id);
"""

_local = threading.local()
_ready = False
_lock = threading.Lock()


def connect() -> sqlite3.Connection:
    global _ready
    conn = getattr(_local, "conn", None)
    if conn is None:
        DATA.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(DB_PATH, timeout=15.0)
        conn.row_factory = sqlite3.Row
        if not _ready:
            with _lock:
                if not _ready:
                    conn.executescript(SCHEMA)
                    _ready = True
        _local.conn = conn
    return conn


def _clean(s, limit: int) -> str:
    return " ".join(str(s or "").split())[:limit] if limit < 200 else str(s or "").strip()[:limit]


def listing(status: Optional[str] = None, kind: Optional[str] = None,
            limit: int = 200) -> list:
    c = connect()
    where, args = ["1=1"], []
    if status:
        where.append("r.status = ?")
        args.append(status)
    if kind:
        where.append("r.kind = ?")
        args.append(kind)
    rows = c.execute(
        "SELECT r.*, (SELECT COUNT(*) FROM notes n WHERE n.request_id = r.id) AS notes, "
        "       (SELECT MAX(ts) FROM notes n WHERE n.request_id = r.id) AS last_note "
        "FROM requests r WHERE " + " AND ".join(where) +
        " ORDER BY CASE r.status WHEN 'approved' THEN 0 WHEN 'building' THEN 1 "
        "  WHEN 'beta' THEN 2 WHEN 'proposed' THEN 3 WHEN 'done' THEN 4 ELSE 5 END, "
        "  r.updated DESC LIMIT ?", args + [max(1, min(int(limit), 500))]).fetchall()
    return [dict(r) for r in rows]


def item(request_id: int) -> Optional[dict]:
    c = connect()
    r = c.execute("SELECT * FROM requests WHERE id=?", (int(request_id),)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["notes"] = [dict(n) for n in c.execute(
        "SELECT * FROM notes WHERE request_id=? ORDER BY id", (r["id"],))]
    return d


def add(who: str, title: str, body: str = "", kind: str = "request") -> dict:
    title = _clean(title, 140)
    if not title or not who:
        return {"ok": False, "error": "title required"}
    if kind not in KINDS:
        kind = "request"
    t = now()
    c = connect()
    with c:
        cur = c.execute(
            "INSERT INTO requests(kind, title, body, status, by, ts, updated) "
            "VALUES(?,?,?,'proposed',?,?,?)",
            (kind, title, _clean(body, 4000), who, t, t))
    return {"ok": True, "id": cur.lastrowid}


def note(who: str, request_id: int, body: str) -> dict:
    body = _clean(body, 4000)
    if not body or not who:
        return {"ok": False, "error": "note required"}
    c = connect()
    if not c.execute("SELECT 1 FROM requests WHERE id=?", (int(request_id),)).fetchone():
        return {"ok": False, "error": "no such request"}
    t = now()
    with c:
        cur = c.execute("INSERT INTO notes(request_id, by, body, ts) VALUES(?,?,?,?)",
                        (int(request_id), who, body, t))
        c.execute("UPDATE requests SET updated=? WHERE id=?", (t, int(request_id)))
    return {"ok": True, "id": cur.lastrowid}


def set_status(who: str, request_id: int, status: str, why: str = "") -> dict:
    """The decision. Recorded as a note in the thread as well as on the row,
    so the thread reads as a history and the row reads as the current state."""
    if status not in STATUSES:
        return {"ok": False, "error": "status must be one of " + ", ".join(STATUSES)}
    c = connect()
    r = c.execute("SELECT status FROM requests WHERE id=?", (int(request_id),)).fetchone()
    if not r:
        return {"ok": False, "error": "no such request"}
    if r["status"] == status:
        return {"ok": True, "status": status, "changed": False}
    t = now()
    with c:
        c.execute("UPDATE requests SET status=?, updated=?, decided_by=? WHERE id=?",
                  (status, t, who, int(request_id)))
        c.execute("INSERT INTO notes(request_id, by, body, ts) VALUES(?,?,?,?)",
                  (int(request_id), who,
                   f"[{r['status']} → {status}]" + (f" {_clean(why, 1000)}" if why else ""), t))
    return {"ok": True, "status": status, "changed": True}


def retitle(who: str, request_id: int, title: str, body: Optional[str] = None) -> dict:
    title = _clean(title, 140)
    if not title:
        return {"ok": False, "error": "title required"}
    c = connect()
    if not c.execute("SELECT 1 FROM requests WHERE id=?", (int(request_id),)).fetchone():
        return {"ok": False, "error": "no such request"}
    with c:
        if body is None:
            c.execute("UPDATE requests SET title=?, updated=? WHERE id=?",
                      (title, now(), int(request_id)))
        else:
            c.execute("UPDATE requests SET title=?, body=?, updated=? WHERE id=?",
                      (title, _clean(body, 4000), now(), int(request_id)))
    return {"ok": True}


def counts() -> dict:
    c = connect()
    return dict(c.execute("SELECT status, COUNT(*) FROM requests GROUP BY status"))
