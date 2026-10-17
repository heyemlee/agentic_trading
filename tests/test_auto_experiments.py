"""A result beside what else the same money would have done, and a record of it that can be checked.

docs/todo.md, P2.2 to P2.4:

* every comparator is given the same money, on the same days, under the same cap and at the
  same costs, and is summarised by the same arithmetic;
* an experiment's record carries the ledgers its figures came from, and is checked against them;
* whether a window could test a strategy at all is computed -- from the day the strategy was
  fixed, and from what else has been tried on that window -- and cannot be asserted;
* the live simulation and the historical backtests are reported apart;
* everything the record did and did not do is counted, and confidence is never a probability.

History is invented by the tests (tests/auto_market.py). Nothing here reads a market.
"""

import json
from datetime import UTC, date, datetime, time, timedelta

import pytest

from agentic_trading import risk_scenarios
from agentic_trading.auto import backtest as bt
from agentic_trading.auto import cli, comparison, costs, experiments, metrics, paper, runner, store
from agentic_trading.service import ServiceError
from agentic_trading.state_db import StateDB
from tests.auto_market import invented_bars, scripted, scripted_history, today, write_research
from tests.test_api import sandbox_service  # noqa: F401 - shared isolated fixture

MICRO = "micro_live"
RULES = bt.Rules(universe=("VTI", "VXUS"), max_positions=2, position_pct=30.0, stop_loss_pct=10.0,
                 max_holding_days=90, min_confidence=80)
START, END = "2025-01-02", "2025-12-31"
WEIGHTS = {"VTI": 60, "BND": 40}


def history(*symbols, start="2024-06-03", end=END):
    return {symbol: invented_bars(symbol, start, end) for symbol in symbols}


def compare(**over):
    args = {"bars": history("VTI", "VXUS", "BND"), "start": START, "end": END, "mode": "steady",
            "rules": RULES, "capital_usd": 1000.0, "opening_cash_usd": 1000.0, "deploy_cap_usd": 600.0,
            "benchmark_weights": WEIGHTS}
    return comparison.run(**{**args, **over})


# ---------------------------------------------------------------- the same conditions for all
def test_every_comparator_is_given_the_same_money_cap_and_costs():
    result = compare(flows=[bt.Flow("2025-06-02", 500.0), bt.Flow("2025-09-02", -200.0)])
    conditions = result["conditions"]
    assert (conditions["opening_cash_usd"], conditions["capital_usd"], conditions["deploy_cap_usd"]) == (
        1000.0, 1000.0, 600.0)
    assert conditions["flows"] == [{"date": "2025-06-02", "amount_usd": 500.0},
                                   {"date": "2025-09-02", "amount_usd": -200.0}]
    assert conditions["costs"]["name"] == "standard" and conditions["simple_rule_average_over_sessions"] == 100
    assert set(result["rows"]) == set(comparison.ROWS)
    # The same money reached every one of them, on the same days.
    for name in comparison.ROWS:
        ledger = result["ledgers"][name]
        assert ledger["equity"][-1]["funded_usd"] == 1300.0, name
        assert result["rows"][name]["figures"]["funded_usd"] == 1300.0
        assert set(result["rows"][name]["says"]) == {"zh", "en"}
    # The two shaped like the plan never deploy more than the plan could; the benchmark is the
    # reference from outside that budget, and the table says so rather than leaving it to be noticed.
    for name in (comparison.PASSIVE, comparison.SIMPLE_RULE):
        assert max(point["invested_usd"] for point in result["ledgers"][name]["equity"]) <= 600.0 * 1.6
        assert all(fill["quantity"] * fill["price"] <= 300.01
                   for fill in result["ledgers"][name]["fills"] if fill["side"] == "buy")
    assert result["rows"][comparison.BENCHMARK]["figures"]["average_exposure_pct"] > 95.0
    assert "不受方案的上限约束" in conditions["note"]["zh"]


