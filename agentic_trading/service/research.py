"""Starting research, following a run, and reading what a finished one recorded."""

from __future__ import annotations

import json
import re
import uuid
from datetime import date as calendar_date
from pathlib import Path
from typing import Any, ClassVar

from .. import UPSTREAM_TRADINGAGENTS_COMMIT, __version__, agent_roster, autonomy
from ..micro_live import BUCKET as MICRO_BUCKET
from ..proposals import utcnow
from .base import _last_session
from .errors import ServiceConflict, ServiceError

BUCKETS = ("core", "sandbox", MICRO_BUCKET)

#: Which account scope a bucket's research belongs to. The personal assistant's runs carry the
#: ``advisory`` bucket and are ``personal``; a record with no bucket at all is ``unspecified``
#: and is never guessed into a scope.
SCOPE_OF_BUCKET = {MICRO_BUCKET: "agentic", "core": "demo", "sandbox": "demo", "advisory": "personal"}
BUCKETS_OF_SCOPE = {"agentic": (MICRO_BUCKET,), "demo": ("core", "sandbox")}
PURPOSES = ("review_holding", "candidate", "exit_review")
#: Purposes that only make sense for something the book holds.
HELD_PURPOSES = ("review_holding", "exit_review")
#: How much of the bear case a list row carries; the full text is in the run's trace.
COUNTER_EVIDENCE_CHARS = 600
#: The personal tasks whose records are research records rather than conversation.
RECORD_TASKS = ("analysis", "portfolio_review")


