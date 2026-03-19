# Agentic Trading — console

Frontend for the local API in `agentic_trading`. Fourteen pages, one shared
asset layer, no build step.

```
index.html       Home      Today: what needs you, what is not normal, research conclusions, the account
portfolio.html   Assets     Robinhood snapshots or the Agentic ledger, one switch; each Robinhood holding opens its own view
approvals.html   Approvals proposal detail, Hard Risk rules, approve / reject
risk.html        Risk      regime, drawdown ladders, 15 preflight gates
activity.html    Activity  fills, tracked orders, audit trail
system.html      System    health, notices, kill switch
advisor.html     Assistant personal context, holdings review, candidates, chat
lab.html         Lab       the third tab: the office at work, then what I hold and what I added, each name's report
evaluation.html  Evaluation forward record against the declared benchmark
more.html        Me        preferences, account/device and supporting-tool links
notifications.html Devices signed-in devices, push status and a test
profile.html     Context   my personal context, read by the assistant
settings.html    Settings  schedule, watchlist, calendar, cash flows, every config section
login.html       Sign in   owner passphrase; the one page served without a session

assets/console.css   tokens, components, motion
assets/portfolio.css Assets only: liquid glass, ink on white
assets/console.js    chrome, formatting, live() binding
assets/api.js        polling layer
assets/i18n.js       interface language (EN / 中文)
assets/data.js       sanitized fixtures, rendered with ?demo=1
```

## Serving it

Already wired. `agentic_trading/api/app.py` mounts this folder at `/ui`
when it exists, so the console is same-origin with the API and no CORS
middleware is needed.

```bash
uv run ct-auth         # a 16+ passphrase, or 8+ with upper, lower, a digit and a symbol
uv run ct-api          # http://127.0.0.1:8787/ui/
```

Open `/ui/login.html` and sign in as `owner`. The server refuses business API
requests until credentials are configured. All API calls use the page's origin.
See [the setup and recovery guide](../docs/CONSOLE_SETUP_AND_RECOVERY.md) for the
session model, HTTPS settings and order recovery.

Opening a document costs one request. Each page is served `no-cache`, so the
browser asks the server every time and gets a bodiless 304 when nothing changed;
the scripts and stylesheets a page names are rewritten on the way out to carry the
release they belong to (`assets/console.js?v=8f0c…`) and are served immutable, so
they are fetched once per release and never again. The pages on disk stay plain —
write `assets/console.js`, never a hand-made `?v=` — and a checkout run by hand
stamps by file modification time, so an edit shows on the next load. Nothing the
API answers is ever cached, and no screen opens offline: see `_cache_policy` and
`ConsoleFiles` in `agentic_trading/api/app.py`.

Use `/ui/index.html?demo=1` for a public, read-only demo. Demo navigation preserves
this flag, shows a permanent banner, and makes **no API requests**. Live pages
start empty and never fall back to fixtures.

## Console navigation

All thirteen console pages share one document through `assets/navigation.js`, including
the tools reached from More and their return links. Login remains a separate document.
The initial response includes the static layouts as inert templates, with shared styles.
Page factories are prefetched; even before they arrive a first tap can show the layout.
Opening an initialized tab synchronously switches its retained DOM. Scroll position, chat input, expanded sections and
in-flight actions survive switching. Browser Back/Forward and direct links still work.

Cold opens of console pages get a shared startup curtain before application scripts run.
The branded introduction is shown once per tab session. A sessionStorage boolean (no account
data) selects the compact, blades-only curtain for later document transitions and reloads.
Inside the app, pending page data or scripts show only the same blades over the retained
layout; returning to a ready page hides them immediately. Background refreshes keep content.
It uses the owner's lightsaber animation (Uiverse.io, NlghtM4re), adapted to the console's
light palette. The first successful render with authenticated data dismisses it immediately
with a short fade; no minimum delay or artificial progress is added. Optional slow reads do
not hold it open. A failed initial load, or ten seconds without readiness, offers retry and
an option to enter the page and see its connection details. Signing out dismisses it to the
existing sign-in state. It never returns for tab switches, polling or background resumes,
and reduced-motion preferences disable its animation. No extra business request is made.
Back/Forward and the header return button restore the previous page and scroll position.
Page scripts are registered without running until entry; preloading never starts a write,
push registration, or the research-room animation.

