#!/bin/zsh
set -uo pipefail
source "$(dirname "$0")/common.sh"

# Read-only verification of an unattended deployment. Starts nothing, submits nothing and
# spends no Claude quota; it answers "did everything come back after that reboot".
#
#   deploy/macos/selfcheck.sh
#
# Every check prints ok/FAIL and the script exits non-zero if any failed, so it can be the
# last step of a restart drill.

fails=0
check() {
  local label="$1"; shift
  if output="$("$@" 2>&1)"; then
    print -r -- "ok    $label"
  else
    print -r -- "FAIL  $label: ${output//$'\n'/ | }"
    fails=$((fails + 1))
  fi
}

print -r -- "release: $release_dir"
print -r -- "data:    $data_dir"
print -r -- ""

for label in \
  com.local.agentic-trading.api \
  com.local.agentic-trading.research-weekly \
  com.local.agentic-trading.backup-daily \
  com.local.agentic-trading.maintenance-daily; do
  check "launchd job $label" launchctl print "gui/$UID/$label"
done

check "API answering on loopback" \
  curl --fail --silent --show-error --output /dev/null http://127.0.0.1:8787/ui/login.html
check "console credentials configured" \
  test -f "$data_dir/state/console_auth.json"
check "state database integrity" \
  "$release_dir/.venv/bin/ct-state" status --config-dir "$data_dir/config"
check "research runtime reachable" \
  test -x "${CT_CLAUDE_BIN:-$HOME/.local/bin/claude}"

if command -v tailscale >/dev/null 2>&1; then
  check "tailscale backend running" tailscale status --json
  check "tailscale serve configured" tailscale serve status
else
  print -r -- "skip  tailscale not installed"
fi

print -r -- ""
print -r -- "background activities and disk:"
"$release_dir/.venv/bin/ct-ops" status --config-dir "$data_dir/config" || fails=$((fails + 1))
print -r -- ""
print -r -- "schedule decision (nothing is started):"
"$release_dir/.venv/bin/ct-trigger" scheduled --dry-run --config-dir "$data_dir/config" \
  || fails=$((fails + 1))

print -r -- ""
if (( fails )); then
  print -r -- "$fails check(s) failed"
  exit 1
fi
print -r -- "all checks passed"
