# Working in this repository

Conventions that are not derivable from the code. Read this before changing anything.

## This checkout is also the production data directory

Four launchd jobs run against it: `com.local.agentic-trading.{api,research-weekly,backup-daily,maintenance-daily}`.
Their `CT_DATA_DIR` is this directory. Code runs from an immutable release under
`~/Library/Application Support/agentic-trading/releases/current`; `config/`, `state/`, `runs/`
and `.env` are read live from here, as `CT_DATA_DIR` always was.

`state`, `runs` and `.env` are symlinks into
`~/Library/Application Support/agentic-trading/data/`, put there on 2026-09-09 so that an
ordinary git accident in this checkout — `git clean -xdf`, an errant `rm -rf` — cannot reach the
ledger, the broker token or the backup passphrase. Nothing else changed: every path in the code
and in `deploy/`, `CT_DATA_DIR` included, still just says `state/…`, `runs/…`, `.env`, and the OS
resolves the symlink transparently. `config/` stays a real, git-tracked directory in the
checkout on purpose — it is meant to be reviewed and diffed, and moving it out would only create
a second copy to keep in sync.

Real-money execution is enabled for the `micro_live` bucket.

- Editing `config/*.yaml` changes the running system's behaviour, not just the development copy.
- `state/` holds the ledger, the SQLite audit database and the drawdown high-water marks. Never
  move, rename or hand-edit anything in it, symlink or not. Use `ct-state`.
- **Never `git checkout`/`switch` to another branch or ref in this checkout.** The `state`/`runs`/
  `.env` symlinks only stop a *destructive* git accident (`clean -xdf`, `rm -rf`); they do nothing
  against a normal checkout. If the target ref tracks a real file at one of those names — an
  older `main` once tracked `state/*.example.yaml` — git deletes the symlink and writes a real
  file or directory in its place, and every other tracked path (`config/`, `agentic_trading/`,
  `ui/`, …) changes under the running system too. This happened on 2026-09-10: `git checkout
  main` replaced the `state` symlink with a real directory; the live ledger was untouched only
  because it lives at the symlink's target, but the API kept writing to the new empty directory
  until it was found and restarted. To land a branch on `main`, push a refspec instead —
  `git push origin <branch>:main` (fast-forward) or open a PR — and never move `HEAD` here.
- Before stopping or restarting the live API by hand: `deploy/macos/checks.sh`'s
  `--fail-on-unsettled` guard (see `release.sh publish`) is the check to run first, not a guess.
  `start.sh`'s port cleanup calls `lsof -ti :PORT` without filtering to `LISTEN`, so it will list
  (and try to kill) any process that merely has a stale, already-closed connection to that port
  number — harmless against another app's helper process (macOS blocks the cross-app signal),
  but worth knowing before reading its output as "these are all copies of the API."
- Code and documentation can be reorganised freely; they live in git and ship in a release.

## Boundaries that are load-bearing

- **Upstream TradingAgents is a pinned git dependency and is never vendored or modified.** The
  commit is pinned in `pyproject.toml` and recorded in `agentic_trading/__init__.py`. Personal
  preferences go in `config/`, not upstream.
- **Never set `ANTHROPIC_API_KEY`,** in `.env`, in a shell, or in a test. The LLM layer runs on
  the owner's Claude subscription through the Claude Code CLI; an API key silently switches
  billing to pay-as-you-go. The adapter strips these variables before every call.
- **Safety limits are enforced by deterministic code, never by prompts.** Agents own research
  and produce proposals. `risk/`, `policy.py` and `execution.py` decide what may happen. A
  confident model output never widens a limit.
- **Secrets are never tracked.** `.env`, `state/console_auth.json`, `state/robinhood_token.json`,
  `state/vapid_key.json` and the SQLite files are gitignored and named in the secret scan. The
  repository is public.

## Before you finish

```bash
deploy/checks.sh
```

Ruff, the Python suite, the frontend cases and a secret scan. The same script runs in CI
(`.github/workflows/checks.yml`) and is what a release is held to.

The suite is entirely offline: no venue, no credentials, no model call. `tests/conftest.py`
blocks any test that opens the real state database and fails a test that modifies a real state
file, so a red run there is a defect in the test, not a reason to relax the guard.

## Layout

```
agentic_trading/     the package
  api/               HTTP layer only — a thin FastAPI wrapper over service/
  service/           every product operation, plain Python, no HTTP; shared by the CLI, the
                     scheduler and the API
  broker/            venue adapters (simulated, Robinhood)
  llm/               Claude Code adapter and the MCP tool server
  risk/              hard risk engine, drawdown ladders, regime
config/              profiles, the mandate, the prompt context — live, read by the running
                     service, and (unlike state/runs/.env) tracked in git on purpose
  examples/          state/*.yaml templates a setup script copies into the live ledger
state/               ledgers, audit database, tokens — a symlink outside the checkout; untracked
runs/                research artefacts and logs — a symlink outside the checkout; untracked
ui/                  the console: seven pages, no build step, served at /ui by the API
deploy/macos/        launchd install, release/rollback, backup, maintenance, selfcheck
tools/               developer utilities that are not part of the deployment
docs/                current documentation; docs/archive/ is point-in-time and unmaintained
```

## Conventions

- Console scripts are prefixed `ct-`, environment variables `CT_`. Both are referenced from
  launchd plists, deployment scripts and `.env`; renaming one is a deployment change.
- The frontend deliberately has no build step and no offline caching. Approvals must never be
  queued offline and replayed later.
- `ui/` strings the console writes itself go through `assets/i18n.js`. Anything the API returns
  is rendered verbatim in whatever language the engine produced.
