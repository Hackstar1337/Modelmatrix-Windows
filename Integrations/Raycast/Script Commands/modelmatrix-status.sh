#!/bin/bash

# Required parameters:
# @raycast.schemaVersion 1
# @raycast.title ModelMatrix Status
# @raycast.mode inline
# @raycast.refreshTime 15s

# Optional parameters:
# @raycast.packageName ModelMatrix
# @raycast.icon 💻
# @raycast.description Show local model readiness and running state.

ROOT_DIR="$(cd "$(dirname "$0")/../../.." && pwd)"
"$ROOT_DIR/Scripts/modelmatrixctl" summary | head -1
