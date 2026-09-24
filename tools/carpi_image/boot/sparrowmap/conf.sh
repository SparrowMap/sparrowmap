# Reads /boot/firmware/sparrowmap.txt. Sourced by setup.sh and dashcam.sh.
#
# 🚨 NEVER `source` THE SETTINGS FILE ITSELF. It is typed in Notepad by someone
# who may put a quote, a $ or a space in a Wi-Fi password, and sourcing it would
# run that as shell. Values are read as plain text, one key at a time.
# Notepad also saves CRLF and sometimes a BOM; both are stripped here, or the
# hotspot name would silently carry a \r and never match.

CONF=${CONF:-/boot/firmware/sparrowmap.txt}

conf() {                       # conf KEY [DEFAULT]
    local v
    v=$(sed -e '1s/^\xEF\xBB\xBF//' -e 's/\r$//' "$CONF" 2>/dev/null \
        | grep -m1 "^[[:space:]]*$1[[:space:]]*=" | cut -d= -f2-)
    v="${v#"${v%%[![:space:]]*}"}"          # trim leading space
    v="${v%"${v##*[![:space:]]}"}"          # trim trailing space
    # "My Hotspot" typed with quotes means My Hotspot
    if [ ${#v} -ge 2 ] && { [ "${v:0:1}${v: -1}" = '""' ] || [ "${v:0:1}${v: -1}" = "''" ]; }; then
        v="${v:1:${#v}-2}"
    fi
    [ -n "$v" ] && printf '%s' "$v" || printf '%s' "${2:-}"
}

is_yes() { case "$(conf "$1" "${2:-no}" | tr 'A-Z' 'a-z')" in y|yes|true|1|on) return 0;; esac; return 1; }

APP=/opt/sparrowmap
STATE=/var/lib/sparrowmap
HUB=$(conf hub https://map.sparrowmap.com)
HUB=${HUB%/}
