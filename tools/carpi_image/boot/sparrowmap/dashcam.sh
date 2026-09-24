#!/bin/bash
# SparrowMap car kit: the camera itself, as user "sparrow" (sparrow-dashcam.service).
# Exits on any problem; the unit restarts it in 15 s, so an unplugged webcam or
# a dropped hotspot heals itself when it comes back.
set -u
. /opt/sparrowmap/conf.sh

# A Pi has no clock battery, and a read-only card forgets the time at every
# power cut, so it boots believing it is the day the card was protected.
# Sightings would be stamped with that date. Nothing can be posted without the
# hotspot anyway, so wait for the network clock first.
if command -v timedatectl >/dev/null; then
    n=0
    until [ "$(timedatectl show -p NTPSynchronized --value 2>/dev/null)" = yes ]; do
        [ $((n % 60)) -eq 0 ] && echo "waiting for the clock to sync (needs the hotspot)"
        n=$((n + 1)); sleep 2
    done
fi

SRC=$(conf camera auto)
if [ "$SRC" = auto ]; then
    # 🚨 NOT /dev/video0. A Pi 5 numbers its own image-processor nodes first,
    # and every UVC webcam makes two nodes (index 0 = picture, 1 = metadata).
    # Take the first node that is on USB and is a picture.
    SRC=
    for d in /sys/class/video4linux/video*; do
        [ -e "$d" ] || continue
        case "$(readlink -f "$d/device")" in *usb*) ;; *) continue;; esac
        [ "$(cat "$d/index" 2>/dev/null)" = 0 ] || continue
        SRC=/dev/$(basename "$d"); break
    done
    if [ -z "$SRC" ]; then
        echo "no USB camera found; plug in the webcam (checking again in 15 s)"
        exit 1
    fi
fi

ARGS=(--source "$SRC" --gps gpsd://127.0.0.1:2947 --hub "$HUB")
is_yes plates && ARGS+=(--plates)

# Register once. relay.py remembers the camera in ~/.sparrowmap/node.json and
# only uses it for the SAME hub, so a changed hub= registers afresh.
NODE=$HOME/.sparrowmap/node.json
if ! "$APP/venv/bin/python" -c "
import json, sys
d = json.load(open('$NODE'))
sys.exit(0 if d.get('hub') == '$HUB' and d.get('token') else 1)" 2>/dev/null; then
    ARGS+=(--enroll "$(conf camera_name Dashcam)" --kind mobile)
fi

echo "camera $SRC -> $HUB"
exec "$APP/venv/bin/python" -u "$APP/relay.py" "${ARGS[@]}"
