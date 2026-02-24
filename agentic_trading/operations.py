"""Heartbeats and housekeeping for a machine nobody is watching.

The failures that matter in unattended operation are quiet: a loop that stopped, a backup job
that was never loaded, a disk filling with run artefacts. None of them raise anything. Each
background activity therefore records when it last succeeded, and the console compares that
against how often the activity is expected, so "there was nothing to do" can be told apart
from "it has not run since the machine rebooted".
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .env import load_env

ACTIVITIES = ("reconciliation", "research_dispatch", "backup", "maintenance", "schedule",
              "ledger_confirm")
HEARTBEATS = "activity_heartbeats"


def record(db, activity: str, detail: str = "", *, min_interval_seconds: float = 60.0) -> None:
    """Note that an activity just succeeded, at most once per ``min_interval_seconds``.

    Throttled because the reconciliation loop runs every fifteen seconds and a durable write
    per pass is far more disk traffic than the fact is worth.
    """
    try:
        # Every activity lives in one document that each writer rewrites whole, and the writers
        # are separate processes: the API, the weekly ct-trigger, the backup and maintenance
        # jobs. With the read outside the transaction the API's next heartbeat overwrote
        # whatever another process had just recorded, which is why `schedule` was absent from a
        # document that had been revised 17,866 times. BEGIN IMMEDIATE serialises the pair, and
        # the context manager is re-entrant for callers that already hold a transaction.
        with db.transaction():
            stored = db.read_meta(HEARTBEATS, {}) or {}
            previous = (stored.get(activity) or {}).get("at")
            now = datetime.now(UTC)
            if previous:
                try:
                    age = (now - datetime.fromisoformat(previous)).total_seconds()
                    if 0 <= age < min_interval_seconds:
                        return
                except ValueError:
                    pass
            stored[activity] = {"at": now.isoformat(), "detail": detail}
            db.write_meta(HEARTBEATS, stored)
    except Exception:      # a heartbeat never breaks the work it describes
        logging.getLogger(__name__).debug("could not record the %s heartbeat", activity,
                                          exc_info=True)


def heartbeats(db) -> dict[str, Any]:
    return db.read_meta(HEARTBEATS, {}) or {}


def rotate_logs(directory: Path, *, max_bytes: int, keep: int) -> list[str]:
    """Roll oversized service logs. launchd appends forever; nothing else truncates them."""
    rotated = []
    directory = Path(directory)
    if not directory.is_dir():
        return rotated
    for log in sorted(directory.glob("*.log")):
        if not log.is_file() or log.stat().st_size <= max_bytes:
            continue
        for index in range(keep - 1, 0, -1):
            older = log.with_suffix(f".log.{index}")
            if older.exists():
                older.replace(log.with_suffix(f".log.{index + 1}"))
        log.replace(log.with_suffix(".log.1"))
        # launchd holds the original file descriptor, so recreate the path it writes to and
        # let the next line land in an empty file rather than in a rotated one.
        log.touch(mode=0o600)
        rotated.append(log.name)
    for stale in sorted(directory.glob("*.log.*")):
        try:
            if int(stale.suffix.lstrip(".")) > keep:
                stale.unlink()
        except ValueError:
            continue
    return rotated


def prune_runs(runs_dir: Path, *, retention_days: int) -> list[str]:
    """Remove old run artefacts, keeping the decision record that proves what was decided.

    Reports are the bulk of a run and are reproducible context; the decision record is the
    audit trail, so a pruned run keeps its record and loses its reports.
    """
    runs_dir = Path(runs_dir)
    if not runs_dir.is_dir() or retention_days <= 0:
        return []
    cutoff = datetime.now(UTC).timestamp() - retention_days * 86400
    pruned = []
    for run in sorted(runs_dir.iterdir()):
        if not run.is_dir() or run.name in ("service", "api"):
            continue
        reports = run / "reports"
        if not reports.is_dir() or run.stat().st_mtime >= cutoff:
            continue
        shutil.rmtree(reports, ignore_errors=True)
        pruned.append(run.name)
    return pruned


def disk(path: Path, floor_gb: float) -> dict[str, Any]:
    usage = shutil.disk_usage(Path(path))
    free_gb = usage.free / 1024**3
    return {"free_gb": round(free_gb, 1),
            "free_pct": round(usage.free / usage.total * 100, 1),
            "low": free_gb < floor_gb, "floor_gb": floor_gb}


def main(argv: list[str] | None = None) -> int:
    """``ct-ops``: the housekeeping the daily maintenance job runs."""
    load_env()
    from .profile import load_profiles
    from .state_db import StateDB

    parser = argparse.ArgumentParser(
        prog="ct-ops", description="Rotate logs, prune run artefacts and record a heartbeat.")
    parser.add_argument("action", choices=("maintain", "status", "heartbeat"))
    parser.add_argument("--config-dir", default="config")
    parser.add_argument("--activity", choices=ACTIVITIES, default="maintenance")
    parser.add_argument("--detail", default="")
    args = parser.parse_args(argv)

    profiles = load_profiles(args.config_dir)
    cfg = profiles.risk.get("operations") or {}
    state_file = str((profiles.risk.get("broker") or {}).get("state_file", "state/proposals.json"))
    db = StateDB(profiles.project_dir / state_file)

    if args.action == "heartbeat":
        record(db, args.activity, args.detail, min_interval_seconds=0)
        print(json.dumps({"activity": args.activity, "recorded": True}))
        return 0

    free = disk(profiles.project_dir, float(cfg.get("low_disk_free_gb", 10)))
    if args.action == "status":
        # A stale heartbeat is the one symptom every quiet failure shares, so it has to reach
        # the exit code. While this returned zero regardless, selfcheck.sh printed the ages and
        # then reported "all checks passed" right through an outage in which the API could not
        # open its own database: the numbers were on screen and nothing was reading them.
        late: list[str] = []
        activities: list[dict[str, Any]] | str
        try:
            from .service import Service

            activities = Service(args.config_dir).operations()["activities"]
            late = [row["id"] for row in activities if row["overdue"]]
        except Exception as exc:  # noqa: BLE001 - unreadable state is itself a failed check
            activities = f"{type(exc).__name__}: {exc}"
            late = ["activities could not be read"]
        print(json.dumps({"disk": free, "activities": activities, "late": late}, indent=2))
        return 1 if (late or free["low"]) else 0

    rotated = rotate_logs(profiles.project_dir / "runs" / "service",
                          max_bytes=int(cfg.get("log_max_mb", 32)) * 1024 * 1024,
                          keep=int(cfg.get("log_keep", 5)))
    pruned = prune_runs(profiles.project_dir / "runs",
                        retention_days=int(cfg.get("runs_retention_days", 120)))
    # The forward record is only worth anything if it is unbroken, so the daily job marks it
    # rather than relying on somebody remembering to.
    marked: dict[str, Any] = {"recorded": False, "reason": "no study is running"}
    try:
        from .service import Service

        marked = Service(args.config_dir).evaluation_snapshot()
    except Exception as exc:  # noqa: BLE001 - housekeeping continues even if the study cannot
        marked = {"recorded": False, "reason": f"{type(exc).__name__}: {exc}"}

    # The ledgers expire on their own: max_age_days fails a book closed once nothing has
    # confirmed it, and in a stretch of NO_TRADE weeks nothing does, so the research that
    # reads it stops before the first model call and the Portfolio screen goes blank. This is
    # the same check the console button and `ct-state confirm` run, refusing the same way; it
    # keeps its own report and heartbeat, so all that is left here is to survive it failing.
    from .ledger_confirm import REPORT, confirm_ledgers
    from .proposals import ProposalStore

    try:
        ledgers = confirm_ledgers(profiles, ProposalStore(profiles.project_dir / state_file))
    except Exception as exc:  # noqa: BLE001 - housekeeping continues; the reason is kept
        ledgers = {"confirmed": False, "ledgers": [], "error": f"{type(exc).__name__}: {exc}",
                   "at": datetime.now(UTC).isoformat()}
        try:
            db.write_meta(REPORT, ledgers)
        except Exception:
            logging.getLogger(__name__).debug("could not keep the ledger report", exc_info=True)

    detail = f"{len(rotated)} log(s) rotated, {len(pruned)} run(s) pruned, {free['free_gb']} GB free"
    record(db, "maintenance", detail, min_interval_seconds=0)
    print(json.dumps({"rotated": rotated, "pruned": pruned, "disk": free,
                      "evaluation": marked, "ledgers": ledgers}, indent=2))
    # A full disk stops the database, the logs and the backups at once, so it is worth an
    # exit code the scheduler's own log will show.
    return 1 if free["low"] else 0


if __name__ == "__main__":
    sys.exit(main())