The API client and its polling timers are shared. Switching tabs selects the active resources
without repeating a successful session check or rereading fresh data. First entry shows the
real page layout with unavailable values and disabled actions until authenticated data arrives,
rather than an entire replacement skeleton. Failed reads and expired sessions remain visible.
Retained data exists only in memory; there is no offline storage, write queue or replay.
A template from a different deployed release falls back to a complete document navigation.

`node tools/check_navigation.cjs` runs a mobile browser regression with deliberately delayed,
synthetic APIs. It needs Playwright and Chrome (or `CT_BROWSER_CHANNEL`); it starts its own
static server and never calls the deployed API. The dependency-free regression cases remain
in `tests/frontend.test.cjs` and run through `deploy/checks.sh`.

`node tools/check_approval_ui.cjs` checks the mobile approval dialog against synthetic API
responses, including a version conflict and an explicit second confirmation. It also starts
only a static server; all API requests are intercepted. Set `CT_UI_CAPTURE_DIR` to save a screenshot.

SELL/REDUCE details render the server's `exit` quantities, source readings, expected closure
state and exit chain. Missing quantities say "Not read"; unknown remainder/closure says
"Cannot confirm". Source disagreement uses the server's smaller-size/larger-remainder bases;
the page does no quantity arithmetic. Approvals bind to fixed shares and are unavailable when
exit detail is missing or blocked. A changed exit reading clears confirmation even if the
proposal version is unchanged. Live orders also require the actual action's authority.
Continuation creation is offered only when the current detail reports `continuable.eligible`.
A separate confirmation sends `request_id` and the authority version to `continue_exit`;
authority loosening requires acknowledgement. Creation selects a fresh GET of the new
proposal, never approves it. Double clicks are suppressed; a lost response can be retried
explicitly with the same in-memory request id. Conflicts refresh readings and require another
confirmation. Page/quantity/authority changes invalidate a pending confirmation. Nothing is
queued or replayed offline. Existing continuations are reachable through the chain links.
Approval results render `exit_result` using its post-submission three-state closure reading;
`EXECUTED` alone never means the position is closed. Home and More link `exit_incomplete`
attention to its proposal. The browser check covers creation through separate approval on
mobile; the frontend suite covers conflicts, lost responses, late responses and chain amounts.
REDUCE also displays `exit.autonomous`: the configured retained percentage, holding high-water
mark and its provenance, cycle start, current holding, floor, headroom and reasons. These
figures come from the service, not frontend arithmetic; null never becomes zero. Position
autonomy and account autonomy constraints are separate from human approval constraints.
Passing position conditions is not presented as passing all proposal/execution checks.

## Live data

System activity rows use `/system/operations.activities[].state` exclusively. The legacy
`overdue` boolean is intentionally inconsistent with first-run waits and fresh failures.
Only `failed` and `overdue` contribute to the local needs-attention count; first-run waits
are shown separately. Missing states and stale reads stay unknown. `due_by` is displayed as
a monitoring deadline, not a scheduled trigger or a frontend state countdown. Attempts and
successes have separate timestamps. Failure reasons are escaped, wrapped and rendered in
full, including for an overdue activity whose last failed attempt has stopped arriving.
The demo includes all four states; `check_navigation.cjs` covers long failure text on mobile.

Reads poll on the cadence in `FRONTEND_HANDOFF.md` §15 — orders 10s, proposals
15s, health/authority/attention 30s, portfolio/risk/research/micro-live 60s. Authority is
read only on pages that use it and opens no venue session. No websocket; the
backend offers none and none is needed.

Home and More read current attention from `/attention`, preserving server priority. Home shows
those items alone, and hides them after a current, readable empty result; a native disclosure
shows additional cards. Below them, Home lists only what is not normal, each row opening the page
that owns it: a status other than READY (System), a preflight block while real money is armed
(Risk), an order still working at the venue (Activity), and the number of `/health.warnings`
(System, which holds their text). Nothing there means nothing is abnormal. Research follows: a
running study opens the Lab on it, a finished one its report in the Lab, each naming its account;
the paper book and records bound to no account stay off Home. Missing, stale or partially
readable results say "cannot confirm", never "nothing to do". Links retain
the referenced proposal, activity subject, configuration change or research run. A finished
report is marked with `POST /research/{run_key}/opened` only after its result is displayed in
a visible sheet; polling never marks it. Failed receipts offer an explicit retry and are not
queued. A successful receipt refreshes attention. Until Assistant supports a specific review
deep link, a finished portfolio review opens its exact run in the Lab's report sheet.

