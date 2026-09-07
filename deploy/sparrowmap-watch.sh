#!/bin/bash
# Watch the hub's own health endpoint and act before a person notices.
#
# 🚨 WRITTEN AFTER A TWO-HOUR OUTAGE THAT NOTHING WAS WATCHING.
# The box ran a backup cron and a purge cron and nothing else. The failure
# signal existed the entire time - 105,768 log lines in four days - and there
# was no reader. The gap was never the data.
#
# Design notes, each one paid for:
#
#  * A TIMER, NOT A DAEMON. A long-running watchdog can hang silently and then
#    it is one more thing that needs watching. Each run here is bounded by
#    systemd (TimeoutStartSec) and either finishes or is killed.
#
#  * TWO STRIKES BEFORE ACTING. One failed poll is a network blip or a restart
#    in progress. Restarting on a single sample makes the watchdog the thing
#    that causes outages.
#
#  * A FLAP GUARD. If restarting does not fix it, restarting again will not
#    either, and a service that bounces every two minutes destroys the evidence
#    needed to diagnose it. After MAX_RESTARTS in an hour it stops acting and
#    keeps shouting.
#
#  * IT LOGS THE NUMBERS EVERY RUN, not just the failures. The descriptor
#    exhaustion that caused the outage was a 45-minute RAMP. Only recording
#    incidents would have captured the cliff and none of the slope.

set -uo pipefail

URL="${RAVEN_HEALTH_URL:-${SPARROW_HEALTH_URL:-}}"
SERVICE="sparrowmap.service"
STATE="/var/lib/sparrowmap-watch"
LOG="/opt/sparrowmap/logs/watch.log"
MAX_RESTARTS=3          # per hour
WARN_PCT=80             # descriptor headroom worth shouting about

if [ -z "$URL" ]; then
    echo "No RavenMap health URL configured. Set RAVEN_HEALTH_URL "\
"(legacy SPARROW_HEALTH_URL is also accepted)." >&2
    exit 1
fi

mkdir -p "$STATE" "$(dirname "$LOG")"
FAILS="$STATE/consecutive_fails"
[ -f "$FAILS" ] || echo 0 > "$FAILS"

say() { echo "$(date -Is) $*" >> "$LOG"; }

# --- reaching a human -------------------------------------------------------
#
# 🚨 THE LAYER THAT WAS MISSING. Restarting is not telling anyone. This watch
# can restart the service three times and give up, and until now that produced
# a line in a log file nobody was reading - the same failure, one level up, as
# the outage that caused this script to exist.
#
# It opens a GitHub issue, which does two jobs at once: it notifies Matthew on
# his phone, and it is an event a Claude routine can be triggered by, so the
# give-up can wake something that reasons instead of something that restarts.
#
# ⚠️ THE BODY IS DELIBERATELY THIN, AND THE REPO IS A CHOICE.
# ALERT_REPO must be explicitly configured, so this fork cannot accidentally
# file issues against an upstream repository. Counts and timestamps only -
# never an address, never a coordinate, never a token. /api/health already
# publishes these same numbers, so this leaks nothing new.
ALERT_REPO="${RAVEN_ALERT_REPO:-${SPARROW_ALERT_REPO:-}}"
TOKEN_FILE="${RAVEN_GH_TOKEN_FILE:-${SPARROW_GH_TOKEN_FILE:-/etc/sparrowmap/github_token}}"

