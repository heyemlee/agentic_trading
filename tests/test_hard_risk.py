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
from agentic_trading.risk_scenarios import (
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
    from agentic_trading.risk_scenarios import make_ladders, regime

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


# -- HR05e: web-sourced evidence ----------------------------------------------------
#
# The rule exists to bound how stale this system's *reading* of a page is, and to refuse a web
# claim that arrived without provenance. It must be invisible to the yfinance-only path: every
# scenario above has no web_sources, so HR05e passing there is the property that keeps the
# existing evidence path provably unchanged.

def _evaluate(profiles, *, evidence=None, strategy="long_only_diversified"):
    from agentic_trading.risk_scenarios import make_ladders

    snap = build_snapshot(make_state(core_cash=2500.0), profiles, TRADE_DATE, price_lookup,
                          identity_lookup)
    ladders = make_ladders(snap.total_equity, snap.buckets["core"].equity,
                           snap.buckets["sandbox"].equity, profiles)
    prop = Proposal(ticker="VTI", bucket="core", action="BUY", size_hint="normal", confidence=80,
                    trade_date=TRADE_DATE, cls=classify_instrument("VTI", profiles, identity_lookup),
                    data_quality="good", strategy=strategy,
                    evidence=evidence if evidence is not None else collect_evidence(
                        "VTI", TRADE_DATE, price_lookup=price_lookup))
    engine = HardRiskEngine(profiles, halt_file="/nonexistent/HALT")
    return engine.evaluate(prop, snap, ladders, regime())


def test_hr05e_passes_and_is_recorded_when_there_is_no_web_evidence(profiles):
    result = _evaluate(profiles)
    rule = next(r for r in result.rules if r.rule_id == "HR05e")
    assert rule.passed and "no web-sourced evidence" in rule.detail
    assert "HR05e" not in [r.rule_id for r in result.failed_rules]
    assert result.verdict == "APPROVE", [r.rule_id for r in result.failed_rules]


def test_hr05c_still_reports_the_price_provenance_and_not_the_web_provenance(profiles):
    hr05c = next(r for r in _evaluate(profiles).rules if r.rule_id == "HR05c")
    assert "TradingAgents daily OHLCV" in hr05c.detail
    assert "web" not in hr05c.detail.lower()


def test_hr05e_blocks_the_proposal_when_a_web_source_is_stale(profiles):
    evidence = collect_evidence("VTI", TRADE_DATE, price_lookup=price_lookup)
    evidence["web_sources"] = [{
        "tool": "mcp__ta__fetch_url", "url": "https://sec.gov/a", "domain": "sec.gov",
        "retrieved_at": "2020-01-01T00:00:00+00:00", "sha256": "a" * 64}]
    result = _evaluate(profiles, evidence=evidence)
    assert "HR05e" in [r.rule_id for r in result.failed_rules]
    assert result.verdict == "REJECT" and result.proposed_notional_usd == 0.0


def test_hr05e_refuses_a_web_claim_that_arrived_without_provenance(profiles):
    from agentic_trading.evidence import check_web_sources

    ok, why = check_web_sources({"web_sources": [
        {"tool": "mcp__ta__fetch_url", "requested": "https://sec.gov/a"}]}, 1440)
    assert not ok, why
    assert "did not come back through the allowlisted fetch tool" in why


def test_the_medium_window_narrows_for_an_event_driven_thesis(profiles):
    engine = HardRiskEngine(profiles)
    slow = Proposal(ticker="VTI", bucket="core", action="BUY", size_hint="normal", confidence=80,
                    trade_date=TRADE_DATE, cls=classify_instrument("VTI", profiles, identity_lookup),
                    strategy="long_only_diversified")
    fast = Proposal(ticker="VTI", bucket="core", action="BUY", size_hint="normal", confidence=80,
                    trade_date=TRADE_DATE, cls=classify_instrument("VTI", profiles, identity_lookup),
                    strategy="event_driven")
    assert engine._web_evidence_window(slow) == 1440
    assert engine._web_evidence_window(fast) == 60


# -- the real-money bucket's diversification rules ----------------------------------
#
# micro_live took the generic non-core branch, which carried one max-positions check and
# nothing else. The bucket that deploys actual capital therefore had no exposure-group check,
# no combined-stock or sector cap and — the one that matters — no stock eligibility screen.
# These tests pin that the rules now apply there, and that their absence fails closed rather
# than reading as permission.

MICRO = "micro_live"


def _micro(profiles, *, cash=100.0, positions=(), ladder_equity=100.0):
    from agentic_trading.portfolio import PortfolioState
    from agentic_trading.risk.ladder import LadderState, LadderStates, evaluate_ladders

    state = PortfolioState(as_of=TRADE_DATE, buckets={MICRO: {"cash_usd": cash}},
                           positions=[dict(p) for p in positions], recent_opens=[])
    snap = build_snapshot(state, profiles, TRADE_DATE, price_lookup, identity_lookup)
    ladders = LadderStates(LadderState(3000.0), LadderState(2500.0), LadderState(500.0),
                           LadderState(100.0))
    evaluate_ladders(ladders, profiles, TRADE_DATE, None, None, None,
                     micro_live_equity=ladder_equity)
    return snap, ladders


def _micro_result(profiles, ticker, *, action="BUY", size="normal", stock_info=None, **kw):
    # "small" is 20% of a $100 bucket, which is below sizing.min_order_usd, so the engine
    # returns NO_ACTION before it reaches any of the rules under test.
    from agentic_trading.risk_scenarios import GOOD_STOCK_INFO

    snap, ladders = _micro(profiles, **kw)
    prop = Proposal(ticker=ticker, bucket=MICRO, action=action, size_hint=size, confidence=85,
                    trade_date=TRADE_DATE,
                    cls=classify_instrument(ticker, profiles, identity_lookup),
                    data_quality="good", strategy="long_only_diversified",
                    evidence=collect_evidence(ticker, TRADE_DATE, price_lookup=price_lookup))
    from agentic_trading.risk_scenarios import earnings_lookup

    engine = HardRiskEngine(profiles, halt_file="/nonexistent/HALT",
                            stock_info_lookup=lambda s: (GOOD_STOCK_INFO if stock_info is None
                                                         else stock_info),
                            earnings_lookup=earnings_lookup)
    return engine.evaluate(prop, snap, ladders, regime())


def _ids(result):
    return {r.rule_id for r in result.rules}


def test_real_money_now_runs_the_stock_eligibility_screen(profiles):
    result = _micro_result(profiles, "AAPL")
    assert "HR04b" in _ids(result), "an individual stock in the real-money bucket must be screened"
    assert next(r for r in result.rules if r.rule_id == "HR04b").passed


def test_real_money_refuses_a_stock_that_fails_the_screen(profiles):
    thin = {"marketCap": 4.0e8, "freeCashflow": -1.0e8, "netIncomeToCommon": -5.0e7,
            "debtToEquity": 400.0, "currentRatio": 0.4}
    result = _micro_result(profiles, "AAPL", stock_info=thin)
    assert "HR04b" in {r.rule_id for r in result.failed_rules}
    assert result.verdict == "REJECT" and result.proposed_notional_usd == 0.0


def test_real_money_fails_closed_when_the_screen_is_not_required(profiles):
    profiles.risk["buckets"][MICRO].pop("require_stock_eligibility_screen")
    result = _micro_result(profiles, "AAPL")
    assert result.verdict == "REJECT"
    assert any("no eligibility screen" in r for r in result.fail_closed_reasons), \
        result.fail_closed_reasons


def test_real_money_fails_closed_without_concentration_limits(profiles):
    profiles.risk["buckets"][MICRO].pop("concentration_pct_of_allocation")
    result = _micro_result(profiles, "VTI")
    assert result.verdict == "REJECT"
    assert any("concentration_pct_of_allocation" in r for r in result.fail_closed_reasons), \
        result.fail_closed_reasons


def test_real_money_applies_the_per_class_cap_on_top_of_the_flat_cap(profiles):
    # The flat cap is 40% of the bucket; a single name is capped at 10%. The stricter one wins.
    stock = _micro_result(profiles, "AAPL", size="normal")
    cap = next(r for r in stock.rules if r.rule_id == "HR08")
    assert "cap 10.0%" in cap.detail, cap.detail
    etf = _micro_result(profiles, "VTI", size="normal")
    assert "cap 40.0%" in next(r for r in etf.rules if r.rule_id == "HR08").detail


def test_real_money_refuses_two_etfs_from_one_exposure_group(profiles):
    # VTI and VOO are the same bet on the same index; holding both is not diversification.
    held = [{"symbol": "VTI", "bucket": MICRO, "quantity": 0.1, "cost_basis_per_share": 370.0}]
    result = _micro_result(profiles, "VOO", positions=held, cash=60.0)
    assert "HR09" in {r.rule_id for r in result.failed_rules}, [r.rule_id for r in result.failed_rules]


def test_adding_to_an_existing_real_money_holding_is_not_counted_as_a_new_position(profiles):
    # HR11c looked for the ticker in "sandbox" even for a micro_live proposal, so topping up an
    # existing holding counted as a fourth position and was refused at the three-position cap.
    # Small enough that topping VTI up to its target still clears sizing.min_order_usd.
    held = [{"symbol": sym, "bucket": MICRO, "quantity": q, "cost_basis_per_share": c}
            for sym, q, c in (("VTI", 0.01, 370.0), ("BND", 0.02, 71.0), ("SGOV", 0.01, 100.0))]
    result = _micro_result(profiles, "VTI", action="ADD", positions=held, cash=40.0)
    positions = next(r for r in result.rules if r.rule_id == "HR11c")
    assert positions.passed, positions.detail
    assert "3 positions" in positions.detail


def test_one_symbol_one_bucket_now_looks_at_every_other_bucket(profiles):
    from agentic_trading.portfolio import PortfolioState
    from agentic_trading.risk.ladder import LadderState, LadderStates, evaluate_ladders

    # Previously a micro_live proposal only checked sandbox, so the same symbol held in core
    # passed unnoticed.
    state = PortfolioState(as_of=TRADE_DATE, buckets={
        "core": {"cash_usd": 2000.0}, "sandbox": {"cash_usd": 500.0}, MICRO: {"cash_usd": 100.0}},
        positions=[{"symbol": "VTI", "bucket": "core", "quantity": 1.0,
                    "cost_basis_per_share": 370.0}], recent_opens=[])
    snap = build_snapshot(state, profiles, TRADE_DATE, price_lookup, identity_lookup)
    ladders = LadderStates(LadderState(3000.0), LadderState(2500.0), LadderState(500.0),
                           LadderState(100.0))
    evaluate_ladders(ladders, profiles, TRADE_DATE, snap.total_equity,
                     snap.buckets["core"].equity, snap.buckets["sandbox"].equity,
                     micro_live_equity=snap.buckets[MICRO].equity)
    prop = Proposal(ticker="VTI", bucket=MICRO, action="BUY", size_hint="normal", confidence=85,
                    trade_date=TRADE_DATE, cls=classify_instrument("VTI", profiles, identity_lookup),
                    data_quality="good", strategy="long_only_diversified",
                    evidence=collect_evidence("VTI", TRADE_DATE, price_lookup=price_lookup))
    result = HardRiskEngine(profiles, halt_file="/nonexistent/HALT").evaluate(
        prop, snap, ladders, regime())
    overlap = next(r for r in result.rules if r.rule_id == "HR07b")
    assert not overlap.passed and "held in core" in overlap.detail, overlap.detail