def test_every_row_is_the_same_arithmetic_over_its_own_ledger():
    result = compare()
    for name, row in result["rows"].items():
        ledger = result["ledgers"][name]
        again = metrics.summarize(ledger["equity"], fills=ledger["fills"], trades=ledger["trades"],
                                  skipped=ledger["skipped"], opening_usd=1000.0)
        assert again == row["figures"], name
    cash = result["rows"][comparison.CASH]["figures"]
    assert (cash["net_return_pct"], cash["max_drawdown_pct"], cash["fills"]) == (0.0, 0.0, 0)
    # On this invented history one fund rises steadily and one falls: holding both passively
    # and holding only what is above its average are different results, and neither is the
    # benchmark's. A table whose rows could not differ would not be a comparison.
    figures = {name: row["figures"]["net_return_pct"] for name, row in result["rows"].items()}
    assert len({round(value, 2) for value in figures.values()}) == 4, figures
    assert result["rows"][comparison.PASSIVE]["figures"]["trades_closed"] == 0
    assert result["rows"][comparison.SIMPLE_RULE]["figures"]["costs_usd"] > 0


def test_a_comparator_that_cannot_be_run_is_a_reason_and_not_a_row_of_zeros():
    result = compare(bars=history("VTI", "VXUS"))                      # no history for BND
    row = result["rows"][comparison.BENCHMARK]
    assert row["figures"] is None and "BND" in row["why_not"]
    assert comparison.BENCHMARK not in result["ledgers"]
    strategy = comparison.strategy_row([{"date": START, "equity_usd": 1010.0, "funded_usd": 1000.0}],
                                       opening_usd=1000.0, says={"zh": "策略", "en": "Strategy"})
    apart = comparison.differences(strategy, result["rows"])
    assert apart[comparison.BENCHMARK] == {"net_return_points": None, "max_drawdown_points": None}
    assert apart[comparison.CASH]["net_return_points"] == 1.0


def test_the_simple_rules_one_number_is_fixed_by_the_mode_and_not_tuned():
    assert comparison.AVERAGE_OVER == {"steady": 100, "swing": 20, "intraday": 20}
    swing = compare(mode="swing")
    assert swing["conditions"]["simple_rule_average_over_sessions"] == 20
    assert "20 日均线" in swing["rows"][comparison.SIMPLE_RULE]["says"]["zh"]


# ---------------------------------------------------------------- whether it is a test at all
@pytest.mark.parametrize("designed_on, sample, says", [
    ("2024-12-31", experiments.OUT_OF_SAMPLE, "没有参与设计"),
    ("2025-12-31", experiments.IN_SAMPLE, "不是独立验证"),
    ("2026-05-01", experiments.IN_SAMPLE, "不是独立验证"),
    ("2025-06-30", experiments.MIXED, "不能算独立验证"),
    (None, experiments.UNKNOWN, "无法判断"),
])
def test_a_window_is_a_test_only_if_it_came_after_the_strategy_was_fixed(designed_on, sample, says):
    found = experiments.sample_of(start=START, end=END, designed_on=designed_on)
    assert found["sample"] == sample and says in found["says"]["zh"]


@pytest.fixture
def db(tmp_path):
    return StateDB(tmp_path / "state" / "proposals.json")


STRATEGY = {"template": "research_etf", "max_positions": 2, "position_pct": 30.0, "min_confidence": 80,
            "stop_loss_pct": 10.0, "take_profit_pct": 0.0, "max_holding_days": 90,
            "universe": ["VTI", "VXUS"], "suggested_by": "template"}


def _version(db, profiles, designed_on="2024-12-31", parent=None, **strategy):
    return store.record_version(db, profiles, mode="steady", strategy={**STRATEGY, **strategy},
                                plan_id="plan-1", data_as_of=designed_on, created_by="template",
                                parent=parent)


def _card(db, version, compared=None, **over):
    compared = compared or compare()
    args = {"kind": experiments.BACKTEST, "version": version, "start": START, "end": END,
            "data": {"digest": "abc123", "caliber": "adjusted"}, "conditions": compared["conditions"],
            "rows": compared["rows"]}
    return experiments.build(db, **{**args, **over}), compared


