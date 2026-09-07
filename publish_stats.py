"""Push the public counters from the home hub to sparrowmap.com.

The public site is static (Cloudflare Pages) and the database is on a PC at
home. Those two facts decide the whole design:

  ❌ the page CANNOT fetch the hub directly. That would put the operator's home
     IP address in front of every visitor, which is a worse leak than anything
     the project stores - the site exists to argue that a breach yields only
     the published record.
  ❌ the hub cannot accept inbound connections. No port forwarding, no dynamic
     DNS, nothing listening on the public internet.
  ✅ so the hub PUSHES. Outbound only, a few hundred bytes, on a timer. The
     public endpoint holds the last value it was given and serves it to
     visitors. Nothing about the home network is ever exposed.

⚠️ ONLY AGGREGATES ARE SENT, AND THAT IS ENFORCED HERE RATHER THAN TRUSTED.
The payload is rebuilt field by field from an allowlist below. A future change
to tools/public_stats.py that adds positions, per-node detail or timestamps
cannot leak them by accident, because anything not named here is dropped.

Usage:
    python publish_stats.py --dry-run          # show exactly what would be sent
    python publish_stats.py                    # send it once
    python publish_stats.py --every 900        # keep sending every 15 minutes

The endpoint and secret come from config.json (gitignored):
    "stats_push_url":    "https://sparrowmap.com/api/stats",
    "stats_push_token":  "..."
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# 🚨 THE ALLOWLIST IS THE PRIVACY CONTROL. Nothing outside it is transmitted,
# whatever tools/public_stats.py grows into later. Each of these is a COUNT,
# and a count of vehicles cannot be turned back into which vehicles.
ALLOWED = ("vehicles_seen", "sightings", "confirmed_public", "private_seen",
           "private_plates_stored", "cameras")


def payload() -> dict:
    from tools.public_stats import stats
    s = stats()
    # 🚨 REFUSE TO PUBLISH A BROKEN PROMISE. The site says "0 private number
    # plates stored". If that ever stops being true, stop publishing rather than
    # publish a number that makes the sentence beside it false.
    if s.get("private_plates_stored") != 0:
        raise SystemExit("REFUSING: private plate text exists; the site claims 0")
    out = {k: s[k] for k in ALLOWED if k in s}
    out["as_of"] = datetime.now(timezone.utc).strftime("%d %B %Y, %H:%M UTC")
    return out


def send(url: str, token: str, body: dict) -> int:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json",
                 # Identify the client. See core.NODE_UA - an unnamed client is
                 # what Cloudflare's bot protection banned network-wide. This
                 # module deliberately does not import core (it runs standalone
                 # on the home hub), so the string is repeated rather than
                 # dragging in a dependency for one header.
                 "User-Agent": "RavenMap-Node/1.0",
                 "Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.status


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--every", type=int, default=0,
                    help="seconds between pushes; 0 sends once and exits")
    a = ap.parse_args()

    cfg = {}
    cfgp = ROOT / "config.json"
    if cfgp.exists():
        cfg = json.loads(cfgp.read_text(encoding="utf-8"))
    url = cfg.get("stats_push_url")
    token = cfg.get("stats_push_token")

    while True:
        body = payload()
        if a.dry_run or not (url and token):
            if not a.dry_run:
                print("no stats_push_url/stats_push_token in config.json - "
                      "showing the payload instead of sending it")
            print(json.dumps(body, indent=1))
        else:
            try:
                print(f"{body['as_of']}  ->  HTTP {send(url, token, body)}  "
                      f"{body['vehicles_seen']} seen / "
                      f"{body['confirmed_public']} confirmed")
            except urllib.error.URLError as exc:
                # Never die on a network blip. The endpoint keeps serving the
                # last good value, which is stale by minutes rather than absent.
                print(f"push failed, will retry: {exc}")
        if not a.every:
            return 0
        time.sleep(max(60, a.every))


if __name__ == "__main__":
    raise SystemExit(main())
