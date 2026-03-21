"""One pass over an account under a plan, and the record that the pass was made.

``trader.tick`` decides what a pass does. This module is what runs one: it settles what is
outstanding first, keeps the simulated book dated, calls the trader, and writes down that the
pass happened and whether anything was wrong with it. That last part is the whole of what
"thirty sessions of simulation" will ever mean (docs/todo.md, P3.2), so it is recorded here, by
the thing that ran, and counted from the record -- never asserted by whoever wants the number.

The order inside a pass is fixed, and each step is a reason the next may not go ahead:

1. orders whose outcome is unknown are reconciled -- nothing new is decided on a book that
   might be about to change;
2. the simulated book is dated to the session, once nothing is outstanding against it;
3. the trader makes its pass;
4. the pass is recorded against the strategy version that is opening positions, and what the
   account was worth is noted for the session.

A pass that fails is still a pass that is recorded: it marks the session as one on which
something was wrong, which is the honest thing for a failure to do to a count of good days.
Nothing here places, approves or cancels an order.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from ..micro_live import BUCKET as ACCOUNT
from ..proposals import EXECUTED, needs_reconciliation
from ..storage import file_lock
from . import paper, positions, store, trader

logger = logging.getLogger("agentic_trading.service")

NEW_YORK = ZoneInfo("America/New_York")

#: What can be wrong with a pass. Any one of them on any pass of a session keeps that session
#: out of the count; each is a code, because a screen has to say which it was.
PASS_FAILED = "pass_failed"                  # the book could not be read or priced, or the pass raised
PAUSED = "paused"                            # a person, or the loss line, had the plan stopped
AUTONOMY_WITHDRAWN = "autonomy_withdrawn"    # an order had ended in a way nobody could account for
UNKNOWN_ORDER = "unknown_order"              # an order's outcome was not known
RESULT_UNKNOWN = "result_unknown"            # a position did not match the fills on record
REGIME_UNKNOWN = "regime_unknown"            # the index and volatility data could not be read
BOOK_UNCONFIRMED = "book_unconfirmed"        # the simulated book could not be dated to the session
PROBLEMS = (PASS_FAILED, PAUSED, AUTONOMY_WITHDRAWN, UNKNOWN_ORDER, RESULT_UNKNOWN, REGIME_UNKNOWN,
            BOOK_UNCONFIRMED)


def session_of(now: datetime) -> str:
    """The session a moment belongs to: the market's own date, or the weekday before a weekend."""
    day = now.astimezone(NEW_YORK).date()
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day.isoformat()


def settle(service: Any, profiles: Any) -> int:
    """Reconcile orders whose outcome is not on record. Returns how many needed it."""
    waiting = [p for p in service._store(profiles).all()
               if p.bucket == ACCOUNT and needs_reconciliation(p)]
    if waiting:
        service.reconcile_orders()
    return len(waiting)


def confirm_book(service: Any, profiles: Any, session: str) -> str | None:
    """Date the simulated book to this session. Returns why it could not be, or None.

    The simulated book has no account to be read back against -- its venue holds what it holds
    -- so what can be checked is what a book with no venue is always checked on: nothing is
    outstanding against it, and every order this system executed for it has reached it
    (``portfolio.confirm_unbacked``). A real account's book is confirmed against its venue by
    the daily job that already does that, and is not touched from here.
    """
    from ..portfolio import PortfolioError, confirm_unbacked, load_portfolio

    if not paper.is_paper(profiles):
        return None
    path = paper.ledger_path(profiles)
    try:
        ledger = load_portfolio(path)
        if str(ledger.as_of) >= session:
            return None
        proposals = service._store(profiles).all()
        if any(p.bucket == ACCOUNT and needs_reconciliation(p) for p in proposals):
            return "an order is still outstanding against the book"
        confirm_unbacked(path, trade_date=session, executed_orders={
            p.client_order_id for p in proposals
            if p.bucket == ACCOUNT and p.status == EXECUTED and p.client_order_id})
    except (PortfolioError, OSError, ValueError) as exc:
        return f"{type(exc).__name__}: {exc}"[:300]
    return None


