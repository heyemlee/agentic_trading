#!/bin/zsh
set -euo pipefail
source "$(dirname "$0")/common.sh"

# Rotates the launchd logs, drops report bodies from runs past their retention window and
# records the heartbeat the console reads. Exits non-zero when free disk is below the floor,
# so the failure is visible in the job's own log rather than only inside the console.
exec "$release_dir/.venv/bin/ct-ops" maintain --config-dir "$data_dir/config"
