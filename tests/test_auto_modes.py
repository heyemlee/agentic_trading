"""Three modes that run differently, and one that says exactly why it does not run yet.

docs/todo.md, P2.5. A mode is what decides a trade, how often, and how a position ends:

* steady  -- research, weekly; out at the stop or the end of a long holding period;
* swing   -- research, every session, and a rule over the fund's own closes that times the
             purchase and can end the position; a shorter hold and a tighter stop;
* intraday -- a plan set once before the open and carried out by local rules. Not built, and
             what stands in for it is four checks that can be run today.

No pass of any mode asks a model anything. History is the tests' own (tests/auto_market.py).
"""

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import agentic_trading
from agentic_trading import risk_scenarios
from agentic_trading.auto import backtest as bt
from agentic_trading.auto import bars as bar_source
from agentic_trading.auto import cli, costs, intraday, modes, plan, positions, quotes, store, trader
from agentic_trading.auto.plan import Envelope, Strategy
from tests import auto_market
from tests.auto_market import scripted, scripted_history, write_research
from tests.test_api import sandbox_service  # noqa: F401 - shared isolated fixture
from tests.test_auto_plan import account

MICRO = "micro_live"
NY = quotes.NEW_YORK


# ---------------------------------------------------------------- the swing plan
def test_a_swing_plan_is_a_different_strategy_and_says_what_its_research_costs(profiles):
    steady = plan.build(profiles, account(profiles, cash=1000.0), Envelope(modes.STEADY, 1000.0))
    swing = plan.build(profiles, account(profiles, cash=1000.0), Envelope(modes.SWING, 1000.0))
    a, b = steady["strategy"], swing["strategy"]
    assert (a["template"], a["trend_window"], a["max_holding_days"], a["stop_loss_pct"]) == (
        "research_etf", 0, 90, 10.0)
    assert (b["template"], b["trend_window"], b["max_holding_days"], b["stop_loss_pct"]) == (
        "trend_etf", 20, 20, 6.0)
    assert b["max_positions"] == 2 and "20 日均线" in b["rationale"]["zh"]
    assert swing["bounds"]["trend_window"] == [10, 60] and steady["bounds"]["trend_window"] == [0, 0]
    assert swing["bounds"]["max_holding_days"] == [2, 30]
    # The research a swing plan runs on is asked for every session, and that is priced.
    daily = next(c for c in swing["changes"] if c["path"].endswith("candidates.period"))
    assert (daily["from"], daily["to"], daily["kind"]) == ("weekly", "daily", plan.SCHEDULE)
    assert "每周约 15 次" in daily["why"]["zh"] and "Claude 订阅额度" in daily["why"]["zh"]
    assert not any(c["path"].endswith("candidates.period") for c in steady["changes"])
    assert steady["plan_id"] != swing["plan_id"]


@pytest.mark.parametrize("mode, asked, used", [
    (modes.SWING, 5, 10), (modes.SWING, 200, 60), (modes.SWING, 30, 30),
    (modes.STEADY, 20, 0),                    # a steady plan has no trend rule to give a window to
])
def test_the_trend_window_is_held_to_the_modes_own_range(profiles, mode, asked, used):
    template = "trend_etf" if mode == modes.SWING else "research_etf"
    doc = plan.build(profiles, account(profiles, cash=1000.0), Envelope(mode, 1000.0),
                     suggestion=Strategy(template=template, trend_window=asked, universe=["VTI"]),
                     suggested_by="model")
    assert doc["strategy"]["trend_window"] == used
    assert ("trend_window" in {note["field"] for note in doc["clamps"]}) is (asked != used)


def test_a_different_trend_window_is_a_different_strategy_version(profiles):
    code = store.code_identity(profiles)
    base = {"template": "trend_etf", "max_positions": 2, "position_pct": 50.0, "min_confidence": 80,
            "stop_loss_pct": 6.0, "take_profit_pct": 0.0, "max_holding_days": 20, "universe": ["VTI"]}
    assert store.version_id("swing", {**base, "trend_window": 20}, code) != store.version_id(
        "swing", {**base, "trend_window": 30}, code)


