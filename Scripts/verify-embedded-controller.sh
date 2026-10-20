#!/usr/bin/env bash
# Verify that an .app bundle embeds ModelMatrixController + its LaunchAgent.
# Shared by install.sh and verify-distribution.sh.
set -euo pipefail

APP_PATH="${1:?Usage: verify-embedded-controller.sh /path/to/App.app}"

if [ ! -d "$APP_PATH/Contents" ]; then
  echo "verify-embedded-controller: not an app bundle: $APP_PATH" >&2
  exit 1
fi

CONTROLLER_BIN="$APP_PATH/Contents/Resources/ModelMatrixController"
CONTROLLER_PLIST="$APP_PATH/Contents/Library/LaunchAgents/io.github.hackstar1337.modelmatrix.controller.plist"
REMOTE_AGENT_PY="$APP_PATH/Contents/Resources/RemoteAgent/modelmatrix_agent.py"
REMOTE_AGENT_CORE="$APP_PATH/Contents/Resources/RemoteAgent/agent_core.py"
REMOTE_AGENT_DISCOVERY="$APP_PATH/Contents/Resources/RemoteAgent/discovery.py"
REMOTE_AGENT_HF="$APP_PATH/Contents/Resources/RemoteAgent/hf_agent.py"
REMOTE_AGENT_CONNECTIONS="$APP_PATH/Contents/Resources/RemoteAgent/client_connections.py"
REMOTE_AGENT_INSTALLER="$APP_PATH/Contents/Resources/RemoteAgent/install-remote-agent.sh"

[ -x "$CONTROLLER_BIN" ] || {
  echo "embedded controller missing: $CONTROLLER_BIN" >&2
  exit 1
}
[ -f "$CONTROLLER_PLIST" ] || {
  echo "controller LaunchAgent missing: $CONTROLLER_PLIST" >&2
  exit 1
}
[ -f "$APP_PATH/Contents/Resources/Dashboard/index.html" ] || {
  echo "verify-embedded-controller: missing web dashboard at $APP_PATH/Contents/Resources/Dashboard" >&2
  exit 1
}
[ -f "$REMOTE_AGENT_PY" ] || {
  echo "embedded remote agent missing: $REMOTE_AGENT_PY" >&2
  exit 1
}
[ -f "$REMOTE_AGENT_CORE" ] || {
  echo "embedded remote agent core module missing: $REMOTE_AGENT_CORE" >&2
  exit 1
}
[ -f "$REMOTE_AGENT_DISCOVERY" ] || {
  echo "embedded remote agent discovery module missing: $REMOTE_AGENT_DISCOVERY" >&2
  exit 1
}
[ -f "$REMOTE_AGENT_HF" ] || {
  echo "embedded remote agent Hugging Face module missing: $REMOTE_AGENT_HF" >&2
  exit 1
}
[ -f "$REMOTE_AGENT_CONNECTIONS" ] || {
  echo "embedded remote connection module missing: $REMOTE_AGENT_CONNECTIONS" >&2
  exit 1
}
[ -x "$REMOTE_AGENT_INSTALLER" ] || {
  echo "embedded remote agent installer missing or not executable: $REMOTE_AGENT_INSTALLER" >&2
  exit 1
}

printf 'embedded_controller_ok=%s\n' "$APP_PATH"
