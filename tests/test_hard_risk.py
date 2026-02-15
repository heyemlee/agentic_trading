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
    """Hard Risk and revalidation read the window from one definition, so they cannot disagree."""
    from agentic_trading.proposals import web_evidence_window

    assert web_evidence_window(profiles, "long_only_diversified") == 1440
    assert web_evidence_window(profiles, "event_driven") == 60
    assert web_evidence_window(profiles, None) == 1440


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
    # Funded so live equity supports a normal-sized top-up: with sizing_basis live_equity the
    # denominator is what the account holds, not a number in the config file.
    result = _micro_result(profiles, "VTI", action="ADD", positions=held, cash=100.0)
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


# -- HR20: order-rate limits --------------------------------------------------------
#
# max_orders_per_week and no_same_day_round_trips were declared in config with nothing reading
# them. They differ from HR12 in what they count: HR12 counts positions opened, so it is blind
# to an add or a sell, which is most of what an order-rate limit is about.

def _with_orders(profiles, orders, *, ticker="VTI", action="BUY", bucket="core", held=()):
    from agentic_trading.portfolio import PortfolioState
    from agentic_trading.risk_scenarios import make_ladders

    state = PortfolioState(
        as_of=TRADE_DATE, buckets={"core": {"cash_usd": 2000.0}, "sandbox": {"cash_usd": 500.0}},
        positions=[dict(p) for p in held], recent_opens=[], applied_orders=orders)
    snap = build_snapshot(state, profiles, TRADE_DATE, price_lookup, identity_lookup)
    ladders = make_ladders(snap.total_equity, snap.buckets["core"].equity,
                           snap.buckets["sandbox"].equity, profiles)
    prop = Proposal(ticker=ticker, bucket=bucket, action=action, size_hint="small", confidence=85,
                    trade_date=TRADE_DATE, cls=classify_instrument(ticker, profiles, identity_lookup),
                    data_quality="good", strategy="long_only_diversified",
                    evidence=collect_evidence(ticker, TRADE_DATE, price_lookup=price_lookup))
    return HardRiskEngine(profiles, halt_file="/nonexistent/HALT").evaluate(
        prop, snap, ladders, regime())


def _order(symbol, side, when, bucket="core"):
    return {"symbol": symbol, "bucket": bucket, "side": side, "quantity": 1.0, "notional": 100.0,
            "events": [{"trade_date": when, "quantity": 1.0, "price": 100.0, "notional": 100.0}]}


def test_order_rate_counts_adds_and_sells_not_just_opens(profiles):
    profiles.risk["buckets"]["core"]["activity_limits"]["max_orders_per_week"] = 3
    orders = {"a": _order("BND", "buy", "2026-08-31"),
              "b": _order("SGOV", "sell", "2026-09-01"),
              "c": _order("BND", "buy", "2026-09-02")}
    result = _with_orders(profiles, orders)
    hr20 = next(r for r in result.rules if r.rule_id == "HR20")
    assert not hr20.passed and "3 order(s)" in hr20.detail, hr20.detail


def test_orders_outside_the_trailing_week_do_not_count(profiles):
    profiles.risk["buckets"]["core"]["activity_limits"]["max_orders_per_week"] = 2
    orders = {"old": _order("BND", "buy", "2026-08-20"), "recent": _order("BND", "buy", "2026-09-01")}
    result = _with_orders(profiles, orders)
    hr20 = next(r for r in result.rules if r.rule_id == "HR20")
    assert hr20.passed and "1 order(s)" in hr20.detail, hr20.detail


def test_the_rule_is_absent_when_the_bucket_declares_no_order_cap(profiles):
    profiles.risk["buckets"]["core"]["activity_limits"].pop("max_orders_per_week", None)
    result = _with_orders(profiles, {"a": _order("BND", "buy", "2026-09-02")})
    assert "HR20" not in {r.rule_id for r in result.rules}


def test_buying_back_what_was_sold_today_is_a_round_trip(profiles):
    result = _with_orders(profiles, {"s": _order("VTI", "sell", TRADE_DATE)})
    hr20b = next(r for r in result.rules if r.rule_id == "HR20b")
    assert not hr20b.passed and "round trip" in hr20b.detail


def test_a_sale_on_an_earlier_day_is_not_a_round_trip(profiles):
    result = _with_orders(profiles, {"s": _order("VTI", "sell", "2026-09-01")})
    assert next(r for r in result.rules if r.rule_id == "HR20b").passed


