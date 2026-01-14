# P1 acceptance: the checks a person has to run

The engineering behind the seven P1 items is implemented and covered by the suite. What remains
cannot be done from a keyboard here, because it needs your phone in your hand, this Mac
physically rebooted, and decisions only you can make (which notification service, which second
machine holds the backups, whether real money is ever switched on).

Print this, or keep it open on the laptop while you work through it. Record what actually
happened, not what you expected: an acceptance record that only contains successes is not one.

Nothing in this document authorises real-money trading. `micro_live_execution.enabled` stays
`false` throughout.

---

## Before you start

```bash
deploy/checks.sh                  # lint, tests, frontend cases, secret scan — expect all pass
uv run ct-state status            # integrity ok; note the counts
uv run ct-state sessions          # how many persistent logins exist right now
uv run ct-ops status              # heartbeats and free disk
uv run ct-trigger scheduled --dry-run
```

Then publish the current code as a release, so everything below tests a fixed version rather
than whatever the checkout happens to contain (see `MACOS_DEPLOYMENT.md`):

```bash
deploy/macos/release.sh publish
deploy/macos/release.sh list
```

---

## 1. Phone login and the 30-day session

`ct-state sessions` currently reports what the database holds. The point of this section is to
see a *new* session created by the phone against the deployed release, and to see it survive.

| # | Step | Record |
|---|---|---|
| 1.1 | Sign in on the phone at `https://<device>.<tailnet>.ts.net/ui/`. Note device, OS version, browser. | ✅ 2026-09-05 06:03:35Z — `POST /auth/login` 200 from 100.124.169.97 (phone, over Tailscale) |
| 1.2 | `uv run ct-state sessions` on the Mac. A new row, ~30 days left. | ✅ one row, `days_left: 30.0`, expires 2026-10-05 |
| 1.3 | Close the tab, reopen it after 10+ minutes. Still signed in? | ✅ |
| 1.4 | Switch Wi-Fi → cellular → Wi-Fi. Still signed in? Does the console reconnect on its own? | ✅ |
| 1.5 | Pick a window with **no research running and no open order**. Restart the API (`launchctl kickstart -k gui/$UID/com.local.agentic-trading.api`). Phone still signed in without retyping the password? | ✅ the previous session survived four API restarts (install + three publish/rollback cycles) without a re-login. Not yet re-confirmed for the session created at 06:03; the next publish will do that. |
| 1.6 | `ct-state sessions` again: same session, not a new one. | ✅ a signed-in phone reuses its session; a new row appeared only after a deliberate sign-out and sign-in |

**Passes when** the phone stays signed in across a close/reopen, a network change and an API
restart, and step 1.6 shows the same session rather than a replacement.

**2026-09-05 result: passed**, with one caveat carried forward. The sign-out/sign-in was done
deliberately because the login path had been broken and repaired earlier that evening (a read
migrated the live database ahead of the running release; see TODO item 0), so it needed proving
rather than assuming. It works. What is still unproven is 1.5 for the *current* session, which
the next publish will exercise on its own.

Device: iPhone over Tailscale. The console is reachable only from inside the tailnet — the API
listens on 127.0.0.1 alone and Tailscale Serve is marked "tailnet only", so there is no public
entry point at all. Turning Tailscale off makes the console unreachable by design.

## 2. The full loop, on simulated money only

Confirm `broker.adapter: simulated` and `micro_live_execution.enabled: false` first.

| # | Step | Record |
|---|---|---|
| 2.1 | From the phone, start research on one ticker. Note the `run_key`. | |
| 2.2 | **Close the page.** Wait. Reopen it. Does the run still appear, and does it still progress? | |
| 2.3 | Tap "start" again for the same ticker/date while it runs. Expect one job, not two (`GET /research/jobs`). | |
| 2.4 | Stop a run from the Research screen. State becomes `CANCELLED`; the worker process is gone (`ps aux | grep research_worker`). | |
| 2.5 | When a run produces a proposal: open it, read the bull/bear case, **reject** it. Note the proposal id. | |
| 2.6 | Produce another proposal and **approve** it. Note the id, version, and the outcome text. | |
| 2.7 | Double-tap approve on a slow connection. Expect exactly one order (`GET /orders`, one `client_order_id`). | |
| 2.8 | Check the Activity screen and `uv run ct-state status` — the fill is recorded once, the ledger moved by the right amount. | |

