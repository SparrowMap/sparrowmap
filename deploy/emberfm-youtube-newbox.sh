#!/bin/bash
# Set up a NEW Hetzner box to run EmberFM's YouTube encoder, and nothing else.
#
# 🚨 WHY ONLY THIS SERVICE. Measured on the shared box: the YouTube libx264
# encoder used 107% CPU while the ENTIRE radio station - icecast2, radio.py and
# the audio encoder together - used 4.3%. Moving the radio would drop every
# listener and the TuneIn feed to reclaim 4.3%. Moving this one service reclaims
# a whole core and no listener is downstream of it.
# See docs/EMBERFM_MOVE_PLAN.md.
#
# Run FROM the old box:
#     bash deploy/project-stream-newbox.sh <new-box-ip> [--cutover]
#
# Without --cutover it installs and stops, changing nothing that is live. The
# cutover is separate and deliberate because the upstream accepts one stream per key,
# so the old encoder must stop before the new one starts and the channel has a
# short gap. That is a moment to choose, not a side effect of an install.

set -euo pipefail

NEW="${1:-}"
CUTOVER="${2:-}"
SRC=/srv/ravenmap/streamer
# The new box reads the audio over the public stream instead of localhost. This
# is the only functional change, and it is why the migration is small: the
# encoder's only real dependency is a URL that is already public and live.
AUDIO_URL="https://example-stream.example.local/ember.mp3"

[ -n "$NEW" ] || { echo "usage: $0 <new-box-ip> [--cutover]"; exit 2; }

say() { echo -e "\n=== $* ==="; }

say "1. checking the source box has what we expect"
for f in "$SRC/youtube_stream.py" "$SRC/pngs"; do
    [ -e "$f" ] || { echo "MISSING: $f"; exit 1; }
done
echo "payload: $(du -sh $SRC/pngs | cut -f1) of images + youtube_stream.py"

say "2. checking the new box is reachable and the audio stream is up"
ssh -o BatchMode=yes -o ConnectTimeout=10 "root@$NEW" true \
    || { echo "cannot ssh to root@$NEW - copy your key over first"; exit 1; }
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 -r 0-1024 "$AUDIO_URL" || true)
# 200 or 206; icecast may also answer 200 and stream forever, hence the range.
case "$code" in 200|206) echo "audio stream OK ($code)";;
  *) echo "AUDIO STREAM NOT REACHABLE ($code) at $AUDIO_URL - stopping."; exit 1;; esac

say "3. installing packages on the new box"
ssh "root@$NEW" 'apt-get update -qq && apt-get install -y -qq ffmpeg python3-venv python3-pip curl \
&& (id ravenmap >/dev/null 2>&1 || useradd -m -s /bin/bash ravenmap) && echo packages ok'

say "4. copying the encoder"
# ⚠️ THE STREAM KEY IS INSIDE youtube_stream.py. It goes over ssh directly and
# must never be committed, pasted, or written anywhere a repo can reach.
ssh "root@$NEW" 'mkdir -p /srv/ravenmap/streamer/logs && chown -R ravenmap:ravenmap /srv/ravenmap/streamer'
tar -C "$SRC" -cf - youtube_stream.py pngs \
| ssh "root@$NEW" 'tar -C /srv/ravenmap/streamer -xf - && chown -R ravenmap:ravenmap /srv/ravenmap/streamer'
[ -f "$SRC/requirements.txt" ] && scp "$SRC/requirements.txt" "root@$NEW:/srv/ravenmap/streamer/" || true

say "5. python env on the new box"
ssh "root@$NEW" 'cd /srv/ravenmap/streamer && sudo -u ravenmap python3 -m venv .venv \
&& sudo -u ravenmap .venv/bin/pip install -q --upgrade pip \
&& { [ -f requirements.txt ] && sudo -u ravenmap .venv/bin/pip install -q -r requirements.txt || true; } \
    && echo venv ok'

say "6. pointing the encoder at the public stream"
# Set as an environment variable in the unit rather than editing the script, so
# the file stays byte-identical to the one on the old box and a rollback is a
# straight copy back rather than a reverse-edit.
ssh "root@$NEW" "cat > /etc/systemd/system/ravenmap-stream.service <<'UNIT'
[Unit]
Description=Project audio -> YouTube 24/7 stream (libx264, 720p24)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ravenmap
WorkingDirectory=/srv/ravenmap/streamer
Environment=EMBER_BASE=/srv/ravenmap/streamer
# Reads audio from the public stream; the upstream remains elsewhere now.
Environment=EMBER_STREAM_URL=$AUDIO_URL
ExecStart=/srv/ravenmap/streamer/.venv/bin/python /srv/ravenmap/streamer/youtube_stream.py
Restart=always
RestartSec=10
# No Nice= here. On the old box it yielded to the service; on this box the
# encoder IS the workload and there is nothing to yield to.
StandardOutput=append:/srv/ravenmap/streamer/logs/youtube.log
StandardError=append:/srv/ravenmap/streamer/logs/youtube.log

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload && echo unit installed"

say "7. checking youtube_stream.py will honour EMBER_STREAM_URL"
if grep -q 'EMBER_STREAM_URL' "$SRC/youtube_stream.py"; then
    echo "ok - the script reads EMBER_STREAM_URL"
else
    cat <<'WARN'
⚠️  youtube_stream.py does NOT read EMBER_STREAM_URL - it has the localhost URL
    hard-coded. Before cutover, change its input from
        http://localhost:8000/ember.mp3
    to read os.environ.get("EMBER_STREAM_URL", "http://localhost:8000/ember.mp3")
    ON THE NEW BOX ONLY. Left for a human deliberately: this script holds the
    stream key and is not something to sed blindly at 3am.
WARN
fi

if [ "$CUTOVER" != "--cutover" ]; then
    say "PREPARED, NOT CUT OVER"
    cat <<EOF
Nothing live has changed. The old encoder is still streaming.

When you are ready:
    bash $0 $NEW --cutover

Rollback at any point:
    ssh root@$NEW systemctl stop project-stream
    systemctl start project-stream      # on this box
EOF
    exit 0
fi

say "8. CUTOVER - stopping the old encoder"
systemctl stop project-stream
echo "old encoder stopped at $(date -Is)"

say "9. starting the new encoder"
ssh "root@$NEW" 'systemctl enable --now project-stream && sleep 8 && systemctl is-active project-stream'

say "10. did it actually take?"
sleep 20
ssh "root@$NEW" 'echo "--- last log lines ---"; tail -15 /srv/ravenmap/streamer/logs/youtube.log; \
    echo "--- encoder cpu ---"; ps -eo %cpu,etimes,comm --sort=-%cpu | grep ffmpeg | head -3'
cat <<EOF

Now CONFIRM THE CHANNEL IS ACTUALLY LIVE on YouTube before walking away. ffmpeg
running is not the same as YouTube receiving - a bad key fails at the far end
and looks fine here.

If it is not live:
    ssh root@$NEW systemctl stop project-stream
    systemctl start project-stream

Once it IS live, retire the old one so a reboot cannot start two encoders on one
key:
    systemctl disable project-stream      # on this box

Then watch SparrowMap recover the core:
    curl -s https://map.sparrowmap.com/api/health
EOF
