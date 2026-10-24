"""Read the markings off a sighting the minute it is confirmed, not the next night.

    D:\\LLM\\reid_venv\\Scripts\\python.exe tools/reid/fast.py --singleton   # the loop
    ... --once                                                          # one pass

🚨 WHY THIS EXISTS. run.py (OCR + CLIP + linking) only runs 00:00-06:00, his
call on 2026-09-27 after it lagged the game. A sighting confirmed at 09:00 was
not searchable by its markings until the next night: measured 2026-10-24
14:55, 144 published police rows confirmed that day carried no markings at
all. His ask the same day: "a sighting is searchable as soon as its put on the
map".

WHY AT CONFIRMATION AND NOT AT PEN ENTRY. Measured the same day: the pen took
3,268 cards from box_puller in ~22 h and confirmations run ~150-500 a day, so
reading every pen card is ~10-20x the OCR for the same result. Pen cards
parked at ingest on the box (mirror.review_write) never pass the desktop at
all. And db.set_markings refuses any row that is not public police/gov - the
text on an unconfirmed (maybe private) vehicle is never kept - so a pen-time
read would have had to wait somewhere for the confirmation anyway.

WHAT IT DOES, every --interval seconds:
  export rows that became eligible since the cursor (the same export.py and
  the same max(ts, reviewed_at) cursor run.py uses: 0.1 s on the box), read
  the new crops with a PaddleOCR that stays loaded, mark them with
  link.marking_of (the one rule both lanes share), apply through apply.py.
  An empty pass is one ssh call.

WHAT IT LEAVES TO THE NIGHT RUN: trails (vehicle_tag). Linking needs the CLIP
embeddings of every row; run.py picks these rows up from reid_rows.json and
ocr.json without re-reading them.

⚠️ NOT WHILE HE PLAYS. Standing rule 2026-10-06: game running = no background
jobs at all. While BeamNG is up a pass is skipped; rows wait (and the night
run reads them if the game is still up then).

⚠️ ONE WRITER. run.py and this both write reid_rows.json and ocr.json, so both
take data/reid/reid.lock: a pass is skipped while run.py holds it.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tarfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import link        # noqa: E402
import ocr as ocrmod   # noqa: E402

REPO = Path(__file__).resolve().parent.parent.parent
DATA = REPO / "data" / "reid"
BOX = os.environ.get("SPARROW_BOX", "")
KEY = os.environ.get("SPARROW_KEY", "")
REMOTE = "/opt/sparrowmap"
ROUT = f"{REMOTE}/data/reid_fast"          # its own scratch: never run.py's files
GAME = "BeamNG.drive.x64.exe"


def log(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


def ssh(cmd: str) -> str:
    return subprocess.run(["ssh", "-i", KEY, "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
                           BOX, cmd], check=True, capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, timeout=120).stdout


def scp(src: str, dst: str) -> None:
    subprocess.run(["scp", "-q", "-i", KEY, src, dst], check=True,
                   stdin=subprocess.DEVNULL, timeout=300)


def game_running() -> bool:
    out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {GAME}", "/NH"],
                         capture_output=True, text=True).stdout
    return GAME.lower() in out.lower()


def lock(path: Path, wait: bool = False):
    """An exclusive OS lock on byte 0 of `path`, or None if somebody holds it.

    Byte 0 after a seek, for the reason box_puller's singleton gives: in append
    mode the position is the end of the file, and two copies that disagree about
    where the end is lock different bytes and both believe they are alone.
    """
    fh = open(path, "a+b")
    fh.seek(0)
    while True:
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fh
        except OSError:
            if not wait:
                fh.close()
                return None
            time.sleep(2)


def one_pass(reader) -> int:
    """Mark every row that became public since the cursor. Returns rows marked."""
    rows_p = DATA / "reid_rows.json"
    have = json.load(open(rows_p)) if rows_p.exists() else []
    since = max((max(r["ts"], r.get("reviewed_at") or 0) for r in have), default=0.0)
    out = ssh(f"cd {REMOTE} && sudo -u sparrow mkdir -p {ROUT} && sudo -u sparrow "
              f".venv/bin/python3 tools/reid/export.py --since {since} --out {ROUT}")
    if out.startswith("0 rows"):
        return 0
    scp(f"{BOX}:{ROUT}/reid_rows.json", str(DATA / "reid_rows.fast.json"))
    scp(f"{BOX}:{ROUT}/reid_snaps.tar", str(DATA / "reid_snaps.fast.tar"))
    new = json.load(open(DATA / "reid_rows.fast.json"))
    seen = {r["id"] for r in have}
    new = [r for r in new if r["id"] not in seen]
    if not new:
        return 0
    with tarfile.open(DATA / "reid_snaps.fast.tar") as t:
        t.extractall(DATA / "snaps")

    # Reads go into the same ocr.json run.py's ocr.py keeps, under the same
    # model marker, so the night run treats them as done. A file written by a
    # different model is left alone: ocr.py will re-read all of it anyway.
    ocr_p = DATA / "ocr.json"
    done = json.load(open(ocr_p)) if ocr_p.exists() else {"_model": "server"}
    model_ok = done.get("_model") == "server"
    marks, t0 = [], time.time()
    for r in new:
        p = ocrmod.crop_path(r)
        items = ocrmod.read_crop(reader, p, ocrmod.SCALE, r["id"]) if p else []
        if model_ok:
            done[str(r["id"])] = items
        size = None
        if p:
            from PIL import Image
            with Image.open(p) as im:
                size = im.size
        f = {"id": r["id"], "place": r.get("place"), "lat": r["lat"], "lon": r["lon"],
             "size": size}
        m = link.marking_of(f, items)
        if m["text"]:
            marks.append({"id": r["id"], "text": m["text"]})

    # Mark first, then advance the cursor: a pass that dies before apply is
    # simply repeated, never skipped.
    tags = DATA / "tags.fast.json"
    json.dump({"rev": link.REV, "markings": marks, "tags": [],
               "considered": [r["id"] for r in new]}, open(tags, "w"))
    scp(str(tags), f"{BOX}:{ROUT}/tags.json")
    res = ssh(f"cd {REMOTE} && sudo -u sparrow .venv/bin/python3 tools/reid/apply.py {ROUT}/tags.json")
    if model_ok:
        json.dump(done, open(ocr_p, "w"))
    json.dump(have + new, open(rows_p, "w"))
    log(f"{len(new)} new, {len(marks)} marked in {time.time() - t0:.0f}s: "
        f"{res.strip().splitlines()[-1] if res.strip() else ''}")
    for m in marks[:5]:
        log(f"  #{m['id']}: {m['text']}")
    return len(marks)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=float, default=60)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--singleton", action="store_true",
                    help="exit quietly if a copy is already running (the 15-min task is the watchdog)")
    args = ap.parse_args()
    if not BOX or not KEY:
        sys.exit("set SPARROW_BOX and SPARROW_KEY")
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / "snaps").mkdir(exist_ok=True)
    if os.name == "nt":                  # below normal: the detector and the desktop come first
        import ctypes
        k32 = ctypes.windll.kernel32
        k32.SetPriorityClass(k32.GetCurrentProcess(), 0x4000)
    if args.singleton:
        _single = lock(DATA / "fast.lock")
        if _single is None:
            return
    reader = None
    paused = False
    log(f"fast lane up, every {args.interval:.0f}s")
    while True:
        try:
            if game_running():
                if not paused:
                    log("BeamNG is running: paused")
                paused = True
            else:
                if paused:
                    log("BeamNG closed: resuming")
                paused = False
                held = lock(DATA / "reid.lock")
                if held is not None:
                    try:
                        if reader is None:
                            reader = ocrmod.make_reader()
                        one_pass(reader)
                    finally:
                        held.close()
        except Exception as exc:      # the box or the network blinked: try again next pass
            log(f"pass failed: {exc.__class__.__name__}: {exc}")
        if args.once:
            return
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
