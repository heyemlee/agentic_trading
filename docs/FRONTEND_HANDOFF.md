# Frontend Handoff

Everything a frontend team needs to build the interface for this system, without reading the
trading engine. Written for an implementation agent such as OpenDesign.

- **Backend contract**: `http://127.0.0.1:8787/openapi.json` (start it with `uv run ct-api`)
- **Interactive schema**: `http://127.0.0.1:8787/docs`
- **Example payloads**: [`ui/assets/data.js`](../ui/assets/data.js) — sanitized, rendered by the console with `?demo=1`
- **Status vocabulary**: [`agentic_trading/service/status.py`](../agentic_trading/service/status.py)

---

## 1. Product overview

A conservative, personal investment system for one investor. An AI research pipeline studies a
security, a deterministic risk layer decides what is permitted, and the investor approves
anything that touches money.

Three pools of capital, deliberately isolated:

| Bucket | Capital | Real money | Venue |
|---|---|---|---|
| Core | $2,500 | No, conceptual | none |
| Sandbox | $500 max | No, conceptual | none |
| Micro-Live | **$100 hard cap** | **Yes** | Robinhood Agentic account |

Two ideas shape every screen:

1. **NO_TRADE is a success.** The system is judged on capital preservation, not on activity. A
   dashboard showing "no recommendation today" is the system working, not idling. Never design
   an empty state that implies something is broken or that nudges the investor to trade.
2. **Approval is conditional.** Approving a proposal means *"act on this idea if conditions
   still hold"*, never *"send the stored order"*. Between analysis and approval the price,
   portfolio and risk state can all move; the backend re-reads them and may refuse. The UI must
   be able to say so clearly.

Currently real-money execution is **disabled** and the $100 sits in cash. Every screen must be
buildable in that state.

---

## 2. User flows

**Open the dashboard.** `GET /health`, `GET /portfolio`, `GET /proposals`, `GET /micro-live`.
Show system status, total equity per bucket, anything awaiting approval, and any warnings.

**View the portfolio.** `GET /portfolio`. Three buckets, each with cash, positions, sleeve
weights. Mark Micro-Live visibly as real money.

**See current recommendations.** `GET /proposals`. Usually empty. When empty, show the last
research outcome from `GET /research` so the investor knows the system ran and chose NO_TRADE.

**Open a proposal.** `GET /proposals/{id}` returns the full record: thesis, bull case, bear
case, primary risks, what would make it wrong, why doing nothing may be better, missing
information, and every Hard Risk rule with its verdict.

**Check before approving.** `GET /proposals/{id}/revalidate` runs the full current-state check
and changes nothing. Recommended as a preview step before showing the approve button.

**Approve.** `POST /proposals/{id}/approve`. The response says what actually happened; it may
be `EXECUTED`, or `STALE`, `REANALYSIS_REQUIRED`, `INVALIDATED`, `HARD_RISK_REJECTED`,
`BROKER_REVIEW_REJECTED`. Never render "done" before reading `status`.

**Reject.** `POST /proposals/{id}/reject` with an optional reason.

**Ask for fresh research.** `POST /proposals/{id}/reanalyze` marks the proposal and starts a
new run; poll `GET /research/{run_key}`.

**See a stale proposal.** Any proposal whose `freshness.state` is `STALE` or `EXPIRED`. Show
the age against the TTL and explain that approval will trigger revalidation, not a blind send.

**See NO_TRADE.** `GET /research` rows with `final_action: "NO_TRADE"`. Present the reasoning
as a legitimate decision with its own detail view.

**Order status.** `GET /orders` returns recorded fills and the orders this system is tracking.

**History and audit.** `GET /research` for decision records; each proposal carries a `history`
array of timestamped events.

**Pause the system.** `POST /system/pause` engages the kill switch: research continues, no
order can be submitted. `POST /system/resume` releases it.

---

## 3. Information architecture

Sections the backend actually supports. Visual design and navigation are yours.

