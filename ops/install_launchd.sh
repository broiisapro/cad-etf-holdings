#!/bin/bash
# Install the scheduled collection launchd job for cad-etf-holdings.
#
# Substitutes the __REPO__ and __HOME__ placeholders in
# ops/launchd/com.etfh.collect.plist, writes the result to
# ~/Library/LaunchAgents/com.etfh.collect.plist and prints the launchctl
# command to load it. launchd then runs ops/collect_guard.sh every 30 minutes
# and at login; the guard runs the collection at most once per local day
# (first chance after 18:00, or immediately when a day was missed, D64), so a
# sleeping or restarted laptop still collects. The job is never loaded
# automatically.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE="$REPO/ops/launchd/com.etfh.collect.plist"
TARGET_DIR="$HOME/Library/LaunchAgents"
TARGET="$TARGET_DIR/com.etfh.collect.plist"
LOG_DIR="$HOME/Library/Logs/etfh"

mkdir -p "$TARGET_DIR" "$LOG_DIR"
sed -e "s|__REPO__|$REPO|g" -e "s|__HOME__|$HOME|g" "$SOURCE" > "$TARGET"

echo "Installed $TARGET (runs ops/collect_guard.sh every 30 minutes and at login;"
echo "the guard collects once a day after 18:00 local time; logs in $LOG_DIR)"
echo "Load it with:"
echo "  launchctl bootstrap gui/$(id -u) \"$TARGET\""
echo "Unload it with:"
echo "  launchctl bootout gui/$(id -u) \"$TARGET\""