def test_independent_is_computed_from_the_record_and_cannot_be_claimed(db, profiles):
    first = _version(db, profiles)
    card, _compared = _card(db, first)
    assert card["design"]["sample"] == experiments.OUT_OF_SAMPLE and card["design"]["independent"] is True
    assert card["lineage"] == first["version_id"] and card["design"]["earlier_trials_on_window"] == []
    experiments.register(db, card)
    experiments.register(db, card)
    assert len(experiments.registered(db)) == 1, "running it again is not trying something else"
    assert _card(db, first)[0]["design"]["independent"] is True

    # A second variant of the same strategy on the same window: the window has now been used to
    # compare two of them, so its result for either is a choice and not a test.
    second = _version(db, profiles, parent=first["version_id"], stop_loss_pct=7.0)
    other, _ = _card(db, second)
    assert other["lineage"] == first["version_id"]
    assert other["design"]["earlier_trials_on_window"] == [first["version_id"]]
    assert other["design"]["sample"] == experiments.OUT_OF_SAMPLE and other["design"]["independent"] is False
    assert "不能当作独立验证" in other["design"]["selection"]["zh"]
    # A window the first was never run on is still clean for the second ...
    later, _ = _card(db, second, start="2026-01-02", end="2026-06-30")
    assert later["design"]["independent"] is True
    # ... and an unrelated strategy is not a variant of this one.
    unrelated = _version(db, profiles, universe=["BND"])
    assert _card(db, unrelated)[0]["design"]["independent"] is True
    # Data from before the strategy was fixed is never a test, however clean the window.
    built_on_it = _version(db, profiles, designed_on="2026-01-01", max_holding_days=30)
    assert _card(db, built_on_it)[0]["design"]["independent"] is False


def test_the_card_says_what_was_run_and_what_it_cannot_say(db, profiles):
    version = _version(db, profiles)
    card, compared = _card(db, version)
    assert card["card_id"].startswith("xp-") and card["card_id"] == _card(db, version, compared)[0]["card_id"]
    assert card["strategy"]["stop_loss_pct"] == 10.0 and card["mode"] == "steady"
    assert set(card["code"]) >= {"logic", "mandate", "deep_model", "quick_model", "upstream_commit"}
    assert card["model"]["training_cutoff"] is None and "看过答案" in card["model"]["caveat"]["zh"]
    assert len(card["limits"]) == 4 and all(set(limit) == {"zh", "en"} for limit in card["limits"])
    said = " ".join(limit["zh"] for limit in card["limits"])
    for phrase in ("没有逐条重放硬风控", "复权日线", "下一个开盘价", "假设值"):
        assert phrase in said
    # Anything that changes what was run is a different experiment.
    assert _card(db, version, compared, end="2025-11-28")[0]["card_id"] != card["card_id"]
    assert _card(db, version, compared, data={"digest": "other"})[0]["card_id"] != card["card_id"]


def test_a_card_is_checked_against_the_ledgers_it_was_written_with(db, profiles, tmp_path):
    card, compared = _card(db, _version(db, profiles))
    directory = experiments.write(tmp_path / "experiments", card, compared["ledgers"])
    assert directory == tmp_path / "experiments" / card["card_id"]
    assert set(card["artifacts"]) == set(comparison.ROWS)
    assert experiments.verify(directory) == []
    summary = experiments.summary(card, directory)
    assert summary["figures"]["passive"]["net_return_pct"] == card["rows"]["passive"]["figures"]["net_return_pct"]

    # A figure edited on the card: the ledger does not produce it.
    path = directory / "card.json"
    edited = json.loads(path.read_text())
    real = edited["rows"]["passive"]["figures"]["net_return_pct"]
    edited["rows"]["passive"]["figures"]["net_return_pct"] = real + 25.0
    path.write_text(json.dumps(edited))
    assert experiments.verify(directory) == [
        f"passive.net_return_pct: the card says {real + 25.0!r}, the ledger gives {real!r}"]
    # A ledger edited to match: it is no longer the file the card recorded.
    edited["rows"]["passive"]["figures"]["net_return_pct"] = real
    path.write_text(json.dumps(edited))
    ledger = directory / "passive.json"
    ledger.write_text(ledger.read_text().replace('"equity_usd":1', '"equity_usd":2', 1))
    assert experiments.verify(directory) == ["passive: the ledger is not the file the card recorded"]
    ledger.unlink()
    assert experiments.verify(directory) == ["passive: the ledger file is missing"]
    (directory / "card.json").unlink()
    assert experiments.verify(directory) == ["the card cannot be read: FileNotFoundError"]


# ---------------------------------------------------------------- through the service
@pytest.fixture
def desk(sandbox_service, monkeypatch):  # noqa: F811
    """A started simulation, a scripted market and an invented history."""
    prices = scripted(monkeypatch, dict(risk_scenarios.PRICES))
    scripted_history(monkeypatch)
    preview = sandbox_service.auto_plan()
    sandbox_service.auto_start(plan_id=preview["plan_id"], identity="owner")
    return sandbox_service, prices, sandbox_service.profiles().project_dir / "runs"