| Section | Primary calls |
|---|---|
| Dashboard | `/health`, `/portfolio`, `/proposals`, `/micro-live` |
| Portfolio | `/portfolio`, `/risk` |
| AI Research | `/research`, `/research/{run_key}`, `POST /research` |
| Approval queue | `/proposals`, `/proposals/{id}`, approve / reject / reanalyze |
| Risk | `/risk`, `/micro-live` (preflight gates), Hard Risk rules inside a proposal |
| Orders and activity | `/orders` |
| History and audit | `/research`, proposal `history` |
| System and settings | `/health`, `/system/claude`, `/system/market`, pause / resume |

---

## 4. Backend interface

Base URL `http://127.0.0.1:8787`. JSON in, JSON out. Errors are `{"detail": "..."}` with 400
for a product-level refusal and 500 for an unexpected fault.

### Read

| Method | Path | Returns |
|---|---|---|
| GET | `/health` | system status, market session, Claude state, warnings |
| GET | `/system/claude` | subscription runtime state, calls today |
| GET | `/system/market` | session open/closed, next open or close |
| GET | `/system/schedule` | next scheduled research, the last run, and why a run was skipped |
| GET | `/system/operations` | background activity heartbeats, free disk, retention windows |
| GET | `/system/notifications` | notification channel, what is enabled, delivery record |
| GET | `/attention` | what still needs a person, computed from unresolved state on every call: `items[]` in the order to take them (`priority` 1 first, then `severity`), each with a stable `id`, a closed `kind`, `requires_person`, the account it concerns, `since`, a `link` as `{page, params}` and the `actions` a screen may offer; `top` is the first item or `null`; `readable: false` with `unreadable[]` means a source could not be read, and a screen must then say it cannot confirm rather than "nothing to do". Never derived from what was sent or from the journal |
| GET | `/config/authority` | what may happen to the real-money account, from every rule that decides it: per action (`BUY`, `ADD`, `REDUCE`, `SELL`) the mode, what blocks a person and what blocks code, the limits; pending changes; the authority `version` and its `history`. Computed without the venue: `preflight` is the reading `/micro-live` keeps, or `ready: null` with `stale: true` |
| GET | `/auth/sessions` | devices signed in: opaque id, device label, created/last-used/expiry |
| GET | `/accounts` | the account scopes a screen may bind to, for the switcher and for checking a deep link: the Agentic account from the kept portfolio and each observed personal account plus one `all_personal` entry listing its members, each with `scope`, `account_scope_kind`, `account_id`, `account_ids`, `account_label`, `as_of`, `complete`; `personal_snapshot` says when the personal accounts were last read and whether that is stale. Opens no venue session; the Demo book is not offered |
| GET | `/advisor?scope=&account_id=` | the assistant screen for one account scope (`personal`, the default, optionally one `account_id`; or `agentic`). The server decides what belongs to it: a personal account sees its own records, jobs and conversations and the all-accounts reviews that covered it; `agentic` carries an empty `snapshot` and only the real-money account's records. Adds `scope`, `account_scope_kind`, `account_id`, `account_ids`, `account_label`, `latest_review` (the newest finished portfolio review for the scope, with `stale` against the snapshot now, or `null`), `coverage_info` and `conversation_summaries`; `coverage` and `conversations` keep their old types |
| GET | `/evaluation?scope=` | the forward record against a passive benchmark, including what it did not do. `scope` is `demo` (the paper book, the default), `agentic` (the real-money account, its own study from its own start) or `route_quality` (what the record says about each research route: timing, calls, failures, evidence check; fact accuracy and human edits are `null` until the fixed-case review runs, never zero). Each scope counts only its own buckets' decisions and fills. A scope with no declared study answers 200 with `state: NOT_STARTED`, the record's counts and `null` performance, never an error. `sample` says how continuous the record is: `gaps[]` in exchange sessions and `stale` when it fell more than two sessions behind |
| GET | `/activity?kind=&subject=&cursor=&limit=` | the audit journal, newest first, with `next_cursor` |
| GET | `/portfolio` | all buckets, positions, cash, sleeves |
| GET | `/portfolio/ledgers` | the last ledger check's report, refusal figures (`cash_difference_usd`) included; opens no venue session |
| GET | `/risk` | drawdown ladders and market regime |
| GET | `/micro-live` | real-money status, preflight gates, capital boundary |
| GET | `/proposals?include_closed=` | proposal list with product status and freshness |
| GET | `/proposals/{id}` | full proposal: research, bull/bear, Hard Risk rules, history |
| GET | `/proposals/{id}/revalidate` | dry-run of the approval check; changes nothing |
| GET | `/research?scope=&account_id=&task_type=&include_all_personal=&limit=` | completed decision records, newest first, each bound to the account it is about: `scope` (`agentic`, `demo`, `personal`, or `unspecified` for a record that predates buckets), `account_scope_kind` (`single`, `all_personal`, `unknown`), `account_id`, `account_ids`, `account_label`, `task_type`, `purpose`, `route` (`name`, `inferred`, versions, models), `authority_version`; what the facts are as of (`snapshot_as_of`, `prices_as_of`, `sources_retrieved_at_max`; `facts_as_of` stays `null` until sources carry their own event time), `evidence`, `limitations`, `counter_evidence` (600 chars, `counter_evidence_truncated`; the full bear case is in the trace), `superseded_by` (a newer record of the same kind about the same thing; chat and lightweight checks never supersede a full analysis), `applicable_floor` (the confidence a run in that scope needs to open; `null` for personal research). `account_id` narrows to that account's own records; `include_all_personal=true` adds the all-accounts reviews that covered it |
| GET | `/research/jobs?scope=&account_id=&limit=` | persistent queue, including queued/running/failed jobs, each with the same `scope`/account/`task_type`/`route` binding, `purpose`, `request_id`, `authority_version`, `opened_at` and `cancellable` |
| GET | `/research/{run_key}` | one run's progress |
| GET | `/orders?limit=` | recorded fills and tracked orders, from the local record |

