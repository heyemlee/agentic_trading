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
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

#: Statuses that make research pointless or impossible regardless of configuration. PAUSED is
#: deliberately absent: the kill switch stops orders, and research is how the system notices
#: that conditions changed while it was paused.
BLOCKING_STATUS = ("HALTED", "CLAUDE_LIMIT_REACHED", "CONFIG_ERROR")

#: The document that records which periods have already been executed.
EXECUTIONS = "schedule_executions"
MAX_RECORDS = 60

#: What the schedule can do for one account. Reviewing what an account already holds comes
#: before looking at what it might buy, because the risk that is already on the books is the
#: risk a person is already carrying. A candidate study reads the configured universe; a
#: watchlist study reads the names the owner put on their Robinhood watchlists instead.
HOLDINGS, CANDIDATES, WATCHLIST, REVIEW = "holdings", "candidates", "watchlist", "review"
KIND_ORDER = (HOLDINGS, CANDIDATES, WATCHLIST)

#: The owner's own Robinhood accounts. The system observes them and never trades them, so
#: research about them is personal and read-only and is recorded under this bucket, and the only
#: study the schedule gives them is a review of what they hold.
PERSONAL = "advisory"
#: How advisor.fetch_snapshot marks a name that came from one of the owner's watchlists.
WATCHLIST_SOURCE = "watchlists:"


@dataclass(frozen=True)
class Task:
    """One account's one kind of scheduled research.

    ``scoped`` says whether the executed-period key names the account and the kind. A deployment
    that configures no accounts keeps the single unscoped review it has always had, on the key it
    has always had: installing this code must not make the current period look unexecuted and
    spend a week's allowance researching it again.
    """

    bucket: str
    kind: str
    period: str
    max_tickers: int
    scoped: bool = True
    #: The language a personal review writes its reports in; the trading buckets name none.
    language: str | None = None

    @property
    def label(self) -> str:
        return f"{self.bucket}:{self.kind}" if self.scoped else self.bucket


def tasks(cfg: dict[str, Any], *, bucket: str | None = None) -> list[Task]:
    """Every task the schedule would consider, holdings first, optionally for one account.

    ``research_schedule.accounts`` is a map of bucket to the kinds it schedules, each with its
    own period and its own cap::

        accounts:
          micro_live:
            holdings:   {period: daily,  max_tickers_per_run: 2}
            watchlist:  {period: weekly, max_tickers_per_run: 1}
          advisory:
            holdings:   {period: weekly, max_tickers_per_run: 2, language: zh}

    ``advisory`` is the owner's own accounts, reviewed read-only; any kind but ``holdings``
    under it is ignored, because nothing is ever bought for them. Within a kind the managed
    accounts come first: their exits are the risk this system carries.

    Absent, this answers the one review the deployment already runs, so the schedule behaves
    exactly as it did until an account is named deliberately.
    """
    accounts = cfg.get("accounts") or {}
    period = str(cfg.get("period", "weekly"))
    cap = int(cfg.get("max_tickers_per_run", 3))
    if not accounts:
        out = [Task(bucket=str(cfg.get("bucket", "core")), kind=REVIEW, period=period,
                    max_tickers=cap, scoped=False)]
    else:
        out = []
        for kind in KIND_ORDER:
            for name in sorted(accounts, key=lambda n: (n == PERSONAL, n)):
                block = (accounts.get(name) or {}).get(kind)
                if not isinstance(block, dict) or block.get("enabled") is False:
                    continue
                if name == PERSONAL and kind != HOLDINGS:
                    continue
                out.append(Task(bucket=name, kind=kind,
                                period=str(block.get("period", period)),
                                max_tickers=int(block.get("max_tickers_per_run", cap)),
                                language=(str(block.get("language", "zh"))
                                          if name == PERSONAL else None)))
    return [task for task in out if bucket is None or task.bucket == bucket]


