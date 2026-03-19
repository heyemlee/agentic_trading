"""The personal assistant: its decisions, its conversation and the notes it is given."""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

from ..proposals import utcnow
from .base import _last_session
from .errors import ServiceConflict, ServiceError
from .research import _describe_record, _in_scope, _supersede_key
from .shapes import (
    AdvisorConversation,
    AdvisorDecision,
    AdvisorJob,
    AdvisorMessage,
    AdvisorSnapshot,
    AdvisorState,
    PersonalNotes,
)

#: The state document that binds a conversation to the account scope it was opened under.
CONVERSATIONS = "advisor.conversations"
SCOPES = ("personal", "agentic")
#: What the assistant may be asked for the Agentic account: an explanation of what is
#: recorded. Discovery, refreshes and holding analyses are personal.
AGENTIC_KINDS = ("chat",)
KINDS = ("refresh", "chat", "shortlist", "analysis")
#: Personal tasks the console asks for by name and is told about when they finish.
CONVERSATIONAL = ("chat", "shortlist")


class AssistantMixin:
    """What the assistant has decided, what it was asked, and what it was told to remember."""

    # ---------------------------------------------------------------- the assistant
    def advisor_decisions(self, limit: int = 40, *, scope: str | None = None,
                          account_id: str | None = None,
                          include_unknown: bool = True) -> list[AdvisorDecision]:
        """The same decision records as the research page, including the actual policy details.

        Bound the way the research list binds them (service/research.py): a personal account
        sees its own records and the all-accounts reviews that covered it; ``include_unknown``
        is for a screen listing history, never for what goes into a model's context.
        """
        profiles = self.profiles()
        labels = self._personal_labels(profiles)
        records = sorted((profiles.project_dir / "runs").glob("*/decision_record.json"),
                         key=lambda p: p.stat().st_mtime, reverse=True)
        job_states = {j["run_key"]: j["state"]
                      for j in self._research_manager(profiles).store.list(limit=10000)}
        out: list[AdvisorDecision] = []
        latest: dict[tuple, str] = {}
        for path in records:
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not record.get("policy"):
                continue
            if record.get("advisory") and job_states.get(record.get("run_id")) != "COMPLETE":
                continue
            row = _describe_record(record, profiles, labels)
            key = _supersede_key(row)
            superseded_by = None
            if key is not None:
                superseded_by = latest.get(key)
                latest.setdefault(key, row["run_id"])
            if not _in_scope(row, scope, account_id, include_all_personal=True):
                continue
            if not include_unknown and row["account_scope_kind"] == "unknown":
                continue
            out.append(AdvisorDecision(
                run_id=record.get("run_id"), ticker=record.get("ticker"),
                bucket=record.get("bucket"), account_id=record.get("account_id"),
                created_at=record.get("created_at"), trade_date=record.get("trade_date"),
                action=(record.get("final_recommendation") or {}).get("action"),
                policy=record.get("policy"), advisory=bool(record.get("advisory")),
                proposal_id=record.get("proposal_id"),
                scope=row["scope"], account_scope_kind=row["account_scope_kind"],
                account_label=row["account_label"], task_type=row["task_type"],
                superseded_by=superseded_by))
            if len(out) >= limit:
                break
        return out

    # ---------------------------------------------------------------- account scope
    def _snapshot_accounts(self, db) -> list[dict[str, Any]]:
        from ..advisor import SNAPSHOT

        snapshot = db.read_meta(SNAPSHOT, {}) or {}
        return [a for a in snapshot.get("accounts") or [] if isinstance(a, dict) and a.get("id")]

    def _bind_conversation(self, db, conversation_id: str, scope: str, kind: str,
                           account_id: str | None, account_ids: list[str]) -> dict[str, Any]:
        """Hold a conversation to the account scope it was opened under, or refuse the request.

        The first message binds. A later message under another scope, or another account, is
        refused rather than quietly re-binding the history to it: that is how one account's
        past never becomes another's context. A conversation from before bindings existed is
        personal by construction (nothing else existed) and takes the kind of its next
        personal message.
        """
        with db.transaction() as conn:
            bindings = db.read_meta(CONVERSATIONS, {}) or {}
            bound = bindings.get(conversation_id)
            if bound is None and any(
                    (j.get("task") or {}).get("conversation_id") == conversation_id
                    for j in self._research_manager(self.profiles()).store.list(limit=10000)):
                bound = {"scope": "personal", "account_scope_kind": "unknown", "account_id": None,
                         "account_ids": [], "created_at": None}
            if bound is not None:
                if bound.get("scope") != scope:
                    raise ServiceConflict("conversation is bound to another account scope")
                if (bound.get("account_scope_kind") != "unknown"
                        and (bound.get("account_scope_kind"), bound.get("account_id")) != (kind, account_id)):
                    raise ServiceConflict("conversation is bound to another account scope")
            stamp = utcnow().isoformat()
            binding = {"scope": scope, "account_scope_kind": kind, "account_id": account_id,
                       "account_ids": list(account_ids),
                       "created_at": (bound or {}).get("created_at") or stamp, "last_at": stamp}
            bindings[conversation_id] = binding
            db.write_meta(CONVERSATIONS, bindings, conn)
        return binding

    def _conversation_binding(self, db, conversation_id: str) -> dict[str, Any] | None:
        return (db.read_meta(CONVERSATIONS, {}) or {}).get(conversation_id)

    def _advisor_result(self, run_key: str) -> dict[str, Any] | None:
        from ..advisor import readable_answer

        if not re.fullmatch(r"[A-Za-z0-9._-]+", run_key):
            raise ServiceError("invalid run identifier")
        root = (self.profiles().project_dir / "runs").resolve()
        path = (root / run_key / "decision_record.json").resolve()
        if root not in path.parents:
            raise ServiceError("result is outside the run directory")
        try:
            result = json.loads(path.read_text(encoding="utf-8")).get("assistant_result")
            if isinstance(result, dict) and isinstance(result.get("answer"), str):
                result["answer"] = readable_answer(result["answer"], result.get("research_candidates"))
            return result
        except (OSError, ValueError):
            return None

    def advisor_conversation(self, conversation_id: str = "default") -> AdvisorConversation:
        profiles = self.profiles()
        jobs = self._research_manager(profiles).store.list(limit=1000)
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
        bound = self._conversation_binding(self.state_db(profiles), conversation_id) or (
            {"scope": "personal", "account_scope_kind": "unknown", "account_id": None} if messages else {})
        return AdvisorConversation(conversation_id=conversation_id, messages=messages[-80:],
                                   scope=bound.get("scope"), account_scope_kind=bound.get("account_scope_kind"),
                                   account_id=bound.get("account_id"))

    def advisor_state(self, scope: str = "personal", account_id: str | None = None) -> AdvisorState:
        """Everything the assistant screen renders for one account scope.

        The server decides what belongs to the scope: a personal account sees its own records,
        jobs and conversations and the all-accounts reviews that covered it; the Agentic view
        carries no personal snapshot at all and only the real-money account's records.
        """
        from ..advisor import NOTES, SNAPSHOT, age

        if scope not in SCOPES:
            raise ServiceError(f"unknown assistant scope {scope!r}; one of {', '.join(SCOPES)}")
        if account_id is not None and (scope != "personal" or not re.fullmatch(r"[0-9a-f]{20}", account_id)):
            raise ServiceError("account_id names one observed personal account")
        profiles = self.profiles()
        db = self.state_db(profiles)
        snapshot = db.read_meta(SNAPSHOT, {}) or {}
        accounts = self._snapshot_accounts(db)
        kind = "single" if scope == "agentic" or account_id else "all_personal"
        bound_account = "micro_live" if scope == "agentic" else account_id
        account_ids = [] if kind == "single" else [a["id"] for a in accounts]
        label = ("Agentic" if scope == "agentic" else
                 next((a.get("label") for a in accounts if a["id"] == account_id), account_id)
                 if account_id else "all personal accounts")

        def task_in_scope(task: dict[str, Any]) -> bool:
            if task.get("scope", "personal") != scope:
                return False
            if account_id is None:
                return True
            return (task.get("account_id") == account_id
                    or (task.get("account_scope_kind") == "all_personal"
                        and account_id in (task.get("account_ids") or [])))

        jobs = self._research_manager(profiles).store.list(limit=500)
        helper_jobs = [j for j in jobs if j.get("task") and task_in_scope(j["task"])]
        bindings = db.read_meta(CONVERSATIONS, {}) or {}
        summaries: list[dict[str, Any]] = []
        for job in helper_jobs:
            conversation = job["task"].get("conversation_id")
            if not conversation or any(s["conversation_id"] == conversation for s in summaries):
                continue
            bound = bindings.get(conversation) or {"scope": "personal", "account_scope_kind": "unknown",
                                                   "account_id": None}
            summaries.append({"conversation_id": conversation, "scope": bound.get("scope"),
                              "account_scope_kind": bound.get("account_scope_kind"),
                              "account_id": bound.get("account_id"), "last_at": job.get("created_at")})
        shown = dict(snapshot)
        if scope == "agentic":
            shown = {}
        elif account_id is not None:
            shown["accounts"] = [a for a in snapshot.get("accounts") or [] if a.get("id") == account_id]
        coverage = ("The Agentic account's recorded research, proposals and authority; the assistant "
                    "explains and cannot act." if scope == "agentic" else
                    "All Robinhood brokerage accounts, stocks/ETFs only; analysis is read-only.")
        return AdvisorState(
            snapshot=AdvisorSnapshot(**shown),
            stale=age(snapshot) > 300,
            notes=PersonalNotes(**(db.read_meta(NOTES, {"text": "", "revision": 0}) or {})),
            jobs=[AdvisorJob(**{k: j.get(k)
                                for k in ("run_key", "ticker", "state", "error", "created_at", "task")})
                  for j in helper_jobs[:30]],
            conversations=[s["conversation_id"] for s in summaries][:50],
            decisions=self.advisor_decisions(scope=scope, account_id=account_id),
            coverage=coverage,
            scope=scope, account_scope_kind=kind, account_id=bound_account, account_ids=account_ids,
            account_label=label,
            coverage_info={"scope": scope, "instruments": "stocks_and_etfs", "note": coverage},
            conversation_summaries=summaries[:50])

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
                      account_id: str | None = None, language: str = "zh",
                      scope: str = "personal", trigger: str = "manual") -> dict[str, Any]:
        """Queue an assistant task, bound by the server to the account scope it is about.

        The binding is written into the task from what the server knows -- the snapshot's
        accounts, the conversation's existing binding -- and never from anything the model
        says later. A personal task names one observed account (``single``) or, with no
        account, all of them as the snapshot lists them now (``all_personal``). The Agentic
        scope is for explaining what is recorded and admits nothing else. ``trigger`` is
        ``scheduled`` only when the weekly review of the owner's holdings asks; the console
        cannot say it.
        """
        from ..advisor import SYMBOL
        from ..schedule import analyses_today, settings

        if kind not in KINDS:
            raise ServiceError("unknown assistant task")
        if trigger not in ("manual", "scheduled") or (trigger == "scheduled" and kind != "analysis"):
            raise ServiceError("only a holding analysis can be scheduled")
        if scope not in SCOPES:
            raise ServiceError(f"unknown assistant scope {scope!r}; one of {', '.join(SCOPES)}")
        if scope == "agentic" and kind not in AGENTIC_KINDS:
            raise ServiceError(f"the Agentic assistant only explains what is recorded; {kind} is a "
                               "personal task, and Agentic research is started from a proposal or the "
                               "research entry, never from the conversation")
        try:
            request_id = str(uuid.UUID(request_id))
        except ValueError as exc:
            raise ServiceError("request_id must be a UUID") from exc
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", conversation_id):
            raise ServiceError("invalid conversation identifier")
        if language not in ("zh", "en") or len(message) > 4000:
            raise ServiceError("invalid language or message length")
        if kind in CONVERSATIONAL and not message.strip():
            raise ServiceError("a message is required")
        ticker = ticker.strip().upper() if kind == "analysis" else "ASSISTANT"
        if not SYMBOL.fullmatch(ticker):
            raise ServiceError("enter a stock or ETF symbol")
        if account_id is not None and not re.fullmatch(r"[0-9a-f]{20}", account_id):
            raise ServiceError("invalid observed account identifier")
        if scope == "agentic" and account_id is not None:
            raise ServiceError("the Agentic scope names no personal account")
        profiles = self.profiles()
        manager = self._research_manager(profiles)
        db = manager.store.db
        accounts = self._snapshot_accounts(db)
        if account_id is not None and not any(a["id"] == account_id for a in accounts):
            raise ServiceError("unknown personal account; refresh the snapshot")
        if scope == "agentic" or account_id is not None:
            account_kind, account_ids = "single", []
        else:
            account_kind, account_ids = "all_personal", [a["id"] for a in accounts]
        task = {"kind": kind, "language": language, "account_id": account_id, "scope": scope,
                "account_scope_kind": account_kind, "account_ids": account_ids}
        if kind in CONVERSATIONAL:
            task.update(conversation_id=conversation_id, message=message.strip())
        # What the console asked for, which is what a replay must match. The members of an
        # all-accounts scope come from the snapshot and may move between two taps.
        asked = {k: task.get(k) for k in ("kind", "language", "account_id", "scope",
                                          "conversation_id", "message")}
        run_key = "advisor_" + request_id.replace("-", "")
        with db.transaction():
            existing = manager.store.get(run_key)
            if existing:
                stored = {k: (existing.get("task") or {}).get(k) for k in asked}
                stored["scope"] = stored["scope"] or "personal"
                if stored != asked or existing["ticker"] != ticker:
                    raise ServiceConflict("request_id was already used for a different request")
                return {**existing, "deduplicated": True}
            jobs = manager.store.list(limit=10000)
            if any(j["state"] in ("QUEUED", "RUNNING") and j.get("task", {}).get("conversation_id") == conversation_id
                   for j in jobs) and kind in CONVERSATIONAL:
                raise ServiceConflict("The assistant is still answering this conversation.")
            cfg = settings(profiles)
            if kind == "analysis":
                if analyses_today(jobs, cfg) >= int(cfg["max_analyses_per_day"]):
                    raise ServiceError("Today's full-research budget is used; try again next trading day.")
            elif kind != "refresh":
                assistant_jobs = [j for j in jobs if j.get("task", {}).get("kind") in CONVERSATIONAL]
                # These short calls have their own cap while sharing the existing concurrency pool.
                stamp = utcnow().date().isoformat()
                if sum(j["created_at"].startswith(stamp) for j in assistant_jobs) >= 30:
                    raise ServiceError("Today's 30 assistant replies are used.")
            if kind in CONVERSATIONAL:
                self._bind_conversation(db, conversation_id, scope, account_kind, account_id, account_ids)
            job, created = manager.create(run_key=run_key, ticker=ticker, bucket="advisory",
                                          trade_date=_last_session(), instrument_type="auto",
                                          trigger=trigger, task=task)
        manager.dispatch()
        return {**(manager.store.get(job["run_key"]) or job), "deduplicated": not created}
