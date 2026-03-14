"""Scheduling decisions: the calendar, the executed period, the budget and the kill switch.

Offline. No model call, no broker, no launchd. Every case fixes ``now`` so a holiday or a
half-day is exercised deliberately rather than whenever the suite happens to run.
"""
from __future__ import annotations

import shutil
from datetime import UTC, datetime, timedelta

import pytest

from agentic_trading import schedule
from agentic_trading.profile import load_profiles
from agentic_trading.service import Service
from agentic_trading.service.shapes import Health
from agentic_trading.state_db import StateDB
from agentic_trading.triggers import scheduled_review
from tests.conftest import ROOT, copy_example_ledger


def _health(status: str) -> Health:
    """A stub health answer carrying what the real one always carries.

    The scheduler reads status and paused; the shape it reads them from also always says why
    and when, so a stub that omitted those would be exercising a response no endpoint can make.
    """
    return Health(status=status, detail=f"stubbed {status}", paused=False,
                  checked_at="2026-09-10T00:00:00+00:00")

FRIDAY_AFTER_CLOSE = datetime(2026, 9, 4, 21, 0, tzinfo=UTC)     # 17:00 New York
FRIDAY_DURING_SESSION = datetime(2026, 9, 4, 16, 0, tzinfo=UTC)  # 12:00 New York
SATURDAY = datetime(2026, 9, 5, 21, 0, tzinfo=UTC)
INDEPENDENCE_DAY = datetime(2026, 7, 3, 21, 0, tzinfo=UTC)       # observed holiday, a Friday


@pytest.fixture
def sandbox(tmp_path):
    """A Service and a state database whose every path is inside a temp directory."""
    cfg = tmp_path / "config"
    shutil.copytree(ROOT / "config", cfg)
    (tmp_path / "state").mkdir()
    (tmp_path / "runs").mkdir()
    copy_example_ledger("portfolio.example.yaml", tmp_path / "state" / "portfolio.yaml")
    copy_example_ledger("micro_live_portfolio.example.yaml", tmp_path / "state" / "micro_live_portfolio.yaml")
    return Service(cfg), StateDB(tmp_path / "state" / "proposals.json")


@pytest.fixture
def cfg(config_dir):
    return schedule.settings(load_profiles(config_dir))


def decide(profiles, **kwargs):
    base = {"health_status": "READY", "paused": False, "executed": {}, "jobs": [],
            "now": FRIDAY_AFTER_CLOSE}
    return schedule.decide(profiles, **{**base, **kwargs})


def test_the_session_comes_from_the_exchange_calendar_not_the_laptop(cfg):
    assert schedule.session_for(cfg, FRIDAY_AFTER_CLOSE)[0] == "2026-09-04"
    # a session that has not closed cannot be analysed as if it had
    assert schedule.session_for(cfg, FRIDAY_DURING_SESSION)[0] == "2026-09-03"
    # a weekend and a holiday step back to the last completed session
    assert schedule.session_for(cfg, SATURDAY)[0] == "2026-09-04"
    assert schedule.session_for(cfg, INDEPENDENCE_DAY)[0] == "2026-07-02"
    holiday = schedule.session_for(cfg, INDEPENDENCE_DAY)[1]
    assert "not a trading session" in holiday


def test_a_holiday_can_be_configured_to_skip_entirely(cfg):
    trade_date, reason = schedule.session_for({**cfg, "on_non_session_day": "skip"}, SATURDAY)
    assert trade_date is None and "not a trading session" in reason


def test_the_period_key_is_the_markets_week_not_the_machines(cfg):
    # 23:00 in New York on Friday is already Saturday in UTC; the schedule must not roll over
    late_friday = datetime(2026, 9, 5, 3, 0, tzinfo=UTC)
    assert schedule.period_key(cfg, FRIDAY_AFTER_CLOSE) == schedule.period_key(cfg, late_friday)
    next_week = schedule.period_key(cfg, FRIDAY_AFTER_CLOSE + timedelta(days=7))
    assert next_week != schedule.period_key(cfg, FRIDAY_AFTER_CLOSE)


