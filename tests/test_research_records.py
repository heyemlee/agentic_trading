"""Research records and jobs bound to the account they are about. Offline.

What a screen binds a record by -- scope, account, task type, route, what the facts are as of,
what it lacked, whether a newer record superseded it -- is read from the record, and a record
that predates a field says so instead of being guessed into a scope.
"""

from __future__ import annotations

import json
import shutil
import uuid

import pytest

from agentic_trading.research_jobs import ResearchJobManager
from agentic_trading.service import Service, ServiceConflict, ServiceError
from tests.conftest import ROOT, copy_example_ledger


@pytest.fixture
def service(tmp_path, monkeypatch, offline_market_reads):
    cfg = tmp_path / "config"
    shutil.copytree(ROOT / "config", cfg)
    state = tmp_path / "state"
    state.mkdir()
    (tmp_path / "runs").mkdir()
    copy_example_ledger("portfolio.example.yaml", state / "portfolio.yaml")
    copy_example_ledger("micro_live_portfolio.example.yaml", state / "micro_live_portfolio.yaml")
    monkeypatch.setattr(ResearchJobManager, "dispatch", lambda self: [])
    return Service(cfg)


def write_record(service, run_id, **fields):
    """A decision record on disk, shaped like the pipeline's, with whatever the test needs."""
    record = {"run_id": run_id, "ticker": "VTI", "bucket": "core", "trade_date": "2026-09-16",
              "created_at": f"2026-09-16T1{len(run_id) % 10}:00:00+00:00", "instrument_type": "etf",
              "policy": {"final_action": "NO_TRADE", "confidence": 61,
                         "proposal": {"data_quality": "partial", "missing_information": ["FRED macro"]}},
              "upstream_decision": {"portfolio_manager_signal": "Hold", "bear_case_excerpt": "x" * 700},
              "hard_risk": {"verdict": "NO_ACTION"}, "final_recommendation": {"action": "NO_TRADE"},
              "llm_usage": {"calls": 12}, "elapsed_seconds": 400.0,
              "data_sources": {"observed_tools": ["get_stock_data", "get_news"],
                               "web_access": {"fetches": [{"retrieved_at": "2026-09-16T14:00:00+00:00"},
                                                          {"retrieved_at": "2026-09-16T15:30:00+00:00"}],
                                              "cli_web_search_used": False}},
              "unresolved_profile_fields": ["a", "b"], "portfolio": {"as_of": "2026-09-15"},
              "package_version": "0.1.0", "upstream_tradingagents_commit": "9dee508c",
              "models": {"deep": "claude-opus-5", "quick": "claude-sonnet-5"}}
    record.update(fields)
    path = service.profiles().project_dir / "runs" / run_id / "decision_record.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(record), encoding="utf-8")
    return record


# ------------------------------------------------------------------ records
def test_a_record_says_what_it_is_about_and_what_it_lacked(service):
    write_record(service, "run_core", authority={"research": "v-1", "proposal": "v-1",
                                                 "changed_during_research": False},
                 route={"name": "tradingagents", "skill_version": None}, purpose="candidate")
    (row,) = service.research_history()
    assert row["scope"] == "demo" and row["account_id"] == "conceptual" and row["account_label"] == "Demo"
    assert row["account_scope_kind"] == "single" and row["task_type"] == "full_research"
    assert row["purpose"] == "candidate" and row["authority_version"] == "v-1"
    assert row["route"] == {"name": "tradingagents", "inferred": False, "package_version": "0.1.0",
                            "upstream_commit": "9dee508c", "skill_version": None,
                            "models": {"deep": "claude-opus-5", "quick": "claude-sonnet-5"}}
    assert row["snapshot_as_of"] == "2026-09-15" and row["prices_as_of"] == "2026-09-16"
    assert row["sources_retrieved_at_max"] == "2026-09-16T15:30:00+00:00"
    # Never from the time this side read a source.
    assert row["facts_as_of"] is None and row["facts_as_of_basis"] == "unknown"
    assert row["evidence"] == {"sources": 2, "web_fetches": 2, "cli_web_search_used": False}
    assert row["limitations"] == {"data_quality": "partial", "missing_information": ["FRED macro"],
                                  "unresolved_profile_fields": 2}
    assert len(row["counter_evidence"]) == 600 and row["counter_evidence_truncated"] is True
    assert row["changes_since_previous"] is None and row["refers_to_run_id"] is None
    assert row["superseded_by"] is None
    # The floor to open a small core ETF position, from the profile; a person reads it as the
    # bar this run was measured against, not as a score.
    assert row["applicable_floor"] == service.profiles().confidence.floor("core", "etf", "small")


