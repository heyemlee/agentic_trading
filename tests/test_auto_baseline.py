"""The baseline template: the owner's weights bought in tranches, research a veto (contract 2.1).

docs/AUTO_BASELINE_CONTRACT_2026-10-15.md, decisions D1-D6, all the owner's of 2026-10-15. What
is held here:

* the targets a preview shows, for the $100 account and for $1,000, worked out by the Hard Risk
  engine's own arithmetic, and the comparison at a $10 minimum that is never applied;
* the template's way out, fixed: a 15% stop, no longest holding period;
* a tranche needs no research confidence -- and in its place is only ever a low-tier fund the
  plan's version names, no larger than one tranche, in a market and ladder that allow it;
* research saying less of a fund vetoes it, and a fund nobody researched is not bought;
* tranches are spaced, a position that ended is not bought back for twenty sessions, and the
  simulated account sells once to show its exit works; the real account never does;
* what came of each fund is derived from the records, never counted apart.

Offline throughout: the simulated account at scripted prices, and nothing reads a venue.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from agentic_trading import autonomy, risk_scenarios
from agentic_trading.auto import modes, plan, positions, runner, store, trader
from agentic_trading.auto.plan import Envelope, PlanError, Strategy
from agentic_trading.portfolio import classify_instrument
from agentic_trading.proposals import EXECUTED, INVALIDATED, PROPOSED
from agentic_trading.risk import HardRiskEngine
from agentic_trading.risk import Proposal as RiskProposal
from agentic_trading.service.shapes import MarketStatus
from tests.auto_market import today
from tests.test_auto_lifecycle import Desk, sold
from tests.test_auto_live import real  # noqa: F401 - the real account's desk
from tests.test_auto_plan import account

MICRO = "micro_live"
BASELINE = "baseline_etf"


# ---------------------------------------------------------------- the targets a preview shows
def _baseline(profiles, cash):
    return plan.build(profiles, account(profiles, cash=cash), Envelope(modes.STEADY, cash),
                      template=BASELINE)


def test_at_100_the_baseline_buys_vti_alone_and_says_why_the_others_are_not_bought(profiles):
    doc = _baseline(profiles, 100.0)
    deployment = doc["strategy"]["deployment"]
    # The owner's weights over what the plan may buy: VTI 35, VXUS 10, BND 15, VGIT 10, SGOV 20.
    assert deployment["baseline_pct"] == {"VTI": 35.0, "VXUS": 10.0, "BND": 15.0, "VGIT": 10.0, "SGOV": 20.0}
    # Three positions at most: the smallest weights are left out first. Then the low tier's 60%
    # is shared out, and what falls under the $25 minimum is left out, smallest first, until
    # what remains clears it. VTI is held to the engine's normal size, 35% of the account.
    assert deployment["deployable_usd"] == 60.0
    assert deployment["targets"] == [{"symbol": "VTI", "baseline_pct": 35.0, "target_usd": 35.0, "tranches": 1}]
    out = {row["symbol"]: row for row in deployment["left_out"]}
    assert out["VXUS"]["code"] == out["VGIT"]["code"] == "tranche_position_limit"
    assert out["BND"]["code"] == out["SGOV"]["code"] == "tranche_below_minimum"
    assert out["BND"]["share_usd"] < 25.0 and out["SGOV"]["share_usd"] < 25.0
    assert deployment["cash_left_usd"] == 65.0 and deployment["minimum_order_usd"] == 25.0
    # The minimum is the owner's to change. What a $10 one would mean is shown, and not applied.
    assert doc["what_if_minimum_usd"] == {"10": [
        {"symbol": "VTI", "target_usd": 30.0, "tranches": 3},
        {"symbol": "SGOV", "target_usd": 17.14, "tranches": 1},
        {"symbol": "BND", "target_usd": 12.85, "tranches": 1}]}
    assert not any(change["path"] == "sizing.min_order_usd" for change in doc["changes"])
    assert "VTI $35.00（1 批）" in doc["summary"]["zh"] and "$65.00 留现金" in doc["summary"]["zh"]
    # The template's way out is its own (D5), and its rationale says what it does.
    strategy = doc["strategy"]
    assert (strategy["template"], strategy["stop_loss_pct"], strategy["take_profit_pct"],
            strategy["max_holding_days"], strategy["trend_window"]) == (BASELINE, 15.0, 0.0, 0, 0)
    assert "研究只用来否决" in strategy["rationale"]["zh"] and "不设最长持有期" in strategy["rationale"]["zh"]
    assert deployment["rules"] == {"tranches": 3, "spacing_sessions": 5, "veto_window_days": 14,
                                   "regime_skips": ["ELEVATED_RISK", "HIGH_RISK", "UNKNOWN"],
                                   "after_stop_cooldown_sessions": 20}


def test_at_1000_three_funds_are_bought_in_three_tranches_each(profiles):
    deployment = _baseline(profiles, 1000.0)["strategy"]["deployment"]
    assert deployment["deployable_usd"] == 600.0
    assert deployment["targets"] == [
        {"symbol": "VTI", "baseline_pct": 35.0, "target_usd": 300.0, "tranches": 3},
        {"symbol": "SGOV", "baseline_pct": 20.0, "target_usd": 171.42, "tranches": 3},
        {"symbol": "BND", "baseline_pct": 15.0, "target_usd": 128.57, "tranches": 3}]
    assert [row["symbol"] for row in deployment["left_out"]] == ["VXUS", "VGIT"]


def test_one_order_too_small_for_a_tranche_splits_it_further():
    targets, left_out = plan.baseline_targets(
        {"VTI": 35.0}, deployable_usd=600.0, position_usd={"VTI": 700.0}, order_usd={"VTI": 200.0},
        minimum_usd=25.0, tranches=3, max_positions=3)
    assert targets == [{"symbol": "VTI", "baseline_pct": 35.0, "target_usd": 600.0, "tranches": 3}]
    targets, _ = plan.baseline_targets(
        {"VTI": 35.0}, deployable_usd=700.0, position_usd={"VTI": 700.0}, order_usd={"VTI": 200.0},
        minimum_usd=25.0, tranches=3, max_positions=3)
    assert targets[0]["tranches"] == 4, "$700 in tranches of no more than $200"
    assert left_out == []
    # Nothing clears the minimum: no target, every fund said why.
    targets, left_out = plan.baseline_targets(
        {"VTI": 35.0, "BND": 15.0}, deployable_usd=30.0, position_usd={"VTI": 20.0, "BND": 20.0},
        order_usd={}, minimum_usd=25.0, tranches=3, max_positions=3)
    assert targets == [] and {row["symbol"] for row in left_out} == {"VTI", "BND"}


def test_the_strategy_that_was_simulated_is_the_one_that_starts(profiles):
    """The real account starts with the strategy simulated, targets and all; a template worked
    out again for an account that has since changed size would be a different version."""
    shown = _baseline(profiles, 100.0)
    handed = Strategy(**{key: shown["strategy"][key] for key in Strategy.model_fields
                         if key in shown["strategy"] and not key.startswith("rationale")})
    bigger = plan.build(profiles, account(profiles, cash=140.0), Envelope(modes.STEADY, 100.0),
                        suggestion=handed, suggested_by="template")
    assert bigger["strategy"]["deployment"]["targets"] == shown["strategy"]["deployment"]["targets"]
    assert store.version_id("steady", bigger["strategy"], {}) == store.version_id("steady", shown["strategy"], {})
    # One that could not have been drawn for this envelope is worked out again instead.
    forged = handed.model_copy(update={"deployment": {**handed.deployment, "targets": [
        {"symbol": "TQQQ", "target_usd": 90.0, "tranches": 1}]}})
    redrawn = plan.build(profiles, account(profiles, cash=100.0), Envelope(modes.STEADY, 100.0),
                         suggestion=forged, suggested_by="template")
    assert [row["symbol"] for row in redrawn["strategy"]["deployment"]["targets"]] == ["VTI"]
    # A different deployment is a different strategy version.
    other = {**shown["strategy"], "deployment": {**shown["strategy"]["deployment"], "targets": []}}
    assert store.version_id("steady", other, {}) != store.version_id("steady", shown["strategy"], {})


def test_the_baseline_template_is_the_steady_modes_alone(profiles):
    with pytest.raises(PlanError, match="not a template the swing mode can run"):
        plan.build(profiles, account(profiles), Envelope(modes.SWING, 100.0), template=BASELINE)
    research = plan.build(profiles, account(profiles), Envelope(modes.STEADY, 100.0))
    assert research["strategy"]["template"] == "research_etf" and research["strategy"]["deployment"] is None
    assert research["what_if_minimum_usd"] is None and research["deployment"] is None


# ---------------------------------------------------------------- the engine and the approval gate
def _tranche_proposal(symbol="VTI", *, cls, **over):
    base = {"ticker": symbol, "bucket": MICRO, "action": "BUY", "size_hint": "normal",
            "confidence": None, "trade_date": risk_scenarios.TRADE_DATE, "cls": cls,
            "source": "auto:tranche", "evidence": {"purpose": autonomy.TRANCHE_PURPOSE},
            "autonomous": True, "weight_override_pct": 35.0}
    return RiskProposal(**{**base, **over})


def test_a_tranche_needs_no_research_confidence_and_nothing_else_is_excused(profiles):
    from agentic_trading.risk import engine as engine_module
    from agentic_trading.risk.ladder import LadderState, LadderStates, evaluate_ladders

    snap = account(profiles, cash=100.0)
    engine = HardRiskEngine(profiles, halt_file="/nonexistent/test")
    ladders = LadderStates(LadderState(3000.0), LadderState(2500.0), LadderState(500.0), LadderState(100.0))
    evaluate_ladders(ladders, profiles, risk_scenarios.TRADE_DATE, None, None, None, micro_live_equity=100.0)
    evidence = {"purpose": autonomy.TRANCHE_PURPOSE, "symbol": "VTI", "source": "test",
                "price": risk_scenarios.PRICES["VTI"], "price_date": risk_scenarios.TRADE_DATE,
                "observed_at": datetime.now(UTC).isoformat(), "missing_required_reports": []}
    cls = classify_instrument("VTI", profiles, risk_scenarios.identity_lookup)
    asked = _tranche_proposal(cls=cls, evidence=evidence)
    rules = {r.rule_id: r for r in engine.evaluate(asked, snap, ladders, risk_scenarios.regime("NORMAL")).rules}
    assert rules["HR05"].passed and not rules["HR05"].blocking and "baseline tranche" in rules["HR05"].detail
    assert rules["HR05d"].passed and rules["HR05b"].passed
    # Its price is still the market's, checked like any other.
    no_price = engine.evaluate(_tranche_proposal(cls=cls, evidence={"purpose": autonomy.TRANCHE_PURPOSE}),
                               snap, ladders, risk_scenarios.regime("NORMAL"))
    assert {r.rule_id for r in no_price.failed_rules} >= {"HR05c"}
    # A tense regime asks a purchase for a confidence, which a tranche does not have.
    tense = engine.evaluate(asked, snap, ladders, risk_scenarios.regime("ELEVATED_RISK"))
    assert "HR05" in {r.rule_id for r in tense.failed_rules}
    # Anything else that calls itself a tranche is judged as what it is: a stock, a sale, a
    # purchase in another bucket, or one that carries a confidence of its own.
    stock = classify_instrument("AAPL", profiles, risk_scenarios.identity_lookup)
    assert engine_module._is_tranche(asked)
    for impostor in (_tranche_proposal("AAPL", cls=stock), _tranche_proposal(cls=cls, action="SELL"),
                     _tranche_proposal(cls=cls, bucket="core"), _tranche_proposal(cls=cls, confidence=90)):
        assert not engine_module._is_tranche(impostor), impostor
    sector = classify_instrument("XLK", profiles, risk_scenarios.identity_lookup)
    if sector.limit_class not in autonomy.TRANCHE_CLASSES:
        assert not engine_module._is_tranche(_tranche_proposal("XLK", cls=sector))
    # A proposal is one this module wrote as a tranche only with its note as well as its claim.
    assert not trader.is_tranche(SimpleNamespace(evidence={"purpose": autonomy.TRANCHE_PURPOSE}, history=[]))


@pytest.fixture
def armed(tmp_path):
    from agentic_trading.execution_scenarios import _profiles

    profiles = _profiles(tmp_path)
    profiles.risk["buckets"][MICRO]["autonomy"] = {"open": {
        "enabled": True, "min_confidence": 80, "classes": ["broad_diversified_etf", "bond_or_cash_like_etf"],
        "max_new_positions_per_day": 1}}
    return profiles


def _permits(profiles, **over):
    facts = {"template": BASELINE, "symbol": "VTI",
             "target": {"symbol": "VTI", "target_usd": 35.0, "tranches": 1}, "tranche_usd": 35.0}
    base = {"bucket": MICRO, "action": "BUY", "confidence": None,
            "limit_class": "broad_diversified_etf", "tranche": facts, "requested_usd": 35.0}
    return autonomy.permits(profiles, **{**base, **over})


def test_code_approves_a_tranche_only_as_the_version_wrote_it(armed):
    profiles = armed
    assert _permits(profiles).permitted, _permits(profiles).reasons
    assert "a baseline tranche" in _permits(profiles).reasons[-1]
    assert not _permits(profiles, requested_usd=35.02).permitted, "more than one tranche"
    assert not _permits(profiles, confidence=95).permitted, "a confidence: not a tranche"
    assert not _permits(profiles, limit_class="individual_stock").permitted
    assert not _permits(profiles, tranche={"template": "research_etf", "target": {"symbol": "VTI"},
                                           "tranche_usd": 35.0}).permitted
    assert not _permits(profiles, tranche={"template": BASELINE, "target": None, "tranche_usd": 0.0}).permitted
    # And a research purchase still needs its confidence, exactly as before.
    research = autonomy.permits(profiles, bucket=MICRO, action="BUY", confidence=None,
                                limit_class="broad_diversified_etf")
    assert not research.permitted and "no usable confidence" in research.reasons[-1]


# ---------------------------------------------------------------- one fund's status, from its facts
DEPLOYMENT = {"targets": [{"symbol": "VTI", "baseline_pct": 35.0, "target_usd": 300.0, "tranches": 3}],
              "left_out": [{"symbol": "BND", "baseline_pct": 15.0, "share_usd": 12.85,
                            "code": "tranche_below_minimum"},
                           {"symbol": "VXUS", "baseline_pct": 10.0, "share_usd": None,
                            "code": "tranche_position_limit"}],
              "minimum_order_usd": 25.0, "max_positions": 3, "cash_left_usd": 700.0,
              "rules": {"tranches": 3, "spacing_sessions": 5, "veto_window_days": 14,
                        "regime_skips": ["ELEVATED_RISK", "HIGH_RISK", "UNKNOWN"],
                        "after_stop_cooldown_sessions": 20}}
GOOD = {"VTI": {"run_id": "r1", "rating": "Hold", "reviewer_action": "HOLD", "at": "2026-10-14T14:00:00+00:00",
                "negative": False}}


def _row(facts=None, research=GOOD, gate=None, day="2026-10-15"):
    rows = trader.tranche_rows(DEPLOYMENT, facts={"VTI": facts} if facts else {}, research=research,
                               gate=gate, trade_date=day)
    return {row["symbol"]: row for row in rows}


def _facts(**over):
    return {"done": 0, "waiting": None, "held_usd": 0.0, "ended": None, "last_fill_day": None,
            "tried_today": 0, **over}


def test_each_status_is_read_off_the_records_in_the_contracts_order():
    due = _row()["VTI"]
    assert due["status"] == "due" and due["next"] == {"index": 1, "of": 3, "usd": 100.0, "due": "2026-10-15"}
    assert due["tranches"] == {"planned": 3, "done": 0} and due["diagnosis"]["code"] == "tranche_due"
    # Spaced: the second waits five sessions from the first one's fill.
    waiting = _row(_facts(done=1, last_fill_day="2026-10-15", held_usd=100.0))["VTI"]
    assert waiting["status"] == "waiting" and waiting["next"]["due"] == "2026-10-22"
    assert waiting["next"]["index"] == 2 and "10-22" in waiting["why"]["zh"]
    assert _row(_facts(done=1, last_fill_day="2026-10-15"), day="2026-10-22")["VTI"]["status"] == "due"
    assert _row(_facts(done=3))["VTI"]["status"] == "complete"
    # A position that ended is not bought back for twenty sessions; after the drill it is spaced.
    ended = {"at": "2026-10-15T15:00:00+00:00", "day": "2026-10-15", "drill": False, "stop": True}
    cooling = _row(_facts(ended=ended))["VTI"]
    assert cooling["status"] == "cooling_down" and cooling["next"]["due"] == "2026-11-12"
    drilled = _row(_facts(ended={**ended, "drill": True, "stop": False}))["VTI"]
    assert drilled["status"] == "waiting" and drilled["next"]["due"] == "2026-10-22"
    # The market, then research.
    gated = _row(gate={"gate": "regime", "level": "ELEVATED_RISK"})["VTI"]
    assert gated["status"] == "regime_gate" and "风险偏高" in gated["why"]["zh"]
    assert "回撤阶梯" in _row(gate={"gate": "ladder", "level": "WARNING"})["VTI"]["why"]["zh"]
    vetoed = _row(research={"VTI": {**GOOD["VTI"], "rating": "Underweight", "negative": True}})["VTI"]
    assert vetoed["status"] == "vetoed" and vetoed["veto"]["run_id"] == "r1"
    assert vetoed["veto"]["text"]["zh"] == "10-14 的研究给出“减持”，这一批不买。"
    reviewed = _row(research={"VTI": {**GOOD["VTI"], "reviewer_action": "SELL", "negative": True}})["VTI"]
    assert "“卖出”" in reviewed["veto"]["text"]["zh"]
    assert _row(research={})["VTI"]["status"] == "not_researched"
    # Funds left out say why, every time.
    rows = _row()
    assert rows["BND"]["status"] == "below_minimum" and "$12.85" in rows["BND"]["why"]["zh"]
    assert rows["VXUS"]["status"] == "position_limit" and "3 个仓位" in rows["VXUS"]["why"]["zh"]


def test_sessions_are_the_exchanges():
    assert trader.sessions_after("2026-10-15", 5) == "2026-10-22"
    assert trader.sessions_after("2026-10-17", 0) == "2026-10-19", "a Saturday belongs to Monday"
    assert trader.sessions_after("2026-11-25", 1) == "2026-11-27", "Thanksgiving is no session"


def test_research_is_read_newest_first_within_its_window():
    now = datetime(2026, 10, 15, 18, tzinfo=UTC)

    def record(run, ticker, rating, days, review=None):
        return ({"run_id": run, "ticker": ticker, "created_at": (now - timedelta(days=days)).isoformat(),
                 "upstream_decision": {"portfolio_manager_signal": rating},
                 "policy": {"reviewer_action": review}}, None)

    seen = trader.research_by_fund([record("new", "VTI", "Buy", 1), record("old", "VTI", "Sell", 3),
                                    record("blank", "BND", "", 1), record("bnd", "BND", "Hold", 2, "REDUCE"),
                                    record("stale", "SGOV", "Buy", 15)], window_days=14, now=now)
    assert seen["VTI"]["run_id"] == "new" and seen["VTI"]["negative"] is False
    assert seen["BND"]["run_id"] == "bnd" and seen["BND"]["negative"] is True, "the reviewer said reduce"
    assert "SGOV" not in seen, "fifteen days old is outside the window"


# ---------------------------------------------------------------- in the simulated account
@pytest.fixture
def small(tmp_path, monkeypatch, offline_market_reads):
    """The simulated account at $100 under a baseline plan: VTI, one tranche of $35."""
    return Desk(tmp_path, monkeypatch, cash=100.0, template=BASELINE)


@pytest.fixture
def larger(tmp_path, monkeypatch, offline_market_reads):
    """At $1,000, three tranches a fund, and room for three openings a day."""
    return Desk(tmp_path, monkeypatch, cash=1000.0, openings_per_day=3, template=BASELINE)


def _tranche_rows(result):
    return {row["ticker"]: row for row in result.signals if row.get("kind") == "tranche"}


def test_a_tranche_is_bought_on_research_that_does_not_say_no(small):
    desk = small
    assert desk.active["strategy"]["template"] == BASELINE
    desk.research("VTI", rating="Buy", confidence=88)
    first = desk.tick()
    # Research is a veto under this template, never a reason to buy ...
    research = next(row for row in first.signals if row.get("run_id"))
    assert research["diagnosis"]["code"] == "baseline_research_not_a_buy"
    # ... and the tranche is the plan's own: no confidence, no run, bought by code.
    assert _tranche_rows(first)["VTI"]["diagnosis"]["code"] == "tranche_written"
    bought = next(p for p in desk.proposals() if p.ticker == "VTI")
    assert (bought.status, bought.action, bought.confidence, bought.run_id) == (EXECUTED, "BUY", None, None)
    assert trader.is_tranche(bought) and bought.evidence["tranche"]["index"] == 1
    assert bought.approved_by == "autonomy:open" and bought.notional_usd == pytest.approx(35.0, abs=0.01)
    assert bought.strategy_version == desk.active["opening_version"]
    # It is the plan's, held to the template's stop and no term.
    held = desk.managed()
    assert held.quantity > 0 and held.version_id == bought.strategy_version
    version = store.version(desk.sim.state_db(), held.version_id)
    assert version["strategy"]["stop_loss_pct"] == 15.0 and version["strategy"]["max_holding_days"] == 0
    # Bought to its target, nothing more is written; the page reads what the pass saw.
    second = desk.tick()
    assert "VTI" not in _tranche_rows(second)
    assert len([p for p in desk.proposals() if p.action == "BUY"]) == 1
    view = desk.live.auto_simulation()["deployment"]
    rows = {row["symbol"]: row for row in view["targets"]}
    assert rows["VTI"]["status"] == "complete" and rows["VTI"]["tranches"] == {"planned": 1, "done": 1}
    assert rows["VTI"]["held_usd"] == pytest.approx(35.0, abs=0.5)
    assert view["plan_id"] == desk.active["plan_id"] and view["regime"]["open"] is True
    assert {row["status"] for symbol, row in rows.items() if symbol != "VTI"} <= {"below_minimum", "position_limit"}


def test_research_saying_less_of_it_vetoes_the_tranche_and_withdraws_one_that_waits(small, monkeypatch):
    desk = small
    # Written while the market is shut, it waits for the open ...
    monkeypatch.setattr(desk.sim, "market_status", lambda: MarketStatus(is_open=False))
    desk.research("VTI", rating="Hold", confidence=70)
    desk.tick()
    waiting = next(p for p in desk.proposals() if p.ticker == "VTI")
    assert waiting.status == PROPOSED and trader.is_tranche(waiting)
    # ... and research saying less of it arrives first.
    run = desk.research("VTI", rating="Underweight", confidence=72, reviewer="REDUCE")
    vetoed = desk.tick()
    row = _tranche_rows(vetoed)["VTI"]
    assert row["diagnosis"]["code"] == "tranche_vetoed" and row["status"] == "vetoed"
    gone = next(p for p in desk.proposals() if p.proposal_id == waiting.proposal_id)
    assert gone.status == INVALIDATED and gone.history[-1]["event"] == "auto_tranche_withdrawn"
    view = trader.last_deployment(desk.sim.state_db())
    vti = next(r for r in view["targets"] if r["symbol"] == "VTI")
    assert vti["veto"]["run_id"] == run and "减持" in vti["veto"]["text"]["zh"]
    monkeypatch.setattr(desk.sim, "market_status", lambda: MarketStatus(is_open=True))
    desk.tick()
    assert desk.held() == 0.0 and not desk.broker._orders


def test_a_fund_nobody_researched_is_not_bought(small):
    result = small.tick()
    assert _tranche_rows(result)["VTI"]["diagnosis"]["code"] == "tranche_not_researched"
    assert small.proposals() == []


@pytest.mark.parametrize("level", ["ELEVATED_RISK", "HIGH_RISK", "UNKNOWN"])
def test_a_tense_regime_skips_the_tranche(small, monkeypatch, level):
    from agentic_trading import cli

    monkeypatch.setattr(cli, "assess_regime", lambda *_a, **_k: risk_scenarios.regime(level))
    small.research("VTI", rating="Buy", confidence=90)
    row = _tranche_rows(small.tick())["VTI"]
    assert row["diagnosis"]["code"] == "tranche_regime_gate" and small.proposals() == []


def test_tranches_are_spaced_and_bought_toward_the_target(larger, monkeypatch):
    desk = larger
    # The drill would sell the first tranche as soon as the clock moves: it has its own test.
    monkeypatch.setattr(trader, "drill_pass", lambda *_a, **_k: [])
    desk.research("VTI", rating="Hold", confidence=70)
    desk.tick()
    first = [p for p in desk.proposals() if p.ticker == "VTI"]
    assert len(first) == 1 and first[0].status == EXECUTED and first[0].notional_usd == pytest.approx(100.0, abs=0.01)
    # The next is five sessions on: it waits, and says until when.
    waiting = _tranche_rows(desk.tick())["VTI"]
    assert waiting["status"] == "waiting" and waiting["diagnosis"]["code"] == "tranche_not_due"
    view = {r["symbol"]: r for r in trader.last_deployment(desk.sim.state_db())["targets"]}
    assert view["VTI"]["next"]["due"] == trader.sessions_after(today(), 5)
    # SGOV and BND have targets and nobody researches them, so they are not bought.
    assert view["SGOV"]["status"] == view["BND"]["status"] == "not_researched"
    # Five sessions later (the clock is the test's), the second; then the third; then no more.
    monkeypatch.setattr(trader, "sessions_after", lambda day, _count: str(day)[:10])
    for index in (2, 3):
        desk.tick()
        last = [p for p in desk.proposals() if p.ticker == "VTI"][-1]
        assert last.action == "ADD" and last.status == EXECUTED and last.evidence["tranche"]["index"] == index
    desk.tick()
    assert len([p for p in desk.proposals() if p.ticker == "VTI"]) == 3
    view = {r["symbol"]: r for r in trader.last_deployment(desk.sim.state_db())["targets"]}
    assert view["VTI"]["status"] == "complete" and view["VTI"]["held_usd"] == pytest.approx(300.0, abs=1.0)


def test_after_a_stop_the_fund_is_not_bought_back_for_twenty_sessions(small, monkeypatch):
    desk = small
    desk.research("VTI", rating="Hold", confidence=70)
    desk.tick()
    assert desk.held() > 0
    desk.move("VTI", 0.84)                                   # past the template's 15% stop
    stopped = desk.tick()
    assert stopped.exits[0]["exit"]["reason"] == positions.STOP_LOSS and desk.held() == 0.0
    cooling = _tranche_rows(desk.tick())["VTI"]
    assert cooling["status"] == "cooling_down" and cooling["diagnosis"]["code"] == "tranche_cooling_down"
    view = {r["symbol"]: r for r in trader.last_deployment(desk.sim.state_db())["targets"]}
    assert view["VTI"]["next"]["due"] == trader.sessions_after(today(), 20)
    assert len([p for p in desk.proposals() if p.action == "BUY"]) == 1
    # Twenty sessions on, it is due again -- and the engine's own rules still stand between it and
    # a purchase today: a fund sold this session is not bought back in it.
    monkeypatch.setattr(trader, "sessions_after", lambda day, _count: str(day)[:10])
    again = _tranche_rows(desk.tick())["VTI"]
    assert again["status"] == "due" and again["diagnosis"]["code"] in trader.NOT_NOW, again
    assert len([p for p in desk.proposals() if p.action == "BUY"]) == 1


def test_the_simulated_account_sells_once_to_show_its_exit_works(small, monkeypatch):
    desk = small
    desk.research("VTI", rating="Hold", confidence=70)
    desk.tick()
    version = desk.active["opening_version"]
    assert not runner.exits_verified(desk.sim, desk.sim.profiles(), version)
    assert all(row["exit"]["reason"] != positions.DRILL for row in desk.tick().exits), "not before five sessions"
    monkeypatch.setattr(trader, "sessions_after", lambda day, _count: str(day)[:10])
    drilled = desk.tick()
    drill = next(row for row in drilled.exits if row["exit"]["reason"] == positions.DRILL)
    assert "卖出演练" in drill["exit"]["text"]["zh"]
    sale = next(p for p in desk.proposals() if p.action == "SELL")
    assert sale.status == EXECUTED and sale.approved_by == "autonomy:exit" and desk.held() == 0.0
    assert any(entry.get("reason") == positions.DRILL for entry in sale.history)
    assert runner.exits_verified(desk.sim, desk.sim.profiles(), version)
    assert sold(desk.broker) > 0
    # Once is all the record needs; afterwards it is bought back by the tranche rules, not cooled.
    after = desk.tick()
    assert all(row["exit"]["reason"] != positions.DRILL for row in after.exits)
    assert _tranche_rows(after)["VTI"]["status"] == "due"


def test_the_real_account_never_drills(small):
    book = SimpleNamespace(plan={"strategy": {"template": BASELINE}, "opening_version": "sv-x"},
                           profiles=small.live.profiles())
    assert trader.drill_pass(book, blocked=[]) == []


# ---------------------------------------------------------------- over HTTP
def test_a_baseline_preview_and_its_proposals_over_http(auth_client, small, monkeypatch):
    monkeypatch.setattr("agentic_trading.api.app._service", small.live)
    previewed = auth_client.post("/auto-trading/plan", json={"capital_usd": 100.0, "template": BASELINE})
    assert previewed.status_code == 200, previewed.text
    body = previewed.json()
    assert body["strategy"]["template"] == BASELINE and body["deployment"]["template"] == BASELINE
    rows = {row["symbol"]: row for row in body["deployment"]["targets"]}
    assert rows["VTI"]["status"] == "not_researched" and rows["VTI"]["target_usd"] == 35.0
    assert body["deployment"]["regime"] == {"level": None, "ladder": None, "open": None}
    assert body["what_if_minimum_usd"]["10"][0]["symbol"] == "VTI"
    refused = auth_client.post("/auto-trading/plan", json={"capital_usd": 100.0, "template": "grid"})
    assert refused.status_code == 422
    research = auth_client.post("/auto-trading/plan", json={"capital_usd": 100.0})
    assert research.json()["deployment"] is None and research.json()["strategy"]["template"] == "research_etf"
    # A tranche in the simulated account carries its plan block, and says it is not the first
    # real-money purchase there: the simulated account never waits.
    small.research("VTI", rating="Hold", confidence=70)
    small.tick()
    monkeypatch.setattr("agentic_trading.api.app._service", small.sim)
    listed = auth_client.get("/proposals?include_closed=true")
    assert listed.status_code == 200, listed.text
    tranche = next(row for row in listed.json() if row["ticker"] == "VTI")
    assert tranche["plan"]["origin"] == "tranche" and tranche["plan"]["tranche"] == {"index": 1, "of": 1}
    assert tranche["plan"]["template"] == BASELINE and tranche["plan"]["managed_by_plan"] is True
    assert tranche["plan"]["exit_terms"] == {"stop_loss_pct": 15.0, "take_profit_pct": 0.0,
                                             "max_holding_days": None, "trend_window": None}
    assert tranche["plan"]["first_real_order"] is False and tranche["plan"]["waits_for_person"] is None
    assert tranche["confidence"] is None and tranche["run_id"] is None


# ---------------------------------------------------------------- the real account
def test_on_the_real_account_the_first_tranche_is_the_owners_and_carries_no_confidence(real):  # noqa: F811
    """A baseline plan that ran in the simulated account -- its stop seen to work there -- put in
    force on the real one. Its first tranche waits for the owner (contract 2.5); approved, it is
    revalidated and sent like any order, with no research confidence asked of it (D4)."""
    real.simulate(template=BASELINE)
    real.research("VTI", rating="Hold", confidence=70)
    real.service.auto_tick()                                   # the simulated account buys its tranche
    real.move("VTI", 0.84)
    real.service.auto_tick()                                   # and sells it on the template's stop
    real.move("VTI", 1 / 0.84)
    sim = real.sim()
    simulated = store.active_plan(sim.state_db())
    assert simulated["strategy"]["template"] == BASELINE
    assert runner.exits_verified(sim, sim.profiles(), simulated["opening_version"])
    assert real.confirm_book()["confirmed"] is True
    real.start()
    real.cool()
    assert real.service.auto_tick()["live"]["activation"]["request"] == "activated"
    active = store.active_plan(real.db)
    assert active["strategy"]["deployment"] == simulated["strategy"]["deployment"]

    real.service.auto_tick()
    waiting = next(p for p in real.proposals() if p.ticker == "VTI")
    assert trader.is_tranche(waiting) and waiting.confidence is None and waiting.status == PROPOSED
    block = real.service.proposal(waiting.proposal_id).plan
    assert block["first_real_order"] is True and block["waits_for_person"]["code"] == "first_real_order"
    assert block["origin"] == "tranche" and block["exit_terms"]["stop_loss_pct"] == 15.0
    report = real.service.autonomous_reductions()
    assert report["approved"] == [] and report["declined"][0]["proposal_id"] == waiting.proposal_id
    to_do = [item for item in real.service.attention()["items"] if item["kind"] == "proposal_waiting"]
    assert len(to_do) == 1 and "方案在实盘的第一笔买入" in to_do[0]["detail_i18n"]["zh"]
    bought = real.by_hand(waiting)
    assert bought.status == EXECUTED and (bought.approved_via, bought.approved_by) == ("console", "owner")
    assert real.held("VTI") > 0
    # It is the plan's, on the plan's stop -- and once seen through, nothing waits on the owner.
    assert {view["symbol"] for view in real.service.auto_live()["positions"]} == {"VTI"}
    assert real.service.proposal(waiting.proposal_id).plan["waits_for_person"] is None
