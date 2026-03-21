"""Changing mode on the real account, and what a mode being open does not open (docs/todo.md, P3.6).

A steady plan is in force and holds something. The owner then starts a swing plan -- behind the
same gates as any start, plus the two decisions about swing that are only theirs, which this
file makes in a copy of the configuration because a test cannot wait for them. What is held:

* the mode that was in force opens nothing more and goes on managing what it bought, on the
  terms it bought it under -- its own stop, not the new mode's tighter one;
* the new mode opens with what is left, and both are inside one set of account limits: the
  positions the old mode holds count against what the new one may hold;
* a fund the old mode still holds is not bought a second time by the new one;
* none of this says anything about single stocks, which are not a mode and do not open because
  one did.

The market is the test's own (tests/auto_market.py).
"""

import pytest

from agentic_trading import cli
from agentic_trading import config_changes as cc
from agentic_trading.auto import modes, runner, store
from agentic_trading.proposals import EXECUTED
from agentic_trading.service import ServiceError
from tests.auto_market import scripted_history
from tests.test_auto_live import MICRO, Real


def _studied_daily(risk_profile):
    """The plan's funds studied every session: something the owner sets in Settings, set in a copy.

    Swing is designed on research every session, and stays shut for real money while its funds
    are studied weekly or not at all (``modes.blockers``). The other thing only the owner can
    decide about swing -- whether timing with a trend rule is "momentum" -- is not decided here:
    the plan puts that question to them itself, and these tests answer it the way a person would.
    """
    text = risk_profile.read_text(encoding="utf-8")
    study = "candidates: {period: weekly, max_tickers_per_run: 3, enabled: false}"
    assert text.count(study) == 1, "the configuration reads differently now"
    risk_profile.write_text(text.replace(
        study, "candidates: {period: daily, max_tickers_per_run: 3, enabled: true}"), encoding="utf-8")


SWING_QUESTION = "buckets.micro_live.autonomy.open.modes"


def _buy(real, symbol, confidence=90):
    """Research says buy; the pass writes it and the approval loop sends it. Returns the proposal."""
    real.research(symbol, confidence=confidence)
    tick = real.service.auto_tick()
    report = real.service.autonomous_reductions()
    real.forget_research()
    found = [p for p in real.proposals() if p.ticker == symbol and p.action == "BUY"]
    assert found and found[-1].status == EXECUTED, (
        symbol, report, [(e.get("event"), e.get("detail")) for e in found[-1].history[-4:]] if found
        else tick["live"]["pass"]["signals"])
    return found[-1]


def _simulate_swing(real):
    """A swing plan in the simulated account, with its sessions and an exit of its own."""
    swing = real.simulate(mode="swing")
    sim = real.sim()
    real.research("BND")
    runner.run(sim, research_from=real.runs)
    real.move("BND", 0.92)                       # past swing's 6% stop
    runner.run(sim, research_from=real.runs)
    real.move("BND", 1 / 0.92)
    real.forget_research()
    assert runner.exits_verified(sim, sim.profiles(), swing), "the swing exit did not happen"
    return swing


def _switch(real):
    """Start what the simulated account is running on the real account, as a person would."""
    assert real.confirm_book()["confirmed"] is True
    failing = [code for code, row in real.gates().items() if not row["ok"]]
    assert failing in ([], ["settings_pending"]), failing
    answer = real.start()
    if not answer["started"]:
        real.cool()
        assert real.service.auto_tick()["live"]["activation"]["request"] == "activated"
    return store.active_plan(real.db)


@pytest.fixture
def steady_in_force(tmp_path, monkeypatch, offline_market_reads):
    real = Real(tmp_path, monkeypatch, openings_per_day=6, configure=_studied_daily)
    scripted_history(monkeypatch)
    return real, real.in_force()["opening_version"]


