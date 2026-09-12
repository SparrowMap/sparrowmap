"""The officer-accountability review surface behind /ov - phase 2.

Everything a trusted reviewer does to `data/oversight.db` goes through here:
work the queue of party strings, mint an officer, link party strings to that
officer, publish or retract, and read the audit log. Nothing here is reachable
without an operator-issued pool token (review_api.is_trusted), and every write
lands a row in `vetting` in the same transaction - the project's claim is "no
assertion about a person reaches the public without a named human", and that
is only true if the table can prove it.

What this module does NOT do, on purpose:

- It never mints or links on its own. `classify_party` produces a QUEUE; a
  human turns a queue row into a fact. Same rule that keeps the map honest -
  weak similarity is a queue, never a write.
- It never edits `case_parties.raw_name` or anything in `cases`. Those are
  copies of the public record and stay exact.
- It never returns `allegations.contact`, and it never serves a home address
  because there is no column for one.
"""
from __future__ import annotations

import importlib.util
import re
import sqlite3
from typing import Any, Optional

import oversight
from core import ROOT, now

# --- court -> state --------------------------------------------------------
# STATE_COURTS lives in tools/courtlistener_fetch.py because that is where it is
# maintained against the CourtListener court list. Loaded by path so the hub
# does not need tools/ on sys.path; a load failure degrades to "group by court"
# rather than taking the page down.
_COURT_STATE: Optional[dict] = None


def court_state() -> dict:
    global _COURT_STATE
    if _COURT_STATE is None:
        m: dict = {}
        try:
            p = ROOT / "tools" / "courtlistener_fetch.py"
            spec = importlib.util.spec_from_file_location("_clf", p)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)          # type: ignore[union-attr]
            for st, courts in mod.STATE_COURTS.items():
                for c in courts:
                    m[c] = st
        except Exception:
            m = {}
        _COURT_STATE = m
    return _COURT_STATE


def courts_for(state: Optional[str]) -> Optional[list]:
    if not state:
        return None
    st = state.strip().upper()
    return [c for c, s in court_state().items() if s == st] or None


# --- the queue -------------------------------------------------------------
# Party strings that are not a person's name. "Sheriff", "Chief of Police",
# "U.S. Marshal Service" carry a rank so the classifier scores them 3, but a
# reviewer cannot mint an officer out of an office. Filtered here, in one
# place, and COUNTED so the number is visible rather than silently gone.
_TITLE_WORDS = {
    "officer", "officers", "police", "deputy", "deputies", "sheriff", "sheriffs",
    "sergeant", "sgt", "sgt.", "lieutenant", "lt", "lt.", "captain", "capt",
    "capt.", "chief", "detective", "det", "det.", "trooper", "marshal",
    "marshall", "marshals", "corporal", "cpl", "cpl.", "commander", "inspector",
    "agent", "agents", "special", "patrolman", "patrol", "constable", "warden",
    "of", "the", "and", "us", "u.s.", "u.s", "united", "states", "city",
    "county", "state", "department", "dept", "dept.", "dep", "dep.", "office",
    "service", "services", "division", "unit", "task", "force", "k9", "k-9",
    "jr", "jr.", "sr", "sr.", "ii", "iii", "iv", "et", "al", "al.", "individual",
    "official", "capacity", "in", "his", "her", "their", "all", "unknown",
    "fnu", "lnu", "id", "no", "no.", "#", "badge", "shield", "star",
    "auxiliary", "reserve", "detention", "center", "jail", "prison",
    "correctional", "corrections", "facility", "bureau", "agency", "authority",
}
# Nobody. PACER's placeholders for a defendant the plaintiff could not name.
# Not "unknown" on its own: "Unknown Barton" is a real deputy with an unknown
# first name (signal 1), and a reviewer may well mint Barton after reading.
_PLACEHOLDER = re.compile(r"\bdoes?\b|\bfnu\b|\blnu\b", re.I)
# "<title> of <place>" and "<title>, <place> County" are offices, not people,
# even though the place name looks like a surname.
_PLACE = re.compile(r"\b(county|parish|city|township|village|borough|department|"
                    r"dept|office|police|sheriff'?s)\b", re.I)


