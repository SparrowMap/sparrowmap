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

URL="${SPARROW_HEALTH_URL:-https://map.sparrowmap.com/api/health}"
SERVICE="sparrowmap.service"
STATE="/var/lib/sparrowmap-watch"
#: Every incident, append-only. See alert_human: this is what is still
#: readable when every notification channel is broken.
INCIDENTS="${SPARROW_INCIDENTS:-/opt/sparrowmap/logs/incidents.jsonl}"
LOG="/opt/sparrowmap/logs/watch.log"
MAX_RESTARTS=3          # per hour
WARN_PCT=80             # descriptor headroom worth shouting about

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
# ALERT_REPO defaults to the public code repo, so anything written here is
# public and permanent. Counts and timestamps only - never an address, never a
# coordinate, never a token. /api/health already publishes these same numbers,
# so this leaks nothing new; a detailed post-mortem does not belong here.
# 📌 If you would rather not announce outages in public at all, point
# ALERT_REPO at a private repo - that is the only change needed.
ALERT_REPO="${SPARROW_ALERT_REPO:-SparrowMap/sparrowmap}"
TOKEN_FILE="${SPARROW_GH_TOKEN_FILE:-/etc/sparrowmap/github_token}"

alert_human() {
    local status="$1"
    # 🚨 AN INCIDENT IS RECORDED WHETHER OR NOT ANYBODY CAN BE TOLD.
    #
    # The bug-report alert on this project died the day the map moved boxes
    # and nobody noticed for a MONTH - twelve reports, one of them from a
    # journalist, sat unread because the only notification path had quietly
    # stopped working and its own failure went nowhere. An alarm whose
    # failure is silent is not an alarm.
    #
    # So the incident lands in a file FIRST, always, before any network call
    # that can fail. It needs no token, no network and no third party to be
    # readable afterwards.
    mkdir -p "$(dirname "$INCIDENTS")"
    printf '{"ts":"%s","status":"%s"}\n' "$(date -Is)" "$status" >> "$INCIDENTS"
    logger -t sparrowmap-watch "INCIDENT: $status" 2>/dev/null || true
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

# 🚨 PROVE THE ALARM STILL WORKS, ON A SCHEDULE, BECAUSE THIS ONE ROTTED.
# The GitHub token that both this watch and the bug portal alert through is
# a fine-grained PAT, and those EXPIRE. On 2026-09-24 it was found returning
# "401 Bad credentials", which meant every outage alert AND every bug report
# for weeks had gone nowhere. A credential that expires silently needs
# something that checks it on purpose, so once an hour this asks GitHub
# whether the token still works and says so loudly when it does not.
if [ "$(date +%M)" -lt 2 ] && [ -r "$TOKEN_FILE" ]; then
    TOK=$(tr -d '\r\n' < "$TOKEN_FILE" | sed 's/^\xef\xbb\xbf//')
    CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 \
           -H "Authorization: Bearer $TOK" \
           "https://api.github.com/repos/$ALERT_REPO" 2>/dev/null)
    if [ "$CODE" != "200" ]; then
        say "ALARM IS DEAD: GitHub rejected the token ($CODE). Outage alerts and bug reports go NOWHERE until it is replaced. Incidents are still recorded in $INCIDENTS."
        logger -t sparrowmap-watch "ALARM DEAD: github token -> $CODE" 2>/dev/null || true
    fi
fi

BODY=$(curl -fsS --max-time 10 "$URL" 2>/dev/null)
CURL_RC=$?

# jq is not assumed - this box has no reason to carry it for four fields.
field() { echo "$BODY" | grep -o "\"$1\": *[^,}]*" | head -1 | sed 's/.*: *//;s/"//g'; }

# 🚨 ASK THE QUESTION A VISITOR ASKS, NOT THE ONE THE PROCESS ANSWERS.
#
# On 2026-09-24 the map was returning 503 to every viewer for the better part
# of an hour while /api/health said `"ok": true, "db": "ok"`, descriptors at
# 1.3% and nothing else out of range. Every field this watch looked at was
# green. `ok` means "the handler ran and the database answered a trivial
# query" - it cannot mean "the site works", because the request that was
# failing never got far enough to be counted by it.
#
# So the watch now FETCHES THE MAP'S OWN DATA, the same URL a phone asks for,
# and times it. That is the only check that could have caught any of the three
# root causes found that day, and it costs one request every two minutes.
PROBE_URL="${SPARROW_PROBE_URL:-https://map.sparrowmap.com/api/sightings?since=0&vclass=public&limit=5000}"
PROBE_MAX_S="${SPARROW_PROBE_MAX_S:-20}"     # a phone gives up at 12s on the poll
PROBE=$(curl -fsS -o /dev/null -w '%{http_code} %{time_total}'         --max-time "$PROBE_MAX_S" "$PROBE_URL" 2>/dev/null)
PROBE_RC=$?
PROBE_CODE="${PROBE%% *}"; PROBE_T="${PROBE##* }"
[ -z "$PROBE_CODE" ] && PROBE_CODE="timeout"