def test_the_old_mode_keeps_what_it_bought_and_the_new_one_opens_with_what_is_left(steady_in_force):
    real, steady = steady_in_force
    first = _buy(real, "VXUS")
    assert first.strategy_version == steady
    swing = _simulate_swing(real)
    candidate = real.candidate()
    assert candidate["plan"]["envelope"]["mode"] == "swing" and candidate["version_id"] == swing

    # The plan puts its one question to the owner as a setting of its own, in so many words.
    asked = next(change for change in candidate["plan"]["changes"] if change["path"] == SWING_QUESTION)
    assert asked["to"] == ["steady", "swing"] and asked["kind"] == "boundary"
    assert "只有你能做的决定" in asked["why"]["zh"] and "动量" in asked["why"]["zh"]
    assert "only you can make" in asked["why"]["en"]
    landing = {row["path"]: row for row in candidate["plan"]["landing"]["changes"]}
    assert landing[SWING_QUESTION]["waits"] is True, "it loosens, so it takes the password and a day"
    # Until they answer, swing is not a mode code may open under -- the real configuration says so.
    assert modes.allowed_modes(real.service.profiles()) == ("steady",)

    active = _switch(real)
    assert modes.allowed_modes(real.service.profiles()) == ("steady", "swing")
    # The rule about momentum strategies is exactly where it was: the preflight still holds it.
    gated = real.service.profiles().risk_cfg("strategy_policy", "sandbox",
                                             "strategies_requiring_human_approval_always")
    assert "momentum" in gated
    assert active["envelope"]["mode"] == "swing" and active["scope"] == "live"
    assert active["opening_version"] == swing and active["managing"] == [steady]
    status = real.service.auto_status()
    assert status["real_account"]["opened_modes"] == ["steady", "swing"]
    assert [row["key"] for row in status["modes"] if row["open_for_real_money"]] == ["swing"]

    # What steady bought is still steady's, on steady's terms. Down 7%: past the 6% stop a swing
    # position is held to, inside the 10% this one was bought under.
    view = next(p for p in real.service.auto_live()["positions"] if p["symbol"] == "VXUS")
    assert view["strategy_version"] == steady
    real.move("VXUS", 0.93)
    exits = {row["symbol"]: row for row in real.service.auto_tick()["live"]["pass"]["exits"]}
    assert exits["VXUS"]["strategy_version"] == steady and exits["VXUS"]["exit"]["due"] is False
    assert real.held("VXUS") > 0

    # The same fund is not bought again by the new mode while the old one holds it.
    real.research("VXUS", confidence=95)
    again = real.service.auto_tick()["live"]["pass"]["signals"][0]
    assert again["diagnosis"]["code"] == "already_held"
    real.forget_research()

    # The new mode opens something else, under its own version.
    second = _buy(real, "BND")
    assert second.strategy_version == swing
    rows = {p["symbol"]: p["strategy_version"] for p in real.service.auto_live()["positions"]}
    assert rows == {"VXUS": steady, "BND": swing}

    # One account, one set of limits: the swing plan holds two positions at most, and the one
    # steady still holds is one of them.
    assert active["strategy"]["max_positions"] == 2
    real.research("VTI", confidence=95)
    full = real.service.auto_tick()["live"]["pass"]["signals"][0]
    assert full["diagnosis"]["code"] == "plan_positions_full"
    real.forget_research()
    ledger = real.ledger()
    invested = sum(float(p.quantity) * real.prices[p.symbol] for p in ledger.positions)
    assert invested <= float(active["envelope"]["capital_usd"]) + 0.01, "never more than was confirmed"

    # Steady's position leaves when it meets steady's own stop, and only then.
    real.move("VXUS", 0.95)                       # 11.65% down from where it was bought
    stopped = {row["symbol"]: row for row in real.service.auto_tick()["live"]["pass"]["exits"]}
    assert stopped["VXUS"]["exit"]["reason"] == "stop_loss"
    assert stopped["VXUS"]["exit"]["facts"]["limit_pct"] == "10"
    assert stopped["BND"]["exit"]["due"] is False
    real.service.autonomous_reductions()
    assert real.held("VXUS") == 0.0 and real.held("BND") > 0
    closing = next(p for p in real.proposals() if p.action == "SELL")
    assert closing.strategy_version == steady and closing.approved_by == "autonomy:exit"
    # With it gone, the fund is no longer "already held": whatever the answer now is, it is the
    # new mode's own (here its trend rule, on a fund that is falling).
    real.research("VXUS", confidence=95)
    later = real.service.auto_tick()["live"]["pass"]["signals"][0]
    assert later["diagnosis"]["code"] != "already_held"


def test_swing_stays_shut_while_its_funds_are_not_studied_every_session(tmp_path, monkeypatch,
                                                                       offline_market_reads):
    """The same switch in the configuration as it really is: refused, with the reason."""
    real = Real(tmp_path, monkeypatch, openings_per_day=6)
    scripted_history(monkeypatch)
    steady = real.in_force()["opening_version"]
    _simulate_swing(real)
    real.confirm_book()
    gates = real.gates()
    assert gates["daily_research_not_scheduled"]["ok"] is False
    assert "earlier_mode_opened" not in gates, "steady has been opened; that is not what stops it"
    assert real.candidate()["may_confirm"] is False
    with pytest.raises(ServiceError, match="not studied every session"):
        real.start()
    assert store.active_plan(real.db)["opening_version"] == steady
    assert store.start_request(real.db) is None