### Write

The approval screen captures the proposal and current authority versions together. Its detail's
`authority_changed.to` (or `authority_version` when unchanged) must match the authority reading.
Loosened, mixed and unknown changes require a separate unchecked acknowledgement. A changed or
unreadable authority invalidates an open confirmation. A 409 refreshes the authority and proposal
for another explicit review; it never retries the write. Transport failures still require checking
the order state. Ship this UI together with the backend authority checks, so older proposals can
be reviewed and acknowledged.

| Method | Path | Body | Effect |
|---|---|---|---|
| POST | `/research` | `{ticker, bucket, instrument_type?, trade_date?, trigger?, scope?, request_id?, purpose?}` | enqueues a durable run; an identical active request returns its existing `run_key`. `scope` (`agentic` or `demo`) must hold `bucket` or the request is refused (400). `request_id` (UUID) makes a repeated tap return the run it already started (`deduplicated: true`) and a different request under the same id a 409. `purpose` is `review_holding`, `candidate` or `exit_review`; the two that speak of a holding are refused (400) for a symbol the book does not hold |
| POST | `/proposals/{id}/approve` | `{confirm, version, idempotency_key, ticker, bucket, action, notional_usd, sell_quantity?, note?, authority_version?, acknowledge_authority_change?}` | authorises, revalidates, re-runs Hard Risk, venue review, then may execute. `authority_version` is the version `/config/authority` showed; one that has moved is refused with 409 like a moved proposal `version`. A proposal made under an authority that has since loosened, or moved in a way the record cannot classify (including one made before the record existed), is refused with 409 until `acknowledge_authority_change: true`; the response and `GET /proposals/{id}` carry `authority_changed` |
| POST | `/proposals/{id}/reject` | `{reason?}` | closes the proposal |
| POST | `/proposals/{id}/reanalyze` | none | marks it and starts fresh research |
| POST | `/portfolio/confirm` | `{ledger?, dry_run?}` | runs the ledger check and renews `as_of` where it agrees |
| POST | `/portfolio/cashflow` | `{bucket, amount_usd, kind, date?, reason?}` | records funding (`deposit`/`withdrawal`, never return) or `income`/`fee` (return) against a bucket |
| POST | `/advisor/jobs` | `{kind, request_id, scope?, account_id?, message?, conversation_id?, ticker?, language?}` | queues an assistant task bound by the server to an account scope. `kind` is `refresh`, `chat`, `shortlist`, `analysis` or `portfolio_review` (read-only, at most three priorities, never a proposal; page loads and refreshes never start it). `scope` is `personal` (default) or `agentic`, which admits `chat` only (400 otherwise). A personal `account_id` must be one the snapshot holds (400 otherwise) and binds the task to that account; none binds it to all personal accounts as the snapshot lists them now. A conversation is bound on its first message; a message under another scope or account is refused with 409, never re-bound |
| POST | `/research/{run_key}/cancel` | `{reason?}` | stops a queued or running job; 409 if it already finished |
| POST | `/research/{run_key}/opened` | `{}` | records that the finished run's result was shown to the person (`opened_at`), which is what retires a finished-task item on `/attention`; idempotent; 409 while the run is not finished. Deliberately a write of its own: reading the run, which the console polls, never marks anything |
| POST | `/auth/sessions/{session_id}/revoke` | none | signs one device out; 404 if it is already gone |
| POST | `/auth/sessions/revoke-others` | none | signs out every device except this one |
| POST | `/system/pause` | `{reason?}` | kill switch on |
| POST | `/system/resume` | none | kill switch off |

