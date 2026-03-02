"""The forward record: what it counts, what it excludes, and what it refuses to claim.

Offline. Prices are stubbed so a market holiday or a data outage cannot make these pass or fail
for reasons that have nothing to do with the arithmetic being checked.
"""
from __future__ import annotations

import shutil

import pytest

from agentic_trading import evaluation
from agentic_trading.service import Service
from agentic_trading.state_db import StateDB
from tests.conftest import ROOT, copy_example_ledger


@pytest.fixture
def study(tmp_path, monkeypatch):
    """A started study over a two-symbol benchmark, with prices under the test's control."""
    cfg = tmp_path / "config"
    shutil.copytree(ROOT / "config", cfg)
    (tmp_path / "state").mkdir()
    (tmp_path / "runs").mkdir()
    copy_example_ledger("portfolio.example.yaml", tmp_path / "state" / "portfolio.yaml")

    prices = {"VTI": {"2026-09-01": 100.0}, "BND": {"2026-09-01": 50.0}}
    monkeypatch.setattr(evaluation, "_price",
                        lambda symbol, on: (prices[symbol][on], on))

    service = Service(cfg)
    db = StateDB(tmp_path / "state" / "proposals.json")
    profiles = service.profiles()
    evaluation.start(db, profiles, on="2026-09-01", equity_usd=3000.0, net_deposits_usd=3000.0)
    return db, profiles, prices


def mark(db, profiles, prices, *, on, equity, deposits, vti, bnd):
    prices["VTI"][on] = vti
    prices["BND"][on] = bnd
    return evaluation.snapshot(db, profiles, on=on, equity_usd=equity, net_deposits_usd=deposits)


def test_a_deposit_is_not_a_return_on_either_side(study):
    """Both sides receive the same money on the same day; neither may record it as a gain."""
    db, profiles, prices = study
    mark(db, profiles, prices, on="2026-09-02", equity=3000.0, deposits=3000.0, vti=100.0, bnd=50.0)
    # $1,000 paid in, and not a cent of market movement
    row = mark(db, profiles, prices, on="2026-09-03", equity=4000.0, deposits=4000.0,
               vti=100.0, bnd=50.0)

    assert row["flow_usd"] == 1000.0
    assert row["strategy_equity_usd"] == 4000.0
    assert row["benchmark_equity_usd"] == pytest.approx(4000.0, abs=0.01)

    report = evaluation.report(db, profiles, research=[], jobs=[], proposals=[], fills=[])
    assert report["strategy"]["return_pct"] == 0.0
    assert report["benchmark"]["return_pct"] == 0.0
    assert report["strategy_minus_benchmark_pct_points"] == 0.0


def test_the_benchmark_receives_the_same_deposit_and_keeps_its_own_prices(study):
    db, profiles, prices = study
    mark(db, profiles, prices, on="2026-09-02", equity=3000.0, deposits=3000.0, vti=100.0, bnd=50.0)
    # the benchmark rises 10% on the equity sleeve, then takes a deposit at the higher price
    mark(db, profiles, prices, on="2026-09-03", equity=3000.0, deposits=3000.0, vti=110.0, bnd=50.0)
    row = mark(db, profiles, prices, on="2026-09-04", equity=4000.0, deposits=4000.0,
               vti=110.0, bnd=50.0)

    # 1800 in VTI grew to 1980, 1200 in BND unchanged, plus the 1000 deposit
    assert row["benchmark_equity_usd"] == pytest.approx(4180.0, abs=0.01)
    report = evaluation.report(db, profiles, research=[], jobs=[], proposals=[], fills=[])
    assert report["benchmark"]["return_pct"] == pytest.approx(6.0, abs=0.01)
    assert report["strategy"]["return_pct"] == 0.0        # the strategy only took the deposit
    assert report["strategy_minus_benchmark_pct_points"] == pytest.approx(-6.0, abs=0.01)


def test_drawdown_is_measured_on_the_deposit_adjusted_value(study):
    db, profiles, prices = study
    mark(db, profiles, prices, on="2026-09-02", equity=3000.0, deposits=3000.0, vti=100.0, bnd=50.0)
    mark(db, profiles, prices, on="2026-09-03", equity=2700.0, deposits=3000.0, vti=100.0, bnd=50.0)
    # a deposit while under water must not repair the drawdown
    mark(db, profiles, prices, on="2026-09-04", equity=3700.0, deposits=4000.0, vti=100.0, bnd=50.0)

    report = evaluation.report(db, profiles, research=[], jobs=[], proposals=[], fills=[])
    assert report["strategy"]["max_drawdown_pct"] == pytest.approx(-10.0, abs=0.01)
    assert report["strategy"]["return_pct"] == pytest.approx(-10.0, abs=0.01)


