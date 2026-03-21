"""The automatic-trading plan: what a person confirms, what code may adjust, what can be ordered.

The acceptance held here is the plan's own (docs/todo.md, P1.3 and P1.4): from the $100 the
account holds today to several thousand, a plan says what could actually be invested and why
not more; every order it counts on satisfies every limit at once; where none exists it names the
limit; and it never proposes widening a limit it was not asked to. Fully offline: the account is
a synthetic ledger and nothing here reads a venue or calls a model.
"""

import copy

import pytest

from agentic_trading import sizing
from agentic_trading.auto import modes, plan
from agentic_trading.auto.plan import Envelope, PlanError, Strategy
from agentic_trading.broker import requirements
from agentic_trading.portfolio import PortfolioState, build_snapshot, classify_instrument
from agentic_trading.risk import HardRiskEngine
from agentic_trading.risk import Proposal as RiskProposal
from agentic_trading.risk_scenarios import PRICES, TRADE_DATE, identity_lookup, price_lookup

MICRO = "micro_live"


def account(profiles, cash=100.0, positions=()):
    state = PortfolioState(as_of=TRADE_DATE, buckets={MICRO: {"cash_usd": cash}},
                           positions=[dict(p) for p in positions], recent_opens=[])
    return build_snapshot(state, profiles, TRADE_DATE, price_lookup, identity_lookup)


def change(plan_doc, path):
    return next((c for c in plan_doc["changes"] if c["path"] == path), None)


# ---------------------------------------------------------------- the $100 account, as it stands
def test_at_100_nothing_can_be_ordered_today_and_the_plan_says_which_setting_it_is(profiles):
    doc = plan.build(profiles, account(profiles), Envelope(modes.STEADY, 100.0))
    today = doc["today"]
    assert today["any_order"] is False and today["deployable_usd"] == 0.0
    # The share code may open is 20% -- $20 -- and each position is $33: refused, not trimmed.
    first = today["path"][0]
    assert first["symbol"] == "VTI" and first["feasible"] is False
    assert "$33.00" in first["why_not"]["zh"] and "$20.00" in first["why_not"]["zh"]
    assert "自动交易额度" in first["why_not"]["zh"] and "整笔拒绝" in first["why_not"]["zh"]
    assert "一笔也下不了" in doc["summary"]["zh"]


def test_at_100_the_plan_makes_orders_exist_and_says_how_much_can_really_be_invested(profiles):
    doc = plan.build(profiles, account(profiles), Envelope(modes.STEADY, 100.0))
    under = doc["under_plan"]
    assert under["any_order"] is True and under["fits_to_limits"] is True
    bought = [step for step in under["path"] if step["feasible"]]
    # $33 of a broad fund, then the low tier's 60% of the account leaves $27 for the next.
    assert [(s["symbol"], s["order_usd"]) for s in bought] == [("VTI", 33.0), ("VXUS", 27.0)]
    assert bought[1]["binding"] == ["tier_share"]
    assert under["deployable_usd"] == 60.0 and under["positions"] == 2
    # The third fund is in the same tier, and the plan says that rather than counting it.
    refused = next(step for step in under["path"] if not step["feasible"])
    assert refused["symbol"] == "BND" and "风险档位占比上限" in refused["why_not"]["zh"]
    assert "$60" in doc["summary"]["zh"] and "$100" in doc["summary"]["zh"]
    assert "$10.00" in doc["summary"]["zh"], "the loss line is stated in dollars"


def test_the_plan_lists_each_change_for_what_it_is(profiles):
    doc = plan.build(profiles, account(profiles), Envelope(modes.STEADY, 100.0))
    kinds = {c["path"]: c["kind"] for c in doc["changes"]}
    assert kinds == {
        "buckets.micro_live.autonomy.open.max_pct_of_account": plan.BOUNDARY,
        "buckets.micro_live.autonomy.open.max_usd": plan.BOUNDARY,
        "buckets.micro_live.autonomy.open.min_confidence": plan.BOUNDARY,
        "buckets.micro_live.autonomy.open.enabled": plan.BOUNDARY,
        "buckets.micro_live.autonomy.managed_exit.enabled": plan.BOUNDARY,
        "sizing.fit_to_limits": plan.PRODUCT,
        "sizing.min_order_usd": plan.PRODUCT,
        "research_schedule.accounts.micro_live.candidates.enabled": plan.SCHEDULE,
    }
    share = change(doc, "buckets.micro_live.autonomy.open.max_pct_of_account")
    assert (share["from"], share["to"]) == (20, 100)
    assert change(doc, "buckets.micro_live.autonomy.open.max_usd")["to"] == 100.0
    for item in doc["changes"]:
        assert item["why"]["zh"] and item["why"]["en"]


