"""The personal assistant: its decisions, its conversation and the notes it is given."""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

from ..proposals import utcnow
from .base import _last_session
from .errors import ServiceConflict, ServiceError
from .shapes import (
    AdvisorConversation,
    AdvisorDecision,
    AdvisorJob,
    AdvisorMessage,
    AdvisorSnapshot,
    AdvisorState,
    PersonalNotes,
)


class AssistantMixin:
    """What the assistant has decided, what it was asked, and what it was told to remember."""

    # ---------------------------------------------------------------- the assistant
    def advisor_decisions(self, limit: int = 40) -> list[AdvisorDecision]:
        """The same decision records as the research page, including the actual policy details."""
        profiles = self.profiles()
        records = sorted((profiles.project_dir / "runs").glob("*/decision_record.json"),
                         key=lambda p: p.stat().st_mtime, reverse=True)
        job_states = {j["run_key"]: j["state"]
                      for j in self._research_manager(profiles).store.list(limit=10000)}
        out: list[AdvisorDecision] = []
        for path in records:
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not record.get("policy"):
                continue
            if record.get("advisory") and job_states.get(record.get("run_id")) != "COMPLETE":
                continue
            out.append(AdvisorDecision(
                run_id=record.get("run_id"), ticker=record.get("ticker"),
                bucket=record.get("bucket"), account_id=record.get("account_id"),
                created_at=record.get("created_at"), trade_date=record.get("trade_date"),
                action=(record.get("final_recommendation") or {}).get("action"),
                policy=record.get("policy"), advisory=bool(record.get("advisory")),
                proposal_id=record.get("proposal_id")))
            if len(out) >= limit:
                break
        return out

    def _advisor_result(self, run_key: str) -> dict[str, Any] | None:
        if not re.fullmatch(r"[A-Za-z0-9._-]+", run_key):
            raise ServiceError("invalid run identifier")
        root = (self.profiles().project_dir / "runs").resolve()
        path = (root / run_key / "decision_record.json").resolve()
        if root not in path.parents:
            raise ServiceError("result is outside the run directory")
        try:
            return json.loads(path.read_text(encoding="utf-8")).get("assistant_result")
        except (OSError, ValueError):
            return None

    def advisor_conversation(self, conversation_id: str = "default") -> AdvisorConversation:
        jobs = self._research_manager(self.profiles()).store.list(limit=1000)
        messages: list[AdvisorMessage] = []
        for job in sorted(jobs, key=lambda j: j["created_at"]):
            task = job.get("task") or {}
            if task.get("conversation_id") != conversation_id or not task.get("message"):
                continue
            messages.append(AdvisorMessage(role="user", content=task["message"],
                                           run_key=job["run_key"], state=job["state"]))
            result = self._advisor_result(job["run_key"]) if job["state"] == "COMPLETE" else None
            if result:
                messages.append(AdvisorMessage(
                    role="assistant", content=result["answer"],
                    research_candidates=result.get("research_candidates", []),
                    run_key=job["run_key"], state=job["state"]))
        return AdvisorConversation(conversation_id=conversation_id, messages=messages[-80:])

    def advisor_state(self) -> AdvisorState:
        from ..advisor import NOTES, SNAPSHOT, age

        db = self.state_db()
        snapshot = db.read_meta(SNAPSHOT, {}) or {}
        jobs = self._research_manager(self.profiles()).store.list(limit=500)
        helper_jobs = [j for j in jobs if j.get("task")]
        conversations = list(dict.fromkeys(j["task"]["conversation_id"] for j in helper_jobs
                                          if j["task"].get("conversation_id")))
        return AdvisorState(
            snapshot=AdvisorSnapshot(**snapshot),
            stale=age(snapshot) > 300,
            notes=PersonalNotes(**(db.read_meta(NOTES, {"text": "", "revision": 0}) or {})),
            jobs=[AdvisorJob(**{k: j.get(k)
                                for k in ("run_key", "ticker", "state", "error", "created_at", "task")})
                  for j in helper_jobs[:30]],
            conversations=conversations[:50],
            decisions=self.advisor_decisions(),
            coverage="All Robinhood brokerage accounts, stocks/ETFs only; analysis is read-only.")

    def advisor_notes(self, text: str, revision: int) -> PersonalNotes:
        from ..advisor import NOTES

        if len(text) > 4000:
            raise ServiceError("personal notes must be at most 4000 characters")
        db = self.state_db()
        with db.transaction() as conn:
            old = db.read_meta(NOTES, {}) or {}
            if old.get("revision", 0) != revision:
                raise ServiceConflict("Personal notes changed on another device; reload before saving.")
            value = PersonalNotes(text=text.strip(), revision=revision + 1,
                                  updated_at=utcnow().isoformat())
            db.write_meta(NOTES, value.model_dump(), conn)
        return value

    def start_advisor(self, *, kind: str, request_id: str, message: str = "",
                      conversation_id: str = "default", ticker: str = "",
                      account_id: str | None = None, language: str = "zh") -> dict[str, Any]:
        from ..advisor import SYMBOL
        from ..schedule import analyses_today, settings

        if kind not in ("refresh", "chat", "shortlist", "analysis"):
            raise ServiceError("unknown assistant task")
        try:
            request_id = str(uuid.UUID(request_id))
        except ValueError as exc:
            raise ServiceError("request_id must be a UUID") from exc
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", conversation_id):
            raise ServiceError("invalid conversation identifier")
        if language not in ("zh", "en") or len(message) > 4000:
            raise ServiceError("invalid language or message length")
        if kind in ("chat", "shortlist") and not message.strip():
            raise ServiceError("a message is required")
        ticker = ticker.strip().upper() if kind == "analysis" else "ASSISTANT"
        if not SYMBOL.fullmatch(ticker):
            raise ServiceError("enter a stock or ETF symbol")
        if account_id is not None and not re.fullmatch(r"[0-9a-f]{20}", account_id):
            raise ServiceError("invalid observed account identifier")
        profiles = self.profiles()
        manager = self._research_manager(profiles)
        task = {"kind": kind, "language": language, "account_id": account_id}
        if kind in ("chat", "shortlist"):
            task.update(conversation_id=conversation_id, message=message.strip())
        run_key = "advisor_" + request_id.replace("-", "")
        with manager.store.db.transaction():
            existing = manager.store.get(run_key)
            if existing:
                if existing.get("task") != task or existing["ticker"] != ticker:
                    raise ServiceConflict("request_id was already used for a different request")
                return {**existing, "deduplicated": True}
            jobs = manager.store.list(limit=10000)
            if any(j["state"] in ("QUEUED", "RUNNING") and j.get("task", {}).get("conversation_id") == conversation_id
                   for j in jobs) and kind in ("chat", "shortlist"):
                raise ServiceConflict("The assistant is still answering this conversation.")
            cfg = settings(profiles)
            if kind == "analysis":
                if analyses_today(jobs, cfg) >= int(cfg["max_analyses_per_day"]):
                    raise ServiceError("Today's full-research budget is used; try again next trading day.")
            elif kind != "refresh":
                assistant_jobs = [j for j in jobs if j.get("task", {}).get("kind") in ("chat", "shortlist")]
                # These short calls have their own cap while sharing the existing concurrency pool.
                stamp = utcnow().date().isoformat()
                if sum(j["created_at"].startswith(stamp) for j in assistant_jobs) >= 30:
                    raise ServiceError("Today's 30 assistant replies are used.")
            job, created = manager.create(run_key=run_key, ticker=ticker, bucket="advisory",
                                          trade_date=_last_session(), instrument_type="auto",
                                          trigger="manual", task=task)
        manager.dispatch()
        return {**(manager.store.get(job["run_key"]) or job), "deduplicated": not created}
