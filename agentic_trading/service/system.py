"""How the system itself is doing: health, the model budget, the market, the schedule, the log."""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .. import autonomy
from ..env import load_env
from ..profile import Profiles
from ..proposals import utcnow
from .errors import ServiceError
from .shapes import ClaudeStatus, Health, MarketStatus
from .status import SystemStatus

#: How much of runs/llm_calls.jsonl /health reads: several days of calls at the daily budget.
AUDIT_TAIL_BYTES = 2_000_000


#: Named for the package rather than this module: the split is internal, so a log
#: record must still read as it did, and getLogger returns the one same object.
logger = logging.getLogger("agentic_trading.service")


class SystemMixin:
    """Status the console shows about the deployment rather than about the money."""

    # ---------------------------------------------------------------- health
    def health(self) -> Health:
        """One call a dashboard can poll: can the system act, and what is blocking it."""
        warnings: list[str] = []
        try:
            profiles = self.profiles()
        except ServiceError as exc:
            # Nothing below this line can be answered without a config, so the shape is the
            # status, the reason and the time — and every other field is absent, not zeroed.
            return Health(status=SystemStatus.CONFIG_ERROR, detail=str(exc),
                          checked_at=utcnow().isoformat())

        halt_file = autonomy.halt_file(profiles)
        paused = halt_file.exists() or bool(
            profiles.risk_cfg("execution_policy", "kill_switch", "trading_halted", default=False))

        risk = self.risk_state()
        halted = risk.get("total", {}).get("manual_reset_required", False)

        claude = self.claude_status()
        # A halt waits for a person; a subscription limit clears on its own at a stated time.
        # Ranked the other way, a drawdown halt was reported as "limit reached" for as long as
        # both held. Either one blocks scheduled research, and `claude` still carries the limit.
        if halted:
            status = SystemStatus.HALTED
        elif claude.state == "LIMIT_REACHED":
            status = SystemStatus.CLAUDE_LIMIT_REACHED
        elif claude.state == "LOGIN_REQUIRED":
            status = SystemStatus.CLAUDE_LOGIN_REQUIRED
        elif paused:
            status = SystemStatus.PAUSED
        else:
            status = SystemStatus.READY

        if not _fred_configured(profiles):
            warnings.append("FRED macro data is unavailable, so inflation, rate and labour context cannot be "
                            "verified. The system records the gap, lowers confidence and prefers NO_TRADE. "
                            "This is an optional enhancement, not a blocker.")
        if profiles.unresolved and not profiles.accept_defaults:
            warnings.append(f"{len(profiles.unresolved)} risk parameters are documented defaults you have not "
                            "explicitly accepted; real-money execution is blocked until you do.")

        market = self.market_status()
        return Health(
            status=status,
            detail={
                SystemStatus.READY: "able to research and to accept approvals",
                SystemStatus.PAUSED: "kill switch engaged: no orders will be submitted",
                SystemStatus.HALTED: "a drawdown ladder halt is active and needs a manual reset",
                SystemStatus.CLAUDE_LIMIT_REACHED: claude.detail,
                SystemStatus.CLAUDE_LOGIN_REQUIRED: claude.detail,
                SystemStatus.CONFIG_ERROR: "configuration error",
            }[status],
            paused=paused,
            stage=profiles.stage,
            market=market,
            claude=claude,
            micro_live_enabled=self._micro_enabled(profiles),
            warnings=warnings,
            checked_at=utcnow().isoformat(),
        )

    def claude_status(self) -> ClaudeStatus:
        """Subscription state, read from the most recent runs. Never exposes credentials."""
        profiles = self.profiles()
        audit = profiles.project_dir / "runs" / "llm_calls.jsonl"
        last_call, calls_today = None, 0
        today = datetime.now(UTC).date()
        # The audit is only ever appended to and nothing rotates it, and /health reads this on
        # every poll: read its end, not all of it. Timestamps carry the Mac's local offset, so a
        # call is counted by its UTC date; comparing the local string against the UTC date
        # dropped every call made in the hours the two dates differ. One unreadable line used to
        # end the count at that line.
        for line in _file_tail(audit, AUDIT_TAIL_BYTES):
            try:
                ts = str(json.loads(line).get("ts") or "")
                when = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S%z")
            except (ValueError, TypeError, AttributeError):
                continue
            last_call = ts
            if when.astimezone(UTC).date() == today:
                calls_today += 1
        state, detail = "OK", "subscription-backed Claude Code runtime"
        jobs = self._research_manager(profiles).store.list(limit=1)
        if jobs and jobs[0].get("state") == "CLAUDE_LIMIT_REACHED":
            state = "LIMIT_REACHED"
            detail = jobs[0].get("error") or "Claude subscription limit reached"
        elif jobs and jobs[0].get("failure_code") == "llm_login_expired" and jobs[0].get(
                "state") in ("FAILED", "QUEUED"):
            # Read from the newest job, as the limit is: the next run that completes clears it.
            # QUEUED counts because that is what a failed run becomes while it waits to be
            # retried, and the login is no more valid for having a retry scheduled.
            state = "LOGIN_REQUIRED"
            detail = ("the Claude login on this Mac has expired; research cannot run until "
                      "someone signs in again (run `claude`, then /login)")
        return ClaudeStatus(state=state, detail=detail,
                            billing="claude_subscription_no_api_charges",
                            calls_today=calls_today, last_call_at=last_call)

    def market_status(self) -> MarketStatus:
        try:
            import pandas as pd

            from ..broker.robinhood import _nyse_calendar

            cal = _nyse_calendar()
            now = pd.Timestamp.now(tz="UTC")
            is_open = bool(cal.is_open_on_minute(now, ignore_breaks=True))
            return MarketStatus(is_open=is_open, calendar=cal.name,
                                next_open=str(cal.next_open(now)) if not is_open else None,
                                next_close=str(cal.next_close(now)) if is_open else None)
        except Exception as exc:  # noqa: BLE001 - an unknown session is reported closed
            return MarketStatus(is_open=False, error=f"{type(exc).__name__}: {exc}")

    # ---------------------------------------------------------------- operations
    def record_activity(self, activity: str, detail: str = "", *,
                        profiles: Profiles | None = None,
                        min_interval_seconds: float = 60.0) -> None:
        """Note that a background activity just succeeded."""
        from .. import operations as ops

        try:
            ops.record(self.state_db(profiles or self.profiles()), activity, detail,
                       min_interval_seconds=min_interval_seconds)
        except Exception:
            logger.debug("could not record the %s heartbeat", activity, exc_info=True)

    @staticmethod
    def _activity_state(db, name: str, success_at: str | None, age: float | None, window: float,
                        expected_since: str | None, now: datetime) -> dict[str, Any]:
        """Which of the four states this activity is in, and the three dates behind the answer.

        The states exist because "overdue" was answering two questions with one word: an activity
        whose code arrived this afternoon and has not come due yet read exactly like one that
        should have run and did not. See docs/ACTIVITY_STATE_CONTRACT_2026-09-19.md.

        ``due_by`` is the moment the state flips to ``overdue``, not the next expected run: those
        differ by the window of tolerance an activity that has run before is given, and a screen
        that showed the second would count down to a deadline nothing happens at.
        """
        from .. import operations as ops

        def moment(stamp: str | None) -> datetime | None:
            try:
                return datetime.fromisoformat(stamp) if stamp else None
            except ValueError:
                return None

        success, since = moment(success_at), moment(expected_since)
        # Two windows once it has run before, one from the moment it became expected. Both are
        # the existing tolerances; only their publication is new.
        due = (success + timedelta(minutes=window * 2) if success is not None
               else since + timedelta(minutes=window) if since is not None else None)

        attempt_at, failure = None, None
        report_key = ops.REPORTED.get(name)
        if report_key:
            report = db.read_meta(report_key, {}) or {}
            attempt_at = report.get("at")
            if attempt_at and not report.get("confirmed", False):
                reasons = [str(row.get("error")) for row in report.get("ledgers") or []
                           if row.get("error")]
                failure = {"at": attempt_at,
                           "reason": "; ".join(reasons) or str(report.get("detail")
                                                               or "the check did not pass")}

        attempt = moment(attempt_at)
        # Failure is the most recent outcome, never a state to be cleared by hand: one success
        # newer than the refusal ends it. And a refusal only means "it runs and refuses" while
        # the attempts themselves are still arriving; once they stop, this is not failing, it is
        # not running, which is what overdue says.
        refused_last = failure is not None and (success is None or attempt > success)
        still_trying = attempt is not None and now <= attempt + timedelta(minutes=window * 2)
        if refused_last and still_trying:
            state = "failed"
        elif (due is not None and now > due) or (due is None and success is None):
            state = "overdue"
        elif success is None:
            state = "awaiting_first_run"
        else:
            state = "current"
        return {"state": state, "expected_since": expected_since,
                "due_by": due.isoformat() if due is not None else None,
                "last_attempt_at": attempt_at, "last_failure": failure}

    def operations(self) -> dict[str, Any]:
        """Whether the unattended parts of the system are still running, and what they left."""
        from .. import operations as ops

        profiles = self.profiles()
        cfg = profiles.risk.get("operations") or {}
        expected = dict(cfg.get("expected_every_minutes") or {})
        db = self.state_db(profiles)
        record = ops.heartbeats(db)
        origin = ops.origins(db)
        now = utcnow()

        activities = []
        for name in ops.ACTIVITIES:
            entry = record.get(name) or {}
            at = entry.get("at")
            age = None
            if at:
                try:
                    age = round((now - datetime.fromisoformat(at)).total_seconds() / 60, 1)
                except ValueError:
                    at = None
            window = float(expected.get(name, 1440))
            row = {
                "id": name, "last_success_at": at, "age_minutes": age,
                "expected_every_minutes": window,
                # Never having run is overdue too: an install whose backup job was never loaded
                # looks identical to a healthy one until the day it is needed. Kept exactly as it
                # was, including for an activity that has not come due yet: `state` is what a
                # screen reads, and this stays put so an older reader loses nothing.
                "overdue": age is None or age > window * 2,
                "detail": entry.get("detail") or "",
            }
            row.update(self._activity_state(db, name, at, age, window, origin.get(name), now))
            activities.append(row)

        return {
            "checked_at": now.isoformat(),
            "activities": activities,
            "disk": ops.disk(profiles.project_dir, float(cfg.get("low_disk_free_gb", 10))),
            "retention": {"runs_days": int(cfg.get("runs_retention_days", 120)),
                          "logs_mb": int(cfg.get("log_max_mb", 32)),
                          "backups_days": int(cfg.get("backup_retention_days", 30))},
        }

    # ---------------------------------------------------------------- activity
    def activity(self, *, kinds: tuple[str, ...] = (), subject: str | None = None,
                 cursor: int | None = None, limit: int = 50) -> dict[str, Any]:
        """One page of the audit journal, newest first.

        The console used to build a timeline from the research list plus the history of
        whichever proposal happened to be open, which could show one proposal's story and never
        the system's. This is the whole record, in one order, and it survives a refresh, a
        different device and a proposal leaving the queue.
        """
        from .. import events

        profiles = self.profiles()
        db = self.state_db(profiles)
        # Proposals that closed before the journal existed still carry their own history.
        events.backfill(db, self._store(profiles).all())
        return events.query(db, kinds=kinds, subject=subject, cursor=cursor, limit=limit)

    # ---------------------------------------------------------------- evaluation
    def evaluation_report(self, scope: str = "demo") -> dict[str, Any]:
        """The forward record for one account's study, assembled from the sources that hold it.

        Nothing is duplicated into a separate ledger of its own: the decisions, jobs, proposals
        and fills are read where they live, so the report cannot drift away from what happened.
        ``scope`` picks the sample: the paper book (``demo``, the study that already exists),
        the real-money account (``agentic``), or what the record says about each research route
        (``route_quality``, which measures no return at all). A scope with no declared study
        answers ``state: NOT_STARTED`` with the record's counts and no performance, never 500.
        """
        from .. import evaluation

        research = self.research_history(limit=500)
        jobs = self.research_jobs(limit=500)
        if scope == "route_quality":
            return evaluation.route_quality(research, jobs)
        if scope not in evaluation.SCOPES:
            raise ServiceError(f"unknown evaluation scope {scope!r}; one of "
                               f"{', '.join((*evaluation.SCOPES, 'route_quality'))}")
        profiles = self.profiles()
        return evaluation.report(
            self.state_db(profiles), profiles, research=research, jobs=jobs,
            # The study reads rows, not the response shape: dumping here keeps the evaluation
            # module free of a model that exists for the console.
            proposals=[p.model_dump() for p in self.proposals(include_closed=True)],
            fills=[f.model_dump() for f in self.orders(limit=200).fills], scope=scope)

    def evaluation_snapshot(self, on: str | None = None, scope: str | None = None) -> dict[str, Any]:
        """Mark a study to market for one session. Called daily by the maintenance job.

        Without a scope, every study that is running is marked from its own account, and the
        result is keyed by scope; with one, that study alone, as ``ct-evaluate snapshot`` asks.
        """
        from .. import evaluation

        profiles = self.profiles()
        db = self.state_db(profiles)
        scopes = [scope] if scope else [name for name in evaluation.SCOPES if evaluation.running(db, name)]
        if not scopes:
            raise ServiceError("no study is running; start one with ct-evaluate start")
        portfolio = self.portfolio()
        marked: dict[str, Any] = {}
        for name in scopes:
            who = evaluation.ACCOUNTS[name]
            account = portfolio.accounts.get(who["account_id"])
            if account is None:
                raise ServiceError(f"the {who['account_label']} account could not be read")
            marked[name] = evaluation.snapshot(
                db, profiles, on=on or portfolio.as_of, equity_usd=account.equity_usd,
                net_deposits_usd=account.net_deposits_usd, scope=name)
        return marked[scope] if scope else marked

    # ---------------------------------------------------------------- schedule
    def schedule_status(self) -> dict[str, Any]:
        """What the schedule will do next, what it last did, and why it last did nothing.

        One entry per scheduled task, because a deployment that reviews its real-money holdings
        daily and its candidates weekly has two answers to "what runs next", and a single one
        would be a guess about which. The top-level fields describe the first task, which is the
        only task until an account is configured, so a screen that reads them keeps working.
        """
        from .. import schedule as scheduling
        from ..advisor import SNAPSHOT

        profiles = self.profiles()
        cfg = scheduling.settings(profiles)
        db = self.state_db(profiles)
        record = db.read_meta(scheduling.EXECUTIONS, {}) or {}
        jobs = self.research_jobs(limit=200)
        history = self.research_history(limit=200)
        snapshot = db.read_meta(SNAPSHOT, {}) or {}
        health = self.health()
        plan = scheduling.tasks(cfg)
        decisions = [scheduling.decide(
            profiles, task=task,
            subjects=scheduling.subjects_for(profiles, task, history=history, snapshot=snapshot),
            health_status=health.status, paused=bool(health.paused), executed=record, jobs=jobs)
            for task in plan]
        periods = record.get("periods") or {}
        last_key = max(periods) if periods else None
        used = scheduling.analyses_today(jobs, cfg)
        first = decisions[0]
        return {
            "enabled": bool(cfg.get("enabled", True)),
            "period": cfg.get("period"),
            "calendar": cfg.get("calendar"),
            "market_timezone": cfg.get("market_timezone"),
            "on_non_session_day": cfg.get("on_non_session_day"),
            "run_while_paused": bool(cfg.get("run_while_paused", True)),
            "tickers": list(first.tickers),
            "next_run_at": first.next_run_at,
            "current_period": first.period_key,
            "would_run_now": first.run,
            "reason": first.reason,
            "analyses_today": used,
            "daily_budget": int(cfg.get("max_analyses_per_day", 6)),
            "last_period": last_key,
            "last_run": periods.get(last_key) if last_key else None,
            "last_skip": record.get("last_skip"),
            # How often the operating system's job has to fire to serve every task, how much of
            # the day's allowance a candidate study may not touch, and whether accounts are
            # scheduled at all yet.
            "fires_every": scheduling.finest_period(cfg),
            "reserve_for_exits": int(cfg.get("reserve_for_exits", 0)),
            "per_account": bool(cfg.get("accounts")),
            "tasks": [{
                "bucket": decision.bucket, "kind": decision.kind, "period": task.period,
                "max_tickers_per_run": task.max_tickers,
                "current_period": decision.period_key, "would_run_now": decision.run,
                "reason": decision.reason, "tickers": list(decision.tickers),
                "trade_date": decision.trade_date,
                "last_run": periods.get(decision.period_key),
            } for task, decision in zip(plan, decisions, strict=True)],
        }

    # ---------------------------------------------------------------- the console password
    def record_password_change(self, identity: str | None = None) -> dict[str, Any]:
        self._journal(self.profiles(), kind="password_changed", actor=identity or "console")
        return {"rotated": True}


# ---------------------------------------------------------------------- helpers
def _fred_configured(profiles: Profiles) -> bool:
    """A real key, not an empty placeholder line copied from .env.example.

    This used to read the environment and then fall back to parsing .env itself, which meant the
    health page could answer from the file while the process doing the fetching had nothing —
    the warning would go quiet without the data arriving. The file is now loaded into the
    environment at every entry point (:mod:`agentic_trading.env`), so there is one answer
    and it is the one the fetcher sees.
    """
    import os

    if os.environ.get("FRED_API_KEY", "").strip():
        return True
    return bool(load_env(profiles.project_dir) and os.environ.get("FRED_API_KEY", "").strip())


def _file_tail(path: Path, max_bytes: int) -> list[str]:
    """The complete lines in the last ``max_bytes`` of a file; nothing if it cannot be read."""
    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(max(0, size - max_bytes))
            data = stream.read()
    except OSError:
        return []
    lines = data.decode("utf-8", errors="replace").splitlines()
    return lines[1:] if size > max_bytes else lines        # the first may start mid-line
