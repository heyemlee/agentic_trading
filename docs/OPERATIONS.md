# Agentic Trading — operations

A conservative, portfolio-aware wrapper around the open-source
[TradingAgents](https://github.com/TauricResearch/TradingAgents) framework.
Principles: reuse mature upstream software, keep personal preferences in
configuration, use the LLM for intelligence and deterministic code for safety.

Current state: the backend and responsive web console are implemented. Research, policy, risk,
approval, revalidation and the Robinhood connection all work. Real-money execution is disabled
by its own master switch and has never been enabled; the $100 Micro-Live account sits in cash.

Frontend teams start at [`docs/FRONTEND_HANDOFF.md`](docs/FRONTEND_HANDOFF.md).

## What runs

```
Trigger (manual, scheduled or event-driven)
  -> portfolio context: bucket ledger + broker state, drawdown ladders, market regime
  -> TradingAgents (unmodified) on the Claude Max subscription
  -> Conservative Policy: mandate-format proposal, confidence, deterministic action mapping
  -> Hard Risk: deterministic rules; approve or reject
  -> Proposal, with a freshness clock set by the strategy class
  -> Human approval (always required for sandbox momentum / short / event-driven)
  -> Freshness and state revalidation against the current world
  -> Hard Risk again, on the fresh state
  -> Broker: paper order, idempotent by client order id
  -> Audit record + bucket ledger update
```

Approval authorises the idea, never the stored order. Between analysis and approval the
market, the portfolio and the risk state can all change, so execution re-reads them and
reruns Hard Risk. If the world moved too far, the proposal is invalidated or sent back for a
fresh analysis rather than executed.

Other commands:

```bash
uv run ct-proposals                               # open proposals and their freshness
uv run ct-approve <proposal-id> --revalidate-only # show the revalidation, change nothing
uv run ct-approve <proposal-id> --dry-run         # revalidate and stop before submitting
uv run ct-approve <proposal-id>                   # approve, revalidate, then submit a paper order
uv run ct-risk-check VTI --confidence 80          # what-if check, no model call
uv run ct-risk-scenarios                          # Hard Risk validation scenarios
uv run ct-execution-scenarios                     # proposal / approval / execution validation
uv run ct-state status                            # integrity, record counts, unsettled orders
uv run ct-state backup --verify                   # consistent SQLite backup, integrity-checked
uv run ct-state sessions                          # persistent console logins and their expiry
uv run ct-state cashflow --bucket core --amount 500   # record a deposit (never a gain)
uv run ct-ops status                              # background activity heartbeats and free disk
uv run ct-notify status                           # notification channel; sends nothing
```

`state/trading_state.sqlite3` is the authoritative store for proposals, approvals, orders,
fills, reservations, research jobs, drawdown ladders and portfolio ledgers. The old proposal,
fill and ladder JSON plus portfolio YAML files are imported once and retained as migration
sources. An empty `state/HALT` file is the kill switch. Use `ct-state migrate`, `ct-state status`
and `ct-state backup`; never copy a live SQLite file as a backup.

## Brokers

`config/risk_profile.yaml` selects the adapter. Broker code lives only behind
`agentic_trading/broker/`; the research and risk layers never import a broker SDK.

| Adapter | Venue | Money | Use |
|---|---|---|---|
| `simulated` | in-process | none | deterministic validation and offline work |
| `robinhood` | Robinhood Agentic Trading, official MCP | **real** | the Micro-Live experiment |

### Robinhood Agentic Trading

The integration talks to Robinhood's official MCP server at
`https://agent.robinhood.com/mcp/trading` over OAuth. It is used as a **broker transport for
deterministic Python**, never as a tool handed to a model. Robinhood's own framing is that an
AI agent connects and trades directly; that would put a model inside the execution path. Here
the model researches, our code decides, and only the adapter talks to the venue. The research
runtime is started with built-in tools disabled and `--strict-mcp-config`, so no Claude session
in this project can reach a trading tool. Preflight check PF11 asserts that on every run.

```bash
uv run ct-robinhood connect     # one-time OAuth in a browser; stores a local token
uv run ct-robinhood status      # accounts, the Agentic account, bound tools
uv run ct-robinhood preflight   # every gate that must pass before a real order
```

Tool names are discovered from the server and matched against candidates in config. A missing
capability raises rather than being assumed, because Robinhood has renamed tools before.

Trading is confined to the dedicated Agentic account, identified by the venue's explicit
`agentic_allowed` flag. Read access spans every Robinhood account; positions from the others
are filtered out rather than treated as tradeable.

Two venue constraints shape the order path and were verified against the live interface:

- A fractional quantity or a dollar amount is accepted **only on a market order in regular
  hours**. A $100 account cannot buy a whole share of a broad ETF, so fractional is mandatory
  and the market order type follows. The mitigations are Robinhood's own `review_equity_order`
  immediately before placing, our drift check against the analysis price, liquid instruments
  only, and small orders.
- Time in force is `gfd` or `gtc`, and the idempotency key is `ref_id`, a UUID derived
  deterministically from our client order id so a retry carries the same value.

## Operating model

Normal use does not involve opening Claude Code. The backend is the product; Claude Code is
development tooling and, separately, the subscription-backed runtime the backend invokes
headlessly for reasoning.

```
scheduler / frontend / manual trigger
  -> backend (this package)
     -> TradingAgents -> Claude Max          research
     -> Conservative Policy                  mandate-format proposal, confidence
     -> Hard Risk                            deterministic permission
  -> human approval                          required for anything touching money
  -> current-state revalidation              re-read the world
  -> Hard Risk again                         on the fresh state
  -> Robinhood pre-trade review              the venue's own check
  -> execution -> reconciliation -> audit
```

### Running it

```bash
uv sync
uv run ct-auth                         # create or rotate the console password locally
uv run ct-api                          # backend on http://127.0.0.1:8787, schema at /docs
```

For an always-on Mac, phone access, launchd supervision, scheduled research and verified daily
backups, follow [`MACOS_DEPLOYMENT.md`](MACOS_DEPLOYMENT.md).

Bind to localhost only: this process holds the Robinhood token and the Claude session. The
console and API require a password-backed session and CSRF protection. Sessions use hashed
server-side tokens in SQLite and remain valid across API restarts for up to 30 days. For phone access, keep
Uvicorn on loopback and put a private-network or HTTPS reverse proxy in front of it; keep the
frontend and API on the same origin and add that exact HTTPS origin to `CT_ALLOWED_ORIGINS`.
Run one application worker because login rate limits are process-local.

### Triggers and the schedule

Three kinds, all entering the same pipeline:

```bash
uv run ct-trigger manual --ticker VTI          # analyse one security now
uv run ct-trigger scheduled                    # the periodic review
uv run ct-trigger scheduled --dry-run          # what it would decide, starting nothing
uv run ct-trigger event --ticker VTI --reason "earnings release"
```

Scheduling uses the operating system's own scheduler rather than a bundled one. It decides only
*when the command is invoked*; whether that invocation should do anything is decided from
`risk_profile.research_schedule`, so a holiday, a catch-up run after a restart, a repeated
trigger and an exhausted budget all get the same answer whichever entry point fires:

| Situation | What happens |
| --- | --- |
| The day is a market holiday or a weekend | Steps back to the last completed session, or skips outright, per `on_non_session_day`. The exchange calendar decides, not a hand-kept list. |
| Today's session has not closed | Analyses the previous completed session. |
| The period already ran | Skipped. The period key (`weekly:2026-W36`) is claimed *before* any research starts, so a machine that reboots and fires the job again resumes the same period instead of analysing the week twice. |
| Two invocations race | One claims the period; the other records that it lost the race. |
| The day's analyses are spent | Skipped, with the count. `max_analyses_per_day` covers every entry point, manual runs included: the subscription is one pool. |
| The system is paused | **Research still runs.** The kill switch is about orders. Set `run_while_paused: false` to change that. |
| The system is halted, out of Claude budget, or misconfigured | Skipped, naming the status. |

Every skip is recorded with its reason and shown on the console's System screen next to the
next scheduled time, so "why did nothing run on Friday" is answerable from a phone.

A run that is going nowhere can be stopped from the Research screen or through
`POST /research/{run_key}/cancel`; it otherwise holds a concurrency slot until its timeout. A
run stopped by the subscription limit is requeued automatically once the reset window passes,
at most `max_limit_recoveries` times.

### Approval

Approval authorises an idea, never a stored order. On approval the backend re-reads the venue,
the quote, the portfolio and the risk state, runs Hard Risk again, runs Robinhood's own
pre-trade review, and submits only if all of it still passes. If the world moved, the answer is
`STALE`, `REANALYSIS_REQUIRED`, `INVALIDATED` or a rejection, never a silent send.

```bash
uv run ct-proposals                            # what is waiting
uv run ct-approve <id> --revalidate-only       # what would happen, changing nothing
uv run ct-approve <id>                         # authorise, revalidate, then maybe execute
```

### Pausing

```bash
uv run python -c "from agentic_trading.service import Service; Service().pause('reason')"
```

or `POST /system/pause`, or simply create the file `state/HALT`. Remove the file or call
`/system/resume` to release it.

**Pausing stops orders, not research.** Analysis is how the system notices that conditions
changed while it was paused, and a proposal produced during a pause still cannot be executed:
Hard Risk checks the kill switch again at execution time. If you would rather stop everything,
set `research_schedule.run_while_paused: false`; the schedule then records "system is paused"
as its skip reason. One setting, one behaviour, in the code and here.

### Real money

`micro_live_execution.enabled` in `config/risk_profile.yaml` is the master switch and is
`false`. It is deliberately not changeable through the API. Before it can be turned on,
`uv run ct-robinhood preflight` must be green, which additionally requires accepting the
documented risk defaults (`accept_all_recommended_defaults`) or replacing them with explicit
values.

### FRED macro data (optional)

`FRED_API_KEY` in `.env` adds verified inflation, rate, labour and yield-curve context. It is
**optional**. Without it the system records the missing evidence, lowers confidence and prefers
NO_TRADE, which is correct conservative behaviour rather than a fault. Adding the key later
needs no code change.

### Frontend

A frontend calls this backend and nothing else. It must never reach Claude, TradingAgents or
the Robinhood MCP directly, so that policy, risk, approval and revalidation cannot be bypassed.
The complete contract, status vocabulary, security boundaries and sanitized example payloads
are in [`docs/FRONTEND_HANDOFF.md`](docs/FRONTEND_HANDOFF.md).

## LLM layer and billing

The system calls Claude through the official Claude Code CLI in headless mode
(`claude -p`) using your Claude Max login. No Anthropic API key is used or
accepted:

- `agentic_trading/llm/claude_code.py` strips `ANTHROPIC_API_KEY` and
  related variables before every call and never uses `--bare` (which would
  disable subscription login).
- Built-in tools are disabled; the only tools the model can call are
  TradingAgents' own data tools, served over MCP by
  `agentic_trading/llm/ta_tool_server.py`.
- A subscription limit stops the run with a clear message. Rerunning the same
  ticker and date resumes from the upstream checkpoint. There is no fallback
  to pay-as-you-go.

Check your login before running: `claude auth status` should report
`"authMethod": "claude.ai"` and `"subscriptionType": "max"`.

## Setup

```bash
uv sync
cp state/portfolio.example.yaml state/portfolio.yaml
cp state/micro_live_portfolio.example.yaml state/micro_live_portfolio.yaml
```

Both ledgers are untracked, because they record real positions and real cash. The
repository carries only the all-cash `*.example.yaml` templates.

Optional data keys go in `.env` (see `.env.example`). FRED is free and
improves macro context; nothing else is required.

## Command reference

```bash
uv run ct-api                                  # backend API for the frontend
uv run ct-trigger manual --ticker VTI          # research now
uv run ct-research VTI --bucket micro_live     # the same run, directly
uv run ct-proposals                            # open proposals and freshness
uv run ct-approve <id> --revalidate-only       # preview the approval check
uv run ct-approve <id>                         # approve, revalidate, maybe execute
uv run ct-risk-check VTI --confidence 80       # what-if against the portfolio, no model call
uv run ct-robinhood status|preflight|connect   # the real-money venue
uv run ct-risk-scenarios                       # Hard Risk validation scenarios
uv run ct-execution-scenarios                  # proposal, approval and execution validation
uv run ct-state migrate                         # one-time import of legacy state
uv run ct-state backup --verify                 # consistent SQLite backup, integrity-checked
uv run ct-state restore --source <f> --into <d> # restore, revoke sessions, list orders to check
uv run ct-state sessions                        # persistent console logins and their expiry
uv run ct-state cashflow --bucket core --amount 500 --kind deposit
uv run ct-ops maintain                          # rotate logs, prune run artefacts, check disk
uv run ct-notify status|pending|test            # notification channel and delivery record
uv run ct-trigger scheduled --dry-run           # the schedule's decision, starting nothing
deploy/checks.sh                                # lint, tests, frontend cases, secret scan
deploy/macos/selfcheck.sh                       # verify an unattended deployment came back up
```

Each research run writes to `runs/<date>_<ticker>_<id>/`: a `decision_record.json` with the
rating, policy action, confidence, every Hard Risk rule, token usage and artifact paths, plus
the upstream per-agent reports under `reports/`.

## Terminology

These four are deliberately distinct and must never be conflated:

| Term | Meaning | Status |
|---|---|---|
| Research run on real market data | The model analyses current market data. No broker, no orders, no money. | Stage 2, what runs today |
| Paper trading | Simulated orders with durable order plumbing. Fake money. | Built |
| Micro-Live | Up to $100 of real capital at Robinhood, genuinely pursuing risk-adjusted return. Not disposable test money. | Built, disabled |
| Live trading | Normal real-money trading at full size. | Not planned for the near term |

A research run that reads today's prices is still a research run. It is never called paper
trading or live trading.

## Strategy policy

Core is long-only, conservative and diversified. Short selling, margin, leverage, momentum
trading as a thesis, and event-driven strategies are rejected deterministically by rule HR15,
not by prompt wording. Momentum may inform timing, volatility and risk only. A naked short is
structurally impossible: a SELL or REDUCE is only accepted against a position that already
exists (HR14).

Sandbox may in a later stage research momentum, short and event-driven strategies. Each is
separately permissioned and all are currently disabled. Independently of any permission, every
one of them is permanently stamped `requires_human_approval` by the deterministic layer, so no
future autonomy setting can execute one without an explicit confirmation.

## Configuration

- `config/investor_profile.yaml`: objectives, capital buckets, universe, LLM and upstream settings
- `config/risk_profile.yaml`: execution stage and modes, limits, drawdown ladder, permissions
- `config/mandate_context.md`: compact mandate injected into every agent prompt
- `config/MANDATE.md`: full mandate used by the policy step

Fields marked `value: TODO` carry a `recommended_default` and `reasoning`.
Research runs work with TODOs present; execution stages refuse to start until
every TODO is resolved (or `accept_all_recommended_defaults: true`).

## Tests

```bash
uv run pytest
```

The suite runs offline against `tests/fake_claude.py`, which fails if an API
key ever reaches the CLI or built-in tools are left enabled.

## Money paid in is not money made

Each account reports its funding and its result separately, because a system that shows only
equity turns a deposit into a return the moment it lands.

- **Opening allocation** is the bucket's configured capital.
- **Recorded flows** are deposits and withdrawals since, entered with
  `ct-state cashflow --bucket core --amount 500 --kind deposit`. They move cash and funding by
  the same amount and never touch the result.
- **Money paid in (net)** is the two added together, and the **investment result** is equity
  minus that. The **return** is stated against money paid in, never against equity.

The console reports these per account, never across accounts: the conceptual core/sandbox book
and the real-money Micro-Live account keep separate ledgers and separate high-water marks, so a
combined total cannot be compared against either mark. Where the portfolio screen does show a
combined figure it says, on the same line, that it spans two kinds of money.

## Unattended operation

Every background activity records when it last succeeded, and the System screen compares that
against how often it is expected. An activity that has *never* run counts as overdue, because a
job that was never loaded looks exactly like a healthy idle one until the day it is needed.

| Activity | Runs | Expected |
| --- | --- | --- |
| `reconciliation` | in the API process, every 15s | every 5 min |
| `research_dispatch` | in the API process, every 15s | every 5 min |
| `backup` | `backup_state.sh`, 03:15 local | daily |
| `maintenance` | `maintenance.sh`, 03:45 local | daily |
| `schedule` | `run_scheduled_research.sh` | weekly |

`maintenance` rotates the launchd logs (which otherwise append forever), drops report bodies
from runs past `operations.runs_retention_days` while keeping every `decision_record.json`, and
exits non-zero when free disk falls under `low_disk_free_gb`.

## Notifications

Nothing is sent while `notifications.channel` is `none`, which is the default. Events are still
collected, deduplicated and recorded, so the delivery record can be reviewed with
`ct-notify status` — but choosing a channel and supplying its credentials is how sending is
authorised, and that decision is not made in code.

A message says what happened and which screen to open. It never carries a credential, an
account number or a quote, because it travels through infrastructure this system does not
control. A standing condition (an unapproved proposal, a broken credential) notifies once per
`min_interval_seconds` rather than on every poll; a failing endpoint is retried with backoff and
then given up on.

Nothing running on this Mac can report that this Mac is gone. Set `CT_HEARTBEAT_URL` to an
external watcher that expects a regular ping and alerts on its absence; that is the only path by
which a power cut or a network outage reaches a phone.

## Does any of this work?

The test suite proves the software behaves. It says nothing about whether multi-agent research
is worth its cost, and no amount of engineering evidence ever will. Only a continuous sample,
declared before it runs, can answer that.

```bash
uv run ct-evaluate start --note "why this study exists"   # once, freezing the rules
uv run ct-evaluate snapshot                                # daily; the maintenance job does it
uv run ct-evaluate report                                  # any time
uv run ct-evaluate stop --reason "..."                     # ends it; keep the report
```

Three properties make the record worth reading.

**It is declared before it runs.** Paper capital, universe, schedule, risk profile and model
settings are fingerprinted when the study starts, and every report says whether that
fingerprint has changed since. Choosing the rules after seeing the outcome is how a strategy is
talked into looking good; a drifted sample is reported as not homogeneous rather than quietly
averaged.

**It counts what the system did not do.** NO_TRADE decisions, rejected and expired proposals,
failed runs, runs stopped by the subscription limit, and the share of runs that concluded on
incomplete evidence are all in the report. A record of trades alone would describe only the
occasions this system chose to act — a biased sample of its own judgement.

**It excludes deposits from return.** Both the strategy and the benchmark are converted to a
unit value that stays continuous across a cash flow, so money paid in never registers as a gain
and a deposit made while under water does not repair the drawdown. The benchmark is bought once
at the study's start date, held, and receives the same deposits on the same days at those days'
prices, which is what makes the two returns comparable at all.

The report also carries what it cost: model calls, research seconds, the median minutes between
a proposal appearing and a person deciding, turnover, and realised slippage in basis points
against the price the analysis used. `observation_days` is how long before the record is worth
reading; `minimum_days_for_judgement` is how long before it can support a judgement. Until then
every report says so in its own text.

Stop rules are declared in `evaluation.stop_rules` and reported when they trigger. They do not
halt anything by themselves: deciding to stop is a person's call, and the point of declaring the
threshold in advance is to make that call before the loss has had time to argue with it.

## Capital boundaries

Three buckets, three ledgers, three drawdown ladders. They never fund each other.

| Bucket | Capital | Venue | Ledger |
|---|---|---|---|
| Core | $2,500 conceptual | none | `state/portfolio.yaml` |
| Sandbox | $500 conceptual, max | none | `state/portfolio.yaml` |
| Micro-Live | **$100 real**, hard cap | Robinhood Agentic | `state/micro_live_portfolio.yaml` |

The $100 boundary is enforced in four independent places: the bucket allocation the sizing
rules work from, the deployed-cost cap (HR07c), a live check of the Agentic account's equity
before every order, and an outright refusal to trade if that account holds more than $150.

## Not built, by design

Evaluation harness and benchmark comparison, autonomy of any kind, additional brokers,
additional model providers, backtesting platform, and multi-host high availability.
