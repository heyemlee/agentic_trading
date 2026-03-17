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
uv run ct-trigger manual --bucket micro_live --ticker VTI   # research now, against real money
uv run ct-trigger scheduled --dry-run             # what the schedule would do, starting nothing
uv run ct-risk-scenarios                          # Hard Risk validation scenarios
uv run ct-execution-scenarios                     # proposal / approval / execution validation
uv run ct-state status                            # integrity, record counts, unsettled orders
uv run ct-state backup --verify                   # consistent SQLite backup, integrity-checked
uv run ct-state sessions                          # persistent console logins and their expiry
uv run ct-state cashflow --bucket core --amount 500   # record a deposit (never a gain)
uv run ct-state confirm --dry-run                 # check the ledgers against the venue, write nothing
uv run ct-state confirm                           # renew as_of where the check agrees
uv run ct-ops status                              # background activity heartbeats and free disk
uv run ct-notify status                           # notification channel; sends nothing
```

`state/trading_state.sqlite3` is the authoritative store for proposals, approvals, orders,
fills, reservations, research jobs, drawdown ladders and portfolio ledgers. The old proposal,
fill and ladder JSON plus portfolio YAML files are imported once and retained as migration
sources. An empty `state/HALT` file is the kill switch. Use `ct-state migrate`, `ct-state status`
and `ct-state backup`; never copy a live SQLite file as a backup.

## Confirming a ledger

`portfolio_state.max_age_days` fails a ledger closed once nothing has confirmed it for that
many days, and the snapshot behind every proposal, the portfolio endpoint and the research run
all stop with it:

```
portfolio state as_of 2026-09-03 is 8 days older than 2026-09-11 (limit 7)
```

That is the guard working. A stretch of NO_TRADE weeks is exactly when it fires, because
nothing executed and so nothing advanced the date on its own. The way out is a check, and the
daily maintenance job (`ct-ops maintain`, 03:45 local, after the backup) runs it on both
ledgers every day, so the date only expires when the check has been refusing for a week. The
same check by hand:

```bash
uv run ct-state confirm --dry-run    # what the check finds, writing nothing
uv run ct-state confirm              # renew as_of on what agreed
```

Every check keeps its report — the job's, the CLI's, the console button's — and a refusal is
notified as a `reconciliation_error`, the venue and a book disagreeing, with the reason. Like
any standing condition it repeats each quiet period until a check agrees, so after putting the
ledger right, confirm it from the console rather than waiting for the next morning's job. A
cash difference names the figure and the `ct-state cashflow` command to record it *if* it was
a deposit or withdrawal; nothing books the difference by itself, because it could as well be
interest, a fee or a loss. The Portfolio screen shows the same refusal with two buttons that
record the figure as what the reader says it was -- money they moved (`deposit`/`withdrawal`,
never return) or income the account earned or was charged (`income`/`fee`, part of the return)
-- on a second tap, then run the check again (`GET /portfolio/ledgers`, `POST
/portfolio/cashflow`, `POST /portfolio/confirm`). `ct-ops status` and the System screen list
`ledger_confirm` beside the other activities, overdue when no check has agreed for two days.

What each book is held against:

* **micro_live** — the account itself. The state is rebuilt from Robinhood's own cash and
  quantities, which refuses a position the ledger cannot attribute to a bucket and cash that
  differs by more than 0.1%. The same rebuild execution uses, so nothing new can be believed
  here that would not be believed at order time.
* **conceptual** (core, sandbox) — a paper book with no account to read back, so its own
  record is the check: every order this system executed for those buckets must already be
  applied to it.

Both refuse while the broker still owns an order — `ct-state status --fail-on-unsettled` asks
the same question the release path asks — and a refusal leaves the date where it was. Nothing
here widens `max_age_days`: raising that limit clears the same error by making the guard stop
looking, on a real-money account, and the ledger is left as unconfirmed as it was.

The Portfolio screen carries the same action, as the fallback for a job that has been refusing.
While a ledger is unconfirmed the strategy figures show a gap rather than a zero — the money
is there, it is the ledger's claim about itself that expired — with the API's own reason
underneath and a **Confirm the ledger** button that posts to `/portfolio/confirm`. That
endpoint calls the same function as the CLI and the job, so the console cannot clear a ledger
they would have refused, and a refusal is printed where the reader asked for it.

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
uv run ct-robinhood probe get_earnings_calendar --args '{"symbols":["AAPL"]}'
                                # one read-only tool's answer, verbatim; get_* and search only
```

