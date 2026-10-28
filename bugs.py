"""Bug reports from the site: store them privately, tell the operator they exist.

🚨 THE REPORT AND THE ALERT ARE DELIBERATELY SEPARATED.
A screenshot from somebody struggling with SparrowMap very often contains the
thing they were struggling WITH: their camera key, the QR that IS their camera
key, a reviewer token, or the street outside their window. They sent it to get
help, not to publish it.

The operator's alert channel is a GitHub issue, and ALERT_REPO defaults to the
PUBLIC code repository. So the alert carries an id, a page and a timestamp -
never the description, never the image. The content is read on an
operator-authenticated page. That split is the whole design; everything else
here is bookkeeping.

⚠️ THIS IS AN UNAUTHENTICATED UPLOAD ENDPOINT, which is a thing worth being
nervous about on a 3 GB box. Hence: a size cap enforced before anything is
decoded, a re-encode through Pillow that strips EXIF (a phone screenshot can
carry a GPS fix), a per-hour cap on how many reports exist at all, and a TTL
sweep so an unattended box cannot fill its disk with other people's pictures.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

import core

MAX_SHOT_BYTES = 4 * 1024 * 1024      # before decode
MAX_EDGE = 1400                       # a screenshot needs to stay readable
MAX_TEXT = 4000
MAX_PER_HOUR = 60                     # across the whole site


def _dir() -> Path:
    core.BUGS.mkdir(parents=True, exist_ok=True)
    return core.BUGS


def recent_count(window_s: int = 3600) -> int:
    cut = time.time() - window_s
    n = 0
    for p in _dir().glob("*.json"):
        try:
            if p.stat().st_mtime >= cut:
                n += 1
        except Exception:
            continue
    return n


def save(desc: str, shot_data_url: str = "", page: str = "",
         ua: str = "", extra: Optional[dict] = None) -> dict:
    """Store one report. Returns {id} or {error}."""
    desc = (desc or "").strip()[:MAX_TEXT]
    if not desc and not shot_data_url:
        return {"error": "say what went wrong, or attach a screenshot"}
    if recent_count() >= MAX_PER_HOUR:
        return {"error": "too many reports right now - please try again later"}

    d = _dir()
    # Sequential-ish and sortable, with no dependence on the clock being right.
    bid = f"{int(time.time())}_{len(list(d.glob('*.json'))) % 1000:03d}"

    shot_name = ""
    if shot_data_url:
        try:
            import base64
            import io
            from PIL import Image
            head, _, b64 = shot_data_url.partition(",")
            if "image" not in head:
                return {"error": "that attachment is not an image"}
            raw = base64.b64decode(b64, validate=False)
            if len(raw) > MAX_SHOT_BYTES:
                return {"error": "that screenshot is too large "
                                 f"({len(raw) // 1024 // 1024} MB) - "
                                 "please crop it or send a smaller one"}
            img = Image.open(io.BytesIO(raw))
            img.load()
            # 🚨 RE-ENCODE, NEVER STORE WHAT ARRIVED. A phone screenshot can
            # carry EXIF with the exact GPS fix of the person reporting a bug
            # about a project that exists to protect people's locations.
            img = img.convert("RGB")
            if max(img.size) > MAX_EDGE:
                s = MAX_EDGE / max(img.size)
                img = img.resize((max(1, int(img.width * s)),
                                  max(1, int(img.height * s))), Image.LANCZOS)
            shot_name = f"{bid}.jpg"
            img.save(d / shot_name, "JPEG", quality=80, optimize=True)
        except Exception as exc:
            return {"error": f"could not read that screenshot: {exc}"}

    rec = {"id": bid, "ts": time.time(), "desc": desc, "shot": shot_name,
           "page": (page or "")[:300], "ua": (ua or "")[:300],
           "extra": extra or {}, "status": "open"}
    (d / f"{bid}.json").write_text(json.dumps(rec, indent=1), encoding="utf-8")
    return {"id": bid, "shot": bool(shot_name)}


def listing(limit: int = 100, include_closed: bool = False) -> list:
    out = []
    for p in sorted(_dir().glob("*.json"), reverse=True):
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not include_closed and r.get("status") == "closed":
            continue
        out.append(r)
        if len(out) >= limit:
            break
    return out


def get(bid: str) -> Optional[dict]:
    p = _dir() / f"{_safe(bid)}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def shot_bytes(bid: str) -> Optional[bytes]:
    p = _dir() / f"{_safe(bid)}.jpg"
    return p.read_bytes() if p.exists() else None


def close(bid: str) -> bool:
    r = get(bid)
    if not r:
        return False
    r["status"] = "closed"
    r["closed_at"] = time.time()
    (_dir() / f"{_safe(bid)}.json").write_text(json.dumps(r, indent=1),
                                               encoding="utf-8")
    return True


def delete(bid: str) -> bool:
    """Destroy a report and its screenshot.

    The operator should be able to get rid of somebody's picture the moment it
    has served its purpose, without waiting for the sweep.
    """
    ok = False
    for suffix in (".json", ".jpg"):
        p = _dir() / f"{_safe(bid)}{suffix}"
        if p.exists():
            p.unlink(missing_ok=True)
            ok = True
    return ok


def sweep(now_ts: Optional[float] = None) -> int:
    """Destroy reports nobody dealt with in time. Returns how many REPORTS went.

    Runs on FILES, not records, so a damaged .json cannot keep its picture alive.
    A report is its .json plus its .jpg, aged together by the report's own `ts`
    (the file's mtime only when the .json is unreadable or missing). mtime alone
    was the wrong clock: closing a report rewrites its .json, so the picture went
    at 30 days and the record lingered another 30. Called by hub._janitor.
    """
    if not core.BUGS.exists():
        return 0
    cut = (now_ts if now_ts is not None else time.time()) - core.BUGS_TTL_S
    groups: dict = {}
    for p in core.BUGS.iterdir():
        if p.is_file():
            groups.setdefault(p.stem, []).append(p)
    gone = 0
    for stem, files in groups.items():
        try:
            born = None
            j = core.BUGS / f"{stem}.json"
            if j.exists():
                try:
                    born = float(json.loads(j.read_text(encoding="utf-8"))["ts"])
                except Exception:
                    born = None
            if born is None:
                born = min(f.stat().st_mtime for f in files)
            if born < cut:
                for f in files:
                    f.unlink(missing_ok=True)
                gone += 1
        except Exception:
            continue
    return gone


def _safe(bid: str) -> str:
    """Filenames are built from caller input, so allow nothing that can traverse."""
    return "".join(ch for ch in str(bid) if ch.isalnum() or ch == "_")[:40]
