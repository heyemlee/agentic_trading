"""When scheduled research is allowed to run, and what it should analyse.

The operating system's scheduler decides only when the command is invoked. Every decision about
whether that invocation should do anything is made here, so a market holiday, a catch-up run
after a restart, a repeated trigger, an exhausted daily budget and an engaged kill switch all
have one answer instead of one per entry point.

Nothing in this module starts research or writes a proposal. It reads configuration and durable
state and returns a decision, which is what makes it testable without a model or a broker.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

#: Statuses that make research pointless or impossible regardless of configuration. PAUSED is
#: deliberately absent: the kill switch stops orders, and research is how the system notices
#: that conditions changed while it was paused.
BLOCKING_STATUS = ("HALTED", "CLAUDE_LIMIT_REACHED", "CONFIG_ERROR")

#: The document that records which periods have already been executed.
EXECUTIONS = "schedule_executions"
MAX_RECORDS = 60


@dataclass
class Decision:
    """Whether to run, what to run, and a reason a person can act on either way.

    ``trade_date`` is the calendar's answer to "which session has closed", used to decide
    whether running now makes sense at all. It is not the session the research anchors to: a
    session closes hours before its prices are published, so the run asks the data layer for
    the newest session that actually has data.
    """

    run: bool
    reason: str
    period_key: str
    trade_date: str | None = None
    tickers: list[str] = field(default_factory=list)
    next_run_at: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"run": self.run, "reason": self.reason, "period_key": self.period_key,
                "trade_date": self.trade_date, "tickers": list(self.tickers),
                "next_run_at": self.next_run_at}


def settings(profiles) -> dict[str, Any]:
    cfg = dict(profiles.risk.get("research_schedule") or {})
    cfg.setdefault("enabled", True)
    cfg.setdefault("period", "weekly")
    cfg.setdefault("calendar", "XNYS")
    cfg.setdefault("market_timezone", "America/New_York")
    cfg.setdefault("on_non_session_day", "previous_session")
    cfg.setdefault("tickers", [])
    cfg.setdefault("max_tickers_per_run", 3)
    cfg.setdefault("max_analyses_per_day", 6)
    cfg.setdefault("run_while_paused", True)
    cfg.setdefault("launchd", {"weekday": 5, "hour": 13, "minute": 30})
    return cfg


def market_now(cfg: dict[str, Any], now: datetime | None = None) -> datetime:
    """The market's clock. A laptop that travels must not move the schedule with it."""
    zone = ZoneInfo(str(cfg.get("market_timezone", "America/New_York")))
    return (now or datetime.now(UTC)).astimezone(zone)


def period_key(cfg: dict[str, Any], now: datetime | None = None) -> str:
    """Identifies one scheduled period. Two triggers inside it are the same execution."""
    stamp = market_now(cfg, now)
    if str(cfg.get("period", "weekly")) == "daily":
        return f"daily:{stamp.date().isoformat()}"
    year, week, _weekday = stamp.isocalendar()
    return f"weekly:{year}-W{week:02d}"


def system_zone():
    """This Mac's own time zone, as a named zone rather than today's offset.

    ``datetime.astimezone()`` with no argument attaches a fixed offset. Adding days to that across
    a daylight-saving change keeps the old offset, so for the week spanning the change the next
    scheduled run was shown an hour off. launchd fires by wall-clock time, and a named zone keeps
    the arithmetic in wall-clock time too.
    """
    name = os.environ.get("TZ", "").strip()
    if not name:
        link = os.path.realpath("/etc/localtime")
        name = link.split("zoneinfo/", 1)[1] if "zoneinfo/" in link else ""
    try:
        return ZoneInfo(name) if name else datetime.now().astimezone().tzinfo
    except (ZoneInfoNotFoundError, ValueError):
        return datetime.now().astimezone().tzinfo


def next_run_at(cfg: dict[str, Any], now: datetime | None = None,
                local_zone: ZoneInfo | None = None) -> str | None:
    """The next time the launchd job fires, in the Mac's own local time.

    Read from the same configuration the job is written from, so the screen cannot promise a
    time the installed job does not keep.
    """
    launchd = dict(cfg.get("launchd") or {})
    hour, minute = int(launchd.get("hour", 13)), int(launchd.get("minute", 30))
    weekday = launchd.get("weekday")
    local = (now or datetime.now(UTC)).astimezone(local_zone or system_zone())
    candidate = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if str(cfg.get("period", "weekly")) == "daily" or weekday is None:
        if candidate <= local:
            candidate += timedelta(days=1)
        return candidate.isoformat()
    ahead = (int(weekday) - candidate.isoweekday()) % 7
    candidate += timedelta(days=ahead)
    if candidate <= local:
        candidate += timedelta(days=7)
    return candidate.isoformat()