if [ $CURL_RC -ne 0 ]; then
    STATUS="unreachable(curl=$CURL_RC)"
    HEALTHY=0
else
    OK=$(field ok); PCT=$(field fd_used_pct); THR=$(field threads); DB=$(field db)
    HFREE=$(field heavy_free); WAL=$(field wal_mb)
    STATUS="ok=$OK db=$DB fd=${PCT}% threads=$THR heavy_free=$HFREE wal=${WAL}MB map=$PROBE_CODE/${PROBE_T}s"
    [ "$OK" = "true" ] && HEALTHY=1 || HEALTHY=0

    # The map itself failing outranks anything the process says about itself.
    if [ $PROBE_RC -ne 0 ] || [ "$PROBE_CODE" != "200" ]; then
        say "MAP DATA FAILING ($PROBE_CODE) while health says ok=$OK - $STATUS"
        HEALTHY=0
    fi

    # 🚨 THE RAMP, NOT THE CLIFF. Each of these was measured on the way into
    # the 09-24 outage and each was visible long before anyone complained:
    #   heavy_free 0   every permit for map data taken; the next reader 503s
    #   wal > 500 MB   readers start blocking on the log's index
    # Warned about rather than acted on, because a warning with the numbers
    # beside it is what lets the next person skip the diagnosis entirely.
    if [ "${HFREE:-99}" = "0" ]; then
        say "WARN  heavy_free=0 - every map-data permit is taken, readers are queueing - $STATUS"
    fi
    if [ -n "${WAL:-}" ] && [ "${WAL%%.*}" -ge 500 ] 2>/dev/null; then
        say "WARN  write-ahead log at ${WAL}MB - checkpointer may have stopped - $STATUS"
    fi
    if [ -n "${PCT:-}" ] && [ "${PCT%%.*}" -ge "$WARN_PCT" ] 2>/dev/null; then
        say "WARN  descriptors at ${PCT}% of the limit - $STATUS"
    fi
fi

# 🚨 PHOTOGRAPH IT BEFORE RESTARTING. A restart is the cure AND the thing that
# destroys the evidence, and every wrong diagnosis on this box came from
# looking after the fact. If the hub is unhealthy and py-spy is available, dump
# every thread first - it costs a second and it is the difference between
# "reconnecting again" and knowing which line 47 threads are sitting on.
capture_threads() {
    local py="/opt/sparrowmap/.venv/bin/py-spy"
    local pid out
    [ -x "$py" ] || return 0
    pid=$(systemctl show -p MainPID --value "$SERVICE" 2>/dev/null)
    [ -n "$pid" ] && [ "$pid" != "0" ] || return 0
    mkdir -p /opt/sparrowmap/logs/stalls
    out="/opt/sparrowmap/logs/stalls/$(date +%Y%m%d-%H%M%S).txt"
    { echo "$STATUS"; echo "probe: $PROBE_URL"; echo "===="; 
      timeout 25 "$py" dump --pid "$pid" --locals 2>&1; } > "$out"
    say "captured thread dump -> $out"
}

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
    capture_threads
    alert_human "$STATUS"
    exit 1
fi

# Drop previous hours' counters first, then record this one, so the directory
# does not grow a file per hour for ever.
find "$STATE" -name 'restarts_*' ! -name "restarts_$HOUR" -delete 2>/dev/null
echo $(( RC + 1 )) > "$RC_FILE"
say "RESTARTING $SERVICE (attempt $(( RC + 1 )) this hour) - $STATUS"
capture_threads
systemctl restart "$SERVICE"
sleep 5
AFTER=$(curl -fsS --max-time 10 "$URL" 2>/dev/null)
if echo "$AFTER" | grep -q '"ok": *true'; then
    say "restart fixed it - $AFTER"
    echo 0 > "$FAILS"
else
    say "restart did NOT fix it - $AFTER"
fi
