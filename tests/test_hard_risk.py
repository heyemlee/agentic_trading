"""Hard Risk Layer: the six required validation situations, plus ladder mechanics. Fully offline."""

import json

import pytest

from agentic_trading.evidence import collect_evidence
from agentic_trading.portfolio import (
    PortfolioError,
    build_snapshot,
    classify_instrument,
    load_portfolio,
)
from agentic_trading.risk import HardRiskEngine, Proposal
from agentic_trading.risk.ladder import LadderState, LadderStates, evaluate_ladders
from agentic_trading.scenarios import (
    TRADE_DATE,
    build_scenarios,
    identity_lookup,
    make_state,
    price_lookup,
    regime,
    run_scenario,
)


@pytest.mark.parametrize("scenario", build_scenarios(), ids=lambda s: s.key)
def test_validation_scenario(scenario, profiles):
    outcome = run_scenario(scenario, profiles)
    assert outcome.verdict == scenario.expect_verdict, (scenario.title, outcome.failed)
    assert set(scenario.expect_rules_failed) <= set(outcome.failed), (scenario.title, outcome.failed)
    if outcome.verdict != "APPROVE":
        assert outcome.notional == 0.0 and outcome.final_action in ("NO_TRADE", "REDUCE", "SELL")


def test_acceptable_buy_is_sized_from_the_baseline(profiles):
    sc = next(s for s in build_scenarios() if s.key == "S1")
    outcome = run_scenario(sc, profiles)
    assert outcome.verdict == "APPROVE" and outcome.final_action == "BUY"
    assert outcome.notional == pytest.approx(2500 * 0.35, abs=0.01)  # VTI baseline 35% of core


@pytest.mark.parametrize('action,quantity', [('SELL', 2.0), ('REDUCE', 1.0)])
def test_risk_engine_generates_nonzero_exit_size(profiles, action, quantity):
    from agentic_trading.risk import HardRiskEngine, Proposal
    from agentic_trading.scenarios import make_ladders, regime

    state = make_state(core_cash=1700, positions=[
        {'symbol': 'VTI', 'bucket': 'core', 'quantity': 2, 'cost_basis_per_share': 370}])
    snap = build_snapshot(state, profiles, TRADE_DATE, price_lookup, identity_lookup)
    ladders = make_ladders(snap.total_equity, snap.buckets['core'].equity,
                           snap.buckets['sandbox'].equity, profiles)
    prop = Proposal('VTI', 'core', action, 'normal', 80, TRADE_DATE, snap.buckets['core'].positions[0].cls)
    result = HardRiskEngine(profiles).evaluate(prop, snap, ladders, regime())
    assert result.verdict == 'APPROVE'
    assert result.proposed_quantity == quantity
    assert result.proposed_notional_usd == pytest.approx(quantity * price_lookup('VTI', TRADE_DATE)[0])


def test_defaults_used_are_recorded(profiles):
    run_scenario(build_scenarios()[0], profiles)
    assert any("baseline_allocation_pct_of_core" in d for d in profiles.defaults_used)
    assert any("position_limits_pct_of_core" in d for d in profiles.defaults_used)


def test_sandbox_never_touches_core_cash(profiles):
    state = make_state(core_cash=2500.0, sandbox_cash=0.0)
    snap = build_snapshot(state, profiles, TRADE_DATE, price_lookup, identity_lookup)
    assert snap.buckets["sandbox"].cash_usd == 0.0 and snap.buckets["core"].cash_usd == 2500.0
    sc = next(s for s in build_scenarios() if s.key == "S3a")
    outcome = run_scenario(sc, profiles)
    assert "HR07" in outcome.failed


def test_one_symbol_one_bucket_is_enforced_at_load(tmp_path):
    p = tmp_path / "portfolio.yaml"
    p.write_text(
        "as_of: 2026-09-02\nbuckets:\n  core: {cash_usd: 100}\n  sandbox: {cash_usd: 100}\n"
        "positions:\n  - {symbol: VTI, bucket: core, quantity: 1, cost_basis_per_share: 300}\n"
        "  - {symbol: VTI, bucket: sandbox, quantity: 1, cost_basis_per_share: 300}\n"
    )
    with pytest.raises(PortfolioError, match="one symbol, one bucket"):
        load_portfolio(p)


def test_ladder_escalates_immediately_and_deescalates_with_hysteresis(profiles):
    ladders = LadderStates(LadderState(3000.0), LadderState(2500.0), LadderState(500.0))
    evaluate_ladders(ladders, profiles, "2026-09-01", 2790.0, 2325.0, 465.0)   # -7.0%
    assert ladders.total.level == "RISK_REDUCTION"
    evaluate_ladders(ladders, profiles, "2026-09-02", 2850.0, 2375.0, 475.0)   # -5.0%: inside hysteresis, stays
    assert ladders.total.level == "RISK_REDUCTION"
    evaluate_ladders(ladders, profiles, "2026-09-03", 2860.0, 2384.0, 476.0)   # -4.67% > -5: step down one level
    assert ladders.total.level == "WARNING"
    evaluate_ladders(ladders, profiles, "2026-09-04", 3010.0, 2510.0, 500.0)   # new high: clears, HWM moves up
    assert ladders.total.level == "NORMAL" and ladders.total.high_water_mark_usd == 3010.0


