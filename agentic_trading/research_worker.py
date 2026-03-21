"""One durable research worker. Started by :mod:`research_jobs`, never by the model."""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys

from .cli import run_one
from .diagnosis import EXIT_LIMIT, EXIT_OK, EXIT_TIMEOUT, failure_for_exit
from .profile import load_profiles
from .proposals import ProposalStore
from .research_jobs import ResearchJobStore, _now


class ResearchTimeout(RuntimeError):
    pass


def _timeout(_signum, _frame):
    raise ResearchTimeout("research worker deadline reached")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ct-research-worker")
    parser.add_argument("--config-dir", required=True)
    parser.add_argument("--run-key", required=True)
    args = parser.parse_args(argv)

    profiles = load_profiles(args.config_dir)
    proposal_path = ProposalStore(
        profiles.project_dir / str((profiles.risk.get("broker") or {}).get(
            "state_file", "state/proposals.json"))
    ).path
    store = ResearchJobStore(proposal_path)
    job = store.get(args.run_key)
    if job is None or job.get("state") != "RUNNING":
        return 2
    store.update(args.run_key, {"pid": os.getpid()}, expected_states=("RUNNING",))

    prior_handler = signal.signal(signal.SIGALRM, _timeout)
    signal.setitimer(signal.ITIMER_REAL, int(job["timeout_seconds"]))
    try:
        if job.get("task"):
            from .advisor import run_task

            code = run_task(profiles, job)
        else:
            code = run_one(
                profiles, job["ticker"], job["trade_date"], job["bucket"],
                job["instrument_type"], profiles.project_dir / "runs", False,
                run_id=args.run_key, authority_version_at_submit=job.get("authority_version"),
                purpose=job.get("purpose"),
            )
        record_path = profiles.project_dir / "runs" / args.run_key / "decision_record.json"
        record = {}
        if record_path.exists():
            try:
                record = json.loads(record_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                record = {}
        valid_record = (record.get("run_id") == args.run_key
                        and str(record.get("ticker", "")).upper() == job["ticker"])
        state = ("COMPLETE" if code == EXIT_OK and valid_record else
                 "CLAUDE_LIMIT_REACHED" if code == EXIT_LIMIT else "FAILED")
        # Which of the ways of not finishing this was, stored beside the sentence. The sentence
        # alone was "exited with code 1" for an expired login and for a defect alike.
        failure, error = (None, None) if state == "COMPLETE" else failure_for_exit(
            code, has_record=valid_record)
        store.update(args.run_key, {
            "state": state, "finished_at": _now(), "exit_code": code, "error": error,
            "failure_code": failure,
            "proposal_created": bool(valid_record and record.get("proposal_id")),
            "proposal_id": record.get("proposal_id") if valid_record else None,
        }, expected_states=("RUNNING",))
        return code
    except ResearchTimeout as exc:
        store.update(args.run_key, {
            "state": "TIMED_OUT", "finished_at": _now(), "exit_code": EXIT_TIMEOUT,
            "error": str(exc), "failure_code": "timeout",
        }, expected_states=("RUNNING",))
        return EXIT_TIMEOUT
    except BaseException as exc:
        store.update(args.run_key, {
            "state": "FAILED", "finished_at": _now(), "exit_code": 1,
            "error": f"worker failed: {type(exc).__name__}: {exc}",
            "failure_code": "worker_crashed",
        }, expected_states=("RUNNING",))
        raise
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, prior_handler)


if __name__ == "__main__":
    sys.exit(main())
