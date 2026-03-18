"""Run triggers: scheduled review, manual request, event-triggered analysis.

Deliberately not a scheduler platform. The mature scheduler on this machine is the operating
system's own (launchd on macOS, cron elsewhere), so this module provides the command those
schedulers call and nothing more. Every trigger enters the same pipeline as a manual run, so
none of them can bypass Policy, Hard Risk, approval or execution-time revalidation.

Whether a given invocation should do anything at all is decided in :mod:`schedule`, from the
exchange calendar, the period already executed and the day's budget. This module carries out
that decision and records what came of it.
"""

from __future__ import annotations

import argparse
import json
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import operations, schedule
from .env import load_env
from .service import Service, ServiceError

logger = logging.getLogger(__name__)

TRIGGERS = ("scheduled", "manual", "event")


#: Why a scheduled run was asked for, by the kind of task that asked. A holdings review is a
#: review of something the book holds, which is what ``purpose`` means downstream; the single
#: legacy review names no purpose, so it behaves exactly as it always has. A name from the
#: owner's watchlists is a candidate like any other.
PURPOSE_OF_KIND = {schedule.HOLDINGS: "review_holding", schedule.CANDIDATES: "candidate",
                   schedule.WATCHLIST: "candidate"}


def scheduled_review(service: Service, *, tickers: list[str] | None = None,
                     bucket: str | None = None, now: datetime | None = None,
                     force: bool = False) -> dict[str, Any]:
    """The periodic reviews, one per configured account and kind, holdings before candidates.

    Each task claims its own period before starting anything, so a repeated trigger, a launchd
    catch-up after a restart and a manual rerun all resume the same execution instead of
    analysing the same week twice, and one account's review can never mark another's as run. A
    skip is recorded with its reason, because "why did nothing run on Friday" has to be
    answerable from the phone.

    The day's allowance is re-read between tasks. Deciding every task against the count taken
    before any of them started would let holdings and candidates together spend more than the
    subscription allows, which is the one thing more concurrency cannot fix.
    """
    from .advisor import SNAPSHOT

    profiles = service.profiles()
    db = service.state_db(profiles)
    cfg = schedule.settings(profiles)
    health = service.health()
    history = service.research_history(limit=200)
    # The owner's own holdings and watchlists, as Robinhood was last read. The run does not read
    # the venue itself: a personal review refreshes the reading when it starts, and refuses a
    # holding that is no longer there.
    snapshot = db.read_meta(SNAPSHOT, {}) or {}
    plan = schedule.tasks(cfg, bucket=bucket)
    results: list[dict[str, Any]] = []

    for task in plan:
        decision = schedule.decide(
            profiles, task=task,
            subjects=[t.upper() for t in tickers] if tickers else
            schedule.subjects_for(profiles, task, history=history, snapshot=snapshot),
            health_status=health.status, paused=bool(health.paused),
            executed=db.read_meta(schedule.EXECUTIONS, {}) or {},
            jobs=service.research_jobs(limit=200), now=now, force=force)
        results.append(_run_task(service, db, task, decision, now=now, force=force,
                                 snapshot=snapshot))

    at = datetime.now(UTC).isoformat()
    if len(results) == 1:
        return {**results[0], "tasks": results, "at": at}
    started = [run for result in results for run in result["started"]]
    reasons = [f"{result['schedule']['bucket']}:{result['schedule']['kind']}: {result['reason']}"
               for result in results if result.get("reason")]
    return {"started": started, "skipped": not started, "reason": "; ".join(reasons),
            "errors": [error for result in results for error in result.get("errors") or []],
            "schedule": results[0]["schedule"] if results else {}, "tasks": results, "at": at}