class ResearchMixin:
    """The research jobs, their traces and their history."""

    # ---------------------------------------------------------------- research
    def start_research(self, ticker: str, *, bucket: str = "core", instrument_type: str = "auto",
                       trade_date: str | None = None, trigger: str = "manual",
                       scope: str | None = None, request_id: str | None = None,
                       purpose: str | None = None) -> dict[str, Any]:
        """Persist and enqueue a research run, deduplicating an identical active run.

        ``scope`` must agree with ``bucket`` when given: the console names the account it is
        looking at, and a run that landed in another book would be researched against the
        wrong money. ``purpose`` says why the run was asked for, and the two purposes that
        speak of a holding are refused for a symbol the book does not hold rather than
        researched as if it did. ``request_id`` makes a repeated tap return the run it already
        started, and a different request under the same id a conflict.
        """
        if bucket not in BUCKETS:
            raise ServiceError(f"unknown bucket {bucket!r}")
        if scope is not None and bucket not in BUCKETS_OF_SCOPE.get(scope, ()):
            raise ServiceError(f"scope {scope!r} does not hold bucket {bucket!r}; "
                               f"agentic is {MICRO_BUCKET}, demo is core or sandbox")
        if purpose is not None and purpose not in PURPOSES:
            raise ServiceError(f"unknown purpose {purpose!r}; one of {', '.join(PURPOSES)}")
        ticker = ticker.upper().strip()
        if (not re.fullmatch(r"[A-Z0-9.^/-]{1,24}", ticker)
                or not any(character.isalnum() for character in ticker)):
            raise ServiceError("ticker contains unsupported characters")
        if instrument_type not in ("auto", "etf", "stock", "crypto"):
            raise ServiceError(f"unknown instrument type {instrument_type!r}")
        if request_id is not None:
            try:
                request_id = str(uuid.UUID(request_id))
            except ValueError as exc:
                raise ServiceError("request_id must be a UUID") from exc
        profiles = self.profiles()
        if purpose in HELD_PURPOSES and not self._holds(profiles, bucket, ticker):
            raise ServiceError(f"{ticker} is not held in {bucket}; use purpose=candidate")
        date = trade_date or _last_session()
        try:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
                raise ValueError
            calendar_date.fromisoformat(date)
        except ValueError as exc:
            raise ServiceError("trade_date must use ISO yyyy-mm-dd format") from exc
        manager = self._research_manager(profiles)
        if request_id is not None:
            for job in manager.store.list(limit=10000):
                if job.get("request_id") != request_id:
                    continue
                if (job["ticker"], job["bucket"], job.get("purpose")) != (ticker, bucket, purpose):
                    raise ServiceConflict("request_id was already used for a different request")
                return {**self._describe_job(job, profiles), "deduplicated": True}
        safe_ticker = re.sub(r"[^A-Z0-9._-]", "-", ticker)
        run_key = f"{date}_{safe_ticker}_{uuid.uuid4().hex[:8]}"
        job, created = manager.create(
            run_key=run_key, ticker=ticker, bucket=bucket, trade_date=date,
            instrument_type=instrument_type, trigger=trigger,
            # The authority the run is submitted under. A proposal born under a different
            # one is marked, and a tightened one sends it to a person (autonomous_reductions).
            authority_version=autonomy.note_version(profiles, manager.store.db),
            purpose=purpose, request_id=request_id,
        )
        manager.dispatch()
        current = manager.store.get(job["run_key"]) or job
        return {**self._describe_job(current, profiles), "deduplicated": not created}

    def _holds(self, profiles, bucket: str, ticker: str) -> bool:
        """Whether the book behind a bucket holds a symbol, from the ledger, never the venue."""
        from ..portfolio import PortfolioError, load_portfolio

        if bucket == MICRO_BUCKET:
            path = (profiles.risk.get("micro_live_execution") or {}).get(
                "ledger_file", "state/micro_live_portfolio.yaml")
        else:
            path = (profiles.risk.get("portfolio_state") or {}).get("file", "state/portfolio.yaml")
        try:
            ledger = load_portfolio(profiles.project_dir / str(path))
        except (PortfolioError, OSError, ValueError) as exc:
            raise ServiceError(f"the {bucket} ledger could not be read: {exc}") from exc
        return ledger.position(ticker, bucket) is not None

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
        return {**self._describe_job(job, profiles), "log_tail": text[-4000:], "progress": progress,
                **({"assistant_result": self._advisor_result(run_key)} if job.get("task") else {})}

    def research_jobs(self, limit: int = 50, *, scope: str | None = None,
                      account_id: str | None = None) -> list[dict[str, Any]]:
        """The persistent queue, newest first, each job saying which account it is about."""
        profiles = self.profiles()
        jobs = [self._describe_job(job, profiles)
                for job in self._research_manager(profiles).store.list(limit=limit)]
        return [job for job in jobs if _in_scope(job, scope, account_id, include_all_personal=True)]

    def _describe_job(self, job: dict[str, Any], profiles) -> dict[str, Any]:
        """A stored job plus the fields a screen binds it by: scope, account, task type, route."""
        task = job.get("task") or {}
        out = dict(job)
        out.update(_scope_fields(job.get("bucket"), task if task else None,
                                 labels=self._personal_labels(profiles)))
        out["task_type"] = task.get("kind") if task else "full_research"
        out.setdefault("purpose", None)
        out.setdefault("request_id", None)
        out.setdefault("authority_version", None)
        out.setdefault("opened_at", None)
        out["route"] = _route(job.get("route"), bucket=job.get("bucket"), task_kind=out["task_type"])
        out["cancellable"] = job.get("state") in ("QUEUED", "RUNNING")
        return out

    def _personal_labels(self, profiles) -> dict[str, str]:
        from ..advisor import SNAPSHOT

        try:
            snapshot = self.state_db(profiles).read_meta(SNAPSHOT, {}) or {}
        except Exception:  # noqa: BLE001 - a label is decoration; the id is the identity
            return {}
        return {str(a.get("id")): str(a.get("label")) for a in snapshot.get("accounts") or []
                if a.get("id")}

    def recover_research_jobs(self) -> dict[str, Any]:
        profiles = self.profiles()
        result = self._research_manager(profiles).tick()
        queued = len(self._research_manager(profiles).store.list(states=("QUEUED", "RUNNING")))
        self.record_activity("research_dispatch", f"{queued} job(s) active", profiles=profiles)
        return result

    def mark_opened(self, run_key: str) -> dict[str, Any]:
        """Record that a finished run's result was shown to the person. Idempotent.

        A write of its own rather than a side effect of reading the run: the console polls a
        run every few seconds while its page is open, and a read that counted as "seen" would
        clear the finished-task item on Home before anyone had looked at the result. The
        console calls this once the result is actually on screen, under the same same-origin
        and CSRF rules as every other write.
        """
        from ..research_jobs import TERMINAL_STATES

        if not re.fullmatch(r"[A-Za-z0-9._-]+", run_key):
            raise ServiceError(f"invalid research run {run_key!r}")
        profiles = self.profiles()
        manager = self._research_manager(profiles)
        job = manager.store.get(run_key)
        if job is None:
            raise ServiceError(f"no research run {run_key!r}")
        if job["state"] not in TERMINAL_STATES:
            raise ServiceConflict(f"research run {run_key} is still {job['state']}; nothing to open yet")
        if not job.get("opened_at"):
            job = manager.store.update(run_key, {"opened_at": utcnow().isoformat()},
                                       expected_states=TERMINAL_STATES) or manager.store.get(run_key) or job
        return self._describe_job(job, profiles)

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

    def research_history(self, limit: int = 20, *, scope: str | None = None,
                         account_id: str | None = None, task_type: str | None = None,
                         include_all_personal: bool = False) -> list[dict[str, Any]]:
        """Completed decision records, newest first, each bound to the account it is about.

        A record says which scope and account it belongs to, which route produced it, what its
        facts are as of, what it lacked, and whether a newer record of the same kind about the
        same thing has superseded it. Nothing here is guessed: a record that predates a field
        reports it as unknown, and a record with no bucket belongs to no scope.
        """
        profiles = self.profiles()
        labels = self._personal_labels(profiles)
        runs = sorted((profiles.project_dir / "runs").glob("*/decision_record.json"),
                      key=lambda p: p.stat().st_mtime, reverse=True)
        out: list[dict[str, Any]] = []
        latest: dict[tuple, str] = {}
        for path in runs:
            try:
                d = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if d.get("task_kind") in ("refresh", "chat", "shortlist"):
                continue
            row = _describe_record(d, profiles, labels)
            # Newest first, so the first record seen under a key is the one that stands and
            # every later one under the same key is superseded by it. Chat and lightweight
            # checks have no key: they never supersede a full analysis.
            key = _supersede_key(row)
            if key is not None:
                if key in latest:
                    row["superseded_by"] = latest[key]
                else:
                    latest[key] = row["run_id"]
            if not _in_scope(row, scope, account_id, include_all_personal=include_all_personal):
                continue
            if task_type is not None and row["task_type"] != task_type:
                continue
            out.append(row)
            if len(out) >= limit:
                break
        return out


