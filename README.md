<div align="center">

<img src="ui/icons/mark.svg" width="64" height="64" alt="" />

# Agentic Trading

**Multi-agent research proposes trades; deterministic code decides whether they happen.**

A trading system built on the open-source TradingAgents graph. Twelve LLM agents research a ticker through a
locked-down Claude Code runtime, plain-Python policy and risk rules turn their verdict into a proposal or refuse
it, a person approves its exact terms in a phone-first console, and execution checks everything again before the
one adapter that reaches the broker.

[![Python](https://img.shields.io/badge/Python-3.12-3776AB?style=flat-square&logo=python&logoColor=white)](pyproject.toml)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.141-009688?style=flat-square&logo=fastapi&logoColor=white)](pyproject.toml)
[![TradingAgents](https://img.shields.io/badge/TradingAgents-LangGraph-1C3C3C?style=flat-square&logo=langchain&logoColor=white)](pyproject.toml)
[![Claude Code](https://img.shields.io/badge/Claude_Code-CLI_%2B_MCP-D97757?style=flat-square&logo=claude&logoColor=white)](agentic_trading/llm/claude_code.py)
[![macOS](https://img.shields.io/badge/macOS-launchd-000000?style=flat-square&logo=apple&logoColor=white)](deploy/macos)
[![CI](https://img.shields.io/badge/CI-GitHub_Actions-2088FF?style=flat-square&logo=githubactions&logoColor=white)](.github/workflows/checks.yml)

**English** · [简体中文](README.zh-CN.md)

[Highlights](#highlights) · [Architecture](#architecture) · [Tech stack](#tech-stack) · [Features](#features) · [Develop](#develop)

</div>

<p align="center">
  <img src="docs/screenshots/research.webp" width="960" alt="Researching VTI in the Lab for the Agentic account: the pixel office's desks take the run in turn, from the market and news analysts through the bull and bear debate and the trader to the risk debate and the portfolio manager; the deterministic rules then record Buy, Approve and confidence 78, and the proposal opens in Approvals, where all 26 of its Hard Risk rules pass and a person approves $200.00 behind a second confirmation and sees the filled order" />
</p>

<p align="center"><sub>A made-up account, research run and proposal, served by a mocked API. Office art from <a href="ui/assets/lab/pixel-agents/NOTICE.md">Pixel Agents</a> (MIT).</sub></p>

## Highlights

- **Models research; code decides.** Twelve TradingAgents roles on LangGraph debate each ticker. Their rating
  becomes an order only through a deterministic policy step and 39 hard-risk rules in plain Python, all evaluated
  every time and failing closed on any exception or missing input; 18 of 18 offline risk scenarios pass.
- **Approval binds terms; execution re-checks the world.** A person approves one proposal version at fixed terms.
  Before the order, price, cash, positions, session, drawdown and regime are read again and Hard Risk runs again;
  17 of 17 execution cases pass, including a double submission that sends one order.
- **A model runtime that cannot trade.** The Claude Code CLI runs as a LangChain chat model with its built-in tools
  off and `--strict-mcp-config`, so agents see only MCP data tools. Robinhood's official MCP server is a transport
  for deterministic Python, and a preflight check refuses real money if research could reach a broker tool.
- **Automation earns real money in simulation.** A plan a person confirms runs first in a simulated account on the
  same code path, with its own database and a venue that cannot be the real one. A mode needs 30 valid simulated
  sessions before real money; a new strategy replaces the running one only after 180 and one comparison.
- **One order path, tested offline.** 1,875 pytest tests and 231 frontend cases run with no venue, credential or
  model call, and an AST test keeps `service/approvals.py` the only live module that can place an order.

## Architecture

<p align="center">
  <img src="docs/architecture.svg" width="960" alt="The console reaches a FastAPI service over HTTPS; the service queues research, and the TradingAgents graph runs twelve agents through the Claude Code CLI, whose only tools are read-only MCP data tools inside a boundary with no broker tools; the decision passes the policy step and Hard Risk to become a proposal, which reaches the console by Web Push; a person approves its exact terms, and execution re-reads the world, runs Hard Risk and 15 preflight checks again and is the only path to Robinhood Agentic Trading or a simulated venue, with every job, proposal, order, fill and ledger kept in SQLite in WAL mode; pulses follow one request in that order" />
</p>

A run starts from the console or the weekly launchd job and waits in a SQLite queue. A worker runs the
TradingAgents graph through the Claude Code CLI; the Portfolio Manager's rating passes the policy step and the
Hard Risk Layer and becomes a proposal, or a recorded reason why not. Web Push brings it to the console, and an
approval goes through `service/approvals.py`, where execution reads the world again, reruns Hard Risk and 15
preflight checks, and only then calls the venue.

- **Unknown beats twice.** The order intent and its client order id are written before submission. A timeout or
  an unreadable answer leaves the order UNKNOWN for reconciliation against the venue, never a blind resubmit, and
  a partial fill stays partial.
- **Nothing works offline.** The service worker handles push only and has no fetch handler, so the console never
  shows a cached price and an approval can never be queued and replayed against a market that has moved.

## Tech stack

| Layer         | Technology                                                       | Role                                                                                                                           |
| ------------- | ---------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| Agents        | TradingAgents (pinned commit) · LangGraph · LangChain            | The twelve-role research graph, unmodified; this repo adds portfolio context, analysts chosen per instrument and model routing |
| Model runtime | Claude Code CLI · MCP Python SDK                                 | A LangChain chat model over `claude -p`; TradingAgents' data tools, SEC Form 4 filings and an allowlisted fetch over MCP       |
| Decisions     | Python 3.12 · Pydantic 2                                         | Policy mapping, 39 hard-risk rules, drawdown ladders, market regime, sizing and revalidation                                   |
| API           | FastAPI · Uvicorn                                                | 77 routes over one `service/` layer that the CLI and the scheduler share                                                       |
| State         | SQLite (WAL) · YAML · JSON                                       | Jobs, proposals, approvals, orders, fills and ledgers in one transactional store; reviewed settings in `config/`               |
| Data, venue   | yfinance · FRED · SEC EDGAR · exchange-calendars · Robinhood MCP | Research evidence, NYSE sessions, the live venue behind deterministic code, and a simulated venue                              |
| Console       | HTML · CSS · vanilla JavaScript · Web Push                       | 14 pages with no build step, in English and Chinese, installable as a home-screen app                                          |
| Operations    | macOS launchd · zsh · uv                                         | Four jobs, immutable releases with rollback, encrypted backups and heartbeats                                                  |
| Quality       | pytest · Ruff · node:test · Playwright                           | The offline suite, frontend cases and browser checks against synthetic APIs                                                    |

## Features

### Research, risk and approval

- **Research about the account.** Each run carries the bucket's holdings, cash, drawdown level and market regime.
  An ETF seats the market and news analysts, a stock all four; every model and tool call is kept, and the Lab
  draws the run desk by desk.
- **Rules that only narrow.** The policy step maps the rating to an action and a size. Hard Risk then checks cash,
  position and order caps, exposure overlap, price and evidence freshness, blackouts and daily limits, and stores
  each result with the figures behind it. A confident rating never widens a limit.
- **Approval for exact terms.** A proposal expires on its strategy's clock: two days for a core holding, 45 minutes
  for an event-driven idea. Approving binds its version and terms behind a second confirmation, and a change of
  authority asks again.

From the two offline runners, `ct-risk-scenarios` (18 scenarios) and `ct-execution-scenarios` (17 cases), each one
as specified:

| Hard Risk scenario                            | Verdict | Failed rules        |
| --------------------------------------------- | ------- | ------------------- |
| Diversified ETF buy within every limit        | Approve | —                   |
| Second fund in an exposure group already held | Reject  | HR09                |
| Stock past the sleeve and sector caps         | Reject  | HR10 · HR11 · HR11b |
| Sandbox buy that would need core's cash       | Reject  | HR07 · HR07c        |
| Market regime data unavailable                | Reject  | HR03                |
| Risk-reducing sell during a hard halt         | Approve | —                   |

| Execution case                               | Outcome          | Orders sent |
| -------------------------------------------- | ---------------- | ----------: |
| Approved and still valid                     | SUBMITTED        |           1 |
| Price moved 5% against a 3% band             | REANALYZE        |           0 |
| Drawdown ladder escalated since the analysis | INVALIDATED      |           0 |
| Hard Risk passed at proposal time, fails now | REJECTED         |           0 |
| The same approval executed twice             | ALREADY_EXECUTED |           1 |
| Claude usage limit during the analysis       | NO_PROPOSAL      |           0 |

The cases are synthetic, and the pytest suite runs the same ones. They show that the software behaves as specified,
not that the strategy makes money; only the forward record below can speak to that.

### Automatic trading, risk and the console

<p align="center">
  <img src="docs/screenshots/console.webp" width="960" alt="The Agentic account on Home holding the VTI just bought, with automatic buying and selling switched off; the automatic-trading page with a steady plan 12 of 30 valid sessions into its simulation and the reasons no mode is open for real money yet; then the Risk page's market regime, drawdown ladder and real-money preflight checks" />
</p>

<p align="center"><sub>The same made-up account and mocked API.</sub></p>

**Automatic trading**

- A person confirms the envelope once: the mode, the money it may use and the loss at which it stops opening. Code
  tunes the strategy only inside it, and every clamp is recorded.
- The simulated account runs the real account's rules, Hard Risk and execution code against its own database, and
  its fills pay an assumed spread and slippage plus the venue's published fees.
- Backtests decide on closes that already happened and fill at the next open; a test checks that changing any bar
  after a date changes nothing on or before it.

**Risk and real money**

- Each bucket has its own ledger and drawdown ladder, with hysteresis; a hard halt is sticky until a person resets
  it with a written note.
- A real-money order needs all 15 preflight checks, and the capital ceiling is enforced twice: in the bucket's
  accounting and in the broker adapter.
- Openings into a company's earnings report are blocked, as are autonomous ones on FOMC and CPI days; an empty
  `state/HALT` file blocks every new position.

**Console and settings**

- Three tabs: Home for the accounts and what needs a person, Lab for research, Me for records and settings. Every
  reading shows when it was taken, and a stale one says so.
- Settings edits `config/*.yaml`: it previews each change, asks for the console password and waits 24 hours before
  anything that loosens a limit, then commits the file to git.
- Web Push to the home-screen app, encrypted end to end, for a waiting proposal, a failed run or a risk-level
  change.

**Forward evaluation**

- A study fingerprints capital, universe, schedule, risk profile and models when it starts, and every report says
  whether they have drifted.
- It counts NO_TRADE decisions, rejected and expired proposals and failed runs, not only trades.
- Returns are measured in unit values, so deposits never read as gains, against a benchmark bought once that
  receives the same deposits.

## Develop

```bash
uv sync --frozen                         # Python 3.12 and the locked dependencies
mkdir -p state runs                      # untracked: ledgers, the audit database, run artefacts
cp .env.example .env                     # optional: FRED key, push contact, heartbeat URL
uv run ct-auth                           # set the console's owner passphrase
uv run ct-api                            # API and console at http://127.0.0.1:8787/ui/
uv run ct-trigger manual --ticker VTI    # queue one research run on the signed-in Claude Code CLI
```

Before the first `ct-api`, copy the two all-cash starting ledgers from `config/examples/` into `state/`
([setup](docs/OPERATIONS.md#setup)). `/ui/index.html?demo=1` shows the console on fixtures and makes no API calls.
Research needs a signed-in Claude Code CLI; never set `ANTHROPIC_API_KEY`, which the adapter strips anyway.
`config/` holds a working deployment's reviewed settings and a fresh clone has no broker token: read
[docs/OPERATIONS.md](docs/OPERATIONS.md) before connecting an account.

## Test

```bash
deploy/checks.sh                         # Ruff, 1,875 pytest tests, 231 frontend cases, secret scan
uv run ct-risk-scenarios                 # the 18 Hard Risk scenarios as a table
uv run ct-execution-scenarios            # the 17 proposal-to-order cases
node tools/check_navigation.cjs          # console navigation in a mobile browser on a synthetic API
```

No test opens a venue, a credential or a model, and `tests/conftest.py` fails any test that touches the real state
database. The CI workflow and the `deploy/hooks/pre-push` hook run the same script; the hook also reads the stored
documents through the API's response models. The browser checks need Playwright.

## Deploy

```bash
deploy/macos/install.sh                  # the four launchd jobs: API, weekly research, backup, maintenance
deploy/macos/release.sh publish          # export HEAD as an immutable release and switch to it
deploy/macos/release.sh rollback         # point current back at the previous release
uv run ct-state backup --verify          # a consistent SQLite backup, integrity-checked
deploy/macos/selfcheck.sh                # read-only: did everything come back after a restart
```

Releases, off-machine backups and phone access are in [docs/MACOS_DEPLOYMENT.md](docs/MACOS_DEPLOYMENT.md);
approvals, autonomy and recovery in [docs/OPERATIONS.md](docs/OPERATIONS.md); the console's design in
[ui/README.md](ui/README.md).
