#!/bin/zsh
set -euo pipefail

for label in \
  com.local.agentic-trading.api \
  com.local.agentic-trading.research-weekly \
  com.local.agentic-trading.backup-daily \
  com.local.agentic-trading.maintenance-daily; do
  launchctl bootout "gui/$UID/$label" 2>/dev/null || true
  rm -f "$HOME/Library/LaunchAgents/$label.plist"
done

echo "Removed launch jobs. Trading state, logs and backups were retained."
