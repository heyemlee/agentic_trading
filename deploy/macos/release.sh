#!/bin/zsh
set -euo pipefail

# Publish and roll back a fixed version of the service.
#
# The problem this solves: while the service ran straight out of the development checkout, every
# save was a deployment. Editing a file changed what the API would load on its next restart, and
# a worker spawned mid-edit could run a different version from the process that spawned it.
#
# So the code and the data are separated. A release is an immutable export of a git commit with
# its own virtual environment, under releases/<timestamp>-<sha>. A symlink named `current` says
# which one the launchd jobs run. Config, state and runs stay in the data directory and are
# never copied into a release, so publishing and rolling back never touch a ledger.
#
#   deploy/macos/release.sh publish [--ref HEAD] [--data-dir DIR] [--allow-dirty] [--force]
#   deploy/macos/release.sh rollback [--to <release name>]
#   deploy/macos/release.sh list
#
# Nothing here starts research, submits an order or sends a notification. It stops the API,
# swaps a symlink and starts it again.

source_dir="$(cd "$(dirname "$0")/../.." && pwd -P)"
releases_root="${CT_RELEASES_DIR:-$HOME/Library/Application Support/agentic-trading/releases}"
current_link="$releases_root/current"
launch_agents="$HOME/Library/LaunchAgents"
data_dir="$source_dir"
ref="HEAD"
allow_dirty=false
force=false
target=""

action="${1:-}"
shift || true
while (( $# )); do
  case "$1" in
    --ref)         ref="$2"; shift 2 ;;
    --data-dir)    data_dir="$(cd "$2" && pwd -P)"; shift 2 ;;
    --to)          target="$2"; shift 2 ;;
    --allow-dirty) allow_dirty=true; shift ;;
    --force)       force=true; shift ;;
    *) print -r -- "unknown option: $1" >&2; exit 2 ;;
  esac
done

die() { print -r -- "$*" >&2; exit 1 }

resolve_release() {                       # the release a name refers to, or the current one
  local name="$1"
  [[ -n "$name" ]] && print -r -- "$releases_root/$name" && return
  [[ -L "$current_link" ]] || die "no release is installed yet; run 'release.sh publish' first"
  print -r -- "$(cd "$current_link" && pwd -P)"
}

venv_for() { print -r -- "$1/.venv/bin" }

activate() {                              # point launchd at a release and prove it came up
  local release="$1"
  local previous="${2:-}"
  ln -sfn "$release" "$current_link"
  "$(venv_for "$release")/python" -m conservative_trading.launchd \
    --data-dir "$data_dir" --release-dir "$release" --launch-agents "$launch_agents"
  for label in \
    com.local.agentic-trading.api \
    com.local.agentic-trading.research-weekly \
    com.local.agentic-trading.backup-daily \
    com.local.agentic-trading.maintenance-daily; do
    plutil -lint "$launch_agents/$label.plist" >/dev/null
    launchctl bootout "gui/$UID/$label" 2>/dev/null || true
    for attempt in {1..10}; do
      launchctl bootstrap "gui/$UID" "$launch_agents/$label.plist" 2>/dev/null && break
      sleep 0.5
    done
    launchctl enable "gui/$UID/$label"
  done
  launchctl kickstart -k "gui/$UID/com.local.agentic-trading.api"

  for attempt in {1..40}; do
    if curl --fail --silent --output /dev/null http://127.0.0.1:8787/ui/login.html; then
      print -r -- "running $(basename "$release")"
      return 0
    fi
    sleep 0.25
  done
  print -r -- "the new release did not answer on loopback" >&2
  if [[ -n "$previous" && -d "$previous" ]]; then
    print -r -- "rolling back to $(basename "$previous")" >&2
    activate "$previous"
    exit 1
  fi
  exit 1
}

case "$action" in
publish)
  command -v uv >/dev/null || die "uv is required to build a release"
  git -C "$source_dir" rev-parse --git-dir >/dev/null 2>&1 || die "$source_dir is not a git checkout"
  if [[ "$allow_dirty" != true ]] && [[ -n "$(git -C "$source_dir" status --porcelain)" ]]; then
    die "the checkout has uncommitted changes; commit them so the release is traceable, or pass --allow-dirty"
  fi
  [[ -d "$data_dir/config" ]] || die "no config directory at $data_dir/config"
  [[ -f "$data_dir/state/trading_state.sqlite3" ]] || die "no state database at $data_dir/state"

  sha="$(git -C "$source_dir" rev-parse --short "$ref")"
  name="$(date -u +%Y%m%dT%H%M%SZ)-$sha"
  release="$releases_root/$name"
  [[ -e "$release" ]] && die "release $name already exists"

  # A release must never be published over an order the broker still owns: a restart between
  # submission and reconciliation is exactly when a fill goes unrecorded.
  if [[ -x "$current_link/.venv/bin/ct-state" ]]; then
    if ! "$current_link/.venv/bin/ct-state" status --config-dir "$data_dir/config" \
          --fail-on-unsettled >/dev/null; then
      [[ "$force" == true ]] || die "an order is still open at the broker; settle it or pass --force"
    fi
    "$current_link/.venv/bin/ct-state" backup --config-dir "$data_dir/config" --verify >/dev/null
    print -r -- "backed up the state database"
  fi

  print -r -- "building $name from $ref"
  mkdir -p "$release"
  git -C "$source_dir" archive --format=tar "$ref" | tar -x -C "$release"
  ( cd "$release" && uv sync --frozen --quiet ) || die "uv sync failed in $release"
  chmod 700 "$release/deploy/macos/"*.sh
  print -r -- "{\"name\":\"$name\",\"sha\":\"$sha\",\"ref\":\"$ref\",\"data_dir\":\"$data_dir\",\"published_at\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}" \
    > "$release/RELEASE.json"

  previous=""
  [[ -L "$current_link" ]] && previous="$(cd "$current_link" && pwd -P)"
  activate "$release" "$previous"
  "$(venv_for "$release")/python" -m conservative_trading.operations heartbeat \
    --config-dir "$data_dir/config" --activity maintenance --detail "published $name" >/dev/null
  print -r -- "published $name; the previous release is kept for rollback"
  ;;

rollback)
  [[ -d "$releases_root" ]] || die "no releases directory at $releases_root"
  current=""
  [[ -L "$current_link" ]] && current="$(basename "$(cd "$current_link" && pwd -P)")"
  if [[ -z "$target" ]]; then
    target="$(ls -1 "$releases_root" | grep -v '^current$' | grep -v "^$current\$" | sort | tail -1)"
  fi
  [[ -n "$target" ]] || die "no earlier release to roll back to"
  release="$(resolve_release "$target")"
  [[ -d "$release" ]] || die "no release named $target"
  print -r -- "rolling back from ${current:-nothing} to $target"
  print -r -- "note: the database is not rolled back. A release that migrated it must be"
  print -r -- "restored with 'ct-state restore' before an older release can read it."
  activate "$release"
  ;;

list)
  [[ -d "$releases_root" ]] || die "no releases directory at $releases_root"
  current=""
  [[ -L "$current_link" ]] && current="$(basename "$(cd "$current_link" && pwd -P)")"
  for entry in "$releases_root"/*(/N); do
    name="$(basename "$entry")"
    marker="  "
    [[ "$name" == "$current" ]] && marker="* "
    print -r -- "$marker$name"
  done
  ;;

*)
  print -r -- "usage: release.sh {publish|rollback|list} [options]" >&2
  exit 2
  ;;
esac
