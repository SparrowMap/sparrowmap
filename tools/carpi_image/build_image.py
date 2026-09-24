"""Build the SparrowMap car-kit SD card image: stock Raspberry Pi OS + boot/.

    python tools/carpi_image/build_image.py --out D:/LLM/carpi_image_build
    (needs: pip install pyfatfs "setuptools<81")

The kit is ONLY files on the FAT boot partition (boot/ here): a cloud-init
user-data that runs sparrowmap/install.sh on first boot, and sparrowmap.txt
for the owner to fill in. The Linux partition is not touched, so the image is
the official one byte for byte apart from those files, and it builds on any
OS - no chroot, no loop mount, no emulator.

🚨 WHY NOT PRE-INSTALL THE PACKAGES INTO THE IMAGE. That needs an arm64
chroot, and it freezes today's opencv/onnxruntime into a file people keep for
a year. First boot installs current wheels instead (~100 MB over the hotspot).

The base image is pinned by URL AND SHA-256 (from raspberrypi.com's download
page). A newer release is a deliberate edit here, not something that changes
under a rebuild.
"""
from __future__ import annotations

import argparse
import hashlib
import lzma
import shutil
import struct
import sys
import urllib.request
from datetime import date
from pathlib import Path

BASE_URL = ("https://downloads.raspberrypi.com/raspios_lite_arm64/images/"
            "raspios_lite_arm64-2026-09-15/2026-09-15-raspios-trixie-arm64-lite.img.xz")
BASE_SHA256 = "cdf4f3bfac35ae947b46e4e767f935453810549779ac3290e05a6754aee627e5"

KIT = Path(__file__).resolve().parent / "boot"
# Opened in Notepad by the owner; everything else is read by Linux only.
CRLF = {"sparrowmap.txt"}


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def fetch_base(work: Path) -> Path:
    xz = work / BASE_URL.rsplit("/", 1)[1]
    if not xz.exists() or sha256(xz) != BASE_SHA256:
        print(f"downloading {BASE_URL}")
        urllib.request.urlretrieve(BASE_URL, xz)
    got = sha256(xz)
    if got != BASE_SHA256:
        sys.exit(f"base image checksum mismatch: {got}")
    return xz


def boot_offset(img: Path) -> int:
    mbr = open(img, "rb").read(512)
    if mbr[510:512] != b"\x55\xaa":
        sys.exit("no MBR in the base image")
    ptype = mbr[446 + 4]
    lba = struct.unpack("<I", mbr[446 + 8:446 + 12])[0]
    if ptype not in (0x0B, 0x0C):
        sys.exit(f"partition 1 is type {ptype:#x}, expected FAT32")
    return lba * 512


def kit_files() -> list[tuple[str, bytes]]:
    out = []
    for p in sorted(KIT.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(KIT).as_posix()
        data = p.read_bytes().replace(b"\r\n", b"\n")
        if p.name in CRLF:
            data = data.replace(b"\n", b"\r\n")
        out.append((rel, data))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--no-xz", action="store_true", help="leave the .img uncompressed")
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)

    from pyfatfs.PyFatFS import PyFatFS

    xz = fetch_base(a.out)
    name = f"sparrowmap-dashcam-{date.today():%Y-%m-%d}"
    img = a.out / f"{name}.img"
    print(f"decompressing to {img}")
    with lzma.open(xz) as src, open(img, "wb") as dst:
        shutil.copyfileobj(src, dst, 1 << 22)

    fs = PyFatFS(str(img), offset=boot_offset(img))
    try:
        for rel, data in kit_files():
            parent = "/" + rel.rsplit("/", 1)[0] if "/" in rel else "/"
            if parent != "/" and not fs.exists(parent):
                fs.makedirs(parent)
            # ⚠️ pyfatfs cannot overwrite in place (the stock user-data): it
            # dies on "FREE_CLUSTER mark found in FAT cluster chain". Delete,
            # then create.
            if fs.exists("/" + rel):
                fs.remove("/" + rel)
            with fs.openbin("/" + rel, "w") as f:
                f.write(data)
            print(f"  + {rel} ({len(data)} B)")
    finally:
        fs.close()

    # Read it all back through a fresh mount: the proof is what is ON the
    # image, not what the writer believed it wrote.
    fs = PyFatFS(str(img), offset=boot_offset(img), read_only=True)
    try:
        for rel, data in kit_files():
            if fs.readbytes("/" + rel) != data:
                sys.exit(f"read-back mismatch: {rel}")
        assert fs.exists("/kernel8.img") and fs.exists("/meta-data")
    finally:
        fs.close()
    print("  read-back OK")

    final = img
    if not a.no_xz:
        final = img.with_suffix(".img.xz")
        print(f"compressing to {final}")
        with open(img, "rb") as src, lzma.open(final, "wb", preset=6) as dst:
            shutil.copyfileobj(src, dst, 1 << 22)
        img.unlink()
    digest = sha256(final)
    (final.parent / (final.name + ".sha256")).write_text(f"{digest}  {final.name}\n")
    print(f"{final}\n  {final.stat().st_size / 1e6:.0f} MB  sha256 {digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
