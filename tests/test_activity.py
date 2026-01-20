"""The audit journal: one order across every proposal, and what it refuses to carry.

The screen this replaces flattened the research list and the history of whichever proposal
happened to be open. It could show one proposal's story, never the system's, and it lost
everything the moment a different proposal took the top of the queue.
"""
from __future__ import annotations

import shutil

import pytest

from agentic_trading import events
from agentic_trading.execution_scenarios import _proposal
from agentic_trading.proposals import PROPOSED, REJECTED, ProposalStore
from agentic_trading.service import Service
from tests.conftest import ROOT


@pytest.fixture
def service(tmp_path):
    cfg = tmp_path / "config"
    shutil.copytree(ROOT / "config", cfg)
    (tmp_path / "state").mkdir()
    (tmp_path / "runs").mkdir()
    shutil.copy(ROOT / "state" / "portfolio.example.yaml", tmp_path / "state" / "portfolio.yaml")
    shutil.copy(ROOT / "state" / "micro_live_portfolio.example.yaml",
                tmp_path / "state" / "micro_live_portfolio.yaml")
    ready = Service(cfg)
    ready.profiles().risk["broker"]["state_file"] = "state/proposals.json"
    return ready


def store(service) -> ProposalStore:
    return ProposalStore(service.profiles().project_dir / "state" / "proposals.json")


def make(service, ticker, *, status=PROPOSED):
    proposal = _proposal(status=status)
    proposal.ticker = ticker
    proposal.proposal_id = f"2026-09-04-{ticker}-{ticker.lower()}"
    store(service).save(proposal)
    return proposal


def test_the_timeline_spans_every_proposal_not_just_the_open_one(service):
    first, second = make(service, "VTI"), make(service, "BND")
    second.status = REJECTED
    second.note("rejected_by_investor", "not now")
    store(service).save(second)

    page = service.activity(limit=50)
    subjects = {row["subject"] for row in page["events"]}
    assert {first.proposal_id, second.proposal_id} <= subjects
    kinds = [row["kind"] for row in page["events"] if row["subject"] == second.proposal_id]
    assert "proposal_rejected" in kinds and "proposal_created" in kinds


def test_paging_is_stable_when_entries_share_an_instant(service):
    for index in range(12):
        make(service, f"SYM{index}")

    first = service.activity(limit=5)
    assert len(first["events"]) == 5 and first["next_cursor"]
    second = service.activity(limit=5, cursor=first["next_cursor"])
    assert len(second["events"]) == 5

    seen = [row["id"] for row in first["events"] + second["events"]]
    assert len(seen) == len(set(seen))            # no entry appears on two pages
    assert seen == sorted(seen, reverse=True)     # and the order is total


def test_the_last_page_ends_rather_than_looping(service):
    make(service, "VTI")
    page = service.activity(limit=50)
    assert page["next_cursor"] is None


def test_the_journal_can_be_filtered_by_kind_and_by_subject(service):
    kept, rejected = make(service, "VTI"), make(service, "BND")
    rejected.status = REJECTED
    rejected.note("rejected_by_investor", "not now")
    store(service).save(rejected)

    only_rejections = service.activity(kinds=("proposal_rejected",))
    assert {row["kind"] for row in only_rejections["events"]} == {"proposal_rejected"}
    assert only_rejections["events"][0]["subject"] == rejected.proposal_id

    one_proposal = service.activity(subject=kept.proposal_id)
    assert {row["subject"] for row in one_proposal["events"]} == {kept.proposal_id}


def test_pausing_leaves_a_trace_after_the_halt_file_is_gone(service):
    """The halt file is deleted on resume, so it cannot be the record of a pause."""
    service.pause("stepping away", identity="owner")
    service.resume(identity="owner")
    assert not (service.profiles().project_dir / "state" / "HALT").exists()

    kinds = [row["kind"] for row in service.activity()["events"]]
    assert kinds[:2] == ["system_resumed", "system_paused"]
    paused = next(row for row in service.activity()["events"] if row["kind"] == "system_paused")
    assert paused["actor"] == "owner" and paused["detail"]["reason"] == "stepping away"


def test_a_finished_run_is_remembered_even_after_it_is_requeued(service):
    """A job row holds only its current state."""
    manager = service._research_manager(service.profiles())
    manager.store.create(run_key="r1", ticker="VTI", bucket="core", trade_date="2026-09-04",
                         instrument_type="etf", trigger="scheduled",
                         log_path=service.profiles().project_dir / "runs" / "r1.log",
                         timeout_seconds=120, max_attempts=2)
    manager.store.update("r1", {"state": "FAILED", "error": "worker exited"},
                         expected_states=("QUEUED",))
    manager.store.update("r1", {"state": "QUEUED", "error": None}, expected_states=("FAILED",))

    assert manager.store.get("r1")["state"] == "QUEUED"          # the row forgot
    failures = service.activity(kinds=("research_failed",))["events"]
    assert len(failures) == 1 and failures[0]["subject"] == "r1"
    assert failures[0]["outcome"] == "FAILED"


def test_a_fill_is_journalled_once_however_often_it_is_replayed(service):
    from agentic_trading.state_db import StateDB

    db = StateDB(service.profiles().project_dir / "state" / "proposals.json")
    fill = {"fill_id": "f1", "proposal_id": "p1", "at": "2026-09-04T18:00:00+00:00",
            "symbol": "VTI", "side": "buy", "quantity": 2.0, "price": 100.0, "notional": 200.0}
    assert db.append_fill(fill) is True
    assert db.append_fill(fill) is False          # the reconciler replays the same fill

    filled = service.activity(kinds=("order_filled",))["events"]
    assert len(filled) == 1
    assert filled[0]["detail"]["symbol"] == "VTI" and filled[0]["detail"]["quantity"] == 2.0


def test_a_credential_or_a_private_note_cannot_reach_the_journal(service):
    assert events.redact({
        "ticker": "VTI", "access_token": "secret", "csrf": "x", "note": "my private reasoning",
        "account_number": "1234", "quantity": 2.0,
    }) == {"ticker": "VTI", "quantity": 2.0}

    approved = make(service, "VTI")
    approved.approval_note = "do not copy this anywhere"
    approved.note("approved", "explicit approval", identity="owner", version="a" * 64)
    store(service).save(approved)

    blob = str(service.activity()["events"])
    assert "do not copy this anywhere" not in blob
    entry = next(row for row in service.activity()["events"] if row["kind"] == "proposal_approved")
    assert entry["actor"] == "owner" and entry["version"] == "a" * 64


def test_an_unknown_kind_is_refused_rather_than_stored(service):
    from agentic_trading.state_db import StateDB

    db = StateDB(service.profiles().project_dir / "state" / "proposals.json")
    with pytest.raises(ValueError, match="unknown event kind"), db.transaction() as conn:
        events.record(conn, kind="something_invented", subject="x")


def test_history_written_before_the_journal_existed_is_caught_up_once(service):
    """A proposal that closed before the journal must not be missing from the record."""
    from agentic_trading.state_db import StateDB

    proposal = make(service, "VTI")
    db = StateDB(service.profiles().project_dir / "state" / "proposals.json")
    with db.transaction() as conn:                 # the state the upgrade finds
        conn.execute("DELETE FROM events")
        conn.execute("DELETE FROM documents WHERE namespace=?", (db.META + events.BACKFILLED,))

    caught_up = service.activity()
    assert [row["kind"] for row in caught_up["events"]] == ["proposal_created"]
    assert caught_up["events"][0]["subject"] == proposal.proposal_id

    # and a second read does not duplicate it
    assert len(service.activity()["events"]) == 1
