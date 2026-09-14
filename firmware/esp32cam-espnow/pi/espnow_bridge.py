#!/usr/bin/env python3
"""Rebuild JPEGs arriving from the ESP-NOW receiver over USB serial.

Original: Lucian H (lucianbuilds), "serial image reconstruction system for
esp-now image bridge", September 2026. Reviewed into the repo with:

  - a newer frame ABANDONS an unfinished one. The original waited for the
    missing packets of a frame the camera had already given up on, and since
    the serial line never went quiet it waited forever.
  - the length and CRC32 in the header are now real (the receiver forwards
    them from the frame-start packet), so a corrupt image is rejected rather
    than saved with a comment saying it was checked.
  - the history folder is capped, so a Pi's SD card does not fill.
  - port, baud and output directory are arguments.

Usage on the Pi:

    python3 espnow_bridge.py --port /dev/ttyUSB0 --out camera

Then point the SparrowMap node at the file it keeps fresh:

    python3 detect/run_live.py --source camera/latest.jpg ...

Find the port: `ls /dev/ttyUSB* /dev/ttyACM*`, unplug the receiver, run it
again - the one that disappeared is the receiver.
"""
from __future__ import annotations

import argparse
import os
import struct
import sys
import time
import zlib

try:
    import serial   # pyserial
except ImportError:
    sys.exit("pip install pyserial")

HEADER = struct.Struct("<4sIHHHII")   # magic, frame_id, seq, total, payload_len, image_len, image_crc
MAGIC = b"SPKT"
MAX_PAYLOAD = 220


def read_exact(ser, count: int) -> bytes:
    data = bytearray()
    while len(data) < count:
        chunk = ser.read(count - len(data))
        if not chunk:
            raise TimeoutError("serial timeout")
        data.extend(chunk)
    return bytes(data)


def find_magic(ser) -> None:
    buf = bytearray()
    while True:
        b = ser.read(1)
        if not b:
            raise TimeoutError("waiting for SPKT")
        buf += b
        if len(buf) > len(MAGIC):
            del buf[0]
        if bytes(buf) == MAGIC:
            return


def read_packet(ser):
    """One serial packet, or None if its header did not make sense."""
    find_magic(ser)
    header = MAGIC + read_exact(ser, HEADER.size - 4)
    _, frame_id, seq, total, payload_len, image_len, image_crc = HEADER.unpack(header)
    if total == 0 or seq >= total or payload_len > MAX_PAYLOAD:
        return None
    payload = read_exact(ser, payload_len)
    return {"frame_id": frame_id, "seq": seq, "total": total, "payload": payload,
            "image_len": image_len, "image_crc": image_crc}


class Frame:
    def __init__(self, first: dict):
        self.id = first["frame_id"]
        self.total = first["total"]
        self.image_len = first["image_len"]
        self.image_crc = first["image_crc"]
        self.packets: dict[int, bytes] = {}
        self.started = time.time()
        self.add(first)

    def add(self, p: dict) -> None:
        self.packets[p["seq"]] = p["payload"]
        # A start packet may have been missed for the first chunk and seen
        # later; take the claim from whichever chunk carries one.
        if p["image_len"] and not self.image_len:
            self.image_len, self.image_crc = p["image_len"], p["image_crc"]

    @property
    def complete(self) -> bool:
        return len(self.packets) >= self.total

    def assemble(self):
        """The JPEG bytes, or a reason string if it fails a check."""
        for seq in range(self.total):
            if seq not in self.packets:
                return f"missing packet {seq}"
        image = b"".join(self.packets[s] for s in range(self.total))
        if self.image_len and len(image) != self.image_len:
            return f"length {len(image)} != declared {self.image_len}"
        if self.image_crc:
            crc = zlib.crc32(image) & 0xFFFFFFFF
            if crc != self.image_crc:
                return f"crc {crc:08X} != declared {self.image_crc:08X}"
        if len(image) < 4 or image[:2] != b"\xFF\xD8" or image[-2:] != b"\xFF\xD9":
            return "not a JPEG (markers)"
        return image


def save(image: bytes, frame_id: int, out: str, history: int) -> None:
    latest = os.path.join(out, "latest.jpg")
    tmp = latest + ".tmp"
    with open(tmp, "wb") as f:
        f.write(image)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, latest)      # atomic: a reader never sees a half file
    if history > 0:
        hdir = os.path.join(out, "history")
        os.makedirs(hdir, exist_ok=True)
        with open(os.path.join(hdir, f"frame_{frame_id:08d}.jpg"), "wb") as f:
            f.write(image)
        names = sorted(n for n in os.listdir(hdir) if n.endswith(".jpg"))
        for old in names[:-history]:
            try:
                os.remove(os.path.join(hdir, old))
            except OSError:
                pass


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--port", default="/dev/ttyUSB0")
    ap.add_argument("--baud", type=int, default=921600, help="must match receiver config.h")
    ap.add_argument("--out", default="camera", help="directory for latest.jpg")
    ap.add_argument("--history", type=int, default=200,
                    help="keep this many past frames in out/history (0 = none)")
    a = ap.parse_args()

    os.makedirs(a.out, exist_ok=True)
    print(f"opening {a.port} at {a.baud} baud, writing {os.path.join(a.out, 'latest.jpg')}")
    ser = serial.Serial(a.port, a.baud, timeout=2)
    time.sleep(2)   # the ESP32 resets when the port opens
    print("waiting for images...")

    frame: Frame | None = None
    saved = rejected = abandoned = 0
    while True:
        try:
            p = read_packet(ser)
            if p is None:
                continue
            if frame is None:
                frame = Frame(p)
            elif p["frame_id"] != frame.id:
                # The camera moved on; whatever we were collecting is dead.
                # Waiting for it is the bug the original had.
                abandoned += 1
                print(f"\nabandoned frame {frame.id} ({len(frame.packets)}/{frame.total}); "
                      f"now frame {p['frame_id']}")
                frame = Frame(p)
            else:
                frame.add(p)
            print(f"\rframe {frame.id}: {len(frame.packets)}/{frame.total}", end="", flush=True)
            if frame.complete:
                result = frame.assemble()
                print()
                if isinstance(result, bytes):
                    save(result, frame.id, a.out, a.history)
                    saved += 1
                    print(f"saved frame {frame.id} ({len(result)} bytes) "
                          f"[saved {saved} rejected {rejected} abandoned {abandoned}]")
                else:
                    rejected += 1
                    print(f"rejected frame {frame.id}: {result}")
                frame = None
        except TimeoutError:
            continue
        except KeyboardInterrupt:
            print("\nstopping")
            break
        except Exception as e:      # noqa: BLE001 - keep the bridge alive
            print(f"\nerror: {e}")
            time.sleep(1)
    ser.close()


if __name__ == "__main__":
    main()
