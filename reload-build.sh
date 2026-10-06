#!/bin/bash
# Background full rebuild + relaunch, launched by reload.sh via nohup.
#
# Builds are serialized with a mkdir lock; a lock older than 10 minutes is
# treated as stale (crashed build) and cleared. The stamp is touched only
# after a successful build, so a failed build retries on the next Stop hook.

ROOT="$(cd "$(dirname "$0")" && pwd)"
APP="$ROOT/dist/Otlet.app"
STAMP="$ROOT/.reload-stamp"
LOCKD="/tmp/otlet-reload.lockdir"

echo "[$(date '+%H:%M:%S')] reload-build: waiting for lock / building..."
until mkdir "$LOCKD" 2>/dev/null; do
  # Empty find output = lock dir older than 10 min = stale, clear it
  [ -n "$(find "$LOCKD" -maxdepth 0 -newermt '-10 minutes' 2>/dev/null)" ] || rm -rf "$LOCKD"
  sleep 2
done
trap 'rm -rf "$LOCKD"' EXIT

if ! "$ROOT/build.sh"; then
  echo "[$(date '+%H:%M:%S')] reload-build: BUILD FAILED — stamp untouched, will retry on next change"
  exit 1
fi
touch "$STAMP"

echo "[$(date '+%H:%M:%S')] reload-build: build ok, relaunching Otlet"
osascript -e 'tell application id "com.otlet.app" to quit' >/dev/null 2>&1 || true
sleep 1
pkill -x otlet >/dev/null 2>&1 || true
sleep 1
open "$APP"
echo "[$(date '+%H:%M:%S')] reload-build: done"
