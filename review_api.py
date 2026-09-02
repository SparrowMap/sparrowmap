"""The reviewer API: serve the pending pool, cast verdicts, manage tokens.

The pool is the review pen (mirror.REVIEW): sub-resolution, plate-less crops of
government CANDIDATES that a human has to confirm before they reach the map -
his own camera's ambiguous calls (parked at ingest) and contributors' calls
that were not clearly-marked enough to auto-publish (parked by box_puller). A
reviewer confirms the real ones onto the map and rejects the rest; every verdict
is audited under their name and is reversible.

The hub wires HTTP routes to these functions and enforces who may call them
(a valid reviewer token for the queue/verdict, the operator secret for token
management). Nothing here trusts the caller on its own.
"""

from __future__ import annotations

import base64
import io
import json
import secrets
import time
from pathlib import Path
from typing import Optional

from PIL import Image

import db
import mirror
import snapshot
import review_auth
from core import EVIDENCE, HELD, SNAPS


def _pen_meta(sid: int) -> dict:
    mp = mirror.REVIEW / f"{sid}.json"
    if mp.exists():
        try:
            return json.loads(mp.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _score(meta: dict, row: Optional[dict]) -> Optional[float]:
    if meta.get("score") is not None:
        return meta["score"]
    head = meta.get("head") or {}
    if head.get("conf") is not None:
        return head["conf"]
    return (row or {}).get("vclass_conf")


def head_rejected(meta: dict) -> bool:
    """Did the trained head already rule this crop out?

    🚨 THE PEN WAS SHOWING HUMANS CARS THE MODEL HAD ALREADY DISMISSED.
    Contributor crops are parked here by box_puller, which used to admit
    anything CLIP's bare argmax called a marked class - even when the head had
    scored it and said no. The head's number was written into the `why` string
    the reviewer reads, and then ignored by the gate that put it there. 477 of
    485 live pen items arrived that way from one browser contributor, CLIP at
    ~0.50 against a head at ~0.00, and a queue full of ordinary cars reads as
    the classifier getting worse rather than as a filter that was never applied.

    Both numbers must be present to hide anything. A score without its operating
    point is not a judgement, and this box cannot load the head to supply one -
    so an item that does not carry its own threshold is SHOWN, not hidden.
    Failing towards a human looking at an extra car is the safe direction; the
    opposite failure hides a real patrol unit for ever.
    """
    h = meta.get("head") or {}
    conf, thr = h.get("conf"), h.get("threshold")
    if conf is None or thr is None:
        return False
    try:
        return float(conf) < float(thr)
    except (TypeError, ValueError):
        return False


def queue(reviewer: dict, scope: str = "pool", limit: int = 60,
          rejected: bool = False) -> dict:
    """Pending pool items this reviewer may see, newest first.

    `rejected=True` returns ONLY the items the trained head threw out - the
    pile the normal queue hides.

    🚨 THIS IS WHERE A MISSED PATROL CAR WOULD BE. The filter is right: a
    reviewer seeing an extra car costs seconds, and hiding a real patrol unit
    defeats the project, so the default queue stays filtered. But held-out
    recall is about 86%, so roughly one government vehicle in seven lands in
    this pile - and until now there was no way to look at it. A filter nobody
    can audit is a filter nobody should trust.

    They are returned SEPARATELY rather than merged in. Mixing them would lose
    the one fact that matters about them: the classifier already said no, and
    the reviewer is second-guessing it deliberately rather than working a queue.
    """
    allowed = reviewer.get("nodes")            # None = all nodes
    if not mirror.REVIEW.exists():
        return {"items": [], "count": 0, "hidden": 0}
    items = []
    hidden = 0
    for jp in mirror.REVIEW.glob("*.json"):
        try:
            sid = int(jp.stem)
        except ValueError:
            continue
        meta = _pen_meta(sid)
        row = db.sighting(sid) if hasattr(db, "sighting") else None
        node_id = meta.get("node_id") or (row or {}).get("node_id") or ""
        node_name = meta.get("node_name")
        if not node_name and node_id:
            nd = db.node(node_id)
            node_name = (nd or {}).get("name")
        # 'own' scope, or a pool token that names specific nodes, filters here.
        #
        # ⚠️ SCOPE IS APPLIED BEFORE THE HEAD FILTER ON PURPOSE. It used to be
        # counted after, so `hidden` included other people's cameras and a
        # single-camera volunteer was told hundreds of their crops were held
        # back when almost none of them were theirs. A number about someone
        # else's cameras is worse than no number.
        if allowed is not None and node_id not in allowed:
            continue
        # ⚠️ "MINE" HAS TO KEEP MEANING MINE FOR A POOL-SCOPED REVIEWER.
        # A camera key now grants the pool (see review_auth.identify), so
        # `allowed` is None for it and this filter would pass everything -
        # "my queue" would quietly become the whole network's queue. own_nodes
        # is the camera the key belongs to, and it is what "mine" means.
        mine_only = reviewer.get("own_nodes") or allowed
        if scope == "mine" and mine_only is not None and node_id not in mine_only:
            continue
        was_rejected = head_rejected(meta)
        if was_rejected:
            hidden += 1
        if was_rejected != bool(rejected):
            continue
        items.append({
            "id": sid, "node_id": node_id,
            "node_name": node_name or "a camera",
            "score": _score(meta, row),
            "ts": meta.get("ts") or (row or {}).get("ts"),
            "crop": f"/api/rv/crop/{sid}",
            "rejected": was_rejected,
            # 🚨 A FLAG THE REVIEWER CANNOT SEE IS STILL A BLACK HOLE.
            # park_reported writes `why` into the pen meta under a comment
            # saying it is "shown to the reviewer as the reason this is in
            # front of them", and this function never returned it - so a public
            # flag arrived looking exactly like a fresh candidate. The reviewer
            # then answered the question the card appeared to ask ("is this a
            # government vehicle?") instead of the one that was actually put
            # ("somebody says this is wrong, and here is why"). Routing the
            # report into the pen was only half the fix.
            "reported": bool(meta.get("reported")),
            "report_reason": meta.get("report_reason") or "",
            "why": meta.get("why") or "",
        })
    if rejected:
        # 🚨 NEWEST-FIRST IS THE WRONG ORDER FOR AN AUDIT.
        # 89% of this pile scores below 0.01 - the head was not hesitating, it
        # was certain. Confirming a 0.0002 teaches nothing and changes no
        # decision, so a reviewer working newest-first spends their attention
        # on the least informative crops in the set. He did 60 that way and
        # found nothing, which was the expected outcome and therefore weak
        # evidence.
        #
        # The only crops where a miss can plausibly hide are the ones the head
        # nearly accepted. Highest score first puts those on top: ten of them
        # say more about whether the filter is safe than sixty from the floor,
        # and they are also the labels worth having, because a confirmed
        # negative next to the threshold moves the boundary and one at 0.0002
        # does not.
        items.sort(key=lambda x: x.get("score") or 0.0, reverse=True)
    else:
        items.sort(key=lambda x: x.get("ts") or 0, reverse=True)
    # `hidden` is reported rather than swallowed. A filter that silently shrinks
    # a queue is indistinguishable from a queue that is genuinely empty, and
    # this one is capable of hiding hundreds of items at once.
    return {"items": items[:limit], "count": len(items), "hidden": hidden}


def crop_bytes(sid: int) -> Optional[bytes]:
    """The picture a reviewer judges by, and the one a confirmation publishes.

    🚨 THE ORIGINAL FIRST, THE 200px PEN COPY ONLY AS A FALLBACK.
    Both callers want the best picture that exists: the reviewer deciding
    whether this is a patrol car, and `_publish`, which attaches whatever this
    returns as the published photograph. Reading the pen copy meant every
    confirmed sighting on the map was a 200px thumbnail of a car - the operator
    marked one from the road and got a picture too small to show what he had
    just seen - and the reviewer was judging livery at the same size.
    Sub-resolution is a rule about PLATES on vehicles nobody has vouched for;
    applying it to the government candidate itself destroyed the record.

    core.EVIDENCE holds the full-resolution original for exactly as long as the
    decision is outstanding, and only at home. When it is not there - a mirror,
    a swept candidate, a reported item parked by `park_reported` - the pen copy
    is still correct and still what gets used.
    """
    for p in (EVIDENCE / f"{int(sid)}.jpg", mirror.REVIEW / f"{int(sid)}.jpg"):
        if p.exists():
            try:
                return p.read_bytes()
            except Exception:
                continue
    return None


def contributed(reviewer: dict) -> dict:
    """What this reviewer's camera(s) have actually contributed.

    🚨 AN EMPTY QUEUE HAS TO READ AS SUCCESS, NOT AS A BROKEN PAGE.
    A volunteer opens their review link and, most of the time, correctly sees
    nothing: the head rejected everything their camera caught, which is the
    normal and desirable outcome. "Nothing waiting for review" next to a blank
    screen reads as "this doesn't work", and the person who just installed a
    camera concludes their camera is not working either.

    So the empty state gets to say what the camera HAS done. These are counts,
    never rows - no plate, no image, nothing about any individual vehicle. A
    contributor is told the size of their contribution, not shown the traffic.
    """
    allowed = reviewer.get("nodes")          # None = pool scope = the whole network
    conn = db.connect()
    if allowed is None:
        where, args = "1=1", ()
    else:
        nodes = [n for n in allowed if n]
        if not nodes:
            return {"scope": "own", "cameras": 0, "sightings": 0, "published": 0}
        where = "node_id IN (%s)" % ",".join("?" * len(nodes))
        args = tuple(nodes)
    row = conn.execute(
        f"SELECT COUNT(*) AS n, "
        f"SUM(CASE WHEN tier='public' THEN 1 ELSE 0 END) AS pub, "
        f"COUNT(DISTINCT node_id) AS cams "
        f"FROM sightings WHERE {where}", args).fetchone()
    return {
        "scope": "pool" if allowed is None else "own",
        "cameras": int(row["cams"] or 0),
        "sightings": int(row["n"] or 0),
        "published": int(row["pub"] or 0),
    }


def is_trusted(reviewer: dict) -> bool:
    """May this reviewer see the retracted-photo shelf?

    Pool scope, which today is one token. The other reviewers are 'own'-scoped
    volunteers who may only see their own camera.

    🔑 WHY THIS GATE AND NOT AN OPERATOR ONE. A mirror deliberately has NO
    operator surface - mirror.route_allowed 404s /review and /api/operator
    outright, on the argument that a disabled review page is still a review
    page to find a bug in. Adding an operator route here to reach these photos
    would reopen exactly that. The gate reuses an existing, audited, revocable
    mechanism instead of cutting a new hole.

    🚨 IT USED TO SAY `scope == "pool"` AND THAT STOPPED BEING SAFE.
    The reasoning above rested on a sentence that is no longer true - "pool
    scope, which today is one token". Pool review is now self-service: any
    camera owner can take a pool token, because a crowd-labelling queue only
    the operator can reach is not a crowd. The old justification was that a
    pool reviewer "can already see every crop and publish or retract anything,
    so showing them these photographs grants no power they did not hold" - and
    that is still true of PUBLISHING. It is not true of DELETION. These are
    photographs of sightings the map has already withdrawn; the shelf exists to
    clear them, and deletion is the one action here that cannot be undone by
    another reviewer. Handing that to every signup after a viral week is not a
    trade anyone chose.

    So trust follows WHO MINTED THE TOKEN, not what it is scoped to.
    `created_by="self"` is the self-service door; anything else was issued by
    the operator by hand.
    """
    return (reviewer.get("nodes") is None
            and reviewer.get("scope") == "pool"
            and (reviewer.get("created_by") or "") != "self")


def retracted(reviewer: dict) -> dict:
    """Photographs still on disk for sightings the map no longer claims."""
    if not is_trusted(reviewer):
        return {"items": [], "count": 0}
    out = []
    for r in db.retracted_with_photo():
        if not (SNAPS / Path(str(r["snap"])).name).exists():
            continue          # row remembers a file that is already gone
        node = db.node(r.get("node_id") or "") or {}
        out.append({
            "id": r["id"], "ts": r.get("ts"),
            "retracted_at": r.get("reviewed_at"),
            "node_name": node.get("name") or "a camera",
            "vclass": r.get("vclass"), "why": r.get("vclass_why"),
            "photo": f"/api/rv/retracted/photo/{r['id']}",
        })
    return {"items": out, "count": len(out)}


def delete_retracted_photo(reviewer: dict, sid: int, ip: str = "") -> dict:
    """Delete the leftover photograph of a retracted sighting.

    Row reference first, file second - see db.clear_snap. The sighting itself
    is kept: the vehicle really did pass, and deleting the row would replace
    one wrong claim with another. Only the picture goes.
    """
    if not is_trusted(reviewer):
        return {"ok": False, "error": "not permitted"}
    row = db.sighting(int(sid))
    if not row or row.get("reviewed") != "retracted":
        return {"ok": False, "error": "not a retracted sighting"}
    name = db.clear_snap(int(sid))
    if not name:
        return {"ok": False, "error": "no photo to delete"}
    try:
        (SNAPS / Path(name).name).unlink(missing_ok=True)
    except Exception:
        pass                  # reference is already cleared; an orphan file is tidy-up
    db.audit("retracted:photo_delete", str(sid),
             actor=reviewer.get("label") or "reviewer", ip=ip)
    return {"ok": True, "id": int(sid)}


def retracted_photo_bytes(reviewer: dict, sid: int) -> Optional[bytes]:
    if not is_trusted(reviewer):
        return None
    row = db.sighting(int(sid))
    if not row or row.get("reviewed") != "retracted" or not row.get("snap"):
        return None
    p = SNAPS / Path(str(row["snap"])).name
    if not p.exists():
        return None
    try:
        return p.read_bytes()
    except Exception:
        return None


def park_reported(sid: int, row: dict, reason: str = "") -> bool:
    """Put a publicly-flagged sighting in front of a human.

    🚨 A FLAG THAT ONLY REACHES A TABLE IS A BLACK HOLE.
    /api/report wrote a `reports` row and stopped. The only reader was the
    OPERATOR queue, which is local-only and does not exist on a public mirror
    at all - so on the live site every public flag landed somewhere nothing
    running on that machine could display. Six real reports sat unresolved,
    four of them "not a government vehicle": the public correctly telling us a
    car was wrong, into a void.

    Parking it in the pen puts it in the reviewer app beside everything else,
    and the existing verdict path already does the right thing from there:
    "not a cop" calls db.review_sighting(sid, "retracted"), which demotes the
    row to private, drops the plate text, and resolves the flags.

    Note what is deliberately NOT recorded here: no `head` verdict. The queue
    filter hides an item only when it has both a score and a threshold, so an
    item with neither is always shown - which is what a human flag deserves.
    A model already had its say on this vehicle and a person is disagreeing.
    """
    if not row or row.get("tier") != "public":
        return False
    jp = mirror.REVIEW / f"{int(sid)}.json"
    if jp.exists():
        return False                      # already queued; a second flag is not a second job
    snap = row.get("snap")
    if not snap:
        return False
    src = SNAPS / Path(str(snap)).name
    if not src.exists():
        return False
    try:
        crop = snapshot.subres_from_stored(src.read_bytes())
    except Exception:
        return False
    node = db.node(row.get("node_id") or "") or {}
    return bool(mirror.review_write(int(sid), crop, {
        "ts": row.get("ts"),
        "node_id": row.get("node_id") or "",
        "node_name": node.get("name") or "a camera",
        "vclass": row.get("vclass"),
        "body": row.get("body"),
        "reported": True,
        "report_reason": reason,
        # Shown to the reviewer as the reason this is in front of them. It is
        # the public's claim, attributed as such - not the model's.
        "why": f"flagged by the public: {db.REPORT_REASONS.get(reason, reason)}",
    }))


def _flags(sid: int) -> list:
    try:
        return db.reports_for(int(sid), open_only=True) or []
    except Exception:
        return []


def _flag_reason(sid: int) -> str:
    f = _flags(sid)
    if not f:
        return ""
    return db.REPORT_REASONS.get(f[0].get("reason") or "", f[0].get("reason") or "")


def _flag_note(sid: int) -> str:
    for f in _flags(sid):
        if f.get("note"):
            return str(f["note"])[:500]
    return ""


def hold_photo(sid: int, row: dict, reason: str = "") -> bool:
    """Take a published photograph off the map NOW, before anyone reviews it.

    🚨 CLEARING THE COLUMN IS NOT TAKING IT DOWN. `/snap/<name>` serves any file
    in SNAPS by name, with no reference to the row - that is precisely how the
    retracted-photo shelf came to exist. So a hold MOVES the file into core.HELD,
    which no route serves, and only then clears `snap`. Reference-first would
    leave a window where the row says nothing and the URL still answers.

    📌 Why this one reason acts before a human does. Every other flag is a claim
    about a VEHICLE, and being wrong about a vehicle for a few hours is
    correctable. This one is a claim about a PERSON who did not ask to be on the
    map - the arm on the wheel of the car alongside, a face through a window, a
    watch. The harm accrues while the queue waits, and the queue is measured in
    hours. Failing towards nobody seeing the photograph is the recoverable
    direction; the opposite failure is not recoverable at all.

    It is a hold and never a delete. The flag may be mistaken, and the stored
    original is the only thing a full-quality crop can be cut from - cropping
    the pen's 200px copy instead would permanently degrade a photograph that
    turned out to be fine.

    ⚠️ ABUSE IS REAL AND ACCEPTED. Anyone can flag, so anyone can strip photos
    (rate-limited to 20/hr per IP). A reviewer restores or crops them back, and
    the sighting itself - dot, class, time - never moves. A recoverable outage
    beats an unrecoverable leak. If it is ever used as a weapon the answer is to
    require a token for this reason, not to stop holding.
    """
    if not row:
        return False
    name = row.get("snap")
    if not name or row.get("snap_held"):
        return False              # nothing served, or already held
    name = Path(str(name)).name
    src = SNAPS / name
    if not src.exists():
        # The row points at a file that is gone: nothing is being served, so
        # there is nothing to hold. Clear the reference so the queue is not
        # given an item whose photograph can never be shown.
        db.set_snap_held(int(sid), None, None)
        return False
    try:
        HELD.mkdir(parents=True, exist_ok=True)
        dst = HELD / name
        if dst.exists():
            dst.unlink()          # a previous hold of the same file; the source wins
        src.replace(dst)          # move, not copy - a copy leaves the original served
    except Exception as exc:
        print(f"[review] could not hold photo for {sid}: {exc}")
        return False
    db.set_snap_held(int(sid), name, None)
    db.audit("photo:held", str(sid), actor="public", ip="")
    return True


def may_fix(reviewer: dict, row: dict) -> bool:
    """May this reviewer work on this held photograph at all?

    Same node rule as the pen: a pool token sees everything, an 'own' token sees
    only its own cameras. Read from the sighting row rather than pen meta,
    because a held photo has no pen card of its own.
    """
    allowed = reviewer.get("nodes")
    if allowed is None:
        return True
    return (row or {}).get("node_id") in allowed


def held_queue(reviewer: dict) -> dict:
    """Photographs held off the map, waiting for someone to look.

    Items carry `photo`, which is the ORIGINAL - the full-quality file, held but
    not destroyed. That is the whole reason it is kept: a crop cut from the pen's
    200px copy would permanently shrink a photograph the flag turned out to be
    wrong about.
    """
    allowed = reviewer.get("nodes")
    rows = db.held_photos(None if allowed is None else list(allowed))
    out = []
    for r in rows:
        name = Path(str(r.get("snap_held") or "")).name
        if not name or not (HELD / name).exists():
            continue              # row remembers a file that is already gone
        out.append({
            "id": r["id"], "ts": r.get("ts"), "held_at": r.get("held_at"),
            "node_id": r.get("node_id") or "",
            "node_name": r.get("node_name") or "a camera",
            "vclass": r.get("vclass"),
            "body": r.get("body"), "color": r.get("color"),
            # The newest OPEN flag, in the flagger's own words where they left
            # any. A reviewer who cannot see what was objected to is guessing at
            # which part of the frame to cut, and a wrong guess here publishes
            # the complaint again.
            "reason": _flag_reason(r["id"]),
            "note": _flag_note(r["id"]),
            "photo": f"/api/rv/held/photo/{r['id']}",
            # Restoring UNTOUCHED is the only action that puts the exact flagged
            # pixels back, so the page hides that button unless the token may.
            "may_restore": is_trusted(reviewer),
        })
    return {"items": out, "count": len(out)}


def held_photo_bytes(reviewer: dict, sid: int) -> Optional[bytes]:
    row = db.sighting(int(sid))
    if not row or not row.get("snap_held") or not may_fix(reviewer, row):
        return None
    p = HELD / Path(str(row["snap_held"])).name
    if not p.exists():
        return None
    try:
        return p.read_bytes()
    except Exception:
        return None


def _drop_file(directory: Path, name: Optional[str], except_id: int) -> None:
    """Unlink a photograph, unless another live sighting still points at it."""
    if not name:
        return
    name = Path(str(name)).name
    if db.snap_referenced_elsewhere(name, int(except_id)):
        return
    try:
        (directory / name).unlink(missing_ok=True)
    except Exception:
        pass


def fix_photo(reviewer: dict, sid: int, action: str,
              box: Optional[dict] = None, ip: str = "") -> dict:
    """Rule on a held photograph: crop it back, restore it whole, or destroy it.

    🔑 THE ASYMMETRY IS DELIBERATE. Cropping and deleting are open to any
    reviewer who may touch the camera; restoring UNTOUCHED needs a trusted
    token. Both of the open actions can only ever make the map show LESS of the
    person who complained - `tighten` cannot pan, zoom or reach back to the
    original frame, and delete removes the picture entirely while the sighting
    itself stays. Restoring is the single action that puts the exact flagged
    pixels back in public, so it is the single one that is gated.

    Making this queue easy to work in the private direction and hard to work in
    the public one is the only arrangement where opening it to volunteers is not
    a way to overrule the person in the photograph.
    """
    sid = int(sid)
    action = (action or "").lower()
    who = reviewer.get("label") or "reviewer"
    row = db.sighting(sid)
    if not row or not row.get("snap_held"):
        return {"ok": False, "error": "no held photo"}
    if not may_fix(reviewer, row):
        return {"ok": False, "error": "not your camera"}
    held_name = Path(str(row["snap_held"])).name
    src = HELD / held_name

    if action == "delete":
        db.set_snap_held(sid, None, None)
        _drop_file(HELD, held_name, sid)
        db.audit("photo:deleted", str(sid), actor=who, ip=ip)
        return {"ok": True, "id": sid, "action": "delete"}

    if action == "restore":
        if not is_trusted(reviewer):
            return {"ok": False, "error": "restoring an untouched photo is "
                                          "limited to an operator-issued token"}
        if not src.exists():
            db.set_snap_held(sid, None, None)
            return {"ok": False, "error": "the held file is gone"}
        try:
            SNAPS.mkdir(parents=True, exist_ok=True)
            src.replace(SNAPS / held_name)
        except Exception as exc:
            return {"ok": False, "error": f"could not restore: {exc}"}
        db.set_snap_held(sid, None, held_name)
        db.audit("photo:restored", str(sid), actor=who, ip=ip)
        return {"ok": True, "id": sid, "action": "restore"}

    if action == "crop":
        if not src.exists():
            db.set_snap_held(sid, None, None)
            return {"ok": False, "error": "the held file is gone"}
        try:
            raw = src.read_bytes()
        except Exception as exc:
            return {"ok": False, "error": f"could not read the photo: {exc}"}
        cropped = tighten(raw, box)
        if cropped == raw:
            # 🚨 SAY SO RATHER THAN PUBLISHING THE FLAGGED PICTURE.
            # tighten() returns the input untouched when the box is missing,
            # degenerate, full-frame or unparseable - which is right for the pen,
            # where the fallback is the crop the reviewer was already happy with.
            # Here the fallback would be re-publishing the exact photograph
            # somebody complained about, dressed as a fix. That is a restore, and
            # a restore is gated, so it cannot happen by accident through this door.
            return {"ok": False, "error": "that box did not change the photo - "
                                          "drag a smaller one"}
        try:
            name = f"{int(row.get('ts') or time.time())}_{secrets.token_hex(4)}.jpg"
            SNAPS.mkdir(parents=True, exist_ok=True)
            (SNAPS / name).write_bytes(cropped)
        except Exception as exc:
            return {"ok": False, "error": f"could not write the crop: {exc}"}
        db.set_snap_held(sid, None, name)
        # Only now is the original expendable: the map is already serving the
        # cropped file, so an unlink that fails leaves a held orphan, not a gap.
        _drop_file(HELD, held_name, sid)
        db.audit("photo:cropped", str(sid), actor=who, ip=ip)
        return {"ok": True, "id": sid, "action": "crop", "snap": name}

    return {"ok": False, "error": "unknown action"}


def may_touch(reviewer: dict, sid: int) -> bool:
    """May this reviewer see or rule on this pen item?

    🚨 SCOPE HAS TO BE CHECKED ON EVERY ACCESS, NOT JUST ON THE LISTING.
    `queue()` filters an 'own'-scoped reviewer down to their own cameras, and
    that was the only place the check existed - so a token scoped to one camera
    could still fetch, publish or reject any other camera's crop simply by
    naming its id. Ids are sequential and appear next to sightings on the public
    map, so guessing one is not a feat. A pool token is unrestricted by design.
    """
    allowed = reviewer.get("nodes")
    if allowed is None:                       # pool scope: the whole queue
        return True
    meta = _pen_meta(sid)
    node_id = meta.get("node_id")
    if not node_id:
        row = db.sighting(sid) if hasattr(db, "sighting") else None
        node_id = (row or {}).get("node_id") or ""
    return node_id in allowed


def _delete_pen(sid: int) -> None:
    (mirror.REVIEW / f"{sid}.jpg").unlink(missing_ok=True)
    (mirror.REVIEW / f"{sid}.json").unlink(missing_ok=True)
    # 🚨 AND THE UNREDACTED ORIGINAL, ON EVERY VERDICT.
    # This function is the one path every outcome funnels through - confirm,
    # confirm-as-gov, reject, and the orphan-clearing branch - which is exactly
    # why the deletion belongs here rather than beside any one of them. The
    # justification for holding a full-resolution plate at all is that somebody
    # is about to look; the moment they have, it expires. `_publish` has
    # already taken its copy through crop_bytes by the time it calls this.
    (EVIDENCE / f"{sid}.jpg").unlink(missing_ok=True)


def tighten(raw: bytes, box: Optional[dict]) -> bytes:
    """Cut the published photo down to the rectangle a reviewer dragged.

    🚨 IT CAN ONLY EVER REMOVE PIXELS. The box arrives as four fractions of the
    image the reviewer was looking at, and every one is clamped into [0,1]
    before use, so the result is a sub-rectangle of the crop and nothing else.
    That is the property that makes this safe to expose to anyone with a token:
    an editor that could pan, zoom out or reach back to the original frame
    could serve pixels the sub-resolution guard and the background strip were
    put there to destroy. This cannot. The worst a malicious box does is
    publish a smaller picture.

    A too-small rectangle is IGNORED rather than honoured - under about 8% of
    either edge is a mis-drag or a stray tap, and silently publishing a
    forty-pixel smear as the evidence for a claim is worse than publishing the
    untouched crop.

    Returns the original bytes unchanged on any failure. The photograph is
    worth more than the trim.
    """
    if not box:
        return raw
    try:
        img = Image.open(io.BytesIO(raw))
        w, h = img.size

        def frac(key, default):
            v = float(box.get(key, default))
            return min(1.0, max(0.0, v))

        x0, y0 = frac("x", 0.0), frac("y", 0.0)
        x1, y1 = min(1.0, x0 + frac("w", 1.0)), min(1.0, y0 + frac("h", 1.0))
        px0, py0 = int(round(x0 * w)), int(round(y0 * h))
        px1, py1 = int(round(x1 * w)), int(round(y1 * h))
        if (px1 - px0) < w * 0.08 or (py1 - py0) < h * 0.08:
            return raw
        if (px1 - px0) >= w and (py1 - py0) >= h:
            return raw                      # the whole thing; nothing to do
        out = io.BytesIO()
        img.convert("RGB").crop((px0, py0, px1, py1)).save(
            out, "JPEG", quality=88)
        return out.getvalue()
    except Exception as exc:
        print(f"[review] could not apply the reviewer's crop: {exc}")
        return raw


def _publish(sid: int, reviewer: dict, force_vclass: Optional[str] = None,
             crop_box: Optional[dict] = None) -> None:
    """Publish a pen item. ``force_vclass`` is the REVIEWER'S own call.

    Without it the class is inherited from whatever the model guessed, which
    makes the two buttons a single decision: a human who can see the vehicle is
    a better judge of police-versus-ordinary-government than the classifier
    that could not tell them apart, and the distinction matters - a patrol car
    and a council pickup are the same tier but not the same claim.

    ``crop_box`` is an optional tighten-only rectangle the reviewer dragged.
    """
    meta = _pen_meta(sid)
    row = db.sighting(sid) if hasattr(db, "sighting") else None
    vclass = force_vclass or meta.get("vclass") or (row or {}).get("vclass") or "gov"
    vclass = "police" if vclass == "police" else "gov"
    kind = "a patrol vehicle" if vclass == "police" else "a government vehicle"
    why = f"confirmed by reviewer {reviewer.get('label')} as {kind}"

    # Attach the pen crop as the published photo (plate-less already).
    crop = tighten(crop_bytes(sid), crop_box)

    db.promote_sighting(sid, vclass, _score(meta, row), why)
    attach_confirmed_photo(sid, row, crop,
                           ts=meta.get("ts"),
                           node_name=meta.get("node_name") or "a camera",
                           vclass=vclass,
                           # 🚨 A REPORTED ITEM'S PEN CROP IS THE PUBLISHED FILE
                           # ITSELF (park_reported -> subres_from_stored), so it
                           # has already been stamped AND already had its
                           # background stripped. Neither is idempotent: the
                           # caption lands on top of the caption, and the strip
                           # keeps a rectangle of a rectangle. Both had to be
                           # suppressed on the same condition, and only one was.
                           stamp=not meta.get("reported"),
                           isolate=not meta.get("reported"))
    _delete_pen(sid)


def attach_confirmed_photo(sid: int, row: Optional[dict], crop: Optional[bytes],
                           *, ts: Optional[float] = None,
                           node_name: str = "a camera",
                           vclass: str = "police",
                           stamp: bool = True,
                           isolate: bool = True) -> Optional[str]:
    """Make ``crop`` the published photograph of an already-public sighting.

    🚨 ONE PUBLISH-A-PHOTO, BECAUSE THERE ARE NOW TWO HUMANS WHO CAN SAY YES.
    A reviewer confirms in the pen (`_publish` above) and a camera operator or
    a driver confirms at the camera (`/api/node/label`). Both are the same
    decision - a person vouched for this vehicle, so this picture becomes the
    map's picture - and the swap has three parts that are each easy to leave
    out. When this logic lived inside `_publish` the second path had none of
    them: it promoted the row and published whatever 200px thumbnail ingest had
    already stored, which is why every drive-mode and camera-labelled patrol
    car on the map is a 160-200px smudge.

    Returns the new snapshot name, or None if nothing was attached (no crop, or
    storing it failed) - in which case the row keeps the photograph it had.
    Publication itself is NOT this function's job and has already happened: a
    picture that cannot be stored must never cost the sighting its place on the
    map.
    """
    if not crop:
        return None
    snap = None
    try:
        data_url = "data:image/jpeg;base64," + base64.b64encode(crop).decode()
        # 🚨 A REPORTED ITEM IS ALREADY STAMPED. park_reported builds its
        # pen crop with subres_from_stored, i.e. from the file the map was
        # already serving, and that file was captioned when it was first
        # stored. Stamping it again lands the second caption on top of the
        # first at the same size and position, so it does not look like a
        # bug - the strip just goes darker and another layer of text is
        # burned into the evidence every time round the report-confirm
        # loop. Everything else in the pen arrives unstamped and still
        # needs one.
        snap = snapshot.store_confirmed(data_url, {
            "ts": ts or time.time(), "node_id": "review",
            "node_name": node_name,
            "tier": "public", "vclass": vclass, "watermark": "CONFIRMED"},
            stamp=stamp, isolate=isolate)
    except Exception:
        # 🚨 THE FALLBACK MUST STILL PROVE IT IS AN IMAGE.
        # It wrote whatever it was handed straight into SNAPS, which was
        # survivable while the only caller passed bytes it had just read out of
        # a stored .jpg. It is not survivable now: /api/node/label reaches this
        # from the network, and unopenable bytes would be written out as the
        # published photograph AND would delete the good one below - so a
        # corrupt upload does not merely fail, it destroys the picture it was
        # meant to improve. Caught by tools/test_fullres_on_confirm.py, which
        # is why that test hands it garbage on purpose.
        try:
            Image.open(io.BytesIO(crop)).verify()
            name = f"{int(ts or time.time())}_{secrets.token_hex(4)}.jpg"
            SNAPS.mkdir(parents=True, exist_ok=True)
            (SNAPS / name).write_bytes(crop)
            snap = name
        except Exception:
            snap = None
    if not snap:
        return None

    # 🚨 THE PHOTOGRAPH THIS REPLACES HAS TO STOP BEING SERVED.
    # This wrote the new name over the old one and left the old FILE in
    # place, and `/snap/<name>` hands out any file in SNAPS by name with no
    # reference to the row - so a reviewer who cropped a person out of a
    # published photo swapped which picture the map linked to and changed
    # nothing about what was reachable. The uncropped original stayed one
    # URL away, which is the same leak the retracted-photo shelf exists to
    # clean up after. Unlinked is not deleted.
    old = (row or {}).get("snap")
    conn = db.connect()
    conn.execute("UPDATE sightings SET snap=? WHERE id=?", (snap, sid))
    conn.commit()
    if old and Path(str(old)).name != snap:
        _drop_file(SNAPS, old, sid)
    # 🚨 TWO PATHS CAN PUBLISH A PHOTO AND ONLY ONE KNEW ABOUT THE HOLD.
    # A reported sighting is BOTH held and parked in the pen, so confirming
    # it here republished the picture while `snap_held` still pointed at the
    # held original. db.set_snap_held's own docstring says a row carrying
    # both is claiming the same picture is in two places - and it was: the
    # map served the new crop, the fix queue went on listing the item as
    # waiting, and the held file was orphaned in core.HELD with nothing left
    # to clean it up. Observed live on 45852.
    held = (row or {}).get("snap_held")
    if held:
        db.set_snap_held(sid, None, snap)
        _drop_file(HELD, held, sid)
    return snap


def verdict(reviewer: dict, sid: int, call: str, ip: str = "",
            crop_box: Optional[dict] = None) -> dict:
    sid = int(sid)
    call = (call or "").lower()
    who = reviewer.get("label") or "reviewer"
    if not may_touch(reviewer, sid):
        return {"ok": False, "error": "not your camera"}
    if call == "cop":
        # 🚨 TELL THE REVIEWER THE TRUTH IF THEIR DECISION CANNOT LAND.
        # promote_sighting now raises when the row is gone, which happens when
        # the retention sweep deletes a sighting whose card is still in the pen.
        # Returning {"ok": true} there told a person their judgement had been
        # recorded when it had been destroyed - and, worse, wrote an audit line
        # under their name saying so. The card is cleared because it is now an
        # orphan pointing at nothing, but the answer says plainly that nothing
        # was published.
        try:
            _publish(sid, reviewer, force_vclass="police",
                     crop_box=crop_box)
        except LookupError as exc:
            _delete_pen(sid)
            db.audit("review:confirm_failed", str(sid), actor=who, ip=ip)
            return {"ok": False, "id": sid, "error": str(exc),
                    "note": "this card has been cleared - the vehicle it "
                            "referred to is no longer in the database"}
        db.audit("review:confirm", str(sid), actor=who, ip=ip)
        return {"ok": True, "id": sid, "verdict": "cop"}
    if call == "gov":
        # 🚨 NO LONGER A PUBLISH. public_tiers is police-only (see core.py), and
        # _publish never consulted it - so this button was the one route that
        # could still put a council truck on a police map. The verdict is still
        # RECORDED, because "a human looked and said government-but-not-police"
        # is exactly the label the classifier needs to learn the difference; it
        # simply no longer reaches the public tier.
        #
        # What it used to say, and what is no longer true: "a publicly owned
        # vehicle doing public work is a public record either way". It still
        # is - it is just not what this map is FOR, and a council truck sitting
        # beside a patrol car weakens the only claim the project makes.
        _delete_pen(sid)
        db.review_sighting(sid, "confirmed_gov")
        db.audit("review:confirm_gov", str(sid), actor=who, ip=ip)
        return {"ok": True, "id": sid, "verdict": "gov",
                "note": "recorded as government but not published - the map "
                        "shows police vehicles only"}

    if call == "not":
        # Never published, so nothing to retract: drop the crop, leave the row
        # private. The verdict is logged so the call is attributable and the crop
        # can be harvested as a civilian training label at home.
        _delete_pen(sid)
        # 🚨 AND RECORD THAT A HUMAN LOOKED. Without this the row stays
        # `reviewed IS NULL`, which is exactly what backfill_pen searches for -
        # so every rejected vehicle came back to be judged again on the next
        # sweep, and a reviewer's work quietly undid itself.
        try:
            db.review_sighting(sid, "retracted")
        except Exception:
            pass
        db.audit("review:reject", str(sid), actor=who, ip=ip)
        return {"ok": True, "id": sid, "verdict": "not"}
    if call == "skip":
        db.audit("review:skip", str(sid), actor=who, ip=ip)
        return {"ok": True, "id": sid, "verdict": "skip"}
    return {"ok": False, "error": "unknown verdict"}


# --- token administration (operator-gated in the hub) ----------------------
def issue_token(label: str, scope: str, nodes: Optional[list]) -> dict:
    tok = review_auth.issue(label, scope, nodes, created_by="operator")
    return {"ok": True, "token": tok, "label": label, "scope": scope,
            "nodes": nodes or []}


def revoke_token(token_id: int) -> dict:
    return {"ok": review_auth.revoke(token_id)}


def list_tokens() -> dict:
    return {"tokens": review_auth.listing()}
