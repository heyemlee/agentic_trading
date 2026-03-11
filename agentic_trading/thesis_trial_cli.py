"""Run the fixed-evidence Thesis Tracker screening trial outside production state."""

from __future__ import annotations

import argparse
from pathlib import Path

from .env import load_env
from .llm.claude_code import ClaudeCodeError
from .profile import load_profiles
from .thesis_trial import ROUTES, failed_case, load_cases, report_name, run_case, write_report


def main(argv: list[str] | None = None) -> int:
    load_env()
    parser = argparse.ArgumentParser(prog="ct-thesis-trial",
                                     description="Run one no-tools route against frozen thesis cases.")
    parser.add_argument("--config-dir", default="config")
    parser.add_argument("--cases", required=True, help="public frozen case-pack JSON")
    parser.add_argument("--output-dir", required=True,
                        help="empty or dedicated trial output directory; never state/ or runs/")
    parser.add_argument("--route", choices=ROUTES, required=True)
    parser.add_argument("--limit", type=int, default=None, help="run only the first N cases")
    args = parser.parse_args(argv)

    profiles = load_profiles(args.config_dir)
    cases = load_cases(args.cases)
    if args.limit is not None:
        if args.limit < 1:
            parser.error("--limit must be positive")
        cases = cases[:args.limit]
    root = Path(args.output_dir).resolve()
    protected = {profiles.project_dir.resolve() / "state", profiles.project_dir.resolve() / "runs"}
    if root in protected or any(parent in protected for parent in root.parents):
        parser.error("--output-dir must not be state/ or runs/")

    rows = []
    rate_limited = False
    for case in cases:
        try:
            rows.append(run_case(case, route=args.route, model=profiles.tradingagents.quick_think_llm,
                                 effort=profiles.llm.quick_effort,
                                 timeout_seconds=profiles.llm.timeout_seconds,
                                 output_dir=root, claude_bin=profiles.llm.claude_bin))
        except ClaudeCodeError as exc:
            rows.append(failed_case(case, route=args.route, error=exc))
            if exc.rate_limited:
                rate_limited = True
                break
    report = write_report(root / report_name(args.route), rows)
    print(report)
    return 3 if rate_limited else 0


if __name__ == "__main__":
    raise SystemExit(main())
