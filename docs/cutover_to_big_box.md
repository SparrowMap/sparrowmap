# Cutover: move the map to the 32 GB box

**Staged and rehearsed 2026-08-18. Nothing is live on the big box yet.**

Why: the map box is 2 cores with **984 MB free**, running the hub (1.6 GB, 85% CPU),
the ingest from ~10,800 cameras, and EmberFM. Memory is the ceiling that caused the
one real outage. The 32 GB box idles at **27.4 GB free** and load 2.7 on 8 cores.

## What is already done

| | |
|---|---|
| repo on the big box | at `origin/main` |
| hub deps | `cryptography`, `cffi`, `pycparser`, `segno` installed, **pinned to the live box's versions** so staging is not a different program |
| database | 448 MB, copied via the sqlite backup API (a consistent online snapshot, not `cp`), `PRAGMA quick_check` = ok, 983,979 rows |
| photographs | 393 files |
| secrets and state | `pepper.json`, `config.json`, `config.launch.json`, `costs.json`, `goals.json`, `faa_registry.json` |
| `public_mirror` | **True** |
| `operator_requires_auth` | **True** |
| service | `sparrowmap.service` running on **127.0.0.1:8150**, loopback only |
| `/support` | rebuilt on the box (it is gitignored and can only be built there) |
| Caddy | installed, config written, **stopped and disabled** |
| TLS certificate | `map.sparrowmap.com` copied, valid to **9 Nov 2026** |

**Rehearsed:** Caddy was started, the full HTTPS chain served `/`, `/api/stats`,
`/about` and `/support` all 200 with the correct certificate, then stopped again.

## Why the swap is short

The certificate is already on the box, so there is **no ACME round trip** at
cutover. The site is behind Cloudflare, so the origin address changes at
Cloudflare rather than in public DNS, and that takes effect in seconds rather
than waiting for a TTL anywhere.

## The cutover

```
# 1. refresh the data (the staged copy is from staging time)
ssh deploy_host 'systemctl stop sparrowmap'
ssh map_host '/opt/sparrowmap/.venv/bin/python -c "
import sqlite3
s=sqlite3.connect(\"/opt/sparrowmap/data/sparrow.db\")
d=sqlite3.connect(\"/tmp/cut.db\"); s.backup(d); d.close(); s.close()"'
ssh deploy_host 'rsync -a -e "ssh -i /path/to/deploy-key" root@map_host:/tmp/cut.db /opt/sparrowmap/data/sparrow.db
         rsync -a -e "ssh -i /path/to/deploy-key" root@map_host:/opt/sparrowmap/data/snaps/ /opt/sparrowmap/data/snaps/
         chown -R sparrow:sparrow /opt/sparrowmap/data
         systemctl start sparrowmap && systemctl enable --now caddy'

# 2. stop the old origin so nothing writes to two databases
ssh map_host 'systemctl stop caddy sparrowmap'

# 3. point Cloudflare at <map-box>  (dashboard, or the API)

# 4. verify
curl -sI https://map.sparrowmap.com/ | head -3
curl -s https://map.sparrowmap.com/api/stats
```

⚠️ **Step 2 matters more than it looks.** If both boxes serve, nodes upload to
whichever they reach and you end up with two divergent databases and no clean way
back. Stop the old one before the DNS flip, accept a few seconds of 5xx, and the
worst case is a node retrying.

## Rolling back

Start Caddy and the hub on the old box, point Cloudflare back. The old box is
untouched by this staging, so rollback is two commands and a DNS change. **Do
not delete anything on the map box until the new one has run a full day.**

## Afterwards, not during

* Move the camera reading to the US box (idle at load 0.07, and nearer the US cameras).
* Leave EmberFM alone on the $6.49 box, which is then doing one job.
* Point `box_puller`, `deploy.py` and the `.onion` at the new address.
* The `.onion` hidden service is configured on the OLD box. It moves by copying
  `/var/lib/tor/sparrowmap/` (that directory IS the address; lose it and the
  address changes).

## Loose ends from staging

* A one-time key `/path/to/deploy-key` on the big box is authorised on the map
  box. Remove it from the map box's `authorized_keys` when the migration is done.
* The staged database is a snapshot from staging time. Step 1 above re-syncs it;
  do not skip that.