def test_a_plan_never_proposes_touching_a_limit_it_was_not_asked_about(profiles):
    """Confidence floors, tiers, position caps, loss gates, the ladder: none are the plan's."""
    for capital in (100.0, 1000.0):
        doc = plan.build(profiles, account(profiles, cash=capital), Envelope(modes.STEADY, capital))
        for item in doc["changes"]:
            path = item["path"]
            assert not path.startswith(("confidence_requirements", "drawdown_ladder", "permissions",
                                        "micro_live_execution", "execution_policy"))
            assert ".tiers." not in path and ".position_limits." not in path
            assert ".loss_limits." not in path and ".activity_limits." not in path
            assert ".concentration_pct_of_allocation." not in path
        # And the one floor it does set is the risk profile's own, not a lower one.
        floor = change(doc, "buckets.micro_live.autonomy.open.min_confidence")["to"]
        assert floor == profiles.confidence.minimum_to_open["autonomous_any"] == 80


# ---------------------------------------------------------------- the minimum order: whose it is
def test_the_25_dollar_minimum_is_named_as_the_products_setting_not_the_venues(profiles):
    doc = plan.build(profiles, account(profiles), Envelope(modes.STEADY, 100.0))
    assert doc["today"]["minimum_order"] == {"usd": 25.0, "source": "product"}
    lowered = change(doc, "sizing.min_order_usd")
    # The half-size position is $16.50; the setting is proposed down to $16, not to the venue's $1.
    assert (lowered["from"], lowered["to"]) == (25.0, 16.0)
    assert "不是券商的要求" in lowered["why"]["zh"] and "$1.00" in lowered["why"]["zh"]
    assert doc["under_plan"]["minimum_order"] == {"usd": 16.0, "source": "product"}
    venue = doc["venue"]
    assert venue["name"] == "robinhood" and venue["min_order_usd"] == 1.0
    assert venue["documented"] is True and venue["confirmed_for_account"] is False
    assert any("fractional-shares" in item["source"] for item in venue["evidence"])
    assert venue["open_questions"], "what has not been established is listed beside what has"


def test_the_minimum_is_not_lowered_toward_a_venue_floor_nobody_has_established(profiles, monkeypatch):
    monkeypatch.setattr(requirements, "for_venue", lambda _venue: None)
    assert requirements.lowest_product_minimum(profiles, MICRO)[0] is None
    doc = plan.build(profiles, account(profiles), Envelope(modes.STEADY, 100.0))
    kept = change(doc, "sizing.min_order_usd")
    assert kept["to"] == kept["from"] == 25.0
    assert "不能下调" in kept["why"]["zh"]
    assert doc["under_plan"]["minimum_order"]["usd"] == 25.0


def test_a_minimum_that_already_fits_is_left_alone(profiles):
    doc = plan.build(profiles, account(profiles, cash=1000.0), Envelope(modes.STEADY, 1000.0))
    assert change(doc, "sizing.min_order_usd") is None       # the half size is $166


# ---------------------------------------------------------------- other account sizes
@pytest.mark.parametrize("equity,capital", [
    (100.0, 100.0), (100.0, 60.0), (250.0, 250.0), (1000.0, 1000.0), (1000.0, 300.0),
    (2000.0, 2000.0), (5000.0, 2000.0)])