def finest_period(cfg: dict[str, Any]) -> str:
    """The shortest period any task runs on.

    The operating system's job has to fire at least this often, or a daily review would only
    ever be considered once a week. Read by :mod:`agentic_trading.launchd` when it writes the
    job, and by :func:`next_run_at`, so the time a screen shows is the time the job keeps.
    """
    return "daily" if any(task.period == "daily" for task in tasks(cfg)) else str(
        cfg.get("period", "weekly"))


def ledger_for(profiles, bucket: str) -> Path:
    """The book that holds a bucket's positions. Real money keeps its own, separate ledger."""
    if bucket == "micro_live":
        configured = (profiles.risk.get("micro_live_execution") or {}).get(
            "ledger_file", "state/micro_live_portfolio.yaml")
    else:
        configured = (profiles.risk.get("portfolio_state") or {}).get("file", "state/portfolio.yaml")
    return Path(profiles.project_dir) / str(configured)


def subjects_for(profiles, task: Task, *, history: list[dict[str, Any]] = (),
                 snapshot: dict[str, Any] | None = None) -> list[str]:
    """What this task should analyse, in the order it should be analysed.

    A holdings review reads the account's own ledger, so an account is reviewed on what it
    actually holds rather than on a list of representative funds that may describe a different
    book entirely. Least recently researched first, and anything never researched before that:
    the rotation is what stops one holding from going unexamined for months while another is
    looked at every day. Ordering by event proximity is deliberately not done here -- that needs
    the event calendar and a verified event time, and guessing it would be worse than rotating.
    The owner's own accounts have no ledger here: their holdings are read from ``snapshot``, the
    last Robinhood reading (advisor.fetch_snapshot), and rotate the same way.

    A candidate study keeps the configured universe, which is what the single review has always
    used. The universe bounds what may be *researched*; what may then be *bought* is the
    authority's business, and a holding that has since left the universe must still be
    reviewable, which is why the two lists are read from different places. A watchlist study
    reads the owner's Robinhood watchlists in that reading instead, and rotates through them,
    because a watchlist can be longer than any one week's cap.

    Both set aside what is already held, before the cap, so the cap reaches the next names. A
    held name is the holdings review's to examine: studied again as a candidate it is the same
    research under another purpose -- the pipeline is told about the position either way -- run
    twice on one day, and able to come back as a BUY beside the review's REDUCE. A watchlist
    study also sets aside what the owner's own accounts hold, which their review examines. The
    single review a deployment runs until it names an account is not a candidate study; it reads
    no book and sets nothing aside, as before.
    """
    from .portfolio import PortfolioError, load_portfolio

    cfg = settings(profiles)
    if task.kind not in (HOLDINGS, CANDIDATES, WATCHLIST):
        return default_tickers(profiles, cfg)
    if task.bucket == PERSONAL:
        return _rotation(personal_holdings(snapshot), history, task.bucket) if task.kind == HOLDINGS else []
    try:
        ledger = load_portfolio(ledger_for(profiles, task.bucket))
    except (PortfolioError, OSError, ValueError):
        # A book that cannot be read schedules no holdings review, so a candidate study has
        # nothing to repeat and sets nothing aside. Either way the reason belongs to whatever
        # reads the book for real, not to a decision about when to run.
        if task.kind == CANDIDATES:
            return default_tickers(profiles, cfg)
        if task.kind == WATCHLIST:
            owned = set(personal_holdings(snapshot))
            return _rotation([s for s in watchlist_names(snapshot) if s not in owned], history, task.bucket)
        return []
    held = [position.symbol for position in ledger.positions if position.bucket == task.bucket]
    if task.kind == CANDIDATES:
        return default_tickers(profiles, cfg, exclude=held)
    if task.kind == WATCHLIST:
        owned = {symbol.upper() for symbol in held} | set(personal_holdings(snapshot))
        return _rotation([s for s in watchlist_names(snapshot) if s not in owned], history, task.bucket)
    return _rotation(held, history, task.bucket)


