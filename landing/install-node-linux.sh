#!/usr/bin/env bash
# SparrowMap desktop camera - turn-key installer for Linux / macOS.
#
#   ⚠  UNTESTED on Linux/macOS. The Windows path is the one we run daily; this
#      mirrors it but has not been exercised end-to-end. Please help us test it
#      and report anything broken at
#      the issue tracker for the configured RavenMap source repository
#
# Turns a machine with a webcam into a SparrowMap camera that contributes to the
# configured RavenMap hub. Installs the camera code into your
# user account, sets up the detector, and starts you at the "aim your camera"
# step.
#
#   Configure RAVEN_HUB and RAVEN_REPO, then run this script from the checkout.
#
# Re-running updates an existing install. Set RAVEN_HUB (or legacy
# SPARROW_HUB) to your hub before running; there is no implicit default.

set -euo pipefail

HUB="${RAVEN_HUB:-${SPARROW_HUB:-}}"; HUB="${HUB%/}"
if [ -z "$HUB" ]; then
  echo "No RavenMap hub configured. Set RAVEN_HUB to your hub URL." >&2
  echo "Legacy SPARROW_HUB is also accepted." >&2
  exit 1
fi
REPO="${RAVEN_REPO:-${SPARROW_REPO:-}}"
if [ -z "$REPO" ]; then
  echo "No RavenMap source repository configured. Set RAVEN_REPO." >&2
  echo "Legacy SPARROW_REPO is also accepted; no upstream default is used." >&2
  exit 1
fi
ROOT="${XDG_DATA_HOME:-$HOME/.local/share}/sparrowmap"
APP="$ROOT/app"

say()  { printf '\n\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '    \033[1;33m%s\033[0m\n' "$*"; }

printf '\n\033[1;36m  SparrowMap desktop camera setup\033[0m\n'
printf '  Contributes a real camera to %s\n' "$HUB"
warn "UNTESTED on this platform - please report issues on GitHub."

# --- prerequisites. Distros vary too much to auto-install safely, so on a
#     missing tool we print the exact package hint and stop rather than guess. --
pkg_hint() {
  if   command -v apt-get >/dev/null 2>&1; then echo "sudo apt-get install -y $1"
  elif command -v dnf     >/dev/null 2>&1; then echo "sudo dnf install -y $1"
  elif command -v pacman  >/dev/null 2>&1; then echo "sudo pacman -S --noconfirm $1"
  elif command -v brew    >/dev/null 2>&1; then echo "brew install $1"
  else echo "install: $1"; fi
}
need() {
  command -v "$1" >/dev/null 2>&1 && return 0
  echo "Missing '$1'. Install it, then re-run:" >&2
  echo "    $(pkg_hint "${2:-$1}")" >&2
  exit 1
}
need git git
need python3 python3
# venv is a separate package on Debian/Ubuntu and its absence is a classic trap.
if ! python3 -c 'import venv, ensurepip' >/dev/null 2>&1; then
  echo "python3 venv/pip support is missing. Install it, then re-run:" >&2
  echo "    $(pkg_hint python3-venv)" >&2
  exit 1
fi
PYV="$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
if [ "$(printf '%s\n3.10\n' "$PYV" | sort -V | head -1)" != "3.10" ]; then
  echo "Python 3.10+ required (found $PYV)." >&2; exit 1
fi

# --- get / update the code ---------------------------------------------------
if [ -d "$APP/.git" ]; then
  say "Updating RavenMap in $APP"; git -C "$APP" pull --ff-only
else
  say "Downloading RavenMap to $APP"; mkdir -p "$ROOT"; git clone --depth 1 "$REPO" "$APP"
fi

# --- python environment + dependencies --------------------------------------
[ -x "$APP/.venv/bin/python" ] || { say "Creating a Python environment"; python3 -m venv "$APP/.venv"; }
PY="$APP/.venv/bin/python"
"$PY" -m pip install --upgrade pip --quiet

# Torch first, matched to the machine: CUDA build if an NVIDIA GPU is present
# (much faster), CPU build otherwise (fine for one camera).
say "Installing the recognition models (this is ~2.5 GB and can take a while)"
if command -v nvidia-smi >/dev/null 2>&1; then
  echo "    NVIDIA GPU detected - installing the CUDA build of PyTorch."
  "$PY" -m pip install torch==2.5.1 --index-url https://download.pytorch.org/whl/cu121
else
  echo "    No NVIDIA GPU - installing the CPU build of PyTorch (fine for one camera)."
  "$PY" -m pip install torch==2.5.1
fi
"$PY" -m pip install -r "$APP/requirements-node.txt"

# --- point this machine at the configured hub --------------------------------
if ! grep -qs "export RAVEN_HUB=" "$HOME/.profile" 2>/dev/null; then
  echo "export RAVEN_HUB=$HUB" >> "$HOME/.profile"
fi
export RAVEN_HUB="$HUB"

# --- autostart + launcher ----------------------------------------------------
chmod +x "$APP/desktop/run-node.sh"
LAUNCH="$APP/desktop/run-node.sh"
if systemctl --user status >/dev/null 2>&1; then
  say "Starting at login via a systemd --user service"
  mkdir -p "$HOME/.config/systemd/user"
  cat > "$HOME/.config/systemd/user/sparrowmap-camera.service" <<UNIT
[Unit]
Description=RavenMap camera node
After=graphical-session.target

[Service]
Environment=RAVEN_HUB=$HUB
ExecStart=$LAUNCH
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
UNIT
  systemctl --user daemon-reload
  systemctl --user enable --now sparrowmap-camera.service || warn "Could not enable the service; start it with: $LAUNCH"
else
  warn "systemd --user not available; adding a desktop autostart entry instead."
  mkdir -p "$HOME/.config/autostart"
  cat > "$HOME/.config/autostart/sparrowmap-camera.desktop" <<DESK
[Desktop Entry]
Type=Application
Name=RavenMap Camera
Exec=$LAUNCH
X-GNOME-Autostart-enabled=true
DESK
fi

# --- go ----------------------------------------------------------------------
say "Done. Opening the camera setup - aim your camera and draw the road it watches."
echo
echo "  To review your camera's catches later, ask for a reviewer link to"
echo "  $HUB/rv"
echo
exec "$LAUNCH"