**Passes when** research survives the page closing, a repeated tap creates no second job or
order, and every id above appears exactly once in the state database.

## 3. Money in versus money made

| # | Step | Record |
|---|---|---|
| 3.1 | Note the conceptual account's `equity_usd`, `net_deposits_usd` and `investment_pnl_usd` on the Portfolio screen. | |
| 3.2 | `uv run ct-state cashflow --bucket core --amount 100 --kind deposit` | |
| 3.3 | Reload. Equity **and** money-paid-in each rose by $100; investment result and return are unchanged. | |
| 3.4 | Home, Portfolio and Risk screens agree on the conceptual high-water mark and drawdown. | |
| 3.5 | Reverse it: `--amount 100 --kind withdrawal`. | |

**Passes when** a deposit never appears as a gain and the three screens never disagree.

## 4. The schedule, over a real week

This one takes calendar time. Start it and come back.

| # | Step | Record |
|---|---|---|
| 4.1 | `uv run ct-trigger scheduled --dry-run` — note `next_run_at`, `current_period`, `reason`. | |
| 4.2 | Let the scheduled job fire on its own. Afterwards: System screen shows the run, the tickers and the count. | |
| 4.3 | Run `uv run ct-trigger scheduled` manually in the same week. Expect a skip: "already executed for …". | |
| 4.4 | Confirm in `runs/service/research.stdout.log` that the launchd environment completed the whole chain — Claude authenticated, data fetched, evidence written under `runs/<id>/`, a decision record produced. | 2026-09-05: chain confirmed; found and fixed a defect (see 4.4b) |
| 4.4b | Check the worker log's `regime=` line. It must not be `UNKNOWN`. A session closes hours before its prices publish, so a run anchored to the calendar's date reasons with no market context while otherwise looking healthy. Re-run after publishing the fix. | |
| 4.5 | Wait for a market holiday (or temporarily set `on_non_session_day: skip` and fire on a weekend). Confirm the skip reason names the calendar. | |
| 4.6 | Exhaust the daily budget with manual runs; confirm the schedule skips with the count. | |

**Passes when** 4.4 shows a complete run started by launchd rather than by your terminal, and
every skip states a reason you can act on.

## 5. Notifications — deferred by decision (2026-09-05)

**Not enabled, deliberately.** Research runs weekly; four runs so far have all been NO_TRADE,
real money is off, and a proposal stays valid for two days — so looking at the console over the
weekend covers it, and the cost of missing one is a wasted quota slot rather than money. The
WeChat route would also hand a third-party relay the knowledge of what is being researched,
which is not worth it at this level of benefit.

Revisit when any of these becomes true: real money is switched on, proposals start arriving
often enough that one gets missed, or you need to know the Mac itself has gone offline (which
only the external heartbeat can tell you — a dead machine sends nothing).

When revisited, prefer PWA + Web Push over any WeChat relay: the notification goes from this Mac
to Apple, encrypted end to end, with nobody in between able to read it.

The steps below are kept for that day.

### The original checklist

Nothing is sent until you choose a channel. **Pick one before doing anything here**, and do not
put its credentials in the repository.

- Simplest to verify: `channel: file`, then watch `runs/notifications.jsonl`.
- For the phone: `channel: webhook` plus an HTTPS endpoint of a service you already trust
  (a push service, a chat webhook, your own relay). Set `CT_NOTIFY_WEBHOOK` in the service
  environment (`launchctl setenv`), never in a tracked file.