def test_every_position_a_plan_counts_on_is_legal_under_every_limit_at_once(profiles, equity, capital):
    snap = account(profiles, cash=equity)
    doc = plan.build(profiles, snap, Envelope(modes.STEADY, capital))
    under = doc["under_plan"]
    bought = [step for step in under["path"] if step["feasible"]]
    cfg = profiles.risk["buckets"][MICRO]
    minimum = under["minimum_order"]["usd"]
    planned = capital * doc["strategy"]["position_pct"] / 100.0
    held_in_tier: dict[str, float] = {}
    for step in bought:
        cls = classify_instrument(step["symbol"], profiles, identity_lookup)
        name, tier = next((n, t) for n, t in cfg["tiers"].items() if cls.limit_class in t["classes"])
        order = step["order_usd"]
        assert order >= minimum - 1e-9
        assert order <= planned + 0.01, "never more than the plan's own position size"
        assert order <= tier["max_order_usd"] + 1e-6
        assert order / equity * 100.0 <= cfg["position_limits"]["max_pct_of_allocation_by_class"][cls.limit_class] + 1e-6
        held_in_tier[name] = held_in_tier.get(name, 0.0) + order
        assert held_in_tier[name] / equity * 100.0 <= tier["max_pct_of_account"] + 1e-6
    total = sum(step["order_usd"] for step in bought)
    assert total == pytest.approx(under["deployable_usd"])
    assert total <= capital + 1e-6, "never more than the capital the person set"
    assert total <= equity + 1e-6 and len(bought) <= doc["strategy"]["max_positions"]
    assert len(bought) <= profiles.effective(cfg["position_limits"]["max_open_positions"], "x")
    # Where something was left out, the plan says why in both languages.
    for step in under["path"]:
        assert step["feasible"] or (step["why_not"]["zh"] and step["why_not"]["en"])


def test_at_1000_the_tier_ticket_is_what_refuses_today_and_what_orders_are_cut_to(profiles):
    doc = plan.build(profiles, account(profiles, cash=1000.0), Envelope(modes.STEADY, 1000.0))
    assert doc["today"]["any_order"] is False
    under = doc["under_plan"]
    assert [s["order_usd"] for s in under["path"] if s["feasible"]] == [200.0, 200.0, 200.0]
    assert under["deployable_usd"] == 600.0                      # the low tier's 60% of the account
    assert under["path"][0]["requested_usd"] == 330.0 and "tier_order" in under["path"][0]["binding"]


def test_capital_below_the_account_caps_what_the_plan_may_hold(profiles):
    doc = plan.build(profiles, account(profiles, cash=1000.0), Envelope(modes.STEADY, 300.0))
    assert change(doc, "buckets.micro_live.autonomy.open.max_pct_of_account")["to"] == 30
    assert change(doc, "buckets.micro_live.autonomy.open.max_usd")["to"] == 300.0
    assert doc["under_plan"]["deployable_usd"] <= 300.0


def test_an_account_that_already_holds_the_fund_is_not_planned_to_buy_it_again(profiles):
    held = [{"symbol": "VTI", "bucket": MICRO, "quantity": 0.5, "cost_basis_per_share": PRICES["VTI"]}]
    snap = account(profiles, cash=811.56, positions=held)
    doc = plan.build(profiles, snap, Envelope(modes.STEADY, 1000.0))
    assert "VTI" not in [step["symbol"] for step in doc["under_plan"]["path"]]


# ---------------------------------------------------------------- the envelope is the person's
def test_a_plan_cannot_ask_for_more_than_the_accounts_own_ceiling(profiles):
    with pytest.raises(PlanError, match="ceiling"):
        plan.build(profiles, account(profiles, cash=5000.0), Envelope(modes.STEADY, 2500.0))


@pytest.mark.parametrize("envelope,expected", [
    (Envelope("scalping", 100.0), "unknown mode"),
    (Envelope(modes.STEADY, 0.0), "positive amount"),
    (Envelope(modes.STEADY, float("nan")), "positive amount"),
    (Envelope(modes.STEADY, 100.0, loss_pause_pct=40.0), "loss line"),
    (Envelope(modes.STEADY, 100.0, loss_pause_pct=0.5), "loss line"),
    (Envelope(modes.STEADY, 100.0, account="core"), "Agentic account only"),
])
def test_an_envelope_outside_what_may_be_asked_is_refused(profiles, envelope, expected):
    with pytest.raises(PlanError, match=expected):
        plan.build(profiles, account(profiles), envelope)


def test_nothing_is_planned_when_the_profile_lets_code_open_nothing(profiles):
    profiles.risk["buckets"][MICRO]["autonomy"]["open"]["classes"] = []
    with pytest.raises(PlanError, match="nothing to plan"):
        plan.build(profiles, account(profiles), Envelope(modes.STEADY, 100.0))