def test_a_backtest_is_kept_with_its_ledgers_and_says_it_is_not_a_test(desk):
    service, _prices, _runs = desk
    answer = service.auto_backtest(start="2025-01-02", end="2025-12-31", identity="owner")
    card = answer["card"]
    assert answer["grounded"] is True and answer["problems"] == []
    root = paper.root_of(service.profiles()) / "experiments" / card["card_id"]
    assert answer["path"] == str(root) and (root / "card.json").is_file()
    assert {path.name for path in root.glob("*.json")} == {
        "card.json", "passive.json", "simple_rule.json", "benchmark.json", "cash.json"}
    # The strategy was fixed today; everything before today is data it could have been built on.
    assert card["design"]["sample"] == experiments.IN_SAMPLE and card["design"]["independent"] is False
    assert "不是独立验证" in card["design"]["says"]["zh"]
    assert card["data"]["caliber"] == "adjusted" and card["data"]["digest"]
    assert card["conditions"]["opening_cash_usd"] == 100.0 and card["conditions"]["deploy_cap_usd"] == 60.0
    for name in comparison.ROWS:
        assert card["rows"][name]["figures"]["sessions"] > 240, name
    # No research was recorded live in 2025, and none is made up for it.
    strategy = card["rows"]["strategy"]
    assert strategy["figures"] is None and "不会为过去的日期重新生成" in strategy["why_not"]["zh"]
    assert card["conditions"]["replayed_conclusions"] == 0 and "strategy" not in card["artifacts"]

    # It is attached to the version it is about, once, and nowhere in the real account's state.
    sim = service.auto_simulation()
    assert sim["version"]["evidence"] == 1
    assert service.auto_backtest(start="2025-01-02", end="2025-12-31")["card"]["card_id"] == card["card_id"]
    assert service.auto_simulation()["version"]["evidence"] == 1
    assert len(experiments.registered(service._auto_paper().state_db())) == 1
    assert not (service.profiles().project_dir / "state" / "experiments").exists()
    assert experiments.registered(service.state_db()) == []


def test_research_recorded_live_inside_the_window_is_replayed_as_it_was(desk, monkeypatch):
    service, _prices, runs = desk
    last = scripted_history(monkeypatch, last=(date.fromisoformat(today()) + timedelta(days=120)).isoformat())
    # Concluded at 15:00 in New York in the session before today's, while the market was open, so
    # the replay may act on it at once; and the window opens a week before that, so it begins
    # before the day the strategy is fixed on (the day the account is valued at, which is that
    # same session). Both once came from the calendar -- research written half an hour ago, a
    # window opened yesterday -- and both broke over a weekend. From Friday's close to Saturday
    # morning in New York the research was over two days old by Monday's open and lapsed under
    # its lifetime, so nothing was bought (the pre-push gate met it at 21:16 PDT on 2026-10-16);
    # and on a Sunday or a Monday, yesterday came after the Friday the strategy was fixed on, so
    # the window read as out of sample.
    session = date.fromisoformat(today()) - timedelta(days=1)
    while session.weekday() >= 5:
        session -= timedelta(days=1)
    concluded = datetime.combine(session, time(15), tzinfo=bt.NEW_YORK)
    age = (datetime.now(UTC) - concluded).total_seconds() / 60
    write_research(runs, "VTI", confidence=88, serial=1, age_minutes=age)
    write_research(runs, "VXUS", confidence=75, serial=2, age_minutes=age)  # a Buy, under the plan's bar of 80
    write_research(runs, "BND", rating="Hold", confidence=70, serial=3,
                   age_minutes=age)                                         # no conclusion to act on
    opened = (session - timedelta(days=7)).isoformat()
    answer = service.auto_backtest(start=opened, end=last)
    card = answer["card"]
    assert answer["grounded"] is True and "strategy" in card["artifacts"]
    assert card["conditions"]["recorded_conclusions"] == 3 and card["conditions"]["replayed_conclusions"] == 2
    ledger = json.loads((paper.root_of(service.profiles()) / "experiments" / card["card_id"]
                         / "strategy.json").read_text())
    # Bought at the first open after the research existed, never at a price from before it.
    fill = ledger["fills"][0]
    assert fill["symbol"] == "VTI" and fill["reason"].startswith("research:")
    assert bt.opens_at(fill["date"]) > datetime.fromisoformat(fill["decided"])
    assert [(row["symbol"], row["why"]) for row in ledger["skipped"]] == [
        ("VXUS", "confidence below the strategy's own bar")]
    assert card["rows"]["strategy"]["figures"]["fills"] >= 1
    assert set(card["differences"]) == set(comparison.ROWS)
    # The window begins before the strategy was fixed and runs on after it: part of it is data
    # the strategy could have been built on, so as a whole it is not a test.
    assert card["design"]["sample"] == experiments.MIXED and card["design"]["independent"] is False