Exact request and response schemas are in the OpenAPI document; generate clients from it rather
than hand-writing types.

---

## 5. Status and state definitions

### SystemStatus (`/health.status`)

| Value | Meaning |
|---|---|
| `READY` | can research and accept approvals |
| `PAUSED` | kill switch on; research continues, no orders |
| `HALTED` | a drawdown ladder halt; needs a manual reset by the investor |
| `CLAUDE_LIMIT_REACHED` | subscription limit hit; runs resume after the reset |
| `CONFIG_ERROR` | configuration invalid; nothing will run |

### MicroLiveStatus (`/micro-live.status`)

`DISABLED` · `NOT_CONNECTED` · `NOT_READY` · `READY` · `WAITING_FOR_PROPOSAL` ·
`WAITING_FOR_APPROVAL` · `STALE` · `REANALYSIS_REQUIRED` · `HARD_RISK_REJECTED` ·
`BROKER_REVIEW_REJECTED` · `EXECUTED`

`DISABLED` is the current and default state. It is not an error: it means the master switch for
real money is off.

`checked_at` is when the venue was last asked about the account. The API keeps that answer
between polls and renews it every minute while the status is being read; the configuration
gates in `checks` are evaluated on every request. Execution never relies on it: an approval
runs preflight again in the session it places the order through.

### ProposalStatus

| Value | Meaning | Actions to offer |
|---|---|---|
| `WAITING_FOR_APPROVAL` | needs the investor | approve, reject, reanalyze |
| `APPROVED_AWAITING_EXECUTION` | approved, execution in flight | none |
| `EXECUTED` | order submitted and reconciled | view order |
| `PARTIALLY_FILLED` | part filled, remainder pending | view order |
| `STALE` | too old to act on | reanalyze |
| `REANALYSIS_REQUIRED` | the world moved materially | reanalyze |
| `INVALIDATED` | no longer valid | reanalyze |
| `HARD_RISK_REJECTED` | the deterministic layer refused | view failed rules |
| `BROKER_REVIEW_REJECTED` | the venue's own review refused | view reason |
| `REJECTED` | the investor declined | none |
| `NO_TRADE` | no action was warranted | none |

### Freshness

Every proposal carries `freshness: {state, label, detail, class, ttl_minutes, age_minutes,
max_price_drift_pct}`. States are `VALID`, `STALE`, `EXPIRED`. The window depends on the
strategy: a core long-only thesis lives 2 days, a momentum or short thesis 90 minutes, an
event-driven thesis 45 minutes. A countdown against `ttl_minutes` is a natural UI element.

---

## 6. Approval workflow