def person_tokens(raw: str) -> list:
    """The parts of a party string that could be a human name."""
    out = []
    for t in re.split(r"[\s,/]+", raw or ""):
        t = t.strip("()[]\"'.").strip()
        if not t:
            continue
        low = t.lower()
        if low in _TITLE_WORDS or not re.search(r"[a-z]", low):
            continue
        out.append(t)
    return out


def looks_like_office(raw: str) -> bool:
    """True when the string cannot be minted as a person: nothing in it could
    be a surname, it is a Doe placeholder, it is "<title> of <place>", or the
    only name-shaped token sits next to a place word ("Sheriff, Cook County")."""
    raw = raw or ""
    if _PLACEHOLDER.search(raw):
        return True
    if re.search(r"\bof\b", raw, re.I):
        return True
    toks = [t for t in person_tokens(raw) if len(t) >= 2]
    if not toks:
        return True
    if len(toks) == 1 and _PLACE.search(raw):
        return True
    return False


def split_name(raw: str) -> dict:
    """A best-effort (given, surname) from a caption string. A SUGGESTION for
    the mint form - the reviewer edits it, the code never trusts it."""
    raw = raw or ""
    # "Deputy Smith, Wayne County Sheriff": after the comma is the agency.
    head, _, tail = raw.partition(",")
    if tail and _PLACE.search(tail) and person_tokens(head):
        raw = head
    toks = person_tokens(raw)
    if not toks:
        return {"given": "", "surname": ""}
    if "," in raw and len(toks) >= 2:
        # "Smith, John" - PACER writes some captions surname-first.
        head = raw.split(",", 1)[0]
        if person_tokens(head) and person_tokens(head)[-1] == toks[0]:
            return {"surname": toks[0], "given": " ".join(toks[1:])}
    return {"surname": toks[-1], "given": " ".join(toks[:-1])}


def queue(scope: str = "police", state: Optional[str] = None, q: str = "",
          min_signal: int = 3, limit: int = 60, offset: int = 0) -> dict:
    """Unlinked party strings, grouped by exact string, most-cited first.

    A name that appears as a titled defendant across several separate cases is
    the highest-value thing in the database - the pattern one case cannot
    show - so that is the sort. Grouping is by the EXACT string: "Officer
    Heard" in two states is two rows, because same name is not same human and
    the reviewer decides that, not a GROUP BY.
    """
    c = oversight.connect()
    where = ["p.officer_id IS NULL", "p.officer_signal >= ?",
             "p.kind_guess = 'person'"]
    args: list = [int(min_signal)]
    if scope == "all":
        where.append("c.police IN ('police','corrections')")
    else:
        where.append("c.police = ?")
        args.append(scope)
    courts = courts_for(state)
    if state and not courts:
        return {"rows": [], "next_offset": offset, "done": True, "skipped": 0}
    if courts:
        where.append("c.court_id IN (%s)" % ",".join("?" * len(courts)))
        args += courts
    if q:
        like = ("%" + q.replace("\\", "\\\\").replace("%", "\\%")
                .replace("_", "\\_") + "%")
        where.append("(p.raw_name LIKE ? ESCAPE '\\' OR p.agency_hint LIKE ? "
                     "ESCAPE '\\' OR c.case_name LIKE ? ESCAPE '\\')")
        args += [like, like, like]
    limit = max(1, min(int(limit), 200))
    scan = limit * 3   # room for the office-string filter below
    sql = ("SELECT p.raw_name, MAX(p.title_guess) AS title, "
           "       MAX(p.agency_hint) AS agency_hint, MAX(p.officer_signal) AS signal, "
           "       COUNT(DISTINCT p.docket_id) AS cases, "
           "       MIN(c.date_filed) AS first, MAX(c.date_filed) AS last, "
           "       GROUP_CONCAT(DISTINCT c.court_id) AS courts "
           "FROM case_parties p JOIN cases c ON c.docket_id = p.docket_id "
           "WHERE " + " AND ".join(where) +
           " GROUP BY p.raw_name, c.court_id "
           " ORDER BY cases DESC, last DESC, p.raw_name "
           f" LIMIT {scan} OFFSET {int(offset)}")
    raw = c.execute(sql, args).fetchall()
    cs = court_state()
    rows, skipped, used = [], 0, 0
    for r in raw:
        used += 1
        if looks_like_office(r["raw_name"]):
            skipped += 1
            continue
        courts_seen = (r["courts"] or "").split(",")
        rows.append({
            "name": r["raw_name"], "title": r["title"],
            "agency_hint": r["agency_hint"], "signal": r["signal"],
            "cases": r["cases"], "first": r["first"], "last": r["last"],
            "courts": courts_seen,
            "state": cs.get(courts_seen[0], "") if courts_seen else "",
        })
        if len(rows) >= limit:
            break
    return {"rows": rows, "next_offset": int(offset) + used,
            "done": len(raw) < scan and used == len(raw), "skipped": skipped}


