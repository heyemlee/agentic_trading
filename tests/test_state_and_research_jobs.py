"""Transactional state and durable research queue regression tests."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from conservative_trading.research_jobs import ResearchJobManager, ResearchJobStore


def create_job(store, tmp_path, name="one", **overrides):
    values = {
        "run_key": name, "ticker": "VTI", "bucket": "core", "trade_date": "2026-09-02",
        "instrument_type": "etf", "trigger": "manual", "log_path": tmp_path / f"{name}.log",
        "timeout_seconds": 120, "max_attempts": 2,
    }
    values.update(overrides)
    return store.create(**values)


def manager(tmp_path, **settings):
    config = tmp_path / "config"
    config.mkdir(exist_ok=True)
    return ResearchJobManager(
        state_source=tmp_path / "proposals.json", project_dir=tmp_path, config_dir=config,
        settings={"max_concurrent": 2, "timeout_seconds": 120, "max_attempts": 2, **settings},
    )


def test_identical_active_research_is_deduplicated(tmp_path):
    store = ResearchJobStore(tmp_path / "proposals.json")
    first, created = create_job(store, tmp_path, "first")
    second, created_again = create_job(store, tmp_path, "second")
    assert created is True and created_again is False
    assert second["run_key"] == first["run_key"]
    assert len(store.list()) == 1


def test_research_concurrency_claim_is_atomic(tmp_path):
    store = ResearchJobStore(tmp_path / "proposals.json")
    create_job(store, tmp_path, "one")
    create_job(store, tmp_path, "two", ticker="BND")
    create_job(store, tmp_path, "three", ticker="VOO")
    claimed = store.claim(2)
    assert {job["run_key"] for job in claimed} == {"one", "two"}
    assert len(store.list(states=("RUNNING",))) == 2
    assert len(store.claim(2)) == 0
    assert store.get("three")["state"] == "QUEUED"


def test_dispatch_persists_worker_pid_and_uses_run_key(tmp_path, monkeypatch):
    mgr = manager(tmp_path, max_concurrent=1)
    create_job(mgr.store, tmp_path, "dispatch")
    calls = []

    class Process:
        pid = 4321

    def popen(command, **kwargs):
        calls.append((command, kwargs))
        return Process()

    monkeypatch.setattr("conservative_trading.research_jobs.subprocess.Popen", popen)
    monkeypatch.setattr("conservative_trading.research_jobs._scrubbed_environment", dict)
    assert mgr.dispatch() == ["dispatch"]
    assert mgr.store.get("dispatch")["pid"] == 4321
    assert calls[0][0][-2:] == ["--run-key", "dispatch"]
    assert calls[0][1]["start_new_session"] is True


def test_orphaned_research_requeues_same_run_id(tmp_path, monkeypatch):
    mgr = manager(tmp_path)
    create_job(mgr.store, tmp_path, "recover")
    mgr.store.claim(2)
    old = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    mgr.store.update("recover", {"started_at": old, "pid": 999_999}, expected_states=("RUNNING",))
    monkeypatch.setattr("conservative_trading.research_jobs._worker_alive", lambda *_: False)
    monkeypatch.setattr(mgr, "dispatch", list)
    result = mgr.tick()
    assert result["recovered"] == ["recover"]
    job = mgr.store.get("recover")
    assert job["state"] == "QUEUED" and job["attempts"] == 1


def test_research_timeout_is_terminal_and_durable(tmp_path, monkeypatch):
    mgr = manager(tmp_path)
    create_job(mgr.store, tmp_path, "late")
    mgr.store.claim(2)
    past = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    mgr.store.update("late", {"deadline_at": past, "pid": 4242}, expected_states=("RUNNING",))
    killed = []
    monkeypatch.setattr("conservative_trading.research_jobs._terminate_worker",
                        lambda pid, key: killed.append((pid, key)) or True)
    monkeypatch.setattr(mgr, "dispatch", list)
    result = mgr.tick()
    assert result["timed_out"] == ["late"] and killed == [(4242, "late")]
    job = mgr.store.get("late")
    assert job["state"] == "TIMED_OUT" and job["exit_code"] == 124


def test_structured_job_state_does_not_depend_on_log_words(tmp_path):
    store = ResearchJobStore(tmp_path / "proposals.json")
    job, _ = create_job(store, tmp_path, "structured")
    (tmp_path / "structured.log").write_text("FAILED and record: are ordinary report text")
    assert job["state"] == "QUEUED"
    store.update("structured", {"state": "COMPLETE", "finished_at": datetime.now(UTC).isoformat()},
                 expected_states=("QUEUED",))
    assert store.get("structured")["state"] == "COMPLETE"


def test_restart_accepts_only_the_matching_decision_record(tmp_path, monkeypatch):
    mgr = manager(tmp_path)
    create_job(mgr.store, tmp_path, "recorded")
    mgr.store.claim(2)
    record_dir = tmp_path / "runs" / "recorded"
    record_dir.mkdir(parents=True)
    (record_dir / "decision_record.json").write_text(json.dumps({
        "run_id": "recorded", "ticker": "VTI", "proposal_id": "proposal-1",
    }))
    monkeypatch.setattr(mgr, "dispatch", list)
    result = mgr.tick()
    assert result["recovered"] == ["recorded"]
    job = mgr.store.get("recorded")
    assert job["state"] == "COMPLETE" and job["proposal_id"] == "proposal-1"


def test_the_suite_cannot_open_the_real_state_database():
    """The guard has to fail the offending test, not report a diff after the damage."""
    from conservative_trading.state_db import StateDB
    from tests.conftest import ROOT

    live = StateDB(ROOT / "state" / "proposals.json")
    with pytest.raises(AssertionError, match="real state database"):
        live.status()
    with pytest.raises(AssertionError, match="real state database"), live.transaction():
        pass


def test_a_subscription_limit_is_recoverable_and_bounded(tmp_path, monkeypatch):
    """A limit is a clock, not a verdict on the work, but retrying it forever is not free."""
    mgr = manager(tmp_path, claude_limit_retry_after_hours=6, max_limit_recoveries=1)
    create_job(mgr.store, tmp_path, "limited")
    mgr.store.claim(2)
    mgr.store.update("limited", {"state": "CLAUDE_LIMIT_REACHED",
                                 "finished_at": (datetime.now(UTC) - timedelta(hours=1)).isoformat()},
                     expected_states=("RUNNING",))
    monkeypatch.setattr(mgr, "dispatch", list)

    assert mgr.tick()["resumed_after_limit"] == []          # still inside the reset window
    mgr.store.update("limited", {"finished_at": (datetime.now(UTC) - timedelta(hours=7)).isoformat()},
                     expected_states=("CLAUDE_LIMIT_REACHED",))
    assert mgr.tick()["resumed_after_limit"] == ["limited"]
    job = mgr.store.get("limited")
    assert job["state"] == "QUEUED" and job["limit_recoveries"] == 1

    mgr.store.update("limited", {"state": "CLAUDE_LIMIT_REACHED",
                                 "finished_at": (datetime.now(UTC) - timedelta(hours=7)).isoformat()},
                     expected_states=("QUEUED",))
    assert mgr.tick()["resumed_after_limit"] == []          # the recovery allowance is spent


def test_cancelling_a_run_is_terminal_and_the_worker_cannot_overwrite_it(tmp_path, monkeypatch):
    mgr = manager(tmp_path)
    create_job(mgr.store, tmp_path, "unwanted")
    mgr.store.claim(2)
    mgr.store.update("unwanted", {"pid": 4242}, expected_states=("RUNNING",))
    killed = []
    monkeypatch.setattr("conservative_trading.research_jobs._terminate_worker",
                        lambda pid, key: killed.append((pid, key)) or True)

    cancelled = mgr.cancel("unwanted", "cancelled from the console")
    assert cancelled["state"] == "CANCELLED" and killed == [(4242, "unwanted")]
    # a worker dying from the signal writes its own failure against RUNNING, which is gone
    assert mgr.store.update("unwanted", {"state": "FAILED"}, expected_states=("RUNNING",)) is None
    assert mgr.store.get("unwanted")["state"] == "CANCELLED"
    assert mgr.cancel("unwanted", "again") is None