def test_selling_something_bought_today_is_never_a_round_trip(profiles):
    # One-directional on purpose: buying back hours later is the churn the rule targets, while
    # selling this morning's purchase is risk reduction. Reductions never reach this code.
    held = [{"symbol": "VTI", "bucket": "core", "quantity": 2.0, "cost_basis_per_share": 370.0}]
    result = _with_orders(profiles, {"b": _order("VTI", "buy", TRADE_DATE)},
                          action="SELL", held=held)
    assert "HR20b" not in {r.rule_id for r in result.rules}
    assert result.verdict == "APPROVE", [r.rule_id for r in result.failed_rules]


def test_the_broker_snapshot_keeps_the_order_history(tmp_path):
    # snapshot_from_broker rebuilt the state without applied_orders, so every order-rate limit
    # counted zero on the execution-time revalidation pass and silently did not bind.
    from agentic_trading.broker import SimulatedBroker
    from agentic_trading.execution_scenarios import _ledger, _profiles
    from agentic_trading.portfolio import load_portfolio, snapshot_from_broker
    from agentic_trading.risk_scenarios import PRICES

    profiles = _profiles(tmp_path)
    path = _ledger(tmp_path)
    ledger = load_portfolio(path)
    ledger.applied_orders = {"ct-1": _order("VTI", "buy", TRADE_DATE)}
    broker = SimulatedBroker(prices=dict(PRICES), cash=3000.0)
    snap = snapshot_from_broker(broker.get_account(), broker.get_positions(), ledger, profiles,
                                TRADE_DATE)
    assert snap.state.applied_orders == ledger.applied_orders


def test_a_broker_position_without_an_average_price_keeps_the_ledger_cost_basis(tmp_path):
    # The Robinhood adapter reads a missing average price as 0.0, and Position requires a cost
    # basis above zero, so revalidation raised a validation error no handler caught.
    from agentic_trading.broker.base import Account, BrokerPosition
    from agentic_trading.execution_scenarios import _ledger, _profiles
    from agentic_trading.portfolio import PortfolioError, load_portfolio, snapshot_from_broker
    from agentic_trading.risk_scenarios import PRICES

    profiles = _profiles(tmp_path)
    ledger = load_portfolio(_ledger(tmp_path, positions=[
        {"symbol": "VTI", "bucket": "core", "quantity": 2.0, "cost_basis_per_share": 250.0}]))
    account = Account(account_id="AG1", cash=3000.0, buying_power=3000.0, equity=3000.0)

    no_average = BrokerPosition("VTI", 2.0, 0.0, 2.0 * PRICES["VTI"])
    snap = snapshot_from_broker(account, [no_average], ledger, profiles, TRADE_DATE,
                                identity_lookup=identity_lookup)
    assert snap.state.positions[0].cost_basis_per_share == 250.0

    # With no market value either, there is nothing to value the position at: refuse, in the
    # error type revalidation already turns into "cannot be reconciled".
    no_value = BrokerPosition("VTI", 2.0, 0.0, 0.0)
    with pytest.raises(PortfolioError, match="without a usable price"):
        snapshot_from_broker(account, [no_value], ledger, profiles, TRADE_DATE,
                             identity_lookup=identity_lookup)


# -- sizing basis: what the percentages are percentages OF ---------------------------
#
# Every cap in the engine divides by BucketView.allocation_usd. Pointing that at live equity
# for the real-money bucket is the whole of the change: caps follow a deposit with no config
# edit, and fall by themselves after a loss.

def _alloc(profiles, bucket, cash, positions=()):
    from agentic_trading.portfolio import PortfolioState

    state = PortfolioState(as_of=TRADE_DATE, buckets={bucket: {"cash_usd": cash}},
                           positions=[dict(p) for p in positions], recent_opens=[])
    snap = build_snapshot(state, profiles, TRADE_DATE, price_lookup, identity_lookup)
    return snap.buckets[bucket]


def test_the_real_bucket_sizes_against_what_the_account_actually_holds(profiles):
    funded = _alloc(profiles, MICRO, 1000.0)
    assert funded.allocation_usd == 1000.0, "a deposit must widen the caps without a config edit"
    drawn_down = _alloc(profiles, MICRO, 620.0)
    assert drawn_down.allocation_usd == 620.0, "and a loss must narrow them with no one acting"