# ---------------------------------------------------------------- when a trend ends a position
def _held(**over):
    base = {"symbol": "VTI", "bucket": MICRO, "quantity": 1.0, "version_id": "sv-x",
            "opened_at": "2026-10-01T14:00:00+00:00", "cost_basis": 100.0}
    return positions.Managed(**{**base, **over})


SWING_RULES = {"strategy": {"stop_loss_pct": 6.0, "take_profit_pct": 0.0, "max_holding_days": 20,
                            "trend_window": 5}}
TODAY = datetime(2026, 10, 6, tzinfo=UTC).date()


def test_a_close_below_its_own_average_ends_a_swing_position():
    falling = [104.0, 103.0, 102.0, 101.0, 99.0]                       # average 101.8, last 99
    call = positions.exit_call(_held(), SWING_RULES, price=99.0, today=TODAY, closes=falling)
    assert call.due and call.reason == positions.TREND_BREAK
    assert call.facts == {"close": "99.00", "average": "101.80", "window": 5}
    assert call.text()["zh"] == "收盘价 99.00 跌破自己的 5 日均线 101.80。"
    rising = [96.0, 97.0, 98.0, 99.0, 101.0]
    assert not positions.exit_call(_held(), SWING_RULES, price=101.0, today=TODAY, closes=rising).due
    # A stop is a stop first: the reason given is the one that would have fired anyway.
    crashed = positions.exit_call(_held(), SWING_RULES, price=90.0, today=TODAY, closes=[*falling[:4], 90.0])
    assert crashed.reason == positions.STOP_LOSS


@pytest.mark.parametrize("closes", [None, [], [100.0, 99.0, 98.0]])
def test_a_trend_that_cannot_be_computed_is_not_a_reason_to_sell(closes):
    call = positions.exit_call(_held(), SWING_RULES, price=99.0, today=TODAY, closes=closes)
    assert not call.due and call.undecided is None
    assert positions.trend(closes, 5) is None


def test_the_trend_is_read_even_when_a_gain_or_loss_cannot_be(profiles):
    falling = [104.0, 103.0, 102.0, 101.0, 99.0]
    call = positions.exit_call(_held(cost_basis=None), SWING_RULES, price=None, today=TODAY, closes=falling)
    assert call.due and call.reason == positions.TREND_BREAK
    # A version with no trend rule never reads one, whatever closes it is handed.
    steady = {"strategy": {"stop_loss_pct": 10.0, "max_holding_days": 90, "trend_window": 0}}
    assert not positions.exit_call(_held(), steady, price=99.0, today=TODAY, closes=falling).due


# ---------------------------------------------------------------- swing, in the simulated account
@pytest.fixture
def swing(sandbox_service, monkeypatch):  # noqa: F811
    """A swing plan started in the simulated account, on an invented history."""
    prices = scripted(monkeypatch, dict(risk_scenarios.PRICES))
    scripted_history(monkeypatch)
    preview = sandbox_service.auto_plan(mode="swing")
    sandbox_service.auto_start(plan_id=preview["plan_id"], mode="swing", identity="owner")
    return sandbox_service, prices, sandbox_service.profiles().project_dir / "runs"