# ---------------------------------------------------------------------- record description
def _scope_fields(bucket: str | None, task: dict[str, Any] | None, *, labels: dict[str, str],
                  explicit_kind: str | None = None, explicit_ids: list[str] | None = None,
                  explicit_account: str | None = None) -> dict[str, Any]:
    """The four fields a screen binds a record or job by, from what the record actually says."""
    if task is not None or bucket == "advisory":
        account = explicit_account if explicit_account is not None else (task or {}).get("account_id")
        kind = explicit_kind or ("single" if account else "unknown")
        ids = list(explicit_ids or [])
        label = (labels.get(account) if account else
                 {"all_personal": "all personal accounts", "unknown": "unspecified"}.get(kind))
        return {"scope": "personal", "account_scope_kind": kind, "account_id": account,
                "account_ids": ids, "account_label": label or (account if account else "unspecified")}
    scope = SCOPE_OF_BUCKET.get(str(bucket)) if bucket else None
    if scope is None:
        return {"scope": "unspecified", "account_scope_kind": "unknown", "account_id": None,
                "account_ids": [], "account_label": "unspecified"}
    account = MICRO_BUCKET if scope == "agentic" else "conceptual"
    return {"scope": scope, "account_scope_kind": "single", "account_id": account,
            "account_ids": [], "account_label": "Agentic" if scope == "agentic" else "Demo"}


def _route(explicit: Any, *, bucket: str | None, task_kind: str | None,
           record: dict[str, Any] | None = None) -> dict[str, Any]:
    """Which route produced a record, believed from the record and otherwise read from history.

    Before records named their route, every run of a trading bucket and every personal
    ``analysis`` went through the TradingAgents graph, and every other personal task through
    the assistant's single call. That is what the code did, so it is stated, and marked as
    read from history rather than from the record.
    """
    record = record or {}
    if isinstance(explicit, dict) and explicit.get("name"):
        name, inferred = str(explicit["name"]), False
    elif bucket == "advisory" or task_kind in ("chat", "shortlist", "refresh", "portfolio_review"):
        name, inferred = ("tradingagents" if task_kind == "analysis" else "assistant"), True
    else:
        name, inferred = "tradingagents", True
    models = record.get("models") or {}
    return {"name": name, "inferred": inferred,
            "package_version": record.get("package_version") or __version__,
            "upstream_commit": record.get("upstream_tradingagents_commit") or UPSTREAM_TRADINGAGENTS_COMMIT,
            "skill_version": (explicit or {}).get("skill_version") if isinstance(explicit, dict) else None,
            "models": {"deep": models.get("deep"), "quick": models.get("quick")}}