```
investor approves
  -> re-read the venue: account, cash, positions, open orders, market session
  -> re-read the current quote and check drift against the analysis price
  -> rebuild the portfolio from broker truth and re-evaluate the drawdown ladder and regime
  -> run the deterministic Hard Risk Layer again
  -> run the venue's own pre-trade review
  -> submit only if every one of those still passes
```

Possible outcomes and what the UI should say:

| Outcome | Message to show |
|---|---|
| `EXECUTED` | the order was submitted and the portfolio updated |
| `STALE` | conditions moved on; nothing was submitted; request fresh research |
| `REANALYSIS_REQUIRED` | the world changed materially; this needs new research, not a re-priced old order |
| `INVALIDATED` | no longer valid; nothing was submitted |
| `HARD_RISK_REJECTED` | the risk layer refused at execution time; show the failed rules |
| `BROKER_REVIEW_REJECTED` | the broker's own review blocked it; show the alert |

Size may be **reduced** rather than refused when current state supports less than the approved
amount, but never below half the approved size. That surfaces as `EXECUTED` with a smaller
`order.notional`, and the reason appears in `reasons`.

---

## 7. Portfolio data

`GET /portfolio` returns three things.

`buckets`, keyed by `core`, `sandbox`, `micro_live`. Each has `account`, `allocation_usd`,
`cash_usd`, `positions_value_usd`, `equity_usd`, `position_count`, `is_real_money`, `venue`,
`sleeves_pct`, and `positions[]` with symbol, quantity, price, market value, cost basis,
unrealized percent, weight, classification and exposure group.

`accounts`, keyed by `conceptual` (core + sandbox) and `micro_live`. **This is what a screen
should lead with.** Each carries its own `equity_usd`, `net_deposits_usd`,
`opening_allocation_usd`, `recorded_flows_usd`, `investment_pnl_usd`, `return_pct`,
`high_water_mark_usd`, `drawdown_pct`, `ladder_level`, `as_of`, `priced_for` and `source`.

`combined`, with `equity_usd`, `complete` and `mixes_real_and_conceptual`.

`checked_at` is when the books were read and priced. The API keeps this reading between polls: a
ledger written, the configuration edited or a new session is read again at once, and otherwise
the reading is renewed every four minutes while someone is reading it.

Two rules follow from the shape:

- **Never compare a combined total against one account's high-water mark.** The paper book and
  the funded account keep separate ledgers and separate marks; a sum across them misstates both
  the assets and the drawdown. When `mixes_real_and_conceptual` is true, say so wherever the
  combined figure appears.
- **Money paid in is not money made.** `net_deposits_usd` is the opening allocation plus every
  recorded deposit and withdrawal; `investment_pnl_usd` is equity minus that, and `return_pct`
  is measured against money paid in. Showing equity alone turns a deposit into a return.

`is_real_money` is the flag that should drive any visual distinction. Only `micro_live` is true.

---

## 8. Risk data

`GET /risk` returns a ladder per portfolio (`total`, `core`, `sandbox`, `micro_live`), each with
`high_water_mark_usd`, `level`, `drawdown_pct`, `manual_reset_required`, and a `history` of
level changes. Levels for the conceptual portfolio are `NORMAL → WARNING → RISK_REDUCTION →
HARD_HALT → EMERGENCY`; Micro-Live uses `NORMAL → WARNING → HALT`.

`regime` carries a deterministic market read: `LOW_RISK`, `NORMAL`, `ELEVATED_RISK`,
`HIGH_RISK` or `UNKNOWN`, with the index and volatility figures behind it. `UNKNOWN` blocks new
positions. `regime.checked_at` is when it was assessed: every quarter hour, every two minutes
while the data is unavailable, and at once for a new session.

Inside a proposal, `hard_risk.rules[]` lists every rule with `id`, `name`, `passed` and
`detail`. Showing the failed ones verbatim is the most useful thing the UI can do; the details
are written to be read by a person.

---

## 9. Research data

