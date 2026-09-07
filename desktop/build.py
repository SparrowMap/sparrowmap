"""Build RavenMap4Biz.exe - the light desktop node for businesses.

    python desktop\\build.py

🚨 THE MODEL IS NOT BUNDLED, AND THAT IS DELIBERATE. yolo11s.onnx is 38 MB and
would nearly double the download; the app fetches it once, from the hub, on
first run, and verifies its SHA-256 before using it (detect/relay.model_path).
So the exe stays small enough to send somebody in a message, and a model swap
does not mean re-releasing the app.

⚠️ AND TORCH IS EXCLUDED BY NAME. This is the LIGHT node: it finds vehicles and
posts a crop, and the classifier that decides "patrol car" runs at home. torch
is installed in this development environment, and PyInstaller is enthusiastic -
without excluding it, a hook could pull in ~2 GB and quietly turn a 60 MB
download into something nobody will install. The exclusion is the feature.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "desktop" / "sparrowmap_app.py"
DIST = ROOT / "desktop" / "dist"

# Everything the light node must never drag in. Each of these is either huge, or
# a development-only dependency that has no business in a shop's download.
EXCLUDE = ["torch", "torchvision", "ultralytics", "transformers", "open_clip",
           "open_clip_torch", "scipy", "pandas", "matplotlib", "PIL.ImageQt",
           "PyQt5", "PySide2", "IPython", "notebook", "sklearn", "sympy"]


def main() -> int:
    if not APP.exists():
        print(f"missing {APP}")
        return 1
    cmd = [sys.executable, "-m", "PyInstaller",
           "--noconfirm", "--clean",
           "--name", "RavenMap4Biz",
           "--onefile",
           # No console window: this is a desktop app, and a black terminal
           # behind it is what makes something feel like a script rather than a
           # program. Errors go to the window's own log instead.
           "--windowed",
           "--distpath", str(DIST),
           "--workpath", str(ROOT / "desktop" / "build"),
           "--specpath", str(ROOT / "desktop"),
           # The relay is imported, not shelled out to, so it has to come along.
           "--hidden-import", "detect.relay",
           "--paths", str(ROOT),
           ]
    for e in EXCLUDE:
        cmd += ["--exclude-module", e]
    ico = ROOT / "public" / "favicon.ico"
    if ico.exists():
        cmd += ["--icon", str(ico)]
    cmd.append(str(APP))

    print("building…")
    r = subprocess.run(cmd, cwd=str(ROOT))
    if r.returncode != 0:
        return r.returncode
    exe = DIST / ("RavenMap4Biz.exe" if sys.platform == "win32" else "RavenMap4Biz")
    if exe.exists():
        mb = exe.stat().st_size / 1024 / 1024
        print(f"\n  {exe}  ({mb:.0f} MB)")
        print("  The detector is downloaded on first run and checksum-verified.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