def test_hard_halt_is_sticky_until_manual_reset(profiles):
    ladders = LadderStates(LadderState(3000.0), LadderState(2500.0), LadderState(500.0))
    evaluate_ladders(ladders, profiles, "2026-09-01", 2700.0, 2250.0, 450.0)   # -10%
    assert ladders.total.level == "HARD_HALT" and ladders.total.manual_reset_required
    evaluate_ladders(ladders, profiles, "2026-09-02", 3000.0, 2500.0, 500.0)   # full recovery, still halted
    assert ladders.total.level == "HARD_HALT"
    assert json.dumps(ladders.as_dict())  # serialisable


def test_a_micro_live_run_never_judges_the_conceptual_ladders(profiles):
    """A snapshot with one bucket may only move that bucket's ladder.

    The Micro-Live ledger carries micro_live and nothing else. Standing a zero in for the
    conceptual buckets it was never shown made a $100 account read as -96.7% against the
    paper book's $3,000 high-water mark, and latched EMERGENCY and HALT on portfolios that
    had never held a position.
    """
    ladders = LadderStates(LadderState(3000.0), LadderState(2500.0), LadderState(500.0),
                           LadderState(100.0))
    evaluate_ladders(ladders, profiles, "2026-09-08", None, None, None, micro_live_equity=100.0)
    for absent in (ladders.total, ladders.core, ladders.sandbox):
        assert absent.level == "NORMAL"
        assert not absent.manual_reset_required
        assert absent.drawdown_pct == 0.0
        assert absent.history == []
    assert ladders.micro_live.level == "NORMAL"
    # and the ladder it was actually given still works
    evaluate_ladders(ladders, profiles, "2026-09-09", None, None, None, micro_live_equity=75.0)
    assert ladders.micro_live.level == "HALT" and ladders.total.level == "NORMAL"


def test_sandbox_ladder_halts_at_minus_20(profiles):
    ladders = LadderStates(LadderState(3000.0), LadderState(2500.0), LadderState(500.0))
    evaluate_ladders(ladders, profiles, "2026-09-01", 2900.0, 2500.0, 400.0)   # sandbox -20%, total -3.3%
    assert ladders.sandbox.level == "HALT" and ladders.total.level == "NORMAL"


def _opening(profiles, *, bucket="core", identity=None, evidence=True, missing=()):
    cls = classify_instrument("VTI", profiles, lambda _: identity or {"quote_type": "ETF"})
    return Proposal(
        "VTI", bucket, "BUY", "small", 80, TRADE_DATE, cls, data_quality="good",
        missing_information=list(missing), evidence=(collect_evidence(
            "VTI", TRADE_DATE, price_lookup=price_lookup) if evidence else {}),
    )


def test_sandbox_open_is_blocked_by_total_portfolio_warning(profiles):
    state = make_state()
    snap = build_snapshot(state, profiles, TRADE_DATE, price_lookup, identity_lookup)
    ladders = LadderStates(LadderState(3000, level="WARNING"), LadderState(2500), LadderState(500))
    result = HardRiskEngine(profiles).evaluate(_opening(profiles, bucket="sandbox"), snap, ladders, regime())
    failed = {rule.rule_id for rule in result.failed_rules}
    assert result.verdict == "REJECT" and "HR02" in failed
    assert "total ladder level WARNING" in next(r.detail for r in result.rules if r.rule_id == "HR02")


@pytest.mark.parametrize("missing_evidence,missing_info,rule_id", [
    (True, (), "HR05c"),
    (False, ("current fund holdings are unavailable",), "HR05d"),
])
def test_open_requires_verifiable_evidence_and_no_material_gaps(
        profiles, missing_evidence, missing_info, rule_id):
    state = make_state()
    snap = build_snapshot(state, profiles, TRADE_DATE, price_lookup, identity_lookup)
    ladders = LadderStates(LadderState(3000), LadderState(2500), LadderState(500))
    prop = _opening(profiles, evidence=not missing_evidence, missing=missing_info)
    result = HardRiskEngine(profiles).evaluate(prop, snap, ladders, regime())
    assert result.verdict == "REJECT" and rule_id in {r.rule_id for r in result.failed_rules}


@pytest.mark.parametrize("identity,expected_flag", [
    ({"quote_type": "ETF", "leveraged": True}, "leveraged"),
    ({"quote_type": "ETF", "inverse": True}, "inverse"),
    ({"quote_type": "ETF", "name": "Example Single-Stock Daily ETF"}, "single_stock"),
    ({"quote_type": "ETF", "category": "Digital Assets"}, "crypto_etf"),
    ({"quote_type": "ETF", "category": "Managed Futures"}, "commodity_futures"),
    ({"quote_type": "ETF", "category": "Derivative Income"}, "covered_call"),
])
def test_excluded_etf_structures_fail_closed(profiles, identity, expected_flag):
    prop = _opening(profiles, identity=identity)
    assert expected_flag in prop.cls.product_flags
    allowed, detail = HardRiskEngine(profiles)._permission(prop)
    assert allowed is False and expected_flag in detail


def test_unknown_etf_is_not_treated_as_a_plain_safe_etf(profiles):
    cls = classify_instrument("MYSTERY", profiles, lambda _: {"quote_type": "ETF"})
    assert cls.instrument_type == "etf" and cls.identity_verified is False
    prop = Proposal("MYSTERY", "sandbox", "BUY", "small", 80, TRADE_DATE, cls,
                    data_quality="good", evidence={})
    allowed, detail = HardRiskEngine(profiles)._permission(prop)
    assert allowed is False and "unverified" in detail