def test_todays_hundred_dollar_account_is_completely_unaffected(profiles):
    # The live account holds $100 and allocation_usd_max says 100, so live equity and the static
    # number agree exactly. This change alters nothing until the account is funded differently.
    assert _alloc(profiles, MICRO, 100.0).allocation_usd == 100.0


def test_equity_counts_positions_and_not_just_cash(profiles):
    held = [{"symbol": "VTI", "bucket": MICRO, "quantity": 1.0, "cost_basis_per_share": 370.0}]
    bucket = _alloc(profiles, MICRO, 600.0, held)
    assert bucket.allocation_usd == pytest.approx(600.0 + price_lookup("VTI", TRADE_DATE)[0])


def test_the_conceptual_buckets_still_size_against_their_declared_allocation(profiles):
    # core and sandbox declare no sizing_basis, so they keep the static number and their
    # scenarios are unaffected. Only the bucket holding real money opted in.
    assert _alloc(profiles, "core", 10.0).allocation_usd == 2500.0
    assert _alloc(profiles, "sandbox", 10.0).allocation_usd == 500.0


def test_a_cap_expressed_in_percent_moves_with_the_account(profiles):
    # An individual stock is capped at 10% of the bucket. That is $10 at $100 and $100 at $1,000,
    # with nothing edited in between.
    for equity, expected in ((100.0, 10.0), (1000.0, 100.0)):
        bucket = _alloc(profiles, MICRO, equity)
        cap = float(profiles.risk["buckets"][MICRO]["position_limits"]
                    ["max_pct_of_allocation_by_class"]["individual_stock"])
        assert bucket.allocation_usd * cap / 100.0 == pytest.approx(expected)


# -- HR21: risk tiers ---------------------------------------------------------------
#
# A tier is a set of instrument classes sharing one share of the account and one order cap,
# rather than a sub-account. One brokerage account holds one pool of cash, so notional envelopes
# would refuse a buy while the account plainly had the money.

def test_the_two_low_risk_classes_share_one_share_of_the_account(profiles):
    # $550 of a bond ETF is 55% of a $1,000 account. A broad equity ETF is a different class in
    # the same tier, so the tier is already near its 60% and the buy is refused — 60 each would
    # let the pair reach 120%.
    held = [{"symbol": "BND", "bucket": MICRO, "quantity": 7.657, "cost_basis_per_share": 71.83}]
    result = _micro_result(profiles, "VTI", cash=450.0, positions=held)
    tier = next(r for r in result.rules if r.rule_id == "HR21")
    assert not tier.passed and "low tier" in tier.name, tier.detail
    assert result.verdict == "REJECT"


def test_a_tier_caps_the_size_of_a_single_order(profiles):
    # 35% of a $1,000 account is $350, above the low tier's $200 ticket.
    result = _micro_result(profiles, "VTI", cash=1000.0)
    cap = next(r for r in result.rules if r.rule_id == "HR21b")
    assert not cap.passed and "$200" in cap.detail, cap.detail


def test_each_tier_carries_its_own_ticket_size(profiles):
    tiers = profiles.risk["buckets"][MICRO]["tiers"]
    assert tiers["low"]["max_order_usd"] == 200
    assert tiers["mid"]["max_order_usd"] == 80
    assert tiers["high"]["max_order_usd"] == 50
    assert [tiers[t]["max_pct_of_account"] for t in ("low", "mid", "high")] == [60, 25, 15]


def test_an_instrument_class_in_no_tier_fails_closed_for_real_money(profiles):
    profiles.risk["buckets"][MICRO]["tiers"]["high"]["classes"] = []
    result = _micro_result(profiles, "AAPL")
    assert result.verdict == "REJECT"
    assert any("belongs to no tier" in r for r in result.fail_closed_reasons), \
        result.fail_closed_reasons


def test_a_tier_that_filled_up_can_still_be_reduced(profiles):
    # The tier caps what may be bought, never what may be sold. Structural: a reduction returns
    # before these rules are reached.
    held = [{"symbol": "BND", "bucket": MICRO, "quantity": 7.657, "cost_basis_per_share": 71.83}]
    result = _micro_result(profiles, "BND", action="SELL", cash=450.0, positions=held)
    assert "HR21" not in {r.rule_id for r in result.rules}
    assert result.verdict == "APPROVE", [r.rule_id for r in result.failed_rules]