def group(name: str, state: Optional[str] = None, court: Optional[str] = None,
          scope: str = "police") -> dict:
    """Every appearance of one exact party string: the cases, with the other
    parties on each so the reviewer can see agency context, plus name and
    agency suggestions for the mint form."""
    c = oversight.connect()
    where = ["p.raw_name = ?"]
    args: list = [name]
    if court:
        where.append("c.court_id = ?")
        args.append(court)
    else:
        courts = courts_for(state)
        if courts:
            where.append("c.court_id IN (%s)" % ",".join("?" * len(courts)))
            args += courts
    if scope != "all":
        where.append("c.police = ?")
        args.append(scope)
    rows = c.execute(
        "SELECT p.id AS party_id, p.docket_id, p.role_guess, p.officer_signal, "
        "       p.title_guess, p.agency_hint, p.officer_id, "
        "       c.case_name, c.docket_number, c.court_id, c.court_name, "
        "       c.date_filed, c.date_terminated, c.cause, c.suit_nature, "
        "       c.absolute_url, c.police, c.police_why "
        "FROM case_parties p JOIN cases c ON c.docket_id = p.docket_id "
        "WHERE " + " AND ".join(where) + " ORDER BY c.date_filed DESC",
        args).fetchall()
    cs = court_state()
    out = []
    agencies: dict = {}
    for r in rows:
        d = dict(r)
        d["state"] = cs.get(r["court_id"], "")
        # Co-parties: who else is on the docket. Entities carry the agency.
        co = c.execute(
            "SELECT raw_name, kind_guess, role_guess, officer_signal, officer_id "
            "FROM case_parties WHERE docket_id = ? AND id != ? "
            "ORDER BY officer_signal DESC, kind_guess, raw_name LIMIT 40",
            (r["docket_id"], r["party_id"])).fetchall()
        d["co_parties"] = [dict(x) for x in co]
        for a in c.execute(
                "SELECT a.display FROM case_agencies ca JOIN agencies a "
                "ON a.id = ca.agency_id WHERE ca.docket_id = ?",
                (r["docket_id"],)):
            agencies[a[0]] = agencies.get(a[0], 0) + 1
        if r["agency_hint"]:
            agencies[r["agency_hint"]] = agencies.get(r["agency_hint"], 0) + 1
        # 🚨 absolute_url as stored. Rebuilding /docket/<id>/ without the slug
        # is a guaranteed 404 - verified in a browser.
        d["url"] = ("https://www.courtlistener.com" + r["absolute_url"]
                    if r["absolute_url"] else None)
        out.append(d)
    sugg = split_name(name)
    top_agency = max(agencies.items(), key=lambda kv: kv[1])[0] if agencies else ""
    states = sorted({d["state"] for d in out if d["state"]})
    return {"name": name, "parties": out, "suggest": {
        "display": " ".join(person_tokens(name)) or name,
        "surname": sugg["surname"], "given": sugg["given"],
        "rank": (rows[0]["title_guess"] if rows else "") or "",
        "agency": top_agency, "agency_state": states[0] if len(states) == 1 else "",
        "agencies": sorted(agencies.items(), key=lambda kv: -kv[1])[:8],
    }}


