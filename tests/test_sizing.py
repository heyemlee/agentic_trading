"""Sizing an order to its limits: shrink first, the minimum order last (docs/todo.md, P1.3).

Two things are held here. The arithmetic of :mod:`agentic_trading.sizing` on its own, and the
plan's acceptance through the real Hard Risk engine: across account sizes from the $100 the
account holds today to several thousand, every order the engine approves satisfies every limit
at once, is never larger than the strategy asked for, and is never below the minimum order --
and where no such order exists the refusal names the limit that left too little room.
Fully offline.
"""

import itertools

import pytest

from agentic_trading import sizing
from agentic_trading.evidence import collect_evidence
from agentic_trading.portfolio import PortfolioState, build_snapshot, classify_instrument
from agentic_trading.risk import HardRiskEngine, Proposal
from agentic_trading.risk.ladder import LadderState, LadderStates, evaluate_ladders
from agentic_trading.risk_scenarios import (
    GOOD_STOCK_INFO,
    PRICES,
    TRADE_DATE,
    build_scenarios,
    earnings_lookup,
    identity_lookup,
    price_lookup,
    regime,
    run_scenario,
)
from agentic_trading.sizing import Limit, fit

MICRO = "micro_live"


def fitting(profiles):
    """The same profiles, with orders cut to their limits instead of refused by them."""
    profiles.risk.setdefault("sizing", {})["fit_to_limits"] = True
    return profiles


def evaluate(profiles, ticker, *, cash=100.0, positions=(), size="normal", action="BUY",
             autonomous=False):
    state = PortfolioState(as_of=TRADE_DATE, buckets={MICRO: {"cash_usd": cash}},
                           positions=[dict(p) for p in positions], recent_opens=[])
    snap = build_snapshot(state, profiles, TRADE_DATE, price_lookup, identity_lookup)
    equity = snap.buckets[MICRO].equity
    ladders = LadderStates(LadderState(3000.0), LadderState(2500.0), LadderState(500.0),
                           LadderState(equity))
    evaluate_ladders(ladders, profiles, TRADE_DATE, None, None, None, micro_live_equity=equity)
    prop = Proposal(ticker=ticker, bucket=MICRO, action=action, size_hint=size, confidence=90,
                    trade_date=TRADE_DATE, cls=classify_instrument(ticker, profiles, identity_lookup),
                    data_quality="good", autonomous=autonomous,
                    evidence=collect_evidence(ticker, TRADE_DATE, price_lookup=price_lookup))
    engine = HardRiskEngine(profiles, halt_file="/nonexistent/HALT",
                            stock_info_lookup=lambda _symbol: GOOD_STOCK_INFO,
                            earnings_lookup=earnings_lookup)
    return engine.evaluate(prop, snap, ladders, regime()), snap


# ---------------------------------------------------------------- the arithmetic alone
def test_an_order_is_cut_to_the_tightest_limit_and_never_raised_to_it():
    limits = [Limit("cash", "HR07", 500.0), Limit("tier_order", "HR21b", 200.0)]
    cut = fit(350.0, limits, minimum_usd=25.0)
    assert (cut.code, cut.order_usd, cut.legal_max_usd) == (sizing.SHRUNK, 200.0, 200.0)
    assert cut.binding == ("tier_order",) and cut.feasible
    roomy = fit(120.0, limits, minimum_usd=25.0)
    assert (roomy.code, roomy.order_usd) == (sizing.OK, 120.0)        # room is not a target


def test_the_minimum_order_is_asked_about_last_and_names_the_limit():
    boxed = fit(35.0, [Limit("cash", "HR07", 100.0), Limit("position_cap", "HR08", 10.0)],
                minimum_usd=25.0)
    assert (boxed.code, boxed.feasible, boxed.order_usd) == (sizing.LIMIT_BELOW_MINIMUM, False, 0.0)
    assert boxed.binding == ("position_cap",) and boxed.legal_max_usd == 10.0
    spent = fit(35.0, [Limit("cash", "HR07", 0.0)], minimum_usd=25.0)
    assert spent.code == sizing.LIMIT_EXHAUSTED and spent.binding == ("cash",)
    past = fit(35.0, [Limit("tier_share", "HR21", -40.0)], minimum_usd=25.0)
    assert past.code == sizing.LIMIT_EXHAUSTED and past.legal_max_usd == 0.0


