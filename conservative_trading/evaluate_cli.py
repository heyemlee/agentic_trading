"""``ct-evaluate``: declare a forward study, mark it daily, and print its report.

The report is the deliverable. It is meant to be readable by someone deciding whether to keep
paying for this approach, which means it has to contain the days it did nothing, the runs that
failed and the trades that lost money, not a selection of the good ones.
"""

from __future__ import annotations

import argparse
import json
import sys

from . import evaluation
from .api.service import Service, ServiceError
from .env import load_env


def _account(service: Service) -> dict:
    """The conceptual paper account: the thing under study. Real money is not evaluated here."""
    portfolio = service.portfolio()
    account = (portfolio.get("accounts") or {}).get("conceptual")
    if account is None:
        raise evaluation.EvaluationError(
            "the conceptual account could not be read: " + "; ".join(
                f"{k}: {v}" for k, v in (portfolio.get("errors") or {}).items()))
    return account


def main(argv: list[str] | None = None) -> int:
    load_env()
    parser = argparse.ArgumentParser(
        prog="ct-evaluate",
        description="A forward record of what this system decided, against a passive benchmark.",
        epilog="Start once, snapshot daily (the maintenance job does this), report any time. "
               "A short record is a record, not evidence: every report says how long it has run "
               "and what was declared as the minimum before it can support a judgement.")
    parser.add_argument("action", choices=("start", "snapshot", "report", "status", "stop"))
    parser.add_argument("--config-dir", default="config")
    parser.add_argument("--date", help="the session to record; defaults to the last completed one")
    parser.add_argument("--note", default="", help="start: why this study exists")
    parser.add_argument("--reason", default="", help="stop: why it ended")
    args = parser.parse_args(argv)

    service = Service(args.config_dir)
    try:
        profiles = service.profiles()
        db = service.state_db(profiles)
        on = args.date or service.portfolio()["as_of"]

        if args.action == "start":
            account = _account(service)
            study = evaluation.start(db, profiles, on=on, equity_usd=account["equity_usd"],
                                     net_deposits_usd=account["net_deposits_usd"], note=args.note)
            print(json.dumps(study, indent=2))
            return 0
        if args.action == "stop":
            if not args.reason:
                parser.error("stopping a study needs --reason; it becomes part of the record")
            print(json.dumps(evaluation.stop(db, args.reason), indent=2))
            return 0
        if args.action == "snapshot":
            account = _account(service)
            result = evaluation.snapshot(db, profiles, on=on, equity_usd=account["equity_usd"],
                                         net_deposits_usd=account["net_deposits_usd"])
            print(json.dumps(result, indent=2))
            return 0 if result.get("recorded") else 1
        if args.action == "status":
            print(json.dumps(db.read_meta(evaluation.STUDY, {"state": "NOT_STARTED"}), indent=2))
            return 0

        print(json.dumps(service.evaluation_report(), indent=2))
    except (evaluation.EvaluationError, ServiceError) as exc:
        print(json.dumps({"error": str(exc)}, indent=2))
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
