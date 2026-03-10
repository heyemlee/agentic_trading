"""The attention list: what still needs a person, from unresolved state, and nothing else.

Offline. The list is computed by the same pass that collects notifications, so what these
tests hold is the half that is new: which conditions are to-dos and which are announcements,
the order they come in, when an item leaves, and that "nothing to do" is never said over a
source that could not be read.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime, timedelta

import pytest

from agentic_trading import operations
from agentic_trading.execution_scenarios import _proposal
from agentic_trading.proposals import PROPOSED, ProposalStore
from agentic_trading.service import Service, ServiceConflict, ServiceError
from tests.conftest import ROOT, copy_example_ledger

MICRO = "micro_live"


@pytest.fixture
def service(tmp_path, monkeypatch, offline_market_reads):
    cfg = tmp_path / "config"
    shutil.copytree(ROOT / "config", cfg)
    state = tmp_path / "state"
    state.mkdir()
    (tmp_path / "runs").mkdir()
    copy_example_ledger("portfolio.example.yaml", state / "portfolio.yaml")
    copy_example_ledger("micro_live_portfolio.example.yaml", state / "micro_live_portfolio.yaml")
    service = Service(cfg)
    # Whether this machine's broker token works today is not something a test may depend on.
    monkeypatch.setattr(service, "_broker_connect_error", lambda: None)
    return service


def rested(service):
    """Every background activity has just run, so nothing is overdue."""
    db = service.state_db()
    for name in operations.ACTIVITIES:
        operations.record(db, name, "ran", min_interval_seconds=0)


def store_for(service):
    profiles = service.profiles()
    return ProposalStore(profiles.project_dir / str(profiles.risk["broker"]["state_file"]))


def kinds(report):
    return [item["kind"] for item in report["items"]]


# ------------------------------------------------------------------ the empty answer
def test_nothing_to_do_is_said_only_after_everything_was_read(service):
    rested(service)
    report = service.attention()
    assert report["readable"] is True and report["unreadable"] == []
    assert report["items"] == [] and report["top"] is None
    assert report["summary"] == {"total": 0, "requires_person": 0,
                                 "by_severity": {"critical": 0, "high": 0, "normal": 0, "info": 0}}
    assert set(report["context"]) == {"market_open", "paused", "autonomy_armed", "autonomous_queue",
                                      "next_scheduled_at"}
    assert report["context"]["paused"] is False and report["context"]["autonomous_queue"] == 0


def test_a_source_that_could_not_be_read_makes_the_answer_unconfirmable(service, monkeypatch):
    rested(service)
    monkeypatch.setattr(service, "operations", lambda: (_ for _ in ()).throw(OSError("disk gone")))
    report = service.attention()
    assert report["readable"] is False
    assert report["unreadable"] and report["unreadable"][0].startswith("operations: OSError")
    # Still an answer: whatever else was readable is there, and the shape is whole.
    assert report["items"] == [] and report["summary"]["total"] == 0


def test_a_fresh_deployment_has_overdue_activities_not_an_empty_list(service):
    report = service.attention()
    assert {"backup_overdue"} == set(kinds(report))
    assert {item["subject"] for item in report["items"]} == {"backup", "maintenance", "ledger_confirm"}
    assert all(item["link"] == {"page": "system", "params": {}} for item in report["items"])


# ------------------------------------------------------------------ order and exit
def test_items_come_in_the_order_a_person_should_take_them(service):
    rested(service)
    store = store_for(service)
    waiting = _proposal(status=PROPOSED, bucket=MICRO, ticker="VTI")
    waiting.approved_at = None
    broken = _proposal(status=PROPOSED, bucket=MICRO, ticker="BND")
    broken.approved_at = None
    broken.reconciliation_error = "venue returned no status for the order; lookup required"
    store.save(waiting)
    store.save(broken)

    report = service.attention()
    assert kinds(report)[0] == "reconciliation_error" and report["top"]["subject"] == broken.proposal_id
    top = report["top"]
    assert top["severity"] == "critical" and top["priority"] == 1 and top["requires_person"] is True
    assert top["scope"] == "agentic" and top["account_id"] == MICRO and top["account_label"] == "Agentic"
    assert top["link"] == {"page": "activity", "params": {"subject": broken.proposal_id}}
    assert top["id"] == f"reconciliation_error:{broken.proposal_id}"

    queue = next(item for item in report["items"] if item["kind"] == "proposal_waiting")
    assert queue["subject"] == waiting.proposal_id and queue["priority"] == 3
    assert queue["actions"] == ["approve", "reject", "reanalyze"]
    assert queue["link"] == {"page": "approvals", "params": {"proposal": waiting.proposal_id}}
    assert queue["id"].startswith(f"proposal_waiting:{waiting.proposal_id}:")
    assert report["summary"]["requires_person"] == 2

    # The problem resolves; the item leaves, and nothing remembers it here.
    broken.reconciliation_error = None
    store.save(broken)
    assert "reconciliation_error" not in kinds(service.attention())


def test_an_expired_proposal_asks_for_fresh_research_not_for_approval(service):
    rested(service)
    store = store_for(service)
    old = _proposal(status=PROPOSED, bucket=MICRO, ticker="VTI", age_minutes=60 * 24 * 3)
    old.approved_at = None
    store.save(old)
    report = service.attention()
    assert kinds(report) == ["proposal_stale"]
    item = report["items"][0]
    assert item["actions"] == ["reanalyze", "reject"] and "expired" in item["title"]
    assert item["severity"] == "normal" and item["requires_person"] is True
    # The notification pass says nothing about it, exactly as before the list existed.
    assert not [e for e in service.collect_notifications() if "proposal" in e.kind]


def test_a_withdrawn_autonomy_and_a_cooling_change_are_told_apart(service):
    from agentic_trading import autonomy
    from agentic_trading import config_changes as cc

    rested(service)
    autonomy.trip(service.profiles(), "an order we cannot account for")
    db = service.state_db()
    cc.save(db, [{"id": "chg_1", "file": "risk_profile.yaml", "status": cc.PENDING, "classification": cc.COOLING,
                  "paths": ["buckets.micro_live.autonomy.reduce.enabled"], "reasons": [],
                  "requested_by": "owner", "requested_at": "2026-09-18T20:58:00+00:00",
                  "apply_after": "2026-09-19T20:58:00+00:00"}])
    report = service.attention()
    assert kinds(report) == ["autonomy_withdrawn", "config_change_pending"]
    withdrawn, pending = report["items"]
    assert withdrawn["priority"] == 2 and withdrawn["actions"] == ["reset_autonomy", "open"]
    assert pending["requires_person"] is False and pending["severity"] == "info"
    assert pending["actions"] == ["withdraw_change"]
    assert pending["link"] == {"page": "settings", "params": {"change": "chg_1"}}


# ------------------------------------------------------------------ personal tasks
def personal_job(service, *, kind="portfolio_review", state="COMPLETE", finished_ago_hours=1.0,
                 ticker="ASSISTANT", account_id=None, run_key=None):
    manager = service._research_manager(service.profiles())
    run_key = run_key or f"advisor_{kind}_{ticker}_{int(finished_ago_hours * 100)}"
    manager.create(run_key=run_key, ticker=ticker, bucket="advisory", trade_date="2026-09-17",
                   instrument_type="auto", trigger="manual",
                   task={"kind": kind, "account_id": account_id, "language": "zh"})
    finished = (datetime.now(UTC) - timedelta(hours=finished_ago_hours)).isoformat()
    return manager.store.update(run_key, {"state": state, "finished_at": finished,
                                          "error": None if state == "COMPLETE" else "boom"})


def test_a_finished_review_stands_until_it_is_opened_not_until_it_is_polled(service):
    rested(service)
    job = personal_job(service)
    report = service.attention()
    assert kinds(report) == ["personal_task_done"]
    item = report["items"][0]
    assert item["scope"] == "personal" and item["account_scope_kind"] == "all_personal"
    assert item["account_label"] == "all personal accounts" and item["requires_person"] is False
    assert item["link"] == {"page": "advisor", "params": {"scope": "personal"}}

    # Reading the run is what the console does every few seconds; it is not the person looking.
    service.research_run(job["run_key"])
    assert kinds(service.attention()) == ["personal_task_done"]

    opened = service.mark_opened(job["run_key"])
    assert opened["opened_at"]
    assert kinds(service.attention()) == []
    assert service.mark_opened(job["run_key"])["opened_at"] == opened["opened_at"]   # idempotent


def test_only_a_finished_run_can_be_opened(service):
    with pytest.raises(ServiceError, match="no research run"):
        service.mark_opened("never-heard-of-it")
    running = personal_job(service, state="RUNNING", run_key="advisor_running")
    with pytest.raises(ServiceConflict, match="still RUNNING"):
        service.mark_opened(running["run_key"])


def test_a_finished_review_nobody_opened_leaves_the_list_after_a_day(service):
    rested(service)
    personal_job(service, finished_ago_hours=25)
    assert kinds(service.attention()) == []


def test_a_failed_personal_task_leaves_when_a_newer_one_finishes(service):
    rested(service)
    failed = personal_job(service, kind="analysis", state="FAILED", ticker="NVDA",
                          account_id="9c2e41b7a0d35f6e8b12", finished_ago_hours=2)
    report = service.attention()
    assert kinds(report) == ["personal_task_failed"]
    assert report["items"][0]["account_scope_kind"] == "single"
    assert report["items"][0]["actions"] == ["retry", "open"]
    # The failure is still announced, as it always was, under the same key.
    assert [e.key for e in service.collect_notifications() if e.kind == "research_failed"] == [
        f"research_failed:{failed['run_key']}"]

    personal_job(service, kind="analysis", state="COMPLETE", ticker="NVDA",
                 account_id="9c2e41b7a0d35f6e8b12", finished_ago_hours=0.5, run_key="advisor_newer")
    report = service.attention()
    assert "personal_task_failed" not in kinds(report)
    assert kinds(report) == ["personal_task_done"]


def test_a_chat_that_failed_is_announced_but_is_not_a_to_do(service):
    rested(service)
    personal_job(service, kind="chat", state="FAILED", finished_ago_hours=1)
    assert kinds(service.attention()) == []
    assert [e for e in service.collect_notifications() if e.kind == "research_failed"]


# ------------------------------------------------------------------ announcements
def test_an_autonomous_order_is_told_once_and_never_listed_as_a_to_do(service):
    rested(service)
    store = store_for(service)
    acted = _proposal(status="EXECUTED", bucket=MICRO, ticker="VTI")
    acted.approved_by = "autonomy:reduce"
    store.save(acted)
    assert kinds(service.attention()) == []
    told = [e for e in service.collect_notifications() if e.kind == "autonomy_acted"]
    assert len(told) == 1 and told[0].key == f"autonomy_acted:{acted.proposal_id}:EXECUTED"


def test_a_broken_broker_connection_is_a_to_do_under_its_old_key(service, monkeypatch):
    rested(service)
    profiles = service.profiles()
    profiles.risk["micro_live_execution"]["enabled"] = True
    monkeypatch.setattr(service, "profiles", lambda: profiles)
    monkeypatch.setattr(service, "_broker_connect_error", lambda: "token expired")
    report = service.attention()
    assert kinds(report) == ["credential_invalid"] and report["items"][0]["id"] == "credential_invalid:robinhood"
    assert report["items"][0]["priority"] == 1


# ------------------------------------------------------- what each activity state does to the list

def test_an_activity_not_yet_due_is_not_a_to_do(service):
    """The false alarm: a daily job whose code arrived an hour ago has nothing for a person."""
    from agentic_trading import operations

    operations.declare(service.state_db(), origin=(datetime.now(UTC) - timedelta(hours=1)).isoformat())
    kinds = [i["kind"] for i in service.attention()["items"]]
    assert "backup_overdue" not in kinds


def test_an_activity_that_missed_its_deadline_is_a_to_do(service):
    from agentic_trading import operations

    operations.declare(service.state_db(), origin=(datetime.now(UTC) - timedelta(days=3)).isoformat())
    items = [i for i in service.attention()["items"] if i["kind"] == "backup_overdue"]
    assert {i["subject"] for i in items} >= {"ledger_confirm"}
    item = next(i for i in items if i["subject"] == "ledger_confirm")
    assert item["severity"] == "normal" and item["priority"] == 4
    assert item["requires_person"] is True


def test_a_refusal_raises_one_item_and_not_two(service):
    """A ledger that refused already says so at critical; saying it again at normal teaches
    a person to ignore both."""
    from agentic_trading import operations

    db = service.state_db()
    operations.declare(db, origin=(datetime.now(UTC) - timedelta(days=3)).isoformat())
    db.write_meta(operations.HEARTBEATS,
                  {"ledger_confirm": {"at": (datetime.now(UTC) - timedelta(hours=25)).isoformat(),
                                      "detail": "ok"}})
    db.write_meta("ledger_confirm_report", {
        "at": (datetime.now(UTC) - timedelta(minutes=20)).isoformat(), "confirmed": False,
        "date": "2026-09-19",
        "ledgers": [{"ledger": "micro_live", "confirmed": False,
                     "error": "CashMismatch: the venue holds 101.50 and the ledger 100.00"}]})

    items = service.attention()["items"]
    overdue = [i["subject"] for i in items if i["kind"] == "backup_overdue"]
    assert "ledger_confirm" not in overdue, "the refusal is the item; it is not also late"
    assert "backup" in overdue, "the others are genuinely late and still say so"
    unconfirmed = [i for i in items if i["kind"] == "ledger_unconfirmed"]
    assert len(unconfirmed) == 1
    assert unconfirmed[0]["severity"] == "critical" and unconfirmed[0]["priority"] == 1
    assert "CashMismatch" in unconfirmed[0]["detail"]