def test_a_hint_below_the_minimum_is_a_setting_conflict_and_blames_no_limit():
    small = fit(20.0, [Limit("cash", "HR07", 100.0)], minimum_usd=25.0)
    assert (small.code, small.feasible, small.binding) == (sizing.TARGET_BELOW_MINIMUM, False, ())
    assert fit(0.0, [], minimum_usd=25.0).code == sizing.AT_TARGET
    assert fit(25.0, [Limit("cash", "HR07", 100.0)], minimum_usd=25.0).feasible   # at it, not under


def test_without_the_opt_in_nothing_is_cut():
    limits = [Limit("tier_order", "HR21b", 200.0)]
    kept = fit(350.0, limits, minimum_usd=25.0, shrink=False)
    # Reported, so the record can say what room there was; not applied.
    assert (kept.code, kept.order_usd, kept.legal_max_usd) == (sizing.OK, 350.0, 200.0)


def test_rounding_is_always_down_and_ties_are_all_named():
    cut = fit(100.0, [Limit("cash", "HR07", 33.339), Limit("order_cap", "HR08b", 33.3351)],
              minimum_usd=1.0)
    assert cut.order_usd == 33.33 and set(cut.binding) == {"cash", "order_cap"}
    assert sizing.floor_cents(0.019999) == 0.01 and sizing.floor_cents(-5) == 0.0
    assert fit(50.0, [Limit("cash", "HR07", float("inf"))], minimum_usd=1.0).legal_max_usd is None
    assert not fit(50.0, [Limit("cash", "HR07", float("nan"))], minimum_usd=1.0).feasible


# ---------------------------------------------------------------- through the engine
def test_the_default_is_unchanged_every_scenario_still_ends_as_it_did(profiles):
    """Cutting to fit is opt-in: with nothing set, the catalogue's refusals are the same."""
    assert sizing.fits_to_limits(profiles, MICRO) is False
    assert sizing.fits_to_limits(profiles, "core") is False
    for scenario in build_scenarios():
        outcome = run_scenario(scenario, profiles)
        assert outcome.verdict == scenario.expect_verdict, scenario.key
        assert set(scenario.expect_rules_failed) <= set(outcome.failed), scenario.key


def test_only_a_literal_true_opts_in_and_a_bucket_setting_wins(profiles):
    settings = profiles.risk["sizing"]
    for value in ("true", 1, "yes", None):
        settings["fit_to_limits"] = value
        assert sizing.fits_to_limits(profiles, MICRO) is False, value
    settings["fit_to_limits"] = True
    assert sizing.fits_to_limits(profiles, MICRO) and sizing.fits_to_limits(profiles, "core")
    settings["micro_live"]["fit_to_limits"] = False
    assert sizing.fits_to_limits(profiles, MICRO) is False and sizing.fits_to_limits(profiles, "core")


def test_at_1000_the_normal_size_is_cut_to_the_tier_ticket_instead_of_refused(profiles):
    refused, _ = evaluate(profiles, "VTI", cash=1000.0)
    assert refused.verdict == "REJECT" and "HR21b" in {r.rule_id for r in refused.failed_rules}

    cut, _ = evaluate(fitting(profiles), "VTI", cash=1000.0)
    assert cut.verdict == "APPROVE" and cut.proposed_notional_usd == 200.0
    assert cut.sizing["code"] == sizing.SHRUNK and cut.sizing["binding"] == ["tier_order"]
    assert cut.sizing["requested"] == 350.0
    hr06 = next(r for r in cut.rules if r.rule_id == "HR06")
    assert "cut from $350.00" in hr06.detail and not cut.failed_rules


def test_at_100_a_stock_cannot_be_cut_to_fit_and_the_refusal_names_the_cap(profiles):
    """A single name is capped at 10% -- $10 of $100 -- and the minimum order is $25."""
    result, _ = evaluate(fitting(profiles), "AAPL", cash=100.0)
    assert result.verdict == "REJECT" and result.proposed_notional_usd == 0.0
    assert result.sizing["code"] == sizing.LIMIT_BELOW_MINIMUM
    assert result.sizing["binding"][0] == "position_cap" and result.sizing["legal_max"] == 10.0
    failed = {r.rule_id for r in result.failed_rules}
    # HR06 says no order fits; HR08 is still shown refusing what was asked for.
    assert {"HR06", "HR08"} <= failed


