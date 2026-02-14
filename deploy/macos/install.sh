#!/bin/zsh
set -euo pipefail

# Installs the launchd jobs for whichever checkout this script lives in. A plain rerun with no
# argument keeps the remote HTTPS origin that is already installed, so redeploying after a code
# change never narrows the allowed origins back to loopback and locks the phone out. Pass an
# origin to set one, or --clear to go loopback-only on purpose.

project_dir="$(cd "$(dirname "$0")/../.." && pwd -P)"
launch_agents="$HOME/Library/LaunchAgents"
origin_args=()
case "${1:-}" in
  "")       ;;
  --clear)  origin_args=(--clear-origins) ;;
  *)        origin_args=(--https-origin "$1") ;;
esac

if [[ ! -x "$project_dir/.venv/bin/ct-api" ]]; then
  echo "missing .venv/bin/ct-api; run 'uv sync' first" >&2
  exit 1
fi
if [[ ! -f "$project_dir/state/trading_state.sqlite3" ]]; then
  echo "missing state database; stop ct-api and run 'uv run ct-state migrate' first" >&2
  exit 1
fi

mkdir -p "$launch_agents" "$project_dir/runs/service" "$project_dir/state/backups"
chmod 700 "$project_dir/runs/service" "$project_dir/state/backups"
[[ ! -f "$project_dir/.env" ]] || chmod 600 "$project_dir/.env"
[[ ! -f "$project_dir/state/console_auth.json" ]] || chmod 600 "$project_dir/state/console_auth.json"
[[ ! -f "$project_dir/state/robinhood_token.json" ]] || chmod 600 "$project_dir/state/robinhood_token.json"

labels=(
  com.local.agentic-trading.api
  com.local.agentic-trading.research-weekly
  com.local.agentic-trading.backup-daily
  com.local.agentic-trading.maintenance-daily
)

# Snapshot each plist before rewriting. A plain rerun with no origin change produces byte-
# identical files, and reloading a job whose plist did not change is not free: re-registering
# an unchanged LaunchAgent trains macOS's background-task manager to throttle it — a later
# `launchctl bootstrap` keeps reporting success while the job silently never starts, visible
# only via `log show --predicate 'eventMessage CONTAINS "Exceeded max notifications"'`. Enough
# reruns in a short window (which is exactly what iterating on this script looks like) trips it.
typeset -A before_reload
for label in "${labels[@]}"; do
  before_reload[$label]="$(shasum -a 256 "$launch_agents/$label.plist" 2>/dev/null || true)"
done

"$project_dir/.venv/bin/python" -m agentic_trading.launchd \
  --data-dir "$project_dir" --launch-agents "$launch_agents" "${origin_args[@]}"

chmod 700 "$project_dir/deploy/macos/"*.sh
for label in "${labels[@]}"; do
  target="$launch_agents/$label.plist"
  after_reload="$(shasum -a 256 "$target" 2>/dev/null || true)"
  if [[ "$after_reload" == "${before_reload[$label]}" ]]; then
    continue                                # unchanged: the loaded job already matches
  fi
  plutil -lint "$target"
  launchctl bootout "gui/$UID/$label" 2>/dev/null || true
  # bootout returns before the job is gone, and the API is given ExitTimeOut 30 to close its
  # broker sessions; launchd answers a bootstrap inside that window with an I/O error. Ten
  # half-second attempts with their errors discarded were the same five seconds release.sh
  # used until 2026-09-12, when the API outlasted them and stayed unregistered: KeepAlive gone,
  # the service down until someone bootstrapped it by hand. Same wait as release.sh now, and the
  # last bootstrap error is the message.
  for attempt in {1..60}; do
    launchctl print "gui/$UID/$label" >/dev/null 2>&1 || break
    sleep 0.5
  done
  bootstrap_error=""
  for attempt in {1..60}; do
    bootstrap_error="$(launchctl bootstrap "gui/$UID" "$target" 2>&1)" && break
    sleep 0.5
  done
  if ! launchctl print "gui/$UID/$label" >/dev/null 2>&1; then
    echo "$label did not register: ${bootstrap_error:-no error reported}. The service is not running." >&2
    exit 1
  fi
  launchctl enable "gui/$UID/$label"
done
# Restarts the running process without touching launchd's registration, so a rerun that
# changed nothing still picks up new code without contributing to the throttle above.
launchctl kickstart -k "gui/$UID/com.local.agentic-trading.api"

ready=false
for attempt in {1..120}; do
  if curl --fail --silent --output /dev/null http://127.0.0.1:8787/ui/login.html; then
    ready=true
    break
  fi
  sleep 0.5
done
if [[ "$ready" != true ]]; then
  echo "ct-api did not become ready; inspect runs/service/api.stderr.log" >&2
  exit 1
fi

echo "Installed API restart, Friday research, daily backup and daily maintenance jobs."
if ! grep -q 'https://' "$launch_agents/com.local.agentic-trading.api.plist"; then
  echo "Remote HTTPS origin is not configured yet; rerun with the Tailscale Serve origin."
fi