Tool names are discovered from the server and matched against candidates in config. A missing
capability raises rather than being assumed, because Robinhood has renamed tools before.

Trading is confined to the dedicated Agentic account, identified by the venue's explicit
`agentic_allowed` flag. Read access spans every Robinhood account; positions from the others
are filtered out rather than treated as tradeable.

Two lines in the log are the venue's server, not a fault here, and are left visible rather than
silenced — a suppressed warning about a cleanup that did not happen is worth less than a noisy
one that is understood:

```
GET    https://agent.robinhood.com/mcp/trading  405 Method Not Allowed
DELETE https://agent.robinhood.com/mcp/trading  400 Bad Request
WARNING mcp.client.streamable_http: Session termination failed: 400
```

The server implements neither the SSE stream nor session termination, so the adapter's close
asks and is refused, and the session lapses on the server's own timeout instead. Closing is
still done at the end of every use: an MCP session left for the interpreter to collect is
terminated from a finaliser after the thread pool that would carry the request is gone, which
reads as `Session termination failed: cannot schedule new futures after shutdown` printed over
whatever the command was reporting.

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

#### Scheduling an account rather than a list of funds

The review above analyses `research_schedule.tickers`, or one representative per core exposure
group when that list is empty. That describes the Core book. It does not describe the Agentic
account, whose holdings are its own, so scheduling it means naming it:

```yaml
research_schedule:
  accounts:                          # absent by default: one Core review, exactly as above
    micro_live:
      holdings:   {period: daily,  max_tickers_per_run: 2}
      candidates: {period: weekly, max_tickers_per_run: 1}
  reserve_for_exits: 1               # of max_analyses_per_day
```

What changes when an account is named:

| | |
| --- | --- |
| Subjects | A `holdings` review reads that account's own ledger: least recently researched first, and anything never researched before those, so the rotation cannot leave one holding unexamined for months. A `candidates` study keeps the configured universe, less whatever the account already holds: a held name is reviewed once, as a holding, and the study's cap reaches the next members of the universe instead. With `holdings` turned off, a held name is not scheduled at all. A holding that has since left the universe is still reviewed, because the two lists are read from different places. |
| Order | Holdings before candidates, across every account. Risk already on the books comes before risk that is not. |
| Budget | `reserve_for_exits` is the part of `max_analyses_per_day` a candidate study may not touch. A holdings review draws on the whole allowance, because a holdings review is how an exit is found. The day's count is re-read between tasks, so two tasks cannot together spend more than the subscription allows. |
| Period keys | Each task claims its own: `daily:micro_live:holdings:2026-09-19`. One account's review can never mark another's as already run, nor a holdings review a candidate study. The single Core review keeps the unscoped key it has always had. |
| The launchd job | Fires as often as the shortest period any account asks for. A daily review invoked only on Fridays would be a weekly review wearing a daily name, so `research_schedule.launchd.weekday` is dropped from the job once any task is daily. Changing this section re-registers the job. |
| Weekends | A daily period is keyed by calendar date, and a non-session day analyses the previous session (`on_non_session_day: previous_session`), so a daily task studies Friday's session again on Saturday and on Sunday. `skip` avoids that for every task, a weekly period for its own. This deployment schedules the Agentic account weekly for that reason. |
| `ct-trigger scheduled` | Runs every configured task. `--bucket micro_live` narrows it to one account's tasks. |
| Purpose | A holdings run is recorded with `purpose: review_holding`, a candidate run with `candidate`, which is what the research record and the queue then carry. |

Not scheduled by any of this: an event-triggered run when verified data arrives. `ct-trigger
event` still exists and is still manual, because the arrival of a *verified* event time is not
something this system can yet detect on its own.

A run that is going nowhere can be stopped from the Research screen or through
`POST /research/{run_key}/cancel`; it otherwise holds a concurrency slot until its timeout. A
run stopped by the subscription limit is requeued automatically once the reset window passes,
at most `max_limit_recoveries` times.

### Approval

Approval authorises an idea, never a stored order. On approval the backend re-reads the venue,
the quote, the portfolio and the risk state, runs Hard Risk again, runs Robinhood's own
pre-trade review, and submits only if all of it still passes. If the world moved, the answer is
`STALE`, `REANALYSIS_REQUIRED`, `INVALIDATED` or a rejection, never a silent send.

Approving is done from the console, on a phone or a browser: Approvals, then the proposal.