def test_swing_buys_what_research_calls_for_only_while_it_is_above_its_average(swing):
    service, _prices, runs = swing
    sim = service.auto_simulation()
    assert sim["plan"]["envelope"]["mode"] == "swing" and sim["plan"]["strategy"]["trend_window"] == 20
    # The invented VTI rises a little every session; VXUS falls a little every session.
    up = write_research(runs, "VTI", confidence=88, serial=1)
    down = write_research(runs, "VXUS", confidence=88, serial=2)
    outcome = {row["ticker"]: row for row in service.auto_tick()["signals"]}
    assert outcome["VTI"]["diagnosis"]["code"] == "proposal_automatic"
    waiting = outcome["VXUS"]["diagnosis"]
    assert waiting["code"] == "trend_not_confirmed" and waiting["category"] == "strategy_no_trade"
    assert "20 日均线" in waiting["summary"]["zh"] and "趋势未确认" in waiting["summary"]["zh"]
    seen = trader.seen_signals(service._auto_paper().state_db())
    assert seen[up]["final"] is True and seen[down]["final"] is False, "not above today is not never"
    held = service.auto_simulation()["positions"]
    assert [row["symbol"] for row in held] == ["VTI"]


def test_swing_leaves_when_the_close_falls_below_its_average(swing, monkeypatch):
    service, _prices, runs = swing
    write_research(runs, "VTI", confidence=88)
    service.auto_tick()
    quiet = service.auto_tick()
    assert quiet["exits"][0]["exit"]["due"] is False
    # The trend turns: the same fund now falls a little every session.
    monkeypatch.setitem(auto_market.DRIFT, "VTI", (200.0, -0.0010))
    turned = service.auto_tick()
    assert turned["exits"][0]["exit"]["reason"] == positions.TREND_BREAK
    assert "跌破自己的 20 日均线" in turned["exits"][0]["exit"]["text"]["zh"]
    sim = service.auto_simulation()
    assert sim["positions"] == [] and sim["trades"][0]["closed_by"] == "code"
    sold = next(p for p in service._auto_paper()._store(service._auto_paper().profiles()).all()
                if p.action == "SELL")
    assert sold.history[1]["event"] == "auto_exit" and sold.history[1]["reason"] == positions.TREND_BREAK


def test_swing_does_not_buy_on_a_trend_it_cannot_compute(swing, monkeypatch):
    service, _prices, runs = swing
    scripted_history(monkeypatch, missing=("VTI",))
    run_id = write_research(runs, "VTI", confidence=88)
    outcome = service.auto_tick()["signals"][0]
    assert outcome["diagnosis"]["code"] == "trend_unknown"
    assert "读不到足够的日线" in outcome["diagnosis"]["summary"]["zh"]
    assert trader.seen_signals(service._auto_paper().state_db())[run_id]["final"] is False
    assert service.auto_simulation()["positions"] == []


def test_a_steady_plan_never_reads_a_trend(sandbox_service, monkeypatch):  # noqa: F811
    prices = scripted(monkeypatch, dict(risk_scenarios.PRICES))
    monkeypatch.setattr(bar_source, "read", lambda *_a, **_k: pytest.fail(
        "a strategy with no trend rule read a fund's history"))
    preview = sandbox_service.auto_plan()
    sandbox_service.auto_start(plan_id=preview["plan_id"], identity="owner")
    runs = sandbox_service.profiles().project_dir / "runs"
    write_research(runs, "VXUS", confidence=88)                     # falling, and bought anyway
    assert sandbox_service.auto_tick()["signals"][0]["diagnosis"]["code"] == "proposal_automatic"
    prices["VXUS"] = round(prices["VXUS"] * 0.97, 4)
    assert sandbox_service.auto_tick()["exits"][0]["exit"]["due"] is False


