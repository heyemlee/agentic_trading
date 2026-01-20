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
from agentic_trading.state_db import StateDB
from agentic_trading.triggers import scheduled_review
from tests.conftest import ROOT

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
    shutil.copy(ROOT / "config" / "examples" / "portfolio.example.yaml", tmp_path / "state" / "portfolio.yaml")
    shutil.copy(ROOT / "config" / "examples" / "micro_live_portfolio.example.yaml",
                tmp_path / "state" / "micro_live_portfolio.yaml")
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
    monkeypatch.setattr(service, "health", lambda: {"status": "HALTED", "paused": False})
    started = []
    monkeypatch.setattr(service, "start_research", lambda *a, **k: started.append(a) or {})

    result = scheduled_review(service, now=FRIDAY_AFTER_CLOSE)
    assert result["skipped"] is True and "HALTED" in result["reason"]
    assert started == []
    assert "HALTED" in db.read_meta(schedule.EXECUTIONS, {})["last_skip"]["reason"]


def test_a_scheduled_run_claims_its_period_then_records_the_runs_it_started(sandbox, monkeypatch):
    service, db = sandbox
    monkeypatch.setattr(service, "health", lambda: {"status": "READY", "paused": False})
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
    monkeypatch.setattr(service, "health", lambda: {"status": "READY", "paused": False})
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