The four CLI tools that used to do this -- `ct-proposals`, `ct-approve`, `ct-research` and
`ct-risk-check` -- refuse to run once `execution_policy.stage` is above 2, which it has been
since Stage 3. They are the research-and-recommendation toolkit, and this deployment is past
that. Research is started with `ct-trigger`; the approval itself has one surface, which is the
one that carries a session, a CSRF token and a fixed proposal version.

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
**`true`**, armed 2026-09-08 after `uv run ct-robinhood preflight` came back green — which
additionally requires accepting the documented risk defaults (`accept_all_recommended_defaults`)
or replacing them with explicit values. Turning it **off** is one gesture from the console's
Settings page and applies at once; turning it **on** again from there re-asks the console
password and waits 24 hours, during which any signed-in device is told and can withdraw it (see
[Configuration changes from the console](#configuration-changes-from-the-console)). An edit on
this Mac applies as soon as it is saved, as it always did.

Arming it submits nothing by itself. Every real order still needs a human approval, the
revalidation above, and Hard Risk twice; what the switch changes is that an approval can now
reach the venue instead of stopping at a simulated one. The emergency control from a phone is
`POST /system/pause`, which stops orders immediately and leaves research running.

### Autonomy

Autonomy is built and entirely switched off. Nothing in this section is running today; it is
written down so that arming it is a decision with a checklist rather than a flag someone finds.

Two blocks configure it, and they are independent:

| Config | Capabilities | State |
| --- | --- | --- |
| `execution_policy.autonomy` | `defensive`, `conservative`, `experimental` — the conceptual buckets | all `enabled: false` |
| `buckets.micro_live.autonomy` | `reduce`, `open` — the real-money pair | both `enabled: false` |

`reduce` and `open` are separate switches because they are different acts: one cuts risk, the
other creates it, and the cost of getting each wrong is not the same. Turning one on never turns
on the other, and they count against separate daily allowances. `open` additionally restricts
*what* may be bought to `classes`; an empty list there means refuse, never "no restriction".

No autonomous action bypasses the Hard Risk Layer, and `SELL` is never autonomous — a full close
is always a person's decision.

**The floor under a position.** Halving is bounded per order and per day, and neither of those
stops code from walking the same position down over a week. `reduce.min_remaining_pct_of_opened`
does: it is a share of `position_high_water_quantity`, the most a position ever reached after a
purchase **a person approved**, and that baseline only ever rises while the position lives. A
person buying one share into a position code has already reduced to its floor therefore gives
code that one share back and nothing more; lowering the floor itself is a configuration change,
with the password and the cooling-off period that implies. It is undeclared today, and an
undeclared limit is a refusal: `/config/authority` reports `no_min_remaining` and no reduction
can be autonomous until the percentage is written down. The baseline is replayed from the fills
and the approval records rather than stored in a counter, so a restart cannot lose it; a position
the fills do not account for — transferred in, seeded, or approved before `approved_via` was
recorded — has no baseline and is refused rather than guessed at.

**Arming order.** Each step is a separate decision, and none of them is code work:

1. Positions exist, bought by hand, and their decision records read well.
2. Declare `reduce.min_remaining_pct_of_opened`. Until it is declared, arming `reduce` does
   nothing, which is the intended order: the floor is decided before the switch.
3. Arm `reduce` only. Watch it on real fills until the daily loss gate has been seen to work.
4. Populate `config/event_calendar.yaml` and set `maintained_through`. An autonomous open fails
   closed while that calendar is empty or expired, which is the current state and is deliberate:
   a blackout calendar carrying guessed dates is worse than none.
5. Arm `open` last.

**Stopping.** One gesture, no config editing:

```bash
uv run ct-autonomy status                      # what autonomy may do right now
uv run ct-autonomy stop --note "why"           # halt autonomous action; the note is recorded
uv run ct-autonomy resume --note "why"         # deliberately separate, deliberately manual
```

The stop flag is sticky on purpose: `resume` is a second decision, so that whatever caused the
stop gets looked at before it is cleared.

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
cp config/examples/portfolio.example.yaml state/portfolio.yaml
cp config/examples/micro_live_portfolio.example.yaml state/micro_live_portfolio.yaml
```

Both ledgers are untracked, because they record real positions and real cash. The
repository carries only the all-cash `*.example.yaml` templates.

Optional data keys go in `.env` (see `.env.example`). FRED is free and
improves macro context; nothing else is required.

## Command reference

```bash
uv run ct-api                                  # backend API for the frontend
uv run ct-trigger manual --ticker VTI          # research now, conceptual core book
uv run ct-trigger manual --bucket micro_live --ticker VTI   # research now, real money
uv run ct-trigger scheduled --dry-run          # the schedule's decision, starting nothing
uv run ct-robinhood status|preflight|connect|probe   # the real-money venue; probe reads one tool
uv run ct-risk-scenarios                       # Hard Risk validation scenarios
uv run ct-execution-scenarios                  # proposal, approval and execution validation
uv run ct-state migrate                         # one-time import of legacy state
uv run ct-state backup --verify                 # consistent SQLite backup, integrity-checked
uv run ct-state restore --source <f> --into <d> # restore, revoke sessions, list orders to check
uv run ct-state sessions                        # persistent console logins and their expiry
uv run ct-state cashflow --bucket core --amount 500 --kind deposit
uv run ct-state confirm [--ledger conceptual|micro_live] [--dry-run]
uv run ct-ops maintain                          # rotate logs, prune runs, confirm the ledgers, check disk
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

### Configuration changes from the console

Since 2026-09-17 the console's Settings page (More → Settings) edits `risk_profile.yaml`,
`investor_profile.yaml` and `event_calendar.yaml`. The mandate documents and `.env` are not
offered. Two ways in, one pipeline (`agentic_trading/config_changes.py`):

- **Forms** for the things that change often — research schedule, stock watchlist, macro
  event calendar — set named values; the file is rewritten by a round-trip YAML writer with
  every comment kept, and only the edited lines differ.
- **Section editor** for everything else: one top-level key's text, comment block included,
  replaced verbatim. The key may not be renamed and sections may not be added or removed.

Whichever way, the rebuilt file is loaded by the same `load_profiles` the service runs on
before anything is written; a file the service could not load is refused with the loader's
message. Then every changed value is classified by a rule table as tightening, loosening or
neutral. A change that loosens nothing applies at once. A change that loosens anything — a
higher cap, a switch turned on, a permission granted, a floor lowered, a longer allow-list — is
**parked for 24 hours**: it needs the console password again when submitted, it is announced
to every device as `config_change_pending`, it can be withdrawn from the Settings page until
it applies, and it applies against the file as it is then, failing rather than re-raising a
ceiling from a stale premise if the part it touches was edited meanwhile. The rule table fails
closed: a guarded path whose direction it cannot tell counts as loosening.

Every applied change is committed locally to this checkout (`git log -- config/`), author
`<user> via console`, never pushed; and journaled as `config_changed` in the audit trail with
the paths touched. A schedule change re-registers the research launchd job by itself, without
restarting the API (`launchd.reregister_research`), and reports what it did beside the change.

The same page records deposits and withdrawals (`POST /portfolio/cashflow`, the same call as
`ct-state cashflow`), stops or restores autonomous action (`ct-autonomy stop`/`resume`; restoring
needs the password), and rotates the console password (which signs every device out).

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
| Micro-Live | **real**, funded $100, ceiling $2,000 | Robinhood Agentic | `state/micro_live_portfolio.yaml` |

Micro-Live sizes against what the account actually holds. Every cap in the engine is a
percentage of the bucket's allocation, and for this bucket that allocation follows live equity
rather than a number typed into a file: a deposit widens the caps without a config edit, and —
the half that matters more — a loss narrows them without anyone having to remember to.

What bounds it is `micro_live_execution.max_capital_usd`, **$2,000**, with $50 of tolerance for
market drift above it. Raising that number is the deliberate act that widens the mandate:
lowering it is a phone gesture that applies at once, raising it from the phone re-asks the
password and waits a day. The ceiling is enforced twice, independently — in the bucket's own accounting and
again in the broker adapter (`refuse_if_account_equity_above_usd`) — so one edited number is not
enough to move the boundary everywhere.

Within that, order size is capped per instrument class, as a share of the account and in dollars:

| Tier | Classes | Share of account | Max order |
|---|---|---|---|
| low | broad diversified ETF, bond or cash-like ETF | 60% | $200 |
| mid | sector, factor or thematic ETF | 25% | $80 |
| high | individual stock | 15% | $50 |

Funding the account should always be recorded with `ct-state cashflow`, or the drawdown ladder
reads the deposit as performance.

## Not built, by design

Additional brokers, additional model providers, a backtesting platform, and multi-host high
availability.

Two things that used to be on this list have since been built and are described above: the
evaluation harness and its benchmark comparison, under [Does any of this work?](#does-any-of-this-work),
and autonomy, which exists in code and is switched entirely off, under [Autonomy](#autonomy).
