#!/usr/bin/env zsh
# Formatted profile inventory for the vhs demo.
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
"$ROOT_DIR/Scripts/modelmatrixctl" status \
  | jq -r '.statuses[:8] | .[] | "  " + .profile + "  (" + .runtime + ")"'