# --- officers ---------------------------------------------------------------
def _vet(c: sqlite3.Connection, who: str, action: str, target: str,
         detail: str = "") -> None:
    c.execute("INSERT INTO vetting(ts, who, action, target, detail) "
              "VALUES(?,?,?,?,?)", (now(), who, action, target, detail))
    _STATS_CACHE["t"] = 0.0   # the header counts just changed


def mint_officer(who: str, display: str, surname: str = "", given: str = "",
                 agency: str = "", agency_state: str = "", rank: str = "",
                 notes: str = "") -> dict:
    display = (display or "").strip()
    if not display or not who:
        return {"ok": False, "error": "display name and reviewer required"}
    if looks_like_office(display):
        return {"ok": False, "error": "that is an office, not a person"}
    c = oversight.connect()
    with c:
        cur = c.execute(
            "INSERT INTO officers(display, surname, given, agency, agency_state, "
            "rank, status, created, created_by, notes) "
            "VALUES(?,?,?,?,?,?,'draft',?,?,?)",
            (display, surname.strip() or None, given.strip() or None,
             agency.strip() or None, (agency_state or "").strip().upper() or None,
             rank.strip() or None, now(), who, notes.strip() or None))
        oid = cur.lastrowid
        _vet(c, who, "mint_officer", f"officer:{oid}",
             f"{display} · {rank or '-'} · {agency or '-'} {agency_state or ''}")
    return {"ok": True, "officer_id": oid}


def link_parties(who: str, officer_id: int, party_ids: list,
                 confidence: str = "probable", reason: str = "") -> dict:
    """Party string -> officer. The editorial act, so it is a row with an
    author (officer_refs), and case_parties.officer_id is set from that row,
    not the other way round."""
    if confidence not in ("certain", "probable", "possible"):
        return {"ok": False, "error": "confidence must be certain|probable|possible"}
    c = oversight.connect()
    off = c.execute("SELECT id, display FROM officers WHERE id=?",
                    (int(officer_id),)).fetchone()
    if not off:
        return {"ok": False, "error": "no such officer"}
    ids = []
    for p in party_ids or []:
        try:
            ids.append(int(p))
        except (TypeError, ValueError):
            pass
    if not ids:
        return {"ok": False, "error": "no parties"}
    linked, already = [], []
    with c:
        for pid in ids:
            row = c.execute("SELECT id, officer_id, raw_name, docket_id "
                            "FROM case_parties WHERE id=?", (pid,)).fetchone()
            if not row:
                continue
            if row["officer_id"]:
                already.append(pid)
                continue
            c.execute("INSERT INTO officer_refs(officer_id, party_id, confidence, "
                      "decided_by, decided_at, reason) VALUES(?,?,?,?,?,?)",
                      (off["id"], pid, confidence, who, now(), reason or None))
            c.execute("UPDATE case_parties SET officer_id=? WHERE id=?",
                      (off["id"], pid))
            linked.append(pid)
            _vet(c, who, "link_party", f"officer:{off['id']}",
                 f"party:{pid} docket:{row['docket_id']} \"{row['raw_name']}\" "
                 f"({confidence}) {reason or ''}".strip())
    return {"ok": True, "linked": linked, "already_linked": already}


def unlink_party(who: str, party_id: int, reason: str = "") -> dict:
    """Undo an identification. The ref row stays, marked withdrawn, so the
    audit log shows a decision AND its reversal rather than nothing."""
    c = oversight.connect()
    row = c.execute("SELECT id, officer_id, raw_name, docket_id FROM case_parties "
                    "WHERE id=?", (int(party_id),)).fetchone()
    if not row or not row["officer_id"]:
        return {"ok": False, "error": "party is not linked"}
    with c:
        c.execute("UPDATE officer_refs SET withdrawn=1 WHERE party_id=? AND "
                  "officer_id=? AND withdrawn=0", (row["id"], row["officer_id"]))
        c.execute("UPDATE case_parties SET officer_id=NULL WHERE id=?", (row["id"],))
        _vet(c, who, "unlink_party", f"officer:{row['officer_id']}",
             f"party:{row['id']} docket:{row['docket_id']} \"{row['raw_name']}\" "
             f"{reason or ''}".strip())
    return {"ok": True, "officer_id": row["officer_id"]}


