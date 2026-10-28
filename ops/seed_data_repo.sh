#!/bin/bash
# Seed or refresh the private data repository (D77) from a local raw store.
#
# Usage: ops/seed_data_repo.sh RAW_DIR DATA_CHECKOUT
#
# Verifies RAW_DIR against its sidecars, copies file/sidecar pairs into
# DATA_CHECKOUT/raw without overwriting different bytes, verifies the result,
# and commits the checkout when anything changed. Commits use the identity the
# data checkout already has; this script never changes git configuration. It
# does not push: the human decides when the data leaves the machine.
#
# The data checkout is a separate clone of the private data repository
# (ETFH_DATA_REPO, default broiisapro/cad-etf-holdings-data) whose raw/ tree
# holds the same layout as the collector's raw store.
set -euo pipefail

usage() {
    echo "usage: $0 RAW_DIR DATA_CHECKOUT" >&2
}

if [ "$#" -ne 2 ]; then
    usage
    exit 2
fi

RAW_DIR="$1"
DATA_CHECKOUT="$2"

if [ ! -d "$RAW_DIR" ]; then
    echo "etfh: error: raw store not found: $RAW_DIR" >&2
    exit 2
fi
if [ ! -d "$DATA_CHECKOUT" ]; then
    echo "etfh: error: data checkout not found: $DATA_CHECKOUT" >&2
    exit 2
fi
if ! git -C "$DATA_CHECKOUT" rev-parse --git-dir >/dev/null 2>&1; then
    echo "etfh: error: $DATA_CHECKOUT is not a git checkout" >&2
    exit 2
fi

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# launchd and cron start jobs with a minimal PATH, so add the directories uv
# commonly lives in. ETFH_UV_DIRS replaces that list; an empty value skips the
# search, which lets a test run against a PATH it controls.
UV_DIRS="${ETFH_UV_DIRS-$HOME/.local/bin:$HOME/.cargo/bin:/opt/homebrew/bin:/usr/local/bin}"
if [ -n "$UV_DIRS" ]; then
    export PATH="$UV_DIRS:${PATH}"
fi

cd "$REPO"

if ! command -v uv >/dev/null 2>&1; then
    if [ -n "$UV_DIRS" ]; then
        echo "etfh: error: uv is not on PATH (looked in $UV_DIRS and the inherited PATH); install uv or add its directory to PATH" >&2
    else
        echo "etfh: error: uv is not on PATH; install uv or add its directory to PATH" >&2
    fi
    exit 2
fi

DEST_RAW="$DATA_CHECKOUT/raw"

uv run python -m etfh.rawcheck verify "$RAW_DIR"
uv run python -m etfh.rawcheck sync "$RAW_DIR" "$DEST_RAW"
uv run python -m etfh.rawcheck verify "$DEST_RAW"

DATA_FILES=$(find "$DEST_RAW" -type f ! -name '*.meta.json' | wc -l | tr -d ' ')
SIDECARS=$(find "$DEST_RAW" -type f -name '*.meta.json' | wc -l | tr -d ' ')
echo "etfh: raw store: $DATA_FILES data file(s), $SIDECARS sidecar(s)"

git -C "$DATA_CHECKOUT" add raw
if git -C "$DATA_CHECKOUT" diff --cached --quiet; then
    echo "etfh: data repository already up to date; nothing committed"
else
    CHANGED=$(git -C "$DATA_CHECKOUT" diff --cached --name-only | wc -l | tr -d ' ')
    git -C "$DATA_CHECKOUT" commit -m "data: seed raw store"
    echo "etfh: committed $CHANGED changed path(s) to the data repository"
fi
