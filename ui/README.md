# Conservative Trading — console

Frontend for the local API in `agentic_trading`. Seven screens, one shared
asset layer, no build step.

```
index.html       Home      equity vs high-water mark, the open proposal, buckets
portfolio.html   Portfolio three buckets, sleeves, positions
approvals.html   Approvals proposal detail, Hard Risk rules, approve / reject
research.html    Research  decision strength, run history, start a run
risk.html        Risk      regime, drawdown ladders, 15 preflight gates
activity.html    Activity  fills, open orders, audit trail
system.html      System    health, notices, kill switch

assets/console.css   tokens, components, motion
assets/console.js    chrome, formatting, live() binding
assets/api.js        polling layer
assets/i18n.js       interface language (EN / 中文)
assets/data.js       sanitized fixtures from docs/examples/
```

## Serving it

Already wired. `conservative_trading/api/app.py` mounts this folder at `/ui`
when it exists, so the console is same-origin with the API and no CORS
middleware is needed.

```bash
uv run ct-api          # http://127.0.0.1:8787/ui/
```

Opened from any other origin, every request is blocked by the browser and the
console falls back to fixtures behind an amber "Sample data" banner.

## Live data

Reads poll on the cadence in `FRONTEND_HANDOFF.md` §15 — orders 10s, proposals
15s, health 30s, portfolio/risk/research/micro-live 60s. No websocket; the
backend offers none and none is needed.

Each screen declares what it reads:

```js
C.live(['health', 'portfolio', 'risk'], render);
```

`render()` runs on first paint and after every poll. When the API is
unreachable the last-known values stay on screen and the banner appears —
sample data can never be mistaken for live data.

## Agent trace

`GET /research/{run_id}/trace` returns the run's decision record plus every
stage report the pipeline wrote under `runs/<id>/reports` — analysts, the
bull/bear debate, the trader, the risk debate, the portfolio decision. Tapping
a run on the Research screen opens that trace as an expandable sequence.

## Writes

Every action calls its real endpoint: approve, reject, reanalyze, pause,
resume. `CT_API.post()` is gated by `CT_API.writesEnabled`, which ships
**`false`** — a prototype must not be able to fire `POST /approve` at a live
account by accident. Arm it in `assets/api.js`:

```js
writesEnabled: true,
```

Reads need no flag; `GET /proposals/{id}/revalidate` runs for real either way.

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