def test_a_day_that_cannot_be_priced_is_skipped_rather_than_half_recorded(study):
    db, profiles, prices = study
    prices["VTI"]["2026-09-02"] = 100.0        # BND deliberately absent
    result = evaluation.snapshot(db, profiles, on="2026-09-02", equity_usd=3000.0,
                                 net_deposits_usd=3000.0)
    assert result["recorded"] is False and any("BND" in m for m in result["missing_prices"])
    assert evaluation.report(db, profiles, research=[], jobs=[], proposals=[],
                             fills=[])["days_recorded"] == 0


def test_the_record_counts_what_the_system_did_not_do(study):
    """A record of trades alone describes only the occasions it chose to act."""
    db, profiles, prices = study
    mark(db, profiles, prices, on="2026-09-02", equity=3000.0, deposits=3000.0, vti=100.0, bnd=50.0)

    research = [
        {"trade_date": "2026-09-02", "final_action": "NO_TRADE", "llm_calls": 13,
         "elapsed_seconds": 420.0, "data_quality": "partial", "missing_information": 2},
        {"trade_date": "2026-09-02", "final_action": "BUY", "llm_calls": 11,
         "elapsed_seconds": 380.0, "data_quality": "complete", "missing_information": 0},
        {"trade_date": "2026-08-01", "final_action": "BUY", "llm_calls": 9,
         "elapsed_seconds": 300.0},                      # before the study: not counted
    ]
    jobs = [{"created_at": "2026-09-02T10:00:00+00:00", "state": "FAILED"},
            {"created_at": "2026-09-02T11:00:00+00:00", "state": "CLAUDE_LIMIT_REACHED"},
            {"created_at": "2026-09-02T12:00:00+00:00", "state": "COMPLETE"}]
    proposals = [
        {"proposal_id": "p1", "status": "REJECTED", "created_at": "2026-09-02T10:00:00+00:00",
         "approved_at": None, "reference_price": 100.0},
        {"proposal_id": "p2", "status": "EXECUTED", "created_at": "2026-09-02T10:00:00+00:00",
         "approved_at": "2026-09-02T10:30:00+00:00", "reference_price": 100.0},
    ]
    fills = [{"proposal_id": "p2", "quantity": 3.0, "price": 101.0}]

    report = evaluation.report(db, profiles, research=research, jobs=jobs,
                               proposals=proposals, fills=fills)
    decisions = report["decisions"]
    assert decisions["research_runs"] == 2                      # the August run is outside
    assert decisions["run_outcomes"] == {"NO_TRADE": 1, "BUY": 1}
    assert decisions["job_states"]["FAILED"] == 1
    assert decisions["job_states"]["CLAUDE_LIMIT_REACHED"] == 1
    assert decisions["proposal_statuses"] == {"REJECTED": 1, "EXECUTED": 1}
    assert decisions["incomplete_data_rate_pct"] == 50.0
    assert decisions["llm_calls"] == 24
    assert decisions["human_decision_minutes"]["median"] == 30.0

    execution = report["execution"]
    assert execution["fills"] == 1 and execution["traded_usd"] == 303.0
    assert execution["slippage_bps"]["median"] == 100.0       # 101 against a 100 reference


def test_a_short_record_never_presents_itself_as_evidence(study):
    db, profiles, prices = study
    mark(db, profiles, prices, on="2026-09-02", equity=3000.0, deposits=3000.0, vti=100.0, bnd=50.0)
    report = evaluation.report(db, profiles, research=[], jobs=[], proposals=[], fills=[])

    assert report["days_recorded"] == 1
    assert report["readable"] is False and report["conclusive"] is False
    assert "not evidence about the strategy" in report["caveat"]
    assert str(report["declared"]["minimum_days_for_judgement"]) in report["caveat"]


def test_changing_the_rules_mid_study_is_reported_not_hidden(study):
    db, profiles, prices = study
    mark(db, profiles, prices, on="2026-09-02", equity=3000.0, deposits=3000.0, vti=100.0, bnd=50.0)
    assert evaluation.report(db, profiles, research=[], jobs=[], proposals=[],
                             fills=[])["config_drifted"] is False

    profiles.risk["sizing"] = {"changed": True}     # the rules move after the sample started
    mark(db, profiles, prices, on="2026-09-03", equity=3000.0, deposits=3000.0, vti=100.0, bnd=50.0)

    report = evaluation.report(db, profiles, research=[], jobs=[], proposals=[], fills=[])
    assert report["config_drifted"] is True
    assert report["frozen_fingerprint"] != report["current_fingerprint"]
    assert "not homogeneous" in report["caveat"]


