"""Run triggers: scheduled review, manual request, event-triggered analysis.

Deliberately not a scheduler platform. The mature scheduler on this machine is the operating
system's own (launchd on macOS, cron elsewhere), so this module provides the command those
schedulers call and nothing more. Every trigger enters the same pipeline as a manual run, so
none of them can bypass Policy, Hard Risk, approval or execution-time revalidation.
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .api.service import Service, ServiceError

logger = logging.getLogger(__name__)

TRIGGERS = ("scheduled", "manual", "event")


def scheduled_review(service: Service, *, tickers: list[str] | None = None,
                     bucket: str = "core") -> dict[str, Any]:
    """A periodic portfolio review.

    Refuses to start when the system is paused, halted or out of Claude budget, so a cron entry
    firing into a halted system does nothing rather than piling up work.
    """
    health = service.health()
    if health["status"] in ("PAUSED", "HALTED", "CLAUDE_LIMIT_REACHED", "CONFIG_ERROR"):
        return {"started": [], "skipped": True, "reason": f"system status is {health['status']}",
                "at": datetime.now(UTC).isoformat()}

    profiles = service.profiles()
    if tickers is None:
        watch = profiles.investor_cfg("universe", "core", "etf_exposure_groups", default={}) or {}
        # One representative per exposure group, so a review never analyses overlapping funds.
        tickers = [group[0] for group in watch.values() if group][:3]
    started = [service.start_research(t, bucket=bucket, trigger="scheduled") for t in tickers]
    return {"started": started, "skipped": False, "at": datetime.now(UTC).isoformat()}


def event_analysis(service: Service, ticker: str, *, bucket: str = "core",
                   reason: str = "external event") -> dict[str, Any]:
    """Analysis triggered by something that happened, rather than by the clock."""
    health = service.health()
    if health["status"] in ("PAUSED", "HALTED", "CLAUDE_LIMIT_REACHED", "CONFIG_ERROR"):
        return {"started": None, "skipped": True, "reason": f"system status is {health['status']}"}
    run = service.start_research(ticker, bucket=bucket, trigger="event")
    run["event_reason"] = reason
    return {"started": run, "skipped": False}


def main(argv: list[str] | None = None) -> int:
    """``ct-trigger``: the entry point a scheduler calls."""
    parser = argparse.ArgumentParser(
        prog="ct-trigger",
        description="Start a research run from a scheduler, a person, or an event. "
                    "Never bypasses Policy, Hard Risk, approval or revalidation.",
        epilog="macOS: schedule with launchd. Linux: a crontab line calling this command. "
               "Example crontab entry for a weekday review at 16:30 New York time:\n"
               "  30 16 * * 1-5 cd /path/to/agentic_trading && .venv/bin/ct-trigger scheduled",
    )
    parser.add_argument("kind", choices=TRIGGERS)
    parser.add_argument("--ticker", action="append", dest="tickers",
                        help="repeatable; defaults to one representative per core exposure group")
    parser.add_argument("--bucket", choices=["core", "sandbox", "micro_live"], default="core")
    parser.add_argument("--reason", default="external event", help="event triggers only")
    parser.add_argument("--config-dir", default="config")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

    service = Service(args.config_dir)
    try:
        if args.kind == "scheduled":
            result = scheduled_review(service, tickers=args.tickers, bucket=args.bucket)
        elif args.kind == "event":
            if not args.tickers:
                parser.error("an event trigger needs at least one --ticker")
            result = event_analysis(service, args.tickers[0], bucket=args.bucket, reason=args.reason)
        else:
            if not args.tickers:
                parser.error("a manual trigger needs at least one --ticker")
            result = {"started": [service.start_research(t, bucket=args.bucket, trigger="manual")
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
                                 "skipped": result.get("skipped"), "reason": result.get("reason")}) + "\n")
    except (OSError, ServiceError):
        pass  # a trigger must never fail because its own log could not be written
