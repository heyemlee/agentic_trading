"""Persistent, deduplicated research queue with restart and timeout recovery."""
from __future__ import annotations

import hashlib
import json
import os
import signal
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .state_db import StateDB

ACTIVE_STATES = ("QUEUED", "RUNNING")
TERMINAL_STATES = ("COMPLETE", "FAILED", "TIMED_OUT", "CLAUDE_LIMIT_REACHED", "CANCELLED")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    stamp = datetime.fromisoformat(value)
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)


class ResearchJobStore:
    def __init__(self, state_source: str | Path):
        self.db = StateDB(state_source)

    def create(self, *, run_key: str, ticker: str, bucket: str, trade_date: str,
               instrument_type: str, trigger: str, log_path: Path,
               timeout_seconds: int, max_attempts: int,
               task: dict[str, Any] | None = None) -> tuple[dict[str, Any], bool]:
        dedupe = f"{trade_date}:{ticker.upper()}:{bucket}:{instrument_type}"
        if task:
            # A conversation is serial; independent conversations and account reviews are not.
            scope = task.get("conversation_id") or task.get("account_id") or task["kind"]
            dedupe += ":" + hashlib.sha256(str(scope).encode()).hexdigest()[:24]
        created = _now()
        job = {
            "run_key": run_key, "dedupe_key": dedupe, "ticker": ticker.upper(),
            "bucket": bucket, "trade_date": trade_date, "instrument_type": instrument_type,
            "trigger": trigger, "state": "QUEUED", "created_at": created,
            "updated_at": created, "started_at": None, "finished_at": None,
            "deadline_at": None, "pid": None, "attempts": 0,
            "max_attempts": max_attempts, "timeout_seconds": timeout_seconds,
            "log": str(log_path), "exit_code": None, "error": None,
            "limit_recoveries": 0, "proposal_created": False, "proposal_id": None,
        }
        if task:
            job["task"] = task
        with self.db.transaction() as conn:
            try:
                conn.execute("INSERT INTO research_jobs VALUES(?,?,?,?,?,?)", (
                    run_key, dedupe, json.dumps(job, allow_nan=False), "QUEUED", created, created))
                return job, True
            except sqlite3.IntegrityError:
                row = conn.execute(
                    "SELECT payload FROM research_jobs WHERE dedupe_key=? AND state IN ('QUEUED','RUNNING')",
                    (dedupe,),
                ).fetchone()
                if row:
                    return json.loads(row[0]), False
                raise

    def get(self, run_key: str) -> dict[str, Any] | None:
        return self.db.get_research_job(run_key)

    def list(self, *, states=(), limit=100) -> list[dict[str, Any]]:
        return self.db.research_jobs(states=states, limit=limit)

    def update(self, run_key: str, changes: dict[str, Any], *, expected_states=()) -> dict[str, Any] | None:
        from . import events

        with self.db.transaction() as conn:
            row = conn.execute("SELECT payload,state FROM research_jobs WHERE run_key=?", (run_key,)).fetchone()
            if row is None or (expected_states and row[1] not in expected_states):
                return None
            job = json.loads(row[0])
            was = job["state"]
            job.update(changes)
            job["updated_at"] = _now()
            conn.execute("UPDATE research_jobs SET payload=?,state=?,updated_at=? WHERE run_key=?", (
                json.dumps(job, allow_nan=False), job["state"], job["updated_at"], run_key))
            # A job row holds only its current state, so a run that finished and was later
            # requeued would otherwise leave no trace of having finished at all.
            if job["state"] != was and job["state"] in TERMINAL_STATES:
                events.record(
                    conn, kind="research_completed" if job["state"] == "COMPLETE"
                    else "research_failed",
                    subject=run_key, outcome=job["state"],
                    detail={"ticker": job.get("ticker"), "bucket": job.get("bucket"),
                            "trade_date": job.get("trade_date"), "trigger": job.get("trigger"),
                            "proposal_id": job.get("proposal_id"),
                            "error": str(job.get("error") or "")[:300]})
            return job

    def claim(self, max_concurrent: int) -> list[dict[str, Any]]:
        claimed: list[dict[str, Any]] = []
        with self.db.transaction() as conn:
            running = conn.execute("SELECT count(*) FROM research_jobs WHERE state='RUNNING'").fetchone()[0]
            slots = max(0, max_concurrent - running)
            rows = conn.execute(
                "SELECT run_key,payload FROM research_jobs WHERE state='QUEUED' ORDER BY created_at LIMIT ?",
                (slots,),
            ).fetchall()
            for row in rows:
                job = json.loads(row[1])
                started = _now()
                job.update({
                    "state": "RUNNING", "started_at": started, "updated_at": started,
                    "deadline_at": (datetime.now(UTC) + timedelta(
                        seconds=int(job["timeout_seconds"]))).isoformat(),
                    "attempts": int(job.get("attempts", 0)) + 1, "pid": None,
                    "finished_at": None, "exit_code": None, "error": None,
                })
                changed = conn.execute(
                    "UPDATE research_jobs SET payload=?,state='RUNNING',updated_at=? "
                    "WHERE run_key=? AND state='QUEUED'",
                    (json.dumps(job, allow_nan=False), started, row[0]),
                )
                if changed.rowcount:
                    claimed.append(job)
        return claimed