def test_a_declared_stop_rule_fires_on_its_own_terms(study):
    db, profiles, prices = study
    mark(db, profiles, prices, on="2026-09-02", equity=3000.0, deposits=3000.0, vti=100.0, bnd=50.0)
    mark(db, profiles, prices, on="2026-09-03", equity=2400.0, deposits=3000.0, vti=100.0, bnd=50.0)

    report = evaluation.report(db, profiles, research=[], jobs=[], proposals=[], fills=[])
    assert report["strategy"]["max_drawdown_pct"] == pytest.approx(-20.0, abs=0.01)
    assert any("drawdown" in reason for reason in report["stop_rules_triggered"])


def test_a_running_study_is_not_silently_replaced(study):
    db, profiles, _prices = study
    with pytest.raises(evaluation.EvaluationError, match="stop it before starting another"):
        evaluation.start(db, profiles, on="2026-09-02", equity_usd=3000.0, net_deposits_usd=3000.0)

    evaluation.stop(db, "superseded by a wider universe")
    restarted = evaluation.start(db, profiles, on="2026-09-01", equity_usd=3000.0,
                                 net_deposits_usd=3000.0)
    assert restarted["state"] == "RUNNING"


def test_marking_the_same_day_twice_does_not_double_count_it(study):
    db, profiles, prices = study
    mark(db, profiles, prices, on="2026-09-02", equity=3000.0, deposits=3000.0, vti=100.0, bnd=50.0)
    mark(db, profiles, prices, on="2026-09-02", equity=3100.0, deposits=3000.0, vti=100.0, bnd=50.0)

    report = evaluation.report(db, profiles, research=[], jobs=[], proposals=[], fills=[])
    assert report["days_recorded"] == 1
    assert report["strategy"]["equity_usd"] == 3100.0      # the later mark wins


# ------------------------------------------------------------------ scopes
def test_each_scope_counts_its_own_buckets_and_an_unlabelled_row_is_the_paper_books(study):
    """The paper book and the real account are different samples. A row written before buckets
    were recorded belongs to the paper book, which was the only study there was."""
    db, profiles, prices = study
    mark(db, profiles, prices, on="2026-09-02", equity=3000.0, deposits=3000.0, vti=100.0, bnd=50.0)
    research = [
        {"trade_date": "2026-09-02", "final_action": "NO_TRADE", "bucket": "core"},
        {"trade_date": "2026-09-02", "final_action": "REDUCE", "bucket": "micro_live"},
        {"trade_date": "2026-09-02", "final_action": "BUY"},                       # unlabelled
        {"trade_date": "2026-09-02", "final_action": "HOLD", "bucket": "advisory"},  # personal: nobody's
    ]
    fills = [{"proposal_id": "a", "quantity": 1.0, "price": 100.0, "bucket": "micro_live"},
             {"proposal_id": "b", "quantity": 2.0, "price": 100.0, "bucket": "core"}]

    demo = evaluation.report(db, profiles, research=research, jobs=[], proposals=[], fills=fills)
    assert demo["scope"] == "demo" and demo["buckets"] == ["core", "sandbox"]
    assert demo["account"]["account_label"] == "Demo" and demo["account"]["is_real_money"] is False
    assert demo["decisions"]["run_outcomes"] == {"NO_TRADE": 1, "BUY": 1}
    assert demo["execution"]["fills"] == 1 and demo["execution"]["traded_usd"] == 200.0

    agentic = evaluation.report(db, profiles, research=research, jobs=[], proposals=[], fills=fills,
                                scope="agentic")
    assert agentic["state"] == "NOT_STARTED" and agentic["readable"] is False
    assert agentic["decisions"]["run_outcomes"] == {"REDUCE": 1}
    assert agentic["execution"]["fills"] == 1 and agentic["execution"]["traded_usd"] == 100.0
    assert agentic["strategy"]["return_pct"] is None and agentic["benchmark"]["return_pct"] is None
    assert agentic["account"]["account_label"] == "Agentic" and "not been declared" not in agentic["caveat"]
    assert "no Agentic study has been declared" in agentic["caveat"]

    with pytest.raises(evaluation.EvaluationError, match="unknown evaluation scope"):
        evaluation.report(db, profiles, research=[], jobs=[], proposals=[], fills=[], scope="live")


