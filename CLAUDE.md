# Working in this repository

Conventions that are not derivable from the code. Read this before changing anything.

## This checkout is also the production data directory

Four launchd jobs run against it: `com.local.agentic-trading.{api,research-weekly,backup-daily,maintenance-daily}`.
Their `CT_DATA_DIR` is this directory. Code runs from an immutable release under
`~/Library/Application Support/agentic-trading/releases/current`, but `config/`, `state/`, `runs/`
and `.env` are read live from here.

Real-money execution is enabled for the `micro_live` bucket.

- Editing `config/*.yaml` changes the running system's behaviour, not just the development copy.
- `state/` holds the ledger, the SQLite audit database and the drawdown high-water marks. Never
  move, rename or hand-edit anything in it. Use `ct-state`.
- Moving the data directory is an operations task: `deploy/macos/release.sh publish --data-dir`,
  with a verified backup first and no unsettled orders. Never do it as part of a refactor.
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
config/              profiles, the mandate, the prompt context — live, read by the running service
state/               ledgers, audit database, tokens — live, untracked except *.example.yaml
runs/                research artefacts and logs — live, untracked
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
