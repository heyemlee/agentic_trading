"""What research is about: what the owner holds, then what the owner added. Offline.

The Lab lists two groups and the weekly run studies the same two, so both are read from one
place: the managed ledger and the owner's own accounts as Robinhood was last read, then the
names on the owner's Robinhood watchlists. Demo books and records bound to no account are about
nothing the owner holds or chose, and appear in neither.
"""

from __future__ import annotations

import json
import os
import shutil
import uuid
from datetime import datetime

import pytest
import yaml

from agentic_trading.advisor import SNAPSHOT, now
from agentic_trading.research_jobs import ResearchJobManager
from agentic_trading.service import Service
from agentic_trading.service.shapes import ResearchSubjects
from tests.conftest import ROOT, copy_example_ledger, recent_session

A, B = "a" * 20, "b" * 20


def owner_snapshot():
    return {"fetched_at": now(), "complete": True, "errors": [], "source": "test",
            "accounts": [{"id": A, "label": "••••1111", "complete": True,
                          "positions": [{"symbol": "AAPL", "quantity": 3}, {"symbol": "VTI", "quantity": 1}]},
                         {"id": B, "label": "••••2222", "complete": True,
                          "positions": [{"symbol": "KO", "quantity": 5}]}],
            "candidates": [{"symbol": "NVDA", "sources": ["watchlists:AI & chips"]},
                           {"symbol": "AAPL", "sources": ["watchlists:Core"]},
                           {"symbol": "META", "sources": ["scans:52-week highs"]},
                           {"symbol": "VXUS", "sources": ["profile:international_equity"]}]}


@pytest.fixture
def service(tmp_path, monkeypatch, offline_market_reads):
    cfg = tmp_path / "config"
    shutil.copytree(ROOT / "config", cfg)
    state = tmp_path / "state"
    state.mkdir()
    (tmp_path / "runs").mkdir()
    copy_example_ledger("portfolio.example.yaml", state / "portfolio.yaml")
    (state / "micro_live_portfolio.yaml").write_text(yaml.safe_dump({
        "as_of": recent_session(), "currency": "USD", "buckets": {"micro_live": {"cash_usd": 40.0}},
        "positions": [{"symbol": "VTI", "bucket": "micro_live", "quantity": 0.2,
                       "cost_basis_per_share": 100.0}],
        "recent_opens": []}), encoding="utf-8")
    monkeypatch.setattr(ResearchJobManager, "dispatch", lambda self: [])
    service = Service(cfg)
    service.state_db().write_meta(SNAPSHOT, owner_snapshot())
    return service


def record(service, run_id, *, ticker, created_at, **fields):
    doc = {"run_id": run_id, "ticker": ticker, "trade_date": created_at[:10], "created_at": created_at,
           "instrument_type": "stock", "policy": {"final_action": "NO_TRADE", "confidence": 52, "proposal": {}},
           "hard_risk": {"verdict": "NO_ACTION"}, **fields}
    path = service.profiles().project_dir / "runs" / run_id / "decision_record.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc))
    # research_history orders by file time, newest first, as a run finishing later would.
    stamp = datetime.fromisoformat(created_at).timestamp()
    os.utime(path, (stamp, stamp))


def test_held_is_the_managed_book_then_the_owners_accounts_and_added_is_the_watchlists(service):
    subjects = ResearchSubjects.model_validate(service.research_subjects())
    held = {row.symbol: row for row in subjects.held}
    assert list(held) == ["VTI", "AAPL", "KO"]
    # VTI is in the managed book and an own account: one row, both holders, reviewed where an
    # exit could follow.
    assert [(h.scope, h.account_id) for h in held["VTI"].holders] == [("agentic", "micro_live"), ("personal", A)]
    assert held["VTI"].start.model_dump(exclude_none=True) == {
        "scope": "agentic", "bucket": "micro_live", "purpose": "review_holding"}
    # A name only the owner holds is analysed read-only in the account that holds it.
    assert held["KO"].start.model_dump(exclude_none=True) == {
        "scope": "personal", "kind": "analysis", "account_id": B}
    assert held["KO"].holders[0].account_label == "••••2222"
    # AAPL is on a watchlist and held: it is listed once, as held, and still names its list.
    assert held["AAPL"].lists == ["Core"]

    # Added: watchlist names only -- not the screener's, not the profile's -- less what is held.
    assert [row.symbol for row in subjects.added] == ["NVDA"]
    assert subjects.added[0].lists == ["AI & chips"]
    assert subjects.added[0].start.model_dump(exclude_none=True) == {
        "scope": "agentic", "bucket": "micro_live", "purpose": "candidate"}
    assert subjects.snapshot.read is True and subjects.snapshot.age_seconds is not None
    assert subjects.errors == []