def test_an_old_record_reports_unknowns_rather_than_a_guess(service):
    write_record(service, "run_old", bucket=None, models={}, package_version=None,
                 upstream_tradingagents_commit=None, data_sources={}, portfolio={},
                 upstream_decision={"portfolio_manager_signal": "Hold"})
    (row,) = service.research_history(scope="unspecified")
    assert row["scope"] == "unspecified" and row["account_scope_kind"] == "unknown"
    assert row["account_label"] == "unspecified" and row["authority_version"] is None
    assert row["route"]["name"] == "tradingagents" and row["route"]["inferred"] is True
    assert row["sources_retrieved_at_max"] is None and row["counter_evidence"] is None
    assert row["counter_evidence_truncated"] is False and row["applicable_floor"] is None
    assert service.research_history(scope="demo") == []          # never guessed into a scope


def test_personal_records_are_bound_to_their_account_and_never_to_another(service):
    from agentic_trading.advisor import SNAPSHOT

    service.state_db().write_meta(SNAPSHOT, {"accounts": [{"id": "a" * 20, "label": "••••1111"},
                                                            {"id": "b" * 20, "label": "••••2222"}]})
    write_record(service, "advisor_one", bucket="advisory", advisory=True, task_kind="analysis",
                 account_id="a" * 20, account_scope_kind="single", account_ids=[],
                 route={"name": "tradingagents"})
    write_record(service, "advisor_all", bucket="advisory", advisory=True, task_kind="analysis",
                 account_id=None, account_scope_kind="all_personal", account_ids=["a" * 20, "b" * 20])
    write_record(service, "advisor_legacy", bucket="advisory", advisory=True, task_kind="analysis",
                 account_id=None)                                  # predates the field

    by_id = {row["run_id"]: row for row in service.research_history(scope="personal")}
    assert by_id["advisor_one"]["account_label"] == "••••1111" and by_id["advisor_one"]["applicable_floor"] is None
    assert by_id["advisor_all"]["account_scope_kind"] == "all_personal"
    assert by_id["advisor_all"]["account_label"] == "all personal accounts"
    assert by_id["advisor_legacy"]["account_scope_kind"] == "unknown"
    assert by_id["advisor_legacy"]["account_label"] == "unspecified"
    assert by_id["advisor_legacy"]["route"] == {**by_id["advisor_legacy"]["route"], "name": "tradingagents",
                                                "inferred": True}

    # Account A's own view: its single record only, and the all-accounts review only when asked.
    assert [r["run_id"] for r in service.research_history(scope="personal", account_id="a" * 20)] == ["advisor_one"]
    both = service.research_history(scope="personal", account_id="a" * 20, include_all_personal=True)
    assert {r["run_id"] for r in both} == {"advisor_one", "advisor_all"}
    # Account B was in the all-accounts review and has no single record; the legacy one is nobody's.
    assert [r["run_id"] for r in service.research_history(scope="personal", account_id="b" * 20,
                                                          include_all_personal=True)] == ["advisor_all"]
    assert service.research_history(scope="personal", account_id="c" * 20, include_all_personal=True) == []


def test_a_newer_record_of_the_same_kind_supersedes_the_older_and_nothing_else(service, monkeypatch):
    import os
    import time

    write_record(service, "core_vti_old", purpose="review_holding")
    write_record(service, "core_vti_new", purpose="review_holding")
    write_record(service, "core_vti_candidate", purpose="candidate")           # another purpose
    write_record(service, "micro_vti", bucket="micro_live", purpose="review_holding")  # another scope
    write_record(service, "personal_vti", bucket="advisory", advisory=True, task_kind="analysis",
                 account_id="a" * 20, account_scope_kind="single")
    runs = service.profiles().project_dir / "runs"
    now = time.time()
    for offset, name in enumerate(["core_vti_old", "personal_vti", "micro_vti", "core_vti_candidate", "core_vti_new"]):
        os.utime(runs / name / "decision_record.json", (now + offset, now + offset))

    rows = {row["run_id"]: row for row in service.research_history(limit=10)}
    assert rows["core_vti_old"]["superseded_by"] == "core_vti_new"
    assert rows["core_vti_new"]["superseded_by"] is None
    assert rows["core_vti_candidate"]["superseded_by"] is None
    assert rows["micro_vti"]["superseded_by"] is None and rows["micro_vti"]["scope"] == "agentic"
    assert rows["personal_vti"]["superseded_by"] is None
    assert [r["run_id"] for r in service.research_history(scope="agentic")] == ["micro_vti"]
    assert [r["run_id"] for r in service.research_history(task_type="analysis")] == ["personal_vti"]