alert_human() {
    local status="$1"
    if [ -z "$ALERT_REPO" ]; then
        say "CANNOT ALERT: no alert repository configured; set RAVEN_ALERT_REPO "\
"(legacy SPARROW_ALERT_REPO is also accepted)."
        return 1
    fi
    if [ ! -r "$TOKEN_FILE" ]; then
        say "CANNOT ALERT: no token at $TOKEN_FILE, so nobody is being told. "\
"Create a fine-grained PAT with Issues:write on $ALERT_REPO and put it there, chmod 600."
        return 1
    fi
    # One issue per incident, not one per tick. A watch that files an issue
    # every two minutes during an outage is a watch that gets muted, and a
    # muted alarm is worse than none.
    local stamp_file="$STATE/last_alert"
    local now_s last_s
    now_s=$(date +%s)
    last_s=$(cat "$stamp_file" 2>/dev/null || echo 0)
    if [ $(( now_s - last_s )) -lt 3600 ]; then
        say "alert suppressed: one was already filed within the hour"
        return 0
    fi
    echo "$now_s" > "$stamp_file"

    local tail_lines body
    tail_lines=$(tail -6 "$LOG" | sed 's/"/'"'"'/g')
    body="The health watch restarted \`$SERVICE\` $MAX_RESTARTS times within an hour and it is still failing, so it has stopped restarting - continuing would only destroy the evidence.

**Last poll:** \`$status\`

**Recent watch log (counts only):**
\`\`\`
$tail_lines
\`\`\`

Live numbers: $URL

Filed automatically by \`deploy/sparrowmap-watch.sh\`. See the memory note on descriptor exhaustion and congestion collapse before assuming the database is broken - that error usually means the process is out of file descriptors, not that the file is bad."

    # ⚠️ THE BODY GOES THROUGH THE ENVIRONMENT, NOT INTO THE SOURCE.
    # Interpolating it into the heredoc would splice log text into Python source,
    # where one stray quote or backslash turns an outage alert into a
    # SyntaxError - the alarm failing silently at exactly the moment it is
    # needed. The token is read from a file rather than passed as an argument so
    # it never appears in the process list.
    if ALERT_BODY="$body" ALERT_REPO="$ALERT_REPO" TOKEN_FILE="$TOKEN_FILE" \
       python3 - <<'PYEOF'
import json, os, sys, urllib.request, urllib.error
repo = os.environ["ALERT_REPO"]
# 🚨 utf-8-sig, NOT utf-8, AND strip() - BOTH ARE LOAD-BEARING.
# The token gets put here by a human, and if it is written from PowerShell it
# arrives with a UTF-8 BOM and CRLF: 45 bytes for a 40-character token. strip()
# removes the CRLF and leaves the BOM, which then goes into an Authorization
# header, and http.client raises on the invalid header value - an unhandled
# traceback rather than the clean "HTTP 401" this code was ready to report.
# The alarm failing with a stack trace is the failure mode this whole path
# exists to prevent, so it tolerates however the file was written.
tok = open(os.environ["TOKEN_FILE"], encoding="utf-8-sig").read().strip()
if not tok:
    print("token file is empty", file=sys.stderr)
    sys.exit(1)
payload = json.dumps({
    "title": "SparrowMap: health watch gave up after repeated restarts",
    "body": os.environ["ALERT_BODY"],
    "labels": ["outage"],
}).encode()
req = urllib.request.Request(
    f"https://api.github.com/repos/{repo}/issues", data=payload, method="POST",
    headers={"Authorization": f"Bearer {tok}",
             "Accept": "application/vnd.github+json",
             "X-GitHub-Api-Version": "2022-11-28",
             "User-Agent": "sparrowmap-watch"})
try:
    with urllib.request.urlopen(req, timeout=20) as r:
        print(json.load(r).get("html_url", "filed"))
except urllib.error.HTTPError as e:
    print(f"HTTP {e.code}: {e.read().decode()[:200]}", file=sys.stderr)
    sys.exit(1)
PYEOF
    then
        say "ALERT FILED on $ALERT_REPO"
    else
        # ⚠️ CAPTURE WHY. "ALERT FAILED" with no reason sends whoever reads this
        # log hunting, at the one moment they are already busy with an outage.
        say "ALERT FAILED - could not file an issue on $ALERT_REPO (see stderr above)"
    fi
}

# 🚨 THE ALERT PATH HAS TO BE TESTABLE ON DEMAND.
# It only ever runs when everything else has already failed, which is the worst
# possible time to discover a typo in it. `--test-alert` exercises it for real
# without pretending the site is down, and bypasses the once-an-hour dedupe so
# a test is never silently swallowed.
#
#     sudo /opt/sparrowmap/deploy/sparrowmap-watch.sh --test-alert
#
if [ "${1:-}" = "--test-alert" ]; then
    rm -f "$STATE/last_alert"
    say "TEST: exercising the alert path deliberately (not a real outage)"
    alert_human "TEST - this is a drill, the site is fine"
    echo "--- last 3 log lines ---"
    tail -3 "$LOG"
    exit 0
fi

BODY=$(curl -fsS --max-time 10 "$URL" 2>/dev/null)
CURL_RC=$?

# jq is not assumed - this box has no reason to carry it for four fields.
field() { echo "$BODY" | grep -o "\"$1\": *[^,}]*" | head -1 | sed 's/.*: *//;s/"//g'; }

if [ $CURL_RC -ne 0 ]; then
    STATUS="unreachable(curl=$CURL_RC)"
    HEALTHY=0
else
    OK=$(field ok); PCT=$(field fd_used_pct); THR=$(field threads); DB=$(field db)
    STATUS="ok=$OK db=$DB fd=${PCT}% threads=$THR"
    [ "$OK" = "true" ] && HEALTHY=1 || HEALTHY=0
    # Shout about the ramp even while everything still answers. This is the
    # whole point: the cliff is not the first thing that happens.
    if [ -n "${PCT:-}" ] && [ "${PCT%%.*}" -ge "$WARN_PCT" ] 2>/dev/null; then
        say "WARN  descriptors at ${PCT}% of the limit - $STATUS"
    fi
fi

if [ "$HEALTHY" = "1" ]; then
    [ "$(cat "$FAILS")" != "0" ] && say "recovered - $STATUS"
    echo 0 > "$FAILS"
    say "ok    $STATUS"
    exit 0
fi

N=$(( $(cat "$FAILS") + 1 ))
echo "$N" > "$FAILS"
say "FAIL  ($N) $STATUS"

# One bad sample is a blip. Two is a problem.
[ "$N" -lt 2 ] && exit 0

# Flap guard: count restarts in the last hour.
HOUR=$(date +%Y%m%d%H)
RC_FILE="$STATE/restarts_$HOUR"
[ -f "$RC_FILE" ] || echo 0 > "$RC_FILE"
RC=$(cat "$RC_FILE")
if [ "$RC" -ge "$MAX_RESTARTS" ]; then
    say "GIVING UP restarting: $RC restarts this hour already and it is still "\
"failing. This needs a human - restarting again only destroys the evidence."
    alert_human "$STATUS"
    exit 1
fi

# Drop previous hours' counters first, then record this one, so the directory
# does not grow a file per hour for ever.
find "$STATE" -name 'restarts_*' ! -name "restarts_$HOUR" -delete 2>/dev/null
echo $(( RC + 1 )) > "$RC_FILE"
say "RESTARTING $SERVICE (attempt $(( RC + 1 )) this hour) - $STATUS"
systemctl restart "$SERVICE"
sleep 5
AFTER=$(curl -fsS --max-time 10 "$URL" 2>/dev/null)
if echo "$AFTER" | grep -q '"ok": *true'; then
    say "restart fixed it - $AFTER"
    echo 0 > "$FAILS"
else
    say "restart did NOT fix it - $AFTER"
fi
