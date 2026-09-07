#!/usr/bin/env bash
# Provision the PUBLIC-CAMERA POLLER box from nothing. Ubuntu 24.04, run as root.
#
#   scp deploy/cameras-box-setup.sh root@<ip>:/root/
#   ssh root@<ip> 'bash /root/cameras-box-setup.sh'
#
# 🚨 THIS IS NOT THE HUB BOX AND MUST NEVER BECOME ONE.
#
# It holds no database, serves nothing, and opens no port. It downloads public
# traffic-camera JPEGs, finds vehicles in them, shrinks each catch below plate
# legibility, and POSTs the crop to map.sparrowmap.com like any other node. If
# this machine is lost, the map keeps working and nobody's data is on it.
#
# Why a second box at all: the poller is a bandwidth job (measured: 0.36s of
# network against 0.07s of inference per camera, ~1 TB/day at fleet scale). That
# does not belong on a 2-vCPU server that is also answering the public site, and
# it does not belong on a home connection either.
#
# WHAT IT DELIBERATELY DOES NOT INSTALL
#   * opencv - this poller decodes with PIL. detect/relay.py imports cv2 at
#     module level, so public_cams.load_model() reads the weights from the
#     checkout instead of asking relay for the path. ~90 MB and a system libGL
#     saved for a file lookup.
#   * torch / CLIP - the classifier head runs at HOME, on the crops the box
#     already holds. This machine only answers "is that a vehicle".
#
# ⚠️ NEVER RUN git AS root IN /opt/sparrowmap. It leaves root-owned objects in
# .git/objects and every later `sudo -u sparrow git pull` fails with
# "insufficient permission", which surfaces as "failed to write object" and
# reads exactly like a full disk. Every git call below is `sudo -u sparrow`.
set -euo pipefail

REPO="${RAVEN_REPO:-${SPARROW_REPO:-${REPO:-}}}"
if [ -z "$REPO" ]; then
  echo "No RavenMap source repository configured. Set RAVEN_REPO." >&2
  echo "Legacy SPARROW_REPO (or existing REPO) is accepted; no upstream default is used." >&2
  exit 1
fi
DIR=/opt/sparrowmap
USER_NAME=sparrow

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

say "packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
# python3-venv is separate from python3 on Ubuntu and its absence only shows up
# as a confusing ensurepip error halfway through.
apt-get install -y -qq git python3 python3-venv python3-pip ca-certificates curl

say "user and checkout"
id -u "$USER_NAME" >/dev/null 2>&1 || useradd -m -s /bin/bash "$USER_NAME"
if [ ! -d "$DIR/.git" ]; then
  mkdir -p "$DIR"
  chown "$USER_NAME:$USER_NAME" "$DIR"
  sudo -u "$USER_NAME" git clone --depth 1 "$REPO" "$DIR"
else
  sudo -u "$USER_NAME" git -C "$DIR" pull --ff-only
fi
chown -R "$USER_NAME:$USER_NAME" "$DIR"

say "python environment"
sudo -u "$USER_NAME" python3 -m venv "$DIR/.venv"
sudo -u "$USER_NAME" "$DIR/.venv/bin/pip" install --quiet --upgrade pip
# Pinned to the three the poller actually imports. Adding to this list is a
# decision, not a convenience: every package here runs unattended for months.
sudo -u "$USER_NAME" "$DIR/.venv/bin/pip" install --quiet \
  "numpy>=1.24" "pillow>=10" "onnxruntime>=1.16"

say "checks"
# Fail here, loudly, rather than inside a service that will just restart.
sudo -u "$USER_NAME" "$DIR/.venv/bin/python" - <<'PY'
import sys, pathlib
sys.path.insert(0, "/opt/sparrowmap")
import numpy, PIL, onnxruntime                       # noqa: F401
w = pathlib.Path("/opt/sparrowmap/public/vendor/yolo11s.onnx")
assert w.exists(), "detector weights missing from the checkout: " + str(w)
print("  numpy", numpy.__version__, "| onnxruntime", onnxruntime.__version__)
print(f"  detector {w.stat().st_size // 1024 // 1024} MB")
PY

say "service"
install -m 644 "$DIR/deploy/sparrowmap-cams.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable sparrowmap-cams.service >/dev/null

cat <<EOF

Provisioned. The service is enabled but NOT started, because it cannot post
without credentials and a service that restart-loops on a missing file is a
worse signal than one that was never started.

⏭️ NEXT, and it is two commands on two different machines:

  1. On the HUB box, export the fleet's credentials (reads the database):
       cd /opt/sparrowmap && sudo -u sparrow .venv/bin/python \\
         tools/public_cams.py tokens --out /tmp/cam_tokens.json

  2. Copy it here and start:
       scp <hub>:/tmp/cam_tokens.json root@<this box>:$DIR/data/cam_tokens.json
       chown $USER_NAME:$USER_NAME $DIR/data/cam_tokens.json
       chmod 600 $DIR/data/cam_tokens.json
       systemctl start sparrowmap-cams && journalctl -fu sparrowmap-cams

⚠️ That file is thousands of camera tokens in plain text. It is a credential
   store, not config: 600, owned by $USER_NAME, and never in the repo.
EOF