def test_a_window_already_used_to_compare_variants_is_not_a_test_for_the_one_chosen(desk, monkeypatch):
    service, _prices, _runs = desk
    tomorrow = (date.fromisoformat(today()) + timedelta(days=1)).isoformat()
    last = scripted_history(monkeypatch, last=(date.fromisoformat(today()) + timedelta(days=200)).isoformat())
    first = service.auto_backtest(start=tomorrow, end=last)["card"]
    assert first["design"]["sample"] == experiments.OUT_OF_SAMPLE and first["design"]["independent"] is True
    service.auto_adjust(strategy={"stop_loss_pct": 8.0})
    second = service.auto_backtest(start=tomorrow, end=last)["card"]
    assert second["version_id"] != first["version_id"] and second["lineage"] == first["lineage"]
    assert second["design"]["earlier_trials_on_window"] == [first["version_id"]]
    assert second["design"]["independent"] is False and "selection" in second["design"]
    # Each version keeps its own evidence; the new one did not inherit the first one's card.
    db = service._auto_paper().state_db()
    assert [row["card_id"] for row in store.version(db, first["version_id"])["evidence"]] == [first["card_id"]]
    assert [row["card_id"] for row in store.version(db, second["version_id"])["evidence"]] == [second["card_id"]]


def test_a_backtest_that_cannot_be_run_says_why_and_leaves_nothing(desk, monkeypatch):
    service, _prices, _runs = desk
    with pytest.raises(ServiceError, match="ends after it begins"):
        service.auto_backtest(start="2025-06-01", end="2025-06-01")
    scripted_history(monkeypatch, missing=("BND",))
    with pytest.raises(ServiceError, match="history for this backtest could not be read: BND"):
        service.auto_backtest(start="2025-01-02", end="2025-12-31")
    assert not (paper.root_of(service.profiles()) / "experiments").exists()
    service.auto_stop(reason="done")
    with pytest.raises(ServiceError, match="no plan is in force"):
        service.auto_backtest()


