#!/bin/zsh
set -uo pipefail

# One command to run before publishing a release: lint, the Python suite, the frontend cases,
# and a scan for credentials that must never enter a commit or a release archive.
#
#   deploy/checks.sh
#
# Read-only. Starts no server, spends no Claude quota and touches no state directory: the suite
# blocks any test that opens the real database (tests/conftest.py).

project_dir="$(cd "$(dirname "$0")/.." && pwd -P)"
cd "$project_dir"
fails=0

step() {
  local label="$1"; shift
  print -r -- "── $label"
  if "$@"; then
    print -r -- "   ok"
  else
    print -r -- "   FAILED"
    fails=$((fails + 1))
  fi
}

secret_scan() {
  # Only files git would ship. Anything matching here is a real credential shape, not a
  # variable name: the point is to catch a token pasted into config or a doc.
  local pattern='(-----BEGIN [A-Z ]*PRIVATE KEY-----|sk-[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}|ghp_[A-Za-z0-9]{30,}|AKIA[0-9A-Z]{16}|(access|refresh|bearer)_token"?\s*[:=]\s*"?[A-Za-z0-9_\-\.]{20,})'
  local hits
  hits="$(git ls-files -z | xargs -0 grep -nEI "$pattern" 2>/dev/null | grep -v '^tests/' || true)"
  if [[ -n "$hits" ]]; then
    print -r -- "$hits"
    return 1
  fi
  # The files that legitimately hold secrets must not be tracked at all.
  local tracked
  tracked="$(git ls-files -- .env 'state/console_auth.json' 'state/robinhood_token.json' \
    'state/*.sqlite3' || true)"
  if [[ -n "$tracked" ]]; then
    print -r -- "these files hold credentials or live state and must not be tracked:"
    print -r -- "$tracked"
    return 1
  fi
  return 0
}

step "ruff"            .venv/bin/python -m ruff check .
step "pytest"          .venv/bin/python -m pytest -q
step "frontend cases"  node --test tests/frontend.test.cjs
step "secret scan"     secret_scan

print -r -- ""
if (( fails )); then
  print -r -- "$fails check(s) failed"
  exit 1
fi
print -r -- "all checks passed"