def _run_task(service: Service, db, task: schedule.Task, decision: schedule.Decision, *,
              now: datetime | None, force: bool,
              snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    """Carry out one task's decision and record what came of it."""
    at = datetime.now(UTC).isoformat()
    if not decision.run:
        schedule.record_skip(db, decision, now=now)
        # A skip is the schedule working, so it counts as the activity having run.
        operations.record(db, "schedule", f"skipped: {decision.reason}", min_interval_seconds=0)
        return {"started": [], "skipped": True, "reason": decision.reason,
                "schedule": decision.as_dict(), "at": at}
    if not force and not schedule.claim(db, decision, now=now):
        decision.run = False
        decision.reason = f"another invocation already claimed {decision.period_key}"
        return {"started": [], "skipped": True, "reason": decision.reason,
                "schedule": decision.as_dict(), "at": at}

    started, errors = [], []
    for ticker in decision.tickers:
        try:
            # The calendar decides whether to run and which period this is; it does not decide
            # which session to analyse. A session can be closed hours before its prices are
            # published, and anchoring to a date with no data yields an UNKNOWN regime and a
            # run that looks healthy while reasoning from evidence it does not have. The
            # service picks the newest session that actually has data.
            if task.bucket == schedule.PERSONAL:
                started.append(_review_personal_holding(service, task, ticker, snapshot))
            else:
                started.append(service.start_research(ticker, bucket=task.bucket, trigger="scheduled",
                                                      purpose=PURPOSE_OF_KIND.get(task.kind)))
        except ServiceError as exc:      # one refused ticker must not lose the others
            errors.append({"ticker": ticker, "error": str(exc)})
    # Bookkeeping must not be able to fail a run that already started.
    analysed = sorted({run["trade_date"] for run in started if run.get("trade_date")})
    schedule.record_result(db, decision.period_key, state="STARTED",
                           run_keys=[run["run_key"] for run in started], errors=errors,
                           bucket=task.bucket, kind=task.kind,
                           calendar_session=decision.trade_date, analysed_sessions=analysed,
                           finished_at=datetime.now(UTC).isoformat())
    operations.record(db, "schedule", f"{len(started)} run(s) started for {decision.trade_date}",
                      min_interval_seconds=0)
    return {"started": started, "skipped": False, "errors": errors,
            "schedule": decision.as_dict(), "at": at}


def _review_personal_holding(service: Service, task: schedule.Task, ticker: str,
                             snapshot: dict[str, Any] | None) -> dict[str, Any]:
    """One holding of the owner's own accounts, reviewed read-only in its account's context.

    The same analysis the console starts from a holding: the research graph runs, and its
    answer is advice about an account this system never trades. It is bound to the account
    that holds the name, which the service checks against the snapshot again.
    """
    account_id = schedule.personal_holdings(snapshot).get(ticker.upper())
    if account_id is None:
        raise ServiceError(f"{ticker} is not held in the last reading of your own accounts")
    return service.start_advisor(kind="analysis", request_id=str(uuid.uuid4()), ticker=ticker,
                                 account_id=account_id, language=task.language or "zh",
                                 scope="personal", trigger="scheduled")


def event_analysis(service: Service, ticker: str, *, bucket: str = "core",
                   reason: str = "external event") -> dict[str, Any]:
    """Analysis triggered by something that happened, rather than by the clock."""
    health = service.health()
    profiles = service.profiles()
    cfg = schedule.settings(profiles)
    if health.status in schedule.BLOCKING_STATUS or (
            health.paused and not cfg.get("run_while_paused", True)):
        return {"started": None, "skipped": True, "reason": f"system status is {health.status}"}
    run = service.start_research(ticker, bucket=bucket, trigger="event")
    run["event_reason"] = reason
    return {"started": run, "skipped": False}


def main(argv: list[str] | None = None) -> int:
    """``ct-trigger``: the entry point a scheduler calls."""
    load_env()
    parser = argparse.ArgumentParser(
        prog="ct-trigger",
        description="Start a research run from a scheduler, a person, or an event. "
                    "Never bypasses Policy, Hard Risk, approval or revalidation.",
        epilog="macOS: schedule with launchd (deploy/macos/install.sh). Linux: a crontab line "
               "calling this command. The command re-checks the exchange calendar, the period "
               "already executed and the daily budget, so firing it more often than needed is "
               "safe and simply records why it declined.",
    )
    parser.add_argument("kind", choices=TRIGGERS)
    parser.add_argument("--ticker", action="append", dest="tickers",
                        help="repeatable; defaults to one representative per core exposure group")
    # No default here. The launchd job names no bucket, and for a scheduled trigger that has to
    # mean every configured task: a default of core narrowed it to core's, so naming an account
    # in research_schedule.accounts ran nothing, recorded no skip, and left the tasks showing as
    # due. Manual and event triggers still research core unless told otherwise.
    parser.add_argument("--bucket", choices=["core", "sandbox", "micro_live"], default=None,
                        help="scheduled: narrow to one account's tasks (default: every task); "
                             "manual and event: the book to research (default: core)")
    parser.add_argument("--reason", default="external event", help="event triggers only")
    parser.add_argument("--force", action="store_true",
                        help="scheduled triggers only: run again inside an executed period")
    parser.add_argument("--dry-run", action="store_true",
                        help="scheduled triggers only: report the decision without starting work")
    parser.add_argument("--config-dir", default="config")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

    service = Service(args.config_dir)
    try:
        if args.kind == "scheduled":
            if args.dry_run:
                result = service.schedule_status()
            else:
                result = scheduled_review(service, tickers=args.tickers, bucket=args.bucket,
                                          force=args.force)
        elif args.kind == "event":
            if not args.tickers:
                parser.error("an event trigger needs at least one --ticker")
            result = event_analysis(service, args.tickers[0], bucket=args.bucket or "core",
                                    reason=args.reason)
        else:
            if not args.tickers:
                parser.error("a manual trigger needs at least one --ticker")
            result = {"started": [service.start_research(t, bucket=args.bucket or "core", trigger="manual")
                                  for t in args.tickers], "skipped": False}
    except ServiceError as exc:
        print(f"trigger failed: {exc}")
        return 2
    print(json.dumps(result, indent=2, default=str))
    _append_trigger_log(service, args.kind, result)
    return 0


def _append_trigger_log(service: Service, kind: str, result: dict[str, Any]) -> None:
    try:
        profiles = service.profiles()
        path = Path(profiles.project_dir) / "runs" / "triggers.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"at": datetime.now(UTC).isoformat(), "kind": kind,
                                 "skipped": result.get("skipped"), "reason": result.get("reason"),
                                 "period_key": (result.get("schedule") or {}).get("period_key"),
                                 }) + "\n")
    except (OSError, ServiceError):
        pass  # a trigger must never fail because its own log could not be written
