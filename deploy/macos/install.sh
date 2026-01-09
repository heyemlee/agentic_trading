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

"$project_dir/.venv/bin/python" -m conservative_trading.launchd \
  --data-dir "$project_dir" --launch-agents "$launch_agents" "${origin_args[@]}"

chmod 700 "$project_dir/deploy/macos/"*.sh
for label in \
  com.local.agentic-trading.api \
  com.local.agentic-trading.research-weekly \
  com.local.agentic-trading.backup-daily \
  com.local.agentic-trading.maintenance-daily; do
  target="$launch_agents/$label.plist"
  plutil -lint "$target"
  launchctl bootout "gui/$UID/$label" 2>/dev/null || true
  loaded=false
  for attempt in {1..10}; do
    if launchctl bootstrap "gui/$UID" "$target" 2>/dev/null; then
      loaded=true
      break
    fi
    sleep 0.5
  done
  if [[ "$loaded" != true ]]; then
    echo "failed to load $label after waiting for the previous job to exit" >&2
    exit 1
  fi
  launchctl enable "gui/$UID/$label"
done
launchctl kickstart -k "gui/$UID/com.local.agentic-trading.api"

ready=false
for attempt in {1..40}; do
  if curl --fail --silent --output /dev/null http://127.0.0.1:8787/ui/login.html; then
    ready=true
    break
  fi
  sleep 0.25
done
if [[ "$ready" != true ]]; then
  echo "ct-api did not become ready; inspect runs/service/api.stderr.log" >&2
  exit 1
fi

echo "Installed API restart, Friday research, daily backup and daily maintenance jobs."
if ! grep -q 'https://' "$launch_agents/com.local.agentic-trading.api.plist"; then
  echo "Remote HTTPS origin is not configured yet; rerun with the Tailscale Serve origin."
fi