def set_status(who: str, officer_id: int, status: str, reason: str = "") -> dict:
    """draft -> published -> retracted, each rung a named decision.

    Publishing needs at least one live document link: the profile page is
    documents first, and a profile with none would be a name and nothing else,
    which is exactly the page this project must never serve.
    """
    if status not in ("draft", "published", "retracted"):
        return {"ok": False, "error": "status must be draft|published|retracted"}
    c = oversight.connect()
    off = c.execute("SELECT * FROM officers WHERE id=?", (int(officer_id),)).fetchone()
    if not off:
        return {"ok": False, "error": "no such officer"}
    if status == "published":
        n = c.execute("SELECT COUNT(*) FROM officer_refs WHERE officer_id=? "
                      "AND withdrawn=0", (off["id"],)).fetchone()[0]
        if not n:
            return {"ok": False, "error": "link at least one case before publishing"}
    with c:
        c.execute("UPDATE officers SET status=? WHERE id=?", (status, off["id"]))
        _vet(c, who, {"published": "publish", "retracted": "retract",
                      "draft": "unpublish"}[status],
             f"officer:{off['id']}", reason or "")
    return {"ok": True, "status": status}


def edit_officer(who: str, officer_id: int, fields: dict) -> dict:
    """Correct the profile's own fields (never a case). Logged as 'correct'."""
    allowed = ("display", "surname", "given", "agency", "agency_state", "rank", "notes")
    c = oversight.connect()
    off = c.execute("SELECT * FROM officers WHERE id=?", (int(officer_id),)).fetchone()
    if not off:
        return {"ok": False, "error": "no such officer"}
    sets, args, changed = [], [], []
    for k in allowed:
        if k in fields:
            v = (fields[k] or "").strip() or None
            if k == "agency_state" and v:
                v = v.upper()
            if k == "display" and not v:
                return {"ok": False, "error": "display cannot be empty"}
            if v != off[k]:
                sets.append(f"{k}=?")
                args.append(v)
                changed.append(f"{k}: {off[k]!r} -> {v!r}")
    if not sets:
        return {"ok": True, "changed": []}
    with c:
        c.execute(f"UPDATE officers SET {', '.join(sets)} WHERE id=?",
                  args + [off["id"]])
        _vet(c, who, "correct", f"officer:{off['id']}", "; ".join(changed))
    return {"ok": True, "changed": changed}


def officers(q: str = "", status: Optional[str] = None, state: Optional[str] = None,
             limit: int = 100) -> list:
    c = oversight.connect()
    where, args = ["1=1"], []
    if q:
        where.append("(o.display LIKE ? OR o.surname LIKE ? OR o.agency LIKE ?)")
        args += ["%" + q + "%"] * 3
    if status:
        where.append("o.status = ?")
        args.append(status)
    if state:
        where.append("o.agency_state = ?")
        args.append(state.upper())
    rows = c.execute(
        "SELECT o.*, "
        "  (SELECT COUNT(*) FROM officer_refs r WHERE r.officer_id=o.id AND r.withdrawn=0) AS links, "
        "  (SELECT COUNT(*) FROM allegations a WHERE a.officer_id=o.id) AS allegations "
        "FROM officers o WHERE " + " AND ".join(where) +
        f" ORDER BY o.created DESC LIMIT {max(1, min(int(limit), 500))}",
        args).fetchall()
    return [dict(r) for r in rows]