`node tools/check_attention_ui.cjs` exercises the mobile attention-to-result flow with a static
server and intercepted API responses. Like the approval check, it needs Playwright and Chrome
and accepts `CT_UI_CAPTURE_DIR` for screenshots.

Evaluation requests an explicit `scope`: Agentic (the live default), Demo, or research-route
quality. Switching scopes clears the previous report and rejects responses for another scope.
Each scope keeps the existing five-minute read interval. `NOT_STARTED` shows record counts
without performance; sample gaps and staleness use the server's exchange-session calculation.
Route metrics render `null` or `evaluated: false` as "Not evaluated", and do not rank routes.
The scope is preserved in the URL and browser history. Demo previews use separate synthetic
fixtures for each scope and make no requests.

`node tools/check_evaluation_ui.cjs` checks these three scopes on a phone viewport, including
history navigation and a delayed response arriving after another scope is selected. It uses
the same offline browser setup and screenshot option as the other browser checks.

The Lab is where research lives (2026-09-23): the office drawing one run, then what the owner
holds and what they added, each name with its newest conclusion. It studies only those. The two
lists come from `researchSubjects`, `GET /research/subjects` every 60s: held is the Agentic ledger
and the owner's own accounts as Robinhood was last read; added is the names on the owner's
Robinhood watchlists that neither holds. Screener results and the profile's ETF groups are not
the owner's and are not listed. These are the names the weekly run studies, read from the same
places (`schedule.subjects_for`). A Demo book's run and a record bound to no account are never
drawn in the office, listed, or offered.

A studied name opens its report over the room; a running one is watched in the office; one never
studied offers Research. A name in the Agentic account is reviewed there
(`POST /research {scope: agentic, purpose: review_holding}`), a name only the owner holds is
analysed read-only in the account that holds it (`POST /advisor/jobs {kind: analysis,
account_id}`), and a watched name is a candidate for the Agentic account, whose BUY still waits
for approval. A retry after a lost response reuses its request id; the answer is said on the row.
"+ Add" explains that a name is added in Robinhood, and offers one reading of Robinhood now.

A report shows the run's strength against the threshold it had to clear, when both were
recorded, and its record details: retrieved time apart from fact time, route provenance,
versions and models, missing evidence, a truncated counter-evidence excerpt, and the record that
superseded it. It is frozen while open. A link naming a run opens that run in the office and, if
it has finished, its report; a link that also names an account refuses a report bound to another.
`research.html` is gone: the API redirects it to `lab.html` with its query, and the console's
navigation does the same for an in-app link.

`node tools/check_research_ui.cjs` checks the lists, owner-only runs, report provenance, the read
receipt, an account-bound deep link, research started per account and an old research link on a
phone viewport using synthetic APIs. It accepts the same browser and screenshot options above.

Each screen declares what it reads:

```js
C.live(['health', 'portfolio', 'risk'], render);
```

Each resource has its own success time and error. A failure, or data older than
its source allows, marks that resource unavailable/stale. A source allows three
polls unless it sets `staleAfter` in `assets/api.js` — assistant 5 min, orders
90s, micro-live 3 min, portfolio 10 min — and a reading the API stamps with
`checked_at` (portfolio, micro-live) is aged from that stamp, moved onto the
API's clock by its `Date` header, not from when the poll came back. The page
waits for its initial data, then can display last-known values with this warning.

System renders `/system/schedule` as both a backward-compatible first-task summary and a
per-account task list. Each task keeps its server-provided reason, subjects, period and last
run together. `fires_every` describes the operating-system trigger, while
`reserve_for_exits` is shown separately from today's use so candidate capacity cannot be read
as capacity available to holding and exit reviews. The screen is read-only; schedule changes
continue through the reviewed Settings flow.
One healthy endpoint cannot clear another endpoint's error. A ledger that has
gone unconfirmed still answers, with its buckets emptied and the reason in
`errors`, and the Portfolio page shows that gap rather than a zero. Approval
requires all of its critical resources to be current.