def problems_of(tick: trader.Tick | None, *, error: str | None, unconfirmed: str | None) -> list[str]:
    """What was wrong with this pass, as codes. Empty when nothing was."""
    found: list[str] = []
    if error or tick is None:
        found.append(PASS_FAILED)
    if unconfirmed:
        found.append(BOOK_UNCONFIRMED)
    if tick is not None:
        health = tick.health or {}
        if tick.paused:
            found.append(PAUSED)
        if health.get("autonomy_withdrawn"):
            found.append(AUTONOMY_WITHDRAWN)
        if health.get("unknown_orders"):
            found.append(UNKNOWN_ORDER)
        if tick.loss is not None and not tick.loss.get("known", True):
            found.append(RESULT_UNKNOWN)
        if health and not health.get("regime_available", True):
            found.append(REGIME_UNKNOWN)
    return found


def exits_verified(service: Any, profiles: Any, version_id: str) -> bool:
    """Whether this version has carried a position from its own purchase to its own sale."""
    trades = positions.closed_trades(fills=service._fills_for_replay(profiles),
                                     proposals=service._store(profiles).all(), bucket=ACCOUNT)
    return any(row["version"] == version_id and row["closed_by"] == "code" for row in trades)


#: Who took a plan out of force, or moved it to a new version, when nobody did: the running code
#: was no longer the code the plan's strategy version names.
CODE_CHANGED = "auto:code_changed"


