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
from .env import load_env
from .service import Service, ServiceError
from .service.shapes import PortfolioAccount


def _account(service: Service, scope: str) -> PortfolioAccount:
    """The account under study: the Demo paper book, or the Agentic account for its own study."""
    who = evaluation.ACCOUNTS[scope]
    portfolio = service.portfolio()
    account = portfolio.accounts.get(who["account_id"])
    if account is None:
        raise evaluation.EvaluationError(
            f"the {who['account_label']} account could not be read: " + "; ".join(
                f"{k}: {v}" for k, v in portfolio.errors.items()))
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
    parser.add_argument("--scope", choices=(*evaluation.SCOPES, "route_quality"), default=evaluation.DEFAULT_SCOPE,
                        help="which account's study: demo (the paper book, the default) or agentic "
                             "(the real-money account, its own record from its own start); "
                             "route_quality is a report only")
    parser.add_argument("--date", help="the session to record; defaults to the last completed one")
    parser.add_argument("--note", default="", help="start: why this study exists")
    parser.add_argument("--reason", default="", help="stop: why it ended")
    args = parser.parse_args(argv)
    scope = args.scope
    if scope == "route_quality" and args.action != "report":
        parser.error("route_quality is a report, not a study; it cannot be started, marked or stopped")

    service = Service(args.config_dir)
    try:
        profiles = service.profiles()
        db = service.state_db(profiles)

        if args.action == "start":
            account = _account(service, scope)
            on = args.date or service.portfolio().as_of
            study = evaluation.start(db, profiles, on=on, equity_usd=account.equity_usd,
                                     net_deposits_usd=account.net_deposits_usd, note=args.note,
                                     scope=scope)
            print(json.dumps(study, indent=2))
            return 0
        if args.action == "stop":
            if not args.reason:
                parser.error("stopping a study needs --reason; it becomes part of the record")
            print(json.dumps(evaluation.stop(db, args.reason, scope=scope), indent=2))
            return 0
        if args.action == "snapshot":
            result = service.evaluation_snapshot(on=args.date, scope=scope)
            print(json.dumps(result, indent=2))
            return 0 if result.get("recorded") else 1
        if args.action == "status":
            study_key, _ = evaluation.scope_keys(scope)
            print(json.dumps(db.read_meta(study_key, {"scope": scope, "state": "NOT_STARTED"}), indent=2))
            return 0

        print(json.dumps(service.evaluation_report(scope), indent=2))
    except (evaluation.EvaluationError, ServiceError) as exc:
        print(json.dumps({"error": str(exc)}, indent=2))
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
