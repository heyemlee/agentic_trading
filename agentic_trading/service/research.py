"""Starting research, following a run, and reading what a finished one recorded."""

from __future__ import annotations

import json
import re
import uuid
from datetime import date as calendar_date
from pathlib import Path
from typing import Any, ClassVar

from .. import agent_roster, autonomy
from ..micro_live import BUCKET as MICRO_BUCKET
from .base import _last_session
from .errors import ServiceConflict, ServiceError

BUCKETS = ("core", "sandbox", MICRO_BUCKET)


class ResearchMixin:
    """The research jobs, their traces and their history."""

    # ---------------------------------------------------------------- research
    def start_research(self, ticker: str, *, bucket: str = "core", instrument_type: str = "auto",
                       trade_date: str | None = None, trigger: str = "manual") -> dict[str, Any]:
        """Persist and enqueue a research run, deduplicating an identical active run."""
        if bucket not in BUCKETS:
            raise ServiceError(f"unknown bucket {bucket!r}")
        ticker = ticker.upper().strip()
        if (not re.fullmatch(r"[A-Z0-9.^/-]{1,24}", ticker)
                or not any(character.isalnum() for character in ticker)):
            raise ServiceError("ticker contains unsupported characters")
        if instrument_type not in ("auto", "etf", "stock", "crypto"):
            raise ServiceError(f"unknown instrument type {instrument_type!r}")
        profiles = self.profiles()
        date = trade_date or _last_session()
        try:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
                raise ValueError
            calendar_date.fromisoformat(date)
        except ValueError as exc:
            raise ServiceError("trade_date must use ISO yyyy-mm-dd format") from exc
        safe_ticker = re.sub(r"[^A-Z0-9._-]", "-", ticker)
        run_key = f"{date}_{safe_ticker}_{uuid.uuid4().hex[:8]}"
        manager = self._research_manager(profiles)
        job, created = manager.create(
            run_key=run_key, ticker=ticker, bucket=bucket, trade_date=date,
            instrument_type=instrument_type, trigger=trigger,
            # The authority the run is submitted under. A proposal born under a different
            # one is marked, and a tightened one sends it to a person (autonomous_reductions).
            authority_version=autonomy.note_version(profiles, manager.store.db),
        )
        manager.dispatch()
        current = manager.store.get(job["run_key"]) or job
        return {**current, "deduplicated": not created}

    def research_run(self, run_key: str) -> dict[str, Any]:
        profiles = self.profiles()
        if not re.fullmatch(r"[A-Za-z0-9._-]+", run_key):
            raise ServiceError(f"invalid research run {run_key!r}")
        job = self._research_manager(profiles).store.get(run_key)
        if job is None:
            raise ServiceError(f"no research run {run_key!r}")
        text = ""
        log_path = Path(job["log"]).resolve()
        runs_root = (profiles.project_dir / "runs").resolve()
        if runs_root not in log_path.parents:
            raise ServiceError("research log path is outside the project run directory")
        try:
            text = log_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            pass
        except OSError as exc:
            text = f"log unavailable: {exc}"
        # The worker names the run directory after the run key, so one lookup reaches the
        # events this run has appended. The containment check is what stops a crafted key
        # from reading a jsonl file elsewhere on disk.
        run_dir = (profiles.project_dir / "runs" / run_key).resolve()
        progress = self._research_progress(run_dir) if runs_root in run_dir.parents else None
        return {**job, "log_tail": text[-4000:], "progress": progress,
                **({"assistant_result": self._advisor_result(run_key)} if job.get("task") else {})}

    def research_jobs(self, limit: int = 50) -> list[dict[str, Any]]:
        profiles = self.profiles()
        return self._research_manager(profiles).store.list(limit=limit)

    def recover_research_jobs(self) -> dict[str, Any]:
        profiles = self.profiles()
        result = self._research_manager(profiles).tick()
        queued = len(self._research_manager(profiles).store.list(states=("QUEUED", "RUNNING")))
        self.record_activity("research_dispatch", f"{queued} job(s) active", profiles=profiles)
        return result

    def cancel_research(self, run_key: str, reason: str = "cancelled from the console") -> dict[str, Any]:
        """Stop a queued or running research job on request.

        A run that is going nowhere otherwise holds a concurrency slot and a share of the daily
        budget until its timeout, which on a long analysis is most of an afternoon.
        """
        if not re.fullmatch(r"[A-Za-z0-9._-]+", run_key):
            raise ServiceError(f"invalid research run {run_key!r}")
        manager = self._research_manager(self.profiles())
        job = manager.store.get(run_key)
        if job is None:
            raise ServiceError(f"no research run {run_key!r}")
        if job["state"] not in ("QUEUED", "RUNNING"):
            raise ServiceConflict(f"research run {run_key} already finished as {job['state']}")
        cancelled = manager.cancel(run_key, reason)
        if cancelled is None:
            raise ServiceConflict("research run changed state while it was being cancelled")
        return cancelled

    # ---------------------------------------------------------------- trace
    #: Report-tree stage and file names, mapped to something a person reads.
    _TRACE_GROUPS: ClassVar = {
        "1_analysts": "Analysts",
        "2_research": "Research debate",
        "3_trading": "Trader",
        "4_risk": "Risk debate",
        "5_portfolio": "Portfolio manager",
    }

    _TRACE_STEPS: ClassVar = {
        "market": "Market analyst", "news": "News analyst",
        "social": "Social analyst", "fundamentals": "Fundamentals analyst",
        "bull": "Bull case", "bear": "Bear case", "manager": "Research manager",
        "trader": "Trade plan",
        "aggressive": "Aggressive risk analyst", "neutral": "Neutral risk analyst",
        "conservative": "Conservative risk analyst",
        "decision": "Portfolio decision",
    }

    _TRACE_CHAR_CAP = 24_000

    def research_trace(self, run_id: str, full: bool = True) -> dict[str, Any]:
        """One run's agent trace: the decision record plus every stage report it wrote.

        The pipeline leaves a report per agent under ``runs/<run_id>/reports``. That tree is
        the closest thing the system has to a step-by-step account of its own reasoning, so a
        frontend can render the run as a sequence rather than as one opaque verdict.
        """
        if not re.fullmatch(r"[A-Za-z0-9._-]+", run_id):
            raise ServiceError(f"invalid run id {run_id!r}")
        profiles = self.profiles()
        root = (profiles.project_dir / "runs" / run_id).resolve()
        runs_root = (profiles.project_dir / "runs").resolve()
        if runs_root not in root.parents or not root.is_dir():
            raise ServiceError(f"no research run {run_id!r}")

        record: dict[str, Any] = {}
        record_path = root / "decision_record.json"
        if record_path.exists():
            try:
                record = json.loads(record_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                record = {}

        steps: list[dict[str, Any]] = []
        reports = root / "reports"
        if reports.is_dir():
            for group_dir in sorted(d for d in reports.iterdir() if d.is_dir()):
                for path in sorted(group_dir.glob("*.md")):
                    try:
                        text = path.read_text(encoding="utf-8")
                    except OSError:
                        continue
                    steps.append({
                        "order": len(steps) + 1,
                        "group": group_dir.name,
                        "group_label": self._TRACE_GROUPS.get(group_dir.name,
                                                              group_dir.name.replace("_", " ")),
                        "key": path.stem,
                        # The report stem and the console's role key differ for the portfolio
                        # manager (decision.md), so carry both rather than make every reader
                        # know that exception.
                        "role": agent_roster.BY_REPORT.get(path.stem),
                        "label": self._TRACE_STEPS.get(path.stem, path.stem.replace("_", " ")),
                        "chars": len(text),
                        "truncated": full and len(text) > self._TRACE_CHAR_CAP,
                        "markdown": text[: self._TRACE_CHAR_CAP] if full else None,
                    })

        policy = record.get("policy") or {}
        return {
            "run_id": record.get("run_id", run_id),
            "ticker": record.get("ticker"), "bucket": record.get("bucket"),
            "trade_date": record.get("trade_date"), "created_at": record.get("created_at"),
            "stage": record.get("stage"), "elapsed_seconds": record.get("elapsed_seconds"),
            "instrument_type": record.get("instrument_type"),
            "analysts": record.get("analysts") or [],
            "models": record.get("models") or {},
            "llm_usage": record.get("llm_usage") or {},
            "confidence": policy.get("confidence"),
            "policy_action": policy.get("final_action"),
            "hard_risk_verdict": (record.get("hard_risk") or {}).get("verdict"),
            "final_action": (record.get("final_recommendation") or {}).get("action"),
            "proposal_id": record.get("proposal_id"),
            "error": record.get("error"),
            "steps": steps,
        }

    #: Progress lines returned with a run. A long analysis writes a few dozen; the cap is
    #: there so a pathological run cannot make the polling response unbounded.
    _PROGRESS_EVENT_CAP: ClassVar = 200

    def _research_progress(self, run_dir: Path) -> dict[str, Any] | None:
        """What a run in flight has done so far, from the events its model calls append.

        The report tree is written in one go when the whole graph finishes, so it says
        nothing while a run is working. ``progress.jsonl`` is appended per model call and is
        the only signal that can tell one desk from another before the run ends.

        Only two desk states are reported here, and both are facts the file states outright:
        a call is open (``working``), or the last one returned (``done``). Whether a desk is
        idle, skipped for this instrument, or stalled is a judgement about the clock and the
        roster, and belongs to the caller that has both.
        """
        try:
            lines = (run_dir / "progress.jsonl").read_text(encoding="utf-8").splitlines()
        except (FileNotFoundError, NotADirectoryError):
            return None
        except OSError:
            return None

        events: list[dict[str, Any]] = []
        for line in lines:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue  # the worker may be mid-write on the final line
            if isinstance(event, dict) and event.get("kind"):
                events.append(event)

        desks: dict[str, dict[str, Any]] = {}
        started: dict[str, Any] = {}
        finished: dict[str, Any] | None = None
        for event in events:
            kind = event.get("kind")
            if kind == "run_started":
                started = event
                continue
            if kind == "run_finished":
                finished = event
                continue
            role = event.get("role")
            if not role or kind not in ("call_started", "call_finished"):
                continue
            desk = desks.setdefault(role, {"calls": 0, "wall_seconds": 0.0, "tools": []})
            desk["last_at"] = event.get("t")
            if kind == "call_started":
                desk["state"] = "working"
                desk["calls"] += 1
            else:
                desk["state"] = "done"
                desk["wall_seconds"] = round(
                    desk["wall_seconds"] + float(event.get("wall_seconds") or 0.0), 1)
                desk["tools"] = sorted({*desk["tools"], *(event.get("tools") or [])})
        return {
            "ticker": started.get("ticker"),
            "bucket": started.get("bucket"),
            "instrument_type": started.get("instrument_type"),
            "analysts": started.get("analysts") or [],
            "started_at": started.get("t"),
            "desks": desks,
            "result": finished,
            "last_event_at": events[-1].get("t") if events else None,
            "events": events[-self._PROGRESS_EVENT_CAP:],
        }

    def research_roster(self) -> dict[str, Any]:
        """The research team, and which analysts each instrument type actually starts.

        The console draws one desk per role. Without this it would keep a second copy of the
        team and of the analyst selection in ``config/``, and disagree with the engine the
        first time either changed — showing a desk that no longer runs, or missing one that
        does.
        """
        profiles = self.profiles()
        return {
            "groups": [{"key": key, "directory": directory}
                       for key, directory in agent_roster.GROUPS],
            "roles": [dict(role) for role in agent_roster.ROLES],
            "analysts": {kind: list(profiles.analysts_for(kind))
                         for kind in ("etf", "stock", "crypto")},
            # How long one model call may run before the adapter gives up. A desk whose call
            # has been open longer than this is genuinely stuck, so the console can say so
            # from configuration rather than from a guessed timeout of its own.
            "call_timeout_seconds": int(profiles.llm.timeout_seconds),
        }

    def research_history(self, limit: int = 20) -> list[dict[str, Any]]:
        """Completed decision records, newest first."""
        profiles = self.profiles()
        runs = sorted((profiles.project_dir / "runs").glob("*/decision_record.json"),
                      key=lambda p: p.stat().st_mtime, reverse=True)
        out = []
        for path in runs:
            try:
                d = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if d.get("task_kind") in ("refresh", "chat", "shortlist"):
                continue
            policy = d.get("policy") or {}
            out.append({
                "run_id": d.get("run_id"), "ticker": d.get("ticker"), "bucket": d.get("bucket"),
                "trade_date": d.get("trade_date"), "created_at": d.get("created_at"),
                # Which desks this run seated. The console shows the team per run rather than
                # inferring an instrument type back out of the analyst list.
                "instrument_type": d.get("instrument_type"),
                "portfolio_manager_rating": (d.get("upstream_decision") or {}).get("portfolio_manager_signal"),
                "policy_action": policy.get("final_action"), "confidence": policy.get("confidence"),
                # Evidence quality travels with the decision: a run that concluded without the
                # macro context is not the same kind of result as one that had it.
                "data_quality": (policy.get("proposal") or {}).get("data_quality"),
                "missing_information": len((policy.get("proposal") or {}).get("missing_information") or []),
                "hard_risk_verdict": (d.get("hard_risk") or {}).get("verdict"),
                "final_action": (d.get("final_recommendation") or {}).get("action"),
                "proposal_id": d.get("proposal_id"),
                "llm_calls": (d.get("llm_usage") or {}).get("calls"),
                "elapsed_seconds": d.get("elapsed_seconds"),
            })
            if len(out) >= limit:
                break
        return out