def test_a_repeat_trigger_inside_an_executed_period_does_not_research_twice(sandbox, profiles):
    service, db = sandbox
    decision = decide(service.profiles())
    assert decision.run is True
    assert schedule.claim(db, decision) is True
    assert schedule.claim(db, decision) is False        # a second invocation loses the race

    again = decide(service.profiles(), executed=db.read_meta(schedule.EXECUTIONS, {}))
    assert again.run is False and "already executed" in again.reason


def test_a_catch_up_after_a_restart_resumes_the_same_period(sandbox):
    service, db = sandbox
    first = decide(service.profiles())
    schedule.claim(db, first)
    # the machine restarts hours later, inside the same week
    later = decide(service.profiles(), now=FRIDAY_AFTER_CLOSE + timedelta(hours=6),
                   executed=db.read_meta(schedule.EXECUTIONS, {}))
    assert later.period_key == first.period_key
    assert later.run is False


def test_the_daily_budget_counts_every_entry_point(sandbox):
    service, _db = sandbox
    profiles = service.profiles()
    budget = int(schedule.settings(profiles)["max_analyses_per_day"])
    manual = [{"created_at": FRIDAY_AFTER_CLOSE.isoformat(), "trigger": "manual"}] * budget
    spent = decide(profiles, jobs=manual)
    assert spent.run is False and "budget spent" in spent.reason

    partial = decide(profiles, jobs=manual[:budget - 1])
    assert partial.run is True and len(partial.tickers) == 1   # only the remaining allowance


def test_pausing_stops_orders_and_not_research(sandbox):
    """The kill switch and the research schedule answer different questions."""
    service, _db = sandbox
    profiles = service.profiles()
    assert decide(profiles, paused=True).run is True

    profiles.risk["research_schedule"]["run_while_paused"] = False
    withheld = decide(profiles, paused=True)
    assert withheld.run is False and "paused" in withheld.reason


def test_an_unusable_system_state_skips_with_the_reason(sandbox):
    service, _db = sandbox
    for status in ("HALTED", "CLAUDE_LIMIT_REACHED", "CONFIG_ERROR"):
        decision = decide(service.profiles(), health_status=status)
        assert decision.run is False and status in decision.reason


def test_the_next_run_keeps_wall_clock_time_across_a_daylight_saving_change(monkeypatch):
    """With no zone passed, as the scheduler calls it, the time used a fixed offset: for the week
    spanning the change the screen promised a run an hour away from when launchd fires it."""
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    cfg = {"period": "weekly", "launchd": {"weekday": 5, "hour": 13, "minute": 30}}
    friday_after_the_run = datetime(2026, 10, 30, 21, 0, tzinfo=UTC)      # 14:00 PDT
    # the next Friday is after the clocks go back on 1 November
    assert schedule.next_run_at(cfg, friday_after_the_run) == "2026-11-06T13:30:00-08:00"


def test_next_run_is_read_from_the_same_configuration_the_job_is_written_from(cfg):
    from zoneinfo import ZoneInfo

    from agentic_trading.launchd import research_interval

    pacific = ZoneInfo("America/Los_Angeles")
    upcoming = datetime.fromisoformat(
        schedule.next_run_at(cfg, FRIDAY_AFTER_CLOSE, local_zone=pacific))
    interval = research_interval(ROOT)
    assert upcoming.isoweekday() == interval["Weekday"]
    assert (upcoming.hour, upcoming.minute) == (interval["Hour"], interval["Minute"])
    assert upcoming > FRIDAY_AFTER_CLOSE.astimezone(pacific)


def test_a_skipped_run_records_why_and_starts_nothing(sandbox, monkeypatch):
    service, db = sandbox
    monkeypatch.setattr(service, "health", lambda: _health("HALTED"))
    started = []
    monkeypatch.setattr(service, "start_research", lambda *a, **k: started.append(a) or {})

    result = scheduled_review(service, now=FRIDAY_AFTER_CLOSE)
    assert result["skipped"] is True and "HALTED" in result["reason"]
    assert started == []
    assert "HALTED" in db.read_meta(schedule.EXECUTIONS, {})["last_skip"]["reason"]


