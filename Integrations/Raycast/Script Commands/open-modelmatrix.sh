#!/bin/bash

# Required parameters:
# @raycast.schemaVersion 1
# @raycast.title Open ModelMatrix
# @raycast.mode compact

# Optional parameters:
# @raycast.packageName ModelMatrix
# @raycast.icon 🧭
# @raycast.description Launch the menu bar app.

ROOT_DIR="$(cd "$(dirname "$0")/../../.." && pwd)"
"$ROOT_DIR/Scripts/modelmatrixctl" open-app >/dev/null