def test_each_name_carries_its_newest_record_and_never_a_demo_or_unbound_one(service):
    record(service, "old-agentic", ticker="VTI", bucket="micro_live", created_at="2026-09-10T12:00:00+00:00")
    record(service, "new-demo", ticker="VTI", bucket="core", created_at="2026-09-18T12:00:00+00:00")
    record(service, "unbound", ticker="VTI", created_at="2026-09-19T12:00:00+00:00")
    record(service, "own-aapl", ticker="AAPL", bucket="advisory", advisory=True, task_kind="analysis",
           account_id=A, created_at="2026-09-15T12:00:00+00:00")
    record(service, "chat", ticker="ASSISTANT", bucket="advisory", advisory=True, task_kind="chat",
           created_at="2026-09-20T12:00:00+00:00")

    subjects = ResearchSubjects.model_validate(service.research_subjects())
    held = {row.symbol: row for row in subjects.held}
    assert held["VTI"].latest.run_id == "old-agentic"          # newer Demo and unbound records ignored
    assert held["AAPL"].latest.run_id == "own-aapl" and held["AAPL"].latest.scope == "personal"
    assert held["KO"].latest is None
    assert subjects.added[0].latest is None


def test_a_run_in_flight_is_named_on_its_row(service):
    job = service.start_research("NVDA", bucket="micro_live", scope="agentic", purpose="candidate",
                                 request_id=str(uuid.uuid4()))
    own = service.start_advisor(kind="analysis", request_id=str(uuid.uuid4()), ticker="KO", account_id=B)
    service.start_research("AAPL", bucket="core", scope="demo", request_id=str(uuid.uuid4()))

    subjects = service.research_subjects()
    rows = {row["symbol"]: row for row in subjects["held"] + subjects["added"]}
    assert rows["NVDA"]["running"]["run_key"] == job["run_key"]
    assert rows["KO"]["running"]["run_key"] == own["run_key"]
    assert rows["AAPL"]["running"] is None                     # a Demo run is not about the owner's AAPL


def test_nothing_read_yet_says_so_instead_of_an_empty_portfolio(service):
    service.state_db().write_meta(SNAPSHOT, {})
    subjects = ResearchSubjects.model_validate(service.research_subjects())
    assert [row.symbol for row in subjects.held] == ["VTI"]     # the managed ledger still reads
    assert subjects.added == []
    assert subjects.snapshot.read is False and subjects.snapshot.age_seconds is None


def test_the_route_answers_in_its_shape_and_old_research_links_land_in_the_lab(auth_client, monkeypatch, service):
    monkeypatch.setattr("agentic_trading.api.app._service", service)
    response = auth_client.get("/research/subjects")
    assert response.status_code == 200
    assert [row["symbol"] for row in response.json()["added"]] == ["NVDA"]

    # Research became part of the Lab; a link written before then keeps its query.
    moved = auth_client.get("/ui/research.html?run=R1&scope=agentic", follow_redirects=False)
    assert moved.status_code == 307
    assert moved.headers["location"] == "/ui/lab.html?run=R1&scope=agentic"
    assert auth_client.get("/ui/research.html", follow_redirects=False).headers["location"] == "/ui/lab.html"

    # Assets became Home: a holding's address, or a delivered notification, keeps its view.
    moved = auth_client.get("/ui/portfolio.html?scope=robinhood&account=A&ticker=VOO", follow_redirects=False)
    assert moved.status_code == 307
    assert moved.headers["location"] == "/ui/index.html?scope=robinhood&account=A&ticker=VOO"
    assert auth_client.get("/ui/portfolio.html", follow_redirects=False).headers["location"] == "/ui/index.html"
