#!/bin/bash

# Required parameters:
# @raycast.schemaVersion 1
# @raycast.title Stop All Local Models
# @raycast.mode compact

# Optional parameters:
# @raycast.packageName ModelMatrix
# @raycast.icon ⛔️
# @raycast.description Stop every running model managed by the controller.

ROOT_DIR="$(cd "$(dirname "$0")/../../.." && pwd)"
"$ROOT_DIR/Scripts/modelmatrixctl" stop-all >/dev/null