def test_a_position_keeps_the_rules_of_the_mode_that_opened_it(sandbox_service, monkeypatch):  # noqa: F811
    """docs/todo.md: a switch of mode leaves old positions with the strategy that opened them."""
    scripted(monkeypatch, dict(risk_scenarios.PRICES))
    scripted_history(monkeypatch)
    steady = sandbox_service.auto_plan()
    first = sandbox_service.auto_start(plan_id=steady["plan_id"], identity="owner")
    runs = sandbox_service.profiles().project_dir / "runs"
    write_research(runs, "VXUS", confidence=88)                     # below its average: steady does not care
    sandbox_service.auto_tick()
    swing_plan = sandbox_service.auto_plan(mode="swing")
    second = sandbox_service.auto_start(plan_id=swing_plan["plan_id"], mode="swing", identity="owner")
    assert second["opening_version"] != first["opening_version"]
    sim = sandbox_service.auto_simulation()
    assert sim["plan"]["managing"] == [first["opening_version"]]
    assert sim["positions"][0]["strategy_version"] == first["opening_version"]
    # VXUS is below its 20-session average, which would end a swing position. This one was
    # opened by the steady version, which has no such rule, and it stays.
    after = sandbox_service.auto_tick()
    assert after["exits"][0]["exit"]["due"] is False
    assert sandbox_service.auto_simulation()["positions"][0]["symbol"] == "VXUS"
    # One mode opens at a time: the status shows the mode now in force.
    status = sandbox_service.auto_status()
    assert status["simulation"]["version"]["mode"] == "swing"
    assert {row["key"]: row["simulated_days"] for row in status["modes"]}["steady"] == 0


# ---------------------------------------------------------------- swing, in a backtest
def _series(closes, first="2026-03-02"):
    from datetime import date as day_type

    out, day = [], day_type.fromisoformat(first)
    for close in closes:
        while day.weekday() >= 5:
            day += timedelta(days=1)
        out.append(bt.Bar(day.isoformat(), float(close), float(close)))
        day += timedelta(days=1)
    return out


def test_the_backtest_holds_research_to_the_same_trend_rule():
    rules = bt.Rules(universe=("VTI",), max_positions=1, position_pct=100.0, min_confidence=80,
                     trend_window=3)
    rising = _series([100, 101, 102, 103, 104, 105, 106, 100, 99, 98])
    days = [bar.date for bar in rising]

    def at(day, hour=12):
        return datetime.fromisoformat(day).replace(hour=hour, tzinfo=NY)

    def run(bars, signals):
        return bt.run(bars={"VTI": bars}, start=days[0], end=days[-1], capital_usd=1000.0, rules=rules,
                      signals=signals, costs=costs.FREE)

    bought = run(rising, [bt.Signal(at(days[3]), "VTI", "BUY", confidence=90)])
    assert [(f["date"], f["side"], f["reason"]) for f in bought.fills] == [
        (days[4], "buy", "research"), (days[8], "sell", "trend_break")]
    assert bought.fills[1]["decided"] == days[7], "decided on the close that broke it, filled the next open"
    # Below its average when research says buy: it waits for the research's two days, then lapses.
    falling = _series([106, 105, 104, 103, 102, 101, 100, 99, 98, 97])
    lapsed = run(falling, [bt.Signal(at(days[3]), "VTI", "BUY", confidence=90)])
    assert lapsed.fills == [] and [(row["date"], row["why"]) for row in lapsed.skipped] == [
        (days[6], "never above its own average while the research was current")]
    # It turns up inside those two days: bought then, on the open after the close that confirmed it.
    turning = _series([106, 105, 104, 103, 102, 108, 109, 110, 111, 112])
    late = run(turning, [bt.Signal(at(days[3]), "VTI", "BUY", confidence=90)])
    assert late.fills[0]["date"] == days[6] and late.fills[0]["side"] == "buy"
    # Research not sure enough is refused for that, at once, and waits for nothing.
    unsure = run(falling, [bt.Signal(at(days[3]), "VTI", "BUY", confidence=60)])
    assert [(row["date"], row["why"]) for row in unsure.skipped] == [
        (days[4], "confidence below the strategy's own bar")]
    # With too little history for the average there is no trend to be above: not known is not
    # yes, and the research lapses waiting for an average that did not exist in time.
    early = run(rising, [bt.Signal(at(days[0], 8), "VTI", "BUY", confidence=90)])
    assert early.fills == [] and [(row["date"], row["why"]) for row in early.skipped] == [
        (days[2], "never above its own average while the research was current")]
    assert rules.trend_window == bt.Rules.of({"universe": ["VTI"], "trend_window": 3}).trend_window