One screen also writes without being asked. The Robinhood block on the
Portfolio screen is a snapshot maintained by an assistant `refresh` job, and
the button that starts one is on the Assistant screen — so when that snapshot
is already stale on arrival, the Portfolio page posts a single refresh for it,
with the tab in front, never while one is already queued or running, and at
most once a minute. The wait and any failure are written into the timestamp
line above the accounts. Nothing else in the console posts without a click,
and this one reaches no order, proposal or approval.

A Robinhood holding opens its own view on the same page,
`portfolio.html?scope=robinhood&account=<id>&ticker=<symbol>`, so it has a link, Back returns
to the same place in the list, and a reload lands on it again. It used to open Research,
which with no scope in the link came up in the Agentic scope with the strategy form filled
in. Everything on it comes from the `/advisor` read the page already polls:

- the position from the snapshot. Cost basis and gain are drawn only when every figure they
  rest on is present;
- the recorded analyses of that symbol bound to that account, plus all-account ones, which
  say so. A record whose account binding is unknown is left out, as it is in the Lab;
- a queued or running analysis, with Stop, and the newest failure until a newer analysis
  replaces it;
- the latest portfolio review's line for the symbol, only when that review covered the
  account.

Analyze sends the Assistant's own `POST /advisor/jobs {kind: 'analysis', ticker, account_id}`.
A second tap while one is in flight sends nothing, and an explicit retry after a lost response
reuses its request id. Charts, news and trading stay in Robinhood, which the view links to
rather than repeats.

## Agent trace

`GET /research/{run_id}/trace` returns the run's decision record plus every
stage report the pipeline wrote under `runs/<id>/reports` — analysts, the
bull/bear debate, the trader, the risk debate, the portfolio decision. Tapping
a name in the Lab opens that trace as an expandable sequence.

## Writes

Authenticated live pages can approve, reject, reanalyze, pause and resume. The
server verifies the session, Origin and CSRF token. No JavaScript flag grants
access. Real-money execution remains separately disabled by the risk profile.

Approval requires `confirm: true`, the exact proposal version and terms, and an
idempotency key. The confirmation dialog captures these when it opens. A changed
queue or stale critical resource disables confirmation; a repeated request cannot
create another order. A transport failure shows an uncertain result and links to
Activity. Submission, partial fill and completion are distinct states.

`GET /proposals/{id}/revalidate` previews the approval-time checks without saving
approval or risk state. It does not place an order.

## Language

The switch in the top bar toggles EN / 中文 and persists in `localStorage`.
Every string the console writes itself goes through `assets/i18n.js`.

Anything the API returns stays verbatim in whatever language the backend
produced: `/health.warnings`, the research thesis and bull/bear cases, Hard
Risk rule details, preflight gate details, revalidation reasons, broker order
status. Translating the engine's own record of what it decided would be
putting words in its mouth. Status codes (`READY`, `NO_TRADE`, `PF00`) also
render verbatim; only the plain-language gloss beside them switches.

## Palette

`--green #00C805`, `--fg #000000`, `--bg #ffffff` are the only literal values;
every neutral is a `color-mix()` of them. `--danger` is a local choice, not a
brand value. Pure green never carries text (1.9:1 on white) — green text uses
`--green-ink`.

Assets is drawn in Home's palette (`<body class="pf-glass">`, `assets/portfolio.css`): Home's
green-tinted field, card fill and shadow, and green for the leading action, the selected
account and anything live. Glass cards are drawn without blur. Gain and loss are mixed from the
brand values and measured above 5:1.


Assistant uses `assistant` (5s, stale after 5 minutes) for
`/advisor?scope=personal|agentic&account_id=…`, plus `accounts` (60s). The existing
unfiltered `advisor` resource remains for Portfolio and Context. Switching accounts clears
scoped evidence immediately and rejects delayed responses. Chat history is checked against
its server binding; drafts, selected conversations and explicit retry IDs are stored per
account in tab storage. Switching or reloading never submits a request.

Personal context notes remain shared across personal accounts. Agentic hides personal tools
and only submits explanation chats. Portfolio review is an explicit personal action, bound
to the selected account scope. Snapshot refresh does not rerun it; an old review keeps its
snapshot/analysis times and stale warning. The completed result is marked opened only after
its detail sheet is visible, using the independent idempotent `/opened` write.