| # | Step | Record |
|---|---|---|
| 5.1 | Channel chosen and credentials set. `uv run ct-notify status` reports `configured: true`. | |
| 5.2 | `uv run ct-notify test` — one message arrives. | |
| 5.3 | Leave a proposal unapproved. Confirm it notifies once, not on every poll. | |
| 5.4 | Break the endpoint deliberately. Confirm retries back off and then stop (`ct-notify status`). | |
| 5.5 | Check the received message: no token, no account number, no quote. Only what happened and a link. | |

### Offline detection — also your decision

Nothing running on this Mac can report that this Mac is gone. Choose an external watcher that
expects a regular ping and alerts on its absence, then:

```bash
launchctl setenv CT_HEARTBEAT_URL https://<your-watcher>/ping/<id>
```

| # | Step | Record |
|---|---|---|
| 5.6 | Heartbeat arriving at the watcher, on its interval. | |
| 5.7 | Pull the network (or stop the API) and confirm the watcher alerts you. **Do this from the phone, away from the Mac.** | |

**Passes when** 5.7 alerts you without you looking at the Mac. Checking that a login page
returns 200 is not this check: it proves nothing about the worker, the data source or reconciliation.

## 6. Backups on other hardware, and a real restore

| # | Step | Record |
|---|---|---|
| 6.1 | Choose the second destination (external disk, another machine, a synced folder). Create the passphrase file and **store a copy of the passphrase somewhere that does not depend on this Mac**. | |
| 6.2 | Set `CT_BACKUP_MIRROR` and `CT_BACKUP_KEY_FILE`; run `deploy/macos/backup_state.sh`. An `.enc` copy appears at the destination. | |
| 6.3 | Restore it into a scratch directory on **another machine or another user account**: `ct-state restore --source <...>.enc --into <dir> --key-file <...>`. | |
| 6.4 | Confirm the report: integrity ok, sessions revoked, orders listed for broker verification. | |
| 6.5 | Point a throwaway config at the restored directory and confirm proposals, ledgers, orders and evidence still relate to each other and nothing is double-counted. | |
| 6.6 | Try to sign in with a cookie from before the snapshot. It must fail. | |
| 6.7 | Write down the data-loss window (backup timestamp → now) and how long the restore took. | |

**Passes when** 6.5 works on hardware that is not this Mac, and 6.7 is written down.

## 7. The reboot drill

Choose a time with no open order and no running research. If item 0 is still blocked on macOS's
background-task throttle ("Exceeded max notifications", see `MACOS_DEPLOYMENT.md`), do this
drill first: the reboot clears it, and `release.sh publish` can be retried right after 7.2.

| # | Step | Record |
|---|---|---|
| 7.1 | Fully restart the Mac. **Log in** (these are LaunchAgents — nothing runs at the login window). | |
| 7.2 | `deploy/macos/selfcheck.sh`. Every line ok. | |
| 7.3 | From the phone, without touching the Mac: console reachable, signed in, data current. | |
| 7.4 | `uv run ct-ops status` — heartbeats resumed. | |
| 7.5 | Record what happens on: unplugging power, display sleep, closing the lid, disconnecting the network, restarting the router. For each, note whether the service survived and what you had to do. | |
| 7.6 | Confirm FileVault status, that the Tailscale device is still authorised, and when the Robinhood credential next needs re-authorising. | |

**Passes when** 7.2 and 7.3 both come back with no manual intervention beyond the login in 7.1,
and 7.5 is written down honestly — including any case that needed a person.

### The emergency path, in advance

Remote pause cannot reach a Mac that is offline. Write down now, while nothing is wrong:

- the Robinhood app/web path to see and cancel an order yourself;
- that `micro_live_execution.enabled: false` means there is nothing at the venue to cancel today;
- who to contact and where the passphrase copy lives.

---

## What "passed" means

An acceptance record is only worth something if it contains the failures. For each section
above, record the date, the device, the ids involved, what you expected and what actually
happened. If something failed, note it and what you changed — do not re-run until it passes and
record only the last attempt.

Real money stays off until sections 1–7 pass **and** you separately decide to enable it. That
decision is item 13 in `TODO.md`, and nothing in this document constitutes it.