def test_at_100_the_small_size_is_a_setting_conflict_in_either_mode(profiles):
    plain, _ = evaluate(profiles, "VTI", cash=100.0, size="small")
    assert (plain.verdict, plain.sizing["code"]) == ("NO_ACTION", sizing.TARGET_BELOW_MINIMUM)
    assert "below the $25.00 minimum order" in next(r for r in plain.rules if r.rule_id == "HR06").detail
    cut, _ = evaluate(fitting(profiles), "VTI", cash=100.0, size="small")
    assert (cut.verdict, cut.sizing["code"]) == ("NO_ACTION", sizing.TARGET_BELOW_MINIMUM)


def test_cash_is_a_limit_like_any_other_an_order_is_cut_to_what_is_there(profiles):
    # Most of the account sits in a gold fund, a different tier from the one being bought, so
    # the tier leaves plenty of room and the $150 of cash is what there is least of.
    held = [{"symbol": "IAU", "bucket": MICRO, "quantity": 10.0, "cost_basis_per_share": 84.23}]
    result, snap = evaluate(fitting(profiles), "VTI", cash=150.0, positions=held)
    assert result.verdict == "APPROVE"
    assert result.proposed_notional_usd == 150.0 == snap.buckets[MICRO].cash_usd
    assert result.sizing["requested"] > 300.0 and "cash" in result.sizing["binding"]


def test_cutting_never_reaches_past_a_rule_that_is_not_about_size(profiles):
    """An order cut to fit is still refused by everything that was never a question of size."""
    held = [{"symbol": "VOO", "bucket": MICRO, "quantity": 0.2, "cost_basis_per_share": 703.10}]
    result, _ = evaluate(fitting(profiles), "VTI", cash=1000.0, positions=held)
    assert result.verdict == "REJECT"
    assert "HR09" in {r.rule_id for r in result.failed_rules}        # one fund per exposure group


def test_the_autonomous_allowance_is_one_of_the_limits_an_order_is_cut_to(profiles):
    profiles = fitting(profiles)
    opening = profiles.risk["buckets"][MICRO]["autonomy"]["open"]
    opening.update(enabled=True, max_pct_of_account=20)
    result, snap = evaluate(profiles, "VTI", cash=1000.0, autonomous=True)
    assert result.verdict == "APPROVE"
    assert result.proposed_notional_usd <= snap.buckets[MICRO].allocation_usd * 0.20 + 1e-6
    allowance = next(r for r in result.rules if r.rule_id == "HR23")
    assert allowance.passed and allowance.facts["post_pct"] <= 20.0 + 1e-9


ACCOUNTS = (100.0, 250.0, 400.0, 1000.0, 2000.0, 5000.0)
SYMBOLS = ("VTI", "BND", "SGOV", "IAU", "AAPL")
HELD = {
    "empty": (),
    "bond": ({"symbol": "BND", "quantity_of_equity": 0.30},),
    "broad": ({"symbol": "VXUS", "quantity_of_equity": 0.25},),
}


#: Every account size, symbol, size hint and starting book -- less the books that already hold
#: the symbol, since topping a holding up is a different question from opening one.
GRID = [(equity, symbol, size, book)
        for equity, symbol, size, book in itertools.product(ACCOUNTS, SYMBOLS, ("small", "normal"), HELD)
        if all(held["symbol"] != symbol for held in HELD[book])]