def test_the_conceptual_buckets_declare_no_tiers_and_are_unaffected(profiles):
    result = _with_orders(profiles, {}, bucket="core", ticker="VTI")
    assert "HR21" not in {r.rule_id for r in result.rules}
    assert "HR21b" not in {r.rule_id for r in result.rules}


# -- the narrow exception in the human-approval lock ---------------------------------
#
# Real capital is human-gated in code, not in config. These pin how narrow the one exception
# is: a named bucket, a named action, and that bucket's own switch. Everything else keeps the
# default, so no reader of requires_human_approval can mistake silence for permission.

HELD_VTI = [{"symbol": "VTI", "bucket": MICRO, "quantity": 0.2, "cost_basis_per_share": 370.0}]


def _arm_reduce(profiles):
    profiles.risk["buckets"][MICRO]["autonomy"] = {
        "reduce": {"enabled": True, "min_confidence": 80, "max_positions_reduced_per_day": 1}}


def test_a_reduction_still_needs_a_person_until_the_bucket_says_otherwise(profiles):
    # The live config ships with reduce-autonomy off, which is what this asserts.
    result = _micro_result(profiles, "VTI", action="REDUCE", positions=HELD_VTI, cash=50.0)
    assert result.requires_human_approval is True
    assert "always requires explicit human confirmation" in result.human_approval_reason


def test_an_armed_bucket_may_reduce_without_a_person(profiles):
    _arm_reduce(profiles)
    result = _micro_result(profiles, "VTI", action="REDUCE", positions=HELD_VTI, cash=50.0)
    assert result.requires_human_approval is False
    assert "decided at execution time" in result.human_approval_reason


@pytest.mark.parametrize("action", ["BUY", "ADD", "SELL"])
def test_nothing_else_is_let_through_by_the_same_switch(profiles, action):
    # Arming reductions must not arm anything that takes on risk, nor closing a position
    # outright — SELL is the whole holding.
    _arm_reduce(profiles)
    result = _micro_result(profiles, "VTI", action=action, positions=HELD_VTI, cash=50.0)
    assert result.requires_human_approval is True, action


@pytest.mark.parametrize("bucket", ["core", "sandbox"])
def test_arming_the_real_bucket_says_nothing_about_the_others(profiles, bucket):
    _arm_reduce(profiles)
    profiles.risk["buckets"][bucket]["autonomy"] = {"reduce": {"enabled": True}}
    result = _with_orders(profiles, {}, bucket=bucket, ticker="VTI", action="REDUCE",
                          held=[{"symbol": "VTI", "bucket": bucket, "quantity": 2.0,
                                 "cost_basis_per_share": 370.0}])
    assert result.requires_human_approval is True


def test_a_permanently_gated_strategy_is_not_reachable_through_the_exception(profiles):
    # The strategy gate is checked first and returns before the action is ever considered, so
    # tagging a momentum trade as a reduction cannot buy its way out of human confirmation.
    _arm_reduce(profiles)
    snap, ladders = _micro(profiles, cash=50.0, positions=HELD_VTI)
    prop = Proposal(ticker="VTI", bucket=MICRO, action="REDUCE", size_hint="normal",
                    confidence=95, trade_date=TRADE_DATE,
                    cls=classify_instrument("VTI", profiles, identity_lookup),
                    data_quality="good", strategy="momentum")
    result = HardRiskEngine(profiles, halt_file="/nonexistent/HALT").evaluate(
        prop, snap, ladders, regime())
    assert result.requires_human_approval is True
    assert "no autonomy setting" in result.human_approval_reason


def test_the_preflight_still_refuses_a_config_that_would_open_autonomously(profiles, tmp_path):
    from agentic_trading.micro_live import run_preflight

    profiles.risk["micro_live_execution"] = {
        **profiles.risk["micro_live_execution"], "enabled": True,
        "allow_autonomous_opening": True}
    pf = run_preflight(profiles, None)
    pf04 = next(c for c in pf.checks if c.check_id == "PF04")
    assert not pf04.passed and "opening" in pf04.name


# -- HR22: the daily loss gate ------------------------------------------------------
#
# The ledger tracked unrealized marks only, so "how much has today cost" had no answer and this
# rule could not be written. It stops the day compounding: after a bad enough day the answer to
# "should we open something else" is no, whatever the analysis says.

def _sold(symbol, quantity, price, basis, date=TRADE_DATE, bucket=MICRO):
    from agentic_trading.portfolio import RealizedTrade

    return RealizedTrade(date=date, symbol=symbol, bucket=bucket, quantity=quantity, price=price,
                         cost_basis_per_share=basis,
                         realized_usd=round((price - basis) * quantity, 6))


