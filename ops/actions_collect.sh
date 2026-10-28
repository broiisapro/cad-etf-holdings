#!/bin/bash
# Daily collection for the scheduled GitHub Actions workflow (FR-2.3).
#
# Usage: ops/actions_collect.sh DATA_CHECKOUT [DATE]
#
#   DATA_CHECKOUT  a checkout of the private data repository
#                  (broiisapro/cad-etf-holdings-data by default); raw files and
#                  their sidecars live under <DATA_CHECKOUT>/raw and are
#                  committed and pushed there, never to this repository (D77).
#   DATE           YYYY-MM-DD; defaults to today in America/Toronto.
#
# Collects holdings and daily fund metrics, verifies the raw store, pushes the
# new files, then rebuilds the dataset from that store and reports QA. The push
# happens before the build so a QA failure never loses collected files. The QA
# markdown report is appended to $GITHUB_STEP_SUMMARY when the runner sets it;
# derived aggregates go to $RUNNER_TEMP/aggregates for the workflow artifact.
#
# Requires ETFH_USER_AGENT, ETFH_GIT_NAME and ETFH_GIT_EMAIL (repository
# variables in the workflow; identity is passed with `git -c`, so no git config
# is ever changed and D14 holds). Written for Linux and macOS: no GNU-only
# options, no `readlink -f`, no `date -d`.
set -euo pipefail

usage() {
    echo "usage: $0 DATA_CHECKOUT [DATE]" >&2
    exit 2
}

if [ "$#" -lt 1 ] || [ "$#" -gt 2 ]; then
    usage
fi

fail() {
    echo "etfh: error: $*" >&2
    exit 2
}

DATA_ARG="$1"
DATE="${2:-}"

if [ ! -d "$DATA_ARG" ]; then
    fail "data checkout not found: $DATA_ARG"
fi
DATA="$(cd "$DATA_ARG" && pwd)"

if [ -z "$DATE" ]; then
    DATE="$(TZ=America/Toronto date +%Y-%m-%d)"
fi
case "$DATE" in
    [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]) ;;
    *) fail "DATE must be YYYY-MM-DD, got '$DATE'" ;;
esac

# Fail before collecting: the workflow checks the same names up front, this
# keeps the script safe when run by hand. Only the names are printed.
missing=()
for name in ETFH_USER_AGENT ETFH_GIT_NAME ETFH_GIT_EMAIL; do
    if [ -z "${!name:-}" ]; then
        missing+=("$name")
    fi
done
if [ "${#missing[@]}" -gt 0 ]; then
    fail "missing required environment variable(s): ${missing[*]}"
fi

if ! command -v uv >/dev/null 2>&1; then
    fail "uv is not on PATH; install uv (the workflow does this) or add it to PATH"
fi

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

# RUNNER_TEMP is set by GitHub Actions; the fallback keeps a hand-run usable
# without writing build output into the checkout.
RUNNER_TEMP="${RUNNER_TEMP:-${TMPDIR:-/tmp}/etfh-actions}"
BUILD_DIR="$RUNNER_TEMP/build"
AGGREGATES_DIR="$RUNNER_TEMP/aggregates"
mkdir -p "$BUILD_DIR" "$AGGREGATES_DIR"
RAW="$DATA/raw"
echo "etfh: preflight ok $DATE data=$DATA"
