#!/usr/bin/env python3
"""Ask the routing engine what it actually returns, rather than trusting docs.

    python3 tools/nav_probe.py                     # status + a route + a limit
    python3 tools/nav_probe.py 42.72 -83.78        # a speed limit at a point

Run ON THE BOX (the engine is loopback only). It prints the raw shape of a
/locate response so the speed-limit parser in nav.py can be written against
what the engine says rather than against a remembered key name - the same rule
that settled the OCR question on 2026-09-23. A parser built on a docstring
returns None for ever and looks like "OpenStreetMap has no data here".
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import nav  # noqa: E402

# Somewhere with a posted limit that OSM is likely to know: an interstate.
DEFAULT = (42.7325, -83.7850)          # US-23 near Fenton, Michigan


def main() -> int:
    print("engine up:", nav.available())
    lat, lon = (float(sys.argv[1]), float(sys.argv[2])) if len(sys.argv) > 2 else DEFAULT
    print(f"\n--- /locate at {lat},{lon} (raw) ---")
    try:
        raw = nav._post("/locate", {"locations": [{"lat": lat, "lon": lon}],
                                    "costing": "auto", "verbose": True})
        txt = json.dumps(raw, indent=1)
        print(txt[:2600] + ("\n... truncated" if len(txt) > 2600 else ""))
        keys = set()

        def walk(n):
            if isinstance(n, dict):
                for k, v in n.items():
                    if "speed" in k.lower():
                        keys.add(f"{k} = {v!r}")
                    walk(v)
            elif isinstance(n, list):
                for v in n:
                    walk(v)
        walk(raw)
        print("\nEVERY speed-ish key in that response:")
        for k in sorted(keys):
            print("   ", k)
    except Exception as exc:
        print("locate failed:", exc.__class__.__name__, exc)

    print("\n--- nav.speed_limit() says ---")
    print(nav.speed_limit(lat, lon))

    print("\n--- a short route (Fenton -> Linden) ---")
    try:
        out = nav.route((42.7975, -83.7049), (42.8189, -83.7827))
        leg = out["trip"]["legs"][0]
        s = leg["summary"]
        print(f"{s['length']:.1f} mi, {s['time'] / 60:.0f} min, "
              f"{len(leg['maneuvers'])} maneuvers, shape {len(leg['shape'])} chars")
        for m in leg["maneuvers"][:4]:
            print("   ", m.get("instruction"))
    except Exception as exc:
        print("route failed:", exc.__class__.__name__, exc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
