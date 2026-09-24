#!/bin/bash
# SparrowMap car kit: run ONCE by cloud-init on the very first boot (user-data
# runcmd). Copies the kit off the boot partition and installs two services.
# Everything that needs the internet happens later, in setup.sh, which retries.
set -eu
KIT=/boot/firmware/sparrowmap
APP=/opt/sparrowmap

install -d -m 755 "$APP"
for f in conf.sh setup.sh dashcam.sh; do
    # The card may have been through Windows; a CR on the #! line breaks bash.
    sed 's/\r$//' "$KIT/$f" > "$APP/$f"
done
chmod 755 "$APP/setup.sh" "$APP/dashcam.sh"; chmod 644 "$APP/conf.sh"

cat > /etc/systemd/system/sparrow-setup.service <<'EOF'
[Unit]
Description=SparrowMap car kit: hotspot, first-time install, card protection
After=NetworkManager.service
Wants=NetworkManager.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/opt/sparrowmap/setup.sh
Restart=on-failure
RestartSec=30
TimeoutStartSec=60min

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/sparrow-dashcam.service <<'EOF'
[Unit]
Description=SparrowMap dashcam relay
After=sparrow-setup.service gpsd.socket
ConditionPathExists=/var/lib/sparrowmap/installed
StartLimitIntervalSec=0

[Service]
User=sparrow
ExecStart=/opt/sparrowmap/dashcam.sh
Restart=always
RestartSec=15

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable sparrow-setup.service sparrow-dashcam.service
systemctl start --no-block sparrow-setup.service