def officer(officer_id: int) -> Optional[dict]:
    c = oversight.connect()
    off = c.execute("SELECT * FROM officers WHERE id=?", (int(officer_id),)).fetchone()
    if not off:
        return None
    d = dict(off)
    cs = court_state()
    d["badges"] = [dict(r) for r in c.execute(
        "SELECT * FROM officer_badges WHERE officer_id=? ORDER BY from_date",
        (off["id"],))]
    refs = c.execute(
        "SELECT r.id AS ref_id, r.party_id, r.confidence, r.decided_by, r.decided_at, "
        "       r.reason, r.withdrawn, p.raw_name, p.docket_id, c.case_name, "
        "       c.docket_number, c.court_id, c.date_filed, c.date_terminated, "
        "       c.absolute_url, c.suit_nature "
        "FROM officer_refs r LEFT JOIN case_parties p ON p.id = r.party_id "
        "LEFT JOIN cases c ON c.docket_id = p.docket_id "
        "WHERE r.officer_id=? ORDER BY r.withdrawn, c.date_filed DESC",
        (off["id"],)).fetchall()
    d["refs"] = []
    for r in refs:
        x = dict(r)
        x["state"] = cs.get(r["court_id"] or "", "")
        x["url"] = ("https://www.courtlistener.com" + r["absolute_url"]
                    if r["absolute_url"] else None)
        d["refs"].append(x)
    # Claims are listed as claims, and the submitter's contact never leaves.
    d["allegations"] = [dict(r) for r in c.execute(
        "SELECT id, agency, incident_date, status, submitted_at, submitter, "
        "       reviewed_by, reviewed_at, body FROM allegations WHERE officer_id=? "
        "ORDER BY submitted_at DESC", (off["id"],))]
    d["vetting"] = [dict(r) for r in c.execute(
        "SELECT ts, who, action, detail FROM vetting WHERE target=? ORDER BY id DESC",
        (f"officer:{off['id']}",))]
    return d


def log(limit: int = 100, before: Optional[int] = None) -> list:
    c = oversight.connect()
    if before:
        rows = c.execute("SELECT * FROM vetting WHERE id < ? ORDER BY id DESC LIMIT ?",
                         (int(before), int(limit))).fetchall()
    else:
        rows = c.execute("SELECT * FROM vetting ORDER BY id DESC LIMIT ?",
                         (int(limit),)).fetchall()
    return [dict(r) for r in rows]


_STATS_CACHE: dict = {"t": 0.0, "v": None}


def stats() -> dict:
    """Counts for the header. Cached a minute: the police slice is 43k rows
    and the per-state breakdown is a full pass over it."""
    if _STATS_CACHE["v"] and now() - _STATS_CACHE["t"] < 60:
        return _STATS_CACHE["v"]
    c = oversight.connect()
    cs = court_state()
    by_state: dict = {}
    for court, n in c.execute("SELECT court_id, COUNT(*) FROM cases WHERE "
                              "police='police' GROUP BY court_id"):
        st = cs.get(court, court)
        by_state[st] = by_state.get(st, 0) + n
    named_by_state: dict = {}
    for court, n in c.execute(
            "SELECT c.court_id, COUNT(DISTINCT p.raw_name) FROM case_parties p "
            "JOIN cases c ON c.docket_id=p.docket_id WHERE c.police='police' "
            "AND p.officer_signal>=3 AND p.kind_guess='person' AND p.officer_id IS NULL "
            "GROUP BY c.court_id"):
        st = cs.get(court, court)
        named_by_state[st] = named_by_state.get(st, 0) + n
    status = dict(c.execute("SELECT status, COUNT(*) FROM officers GROUP BY status"))
    v = {
        "enrich": oversight.enrich_progress("police"),
        "officers": status,
        "links": c.execute("SELECT COUNT(*) FROM officer_refs WHERE withdrawn=0").fetchone()[0],
        "allegations": dict(c.execute("SELECT status, COUNT(*) FROM allegations GROUP BY status")),
        "vetting": c.execute("SELECT COUNT(*) FROM vetting").fetchone()[0],
        "states": sorted(({"state": s, "cases": n, "queue": named_by_state.get(s, 0)}
                          for s, n in by_state.items()),
                         key=lambda x: -x["cases"]),
    }
    _STATS_CACHE.update(t=now(), v=v)
    return v


def available() -> bool:
    """Is there a database to review? A hub without the 700 MB file (a mirror,
    a fresh checkout) answers 503, not an empty queue that looks like done."""
    return oversight.DB_PATH.is_file()
