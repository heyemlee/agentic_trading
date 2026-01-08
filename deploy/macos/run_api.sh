#!/bin/zsh
set -euo pipefail
source "$(dirname "$0")/common.sh"

exec "$release_dir/.venv/bin/ct-api" --host 127.0.0.1 --port 8787