def test_the_owners_answer_about_swing_is_held_where_an_order_is_approved(steady_in_force):
    """Not only where a plan is started: a swing purchase is declined if the mode is not allowed."""
    real, _steady = steady_in_force
    _simulate_swing(real)
    _switch(real)
    assert modes.allowed_modes(real.service.profiles()) == ("steady", "swing")
    # The owner takes the answer back in Settings, with a swing plan still in force. Shortening
    # the list tightens, so it applies at once and takes no password.
    taken_back = real.service.submit_config_change(
        file="risk_profile.yaml", set={SWING_QUESTION: ["steady"]}, identity="owner",
        reason="not sure about swing after all")
    assert taken_back["status"] == cc.APPLIED and taken_back["classification"] == cc.IMMEDIATE
    assert modes.allowed_modes(real.service.profiles()) == ("steady",)
    real.research("BND", confidence=90)
    written = real.service.auto_tick()["live"]["pass"]["signals"][0]
    assert written["proposal_id"], "the pass still writes what the plan decided"
    report = real.service.autonomous_reductions()
    assert report["considered"] == 1 and report["approved"] == []
    assert "the swing mode is not among the modes" in report["declined"][0]["reasons"][0]
    assert real.held("BND") == 0.0 and real.venue._orders == {}
    # It waits where a person can see it, and they may still approve it themselves.
    assert next(p for p in real.proposals() if p.ticker == "BND").status == "PROPOSED"


def test_a_swing_plan_is_never_in_force_before_its_question_is_answered(steady_in_force):
    real, steady = steady_in_force
    _simulate_swing(real)
    real.confirm_book()
    gates = real.gates()
    # The question is being put, with the plan's settings; it is not a separate refusal ...
    assert "swing_policy_undecided" not in gates and gates["settings_pending"]["ok"] is False
    assert SWING_QUESTION in gates["settings_pending"]["text"]["en"]
    answer = real.start()
    assert answer["started"] is False and SWING_QUESTION in answer["waiting"]["waiting_for"]
    # ... and while the answer has not landed, steady is what is in force and swing opens nothing.
    assert store.active_plan(real.db)["opening_version"] == steady
    assert real.service.auto_tick()["live"]["activation"]["request"] == "waiting"
    # Withdrawn in Settings, the question is unanswered again and the start is over.
    real.service.auto_withdraw_request(reason="not yet", identity="owner")
    assert modes.allowed_modes(real.service.profiles()) == ("steady",)
    assert real.gates()["settings_pending"]["ok"] is False
    assert store.active_plan(real.db)["opening_version"] == steady


def test_a_mode_cannot_be_started_before_the_one_ahead_of_it(tmp_path, monkeypatch, offline_market_reads):
    real = Real(tmp_path, monkeypatch, openings_per_day=6, configure=_studied_daily)
    scripted_history(monkeypatch)
    _simulate_swing(real)
    real.confirm_book()
    gates = real.gates()
    assert gates["earlier_mode_opened"]["ok"] is False and "稳健" in gates["earlier_mode_opened"]["text"]["zh"]
    assert gates["simulation_record"]["ok"] is True, "its own record is not what is missing"
    with pytest.raises(ServiceError, match="Modes open in order"):
        real.start()


def test_a_fund_mode_being_open_opens_nothing_for_single_stocks(steady_in_force):
    real, _steady = steady_in_force
    status = real.service.auto_status()
    assert [row["key"] for row in status["modes"] if row["open_for_real_money"]] == ["steady"]
    stocks = status["extensions"][0]
    assert stocks["key"] == "stocks"
    assert stocks["open_for_real_money"] is False and stocks["can_simulate"] is False
    codes = {item["code"] for item in stocks["blockers"]}
    assert codes >= {"stocks_not_in_any_plan", "company_events_unvalidated", "liquidity_unvalidated",
                     "concentration_unvalidated", "stocks_need_their_own_record"}
    said = next(item for item in stocks["blockers"] if item["code"] == "stocks_need_their_own_record")
    assert "不能算作个股的记录" in said["text"]["zh"] and "separate confirmation" in said["text"]["en"]

    # Research that says buy a stock, as sure as research gets, is not the plan's to act on ...
    real.research("AAPL", confidence=99)
    signal = real.service.auto_tick()["live"]["pass"]["signals"][0]
    assert signal["diagnosis"]["code"] == "not_in_plan" and real.proposals() == []
    # ... and is not deferred to it either: it is written where a person decides, as always.
    assert cli._deferred_to_plan(real.db, MICRO, "AAPL", "BUY") is None
    # Nor can a strategy be handed in with one: it is dropped at the bounds, by name.
    plan = store.active_plan(real.db)
    assert "AAPL" not in plan["strategy"]["universe"]
    assert modes.stock_extension(real.service.profiles())["blockers"] == stocks["blockers"]