# ---------------------------------------------------------------- a suggestion is only a suggestion
def test_a_suggestion_is_clamped_to_the_bounds_and_every_clamp_is_recorded(profiles):
    wild = Strategy(template="research_etf", max_positions=9, position_pct=250.0, min_confidence=10,
                    stop_loss_pct=50.0, take_profit_pct=-5.0, max_holding_days=3,
                    universe=["VTI", "TSLA", "tqqq"], rationale_zh="随便", rationale_en="whatever")
    doc = plan.build(profiles, account(profiles), Envelope(modes.STEADY, 100.0),
                     suggestion=wild, suggested_by="model")
    used = doc["strategy"]
    assert used["suggested_by"] == "model"
    assert used["max_positions"] == 3            # the account holds three positions at most
    assert used["min_confidence"] == 80          # never below the autonomous floor
    assert used["stop_loss_pct"] == 15.0 and used["take_profit_pct"] == 0.0
    assert used["max_holding_days"] == 7 and used["position_pct"] == 100.0
    assert used["universe"] == ["VTI"], "what code may not open is not something to suggest"
    noted = {note["field"] for note in doc["clamps"]}
    assert noted == {"max_positions", "position_pct", "min_confidence", "stop_loss_pct",
                     "take_profit_pct", "max_holding_days", "universe"}
    assert all(note["why"]["zh"] and note["why"]["en"] for note in doc["clamps"])


def test_a_model_fills_in_a_templates_numbers_and_can_hand_in_nothing_else(profiles):
    """docs/todo.md, P2.2: a model gives parameters for a template local code carries out.
    It writes no rule and no code, and nothing it returns is ever executed."""
    from pydantic import ValidationError

    # The shape a model's answer is parsed into has no field for a rule, a condition or code,
    # and refuses one that is offered.
    assert set(Strategy.model_fields) == {
        "template", "max_positions", "position_pct", "min_confidence", "stop_loss_pct",
        "take_profit_pct", "max_holding_days", "trend_window", "universe", "rationale_zh",
        "rationale_en"}
    for extra in ({"code": "import os; os.system('x')"}, {"entry_rule": "close > sma(5)"},
                  {"leverage": 3}, {"orders": [{"symbol": "TSLA", "side": "buy"}]}):
        with pytest.raises(ValidationError):
            Strategy(**extra)
    # A template is one of the names this system has code for. Anything else -- another
    # strategy's name, or something that is not a name at all -- is replaced, and the plan says so.
    assert plan.TEMPLATES == {"steady": ("research_etf",), "swing": ("trend_etf",),
                              "intraday": ("trend_etf",)}
    for asked in ("martingale_grid", "__import__('os').system('x')", "", "trend_etf"):
        doc = plan.build(profiles, account(profiles), Envelope(modes.STEADY, 100.0),
                         suggestion=Strategy(template=asked, universe=["VTI"]), suggested_by="model")
        assert doc["strategy"]["template"] == "research_etf"
        note = next(n for n in doc["clamps"] if n["field"] == "template")
        assert note["used"] == "research_etf" and "不是本系统能执行的策略模板" in note["why"]["zh"]
    assert doc["bounds"]["templates"] == ["research_etf"]
    # What it says about its choice is words, shown as words, and kept short.
    long = plan.build(profiles, account(profiles), Envelope(modes.STEADY, 100.0), suggested_by="model",
                      suggestion=Strategy(universe=["VTI"], rationale_zh="理由" * 900, rationale_en="x" * 5000))
    assert len(long["strategy"]["rationale"]["zh"]) == len(long["strategy"]["rationale"]["en"]) == 400


def test_a_position_larger_than_the_configured_size_is_held_to_it(profiles):
    """A strategy may ask for a smaller position than the sizing table's, never a larger one."""
    doc = plan.build(profiles, account(profiles, cash=100.0), Envelope(modes.STEADY, 100.0),
                     suggestion=Strategy(max_positions=1, position_pct=100.0, universe=["VTI"]),
                     suggested_by="model")
    first = doc["under_plan"]["path"][0]
    assert first["requested_usd"] == 35.0 and first["order_usd"] == 35.0     # the table's 35%


def test_without_a_suggestion_the_template_stands_in_and_the_plan_says_so(profiles):
    doc = plan.build(profiles, account(profiles), Envelope(modes.STEADY, 100.0),
                     suggestion_error="the Claude login on this Mac has expired")
    assert doc["strategy"]["suggested_by"] == "template"
    assert doc["strategy"]["suggestion_error"] == "the Claude login on this Mac has expired"
    assert doc["strategy"]["rationale"]["zh"] and doc["clamps"] == []
    assert doc["strategy"]["universe"] == ["VTI", "VXUS", "BND", "VGIT", "SGOV"]