def _with_realized(profiles, trades, *, cash=1000.0):
    from agentic_trading.portfolio import PortfolioState
    from agentic_trading.risk.ladder import LadderState, LadderStates, evaluate_ladders

    state = PortfolioState(as_of=TRADE_DATE, buckets={MICRO: {"cash_usd": cash}},
                           positions=[], recent_opens=[], realized=list(trades))
    snap = build_snapshot(state, profiles, TRADE_DATE, price_lookup, identity_lookup)
    ladders = LadderStates(LadderState(3000.0), LadderState(2500.0), LadderState(500.0),
                           LadderState(100.0))
    evaluate_ladders(ladders, profiles, TRADE_DATE, None, None, None, micro_live_equity=cash)
    prop = Proposal(ticker="BND", bucket=MICRO, action="BUY", size_hint="normal", confidence=85,
                    trade_date=TRADE_DATE,
                    cls=classify_instrument("BND", profiles, identity_lookup),
                    data_quality="good", strategy="long_only_diversified",
                    evidence=collect_evidence("BND", TRADE_DATE, price_lookup=price_lookup))
    return HardRiskEngine(profiles, halt_file="/nonexistent/HALT").evaluate(
        prop, snap, ladders, regime())


def test_a_bad_enough_day_stops_new_positions(profiles):
    # -5% of a $1,000 account is -$50.
    result = _with_realized(profiles, [_sold("VTI", 1.0, 320.0, 380.0)])   # -$60
    gate = next(r for r in result.rules if r.rule_id == "HR22")
    assert not gate.passed and "-60.00" in gate.detail, gate.detail
    assert result.verdict == "REJECT"


def test_a_day_inside_the_gate_opens_normally(profiles):
    result = _with_realized(profiles, [_sold("VTI", 1.0, 340.0, 380.0)])   # -$40
    gate = next(r for r in result.rules if r.rule_id == "HR22")
    assert gate.passed and "-40.00" in gate.detail, gate.detail
    assert "HR22" not in {r.rule_id for r in result.failed_rules}


def test_gains_and_losses_on_the_same_day_are_netted(profiles):
    result = _with_realized(profiles, [_sold("VTI", 1.0, 320.0, 380.0),     # -$60
                                       _sold("BND", 1.0, 100.0, 70.0)])     # +$30
    assert next(r for r in result.rules if r.rule_id == "HR22").passed, "net is -$30"


def test_yesterdays_loss_does_not_close_todays_gate(profiles):
    result = _with_realized(profiles, [_sold("VTI", 1.0, 200.0, 380.0, date="2026-09-01")])
    assert next(r for r in result.rules if r.rule_id == "HR22").passed


def test_another_buckets_loss_does_not_close_this_gate(profiles):
    result = _with_realized(profiles, [_sold("VTI", 1.0, 200.0, 380.0, bucket="core")])
    assert next(r for r in result.rules if r.rule_id == "HR22").passed


def test_the_gate_is_measured_on_closed_trades_and_not_on_the_market(profiles):
    # A position marked down is not a realized loss. A gate that fired on marks would stop
    # trading because the market moved, which stopping trading does not fix.
    held = [{"symbol": "VTI", "bucket": MICRO, "quantity": 1.0, "cost_basis_per_share": 900.0}]
    result = _micro_result(profiles, "BND", cash=1000.0, positions=held)
    assert next(r for r in result.rules if r.rule_id == "HR22").passed


def test_the_gate_never_stands_between_a_holding_and_its_exit(profiles):
    held = [{"symbol": "VTI", "bucket": MICRO, "quantity": 1.0, "cost_basis_per_share": 380.0}]
    from agentic_trading.portfolio import PortfolioState
    from agentic_trading.risk.ladder import LadderState, LadderStates, evaluate_ladders

    state = PortfolioState(as_of=TRADE_DATE, buckets={MICRO: {"cash_usd": 1000.0}},
                           positions=[dict(p) for p in held], recent_opens=[],
                           realized=[_sold("BND", 1.0, 10.0, 500.0)])       # a ruinous day
    snap = build_snapshot(state, profiles, TRADE_DATE, price_lookup, identity_lookup)
    ladders = LadderStates(LadderState(3000.0), LadderState(2500.0), LadderState(500.0),
                           LadderState(100.0))
    evaluate_ladders(ladders, profiles, TRADE_DATE, None, None, None, micro_live_equity=1000.0)
    prop = Proposal(ticker="VTI", bucket=MICRO, action="REDUCE", size_hint="normal",
                    confidence=85, trade_date=TRADE_DATE,
                    cls=classify_instrument("VTI", profiles, identity_lookup), data_quality="good")
    result = HardRiskEngine(profiles, halt_file="/nonexistent/HALT").evaluate(
        prop, snap, ladders, regime())
    assert "HR22" not in {r.rule_id for r in result.rules}
    assert result.verdict == "APPROVE", "the day it fires is the day cutting risk must still work"