def keep_version_current(service: Any, profiles: Any, db: Any,
                         plan: dict[str, Any] | None) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Hold the plan to the code that is running. Returns the plan as it now stands, and what moved.

    A strategy version is its parameters *and* the code, the models and the mandate that carry
    them out (``auto.store``). After a release that changed any of those, the plan's opening
    version names something that is no longer running, and what is running has no record of its
    own. The two accounts answer that differently, and the difference is the point:

    * the simulated account goes on, under the new version, building that version's record from
      nothing. That is what the simulated account is for;
    * the real account stops opening. Its plan was confirmed for a version that had earned its
      place; the version now running has not, and nothing here may decide that it is close
      enough. The plan is taken out of force, what it bought is still closed on the terms it was
      bought under, and a person starts a plan again once the new version has its own record.
    """
    if plan is None or not store.moved_on(profiles, plan):
        return plan, None
    was, now_running = plan["opening_version"], store.running_version_id(profiles, plan)
    if paper.is_paper(profiles):
        updated = store.rebind(db, profiles, identity=CODE_CHANGED)
        service._journal(profiles, kind="auto_strategy_adjusted", subject=now_running,
                         actor=CODE_CHANGED, outcome="active",
                         detail={"scope": "paper", "replaces": was, "reason": "code_changed"})
        return updated, {"from": was, "to": now_running, "action": "rebound"}
    ended = store.withdraw_plan(
        db, identity=CODE_CHANGED,
        reason=(f"the trading logic, the models or the mandate changed: strategy version {was} "
                f"is no longer what is running, and {now_running} has no record of its own yet"))
    service._journal(profiles, kind="auto_plan_stopped", subject=(ended or plan)["plan_id"],
                     actor=CODE_CHANGED, outcome="withdrawn",
                     detail={"scope": "live", "reason": "version_not_validated",
                             "was": was, "now_running": now_running})
    return None, {"from": was, "to": now_running, "action": "withdrawn"}


def run(service: Any, *, research_from: Path | None = None, now: datetime | None = None,
        approve: bool = True, rebind: bool = True) -> dict[str, Any]:
    """One pass over the account ``service`` is for. Returns what it did and what it recorded.

    ``rebind`` is whether this pass may hold the plan to the code it is itself running
    (:func:`keep_version_current`). The service's own loop always may: it is what runs the
    account. A pass somebody starts by hand may be running other code -- a terminal in a
    checkout, beside a service started from a release -- and one made from there would move
    the plan to that code's version and back again on the service's next pass. So a pass that
    is not allowed to rebind does not run at all when the versions differ, and says why.
    """
    now = now or datetime.now(UTC)
    profiles = service.profiles()
    db = service.state_db(profiles)
    session = session_of(now)
    out: dict[str, Any] = {"session": session, "at": now.isoformat(), "ran": False,
                           "skipped": None, "error": None, "recorded": None, "version_moved": None}
    plan = store.active_plan(db)
    if plan is None and not store.versions(db):
        out["skipped"] = "no plan is in force"
        return out
    if not rebind and store.moved_on(profiles, plan):
        out["skipped"] = (
            f"the code running here is not the plan's strategy version ({plan['opening_version']} "
            f"is on record; this code is {store.running_version_id(profiles, plan)}). A pass from "
            "here would move the plan to a new version with an empty record, so none was made")
        return out

    # One pass at a time for an account: the loop and a command a person ran must not both be
    # deciding the same research at once. Orders are safe either way -- approval has its own
    # account lock -- but a research run would be decided twice.
    with file_lock(db.path.with_name("auto.pass")):
        tick: trader.Tick | None = None
        try:
            plan, out["version_moved"] = keep_version_current(service, profiles, db, plan)
        except Exception as exc:
            # Not knowing which version is running is not a reason to go on under the old one.
            logger.exception("the strategy version could not be checked against the running code")
            out["error"] = f"{type(exc).__name__}: {exc}"[:300]
            out["recorded"] = _record(service, profiles, db, plan, None, session=session, now=now,
                                      error=out["error"], unconfirmed=None)
            return out
        try:
            out["settled"] = settle(service, profiles)
            unconfirmed = confirm_book(service, profiles, session)
            tick = trader.tick(service, runs_dir=research_from, trade_date=session,
                               today=now.astimezone(NEW_YORK).date(), approve=approve)
        except Exception as exc:
            # A failed pass is recorded as one, not lost: see problems_of.
            logger.exception("an automatic-trading pass failed")
            out["error"] = f"{type(exc).__name__}: {exc}"[:300]
            unconfirmed = None
        if tick is not None:
            out.update(tick.as_dict())
        out["book_unconfirmed"] = unconfirmed
        out["recorded"] = _record(service, profiles, db, plan, tick, session=session, now=now,
                                  error=out["error"], unconfirmed=unconfirmed)
    return out


def _record(service: Any, profiles: Any, db: Any, plan: dict[str, Any] | None,
            tick: trader.Tick | None, *, session: str, now: datetime, error: str | None,
            unconfirmed: str | None) -> dict[str, Any]:
    """Write down that the pass happened: against the opening version, and for the account."""
    recorded: dict[str, Any] = {"problems": problems_of(tick, error=error, unconfirmed=unconfirmed)}
    try:
        market_open = bool(service.market_status().is_open)
    except Exception:  # noqa: BLE001 - an unreadable calendar is a closed market, as everywhere
        market_open = False
    recorded["market_open"] = market_open
    store.note_pass(db, at=now, error=error, market_open=market_open)
    if tick is not None and tick.account:
        store.mark_equity(db, session, at=now, figures={
            **tick.account, "version": (plan or {}).get("opening_version")})
    if plan is None:
        return recorded
    version_id = plan["opening_version"]
    row = store.mark_pass(db, version_id, session, at=now, market_open=market_open,
                          problems=recorded["problems"])
    days = store.simulated_days(db, version_id)
    verified = exits_verified(service, profiles, version_id)
    current = (store.version(db, version_id) or {}).get("validation") or {}
    if current.get("simulated_days") != days or bool(current.get("exits_verified")) != verified:
        store.update_validation(db, version_id, simulated_days=days, exits_verified=verified,
                                status=store.SIMULATING if current.get("status") in (
                                    None, store.UNVALIDATED) else current.get("status"))
    recorded.update(version=version_id, session_valid=store.session_valid(row),
                    simulated_days=days, exits_verified=verified)
    return recorded