`GET /research` lists decision records. `GET /proposals/{id}` embeds the full `research` object:
`investment_thesis`, `bull_case`, `bear_case`, `primary_risks[]`,
`what_could_make_this_wrong`, `why_doing_nothing_may_be_better`, `missing_information[]`,
`entry_logic`, `exit_logic`, `expected_holding_period`, `confidence`, `data_quality`.

`confidence` is 0–100 and is an internal decision-strength indicator, **not** a probability.
Label it as such. Below 50 the system will not trade at all.

`data_quality` is `good`, `partial` or `poor`. `poor` blocks execution outright.

`missing_information` is important product content, not an error list: it is how the system
shows its own blind spots. Give it real space in the UI.

---

## 10. Execution data

`GET /activity` is the audit journal: one ordered record of every proposal transition, fill,
research outcome and kill-switch change, across all proposals rather than the one that happens
to be open. Filter with repeated `kind=` parameters and/or a `subject` (a proposal id or run
key). Page with `cursor`, which is the id of the last row you received; a response carrying
`next_cursor: null` is the last page.

Page by the cursor, never by timestamp: several entries can share an instant, and a time-based
cursor drops or repeats them at the page boundary. Entries carry `actor`, `version` and
`outcome`; approval notes and anything credential-shaped are deliberately absent, so the
journal can be shown without redacting it again.

`GET /orders` returns `fills[]` (recorded locally at fill time) and `tracked_orders[]` (every
order this system submitted, with the proposal it came from, its reconciliation time and any
reconciliation error). Nothing in it is read from the venue: the venue's own open-order list is
what the Robinhood app shows, and a tracked order's `venue_status` arrives through
reconciliation. Order status values, mapped from the venue: `new`, `accepted`,
`partially_filled`, `filled`, `canceled`, `rejected`, `expired`, `held`.

Micro-Live orders are **market orders with a dollar amount**, because Robinhood only allows
fractional or notional sizing on market orders and a $100 account cannot buy a whole share of a
broad ETF. They are protected by regular-hours-only submission, a tradability check, the venue's
pre-trade review, a current-quote drift check and small order sizes. If the UI shows an order
type, show `market` and, if you explain it, use that reasoning.

---

## 11. Error and warning states

`/health.warnings[]` carries human-readable strings. Render them as informational banners, not
failures:

| Condition | Where | Treatment |
|---|---|---|
| FRED macro data missing | `/health.warnings` | informational; the system records the gap, lowers confidence and prefers NO_TRADE. **Not a blocker.** |
| Risk defaults not accepted | `/health.warnings` | informational; blocks real money only |
| Claude limit reached | `/health.status`, `/system/claude` | show when the limit resets; runs resume afterwards |
| Market closed | `/system/market.is_open` | approval returns outcome `STALE` with status `WAITING_FOR_APPROVAL`: nothing is submitted and the proposal stays in the queue to approve again |
| Broker unavailable | `/micro-live.connect_error` | show as not connected; never imply an order went through |
| Stale quote | inside an approval result's `reasons` | the order was refused, not sent; like a pause or a venue that did not answer, the proposal returns to `WAITING_FOR_APPROVAL` |
| Stale proposal | `freshness.state` | offer reanalyze |
| Hard Risk rejection | proposal `hard_risk.failed_rule_ids` | show the rule details |
| System halted | `/health.status = HALTED` | needs a manual reset by the investor |
| Account mismatch | `/micro-live.checks` PF01/PF02 | show the failing gate |

---

## 12. Actions the frontend may invoke

Start research · cancel a running research job · approve · reject · request reanalysis · pause ·
resume · sign a device out · read anything · and, since 2026-09-17, the Settings page: change a
section or a form's values in `config/{risk_profile,investor_profile,event_calendar}.yaml`
through `POST /config/preview` then `POST /config/changes`, withdraw a change that is still
cooling, record a cash flow, stop or restore autonomous action, rotate the console password.
A loosening change carries the console password in the request and is parked for 24 hours
server-side; the frontend never decides what loosens what.

