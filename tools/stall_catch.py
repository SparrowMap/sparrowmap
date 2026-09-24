#!/usr/bin/env python3
"""Catch the hub mid-stall and photograph every thread. Run ON THE BOX.

    .venv/bin/python tools/stall_catch.py                 # watch until a stall
    .venv/bin/python tools/stall_catch.py --trip 3 --max 5 # tune / stop after 5

🚨 WHY THIS EXISTS. Every diagnosis on this box that went wrong went wrong by
looking AFTER the fact: /api/health's `slowest_ever` says a route once held a
permit for 30 seconds, and says nothing whatever about what the process was
doing at the time. Two root causes on 2026-09-24 were misidentified from
after-the-fact numbers, and the one that was right was found by dumping threads
WHILE it was happening.

So this probes a cheap endpoint continuously and, the moment the answer takes
longer than it possibly should, runs py-spy against the hub before the stall
has cleared. A dump taken 5 seconds late shows an idle server and proves
nothing.

The probe is deliberately /api/stats: it touches the database, it is tiny, and
it is not in the single-flight cache, so it cannot be answered from memory
while the rest of the process is wedged - which is exactly the false negative a
cached probe would give.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HUB = "http://127.0.0.1:8150"
OUT = Path("/tmp/stalls")


def hub_pid() -> str:
    return subprocess.run(["systemctl", "show", "-p", "MainPID", "--value", "sparrowmap"],
                          capture_output=True, text=True).stdout.strip()


def probe(path: str, timeout: float) -> tuple[float, str]:
    t = time.time()
    try:
        with urllib.request.urlopen(HUB + path, timeout=timeout) as r:
            r.read()
        return time.time() - t, "200"
    except Exception as e:
        return time.time() - t, e.__class__.__name__


def health() -> dict:
    try:
        with urllib.request.urlopen(HUB + "/api/health", timeout=10) as r:
            return json.loads(r.read())
    except Exception:
        return {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trip", type=float, default=3.0,
                    help="seconds on the probe that counts as a stall")
    ap.add_argument("--max", type=int, default=3, help="stop after this many catches")
    ap.add_argument("--minutes", type=float, default=60.0)
    args = ap.parse_args()
    OUT.mkdir(exist_ok=True)
    pid = hub_pid()
    print(f"watching hub pid {pid}; trip at {args.trip}s; writing to {OUT}", flush=True)

    caught, end = 0, time.time() + args.minutes * 60
    while time.time() < end and caught < args.max:
        dt, code = probe("/api/stats", timeout=60)
        if dt < args.trip and code == "200":
            time.sleep(1)
            continue
        # STALLING RIGHT NOW. Dump first, ask questions later - every second
        # spent being tidy here is a second the evidence is disappearing.
        stamp = time.strftime("%H%M%S")
        f = OUT / f"stall_{stamp}.txt"
        t0 = time.time()
        dump = subprocess.run([sys.executable.replace("python", "py-spy"), "dump",
                               "--pid", pid, "--locals"],
                              capture_output=True, text=True).stdout
        h = health()
        f.write_text(
            f"probe /api/stats took {dt:.2f}s ({code})\n"
            f"dump taken {time.time() - t0:.1f}s after detection\n"
            f"heavy_free={h.get('heavy_free')} inflight={h.get('inflight')} "
            f"wal_mb={h.get('wal_mb')} threads={h.get('threads')}\n"
            f"slowest_ever={json.dumps(h.get('slowest_ever'), indent=1)}\n"
            f"{'=' * 70}\n{dump}")
        caught += 1
        print(f"CAUGHT #{caught}: probe {dt:.2f}s -> {f}", flush=True)
        time.sleep(5)
    print(f"done: {caught} stall(s) caught", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