def test_a_scheduled_run_claims_its_period_then_records_the_runs_it_started(sandbox, monkeypatch):
    service, db = sandbox
    monkeypatch.setattr(service, "health", lambda: _health("READY"))
    monkeypatch.setattr(service, "start_research",
                        lambda ticker, **kw: {"run_key": f"key-{ticker}", "ticker": ticker,
                                              "trade_date": "2026-09-03"})

    result = scheduled_review(service, now=FRIDAY_AFTER_CLOSE)
    assert result["skipped"] is False and len(result["started"]) >= 1
    record = db.read_meta(schedule.EXECUTIONS, {})["periods"][result["schedule"]["period_key"]]
    assert record["state"] == "STARTED"
    assert record["run_keys"] == [run["run_key"] for run in result["started"]]

    repeat = scheduled_review(service, now=FRIDAY_AFTER_CLOSE)
    assert repeat["skipped"] is True and repeat["started"] == []


def test_the_run_anchors_to_a_session_with_data_not_to_the_calendar_date(sandbox, monkeypatch):
    """A session closes hours before its prices are published.

    Forcing the calendar's date on the run produced an UNKNOWN market regime, which lowers
    confidence and prefers NO_TRADE — a run that looks healthy while reasoning from evidence it
    does not have. The calendar decides whether to run; the data layer decides what to read.
    """
    service, db = sandbox
    monkeypatch.setattr(service, "health", lambda: _health("READY"))
    passed = []

    def start_research(ticker, **kwargs):
        passed.append(kwargs)
        # what the service resolves to, which is a session earlier than the calendar's
        return {"run_key": f"key-{ticker}", "ticker": ticker, "trade_date": "2026-09-03"}

    monkeypatch.setattr(service, "start_research", start_research)
    result = scheduled_review(service, now=FRIDAY_AFTER_CLOSE)

    assert result["schedule"]["trade_date"] == "2026-09-04"      # the calendar's session
    assert all(kwargs.get("trade_date") is None for kwargs in passed)
    record = db.read_meta(schedule.EXECUTIONS, {})["periods"][result["schedule"]["period_key"]]
    assert record["calendar_session"] == "2026-09-04"
    assert record["analysed_sessions"] == ["2026-09-03"]         # what was actually read


# ------------------------------------------------------------------ per-account scheduling
def accounts_config(config_dir, accounts, **extra):
    """Write a per-account schedule into a copied config, leaving everything else as it was."""
    import yaml

    path = config_dir / "risk_profile.yaml"
    doc = yaml.safe_load(path.read_text())
    doc["research_schedule"]["accounts"] = accounts
    doc["research_schedule"].update(extra)
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return load_profiles(config_dir)


def micro_ledger(state_dir, *symbols):
    """A real-money ledger holding these symbols, dated a session the service accepts."""
    import yaml

    from tests.conftest import recent_session

    (state_dir / "micro_live_portfolio.yaml").write_text(yaml.safe_dump({
        "as_of": recent_session(), "currency": "USD", "buckets": {"micro_live": {"cash_usd": 40.0}},
        "positions": [{"symbol": symbol, "bucket": "micro_live", "quantity": 0.2,
                       "cost_basis_per_share": 100.0} for symbol in symbols],
        "recent_opens": []}), encoding="utf-8")


def test_without_accounts_the_plan_and_the_key_are_what_they_always_were(cfg):
    """Installing per-account scheduling must not make this week look unexecuted.

    The deployed schedule is one weekly review. If its executed-period key changed shape, the
    current period would read as never run and the next trigger would spend a week's allowance
    researching it again.
    """
    plan = schedule.tasks(cfg)
    assert len(plan) == 1
    assert (plan[0].bucket, plan[0].kind, plan[0].scoped) == ("core", schedule.REVIEW, False)
    assert schedule.period_key(cfg, FRIDAY_AFTER_CLOSE, plan[0]) == schedule.period_key(
        cfg, FRIDAY_AFTER_CLOSE)
    assert schedule.finest_period(cfg) == cfg["period"]