# ------------------------------------------------------------------ jobs
def test_a_job_carries_the_same_binding_as_its_record_will(service):
    started = service.start_research("VTI", bucket="micro_live", scope="agentic", purpose="candidate",
                                     request_id=str(uuid.uuid4()))
    assert started["scope"] == "agentic" and started["account_id"] == "micro_live"
    assert started["task_type"] == "full_research" and started["purpose"] == "candidate"
    assert started["route"]["name"] == "tradingagents" and started["route"]["inferred"] is True
    assert started["cancellable"] is True and started["authority_version"]
    (job,) = service.research_jobs(scope="agentic")
    assert job["run_key"] == started["run_key"] and service.research_jobs(scope="demo") == []
    assert service.research_run(started["run_key"])["scope"] == "agentic"


def test_a_repeated_request_returns_the_run_it_started_and_a_different_one_conflicts(service):
    request_id = str(uuid.uuid4())
    first = service.start_research("VTI", bucket="core", scope="demo", purpose="candidate", request_id=request_id)
    again = service.start_research("VTI", bucket="core", scope="demo", purpose="candidate", request_id=request_id)
    assert again["run_key"] == first["run_key"] and again["deduplicated"] is True
    with pytest.raises(ServiceConflict, match="different request"):
        service.start_research("BND", bucket="core", scope="demo", request_id=request_id)
    with pytest.raises(ServiceError, match="must be a UUID"):
        service.start_research("VTI", bucket="core", request_id="not-a-uuid")


def test_a_replayed_request_aimed_at_another_account_conflicts(service):
    """The replay is matched on the account too, not only on what was asked about.

    Two consoles are open on two accounts and each makes its own idempotency key, so the ids
    never collide in practice. They collide when something generates one badly -- a retry that
    reuses the last key, a client that derives it from the symbol -- and the failure this guards
    against is not a duplicate run but a silent swap: the Agentic console taps research and is
    handed the Demo account's run, then reads its conclusion as being about real money. A
    refusal is the only safe answer, because the request that arrived and the run on record
    disagree about which book they are about.
    """
    request_id = str(uuid.uuid4())
    demo = service.start_research("VTI", bucket="core", scope="demo", purpose="candidate",
                                  request_id=request_id)
    with pytest.raises(ServiceConflict, match="different request"):
        service.start_research("VTI", bucket="micro_live", scope="agentic", purpose="candidate",
                               request_id=request_id)
    with pytest.raises(ServiceConflict, match="different request"):
        service.start_research("VTI", bucket="sandbox", scope="demo", purpose="candidate",
                               request_id=request_id)
    # Nothing was started for the other books, and the one run on record is still the Demo one.
    assert [j["run_key"] for j in service.research_jobs()] == [demo["run_key"]]
    assert service.research_run(demo["run_key"])["bucket"] == "core"


def test_the_scope_must_hold_the_bucket_and_a_holding_purpose_needs_the_holding(service):
    with pytest.raises(ServiceError, match="does not hold bucket"):
        service.start_research("VTI", bucket="micro_live", scope="demo")
    with pytest.raises(ServiceError, match="unknown purpose"):
        service.start_research("VTI", bucket="core", purpose="speculate")
    # The example ledgers hold nothing: a holding review is refused for either book, a
    # candidate study is not. Once the real-money ledger holds the symbol, the review goes through.
    with pytest.raises(ServiceError, match="not held in micro_live; use purpose=candidate"):
        service.start_research("AAPL", bucket="micro_live", purpose="exit_review")
    with pytest.raises(ServiceError, match="not held in core"):
        service.start_research("VTI", bucket="core", purpose="review_holding")
    assert service.start_research("AAPL", bucket="micro_live", purpose="candidate")["purpose"] == "candidate"

    import yaml

    profiles = service.profiles()
    ledger = profiles.project_dir / str(profiles.risk["micro_live_execution"]["ledger_file"])
    ledger.write_text(yaml.safe_dump({
        "as_of": "2026-09-16", "currency": "USD", "buckets": {"micro_live": {"cash_usd": 40.0}},
        "positions": [{"symbol": "VTI", "bucket": "micro_live", "quantity": 0.2,
                       "cost_basis_per_share": 370.0}], "recent_opens": []}), encoding="utf-8")
    held = service.start_research("VTI", bucket="micro_live", scope="agentic", purpose="review_holding")
    assert held["purpose"] == "review_holding" and held["scope"] == "agentic"