@pytest.mark.parametrize("equity,symbol,size,book", GRID)
def test_every_approved_order_satisfies_every_limit_at_once(profiles, equity, symbol, size, book):
    """The acceptance: from the $100 account upward, an approval is legal under all of them.

    Each limit is recomputed here from the profile's own numbers, not from the engine's
    workings, so a mistake in how the engine combines them cannot also be made by the check.
    """
    positions = [{"symbol": h["symbol"], "bucket": MICRO,
                  "quantity": equity * h["quantity_of_equity"] / PRICES[h["symbol"]],
                  "cost_basis_per_share": PRICES[h["symbol"]]} for h in HELD[book]]
    cash = equity - sum(p["quantity"] * PRICES[p["symbol"]] for p in positions)
    result, snap = evaluate(fitting(profiles), symbol, cash=cash, positions=positions, size=size)
    bucket = snap.buckets[MICRO]
    record = result.sizing
    assert record is not None
    order = result.proposed_notional_usd

    if result.verdict != "APPROVE":
        assert order == 0.0
        if record["code"] in (sizing.LIMIT_BELOW_MINIMUM, sizing.LIMIT_EXHAUSTED):
            # No order exists, and the refusal says which limit left too little room.
            assert record["binding"] and record["legal_max"] < record["minimum"]
        return

    cfg = profiles.risk["buckets"][MICRO]
    cls = classify_instrument(symbol, profiles, identity_lookup)
    alloc = bucket.allocation_usd
    tier = next(t for t in cfg["tiers"].values() if cls.limit_class in t["classes"])
    cap_pct = min(profiles.effective(cfg["position_limits"]["max_single_position_pct_of_allocation"], "x"),
                  cfg["position_limits"]["max_pct_of_allocation_by_class"][cls.limit_class])
    tier_held = sum(bucket.classes_pct.get(c, 0.0) for c in tier["classes"])

    assert order >= record["minimum"] - 1e-9, "below the minimum order"
    assert order <= record["requested"] + 1e-9, "larger than the strategy asked for"
    assert order <= bucket.cash_usd + 1e-6, "more than the cash"
    assert order <= bucket.allocation_usd - bucket.deployed_cost_usd + 1e-6, "past the deployed cap"
    assert order / alloc * 100.0 <= cap_pct + 1e-9, "past the position cap"
    assert order <= tier["max_order_usd"] + 1e-6, "past the tier's ticket"
    assert tier_held + order / alloc * 100.0 <= tier["max_pct_of_account"] + 1e-9, "past the tier's share"
    if cls.limit_class == "individual_stock":
        conc = cfg["concentration_pct_of_allocation"]
        assert order / alloc * 100.0 <= conc["all_individual_stocks_combined"] + 1e-9
        assert order / alloc * 100.0 <= conc["direct_sector_exposure"] + 1e-9
    assert not result.failed_rules and round(order, 2) == order


def test_what_the_100_dollar_account_can_and_cannot_buy_today(profiles):
    """The account as it stands, spelled out, so the conflict is a fact and not an impression."""
    table = {}
    for symbol, size in itertools.product(("VTI", "BND", "IAU", "AAPL"), ("small", "normal")):
        result, _ = evaluate(fitting(profiles), symbol, cash=100.0, size=size)
        table[symbol, size] = (result.verdict, result.sizing["code"], result.proposed_notional_usd)
    # A broad or bond fund: only at the full size, because the half size is $20.
    assert table["VTI", "normal"] == ("APPROVE", sizing.OK, 35.0)
    assert table["BND", "normal"] == ("APPROVE", sizing.OK, 35.0)
    assert table["VTI", "small"][:2] == ("NO_ACTION", sizing.TARGET_BELOW_MINIMUM)
    # A sector or thematic fund is capped at 25%: cut to exactly the minimum order.
    assert table["IAU", "normal"] == ("APPROVE", sizing.SHRUNK, 25.0)
    # A single stock is capped at $10 and can never reach the minimum.
    assert table["AAPL", "normal"][:2] == ("REJECT", sizing.LIMIT_BELOW_MINIMUM)


# ---------------------------------------------------------------- the minimum order's two sources
def test_the_minimum_is_the_products_own_setting_held_to_the_venues_floor(profiles):
    assert sizing.minimum_order(profiles, MICRO) == (25.0, "product")
    assert sizing.minimum_order(profiles, "core") == (25.0, "product")
    profiles.risk["sizing"]["min_order_usd"] = 0.50
    # Robinhood accepts orders from $1; the simulator from a cent.
    assert sizing.minimum_order(profiles, MICRO) == (1.0, "broker")
    assert sizing.minimum_order(profiles, "core") == (0.5, "product")


def test_revalidation_reads_the_minimum_from_the_same_place_as_sizing():
    import inspect

    from agentic_trading import execution

    source = inspect.getsource(execution.revalidate)
    assert "minimum_order(profiles, proposal.bucket)" in source
    assert 'get("min_order_usd"' not in source
