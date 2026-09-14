#!/usr/bin/env bash
# Stand up (or refresh) the contributor beta on the live box. Idempotent.
#
#   ssh root@box 'bash -s' < tools/beta_setup.sh          (tools/beta.py setup does this)
#
# What it makes:
#   /opt/sparrowmap-beta/            own data dir + config, SAME checkout (/opt/sparrowmap)
#   sparrowmap-beta.service          hub.py --port 8152, SPARROW_DATA/SPARROW_CONFIG set
#   Caddy: beta.sparrowmap.com       reverse_proxy 127.0.0.1:8152
#   fixtures                         the synthetic town, ~300 ticks, watermarked crops
#
# It never touches /opt/sparrowmap/data or sparrowmap.service. The beta is a
# second process on the same commit; deploy.py restarts both.
set -euo pipefail

SRC=/opt/sparrowmap
BETA=/opt/sparrowmap-beta
PORT=8152
HOST=beta.sparrowmap.com
UNIT=/etc/systemd/system/sparrowmap-beta.service
CADDY=/etc/caddy/Caddyfile
PY=$SRC/.venv/bin/python

echo "== beta dir"
install -d -o sparrow -g sparrow -m 750 "$BETA" "$BETA/data"

echo "== config"
if [ ! -f "$BETA/config.json" ]; then
  # Start from the live launch config so the beta inherits every public-box
  # safety default, then override the handful that make it a beta.
  "$PY" - "$SRC/config.json" "$BETA/config.json" <<'PYEOF'
import json, sys
src, dst = sys.argv[1], sys.argv[2]
c = json.load(open(src))
c = {k: v for k, v in c.items() if not k.startswith("_")}
c.update({
    "site_name": "SparrowMap beta",
    "http_port": 8152, "https_port": 8153,
    "beta": True,
    # Full hub, not a mirror: the data is synthetic, so the review and
    # operator surfaces are exactly what contributors are here to exercise.
    "public_mirror": False,
    "operator_requires_auth": True,
    "behind_tls": True,
    "auto_approve_nodes": True,
    "publish_public_tier": True,
})
json.dump(c, open(dst, "w"), indent=2)
print("wrote", dst)
PYEOF
  chown sparrow:sparrow "$BETA/config.json"
else
  echo "config exists, kept"
fi

echo "== unit"
cat > "$UNIT" <<EOF
[Unit]
Description=SparrowMap contributor beta (same checkout, own data, synthetic)
After=network-online.target

[Service]
User=sparrow
Group=sparrow
WorkingDirectory=$SRC
Environment=SPARROW_BIND=127.0.0.1
Environment=SPARROW_DATA=$BETA/data
Environment=SPARROW_CONFIG=$BETA/config.json
Environment=PYTHONUNBUFFERED=1
# --sim keeps the synthetic town LIVING: the map's live view is the last hour,
# so a one-off fixture run leaves the beta looking empty by lunchtime. Every
# row it writes is source='synthetic'.
ExecStart=$PY $SRC/hub.py --port $PORT --https-port $((PORT+1)) --sim
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
LimitNOFILE=16384
CPUWeight=200

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload

echo "== fixtures"
if [ ! -f "$BETA/data/sparrow.db" ] || [ "${REFRESH_FIXTURES:-0}" = "1" ]; then
  sudo -u sparrow env SPARROW_DATA="$BETA/data" SPARROW_CONFIG="$BETA/config.json" \
    "$PY" "$SRC/sources/synthetic.py" "${FIXTURE_TICKS:-300}" | tail -5
else
  echo "fixtures exist, kept (REFRESH_FIXTURES=1 to rebuild)"
fi

echo "== caddy"
if ! grep -q "^$HOST" "$CADDY"; then
  cat >> "$CADDY" <<EOF

# Contributor beta: same code as the map, own synthetic data, gated in the hub
# (every route needs a reviewer token) and, once Access is on, at the edge.
$HOST {
	log {
		output discard
	}
	# Same self-hosted basemap as the live map (tiles are tiles; nothing in
	# /basemap is a sighting), so the beta never falls back to a third party.
	handle_path /basemap/tiles/* {
		reverse_proxy 127.0.0.1:8151
		header Access-Control-Allow-Origin "*"
		header Cache-Control "public, max-age=604800"
	}
	handle /basemap/* {
		root * $SRC/data
		file_server
		header Access-Control-Allow-Origin "*"
		header Cache-Control "public, max-age=2592000"
	}
	handle {
		reverse_proxy 127.0.0.1:$PORT {
			header_up -X-Forwarded-For
		}
	}
}
EOF
  caddy validate --config "$CADDY" >/dev/null && systemctl reload caddy
  echo "caddy block added + reloaded"
else
  echo "caddy block exists"
fi

echo "== service"
systemctl enable --now sparrowmap-beta.service >/dev/null
systemctl restart sparrowmap-beta.service
sleep 4
systemctl is-active sparrowmap-beta.service
code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/beta")
echo "local /beta -> $code"
code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/")
echo "local / (signed out) -> $code  (200 = sign-in page served in place)"
code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/api/sightings")
echo "local /api/sightings (signed out) -> $code  (401 expected)"
echo "== done. DNS: $HOST -> this box (proxied) is Matthew's record to add."
