#!/bin/bash
# Agent-Lit dev auto-reload — wired to the ZCode Stop hook (.zcode/config.json),
# runs after every agent reply; manual use is fine too: `bash reload.sh`.
#
# Detects source changes newer than the last handled build (.reload-stamp) and
# syncs dist/Agent-Lit.app accordingly:
#   - only src/agent_lit/web/static/index.html changed → hot-patch the copy
#     bundled inside the .app, relaunch instantly (the app loads it from disk)
#   - anything else (py / spec / assets / pyproject) → delegate to
#     reload-build.sh in the background (log: /tmp/agent-lit-reload.log)
# No changes → silent exit. All messages go to stderr: the Stop hook parses
# stdout as JSON, so stdout must stay empty.

ROOT="$(cd "$(dirname "$0")" && pwd)"
APP="$ROOT/dist/Agent-Lit.app"
STAMP="$ROOT/.reload-stamp"
SRC_HTML="$ROOT/src/agent_lit/web/static/index.html"
BUNDLED_HTML="$APP/Contents/MacOS/_internal/agent_lit/web/static/index.html"

[ -f "$STAMP" ] || : > "$STAMP"

changed=$(find "$ROOT/src" "$ROOT/assets" "$ROOT/agent-lit.spec" "$ROOT/pyproject.toml" \
  -type f -newer "$STAMP" -not -path '*__pycache__*' 2>/dev/null)
[ -z "$changed" ] && exit 0

# Frontend-only means exactly index.html; anything else needs a full rebuild
non_html=$(printf '%s\n' "$changed" | grep -v "^$SRC_HTML$")

if [ -z "$non_html" ] && [ -f "$BUNDLED_HTML" ]; then
  cp "$SRC_HTML" "$BUNDLED_HTML" && touch "$STAMP"
  osascript -e 'tell application id "com.agent-lit.app" to quit' >/dev/null 2>&1 || true
  sleep 1
  pkill -x agent-lit >/dev/null 2>&1 || true
  sleep 1
  open "$APP"
  echo "reload: hot-patched index.html, relaunched Agent-Lit" >&2
else
  echo "reload: backend change detected, background rebuild starting" >&2
  nohup bash "$ROOT/reload-build.sh" >> /tmp/agent-lit-reload.log 2>&1 &
  disown
fi
exit 0