def test_holdings_come_before_candidates_and_each_account_keeps_its_own_period(cfg):
    scheduled = {**cfg, "accounts": {
        "micro_live": {"holdings": {"period": "daily", "max_tickers_per_run": 2},
                       "candidates": {"period": "weekly"}},
        "core": {"holdings": {"period": "weekly"}}}}
    plan = schedule.tasks(scheduled)
    assert [(task.bucket, task.kind) for task in plan] == [
        ("core", schedule.HOLDINGS), ("micro_live", schedule.HOLDINGS),
        ("micro_live", schedule.CANDIDATES)]
    assert plan[1].period == "daily" and plan[1].max_tickers == 2
    assert schedule.finest_period(scheduled) == "daily"

    keys = {schedule.period_key(scheduled, FRIDAY_AFTER_CLOSE, task) for task in plan}
    assert len(keys) == 3                      # no account can mark another's period as run
    assert "daily:micro_live:holdings:" in " ".join(keys)
    assert "weekly:micro_live:candidates:" in " ".join(keys)
    # One account narrows to its own tasks, which is what ct-trigger --bucket asks for.
    assert {task.bucket for task in schedule.tasks(scheduled, bucket="micro_live")} == {"micro_live"}
    # A kind can be turned off without removing it.
    off = {**scheduled, "accounts": {"micro_live": {"holdings": {"enabled": False},
                                                     "candidates": {}}}}
    assert [task.kind for task in schedule.tasks(off)] == [schedule.CANDIDATES]


def test_a_holdings_review_reads_the_account_ledger_least_recently_researched_first(tmp_path):
    cfg_dir = tmp_path / "config"
    shutil.copytree(ROOT / "config", cfg_dir)
    state = tmp_path / "state"
    state.mkdir()
    copy_example_ledger("portfolio.example.yaml", state / "portfolio.yaml")
    micro_ledger(state, "VTI", "BND", "SGOV")
    profiles = accounts_config(cfg_dir, {"micro_live": {"holdings": {"period": "daily"}}})
    task = schedule.tasks(schedule.settings(profiles))[0]

    history = [{"ticker": "BND", "bucket": "micro_live", "created_at": "2026-09-16T10:00:00+00:00"},
               {"ticker": "VTI", "bucket": "micro_live", "created_at": "2026-09-10T10:00:00+00:00"},
               # Another book's research says nothing about this one's rotation.
               {"ticker": "SGOV", "bucket": "core", "created_at": "2026-09-18T10:00:00+00:00"}]
    # Never researched first, then the longest since it was last looked at.
    assert schedule.subjects_for(profiles, task, history=history) == ["SGOV", "VTI", "BND"]
    # A candidate study keeps the configured universe rather than the book.
    candidates = schedule.Task(bucket="micro_live", kind=schedule.CANDIDATES, period="weekly",
                               max_tickers=3)
    assert "SGOV" not in schedule.subjects_for(profiles, candidates, history=history)[:1]


def test_a_holdings_review_of_an_empty_book_says_so_rather_than_inventing_a_subject(tmp_path):
    cfg_dir = tmp_path / "config"
    shutil.copytree(ROOT / "config", cfg_dir)
    state = tmp_path / "state"
    state.mkdir()
    copy_example_ledger("portfolio.example.yaml", state / "portfolio.yaml")
    copy_example_ledger("micro_live_portfolio.example.yaml", state / "micro_live_portfolio.yaml")
    profiles = accounts_config(cfg_dir, {"micro_live": {"holdings": {"period": "daily"}}})
    task = schedule.tasks(schedule.settings(profiles))[0]
    assert schedule.subjects_for(profiles, task) == []
    decision = decide(profiles, task=task, subjects=[])
    assert decision.run is False and "holds nothing to review" in decision.reason
    assert decision.bucket == "micro_live" and decision.kind == schedule.HOLDINGS


