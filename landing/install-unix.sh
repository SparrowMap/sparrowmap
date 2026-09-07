#!/usr/bin/env bash
#
# SparrowMap - desktop camera, for macOS and Linux.
#
#   ./install-unix.sh https://your-hub.example
#
# ## What this installs, and what it deliberately does not
#
# It does NOT install SparrowMap. The camera runs entirely in the browser; there
# is no program to install, and adding one would mean asking people to trust a
# binary when they do not have to.
#
# What it fixes is the one thing a browser cannot fix for itself: **a hidden or
# minimised tab is frozen by the browser and sees nothing.** So this makes a
# launcher that opens SparrowMap as its own window - Chromium's --app mode, no
# tabs, no address bar - and offers to start it at login.

set -euo pipefail

# Pass the hub URL explicitly, or configure RAVEN_HUB/legacy SPARROW_HUB.
URL="${1:-${RAVEN_HUB:-${SPARROW_HUB:-}}}"
AUTOSTART="${2:-yes}"
say() { printf '\n\033[1;36m==>\033[0m %s\n' "$*"; }

if [ -z "$URL" ]; then
  echo "No RavenMap hub configured. Set RAVEN_HUB or pass the hub URL explicitly." >&2
  echo "Legacy SPARROW_HUB is also accepted; no upstream default is used." >&2
  exit 1
fi
if [[ ! "$URL" =~ ^https?:// ]]; then
  echo "usage: install-unix.sh [https://your-instance] [no-autostart]" >&2
  exit 1
fi
APP_URL="${URL%/}/app"
PROFILE="$HOME/.sparrow-camera"
mkdir -p "$PROFILE"

# --- find a Chromium browser ------------------------------------------------
# Firefox has no --app equivalent, so it is not offered rather than silently
# producing a normal tab that gets minimised and freezes.
say "Looking for Chrome, Chromium, Brave or Edge"
BROWSER=""
CANDIDATES=(
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
  "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"
  "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"
  "/Applications/Chromium.app/Contents/MacOS/Chromium"
)
for c in "${CANDIDATES[@]}"; do [[ -x "$c" ]] && BROWSER="$c" && break; done
if [[ -z "$BROWSER" ]]; then
  for c in google-chrome google-chrome-stable chromium chromium-browser \
           brave-browser microsoft-edge; do
    if command -v "$c" >/dev/null 2>&1; then BROWSER="$(command -v "$c")"; break; fi
  done
fi
[[ -n "$BROWSER" ]] || { echo "No Chromium-based browser found. Install Chrome or Chromium." >&2; exit 1; }
echo "    $BROWSER"

ARGS=(--app="$APP_URL" --user-data-dir="$PROFILE" --start-maximized)

if [[ "$(uname)" == "Darwin" ]]; then
  # --- macOS: a real .app bundle, so it behaves like a program -------------
  say "Creating RavenMap Camera.app"
  APP="$HOME/Applications/SparrowMap Camera.app"
  mkdir -p "$APP/Contents/MacOS"
  cat >"$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>RavenMap Camera</string>
  <key>CFBundleIdentifier</key><string>net.sparrowmap.camera</string>
  <key>CFBundleExecutable</key><string>run</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>LSMinimumSystemVersion</key><string>10.13</string>
</dict></plist>
PLIST
  cat >"$APP/Contents/MacOS/run" <<RUN
#!/bin/sh
# caffeinate keeps the Mac awake while the camera window is open, and exits
# with it - a sleeping machine is a camera that stopped.
exec /usr/bin/caffeinate -di "$BROWSER" --app="$APP_URL" --user-data-dir="$PROFILE"
RUN
  chmod +x "$APP/Contents/MacOS/run"
  echo "    $APP"

  if [[ "$AUTOSTART" != "no-autostart" ]]; then
    say "Starting it at login"
    # 🚨 VERIFY, DO NOT ASSUME. This is the ONE path that cannot be tested
    # here - there is no Mac, and running macOS in a VM on other hardware
    # breaks Apple's licence. osascript fails in several quiet ways: no
    # Automation permission, a locked System Events, a newer macOS that
    # changed the dictionary. Each of those can look like success and add
    # nothing. So ask afterwards whether the item is really there, and say so
    # plainly if it is not, rather than printing "added" and being wrong.
    osascript -e "tell application \"System Events\" to make login item at end       with properties {path:\"$APP\", hidden:false, name:\"SparrowMap Camera\"}"       >/dev/null 2>&1 || true
    if osascript -e 'tell application "System Events" to get the name of every login item'          2>/dev/null | grep -q "SparrowMap Camera"; then
      echo "    added to Login Items (verified)"
    else
      echo "    COULD NOT add it automatically."
      echo "    Add it by hand: System Settings -> General -> Login Items -> +"
      echo "      $APP"
      echo "    (macOS may have blocked the automation prompt; the app still works.)"
    fi
  fi
  say "Power"
  echo "    A sleeping Mac stops watching. The launcher runs caffeinate, which"
  echo "    holds it awake for as long as the camera window is open."

else
  # --- Linux: a .desktop entry --------------------------------------------
  say "Creating the RavenMap Camera launcher"
  APPS="$HOME/.local/share/applications"
  mkdir -p "$APPS"
  DESKTOP="$APPS/sparrow-camera.desktop"
  cat >"$DESKTOP" <<EOF
[Desktop Entry]
Type=Application
Name=RavenMap Camera
Comment=Watch the road from this computer
Exec=$BROWSER --app=$APP_URL --user-data-dir=$PROFILE
Terminal=false
Categories=Network;
StartupNotify=true
EOF
  chmod +x "$DESKTOP"
  echo "    $DESKTOP"

  if [[ "$AUTOSTART" != "no-autostart" ]]; then
    say "Starting it at login"
    mkdir -p "$HOME/.config/autostart"
    cp "$DESKTOP" "$HOME/.config/autostart/sparrow-camera.desktop"
    echo "    $HOME/.config/autostart/"
  fi
  say "Power"
  echo "    A suspending machine is a camera that stopped. On GNOME:"
  echo "      gsettings set org.gnome.settings-daemon.plugins.power sleep-inactive-ac-type nothing"
  echo "    (the app also holds a screen wake lock while it is running)"
fi

cat <<EOF

$(say "Done")
    Open "RavenMap Camera".

    First time: name the camera, allow camera and location, press Register,
    then Start watching. The detector downloads once (~21 MB) and is cached.

    Nothing was installed. This is a launcher for a web page in its own
    window. Delete it and all that remains is the browser profile at:
      $PROFILE

    A separate profile on purpose: in your everyday browser, one "clear
    browsing data" would delete the camera's key.
EOF
