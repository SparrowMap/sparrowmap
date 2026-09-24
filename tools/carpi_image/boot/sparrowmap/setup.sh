#!/bin/bash
# SparrowMap car kit, as root, at EVERY boot (sparrow-setup.service).
#
#   1. hotspot  - every boot, from sparrowmap.txt, so a typo is fixed by editing
#                 the file again on any computer; no login, no re-flash
#   2. install  - once, the first time there is internet
#   3. protect  - once the camera is registered, make the card read-only
#
# Exits non-zero while there is no internet yet; the unit retries every 30 s,
# which is the normal state of a first boot before the hotspot is switched on.
set -u
. /opt/sparrowmap/conf.sh
mkdir -p "$STATE"
say() { echo "sparrow-setup: $*"; }

# ---------------------------------------------------------------- 1. hotspot
COUNTRY=$(conf country US | tr 'a-z' 'A-Z')
SSID=$(conf hotspot_name)
PSK=$(conf hotspot_password)

# 🚨 Raspberry Pi OS keeps Wi-Fi soft-blocked until a country is set. Without
# this the hotspot below is configured perfectly and never joined.
raspi-config nonint do_wifi_country "$COUNTRY" >/dev/null 2>&1 || true
rfkill unblock wifi 2>/dev/null || true

if [ -z "$SSID" ]; then
    say "no hotspot_name in sparrowmap.txt; only a wired connection can work"
else
    CON=sparrow-hotspot
    want="$SSID|$PSK"
    have=$(nmcli -s -g 802-11-wireless.ssid,802-11-wireless-security.psk \
           connection show "$CON" 2>/dev/null | paste -sd'|')
    if [ "$have" != "$want" ]; then
        nmcli connection delete "$CON" >/dev/null 2>&1
        if [ -n "$PSK" ]; then
            nmcli connection add type wifi con-name "$CON" ifname wlan0 ssid "$SSID" \
                wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$PSK" \
                connection.autoconnect yes connection.autoconnect-retries 0 >/dev/null
        else
            nmcli connection add type wifi con-name "$CON" ifname wlan0 ssid "$SSID" \
                connection.autoconnect yes connection.autoconnect-retries 0 >/dev/null
        fi
        say "hotspot set to \"$SSID\""
    fi
    nmcli --wait 0 connection up "$CON" >/dev/null 2>&1 || true
fi

# Optional login. The password is never echoed.
PW=$(conf login_password)
if [ -n "$PW" ]; then
    printf 'sparrow:%s\n' "$PW" | chpasswd && passwd -u sparrow >/dev/null 2>&1
    systemctl enable --now ssh >/dev/null 2>&1
else
    passwd -l sparrow >/dev/null 2>&1
    systemctl disable --now ssh >/dev/null 2>&1
fi

# ---------------------------------------------------------------- 2. install
online() { curl -fsS -m 10 -o /dev/null "$HUB/relay.py"; }

if [ ! -f "$STATE/installed" ]; then
    if ! online; then
        say "waiting for internet (is the hotspot on and named exactly as in sparrowmap.txt?)"
        exit 75
    fi
    say "first-time install"
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -q || exit 75
    # gpsd reads the USB GPS; the rest is what pip's wheels need to load.
    apt-get install -y -q --no-install-recommends \
        gpsd gpsd-clients python3-venv v4l-utils || exit 75

    # gpsd: USB receivers are found by udev hotplug; -n keeps it polling the
    # receiver before any client connects, so a fix is ready when the relay asks.
    sed -i 's/^USBAUTO=.*/USBAUTO="true"/; s/^GPSD_OPTIONS=.*/GPSD_OPTIONS="-n"/' /etc/default/gpsd
    systemctl enable gpsd.socket gpsd.service >/dev/null 2>&1
    systemctl restart gpsd.service >/dev/null 2>&1

    # The relay's three packages, in their own environment. Wheels, not apt:
    # apt's python3-opencv pulls in a desktop's worth of libraries over a phone
    # hotspot; the headless wheel is ~40 MB.
    [ -x "$APP/venv/bin/python" ] || python3 -m venv "$APP/venv" || exit 75
    PKGS="numpy opencv-python-headless onnxruntime"
    is_yes plates && PKGS="$PKGS fast-plate-ocr open-image-models"
    "$APP/venv/bin/pip" install -q --disable-pip-version-check $PKGS || exit 75

    curl -fsSL -m 60 -o "$APP/relay.py.part" "$HUB/relay.py" && mv "$APP/relay.py.part" "$APP/relay.py" || exit 75

    # Fetch and hash-check the 38 MB detector NOW, as the camera's user, so it
    # is on the card before the card is made read-only.
    sudo -u sparrow -H "$APP/venv/bin/python" -c "
import sys; sys.path.insert(0, '$APP'); import relay, onnxruntime
print('  detector ready:', relay.model_path('$HUB'))" || exit 75

    touch "$STATE/installed"
    say "installed; starting the camera"
    systemctl start --no-block sparrow-dashcam.service
fi

# A plates=yes added after the install still gets its packages.
if is_yes plates && ! "$APP/venv/bin/python" -c "import fast_plate_ocr, open_image_models" 2>/dev/null; then
    online && "$APP/venv/bin/pip" install -q fast-plate-ocr open-image-models
fi

# ---------------------------------------------------------------- 3. protect
# Only AFTER the camera has registered: its identity (node.json) must be on the
# card, or every boot of a read-only card would register a new camera.
if is_yes protect_card yes && [ -f /home/sparrow/.sparrowmap/node.json ] \
   && ! grep -q ' / overlay ' /proc/mounts; then
    if [ -f "$STATE/protect-tried" ]; then
        say "card protection did not take effect after a restart; leaving it off"
    else
        touch "$STATE/protect-tried"
        say "camera registered; making the card read-only and restarting once"
        raspi-config nonint do_overlayfs 0 && sync && systemctl reboot
    fi
fi
exit 0