def test_a_candidate_study_may_not_spend_the_capacity_reserved_for_exits(profiles):
    """Looking for something to buy must never leave a holding that needs an exit unexamined."""
    # load_profiles reads the files on every call and caches nothing, and this fixture is
    # per-test, so narrowing the allowance here cannot reach another test or the files.
    profiles.risk["research_schedule"].update(max_analyses_per_day=3, reserve_for_exits=1)
    today = FRIDAY_AFTER_CLOSE.isoformat()
    spent = [{"created_at": today, "state": "COMPLETE"} for _ in range(2)]
    holdings = schedule.Task(bucket="micro_live", kind=schedule.HOLDINGS, period="daily", max_tickers=2)
    candidates = schedule.Task(bucket="micro_live", kind=schedule.CANDIDATES, period="weekly", max_tickers=2)

    # Two of three spent: the exit review may still run, the candidate study may not.
    assert decide(profiles, task=holdings, subjects=["VTI"], jobs=spent).run is True
    refused = decide(profiles, task=candidates, subjects=["VTI"], jobs=spent)
    assert refused.run is False and "reserved for exit reviews" in refused.reason


def test_the_job_fires_as_often_as_the_shortest_period_any_account_asks_for(tmp_path):
    """A daily review the operating system only invoked on Fridays is a weekly review."""
    from agentic_trading import launchd

    cfg_dir = tmp_path / "config"
    shutil.copytree(ROOT / "config", cfg_dir)
    weekly = schedule.settings(load_profiles(cfg_dir))
    assert "Weekday" in launchd.research_interval(tmp_path)
    assert "-W" in schedule.period_key(weekly)

    accounts_config(cfg_dir, {"micro_live": {"holdings": {"period": "daily"}}})
    assert "Weekday" not in launchd.research_interval(tmp_path)
    daily = schedule.settings(load_profiles(cfg_dir))
    # The screen reads the same answer the job was written from.
    assert schedule.next_run_at(daily, FRIDAY_AFTER_CLOSE) != schedule.next_run_at(
        weekly, FRIDAY_AFTER_CLOSE)


def test_every_configured_task_runs_and_the_two_share_one_days_allowance(
        tmp_path, monkeypatch, offline_market_reads):
    """Holdings first, candidates after, and the day's count re-read between them.

    Deciding both against the count taken before either started would let them together spend
    more than the subscription allows, which no amount of concurrency can fix.
    """
    from agentic_trading.research_jobs import ResearchJobManager

    monkeypatch.setattr(ResearchJobManager, "dispatch", lambda self: [])
    cfg_dir = tmp_path / "config"
    shutil.copytree(ROOT / "config", cfg_dir)
    state = tmp_path / "state"
    state.mkdir()
    (tmp_path / "runs").mkdir()
    copy_example_ledger("portfolio.example.yaml", state / "portfolio.yaml")
    micro_ledger(state, "VTI", "BND")
    accounts_config(cfg_dir, {"micro_live": {"holdings": {"period": "daily", "max_tickers_per_run": 2},
                                              "candidates": {"period": "daily", "max_tickers_per_run": 2}}},
                    max_analyses_per_day=3, reserve_for_exits=1)
    service = Service(cfg_dir)
    db = StateDB(state / "proposals.json")

    result = scheduled_review(service)
    assert [task["schedule"]["kind"] for task in result["tasks"]] == [
        schedule.HOLDINGS, schedule.CANDIDATES]
    holdings, candidates = result["tasks"]
    # The book's two holdings are reviewed, as holdings, against the whole allowance.
    assert {run["ticker"] for run in holdings["started"]} == {"VTI", "BND"}
    assert {run["purpose"] for run in holdings["started"]} == {"review_holding"}
    assert {run["bucket"] for run in holdings["started"]} == {"micro_live"}
    # Two of three now spent, one reserved for exits: the candidate study is refused, and says so.
    assert candidates["started"] == [] and "reserved for exit reviews" in candidates["reason"]
    assert len(result["started"]) == 2 and result["skipped"] is False

    # Each task claimed its own period, so neither marked the other as already run.
    periods = db.read_meta(schedule.EXECUTIONS, {})["periods"]
    assert holdings["schedule"]["period_key"] in periods
    assert periods[holdings["schedule"]["period_key"]]["kind"] == schedule.HOLDINGS
    assert candidates["schedule"]["period_key"] not in periods

    # A repeat inside the same period starts nothing more.
    again = scheduled_review(service)
    assert again["started"] == [] and "already executed" in again["tasks"][0]["reason"]


