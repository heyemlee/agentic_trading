#!/bin/zsh
set -euo pipefail
source "$(dirname "$0")/common.sh"

exec "$release_dir/.venv/bin/ct-trigger" scheduled \
  --config-dir "$data_dir/config"