def _rotation(symbols: Iterable[str], history: Iterable[dict[str, Any]], bucket: str) -> list[str]:
    """Never researched first, then the longest since this account's research last looked."""
    seen: dict[str, str] = {}
    for row in history or ():
        symbol = str(row.get("ticker") or "").upper()
        if row.get("bucket") == bucket and symbol:
            seen.setdefault(symbol, str(row.get("created_at") or ""))
    return sorted(dict.fromkeys(symbols), key=lambda symbol: (symbol in seen, seen.get(symbol, ""), symbol))


def personal_holdings(snapshot: dict[str, Any] | None) -> dict[str, str]:
    """Each symbol the owner's own accounts hold, with the first account that holds it.

    A review is bound to one observed account, the one whose context it is read in; a symbol
    held in two accounts is reviewed once, in the first the reading lists.
    """
    held: dict[str, str] = {}
    for account in (snapshot or {}).get("accounts") or []:
        if not isinstance(account, dict) or not account.get("id"):
            continue
        for position in account.get("positions") or []:
            symbol = str((position or {}).get("symbol") or "").upper()
            try:
                quantity = float((position or {}).get("quantity") or 0)
            except (TypeError, ValueError):
                quantity = 0.0
            if symbol and quantity > 0:
                held.setdefault(symbol, str(account["id"]))
    return held


def watchlist_names(snapshot: dict[str, Any] | None) -> list[str]:
    """The names on the owner's Robinhood watchlists, as the last reading saw them.

    Screener results and the profile's own ETF groups travel in the same list, marked with
    their source; only a name the owner put on a watchlist is theirs.
    """
    out: list[str] = []
    for item in (snapshot or {}).get("candidates") or []:
        if not isinstance(item, dict):
            continue
        if any(str(source).startswith(WATCHLIST_SOURCE) for source in item.get("sources") or []):
            symbol = str(item.get("symbol") or "").upper()
            if symbol:
                out.append(symbol)
    return list(dict.fromkeys(out))


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
    #: Which account and which kind of research this decision is about.
    bucket: str = "core"
    kind: str = REVIEW

    def as_dict(self) -> dict[str, Any]:
        return {"run": self.run, "reason": self.reason, "period_key": self.period_key,
                "trade_date": self.trade_date, "tickers": list(self.tickers),
                "next_run_at": self.next_run_at, "bucket": self.bucket, "kind": self.kind}


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
    # Per-account scheduling, off until an account is named: see tasks().
    cfg.setdefault("accounts", {})
    # How much of the day's allowance a candidate study may not touch, so that looking for
    # something to buy can never leave a holding that needs an exit unexamined.
    cfg.setdefault("reserve_for_exits", 0)
    return cfg


def market_now(cfg: dict[str, Any], now: datetime | None = None) -> datetime:
    """The market's clock. A laptop that travels must not move the schedule with it."""
    zone = ZoneInfo(str(cfg.get("market_timezone", "America/New_York")))
    return (now or datetime.now(UTC)).astimezone(zone)


def period_key(cfg: dict[str, Any], now: datetime | None = None,
               task: Task | None = None) -> str:
    """Identifies one scheduled period. Two triggers inside it are the same execution.

    A scoped task names its account and its kind, so one account's review can never mark
    another's as already run, and a holdings review never marks a candidate study as run.
    """
    period = task.period if task is not None else str(cfg.get("period", "weekly"))
    stamp = market_now(cfg, now)
    if period == "daily":
        moment = f"daily:{stamp.date().isoformat()}"
    else:
        year, week, _weekday = stamp.isocalendar()
        moment = f"weekly:{year}-W{week:02d}"
    if task is None or not task.scoped:
        return moment
    head, _, rest = moment.partition(":")
    return f"{head}:{task.bucket}:{task.kind}:{rest}"


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
    if finest_period(cfg) == "daily" or weekday is None:
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