# -- the accounting the gate rests on ------------------------------------------------

def test_a_sale_records_what_it_actually_gained_or_lost(tmp_path):
    from agentic_trading.execution_scenarios import _ledger, _profiles
    from agentic_trading.portfolio import apply_fill_to_ledger, load_portfolio, realized_on

    _profiles(tmp_path)
    path = _ledger(tmp_path, positions=[{"symbol": "VTI", "bucket": "core", "quantity": 2.0,
                                         "cost_basis_per_share": 300.0}])
    apply_fill_to_ledger(path, symbol="VTI", bucket="core", side="sell", quantity=1.0,
                         price=350.0, trade_date=TRADE_DATE, client_order_id="ct-1")
    state = load_portfolio(path)
    assert len(state.realized) == 1
    assert state.realized[0].realized_usd == pytest.approx(50.0)
    assert state.realized[0].cost_basis_per_share == 300.0
    assert realized_on(state, "core", TRADE_DATE) == pytest.approx(50.0)


def test_the_basis_is_the_one_the_position_carried_at_the_moment_of_sale(tmp_path):
    # Closing a position discards its basis, so recording afterwards would have nothing to
    # measure against.
    from agentic_trading.execution_scenarios import _ledger, _profiles
    from agentic_trading.portfolio import apply_fill_to_ledger, load_portfolio

    _profiles(tmp_path)
    path = _ledger(tmp_path, positions=[{"symbol": "VTI", "bucket": "core", "quantity": 1.0,
                                         "cost_basis_per_share": 400.0}])
    apply_fill_to_ledger(path, symbol="VTI", bucket="core", side="sell", quantity=1.0,
                         price=350.0, trade_date=TRADE_DATE, client_order_id="ct-2")
    state = load_portfolio(path)
    assert state.positions == [], "the position is gone"
    assert state.realized[0].realized_usd == pytest.approx(-50.0), "the loss survives it"


def test_a_purchase_records_nothing_realized(tmp_path):
    from agentic_trading.execution_scenarios import _ledger, _profiles
    from agentic_trading.portfolio import apply_fill_to_ledger, load_portfolio

    _profiles(tmp_path)
    path = _ledger(tmp_path)
    apply_fill_to_ledger(path, symbol="VTI", bucket="core", side="buy", quantity=1.0,
                         price=350.0, trade_date=TRADE_DATE, client_order_id="ct-3")
    assert load_portfolio(path).realized == [], "buying closes nothing"


def test_a_ledger_written_before_this_existed_still_loads(tmp_path):
    from agentic_trading.execution_scenarios import _ledger, _profiles
    from agentic_trading.portfolio import load_portfolio, realized_on

    _profiles(tmp_path)
    state = load_portfolio(_ledger(tmp_path))     # no realized key at all
    assert state.realized == []
    assert realized_on(state, "core", TRADE_DATE) == 0.0


# -- HR23: the autonomous allowance --------------------------------------------------
#
# The one place code may create risk rather than cut it. Measured on provenance, because one
# brokerage account holds one pool of cash: the boundary is drawn through it, not around a
# separate account the venue would not know about.

