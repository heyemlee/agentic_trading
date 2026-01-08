#!/bin/zsh
set -euo pipefail
source "$(dirname "$0")/common.sh"

# A local copy protects against a bad migration or a corrupt write. It does not protect
# against losing the Mac, which is what --mirror-dir is for: set CT_BACKUP_MIRROR to a
# directory on other hardware (an external disk, a synced folder) and CT_BACKUP_KEY_FILE to a
# passphrase file to have an encrypted copy written there too.
mirror_args=()
[[ -z "${CT_BACKUP_MIRROR:-}" ]] || mirror_args+=(--mirror-dir "$CT_BACKUP_MIRROR")
[[ -z "${CT_BACKUP_KEY_FILE:-}" ]] || mirror_args+=(--key-file "$CT_BACKUP_KEY_FILE")

exec "$release_dir/.venv/bin/ct-state" backup \
  --config-dir "$data_dir/config" --verify --retain-days 30 "${mirror_args[@]}"