def default_tickers(profiles, cfg: dict[str, Any], *, exclude: Iterable[str] = ()) -> list[str]:
    """The configured universe in its own order, less ``exclude``, cut to the run's cap.

    What is excluded is taken out before the cut, so it makes room for the next member of the
    universe rather than shortening the run.
    """
    configured = [str(x).upper() for x in (cfg.get("tickers") or []) if str(x).strip()]
    if not configured:
        groups = profiles.investor_cfg("universe", "core", "etf_exposure_groups", default={}) or {}
        # One representative per exposure group, so a review never analyses overlapping funds.
        configured = [group[0] for group in groups.values() if group]
    skip = {str(symbol).upper() for symbol in exclude}
    kept = [ticker for ticker in configured if str(ticker).upper() not in skip]
    return kept[:max(1, int(cfg.get("max_tickers_per_run", 3)))]


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
           force: bool = False, task: Task | None = None,
           subjects: list[str] | None = None) -> Decision:
    """The whole scheduling decision, from configuration and durable state only.

    ``task`` says which account and which kind of research this is about, and defaults to the
    one review a deployment runs until it configures accounts. ``subjects`` is what that task
    would analyse; absent, the configured universe, which is what the single review has always
    used.
    """
    cfg = settings(profiles)
    task = task or tasks(cfg)[0]
    key = period_key(cfg, now, task)
    upcoming = next_run_at(cfg, now)

    def no(reason: str, trade_date: str | None = None) -> Decision:
        return Decision(False, reason, key, trade_date, [], upcoming, task.bucket, task.kind)

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
    if not force and task.period == "daily":
        # A daily period is keyed by the calendar date, and a day the market is shut analyses
        # the session before it. So Saturday and Sunday each had a period of their own that
        # nothing had executed, and each studied Friday's session again. What a daily task may
        # do once is study a session, whichever days it is asked on.
        mine = key.rsplit(":", 1)[0]
        for other, record in (executed.get("periods") or {}).items():
            if other.rsplit(":", 1)[0] == mine and record.get("trade_date") == trade_date:
                return no(f"session {trade_date} was already studied for this task "
                          f"({other}, at {record.get('at')})", trade_date)

    limit = int(cfg.get("max_analyses_per_day", 6))
    used = analyses_today(jobs, cfg, now)
    # Looking for something to buy may not spend the capacity an exit review needs. A holdings
    # review of a managed account is how an exit is found, so it alone draws on the whole
    # allowance; a review of the owner's own accounts can find no exit this system would take.
    reserved = int(cfg.get("reserve_for_exits", 0)) if (
        task.kind in (CANDIDATES, WATCHLIST) or (task.kind == HOLDINGS and task.bucket == PERSONAL)) else 0
    available = limit - reserved
    if used >= available:
        spent = f"{used}/{limit} analyses today"
        return no(f"daily research budget spent ({spent})" if not reserved else
                  f"only {max(0, limit - used)} of the day's allowance is left and "
                  f"{reserved} is reserved for exit reviews ({spent})", trade_date)

    if subjects is None:
        subjects = default_tickers(profiles, cfg)
    tickers = list(subjects)[:max(0, min(task.max_tickers, available - used))]
    if not tickers:
        if task.kind == HOLDINGS and task.bucket == PERSONAL:
            return no("your own Robinhood accounts hold nothing to review in the last reading "
                      "of them", trade_date)
        if task.kind == HOLDINGS:
            return no(f"{task.bucket} holds nothing to review", trade_date)
        if task.kind == WATCHLIST:
            return no("no name is left on your Robinhood watchlists to study: none has been read "
                      "yet, or every one is already held", trade_date)
        if task.kind == CANDIDATES and not subjects and default_tickers(profiles, cfg):
            # The universe names something and none of it is left: the account holds all of it.
            return no(f"{task.bucket} already holds everything in the universe", trade_date)
        return no("no tickers are configured and no core exposure group has a representative",
                  trade_date)
    return Decision(True, why, key, trade_date, tickers, upcoming, task.bucket, task.kind)


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
