#!/bin/bash
# Daily collection, build and QA pass for the cad-etf-holdings dataset.
#
# Invoked by ops/launchd/com.etfh.collect.plist (see ops/install_launchd.sh)
# or by hand. Uses the repository this script lives in; no hard-coded paths.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PATH="/opt/homebrew/bin:/usr/local/bin:${PATH}"

cd "$REPO"
echo "etfh: collect start $(date -u +%Y-%m-%dT%H:%M:%SZ)"
uv run etfh collect --date today --raw "$REPO/raw"
uv run etfh build --raw "$REPO/raw" --out "$REPO/build"
uv run etfh qa --db "$REPO/build/etfh.duckdb"
echo "etfh: collect done $(date -u +%Y-%m-%dT%H:%M:%SZ)"
