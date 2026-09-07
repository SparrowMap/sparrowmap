"""SparrowMap - the privacy layer.

This module is the reason the project is defensible. Read it before anything
else.

THE RULE
    A publicly owned vehicle, doing public work, on a public road, is a public
    record. Everything else is nobody's business.

So SparrowMap keeps two tiers:

    public tier  (police / gov / fleet)
        Plate text stored readable. Full history. Searchable by anyone.
        Retained indefinitely, like council minutes.

    private tier (everyone else)
        Plate text is NEVER written to disk. Only a keyed hash is stored, so
        the same car still re-identifies itself across cameras - which is what
        makes traffic counts and "a vehicle passed here" work - while no one
        can ask "where has THIS person been".

Three further limits stack on top of the hash, because a hash alone is not
privacy. A license plate has maybe 10^8 possible values, which any laptop can
walk through in seconds. A keyed hash is only as strong as the key, so:

    1. RETENTION. Private sightings are deleted after a short window
       (default 14 days). A trail you cannot assemble cannot be abused.

    2. PEPPER ROTATION. The hash key is rotated on a cadence (default 30 days).
       After a rotation the same car hashes differently, so trails cannot be
       joined across the boundary. This caps how long anyone - including
       whoever runs the hub - can follow a private citizen, even with full
       database access.

    3. NO LOOKUP PATH. There is deliberately no endpoint that turns a plate
       into a private-tier history. See `lookup_private_plate` at the bottom
       for why the obvious "let owners see their own trail" feature is
       switched off until there is a real proof-of-ownership channel.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import time
from hashlib import blake2b
from typing import Iterable, Optional

from core import CONFIG, DATA, now

PEPPER_PATH = DATA / "pepper.json"

_PLATE_STRIP = re.compile(r"[^A-Z0-9]")

# Characters an OCR reliably confuses. Plates are normalised through this map
# so that "0" and "O" do not produce two different hashes for one car. This
# costs a little collision resistance and buys a lot of tracking accuracy.
_OCR_FOLD = str.maketrans({"O": "0", "I": "1", "Q": "0", "S": "5", "Z": "2", "B": "8"})


def normalize_plate(text: str) -> str:
    """Canonical form of a plate read. Uppercase, alphanumeric only, OCR-folded."""
    if not text:
        return ""
    return _PLATE_STRIP.sub("", text.upper()).translate(_OCR_FOLD)


# --------------------------------------------------------------------------
# Pepper (the hash key)
# --------------------------------------------------------------------------

def _new_pepper() -> dict:
    return {"epoch": 0, "created": now(), "key": secrets.token_hex(32), "previous": []}


def _load_pepper() -> dict:
    if not PEPPER_PATH.exists():
        p = _new_pepper()
        _save_pepper(p)
        return p
    return json.loads(PEPPER_PATH.read_text(encoding="utf-8"))


def _save_pepper(p: dict) -> None:
    PEPPER_PATH.write_text(json.dumps(p, indent=2), encoding="utf-8")
    # Best effort on Windows: the pepper is the one file whose theft turns
    # every stored hash back into a readable plate.
    try:
        os.chmod(PEPPER_PATH, 0o600)
    except Exception:
        pass


def current_pepper() -> dict:
    """Return the live pepper, rotating it first if it has aged out."""
    p = _load_pepper()
    days = float(CONFIG.get("pepper_rotation_days", 30) or 0)
    if days > 0 and (now() - p["created"]) > days * 86400:
        p = rotate_pepper(p)
    return p


def rotate_pepper(p: Optional[dict] = None) -> dict:
    """Retire the current key and mint a new one.

    We keep exactly one generation of history, which is enough to still search
    a car's own recent sightings across a rotation boundary as long as the
    retention window is shorter than the rotation cadence. Older keys are
    destroyed, and once a key is gone those hashes are permanently opaque.
    """
    p = p or _load_pepper()
    prev = [{"epoch": p["epoch"], "key": p["key"], "created": p["created"]}]
    p = {
        "epoch": p["epoch"] + 1,
        "created": now(),
        "key": secrets.token_hex(32),
        "previous": prev,          # deliberately truncated to one generation
    }
    _save_pepper(p)
    return p


def plate_hash(plate: str, state: str = "", pepper: Optional[dict] = None) -> str:
    """Keyed, epoch-tagged hash of a plate. This is what the database stores.

    Format: ``e<epoch>:<32 hex chars>``. The epoch prefix makes it obvious at a
    glance that two hashes from different generations are not comparable, and
    lets the purge job find pre-rotation rows.
    """
    norm = normalize_plate(plate)
    if not norm:
        return ""
    p = pepper or current_pepper()
    mac = hmac.new(bytes.fromhex(p["key"]),
                   f"{state.upper()}|{norm}".encode("utf-8"),
                   blake2b).hexdigest()[:32]
    return f"e{p['epoch']}:{mac}"


def plate_hashes_all_epochs(plate: str, state: str = "") -> list[str]:
    """Every hash this plate could currently carry (live epoch + retained one).

    Used only by the operator-side tools. Never wire this to a public route.
    """
    p = current_pepper()
    out = [plate_hash(plate, state, p)]
    for old in p.get("previous", []):
        out.append(plate_hash(plate, state, old))
    return out


# --------------------------------------------------------------------------
# Tiering
# --------------------------------------------------------------------------

def tier_for(vclass: str) -> str:
    """'public' if this vehicle class is a public record, else 'private'."""
    return "public" if vclass in set(CONFIG.get("public_tiers", [])) else "private"


def audit_ip(ip: str) -> str:
    """What the audit log is allowed to remember about an address.

    🚨 AN AUDIT LOG OF WHO LOOKED AT WHAT IS A SUBPOENA TARGET.
    The table exists for a good reason - "we are building a surveillance
    network; the least we can do is be the first thing it watches" - but a
    column of raw addresses turns the accountability record into the exact
    kind of dossier the project refuses to build about vehicles. Somebody
    checking whether police are near their protest should not be identifiable
    from this file later.

    So the address is kept as a per-day keyed hash: repeated actions from one
    person still group together within a day, which is all abuse triage needs,
    and nothing survives the day to be joined to a name. Keyed with the same
    rotating pepper as plate hashes, so it cannot be brute-forced from the
    (small) space of possible addresses - an unkeyed hash of an IPv4 is
    reversible in seconds.
    """
    if not ip:
        return ""
    day = int(now() // 86400)
    key = bytes.fromhex(current_pepper()["key"])
    return "ip:" + hmac.new(key, f"{day}|{ip}".encode(), blake2b).hexdigest()[:16]


# --------------------------------------------------------------------------
# Per-day plate-hash alias mapping
#
# Moved from hub.py in Stage 2B so route modules that need to resolve a
# client-supplied alias back to a real plate hash (e.g. /api/track/<hash>)
# can do so without importing hub. This is the SAME dict/list objects hub.py
# used to own directly - hub.py now aliases them rather than keeping a
# second, disjoint copy, exactly like the microcache/admission/tiles pattern
# from Stage 1B.
# --------------------------------------------------------------------------

ALIAS: dict[str, str] = {}
ALIAS_DAY = [0]


def alias_map(rows: list[dict]) -> None:
    """Record which per-day aliases (see redact()) map back to real hashes."""
    day = int(now() // 86400)
    if day != ALIAS_DAY[0]:
        ALIAS.clear()
        ALIAS_DAY[0] = day
    for r in rows:
        red = redact(r, "anon")
        # `or ""` matters: plate_hash is NULL for a pass with no readable
        # plate, and dict.get's default does not fire on a present-but-None key.
        if (red.get("plate_hash") or "").startswith("a:") and r.get("plate_hash"):
            ALIAS[red["plate_hash"]] = r["plate_hash"]


def resolve_hash(h: str) -> str:
    """Turn a client-supplied alias back into the real hash, if we minted it."""
    return ALIAS.get(h, h)


def public_rows(rows: list[dict]) -> list[dict]:
    """Anonymized/redacted view of `rows`, with alias bookkeeping recorded."""
    alias_map(rows)
    return [redact(r, "anon") for r in rows]


def redact(row: dict, viewer: str = "anon") -> dict:
    """Strip anything the given viewer is not entitled to see.

    This runs on the way OUT of the database on every single read path. The
    database itself never holds private plate text, so this is a second belt on
    top of braces, but the belt is cheap and the failure mode is somebody's
    address.
    """
    r = dict(row)

    # 🚨 WHICH CAMERA SAW IT IS NOT PUBLIC.
    #
    # Every sighting was serving `node_id`, so anyone could group a volunteer's
    # entire output by camera and, with the accurately-published road span,
    # build a profile of one identifiable installation: its hours, its quiet
    # days, when it stopped reporting. For a project whose contributors are
    # photographing police, that is the thing worth protecting.
    #
    # Same treatment the plate hash already gets: a per-day rolling alias, so a
    # camera's sightings still group together ON SCREEN - which the map needs -
    # but cannot be joined up across days into a history of one household.
    #
    # Operators keep the real id; they are looking at their own cameras.
    if viewer == "anon" and r.get("node_id"):
        day = int(now() // 86400)
        r["node_id"] = "n:" + blake2b(
            f"{day}|{r['node_id']}".encode(), digest_size=4).hexdigest()

    # 🚨 AND THE ALIAS ABOVE IS DEFEATED IF THE REAL ID IS ALSO IN THE REASON.
    # Phone sightings write "human-submitted by <real node_id>" into vclass_why
    # (hub._ingest), so an anon viewer could read the true id straight out of the
    # reason string and re-join a camera's whole output - exactly what the
    # per-day alias exists to prevent. Rewrite the reason to carry the ALIASED id
    # instead, so the explanation survives without the identifier.
    if viewer == "anon" and r.get("vclass_why") and row.get("node_id"):
        r["vclass_why"] = str(r["vclass_why"]).replace(
            str(row["node_id"]), r.get("node_id", "a camera"))

    # Internal machinery that has no business on a public read path. Each of
    # these is small on its own; together they describe how the classifier
    # works, which crop backs which claim, and which rows an operator has
    # looked at - none of it needed to draw a map.
    if viewer == "anon":
        for k in ("bank_ref", "sig_ok", "confirmed_by", "detections",
                  "reviewed_at", "plate_conf", "vclass_conf"):
            r.pop(k, None)

    # 🚨 A GOVERNMENT PLATE IS SEARCHABLE ONLY AFTER A HUMAN CONFIRMS IT.
    #
    # the operator wants police and government plates searchable, and that is the
    # point of the public tier. But the classifier runs at ~95% precision, so
    # one in twenty vehicles it calls government is not one - and publishing a
    # SEARCHABLE plate for those is precisely the harm this project exists to
    # prevent. The photograph already shows the plate on a public-tier row;
    # what a text field adds is aggregation across time, which is the higher-
    # risk capability and therefore the one that gets the stricter gate.
    #
    # Operator confirmation is that gate. It is the same judgement that already
    # promotes and retracts, and a retraction purges the plate with it
    # (db.review_sighting). Until somebody has looked, the plate stays out of
    # every read path - which is what the note in hub._ingest means by
    # "unverified".
    if r.get("tier") == "public" and r.get("reviewed") != "confirmed":
        r.pop("plate_text", None)
        r.pop("plate_state", None)

    if r.get("tier") != "public":
        r.pop("plate_text", None)
        r.pop("plate_state", None)
        # 🚨 AND THE VEHICLE LINK, WHICH IS THE STRONGEST THING ON THE ROW.
        #
        # A tag says "these sightings are probably one vehicle", which across
        # cameras and time is a track. db.tag_sighting already refuses to write
        # one to anything but a published police or government row, so this
        # should be unreachable - and it is here for the same reason the plate
        # redaction above is, on a database that never holds private plate
        # text. The belt is cheap and the failure mode is following somebody
        # home.
        for k in ("vehicle_tag", "tag_conf", "tag_why", "tag_rev"):
            r.pop(k, None)
        # 🚨 AND THE PHOTOGRAPH. A private-tier row still carries `snap` (the
        # image filename, servable via /snap/<name>), and a photograph of a car
        # IS a photograph of its plate. redact used to strip the plate TEXT but
        # leave the picture, so an anon reader enumerating /api/sighting/<id> on
        # a non-mirror hub could pull the private image the text redaction was
        # protecting. The mirror strips this at ingest; the read path must too,
        # so a hub that is ever exposed does not leak what the mirror would not.
        # Anon only: the operator reviews private crops on the home hub and must
        # still see the redacted photo to judge it.
        if viewer == "anon":
            r.pop("snap", None)
        # The hash is stable and would let a client correlate a car across the
        # whole map. Give anonymous viewers a short, per-day rolling alias
        # instead, so a track is followable on screen but not archivable.
        h = r.get("plate_hash") or ""
        if h and viewer == "anon":
            day = int(now() // 86400)
            r["plate_hash"] = "a:" + blake2b(
                f"{day}|{h}".encode(), digest_size=6).hexdigest()
    return r


# --------------------------------------------------------------------------
# Retention
# --------------------------------------------------------------------------

def _pen_ids() -> list:
    """Sighting ids currently sitting in the review pen, awaiting a human.

    Read from the pen directory rather than inferred from the row, because the
    pen is the actual record of "somebody has been asked about this" - a row's
    own columns cannot distinguish a candidate nobody queued from one a
    reviewer is looking at right now.

    Failing OPEN (empty list) on any error is deliberate: this guards a DELETE,
    and the safe direction for an unreadable pen is to delete nothing extra,
    not to delete everything. An empty list means the sweep behaves exactly as
    it did before this existed.
    """
    try:
        import mirror
        if not mirror.REVIEW.exists():
            return []
        out = []
        for j in mirror.REVIEW.glob("*.json"):
            try:
                out.append(int(j.stem))
            except ValueError:
                continue
        return out
    except Exception:
        return []


def purge_expired(conn) -> dict:
    """Delete sightings past their retention window. Safe to run often.

    Returns a small report so the hub can surface "we deleted N rows today" on
    the public transparency page. A retention promise nobody can see is a
    retention promise nobody can check.
    """
    from core import SNAPS

    report = {"private_deleted": 0, "public_deleted": 0, "snaps_deleted": 0}
    cur = conn.cursor()

    priv_days = float(CONFIG.get("civilian_retention_days", 14) or 0)
    pub_days = float(CONFIG.get("public_retention_days", 0) or 0)

    # 🚨 NEVER DELETE A ROW A HUMAN HAS BEEN ASKED ABOUT.
    #
    # Everything in the review pen is tier != 'public' by definition - that is
    # what "awaiting a decision" means - so the private retention sweep was on
    # a collision course with it. Nothing prunes the pen, so a card sits there
    # indefinitely while its row is 14 days old and gets deleted underneath it.
    #
    # What the reviewer would then see is the worst part: a real crop, a
    # confirm button, promote_sighting updating ZERO rows, the crop deleted, an
    # audit entry written under their name, and {"ok": true} returned. Their
    # judgement is destroyed and they are told it landed. That is a far worse
    # outcome than keeping a row a few days past its window.
    #
    # Retention is a promise about vehicles nobody is looking at. A sighting a
    # person has been asked to rule on is not that, and the pen is the record
    # of having asked.
    pending = _pen_ids()
    keep_sql = ""
    if pending:
        keep_sql = " AND id NOT IN (%s)" % ",".join("?" * len(pending))

    doomed: list[str] = []
    if priv_days > 0:
        cutoff = now() - priv_days * 86400
        args = (cutoff, *pending)
        rows = cur.execute(
            "SELECT id, snap FROM sightings WHERE tier != 'public' AND ts < ?"
            + keep_sql, args).fetchall()
        doomed += [r["snap"] for r in rows if r["snap"]]
        cur.execute("DELETE FROM sightings WHERE tier != 'public' AND ts < ?"
                    + keep_sql, args)
        report["private_deleted"] = cur.rowcount
        report["held_for_review"] = len(pending)

    if pub_days > 0:
        cutoff = now() - pub_days * 86400
        rows = cur.execute(
            "SELECT id, snap FROM sightings WHERE tier = 'public' AND ts < ?",
            (cutoff,)).fetchall()
        doomed += [r["snap"] for r in rows if r["snap"]]
        cur.execute("DELETE FROM sightings WHERE tier = 'public' AND ts < ?", (cutoff,))
        report["public_deleted"] = cur.rowcount

    conn.commit()

    for name in doomed:
        f = SNAPS / name
        try:
            if f.exists():
                f.unlink()
                report["snaps_deleted"] += 1
        except Exception:
            pass

    # 🚨 THE PEN EXEMPTION ABOVE HAS A SHARP EDGE NOW.
    # `pending` keeps a candidate's ROW alive past the private window because a
    # human has not answered - which is right, and which now also means an
    # unredacted full-resolution original sits beside it for as long as nobody
    # does. A queue that is never worked would have held those indefinitely
    # under a rule written to protect the decision, not the picture. So the
    # imagery expires on its own much shorter clock (core.EVIDENCE_TTL_S),
    # independent of the row and independent of anyone remembering to look.
    try:
        import mirror
        report["evidence_swept"] = mirror.evidence_sweep()
    except Exception:
        pass
    return report


# --------------------------------------------------------------------------
# The feature that is deliberately switched off
# --------------------------------------------------------------------------

def lookup_private_plate(conn, plate: str, state: str = ""):
    """Return a private vehicle's own trail. DISABLED, and here is why.

    "Let a driver prove ownership and see their own trail" is a genuinely good
    feature - it answers "is somebody following me?", which is the one question
    a surveillance network can answer *for* an ordinary person instead of about
    them.

    The problem is that proving ownership over the internet is the whole
    difficulty. Any self-service version - type your plate, see your history -
    is indistinguishable from typing someone ELSE's plate and seeing theirs.
    That single endpoint would convert SparrowMap from an accountability tool into
    exactly the stalking service this design exists to avoid.

    Doing it properly needs an out-of-band channel binding a plate to a person:
    a code mailed to the address on the registration, an authenticated state
    DMV integration, or an in-person check at a community meeting. Until one of
    those exists, this raises.
    """
    raise PermissionError(
        "Private-tier plate lookup is disabled by design. It requires "
        "out-of-band proof of vehicle ownership, which this deployment does "
        "not have. See privacy.lookup_private_plate."
    )