def session_for(cfg: dict[str, Any], now: datetime | None = None) -> tuple[str | None, str]:
    """The trade date this run should analyse, from the exchange calendar.

    A holiday or a weekend is not a failure and not a normal session either. Depending on
    configuration the run either steps back to the last completed session or declines, and it
    says which, because "nothing happened on Friday" is a question a person will ask.
    """
    stamp = market_now(cfg, now)
    try:
        import exchange_calendars

        calendar = exchange_calendars.get_calendar(str(cfg.get("calendar", "XNYS")))
    except Exception as exc:  # noqa: BLE001 - an unavailable calendar must not guess a session
        return None, f"exchange calendar unavailable ({type(exc).__name__}: {exc})"

    today = stamp.date()
    try:
        is_session = bool(calendar.is_session(today.isoformat()))
        # date_to_session accepts any date, unlike previous_session, which needs a session:
        # a holiday or a weekend is exactly the case this has to answer.
        previous = calendar.date_to_session(today.isoformat(), direction="previous")
        previous_date = previous.date() if hasattr(previous, "date") else previous
        if is_session:
            earlier = calendar.previous_session(today.isoformat())
            previous_date = earlier.date() if hasattr(earlier, "date") else earlier
            close = calendar.session_close(today.isoformat())
    except Exception as exc:  # noqa: BLE001
        return None, f"exchange calendar could not be read ({type(exc).__name__}: {exc})"

    if is_session:
        if stamp >= close.tz_convert(stamp.tzinfo):
            return today.isoformat(), f"{today.isoformat()} is a completed session"
        return str(previous_date), (f"{today.isoformat()} has not closed yet; "
                                    f"analysing the previous session {previous_date}")
    if str(cfg.get("on_non_session_day", "previous_session")) == "skip":
        return None, f"{today.isoformat()} is not a trading session for {cfg.get('calendar')}"
    return str(previous_date), (f"{today.isoformat()} is not a trading session; "
                                f"analysing the previous session {previous_date}")


def default_tickers(profiles, cfg: dict[str, Any]) -> list[str]:
    configured = [str(x).upper() for x in (cfg.get("tickers") or []) if str(x).strip()]
    if not configured:
        groups = profiles.investor_cfg("universe", "core", "etf_exposure_groups", default={}) or {}
        # One representative per exposure group, so a review never analyses overlapping funds.
        configured = [group[0] for group in groups.values() if group]
    return configured[:max(1, int(cfg.get("max_tickers_per_run", 3)))]


def analyses_today(jobs: list[dict[str, Any]], cfg: dict[str, Any],
                   now: datetime | None = None) -> int:
    """Every research job created today, whatever started it.

    A per-entry-point limit is not a budget: the subscription is one pool, so a manual run and
    a scheduled one draw from the same allowance and are counted the same way.
    """
    today = market_now(cfg, now).date()
    count = 0
    for job in jobs:
        if (job.get("task") or {}).get("kind") in ("refresh", "chat", "shortlist"):
            continue
        try:
            created = datetime.fromisoformat(str(job.get("created_at")))
        except (TypeError, ValueError):
            continue
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        if market_now(cfg, created).date() == today:
            count += 1
    return count


def decide(profiles, *, health_status: str, paused: bool, executed: dict[str, Any],
           jobs: list[dict[str, Any]], now: datetime | None = None,
           force: bool = False) -> Decision:
    """The whole scheduling decision, from configuration and durable state only."""
    cfg = settings(profiles)
    key = period_key(cfg, now)
    upcoming = next_run_at(cfg, now)

    def no(reason: str, trade_date: str | None = None) -> Decision:
        return Decision(False, reason, key, trade_date, [], upcoming)

    if not cfg.get("enabled", True):
        return no("scheduled research is disabled in risk_profile.research_schedule")
    if health_status in BLOCKING_STATUS:
        return no(f"system status is {health_status}")
    if paused and not cfg.get("run_while_paused", True):
        return no("system is paused and research_schedule.run_while_paused is false")
    if not force and key in (executed.get("periods") or {}):
        record = executed["periods"][key]
        return no(f"already executed for {key} at {record.get('at')}")

    trade_date, why = session_for(cfg, now)
    if trade_date is None:
        return no(why)

    limit = int(cfg.get("max_analyses_per_day", 6))
    used = analyses_today(jobs, cfg, now)
    if used >= limit:
        return no(f"daily research budget spent ({used}/{limit} analyses today)", trade_date)

    tickers = default_tickers(profiles, cfg)[:max(0, limit - used)]
    if not tickers:
        return no("no tickers are configured and no core exposure group has a representative",
                  trade_date)
    return Decision(True, why, key, trade_date, tickers, upcoming)


def claim(db, decision: Decision, *, now: datetime | None = None) -> bool:
    """Take ownership of a period, atomically, before any research is started.

    Recording the key first is what makes a repeat trigger or a catch-up run after a restart
    resume the same period instead of analysing it twice.
    """
    with db.transaction() as conn:
        record = db.read_meta(EXECUTIONS, {}) or {}
        periods = dict(record.get("periods") or {})
        if decision.period_key in periods:
            return False
        periods[decision.period_key] = {
            "at": (now or datetime.now(UTC)).isoformat(), "trade_date": decision.trade_date,
            "tickers": list(decision.tickers), "reason": decision.reason, "run_keys": [],
            "state": "CLAIMED",
        }
        record["periods"] = _trim(periods)
        db.write_meta(EXECUTIONS, record, conn)
    return True


def record_result(db, period: str, **changes: Any) -> dict[str, Any]:
    with db.transaction() as conn:
        record = db.read_meta(EXECUTIONS, {}) or {}
        periods = dict(record.get("periods") or {})
        entry = dict(periods.get(period) or {})
        entry.update(changes)
        periods[period] = entry
        record["periods"] = _trim(periods)
        record["last_period"] = period
        db.write_meta(EXECUTIONS, record, conn)
        return entry


def record_skip(db, decision: Decision, *, now: datetime | None = None) -> None:
    """Why a scheduled invocation did nothing, so the phone can answer that question."""
    with db.transaction() as conn:
        record = db.read_meta(EXECUTIONS, {}) or {}
        record["last_skip"] = {"at": (now or datetime.now(UTC)).isoformat(),
                               "period_key": decision.period_key, "reason": decision.reason}
        db.write_meta(EXECUTIONS, record, conn)


def _trim(periods: dict[str, Any]) -> dict[str, Any]:
    """Keep the record bounded; a year of weekly runs is more history than anyone reads."""
    keys = sorted(periods)[-MAX_RECORDS:]
    return {key: periods[key] for key in keys}
