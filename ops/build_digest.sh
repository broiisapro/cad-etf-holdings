#!/bin/bash
# Build the weekly digest and render it as the static GitHub Pages site.
#
# Usage: ops/build_digest.sh MODE DATA_CHECKOUT OUT_DIR
#
#   MODE           "demo" builds the committed synthetic store (no data
#                  checkout) and publishes one week; "real" rebuilds the
#                  dataset from the private raw store and publishes eight weeks
#                  with the public publication policy (D79).
#   DATA_CHECKOUT  checkout of the private data repository (its raw store is
#                  DATA_CHECKOUT/raw). Pass "-" for demo mode.
#   OUT_DIR        directory that receives the rendered site. Created when it
#                  does not exist; index.html must be there when the script ends.
#
# GitHub Pages runs this on a scheduled workflow (.github/workflows/pages.yml,
# disabled until ETFH_PAGES is "true"). The real mode additionally guards
# against leaking the data checkout path into a published file (D9/D77): raw
# data never appears in the public repository or its Pages site.
set -euo pipefail

usage() {
    echo "usage: ops/build_digest.sh MODE DATA_CHECKOUT OUT_DIR" >&2
    echo "  MODE is 'demo' (synthetic store) or 'real' (private data checkout)" >&2
    exit 2
}

if [ "$#" -ne 3 ]; then
    usage
fi

MODE="$1"
DATA="$2"
OUT="$3"

case "$MODE" in
    demo | real) ;;
    *) usage ;;
esac

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

if [ "$MODE" = "real" ] && [ ! -d "$DATA/raw" ]; then
    echo "etfh: error: $DATA/raw is not a directory (real mode needs the data checkout)" >&2
    exit 2
fi

TMP="$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/etfh-digest.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT

mkdir -p "$OUT"

echo "etfh: digest start mode=$MODE $(date -u +%Y-%m-%dT%H:%M:%SZ)"
case "$MODE" in
    demo)
        uv run etfh weekly --demo --out "$OUT" --weeks 1
        ;;
    real)
        uv run etfh build --raw "$DATA/raw" --out "$TMP/build"
        uv run etfh weekly --db "$TMP/build/etfh.duckdb" --out "$OUT" --weeks 8 --public
        ;;
esac

if [ ! -f "$OUT/index.html" ]; then
    echo "etfh: error: the digest did not write $OUT/index.html" >&2
    exit 1
fi

# Leak guard (D9/D77): no published file may contain the data checkout path.
# The absolute path is what a leaked report path would carry; the site itself
# only uses relative links such as data/<week>.json, so checking the absolute
# path cannot produce a false positive on its own navigation.
if [ "$MODE" = "real" ]; then
    DATA_ABS="$(cd "$DATA" && pwd -P)"
    if grep -r -I -l -F -e "$DATA_ABS" "$OUT" >/dev/null 2>&1; then
        echo "etfh: error: a published file contains the data checkout path $DATA_ABS" >&2
        exit 1
    fi
fi

echo "etfh: digest done mode=$MODE out=$OUT $(date -u +%Y-%m-%dT%H:%M:%SZ)"