def _describe_record(d: dict[str, Any], profiles, labels: dict[str, str]) -> dict[str, Any]:
    policy = d.get("policy") or {}
    proposal_doc = policy.get("proposal") or {}
    task_kind = d.get("task_kind")
    task_type = task_kind if task_kind else "full_research"
    scope = _scope_fields(d.get("bucket"), {"account_id": d.get("account_id")} if d.get("advisory") else None,
                          labels=labels, explicit_kind=d.get("account_scope_kind"),
                          explicit_ids=d.get("account_ids"), explicit_account=d.get("account_id"))
    web = ((d.get("data_sources") or {}).get("web_access") or {})
    fetches = web.get("fetches") or []
    retrieved = [str(f.get("retrieved_at")) for f in fetches if isinstance(f, dict) and f.get("retrieved_at")]
    bear = str((d.get("upstream_decision") or {}).get("bear_case_excerpt") or "")
    missing = list(proposal_doc.get("missing_information") or [])
    row: dict[str, Any] = {
        "run_id": d.get("run_id"), "ticker": d.get("ticker"), "bucket": d.get("bucket"),
        "trade_date": d.get("trade_date"), "created_at": d.get("created_at"),
        # Which desks this run seated. The console shows the team per run rather than
        # inferring an instrument type back out of the analyst list.
        "instrument_type": d.get("instrument_type"),
        "portfolio_manager_rating": (d.get("upstream_decision") or {}).get("portfolio_manager_signal"),
        "policy_action": policy.get("final_action"), "confidence": policy.get("confidence"),
        # Evidence quality travels with the decision: a run that concluded without the
        # macro context is not the same kind of result as one that had it.
        "data_quality": proposal_doc.get("data_quality"),
        "missing_information": len(missing),
        "hard_risk_verdict": (d.get("hard_risk") or {}).get("verdict"),
        "final_action": (d.get("final_recommendation") or {}).get("action"),
        "proposal_id": d.get("proposal_id"),
        "llm_calls": (d.get("llm_usage") or {}).get("calls"),
        "elapsed_seconds": d.get("elapsed_seconds"),
        **scope,
        "task_type": task_type,
        "purpose": d.get("purpose"),
        "route": _route(d.get("route"), bucket=d.get("bucket"), task_kind=task_type, record=d),
        "authority_version": (d.get("authority") or {}).get("research"),
        "snapshot_as_of": (d.get("portfolio") or {}).get("as_of"),
        "prices_as_of": d.get("trade_date"),
        "sources_retrieved_at_max": max(retrieved) if retrieved else None,
        # Never derived from the time this side read a source. Only a source that carries its
        # own event time or reporting period can say what the facts are as of, and no record
        # carries those yet.
        "facts_as_of": None, "facts_as_of_basis": "unknown",
        "evidence": {"sources": len((d.get("data_sources") or {}).get("observed_tools") or []),
                     "web_fetches": len(fetches),
                     "cli_web_search_used": bool(web.get("cli_web_search_used"))},
        "limitations": {"data_quality": proposal_doc.get("data_quality"),
                        "missing_information": missing,
                        "unresolved_profile_fields": len(d.get("unresolved_profile_fields") or [])},
        "counter_evidence": bear[:COUNTER_EVIDENCE_CHARS] or None,
        "counter_evidence_truncated": len(bear) > COUNTER_EVIDENCE_CHARS,
        "changes_since_previous": None,
        "refers_to_run_id": None,
        "superseded_by": None,
        "applicable_floor": _applicable_floor(d, profiles, scope["scope"]),
    }
    return row


def _applicable_floor(d: dict[str, Any], profiles, scope: str) -> int | None:
    """The confidence a run in this scope would need to open a position, from the profile.

    Personal research has no floor: nothing it concludes can become an order. A floor the
    profile declares unreachable is reported as none rather than as a number above 100.
    """
    if scope not in ("agentic", "demo"):
        return None
    try:
        floor = profiles.confidence.floor(
            str(d.get("bucket")), str(d.get("instrument_type") or "etf"), "small",
            limit_class=(d.get("proposal") or {}).get("limit_class"))
    except Exception:  # noqa: BLE001 - a floor the profile cannot state is no floor
        return None
    return int(floor) if 0 <= int(floor) <= 100 else None


def _supersede_key(row: dict[str, Any]) -> tuple | None:
    kind = row["task_type"]
    if kind == "full_research":
        return ("full_research", row["scope"], row["bucket"], row["ticker"], row.get("purpose"))
    if kind == "analysis":
        return ("analysis", row["scope"], row["account_scope_kind"], row["account_id"], row["ticker"])
    if kind == "portfolio_review":
        return ("portfolio_review", row["scope"], row["account_scope_kind"], row["account_id"])
    return None


def _in_scope(row: dict[str, Any], scope: str | None, account_id: str | None, *,
              include_all_personal: bool) -> bool:
    if scope is not None and row.get("scope") != scope:
        return False
    if account_id is None:
        return True
    if row.get("account_scope_kind") == "single":
        return row.get("account_id") == account_id
    if row.get("account_scope_kind") == "all_personal":
        return include_all_personal and account_id in (row.get("account_ids") or [])
    return False