class ResearchJobManager:
    def __init__(self, *, state_source: str | Path, project_dir: str | Path,
                 config_dir: str | Path, settings: dict[str, Any] | None = None):
        self.store = ResearchJobStore(state_source)
        self.project_dir = Path(project_dir).resolve()
        self.config_dir = Path(config_dir).resolve()
        settings = settings or {}
        self.max_concurrent = max(1, int(settings.get("max_concurrent", 2)))
        self.timeout_seconds = max(60, int(settings.get("timeout_seconds", 5400)))
        self.max_attempts = max(1, int(settings.get("max_attempts", 2)))
        self.limit_retry_after = max(0.0, float(settings.get("claude_limit_retry_after_hours", 6)))
        self.max_limit_recoveries = max(0, int(settings.get("max_limit_recoveries", 2)))

    def create(self, *, run_key: str, ticker: str, bucket: str, trade_date: str,
               instrument_type: str, trigger: str,
               task: dict[str, Any] | None = None) -> tuple[dict[str, Any], bool]:
        log_dir = self.project_dir / "runs" / "api"
        log_dir.mkdir(parents=True, exist_ok=True)
        return self.store.create(
            run_key=run_key, ticker=ticker, bucket=bucket, trade_date=trade_date,
            instrument_type=instrument_type, trigger=trigger, log_path=log_dir / f"{run_key}.log",
            timeout_seconds=self.timeout_seconds, max_attempts=self.max_attempts,
            task=task,
        )

    def dispatch(self) -> list[str]:
        launched: list[str] = []
        for job in self.store.claim(self.max_concurrent):
            command = [
                sys.executable, "-m", "agentic_trading.research_worker",
                "--config-dir", str(self.config_dir), "--run-key", job["run_key"],
            ]
            try:
                log_path = Path(job["log"])
                log_path.parent.mkdir(parents=True, exist_ok=True)
                with log_path.open("a", encoding="utf-8") as output:
                    log_path.chmod(0o600)
                    output.write(f"# attempt={job['attempts']} trigger={job['trigger']} started={_now()}\n")
                    output.flush()
                    process = subprocess.Popen(
                        command, stdout=output, stderr=subprocess.STDOUT, cwd=self.project_dir,
                        env=_scrubbed_environment(), start_new_session=True,
                    )
                self.store.update(job["run_key"], {"pid": process.pid}, expected_states=("RUNNING",))
                launched.append(job["run_key"])
            except Exception as exc:  # noqa: BLE001 - launch failures must become durable state
                self.store.update(job["run_key"], {
                    "state": "FAILED", "finished_at": _now(),
                    "error": f"worker launch failed: {type(exc).__name__}: {exc}",
                }, expected_states=("RUNNING",))
        return launched

    def cancel(self, run_key: str, reason: str) -> dict[str, Any] | None:
        """End a queued or running job now, stopping its worker if one is alive."""
        job = self.store.get(run_key)
        if job is None or job["state"] not in ACTIVE_STATES:
            return None
        # Record the terminal state first. A worker killed while still RUNNING would otherwise
        # race to write FAILED over the cancellation and report it as a defect.
        cancelled = self.store.update(run_key, {
            "state": "CANCELLED", "finished_at": _now(), "pid": None, "deadline_at": None,
            "exit_code": None, "error": reason,
        }, expected_states=ACTIVE_STATES)
        if cancelled is not None:
            _terminate_worker(job.get("pid"), run_key)
        return cancelled

    def resume_after_limit(self, now: datetime) -> list[str]:
        """Requeue jobs that stopped only because the subscription limit was reached.

        The limit is a clock, not a verdict on the work, so the job waits out the reset window
        and goes back in the queue. Bounded by max_limit_recoveries: an unbounded retry would
        spend a whole day's allowance re-attempting the same ticker the moment it refilled.
        """
        if not self.limit_retry_after:
            return []
        resumed: list[str] = []
        for job in self.store.list(states=("CLAUDE_LIMIT_REACHED",), limit=200):
            finished = _parse(job.get("finished_at")) or _parse(job.get("updated_at"))
            recoveries = int(job.get("limit_recoveries", 0))
            if finished is None or recoveries >= self.max_limit_recoveries:
                continue
            if now - finished < timedelta(hours=self.limit_retry_after):
                continue
            updated = self.store.update(job["run_key"], {
                "state": "QUEUED", "pid": None, "deadline_at": None, "finished_at": None,
                "attempts": 0, "limit_recoveries": recoveries + 1,
                "error": f"subscription limit reached; requeued after "
                         f"{self.limit_retry_after:g}h (recovery {recoveries + 1}"
                         f"/{self.max_limit_recoveries})",
            }, expected_states=("CLAUDE_LIMIT_REACHED",))
            if updated:
                resumed.append(job["run_key"])
        return resumed

    def tick(self) -> dict[str, Any]:
        """Recover orphaned workers, enforce deadlines, then fill free queue slots."""
        recovered: list[str] = []
        timed_out: list[str] = []
        now = datetime.now(UTC)
        for job in self.store.list(states=("RUNNING",), limit=1000):
            run_key = job["run_key"]
            record = self.project_dir / "runs" / run_key / "decision_record.json"
            if record.exists():
                try:
                    data = json.loads(record.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    data = None
                if (data is not None and data.get("run_id") == run_key
                        and str(data.get("ticker", "")).upper() == job["ticker"]):
                    self.store.update(run_key, {
                        "state": "COMPLETE", "finished_at": _now(), "exit_code": 0,
                        "proposal_created": bool(data.get("proposal_id")),
                        "proposal_id": data.get("proposal_id"), "error": None,
                    }, expected_states=("RUNNING",))
                    recovered.append(run_key)
                    continue
            deadline = _parse(job.get("deadline_at"))
            pid = job.get("pid")
            if deadline and now > deadline:
                _terminate_worker(pid, run_key)
                self.store.update(run_key, {
                    "state": "TIMED_OUT", "finished_at": _now(), "exit_code": 124,
                    "error": f"research exceeded {job['timeout_seconds']} seconds",
                }, expected_states=("RUNNING",))
                timed_out.append(run_key)
                continue
            # Give a just-claimed worker time to persist its PID. If the manager died during
            # launch, a later tick safely requeues the same run id and checkpoint.
            started = _parse(job.get("started_at"))
            launch_grace = started and (now - started).total_seconds() < 30
            if (not pid and launch_grace) or (pid and _worker_alive(pid, run_key)):
                continue
            if int(job.get("attempts", 0)) < int(job.get("max_attempts", self.max_attempts)):
                self.store.update(run_key, {
                    "state": "QUEUED", "pid": None, "deadline_at": None,
                    "error": "worker exited before recording a terminal result; queued for recovery",
                }, expected_states=("RUNNING",))
                recovered.append(run_key)
            else:
                self.store.update(run_key, {
                    "state": "FAILED", "finished_at": _now(),
                    "error": "worker exited without a result and recovery attempts are exhausted",
                }, expected_states=("RUNNING",))
        resumed = self.resume_after_limit(now)
        launched = self.dispatch()
        return {"recovered": recovered, "timed_out": timed_out,
                "resumed_after_limit": resumed, "launched": launched}


def _scrubbed_environment() -> dict[str, str]:
    from .llm.claude_code import scrubbed_environment

    return scrubbed_environment()


def _worker_command(pid: int) -> str:
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="], capture_output=True,
            text=True, check=False, timeout=2,
        )
        return result.stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _worker_alive(pid: int | None, run_key: str) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except (OSError, ValueError, TypeError):
        return False
    command = _worker_command(int(pid))
    return "agentic_trading.research_worker" in command and run_key in command


def _terminate_worker(pid: int | None, run_key: str) -> bool:
    if not _worker_alive(pid, run_key):
        return False
    try:
        os.killpg(int(pid), signal.SIGTERM)
        return True
    except OSError:
        return False
