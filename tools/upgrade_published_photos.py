"""Replace the 200px photograph on a published sighting with the real one.

    python tools\\upgrade_published_photos.py --dry-run
    python tools\\upgrade_published_photos.py --limit 20

🚨 WHY PUBLISHED PHOTOS ARE 200px EVEN AFTER THE FULL-RES FIX.
Three things can publish a sighting and only two of them were fixed. A drive
phone and a camera operator now send the good crop WITH their verdict. The
third - a reviewer pressing confirm on /rv - has no client holding a picture:
review_api._publish reads crop_bytes(sid), which looks in core.EVIDENCE and then
falls back to the review pen.

On the BOX, core.EVIDENCE does not exist. mirror.evidence_write refuses on a
mirror, on purpose: a public server must not hold un-degraded pictures of
traffic nobody has looked at yet. So the fallback is the pen copy, and the pen
copy is 200px by contract - the size that destroys a plate.

The good pixels are not lost. They are HERE, in the local training bank, where
run_live put them. This walks the bank, finds the passes that are now published,
and hands each one back to the box.

⚠️ IT RUNS AT HOME, AND THAT IS THE POINT. Nothing about this weakens the rule
that the box keeps no un-degraded imagery of unreviewed traffic: a photograph
only travels once a human has already confirmed the vehicle, at which point it
is a public-tier picture of a government vehicle. Same argument as the drive
phone holding its own crop until somebody says yes.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

BANK = ROOT / "data" / "training"
PLACEMENT = ROOT / "camctl" / "placement.json"
UA = "SparrowMap-node/1.0"
SUBRES = 200          # snapshot.SUBRES_MAX_EDGE - the plate-destroying cap


def creds(hub_override: str = ""):
    p = json.loads(PLACEMENT.read_text(encoding="utf-8"))
    hub = (hub_override or os.environ.get("RAVEN_HUB") or
           os.environ.get("SPARROW_HUB") or "")
    if not hub:
        raise RuntimeError("No RavenMap hub configured. Set RAVEN_HUB; "
                           "legacy SPARROW_HUB is also accepted.")
    return hub.rstrip("/"), p["node_id"], p["token"]


def get(url: str, timeout: int = 20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def post(hub: str, path: str, body: dict, token: str):
    req = urllib.request.Request(
        hub + path, method="POST", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "User-Agent": UA,
                 "Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw or b"{}")
        except Exception:
            return e.code, {}


def banked() -> dict:
    """{sighting_id: jpg path} for every banked crop that reached the map."""
    out = {}
    if not BANK.exists():
        return out
    for j in BANK.rglob("*.json"):
        try:
            d = json.loads(j.read_text(encoding="utf-8"))
        except Exception:
            continue
        sid = d.get("sighting_id")
        if not sid:
            continue
        img = j.with_suffix(".jpg")
        if img.exists():
            out[int(sid)] = img
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hub", default="")
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    hub, node, token = creds(args.hub)
    bank = banked()
    print(f"hub {hub}\nnode {node}\nbanked crops with a sighting id: {len(bank)}\n")
    if not bank:
        print("nothing banked locally - is this the machine the camera runs on?")
        return 0

    pub = json.loads(get(f"{hub}/api/sightings?vclass=public&limit=2000"))
    if isinstance(pub, dict):
        pub = pub.get("sightings", [])
    print(f"published sightings on the map: {len(pub)}")

    todo = []
    for s in pub:
        sid = int(s.get("id", 0))
        if sid not in bank or not s.get("snap"):
            continue
        # Measure what the map is ACTUALLY serving rather than trusting a field.
        try:
            cur = Image.open(io.BytesIO(get(f"{hub}/snap/{s['snap']}")))
        except Exception:
            continue
        if max(cur.size) > SUBRES:
            continue                       # already a real photograph
        try:
            better = Image.open(bank[sid])
        except Exception:
            continue
        if max(better.size) <= max(cur.size):
            continue                       # the bank is no better; leave it
        todo.append((sid, cur.size, better.size, bank[sid]))

    print(f"published at pen resolution, with a better copy here: {len(todo)}\n")
    if not todo:
        print("nothing to upgrade")
        return 0

    done = 0
    for sid, was, now_size, path in todo[:args.limit]:
        print(f"  {sid:>6}  {was[0]}x{was[1]}  ->  {now_size[0]}x{now_size[1]}", end="")
        if args.dry_run:
            print("   (dry run)")
            continue
        data = ("data:image/jpeg;base64,"
                + base64.b64encode(path.read_bytes()).decode())
        # 🚨 REUSES THE VERDICT ROUTE ON PURPOSE. /api/node/label already
        # attaches a photograph when the verdict publishes, already proves the
        # sighting belongs to this camera, and already refuses to attach one to
        # a row that stays private. A second route doing the same job is a
        # second place for the rule to be forgotten.
        st, out = post(hub, "/api/node/label",
                       {"node_id": node, "sighting_id": sid,
                        "label": "police", "snap_b64": data}, token)
        if st == 200 and not out.get("error"):
            done += 1
            print("   ok")
        else:
            print(f"   FAILED {st} {out.get('error', '')}")
        time.sleep(0.3)          # be kind to a 2-vCPU box

    print(f"\nupgraded {done} of {len(todo[:args.limit])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