`node tools/check_assistant_ui.cjs` verifies account switching, retained drafts, Agentic chat,
explicit review and opened receipts with synthetic APIs, including phone layout.

The Assistant chat is a mobile-first, single-viewport layout. Its welcome prompts fill
an editable draft. The account in view is the header's title: a glass capsule, the way
ChatGPT titles a chat with its model, whose dot is ink when the account reads current,
hollow while it loads and amber when the picker has something to say. The capsule opens
the account picker, one row per scope; one tap switches, because each row is a link to
this page for that scope. Chat and Discover share one glass segmented switch below it,
hidden for Agentic, which has only Chat; history and a new conversation show in Chat only.
Portfolio reviews and supporting tools open from the composer's + button, in a sheet that
names its account and hands over to the picker. Messages scroll independently above the composer,
with a jump-to-latest control that does not interrupt someone reading older replies.
The composer grows with its draft, respects IME composition, and keeps Enter as a newline
on touch devices (desktop Enter sends; Shift+Enter adds a line). The visual viewport keeps
it above the software keyboard. All controls retain the existing scoped API and draft storage.

The supplied Chat02 React design is adapted as plain HTML/CSS/JavaScript in `advisor.html`,
`assets/advisor.css` and `assets/pages/advisor.js`, preserving this console's no-build
architecture. Its message bubbles, growing composer and latest-message control are connected
to real conversation state. No React/shadcn/Tailwind runtime, simulated streaming, model
picker or unsupported attachment/voice controls are added. The account-centered entry and
progressive disclosure take inspiration from [Robinhood's Cortex overview](https://robinhood.com/us/en/newsroom/robinhood-presents-yes-no-event/).

The Assistant browser check also exercises prompt drafts, pending and completed replies,
clipboard actions, long-message scroll preservation, and phone, landscape, keyboard-height
and desktop viewports. It checks scroll-only visual-viewport panning, keyboard dismissal
with a stale offset, page restoration, and the absence of gaps below the composer.
`CT_BROWSER_ENGINE=webkit` runs these cases in Playwright WebKit (clipboard permission
checks remain Chromium-only). These are browser regressions, not a real iOS keyboard.
`CT_UI_CAPTURE_DIR` saves synthetic-data previews of the welcome,
conversation and waiting states.

The Assistant hides the shared top and bottom navigation as soon as it opens, in both Chat
and Discover. Its own header keeps a back control visible even while the keyboard is open
or account data is unavailable. Back returns to the page that opened this visit, skipping
account changes inside Assistant; a direct entry falls back to Home. Data-source details
remain available from Account & tools.
The chat shell fills the viewport in normal document flow, reserving only the device safe
areas. The shared router clears source-page scroll before restoring a retained chat.
Only an open software keyboard with a focused editor switches the shell to a pixel-height fixed layout; keyboard
resize and scroll events apply its visual height and vertical offset. Route entry and page
restoration refresh geometry even when the keyboard closed on another page. Composer height
changes only with its text or width, so background polls do not disturb the caret.
See the [VisualViewport API](https://developer.mozilla.org/en-US/docs/Web/API/VisualViewport)
for the difference between visual and layout viewport position.

Discover lists the candidate pool of the last Robinhood snapshot (`snapshot.candidates`):
watchlists, saved screeners and the profile's ETF groups, filtered by source kind and
searchable across the whole pool, 24 rows at a time. A row marks a held symbol and the
newest research verdict with its date, and shows "Researching" in place of its button while
an analysis of that symbol is queued or running. Research starts only from that button or
the search field, whose symbol is checked against the service's rule before any request;
the shortlist call is off for an empty pool, since ranking nothing still spends a reply.
Discover reports its own requests in its own status line, out of Chat's composer. Its look
is liquid glass in ink on white, and the glass is only for controls: the search field and
filters stick under the header and frost only while rows pass beneath them; rows, verdicts
and research cards stay opaque. In the demo, the assistant reads the one `CT.advisor`
fixture, narrowed per account the way the service narrows `/advisor`.

Leaked `research_candidates` serialization is cleaned in the assistant service for both
new and historical answers. Valid embedded ideas remain readable prose; only the separately
stored, filtered candidate field creates research buttons. Stored records are not rewritten.
