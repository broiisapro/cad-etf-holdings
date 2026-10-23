#!/bin/bash
# Sleep-proof daily collection guard for cad-etf-holdings (D64).
#
# launchd runs this every 30 minutes and at login. It starts
# ops/run_collect.sh at most once per local calendar day: the first chance at
# or after 18:00 local time, or immediately when the previous day was missed.
# A calendar-only trigger proved unreliable on a laptop (sleep, restarts), so
# the schedule is a poll and this guard is the "once a day" decision.
#
# The day is stamped only when the run reported at least one source/fund
# fetched or confirmed unchanged (a line starting "fetched " or "unchanged ").
# A network outage therefore leaves the day open and the next 30-minute run
# retries. The guard always exits 0 so launchd does not mark the job as failed
# for a collection problem; the run's own output and the guard log carry the
# details.
#
# State: <repo>/.cache/etfh/last-collect-date
# Log:   ${ETFH_LOG_DIR:-$HOME/Library/Logs/etfh}/guard.log
#
# ETFH_GUARD_NOW="YYYY-MM-DD HH:MM" replaces the wall clock. It exists for the
# hermetic tests in tests/test_ops.py; production runs never set it.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE_DIR="$REPO/.cache/etfh"
STAMP="$STATE_DIR/last-collect-date"
LOG_DIR="${ETFH_LOG_DIR:-$HOME/Library/Logs/etfh}"
LOG="$LOG_DIR/guard.log"
START_HOUR=18

if [ -n "${ETFH_GUARD_NOW:-}" ]; then
    NOW="$ETFH_GUARD_NOW"
else
    NOW="$(date '+%Y-%m-%d %H:%M')"
fi
TODAY="${NOW%% *}"
CLOCK="${NOW#* }"
HOUR="${CLOCK%%:*}"
case "$TODAY" in
    [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]) ;;
    *)
        echo "etfh-guard: error: ETFH_GUARD_NOW must be 'YYYY-MM-DD HH:MM', got '$NOW'" >&2
        exit 2
        ;;
esac
case "$HOUR" in
    '' | *[!0-9]*)
        echo "etfh-guard: error: ETFH_GUARD_NOW has no numeric hour, got '$NOW'" >&2
        exit 2
        ;;
esac
HOUR=$((10#$HOUR))

# BSD date (macOS) speaks -j -f; GNU date falls back to -d.
yesterday_of() {
    local day="$1"
    if date -j -f "%Y-%m-%d" "$day" "+%Y-%m-%d" >/dev/null 2>&1; then
        date -j -v-1d -f "%Y-%m-%d" "$day" "+%Y-%m-%d"
    else
        date -d "$day -1 day" "+%Y-%m-%d"
    fi
}
YESTERDAY="$(yesterday_of "$TODAY")"
LAST="$(cat "$STAMP" 2>/dev/null || echo none)"

mkdir -p "$LOG_DIR"
log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $*" >>"$LOG"; }

if [ "$LAST" = "$TODAY" ]; then
    exit 0
fi
if [ "$LAST" = "$YESTERDAY" ] && [ "$HOUR" -lt "$START_HOUR" ]; then
    exit 0
fi

log "starting collection (last=$LAST, now=$NOW)"
OUTPUT="$(mktemp "${TMPDIR:-/tmp}/etfh-guard.XXXXXX")"
trap 'rm -f "$OUTPUT"' EXIT
rc=0
bash "$REPO/ops/run_collect.sh" >"$OUTPUT" 2>&1 || rc=$?
cat "$OUTPUT"
count="$(grep -cE '^(fetched|unchanged) ' "$OUTPUT" || true)"
mkdir -p "$STATE_DIR"
if [ "${count:-0}" -gt 0 ]; then
    printf '%s\n' "$TODAY" >"$STAMP"
    log "done rc=$rc; $count source/fund pair(s) fetched or unchanged; stamped $TODAY"
else
    log "done rc=$rc; nothing fetched (network down?), not stamped; retry in 30 min"
fi
exit 0
