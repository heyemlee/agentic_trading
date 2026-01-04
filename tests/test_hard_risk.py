"""Hard Risk Layer: the six required validation situations, plus ladder mechanics. Fully offline."""

import json

import pytest

from conservative_trading.portfolio import PortfolioError, build_snapshot, load_portfolio
from conservative_trading.risk.ladder import LadderState, LadderStates, evaluate_ladders
from conservative_trading.scenarios import (
    TRADE_DATE,
    build_scenarios,
    identity_lookup,
    make_state,
    price_lookup,
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


def test_sandbox_ladder_halts_at_minus_20(profiles):
    ladders = LadderStates(LadderState(3000.0), LadderState(2500.0), LadderState(500.0))
    evaluate_ladders(ladders, profiles, "2026-09-01", 2900.0, 2500.0, 400.0)   # sandbox -20%, total -3.3%
    assert ladders.sandbox.level == "HALT" and ladders.total.level == "NORMAL"