A session is identified to the interface by an opaque id. The token hash never leaves the
database, so listing devices can never yield a way to impersonate one. Using a session records
the time; it never extends the 30-day deadline.

That is the complete set. Anything else is out of bounds.

---

## 13. Security boundaries

The backend holds the Robinhood OAuth token and the Claude session. **Neither ever crosses the
wire.** The API returns product data only; account numbers are masked to the last four digits.

The frontend must never be able to:

- bypass the **Hard Risk Layer** — it runs at proposal time and again at execution time, in the
  backend, on every path
- bypass **human approval** — sandbox momentum, short and event-driven strategies are
  permanently gated and no setting can clear that
- exceed the **$100 Micro-Live capital boundary** — enforced in four independent places
  including a live check of the account before every order
- cross **Core / Sandbox / Micro-Live isolation** — a bucket may only execute at its own venue
- skip **stale-proposal revalidation** — approval always re-reads the world first
- skip the **broker's pre-trade review**
- **enable real-money execution at once** — the master switch lives in `config/risk_profile.yaml`;
  switching it off from the console applies immediately, switching it on goes through the
  24-hour cooling period behind the password, like every loosening change
- read or write credentials or state files directly, or write configuration by any path other
  than the change pipeline (`agentic_trading/config_changes.py`), which validates with the real
  loader, classifies, journals and commits every write

Run the API bound to `127.0.0.1` only. Every business endpoint requires a signed-in owner session
and every write also carries that session's CSRF token; a phone reaches the API through a
private HTTPS proxy, never by binding it to a network. The session model is in
[CONSOLE_SETUP_AND_RECOVERY.md](CONSOLE_SETUP_AND_RECOVERY.md).

---

## 14. Example data

[`ui/assets/data.js`](../ui/assets/data.js) holds sanitized payloads for every screen. The console
renders them when opened with `?demo=1`, which sends no request and cannot act on an account.
They are maintained by hand alongside the screens. `tests/test_frontend.py` holds the payloads of
the endpoints with a declared response model — health, portfolio, Micro-Live, orders, the
proposal list and a proposal's detail — to those models; for the rest, `/openapi.json` is the
contract when the two disagree.

---

## 15. Keeping the screen current

Poll. There is no websocket and none is needed for a single-user system where the fastest thing
that moves is a quote.

| Data | Suggested interval | Out of date after |
|---|---|---|
| `/health` | 30s | 90s |
| `/config/authority` on Approvals | 30s (local state and kept readings only) | 90s from `checked_at` |
| `/proposals` while one is open | 15s (freshness is a countdown) | 45s |
| `/portfolio` | 60s | 10 min from `checked_at` |
| `/micro-live` | 60s | 3 min from `checked_at` |
| `/risk` | 60s | 3 min |
| `/advisor` | 5s while a conversation is open | 5 min |
| `/research/{run_key}` during a run | 5s | 30s |
| `/research/jobs` | 10s; use it to resume an active run after page reload | 30s |
| `/orders` when an order is open | 10s (a local read; it opens no venue session) | 90s |

A poll is only how often a screen asks. Whether what it holds is current is a separate question,
answered by age: three polls unless the table says otherwise, and for a reading the API stamps
with `checked_at`, from that stamp rather than from when the poll came back. The API keeps
`/portfolio` and `/micro-live` between polls, renewing them every four minutes and every minute
while they are being read and serving them for at most eight minutes and ninety seconds, so a
poll that answers at once can carry a reading minutes old; the thresholds allow for that and for
one failed poll. Move the stamp onto the API's clock with the response's `Date` header before
taking its age. Never enable an approval on data that is out of date.

Recompute `freshness.age_minutes` client-side between polls so a countdown stays smooth. Always
call `/proposals/{id}/revalidate` immediately before showing an approve button on a proposal
that has been on screen for a while.

---

## 16. Running the backend

```bash
uv sync
uv run ct-api                 # http://127.0.0.1:8787, schema at /docs
```

Everything else the backend needs (Claude login, Robinhood connection) is set up on the machine
with `claude auth login` and `uv run ct-robinhood connect`. The frontend never touches either.
