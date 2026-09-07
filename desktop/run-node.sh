#!/usr/bin/env bash
# RavenMap camera node - launcher (Linux / macOS).
#
# Starts the camera-control UI (which owns the webcam and serves its video to
# the detector) and then the detector, which posts sightings to the public
# RavenMap network. Installed by install-node-linux.sh; also run by the
# systemd --user service and from the desktop entry.
#
# Set RAVEN_HUB (or legacy SPARROW_HUB) to point this at your hub before it
# runs (e.g. export RAVEN_HUB=http://localhost:8150). There is no implicit
# default hub; an unconfigured install fails clearly instead of guessing.

set -euo pipefail

SELF="$(cd "$(dirname "$0")" && pwd)"
APP="$(dirname "$SELF")"
PY="$APP/.venv/bin/python"
PLACE="$APP/camctl/placement.json"
HUB="${RAVEN_HUB:-${SPARROW_HUB:-}}"
HUB="${HUB%/}"

if [ ! -x "$PY" ]; then
  echo "RavenMap is not installed yet. Run install-node-linux.sh first." >&2
  exit 1
fi
if [ -z "$HUB" ]; then
  echo "No RavenMap hub configured. Set RAVEN_HUB to your hub URL." >&2
  echo "Legacy SPARROW_HUB is also accepted." >&2
  exit 1
fi

open_url() {
  if command -v xdg-open >/dev/null 2>&1; then xdg-open "$1" >/dev/null 2>&1 || true
  elif command -v open >/dev/null 2>&1; then open "$1" >/dev/null 2>&1 || true
  else echo "  Open $1 in your browser."; fi
}
camctl_up() { curl -fsS -m 2 http://localhost:8160/ >/dev/null 2>&1; }
enrolled() {
  [ -f "$PLACE" ] || return 1
  "$PY" -c "import json,sys; sys.exit(0 if json.load(open('$PLACE')).get('enrolled') else 1)" 2>/dev/null
}

echo
echo "  RavenMap camera node"
echo "    posts to  $HUB"
echo

# 1) Camera control first: it owns the webcam and re-serves it as MJPEG. If the
#    detector opens the camera first, the control page shows a black rectangle.
if ! camctl_up; then
  echo "  Starting camera control..."
  ( cd "$APP" && "$PY" camctl/camctl.py >/dev/null 2>&1 & )
  for _ in $(seq 1 40); do camctl_up && break; sleep 1; done
fi
if ! camctl_up; then
  echo "  Camera control did not start - is a webcam connected?" >&2
  exit 1
fi

# 2) First run: aim the camera and draw the road. Saving enrolls the camera on
#    the network and writes its id + token to placement.json.
if ! enrolled; then
  echo "  Opening the setup page. Aim your camera, draw the stretch of public"
  echo "  road it watches, and click Save. Waiting for that..."
  open_url 'http://localhost:8160/'
  until enrolled; do sleep 3; done
  echo "  Camera enrolled."
fi

# Show the owner where to review their OWN camera's catches. A camera enrolled
# through camctl already has its reviewer token saved; one enrolled before that
# existed fetches it now, proving ownership with its node token. Minted once.
RTOK="$("$PY" - "$PLACE" "$HUB" <<'PY' 2>/dev/null || true
import json, sys, urllib.request
place_path, hub = sys.argv[1], sys.argv[2]
p = json.load(open(place_path))
tok = p.get("review_token")
if not tok and p.get("node_id") and p.get("token"):
    try:
        req = urllib.request.Request(
            hub.rstrip("/") + "/api/rv/my-token", method="POST",
            data=json.dumps({"node_id": p["node_id"]}).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + p["token"]})
        r = json.loads(urllib.request.urlopen(req, timeout=15).read())
        if r.get("new") and r.get("review_token"):
            tok = r["review_token"]
            p["review_token"] = tok
            json.dump(p, open(place_path, "w"), indent=1)
    except Exception:
        pass
print(tok or "")
PY
)"
if [ -n "$RTOK" ]; then
  echo
  echo "  Review your camera's catches: open $HUB/rv and paste this token:"
  echo "    $RTOK"
  echo
fi

# 3) Run the detector against the hub. It reads the node id + token from the
#    placement file (never pasted here) and is restarted if it dies for a real
#    reason. Exit code 1 is the single-instance guard - do not loop on it.
while true; do
  NODE="$("$PY" -c "import json;print(json.load(open('$PLACE'))['node_id'])")"
  TOKEN="$("$PY" -c "import json;print(json.load(open('$PLACE'))['token'])")"
  echo "  Detector running as $NODE. Ctrl-C to stop."
  set +e
  ( cd "$APP" && "$PY" detect/run_live.py --post --visual --hub "$HUB" --node "$NODE" --token "$TOKEN" )
  code=$?
  set -e
  if [ "$code" -eq 1 ]; then
    echo "  Another detector is already running for this camera. Close it first." >&2
    exit 1
  fi
  echo "  Detector exited; restarting in 10s (Ctrl-C to stop)."
  sleep 10
done
