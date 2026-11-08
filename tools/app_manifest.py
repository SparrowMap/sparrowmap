"""Write desktop/release/version.json for a desktop build.

The in-app updater (desktop/sparrowmap_app.py) reads this file from
https://map.sparrowmap.com/app/version.json and refuses any download whose
sha256 does not match it. It is generated from the real exe here, never typed
by hand: a wrong checksum is worse than none.

    python tools/app_manifest.py desktop/dist/SparrowMap.exe --notes "what changed"
    # commit + push the manifest, then:
    python tools/deploy.py --release-app desktop/dist/SparrowMap.exe --restart-camera

The version comes from APP_VERSION in sparrowmap_app.py, so the manifest can
not claim a version the build does not carry. Bump APP_VERSION, rebuild, run
this. The exe itself never goes in git.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_SRC = ROOT / "desktop" / "sparrowmap_app.py"
OUT = ROOT / "desktop" / "release" / "version.json"
BASE = "https://map.sparrowmap.com/app/SparrowMap.exe"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("exe", type=Path)
    ap.add_argument("--notes", default="")
    a = ap.parse_args()
    m = re.search(r'^APP_VERSION\s*=\s*"([^"]+)"', APP_SRC.read_text(encoding="utf-8"), re.M)
    if not m:
        sys.exit("APP_VERSION not found in sparrowmap_app.py")
    ver = m.group(1)
    data = {"version": ver, "notes": a.notes,
            # ?v= makes every release a new cache key at Cloudflare's edge.
            "url": f"{BASE}?v={ver}",
            "sha256": sha256(a.exe), "size": a.exe.stat().st_size}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(data, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