def test_a_held_name_is_reviewed_once_as_a_holding_and_not_studied_again_as_a_candidate(
        tmp_path, monkeypatch, offline_market_reads):
    """Holdings and candidates in one run: the candidate study sets aside what the book holds.

    It read the universe without the book, so an account holding VTI and BND had both reviewed
    as holdings and then asked for again as candidates. The queue folded each second request
    into the first only while that one was still active, which spent the candidate study's cap
    on names it never studied; once the first had finished, the same position was researched
    twice on one day under two purposes, and could come back as a REDUCE and a BUY.
    """
    from agentic_trading.research_jobs import ResearchJobManager

    monkeypatch.setattr(ResearchJobManager, "dispatch", lambda self: [])
    cfg_dir = tmp_path / "config"
    shutil.copytree(ROOT / "config", cfg_dir)
    state = tmp_path / "state"
    state.mkdir()
    (tmp_path / "runs").mkdir()
    copy_example_ledger("portfolio.example.yaml", state / "portfolio.yaml")
    micro_ledger(state, "VTI", "BND")
    # The universe, the caps and the allowance are named here: the copied config is the live
    # one, and the console can change any of them.
    accounts_config(cfg_dir, {"micro_live": {
        "holdings": {"period": "weekly", "max_tickers_per_run": 3},
        "candidates": {"period": "weekly", "max_tickers_per_run": 3}}},
        tickers=["VTI", "VXUS", "BND", "VGIT", "SGOV"], max_tickers_per_run=3,
        max_analyses_per_day=6, reserve_for_exits=0)

    result = scheduled_review(Service(cfg_dir))
    holdings, candidates = result["tasks"]
    assert sorted(run["ticker"] for run in holdings["started"]) == ["BND", "VTI"]
    assert {run["purpose"] for run in holdings["started"]} == {"review_holding"}
    # The universe keeps its order, and the cap reaches past what is held instead of spending
    # itself on names the holdings review has just asked for.
    assert [run["ticker"] for run in candidates["started"]] == ["VXUS", "VGIT", "SGOV"]
    assert {run["purpose"] for run in candidates["started"]} == {"candidate"}
    # Five runs of their own: none is a request folded into another task's.
    assert not any(run["deduplicated"] for run in result["started"])
    assert len({run["run_key"] for run in result["started"]}) == 5


def test_only_a_candidate_study_sets_the_book_aside_and_an_empty_one_says_why(tmp_path):
    """The single review keeps every name, as it always has; a study left with none says so.

    The single review is what a deployment runs until it names an account, and it reads no
    book, so a held name stays in it. A candidate study left with nothing must not blame a
    configuration that names plenty.
    """
    cfg_dir = tmp_path / "config"
    shutil.copytree(ROOT / "config", cfg_dir)
    state = tmp_path / "state"
    state.mkdir()
    copy_example_ledger("portfolio.example.yaml", state / "portfolio.yaml")
    micro_ledger(state, "VTI", "BND")
    profiles = accounts_config(cfg_dir, {}, tickers=["VTI", "BND"], max_tickers_per_run=3,
                               max_analyses_per_day=6, reserve_for_exits=0)

    single = schedule.Task(bucket="micro_live", kind=schedule.REVIEW, period="weekly",
                           max_tickers=3, scoped=False)
    assert schedule.subjects_for(profiles, single) == ["VTI", "BND"]

    candidates = schedule.Task(bucket="micro_live", kind=schedule.CANDIDATES, period="weekly",
                               max_tickers=3)
    assert schedule.subjects_for(profiles, candidates) == []
    decision = decide(profiles, task=candidates, subjects=[])
    assert decision.run is False and "already holds everything" in decision.reason
