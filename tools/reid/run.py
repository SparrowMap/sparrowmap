"""The whole re-id pipeline, one command, safe to run on a timer.

    python tools/reid/run.py            # export -> ocr -> embed -> link -> apply
    python tools/reid/run.py --dry-run  # everything but the box write

Needs SPARROW_BOX and SPARROW_KEY (the same two deploy.py uses). Runs on the
DESKTOP: the OCR and the embedding are the expensive part and the box has two
cores and no GPU. Every stage is resumable, so a scheduled run after the first
costs about as long as the new crops take to read (~1.3 s each on CPU).

Stages, and which interpreter each wants:
  export   on the box, its venv          tools/reid/export.py
  ocr      D:\\LLM\\reid_venv (paddle)      tools/reid/ocr.py --mobile
  analyze  D:\\LLM\\.venv (torch/open_clip) tools/reid/analyze.py --no-ocr --cpu
  link     either                          tools/reid/link.py
  apply    on the box, its venv          tools/reid/apply.py
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

REPO = Path(__file__).resolve().parent.parent.parent
DATA = REPO / "data" / "reid"
BOX = os.environ.get("SPARROW_BOX", "")
KEY = os.environ.get("SPARROW_KEY", "")
REMOTE = "/opt/sparrowmap"
# The box-side scratch dir: owned by the sparrow user, so a root-owned leftover
# in /tmp can never block the export.
ROUT = f"{REMOTE}/data/reid_out"
OCR_PY = Path(os.environ.get("REID_OCR_PY", r"D:\LLM\reid_venv\Scripts\python.exe"))
MAIN_PY = Path(os.environ.get("REID_MAIN_PY", r"D:\LLM\.venv\Scripts\python.exe"))


def sh(cmd: list[str], **kw) -> None:
    print("+", " ".join(str(c) for c in cmd), flush=True)
    subprocess.run(cmd, check=True, **kw)


def ssh(cmd: str) -> None:
    sh(["ssh", "-i", KEY, "-o", "BatchMode=yes", BOX, cmd], stdin=subprocess.DEVNULL)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if not BOX or not KEY:
        sys.exit("set SPARROW_BOX and SPARROW_KEY")
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / "snaps").mkdir(exist_ok=True)
    t0 = time.time()

    # 1. export - only rows newer than what we already hold, then merge.
    have = []
    if (DATA / "reid_rows.json").exists():
        have = json.load(open(DATA / "reid_rows.json"))
    since = max((r["ts"] for r in have), default=0.0)
    ssh(f"cd {REMOTE} && sudo -u sparrow mkdir -p {ROUT} && sudo -u sparrow .venv/bin/python3 tools/reid/export.py --since {since} --out {ROUT}")
    sh(["scp", "-q", "-i", KEY, f"{BOX}:{ROUT}/reid_rows.json", str(DATA / "reid_rows.new.json")])
    sh(["scp", "-q", "-i", KEY, f"{BOX}:{ROUT}/reid_snaps.tar", str(DATA / "reid_snaps.tar")])
    new = json.load(open(DATA / "reid_rows.new.json"))
    seen = {r["id"] for r in have}
    merged = have + [r for r in new if r["id"] not in seen]
    json.dump(merged, open(DATA / "reid_rows.json", "w"))
    with tarfile.open(DATA / "reid_snaps.tar") as t:
        t.extractall(DATA / "snaps")
    print(f"export: {len(new)} new rows, {len(merged)} total", flush=True)

    # 2. read the text, 3. embed, 4. decide.
    sh([str(OCR_PY), str(REPO / "tools/reid/ocr.py"), "--mobile"])
    sh([str(MAIN_PY), str(REPO / "tools/reid/analyze.py"), "--no-ocr", "--cpu"])
    sh([str(MAIN_PY), str(REPO / "tools/reid/link.py"), "--show", "5"])

    # 5. write, through the gated writers on the box.
    if args.dry_run:
        print("dry run: tags.json not applied")
    else:
        sh(["scp", "-q", "-i", KEY, str(DATA / "tags.json"), f"{BOX}:{ROUT}/tags.json"])
        ssh(f"cd {REMOTE} && sudo -u sparrow .venv/bin/python3 tools/reid/apply.py {ROUT}/tags.json")
    print(f"done in {time.time() - t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
