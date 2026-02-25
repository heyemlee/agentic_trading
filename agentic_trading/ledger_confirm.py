"""Renewing a ledger's as_of against something that can contradict it.

`portfolio_state.max_age_days` fails a ledger closed once nothing has confirmed it for that
many days. Clearing that by writing today's date over a book nobody read back would leave the
guard measuring paperwork instead of truth, so confirmation is a check that can refuse, and
this is the one implementation of it: `ct-state confirm` and the console endpoint are two doors
into this function, never two opinions about what confirming means.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .portfolio import (
    CashMismatch,
    confirm_from_broker,
    confirm_unbacked,
    last_session_date,
    load_portfolio,
)
from .profile import Profiles
from .proposals import EXECUTED
from .state_db import StateDB

logger = logging.getLogger(__name__)

#: Where the last check leaves its report, refusal reasons included, so the notification pass
#: can say why a ledger was not confirmed without opening the venue again.
REPORT = "ledger_confirm_report"


def ledger_paths(profiles: Profiles) -> dict[str, Path]:
    portfolio_cfg = profiles.risk.get("portfolio_state") or {}
    micro_cfg = profiles.risk.get("micro_live_execution") or {}
    return {
        "conceptual": profiles.project_dir / str(portfolio_cfg.get("file", "state/portfolio.yaml")),
        "micro_live": profiles.project_dir / str(micro_cfg.get(
            "ledger_file", "state/micro_live_portfolio.yaml")),
    }


def _robinhood(profiles: Profiles):
    from .broker.robinhood import RobinhoodBroker

    return RobinhoodBroker(profiles)


def _hand_back(broker) -> None:
    """Close the venue session here rather than leaving it to the interpreter.

    An MCP session left open is terminated from a finaliser on the way out, by which point the
    thread pool that would carry the request is already gone — "cannot schedule new futures
    after shutdown", printed after every otherwise successful confirm. Closing it is also what
    tells the venue the session is over instead of leaving it to time out.
    """
    try:
        close = getattr(broker, "close", None)
        if close is not None:
            close()
    except Exception as exc:  # noqa: BLE001 - a session that will not close must not fail a check
        logger.debug("error closing the venue session: %s", exc)


def confirm_ledgers(profiles: Profiles, store, *, ledger: str = "all", trade_date: str | None = None,
                    dry_run: bool = False, broker_factory=_robinhood) -> dict[str, Any]:
    """Confirm one ledger or both, and report per book what was checked or what refused.

    * micro_live is rebuilt from the venue's own cash and quantities, which refuses a position
      the ledger cannot attribute to a bucket and cash that does not match. That is the same
      rebuild execution already trusts, so nothing is believed here that would not be believed
      at order time.
    * the conceptual book has no account to read back, so it is held against this system's own
      record: every order it executed for those buckets must already be applied to it.

    Neither runs while the broker still owns an order — the ledgers are about to change again,
    and dating a book mid-flight is the thing this is supposed to prevent.
    """
    from .state_cli import unsettled_orders

    date = trade_date or last_session_date()
    report: dict[str, Any] = {"date": date, "dry_run": dry_run, "confirmed": False,
                              "unsettled_orders": unsettled_orders(store.db), "ledgers": []}
    if report["unsettled_orders"]:
        report["detail"] = "settle these before confirming a ledger"
        return _remember(store.db, report)

    wanted = ledger_paths(profiles)
    if ledger != "all":
        wanted = {ledger: wanted[ledger]}
    executed = {p.client_order_id: p.bucket for p in store.all()
                if p.status == EXECUTED and p.client_order_id}

    for name, path in wanted.items():
        if not path.exists() and StateDB(path).read_document(path) is None:
            continue
        row = {"ledger": name, "file": path.name, "confirmed": False}
        try:
            if name == "micro_live":
                broker = broker_factory(profiles)
                try:
                    state = confirm_from_broker(
                        path, account=broker.get_account(), broker_positions=broker.get_positions(),
                        profiles=profiles, trade_date=date, dry_run=dry_run).state
                finally:
                    _hand_back(broker)
                row["checked_against"] = "the venue's own cash and positions"
            else:
                buckets = set(load_portfolio(path).buckets)
                state = confirm_unbacked(
                    path, trade_date=date, dry_run=dry_run,
                    executed_orders={cid for cid, bucket in executed.items() if bucket in buckets})
                row["checked_against"] = "this system's executed orders; no venue behind this book"
            row.update(confirmed=True, as_of=state.as_of, positions=len(state.positions),
                       cash_usd=round(sum(b.cash_usd for b in state.buckets.values()), 2))
        except Exception as exc:  # noqa: BLE001 - anything that stops the check refuses the date
            row["error"] = f"{type(exc).__name__}: {exc}"
            if isinstance(exc, CashMismatch):
                # The figures, so the Portfolio screen can offer to record the difference as
                # what the person says it was, amount filled in. Never booked from here.
                row.update(bucket=exc.bucket, cash_difference_usd=exc.difference_usd,
                           venue_cash_usd=exc.venue_cash_usd, ledger_cash_usd=exc.ledger_cash_usd)
        report["ledgers"].append(row)

    report["confirmed"] = bool(report["ledgers"]) and all(r["confirmed"] for r in report["ledgers"])
    return _remember(store.db, report)


def _remember(db, report: dict[str, Any]) -> dict[str, Any]:
    """Keep the last real check where the notification pass and the System screen read it.

    Whoever ran it -- the daily job, the CLI, the console button -- the report is the same
    fact about the ledgers, so a refusal the job found is cleared by the confirmation a person
    then ran, and not only by the next morning's job. A dry run changes nothing and is not
    remembered. Agreement is the activity having run; a refusal is not, so a book that keeps
    refusing also shows up as an overdue activity.
    """
    if report.get("dry_run"):
        return report
    from . import operations

    try:
        db.write_meta(REPORT, {**report, "at": datetime.now(UTC).isoformat()})
    except Exception:
        logger.debug("could not keep the ledger confirmation report", exc_info=True)
    if report["confirmed"]:
        operations.record(db, "ledger_confirm",
                          "; ".join(f"{row['ledger']} as_of {row.get('as_of')}"
                                    for row in report["ledgers"]),
                          min_interval_seconds=0)
    return report