# ---------------------------------------------------------------- intraday: what is missing, measured
def _minutes(now, count, skip=()):
    opened = now.astimezone(NY).replace(hour=9, minute=30, second=0, microsecond=0)
    return [opened + timedelta(minutes=i) for i in range(count) if i not in skip]


NOON = datetime(2026, 10, 6, 15, 30, 30, tzinfo=UTC)          # 11:30:30 in New York: 120 minutes in


def test_a_whole_session_of_minute_bars_passes_and_a_holed_one_does_not():
    whole = intraday.data_integrity(_minutes(NOON, 120), now=NOON)
    assert whole.ok is True and whole.facts["complete_pct"] == 100.0 and whole.facts["longest_gap_minutes"] == 0
    # A ten-minute hole: 91.67% of the minutes, and a gap no rule should be trusted across.
    holed = intraday.data_integrity(_minutes(NOON, 120, skip=range(40, 50)), now=NOON)
    assert holed.ok is False and holed.facts["complete_pct"] == 91.67
    assert holed.facts["longest_gap_minutes"] == 10.0 and "最长缺口 10 分钟" in holed.zh
    # A feed that stopped a quarter of an hour ago.
    stopped = intraday.data_integrity(_minutes(NOON, 105), now=NOON)
    assert stopped.ok is False and stopped.facts["longest_gap_minutes"] == 15.0
    # What could not be read is not known, which is neither a pass nor a fail.
    assert intraday.data_integrity(None, now=NOON).ok is None
    assert intraday.data_integrity([], now=NOON).ok is None
    yesterday = [stamp - timedelta(days=1) for stamp in _minutes(NOON, 120)]
    assert intraday.data_integrity(yesterday, now=NOON).ok is None


def test_the_newest_price_is_held_to_the_real_accounts_own_two_minutes():
    fresh = intraday.timeliness(NOON - timedelta(seconds=90), now=NOON, max_age_seconds=120)
    late = intraday.timeliness(NOON - timedelta(minutes=15), now=NOON, max_age_seconds=120)
    assert fresh.ok is True and late.ok is False and late.facts["age_seconds"] == 900.0
    assert "900 秒" in late.zh and "120 秒" in late.zh
    assert intraday.timeliness(None, now=NOON, max_age_seconds=120).ok is None


def test_the_venue_and_the_accounts_pace_are_what_they_are_on_record_to_be(profiles):
    venue = intraday.venue_capability(profiles)
    assert venue.ok is False and venue.facts["confirmed_for_account"] is False
    for phrase in ("尚未对本账户确认", "结算规则", "日内交易规则", "碎股只能下市价单"):
        assert phrase in venue.zh
    pace = intraday.pace(profiles)
    assert pace.ok is False and pace.facts == {"max_orders_per_week": 4, "needed": 10,
                                               "no_same_day_round_trips": True}
    assert "每周最多下 4 笔单" in pace.zh and "禁止同一天买入又卖出" in pace.zh


def test_together_they_say_the_mode_is_not_ready_and_never_that_it_is_built(sandbox_service, monkeypatch):  # noqa: F811
    monkeypatch.setattr(intraday, "_vendor_stamps", lambda _symbol: _minutes(datetime.now(UTC), 60))
    report = sandbox_service.auto_intraday_checks()
    assert report["built"] is False and report["ready"] is False and report["symbol"] == "SPY"
    assert [check["key"] for check in report["checks"]] == [
        intraday.DATA, intraday.TIMELINESS, intraday.VENUE, intraday.PACE]
    assert "尚未实现" in report["note"]["zh"]
    assert all(set(check["text"]) == {"zh", "en"} for check in report["checks"])
    # A feed that cannot be read at all: the two data checks could not run, and that is not a pass.

    def down(_symbol):
        raise ConnectionError("no route")

    monkeypatch.setattr(intraday, "_vendor_stamps", down)
    unread = sandbox_service.auto_intraday_checks()
    assert [check["ok"] for check in unread["checks"]][:2] == [None, None] and unread["ready"] is False
    # Even with everything measurable in order, it is still not open: the mode's blockers say so.
    row = next(r for r in sandbox_service.auto_status()["modes"] if r["key"] == "intraday")
    assert row["can_simulate"] is False and "intraday_not_built" in {b["code"] for b in row["blockers"]}