def test_the_simulation_is_reported_beside_its_comparators_and_apart_from_backtests(desk, monkeypatch):
    service, prices, runs = desk
    empty = service.auto_performance()
    assert empty["exists"] is True and empty["simulation"]["rows"] == {} and empty["backtests"] == []
    assert empty["simulation"]["window"] == {"start": None, "end": None, "sessions": 0}

    sim = service._auto_paper()
    # Four sessions of passes, and a history in which all four have finished. Four weekdays from
    # the session the account was opened in: counted in calendar days, as they once were, a
    # Friday's next two fell on the weekend, which belongs to the Friday, and the passes after the
    # deposit overwrote the mark taken before it -- so on every Friday, Saturday and Sunday in New
    # York the deposit read as a result (the pre-push gate met it at 21:30 PDT on 2026-10-15).
    scripted_history(monkeypatch, last=(date.fromisoformat(today()) + timedelta(days=10)).isoformat())
    sessions, day = [], date.fromisoformat(runner.session_of(datetime.now(UTC)))
    while len(sessions) < 4:
        if day.weekday() < 5:
            sessions.append(datetime.combine(day, datetime.min.time(), tzinfo=UTC).replace(hour=15))
        day += timedelta(days=1)
    write_research(runs, "VTI", confidence=88, serial=1)
    write_research(runs, "VXUS", rating="Hold", confidence=60, serial=2)
    runner.run(sim, research_from=runs, now=sessions[0])
    service.auto_fund(amount_usd=50.0, kind="deposit")
    prices["VTI"] = round(prices["VTI"] * 1.05, 4)
    for when in sessions[1:]:
        runner.run(sim, research_from=runs, now=when)
    service.auto_backtest(start="2025-01-02", end="2025-12-31")

    report = service.auto_performance()
    simulation = report["simulation"]
    assert simulation["comparators_unavailable"] is None
    assert simulation["window"]["sessions"] >= 2 and set(simulation["rows"]) == {
        "strategy", *comparison.ROWS}
    mine = simulation["rows"]["strategy"]["figures"]
    # $33 of the $100 in a fund that then rose 5%: about 1.65% on the account, and the $50 put
    # in afterwards is in the account and not in the return.
    assert mine["net_return_pct"] == pytest.approx(1.65, abs=0.05)
    assert mine["funded_usd"] == 150.0 and mine["equity_usd"] == pytest.approx(151.65, abs=0.05)
    assert mine["fills"] == 1 and mine["result_usd"] == pytest.approx(1.65, abs=0.05)
    # Every comparator was given the same money on the same days.
    assert simulation["conditions"]["opening_cash_usd"] == 100.0
    assert [flow["amount_usd"] for flow in simulation["conditions"]["flows"]] == [50.0]
    for name in comparison.ROWS:
        assert simulation["rows"][name]["figures"]["funded_usd"] == 150.0, name
    assert set(simulation["differences"]) == set(comparison.ROWS)
    assert simulation["differences"]["cash"]["net_return_points"] == mine["net_return_pct"]
    # Everything the record did, and did not do.
    assert simulation["research"]["research_runs"] == 2
    assert simulation["conclusions_by_outcome"] == {"proposal_created": 1, "strategy_no_trade": 1}
    assert "不是盈利概率" in simulation["confidence"]["note"]["zh"]
    assert "不等于盈利能力已被证明" in simulation["caveat"]["zh"]
    # The history is listed apart, with its own standing, and the two are said to be different.
    assert [card["sample"] for card in report["backtests"]] == [experiments.IN_SAMPLE]
    assert report["backtests"][0]["independent"] is False
    assert "不相加" in report["note"]["zh"] and "不能直接比较" in report["note"]["zh"]


def test_when_the_history_cannot_be_read_the_accounts_own_figures_still_stand(desk, monkeypatch):
    service, _prices, runs = desk
    write_research(runs, "VTI", confidence=88)
    service.auto_tick()
    scripted_history(monkeypatch, missing=("VTI",))
    report = service.auto_performance()["simulation"]
    assert set(report["rows"]) == {"strategy"} and report["rows"]["strategy"]["figures"]["fills"] == 1
    assert "VTI" in report["comparators_unavailable"] and report["differences"] == {}


def test_performance_and_backtests_over_http_and_from_the_command_line(auth_client, desk, monkeypatch, capsys):
    service, _prices, runs = desk
    monkeypatch.setattr("agentic_trading.api.app._service", service)
    write_research(runs, "VTI", confidence=88)
    service.auto_tick()
    made = auth_client.post("/auto-trading/backtest", json={"start": "2025-01-02", "end": "2025-12-31"})
    assert made.status_code == 200, made.text
    assert made.json()["grounded"] is True and made.json()["card"]["design"]["independent"] is False
    assert auth_client.post("/auto-trading/backtest", json={"start": "last year"}).status_code == 422
    assert auth_client.post("/auto-trading/backtest", json={"window": 5}).status_code == 422
    shown = auth_client.get("/auto-trading/performance")
    assert shown.status_code == 200 and len(shown.json()["backtests"]) == 1
    assert "strategy" in shown.json()["simulation"]["rows"]

    config = str(service.profiles().config_dir)
    assert cli.main(["--config-dir", config, "performance"]) == 0
    out = capsys.readouterr().out
    assert "【模拟表现】" in out and "— 实时模拟" in out and "— 历史回测 —" in out
    assert "被动持有" in out and "60/40 基准" in out and "不是独立验证" in out
    assert "不是盈利概率" in out
    assert cli.main(["--config-dir", config, "backtest", "--start", "2025-01-02", "--end", "2025-12-31"]) == 0
    out = capsys.readouterr().out
    assert "【回测记录】" in out and "独立验证：否" in out and "记录与原始账目核对：一致" in out
    assert "没有逐条重放硬风控" in out and "看过答案" in out
    assert cli.main(["--config-dir", config, "--lang", "en", "performance"]) == 0
    assert "[Simulated performance]" in capsys.readouterr().out
    assert costs.STANDARD.name == "standard"
