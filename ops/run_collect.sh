#!/bin/bash
# Daily collection, build and QA pass for the cad-etf-holdings dataset.
#
# Invoked by ops/launchd/com.etfh.collect.plist (see ops/install_launchd.sh)
# or by hand. Uses the repository this script lives in; no hard-coded paths.
#
# Loads <repo>/.env when present so launchd (which starts with a minimal
# environment) sees ETFH_USER_AGENT and the optional OPENFIGI_API_KEY. Values
# are never echoed. Adds the usual uv install locations to PATH and fails with
# a clear message when uv is still missing, so a scheduled run cannot die with
# a bare "command not found". The QA exit status is the script's exit status,
# so the launchd job fails exactly when QA fails.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# launchd starts jobs with a minimal PATH, so add the directories uv commonly
# lives in (uv's installer, cargo, Homebrew on Apple Silicon and Intel).
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:/opt/homebrew/bin:/usr/local/bin:${PATH}"

cd "$REPO"

if ! command -v uv >/dev/null 2>&1; then
    echo "etfh: error: uv is not on PATH (looked in \$HOME/.local/bin, \$HOME/.cargo/bin, /opt/homebrew/bin, /usr/local/bin and the inherited PATH); install uv or add its directory to PATH" >&2
    exit 2
fi

if [ -f "$REPO/.env" ]; then
    set -a
    . "$REPO/.env"
    set +a
fi
if [ -z "${ETFH_USER_AGENT:-}" ]; then
    echo "etfh: error: ETFH_USER_AGENT is not set; add it to $REPO/.env (see .env.example)" >&2
    exit 2
fi

echo "etfh: collect start $(date -u +%Y-%m-%dT%H:%M:%SZ)"
uv run etfh collect --date today --raw "$REPO/raw" || {
    echo "etfh: warning: collect exited $?; continuing to the build" >&2
}

BUILD_ARGS=(--raw "$REPO/raw" --out "$REPO/build")
if [ "${ETFH_OPENFIGI:-0}" = "1" ]; then
    BUILD_ARGS+=(--openfigi)
fi
uv run etfh build "${BUILD_ARGS[@]}" || {
    echo "etfh: warning: build exited $?; continuing to QA" >&2
}

QA_STATUS=0
uv run etfh qa --db "$REPO/build/etfh.duckdb" || QA_STATUS=$?
echo "etfh: collect done $(date -u +%Y-%m-%dT%H:%M:%SZ) qa_exit=$QA_STATUS"
exit "$QA_STATUS"
