#!/usr/bin/env bash
# Embed the native controller binary, support scripts, and LaunchAgent into an
# already-built .app bundle. Used by build-app.sh and build-xcode-app.sh so Debug
# / Xcode runs cannot ship a UI-only bundle that cannot start the controller.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
APP_BUNDLE="${1:?Usage: embed-controller.sh /path/to/App.app}"

if [ ! -d "$APP_BUNDLE/Contents" ]; then
  echo "embed-controller: not an app bundle: $APP_BUNDLE" >&2
  exit 1
fi

cd "$ROOT_DIR"
CONTROLLER_BIN_DIR="$(swift build -c release --show-bin-path)"
swift build -c release --product ModelMatrixController >/dev/null

mkdir -p \
  "$APP_BUNDLE/Contents/Resources/ControllerSupport/model-profiles" \
  "$APP_BUNDLE/Contents/Library/LaunchAgents"

cp "$CONTROLLER_BIN_DIR/ModelMatrixController" \
  "$APP_BUNDLE/Contents/Resources/ModelMatrixController"
cp "$ROOT_DIR/Controller/start-model-mac.sh" "$ROOT_DIR/Controller/stop-all-models.sh" \
  "$APP_BUNDLE/Contents/Resources/ControllerSupport/"
rm -rf "$APP_BUNDLE/Contents/Resources/ControllerSupport/model-profiles/examples"
cp -R "$ROOT_DIR/Controller/model-profiles/examples" \
  "$APP_BUNDLE/Contents/Resources/ControllerSupport/model-profiles/examples"
cp "$ROOT_DIR/Resources/Controller/io.github.hackstar1337.modelmatrix.controller.plist" \
  "$APP_BUNDLE/Contents/Library/LaunchAgents/"

# Web dashboard (served by the controller at http://localhost:7700). Built
# with pnpm/npm when Node is available; an existing Dashboard/dist is reused.
DASHBOARD_DIR="$ROOT_DIR/Dashboard"
if [ "${MODELMATRIX_SKIP_DASHBOARD_BUILD:-0}" != "1" ] && command -v node >/dev/null 2>&1; then
  if command -v pnpm >/dev/null 2>&1; then
    (cd "$DASHBOARD_DIR" && pnpm install --frozen-lockfile >/dev/null 2>&1 || pnpm install >/dev/null && pnpm build >/dev/null)
  elif command -v npm >/dev/null 2>&1; then
    (cd "$DASHBOARD_DIR" && npm install --no-audit --no-fund >/dev/null && npm run build >/dev/null)
  fi
fi
if [ -f "$DASHBOARD_DIR/dist/index.html" ]; then
  rm -rf "$APP_BUNDLE/Contents/Resources/Dashboard"
  cp -R "$DASHBOARD_DIR/dist" "$APP_BUNDLE/Contents/Resources/Dashboard"
else
  echo "embed-controller: warning: Dashboard/dist missing (install Node.js to build the web dashboard)" >&2
fi

# Remote agent modules, embedded so the app can deploy them to SSH gateways
# without the remote host downloading anything.
mkdir -p "$APP_BUNDLE/Contents/Resources/RemoteAgent"
cp "$ROOT_DIR/RemoteAgent/modelmatrix_agent.py" \
  "$ROOT_DIR/RemoteAgent/agent_core.py" \
  "$ROOT_DIR/RemoteAgent/discovery.py" \
  "$ROOT_DIR/RemoteAgent/hf_agent.py" \
  "$ROOT_DIR/RemoteAgent/client_connections.py" \
  "$ROOT_DIR/RemoteAgent/install-remote-agent.sh" \
  "$APP_BUNDLE/Contents/Resources/RemoteAgent/"

chmod 755 \
  "$APP_BUNDLE/Contents/Resources/ModelMatrixController" \
  "$APP_BUNDLE/Contents/Resources/ControllerSupport/"*.sh \
  "$APP_BUNDLE/Contents/Resources/RemoteAgent/install-remote-agent.sh"

printf 'embedded_controller=%s\n' "$APP_BUNDLE/Contents/Resources/ModelMatrixController"