def test_the_intraday_checks_over_http_and_from_the_command_line(auth_client, sandbox_service, monkeypatch, capsys):  # noqa: F811
    monkeypatch.setattr("agentic_trading.api.app._service", sandbox_service)
    monkeypatch.setattr(intraday, "_vendor_stamps", lambda _symbol: _minutes(datetime.now(UTC), 60))
    answer = auth_client.get("/auto-trading/intraday-checks")
    assert answer.status_code == 200 and answer.json()["ready"] is False
    assert cli.main(["--config-dir", str(sandbox_service.profiles().config_dir), "intraday-check"]) == 0
    out = capsys.readouterr().out
    assert "【短线模式检查】" in out and "✗ 券商能力尚未核实" in out


# ---------------------------------------------------------------- no model, per trade, in any mode
PACKAGE = Path(agentic_trading.__file__).parent
#: Everything a pass of any mode runs through.
A_PASS = ("auto/trader.py", "auto/runner.py", "auto/positions.py", "auto/venue.py", "auto/paper.py",
          "auto/quotes.py", "auto/store.py", "auto/backtest.py", "auto/comparison.py",
          "auto/metrics.py", "auto/costs.py", "auto/intraday.py", "auto/bars.py", "auto/experiments.py")


def _imports(path):
    found = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom):
            found |= {f"{node.module or ''}.{alias.name}" for alias in node.names}
        elif isinstance(node, ast.Import):
            found |= {alias.name for alias in node.names}
    return found


def test_no_pass_of_any_mode_asks_a_model_anything():
    """docs/todo.md, P2.5: intraday trading must not depend on a full model run per trade.

    Held for every mode, by what the modules import: nothing a pass runs through can reach the
    model adapter or the one module that asks it for a suggestion. A model is asked when a
    person asks for a plan's parameters, and at no other time.
    """
    for name in A_PASS:
        reached = {item for item in _imports(PACKAGE / name)
                   if "llm" in item.split(".") or item.endswith(".advice") or "claude" in item.lower()}
        assert not reached, f"{name} can reach a model: {sorted(reached)}"
    asks = [str(path.relative_to(PACKAGE)) for path in sorted(PACKAGE.rglob("*.py"))
            if any(item.split(".")[-1] == "advice" for item in _imports(path))]
    assert asks == ["service/auto.py"], asks


def test_swing_is_shut_for_real_money_until_the_owner_says_what_it_is(profiles):
    rows = {row["key"]: row for row in modes.status(
        profiles, simulated_days={"steady": 30, "swing": 30}, exits_verified={"steady": True, "swing": True},
        opened=("steady",))}
    held = {b["code"]: b for b in rows["swing"]["blockers"]}
    assert rows["swing"]["open_for_real_money"] is False
    assert {"swing_policy_undecided", "daily_research_not_scheduled"} <= set(held)
    assert "需要你来定" in held["swing_policy_undecided"]["text"]["zh"]
    # Once research is scheduled every session, that one reason is gone and the others stand.
    scheduled = profiles.risk["research_schedule"]["accounts"][MICRO]["candidates"]
    scheduled.update(enabled=True, period="daily")
    again = {b.code for b in modes.blockers(profiles, modes.SWING, simulated_days=30, exits_verified=True)}
    assert "daily_research_not_scheduled" not in again and "swing_policy_undecided" in again
