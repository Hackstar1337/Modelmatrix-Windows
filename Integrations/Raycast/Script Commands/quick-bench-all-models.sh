#!/bin/bash

# Required parameters:
# @raycast.schemaVersion 1
# @raycast.title Quick Bench All Models
# @raycast.mode compact

# Optional parameters:
# @raycast.packageName ModelMatrix
# @raycast.icon ⚡️
# @raycast.description Start the quick benchmark suite across all profiles.

ROOT_DIR="$(cd "$(dirname "$0")/../../.." && pwd)"
"$ROOT_DIR/Scripts/modelmatrixctl" bench-all >/dev/null