def test_the_same_plan_has_the_same_id_and_a_different_one_does_not(profiles):
    one = plan.build(profiles, account(profiles), Envelope(modes.STEADY, 100.0))
    again = plan.build(profiles, account(profiles), Envelope(modes.STEADY, 100.0))
    other = plan.build(profiles, account(profiles), Envelope(modes.STEADY, 100.0, loss_pause_pct=5.0))
    assert one["plan_id"] == again["plan_id"] != other["plan_id"]


def test_asking_what_the_plan_would_do_changes_nothing_real(profiles):
    before = copy.deepcopy(profiles.risk)
    snap = account(profiles)
    cash = snap.buckets[MICRO].cash_usd
    plan.build(profiles, snap, Envelope(modes.STEADY, 100.0))
    assert profiles.risk == before, "the profiles a plan was built from are not edited by it"
    assert snap.buckets[MICRO].cash_usd == cash and snap.state.positions == []


# ---------------------------------------------------------------- the engine pieces a plan leans on
def _room(profiles, snap, **overrides):
    values = {"ticker": "VTI", "bucket": MICRO, "action": "BUY", "size_hint": "normal",
              "confidence": 100, "trade_date": TRADE_DATE, "autonomous": True,
              "cls": classify_instrument("VTI", profiles, identity_lookup)}
    values.update(overrides)
    return HardRiskEngine(profiles, halt_file="/nonexistent/HALT").order_room(
        RiskProposal(**values), snap)


def test_a_strategys_own_size_can_only_be_smaller_than_the_tables(profiles):
    snap = account(profiles, cash=1000.0)
    assert _room(profiles, snap)["as_asked"]["requested"] == 350.0                 # the table
    smaller = _room(profiles, snap, weight_override_pct=12.5)
    assert smaller["as_asked"]["requested"] == 125.0 and smaller["basis"] == "strategy target weight"
    larger = _room(profiles, snap, weight_override_pct=80.0)
    assert larger["as_asked"]["requested"] == 350.0 and "held to the configured 35% normal size" in larger["basis"]
    halved = _room(profiles, snap, weight_override_pct=20.0, size_hint="small")
    assert halved["as_asked"]["requested"] == 100.0 and "halved" in halved["basis"]


def test_a_size_that_is_not_a_positive_number_fails_closed(profiles):
    from agentic_trading.evidence import collect_evidence
    from agentic_trading.risk.ladder import LadderState, LadderStates, evaluate_ladders
    from agentic_trading.risk_scenarios import regime

    snap = account(profiles, cash=1000.0)
    ladders = LadderStates(LadderState(3000.0), LadderState(2500.0), LadderState(500.0), LadderState(1000.0))
    evaluate_ladders(ladders, profiles, TRADE_DATE, None, None, None, micro_live_equity=1000.0)
    for bad in (0.0, -5.0, float("nan")):
        prop = RiskProposal("VTI", MICRO, "BUY", "normal", 90, TRADE_DATE,
                            classify_instrument("VTI", profiles, identity_lookup),
                            data_quality="good", weight_override_pct=bad,
                            evidence=collect_evidence("VTI", TRADE_DATE, price_lookup=price_lookup))
        result = HardRiskEngine(profiles, halt_file="/nonexistent/HALT").evaluate(
            prop, snap, ladders, regime())
        assert result.verdict == "REJECT" and "engine_exception" in result.fail_closed_codes


def test_the_allowance_in_dollars_holds_when_it_is_smaller_than_the_share(profiles):
    opening = profiles.risk["buckets"][MICRO]["autonomy"]["open"]
    opening.update(max_pct_of_account=100, max_usd=50)
    room = _room(profiles, account(profiles, cash=1000.0))
    allowance = next(item for item in room["limits"] if item["code"] == "autonomous_allowance")
    assert allowance["room"] == 50.0
    assert room["cut_to_fit"]["order"] == 50.0 and room["cut_to_fit"]["binding"] == ["autonomous_allowance"]
    # Left as asked for, the same ceiling refuses it.
    assert room["as_asked"]["feasible"] is False and "HR23" in room["as_asked"]["refused_by"]


def test_sizing_only_says_an_order_exists_never_that_it_would_be_approved(profiles):
    """order_room reads no confidence, evidence, ladder or regime: it is arithmetic over size."""
    room = _room(profiles, account(profiles, cash=1000.0), confidence=None)
    assert room["as_asked"]["requested"] == 350.0
    assert sizing.fits_to_limits(profiles, MICRO) is False and room["fits_to_limits"] is False