def _autonomous_buy(profiles, *, cash=1000.0, positions=(), ticker="BND"):
    from agentic_trading.portfolio import PortfolioState
    from agentic_trading.risk.ladder import LadderState, LadderStates, evaluate_ladders

    state = PortfolioState(as_of=TRADE_DATE, buckets={MICRO: {"cash_usd": cash}},
                           positions=[dict(p) for p in positions], recent_opens=[])
    snap = build_snapshot(state, profiles, TRADE_DATE, price_lookup, identity_lookup)
    ladders = LadderStates(LadderState(3000.0), LadderState(2500.0), LadderState(500.0),
                           LadderState(100.0))
    evaluate_ladders(ladders, profiles, TRADE_DATE, None, None, None,
                     micro_live_equity=snap.buckets[MICRO].equity)
    prop = Proposal(ticker=ticker, bucket=MICRO, action="BUY", size_hint="small", confidence=85,
                    trade_date=TRADE_DATE,
                    cls=classify_instrument(ticker, profiles, identity_lookup),
                    data_quality="good", strategy="long_only_diversified", autonomous=True,
                    evidence=collect_evidence(ticker, TRADE_DATE, price_lookup=price_lookup))
    return HardRiskEngine(profiles, halt_file="/nonexistent/HALT").evaluate(
        prop, snap, ladders, regime())


def _opened(symbol, quantity, basis, by):
    return {"symbol": symbol, "bucket": MICRO, "quantity": quantity,
            "cost_basis_per_share": basis, "opened_by": by}


def test_the_allowance_counts_only_what_code_opened_for_itself(profiles):
    # A person's $700 of VTI does not consume the allowance.
    held = [_opened("VTI", 2.0, 350.0, None)]
    result = _autonomous_buy(profiles, cash=300.0, positions=held)
    allowance = next(r for r in result.rules if r.rule_id == "HR23")
    assert allowance.passed, allowance.detail


def test_the_allowance_is_a_ceiling_on_what_it_holds_at_once(profiles):
    # ~$226 of BND opened autonomously against a ~$1,000 account is already past 20%.
    held = [_opened("BND", 3.15, 71.83, "autonomy:open")]
    result = _autonomous_buy(profiles, cash=774.0, positions=held, ticker="SGOV")
    allowance = next(r for r in result.rules if r.rule_id == "HR23")
    assert not allowance.passed and "vs a limit of 20%" in allowance.detail
    assert result.verdict == "REJECT"


def test_a_person_adding_to_it_does_not_free_up_the_allowance(tmp_path):
    # opened_by is set once and never rewritten, so topping up a position code opened cannot
    # quietly make room for another one.
    from agentic_trading.execution_scenarios import _ledger, _profiles
    from agentic_trading.portfolio import apply_fill_to_ledger, load_portfolio

    _profiles(tmp_path)
    path = _ledger(tmp_path)
    apply_fill_to_ledger(path, symbol="VTI", bucket="core", side="buy", quantity=1.0, price=300.0,
                         trade_date=TRADE_DATE, client_order_id="ct-a", opened_by="autonomy:open")
    apply_fill_to_ledger(path, symbol="VTI", bucket="core", side="buy", quantity=1.0, price=320.0,
                         trade_date=TRADE_DATE, client_order_id="ct-b", opened_by="owner")
    position = load_portfolio(path).position("VTI")
    assert position.quantity == 2.0
    assert position.opened_by == "autonomy:open", "a top-up must not launder the provenance"


def test_a_human_approved_buy_is_not_measured_against_the_allowance(profiles):
    held = [_opened("BND", 3.15, 71.83, "autonomy:open")]
    from agentic_trading.portfolio import PortfolioState
    from agentic_trading.risk.ladder import LadderState, LadderStates, evaluate_ladders

    state = PortfolioState(as_of=TRADE_DATE, buckets={MICRO: {"cash_usd": 774.0}},
                           positions=[dict(p) for p in held], recent_opens=[])
    snap = build_snapshot(state, profiles, TRADE_DATE, price_lookup, identity_lookup)
    ladders = LadderStates(LadderState(3000.0), LadderState(2500.0), LadderState(500.0),
                           LadderState(100.0))
    evaluate_ladders(ladders, profiles, TRADE_DATE, None, None, None,
                     micro_live_equity=snap.buckets[MICRO].equity)
    prop = Proposal(ticker="SGOV", bucket=MICRO, action="BUY", size_hint="small", confidence=85,
                    trade_date=TRADE_DATE, cls=classify_instrument("SGOV", profiles, identity_lookup),
                    data_quality="good", strategy="long_only_diversified", autonomous=False,
                    evidence=collect_evidence("SGOV", TRADE_DATE, price_lookup=price_lookup))
    result = HardRiskEngine(profiles, halt_file="/nonexistent/HALT").evaluate(
        prop, snap, ladders, regime())
    assert "HR23" not in {r.rule_id for r in result.rules}, \
        "the allowance bounds what code may do, not what a person may"
