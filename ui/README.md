# Agentic Trading — console

Frontend for the local API in `agentic_trading`. Eleven screens, one shared
asset layer, no build step.

```
index.html       Home      equity vs high-water mark, the open proposal, buckets
portfolio.html   Portfolio three buckets, sleeves, positions
approvals.html   Approvals proposal detail, Hard Risk rules, approve / reject
research.html    Research  decision strength, run history, start a run
risk.html        Risk      regime, drawdown ladders, 15 preflight gates
activity.html    Activity  fills, open orders, audit trail
system.html      System    health, notices, kill switch
advisor.html     Assistant personal context, holdings review, candidates, chat
lab.html         Lab       visual research-agent state and report handoffs
evaluation.html  Evaluation forward record against the declared benchmark
more.html        Me        preferences, account/device and supporting-tool links

assets/console.css   tokens, components, motion
assets/console.js    chrome, formatting, live() binding
assets/api.js        polling layer
assets/i18n.js       interface language (EN / 中文)
assets/data.js       sanitized fixtures from docs/examples/
```

## Serving it

Already wired. `agentic_trading/api/app.py` mounts this folder at `/ui`
when it exists, so the console is same-origin with the API and no CORS
middleware is needed.

```bash
uv run ct-auth         # set your own password locally (14+ characters)
uv run ct-api          # http://127.0.0.1:8787/ui/
```

Open `/ui/login.html` and sign in as `owner`. The server refuses business API
requests until credentials are configured. All API calls use the page's origin.
See [the setup and recovery guide](../docs/CONSOLE_SETUP_AND_RECOVERY.md) for the
session model, HTTPS settings and order recovery.

Use `/ui/index.html?demo=1` for a public, read-only demo. Demo navigation preserves
this flag, shows a permanent banner, and makes **no API requests**. Live pages
start empty and never fall back to fixtures.

## Live data

Reads poll on the cadence in `FRONTEND_HANDOFF.md` §15 — orders 10s, proposals
15s, health 30s, portfolio/risk/research/micro-live 60s. No websocket; the
backend offers none and none is needed.

Each screen declares what it reads:

```js
C.live(['health', 'portfolio', 'risk'], render);
```

Each resource has its own success time and error. A failure or a timestamp older
than twice its polling interval marks that resource unavailable/stale. The page
waits for its initial data, then can display last-known values with this warning.
One healthy endpoint cannot clear another endpoint's error. Order responses can
still show local records when the venue portion fails, with an incomplete-data
warning. Approval requires all of its critical resources to be current.

One screen also writes without being asked. The Robinhood block on the
Portfolio screen is a snapshot maintained by an assistant `refresh` job, and
the button that starts one is on the Assistant screen — so when that snapshot
is already stale on arrival, the Portfolio page posts a single refresh for it,
with the tab in front, never while one is already queued or running, and at
most once a minute. The wait and any failure are written into the timestamp
line above the accounts. Nothing else in the console posts without a click,
and this one reaches no order, proposal or approval.

## Agent trace

`GET /research/{run_id}/trace` returns the run's decision record plus every
stage report the pipeline wrote under `runs/<id>/reports` — analysts, the
bull/bear debate, the trader, the risk debate, the portfolio decision. Tapping
a run on the Research screen opens that trace as an expandable sequence.

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
