#!/usr/bin/env python3
"""Turn the routing engine's own request logging OFF, in its config, every start.

    python3 deploy/valhalla-quiet.py /opt/valhalla/custom_files/valhalla.json

🚨 WHY THIS IS A FILE AND NOT A ONE-OFF EDIT.

Valhalla ships with `"logging": {"type": "std_out"}`, which prints a line per
request. Through docker that lands in the daemon's json log on disk, so the
engine would be keeping a record of every route asked for - origin, destination
and time - in a file nobody thinks of as a database, while /api/policy publishes
route_logging:false and /drive tells a driver their destination is never logged.

A one-off edit would not hold: the image REWRITES valhalla.json from its own
defaults whenever tiles are rebuilt, so the next data refresh would silently
restore logging and nothing would notice. valhalla.service runs this as an
ExecStartPre so the quiet setting is reapplied on every single start, and a
rebuild cannot outlive it.

It also fails loudly rather than quietly: if the config cannot be read or
written, the unit does not start. An engine that is up but logging is worse
than one that is down, because the promise on the page is still being made.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def quiet(cfg: dict) -> dict:
    """Set every `logging` block in the config to silent, wherever it nests.

    Walks rather than reaching for known paths: Valhalla has moved the logging
    block between releases (top level, under httpd, under the service), and a
    quieting script that knows only last year's layout reports success while
    the engine goes on writing. Inventing keys that were not there would be the
    same mistake from the other side - it would mean shipping config the engine
    never asked for - so this only edits blocks that already exist, plus the
    top-level one, which every build reads.
    """
    def walk(node):
        if isinstance(node, dict):
            lg = node.get("logging")
            if isinstance(lg, dict):
                lg["type"] = ""
                # A "slow request" warning still names the request, so the
                # threshold goes past anything reachable rather than relying
                # on the engine being fast.
                lg["long_request"] = 1e9
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(cfg)
    top = cfg.setdefault("logging", {})
    top["type"] = ""
    top["long_request"] = 1e9
    return cfg


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: valhalla-quiet.py <valhalla.json>", file=sys.stderr)
        return 2
    p = Path(sys.argv[1])
    if not p.exists():
        # First ever boot builds the tiles and writes the config afterwards.
        # Nothing to quieten yet, and refusing to start would mean the tiles
        # could never be built at all.
        print(f"[valhalla-quiet] {p} does not exist yet - nothing to quieten")
        return 0
    cfg = json.loads(p.read_text())
    p.write_text(json.dumps(quiet(cfg), indent=2))
    print(f"[valhalla-quiet] request logging disabled in {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
