# Conservative AI Trading System

A conservative, portfolio-aware wrapper around the open-source
[TradingAgents](https://github.com/TauricResearch/TradingAgents) framework.
Principles: reuse mature upstream software, keep personal preferences in
configuration, use the LLM for intelligence and deterministic code for safety.

Current state: **backend complete, frontend not started**. Research, policy, risk, approval,
revalidation and the Robinhood connection all work. Real-money execution is disabled by its own
master switch and has never been enabled; the $100 Micro-Live account sits in cash.

Frontend teams start at [`docs/FRONTEND_HANDOFF.md`](docs/FRONTEND_HANDOFF.md).

## What runs

```
Trigger (manual today; scheduled or event-driven later)
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
uv run ct-scenarios                               # Hard Risk validation scenarios
uv run ct-stage3                                  # proposal / approval / execution validation
```

State files: `state/portfolio.yaml` is the bucket ledger (one symbol belongs to one bucket;
the sandbox never draws on core cash). From Stage 3 the broker is the source of truth for
cash, quantities and prices, and the ledger supplies the bucket attribution the broker does
not have; execution updates it after every fill. `state/risk_state.json` holds the ladder
(high-water marks, level, manual-reset flag), `state/proposals.json` the proposal lifecycle,
and `state/fills.jsonl` one line per fill. An empty `state/HALT` file is the kill switch.

## Brokers

`config/risk_profile.yaml` selects the adapter. Broker code lives only behind
`conservative_trading/broker/`; the research and risk layers never import a broker SDK.

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
uv run ct-api                          # backend on http://127.0.0.1:8787, schema at /docs
```

Bind to localhost only: this process holds the Robinhood token and the Claude session, and has
no authentication because it is a single-user local service.

### Triggers

Three kinds, all entering the same pipeline:

```bash
uv run ct-trigger manual --ticker VTI          # analyse one security now
uv run ct-trigger scheduled                    # periodic review, one per exposure group
uv run ct-trigger event --ticker VTI --reason "earnings release"
```

Scheduling uses the operating system's own scheduler rather than a bundled one. A weekday
review after the close, via crontab:

```
30 16 * * 1-5 cd /path/to/agentic_trading && .venv/bin/ct-trigger scheduled
```

A trigger firing into a paused, halted or usage-limited system does nothing and says so, rather
than queueing work.

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
uv run python -c "from conservative_trading.api.service import Service; Service().pause('reason')"
```

or `POST /system/pause`, or simply create the file `state/HALT`. Research continues; no order
can be submitted. Remove the file or call `/system/resume` to release it.

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

- `conservative_trading/llm/claude_code.py` strips `ANTHROPIC_API_KEY` and
  related variables before every call and never uses `--bare` (which would
  disable subscription login).
- Built-in tools are disabled; the only tools the model can call are
  TradingAgents' own data tools, served over MCP by
  `conservative_trading/llm/ta_tool_server.py`.
- A subscription limit stops the run with a clear message. Rerunning the same
  ticker and date resumes from the upstream checkpoint. There is no fallback
  to pay-as-you-go.

Check your login before running: `claude auth status` should report
`"authMethod": "claude.ai"` and `"subscriptionType": "max"`.

## Setup

```bash
uv sync
```

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
uv run ct-scenarios                            # Hard Risk validation scenarios
uv run ct-stage3                               # proposal, approval and execution validation
```

Each research run writes to `runs/<date>_<ticker>_<id>/`: a `decision_record.json` with the
rating, policy action, confidence, every Hard Risk rule, token usage and artifact paths, plus
the upstream per-agent reports under `reports/`.

## Terminology

These four are deliberately distinct and must never be conflated:

| Term | Meaning | Status |
|---|---|---|
| Research run on real market data | The model analyses current market data. No broker, no orders, no money. | Stage 2, what runs today |
| Paper trading | Simulated orders on a broker's paper account. Fake money, real order plumbing. | Stage 3, not built |
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
- `MANDATE.md`: full mandate used by the policy step

Fields marked `value: TODO` carry a `recommended_default` and `reasoning`.
Research runs work with TODOs present; execution stages refuse to start until
every TODO is resolved (or `accept_all_recommended_defaults: true`).

## Tests

```bash
uv run pytest
```

The suite runs offline against `tests/fake_claude.py`, which fails if an API
key ever reaches the CLI or built-in tools are left enabled.

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

Frontend, evaluation harness and benchmark comparison, autonomy of any kind, additional brokers,
additional model providers, backtesting platform. The trading engine is deliberately finished;
further work belongs in the interface, not in more strategy machinery.