def test_an_agentic_study_keeps_its_own_record_beside_the_paper_books(study):
    db, profiles, prices = study
    evaluation.start(db, profiles, on="2026-09-01", equity_usd=100.0, net_deposits_usd=100.0,
                     scope="agentic")
    prices["VTI"]["2026-09-02"], prices["BND"]["2026-09-02"] = 110.0, 50.0
    evaluation.snapshot(db, profiles, on="2026-09-02", equity_usd=100.0, net_deposits_usd=100.0,
                        scope="agentic")

    demo = evaluation.report(db, profiles, research=[], jobs=[], proposals=[], fills=[])
    agentic = evaluation.report(db, profiles, research=[], jobs=[], proposals=[], fills=[], scope="agentic")
    assert demo["days_recorded"] == 0 and agentic["days_recorded"] == 1
    assert demo["study_id"] != agentic["study_id"]
    assert agentic["state"] == "RUNNING" and agentic["strategy"]["equity_usd"] == 100.0
    assert evaluation.running(db, "demo") and evaluation.running(db, "agentic")
    # Stopping one leaves the other running.
    evaluation.stop(db, "enough", scope="agentic")
    assert evaluation.running(db, "demo") and not evaluation.running(db, "agentic")


def test_the_sample_says_where_sessions_went_unmarked_and_when_it_fell_behind(study):
    """Counted in exchange sessions: 2026-09-07 is Labor Day, and a weekend is not a gap."""
    db, profiles, prices = study
    mark(db, profiles, prices, on="2026-09-02", equity=3000.0, deposits=3000.0, vti=100.0, bnd=50.0)
    mark(db, profiles, prices, on="2026-09-08", equity=3000.0, deposits=3000.0, vti=100.0, bnd=50.0)

    sample = evaluation.report(db, profiles, research=[], jobs=[], proposals=[], fills=[],
                               today="2026-09-08")["sample"]
    assert sample["started_on"] == "2026-09-01" and sample["last_snapshot_on"] == "2026-09-08"
    assert sample["days_recorded"] == 2 and sample["basis"] == "sessions"
    assert sample["gaps"] == [{"from": "2026-09-02", "to": "2026-09-08", "missing_sessions": 2,
                               "reason": "no snapshot"}]
    assert sample["stale"] is False and sample["sessions_behind"] == 0

    late = evaluation.report(db, profiles, research=[], jobs=[], proposals=[], fills=[],
                             today="2026-09-18")["sample"]
    assert late["stale"] is True and late["sessions_behind"] == 8


def test_route_quality_reports_only_what_the_record_can_measure():
    research = [
        {"bucket": "core", "elapsed_seconds": 1800, "llm_calls": 20, "data_quality": "good",
         "missing_information": 0, "created_at": "2026-09-02T10:00:00+00:00"},
        {"bucket": "core", "elapsed_seconds": 1900, "llm_calls": 22, "data_quality": "partial",
         "missing_information": 2, "created_at": "2026-09-03T10:00:00+00:00"},
        {"bucket": "advisory", "elapsed_seconds": 60, "llm_calls": 1, "created_at": "2026-09-03T11:00:00+00:00"},
    ]
    jobs = [
        {"state": "COMPLETE", "created_at": "2026-09-02T10:00:00+00:00"},
        {"state": "TIMED_OUT", "created_at": "2026-09-03T10:00:00+00:00"},
        {"state": "CLAUDE_LIMIT_REACHED", "created_at": "2026-09-03T12:00:00+00:00", "limit_recoveries": 1},
        {"state": "COMPLETE", "created_at": "2026-09-03T11:00:00+00:00", "task": {"kind": "chat"}},
    ]
    report = evaluation.route_quality(research, jobs)
    by = {row["route"]: row for row in report["routes"]}
    graph = by["tradingagents"]
    assert graph["runs"] == 3 and graph["complete"] == 1 and graph["timed_out"] == 1 and graph["limit_hits"] == 1
    assert graph["median_elapsed_seconds"] == 1850.0 and graph["llm_calls_total"] == 42
    assert graph["evidence_check_pass_rate_pct"] == 50.0
    assert graph["fact_accuracy_pct"] is None and graph["human_edit_rate_pct"] is None
    assert graph["evaluated"] == {"timing": True, "calls": True, "failures": True, "format": False,
                                  "evidence": True, "facts": False, "edits": False}
    assistant = by["assistant"]
    assert assistant["runs"] == 1 and assistant["evidence_check_pass_rate_pct"] is None
    assert assistant["evaluated"]["evidence"] is False
    assert report["since"] == "2026-09-02" and "null means not evaluated" in report["note"]
    # A record that names its route is believed over the bucket rule; without one, a personal
    # analysis ran the graph and a chat ran the assistant, which is what the code did.
    assert evaluation.route_of({"bucket": "advisory", "route": {"name": "skill:thesis-tracker"}}) == "skill:thesis-tracker"
    assert evaluation.route_of({"bucket": "advisory", "task_kind": "analysis"}) == "tradingagents"
    assert evaluation.route_of({"task": {"kind": "analysis"}}) == "tradingagents"
    assert evaluation.route_of({"task": {"kind": "shortlist"}}) == "assistant"
