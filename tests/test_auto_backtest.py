"""A backtest that cannot see the future, does not trade where there was no market, and pays to trade.

docs/todo.md, P2.3. Each of the ways a backtest flatters a strategy is a test here:

* a conclusion is never filled at a price from before it existed;
* nothing dated on or before a day depends on any bar after it;
* a symbol with no bar does not trade, and a holding with none is valued stale and says so;
* every fill is on the worse side of the price;
* money put in is not counted as money made.

All of it on bars the tests write themselves. Nothing here reads a market.
"""

from datetime import date, datetime, timedelta

import pytest

from agentic_trading.auto import backtest as bt
from agentic_trading.auto import costs, metrics

NY = bt.NEW_YORK
MONDAY = date(2026, 3, 2)


def days(count, first=MONDAY):
    """``count`` consecutive weekdays, as ISO dates."""
    out, day = [], first
    while len(out) < count:
        if day.weekday() < 5:
            out.append(day.isoformat())
        day += timedelta(days=1)
    return out


def series(prices, first=MONDAY, skip=()):
    """Bars from (open, close) pairs -- or single numbers, for a flat day -- on weekdays."""
    out = []
    for day, price in zip(days(len(prices), first), prices, strict=True):
        if day in skip:
            continue
        opened, closed = price if isinstance(price, tuple) else (price, price)
        out.append(bt.Bar(day, float(opened), float(closed)))
    return out


def at(day, hour, minute=0):
    return datetime.combine(date.fromisoformat(day), datetime.min.time(), tzinfo=NY).replace(
        hour=hour, minute=minute)


RULES = bt.Rules(universe=("VTI", "VXUS"), max_positions=2, position_pct=50.0, min_confidence=80,
                 stop_loss_pct=10.0, take_profit_pct=0.0, max_holding_days=0)
D = days(12)


def run(bars, **over):
    args = {"bars": bars, "start": D[0], "end": D[-1], "capital_usd": 1000.0, "rules": RULES,
            "costs": costs.FREE}
    return bt.run(**{**args, **over})


# ---------------------------------------------------------------- never before it could have known
@pytest.mark.parametrize("when, filled_on", [
    (at(D[2], 9, 0), D[2]),        # before that day's open: that open is the first price after it
    (at(D[2], 9, 30), D[3]),       # at the open is not before it
    (at(D[2], 10, 0), D[3]),       # during the session: the next open
    (at(D[2], 16, 30), D[3]),      # after the close: the next open
    (at(D[4], 17, 0), D[5]),       # Friday evening: Monday's open
])
def test_a_conclusion_fills_at_the_first_open_after_it_existed(when, filled_on):
    bars = {"VTI": series([(100 + i, 100.5 + i) for i in range(12)])}
    result = run(bars, signals=[bt.Signal(when, "VTI", "BUY", confidence=90, source="run-1")])
    fill = result.fills[0]
    assert fill["date"] == filled_on and fill["decided"] == when.isoformat()
    assert bt.opens_at(fill["date"]) > when, "the price is from after the conclusion"
    opened = next(bar.open for bar in bars["VTI"] if bar.date == filled_on)
    assert fill["price"] == opened and fill["reason"] == "research:run-1"


def test_nothing_on_or_before_a_day_depends_on_any_bar_after_it():
    """The property that matters. A rule, stops and research together, and the future rewritten."""
    wave = [100, 101, 103, 102, 104, 107, 105, 96, 94, 97, 101, 104, 108, 107, 111, 109, 99, 95, 98,
            103, 106, 110, 113, 111, 115, 118, 116, 104, 101, 106]
    full = {"VTI": series([(p - 0.4, p) for p in wave]),
            "VXUS": series([(p / 2 - 0.2, p / 2) for p in reversed(wave)])}
    stamps = days(len(wave))
    rules = bt.Rules(universe=("VTI", "VXUS"), max_positions=2, position_pct=45.0, stop_loss_pct=6.0,
                     take_profit_pct=9.0, max_holding_days=12, min_confidence=70)
    signals = [bt.Signal(at(stamps[i], 11), "VXUS", "BUY", confidence=85) for i in (2, 14)]
    args = {"start": stamps[5], "end": stamps[-1], "capital_usd": 1000.0, "rules": rules,
            "signals": signals, "decide": bt.trend_filter(rules, window=5), "costs": costs.STANDARD}
    whole = bt.run(bars=full, **args)
    assert whole.fills and whole.trades, "the run has to do something for this to mean anything"
    for cut in (stamps[9], stamps[15], stamps[22]):
        rewritten = {symbol: [bar if bar.date <= cut else bt.Bar(bar.date, bar.open * 3, bar.close / 2)
                              for bar in rows] for symbol, rows in full.items()}
        other = bt.run(bars=rewritten, **args)
        for part in ("fills", "trades", "equity", "skipped"):
            before = [row for row in getattr(whole, part) if row.get("date", row.get("closed_on")) <= cut]
            after = [row for row in getattr(other, part) if row.get("date", row.get("closed_on")) <= cut]
            assert before == after, f"{part} up to {cut} changed when later bars did"
        # And it is not that nothing changed: what comes after really is different.
        assert whole.equity[-1] != other.equity[-1]


def test_a_rule_is_shown_the_day_it_decides_on_and_nothing_later():
    bars = {"VTI": series(list(range(100, 112)))}
    seen = []

    def spy(view):
        seen.append((view.date, len(view.closes("VTI")), view.bars("VTI")[-1].date))
        return []

    run(bars, decide=spy)
    assert seen == [(day, index + 1, day) for index, day in enumerate(D)]


def test_bars_from_before_the_window_are_history_and_nothing_else():
    earlier = days(20, MONDAY - timedelta(days=28))[:8]
    bars = {"VTI": [bt.Bar(day, 90.0 + i, 90.0 + i) for i, day in enumerate(earlier)]
            + series(list(range(100, 112)))}
    result = run(bars, decide=bt.trend_filter(RULES, window=5))
    assert result.sessions == D and result.equity[0]["date"] == D[0]
    # Five closes of history already exist on the first day, so the rule can act on it ...
    assert result.fills[0]["date"] == D[1] and result.fills[0]["decided"] == D[0]
    # ... and nothing was traded or valued on any of the earlier days.
    assert all(fill["date"] >= D[0] for fill in result.fills)
    # Without that history the rule has no average for four sessions, and trades on none.
    late = run({"VTI": series(list(range(100, 112)))}, decide=bt.trend_filter(RULES, window=5))
    assert late.fills[0]["decided"] == D[4]


# ---------------------------------------------------------------- no market, no fill
def test_an_order_waits_for_a_bar_and_fills_on_the_first_one_there_is():
    bars = {"VTI": series(list(range(100, 112)), skip=(D[3], D[4]))}
    result = run(bars, signals=[bt.Signal(at(D[2], 12), "VTI", "BUY", confidence=90)])
    assert [fill["date"] for fill in result.fills] == [D[5]], "not the two sessions with no market"
    assert result.skipped == []


def test_an_order_with_no_market_for_three_sessions_is_given_up_on_and_listed():
    bars = {"VTI": series(list(range(100, 112)), skip=(D[3], D[4], D[5])),
            "VXUS": series([50] * 12)}
    result = run(bars, signals=[bt.Signal(at(D[2], 12), "VTI", "BUY", confidence=90)])
    assert result.fills == []
    assert [(row["date"], row["symbol"], row["action"]) for row in result.skipped] == [(D[5], "VTI", "BUY")]
    assert "no bar for 3 sessions" in result.skipped[0]["why"]
    assert all(point["equity_usd"] == 1000.0 for point in result.equity), "nothing was made up"


def test_a_holding_with_no_bar_is_valued_at_its_last_close_and_the_day_says_so():
    bars = {"VTI": series([100, 100, 110, 110, 110, 120, 120, 120, 120, 120, 120, 120], skip=(D[3], D[4])),
            "VXUS": series([50] * 12)}
    result = run(bars, signals=[bt.Signal(at(D[0], 8), "VTI", "BUY", confidence=90)])
    by_day = {point["date"]: point for point in result.equity}
    assert by_day[D[2]]["stale"] == [] and by_day[D[2]]["invested_usd"] == 550.0      # 5 shares at 110
    assert by_day[D[3]]["stale"] == ["VTI"] and by_day[D[3]]["invested_usd"] == 550.0
    assert by_day[D[5]]["stale"] == [] and by_day[D[5]]["invested_usd"] == 600.0
    assert result.missing == {"VTI": [D[3], D[4]]}
    assert metrics.summarize(result.equity)["days_valued_on_a_stale_price"] == 2


def test_a_stop_is_not_decided_on_a_day_there_was_no_price():
    prices = [100, 100, 100, 80, 80, 80, 80, 80, 80, 80, 80, 80]
    bars = {"VTI": series(prices, skip=(D[3],)), "VXUS": series([50] * 12)}
    result = run(bars, signals=[bt.Signal(at(D[0], 8), "VTI", "BUY", confidence=90)])
    sold = next(fill for fill in result.fills if fill["side"] == "sell")
    assert sold["decided"] == D[4] and sold["date"] == D[5], "decided on the first close that existed"


# ---------------------------------------------------------------- it pays to trade
def test_every_fill_is_on_the_worse_side_of_the_open():
    bars = {"VTI": series([100, 100, 100, 100, 85, 85, 85, 85, 85, 85, 85, 85])}
    kwargs = {"signals": [bt.Signal(at(D[0], 8), "VTI", "BUY", confidence=90)],
              "limit_class_of": lambda _symbol: "broad_diversified_etf"}
    free, charged = run(bars, **kwargs), run(bars, costs=costs.STANDARD, **kwargs)
    bought, sold = charged.fills
    assert bought["price"] == pytest.approx(100.03) and bought["open"] == 100.0
    assert sold["price"] == pytest.approx(85 * (1 - 0.0003)) and sold["open"] == 85.0
    assert [f["price"] for f in free.fills] == [100.0, 85.0] and all(f["cost_usd"] == 0 for f in free.fills)
    paid = sum(fill["cost_usd"] for fill in charged.fills)
    assert paid == pytest.approx(bought["quantity"] * 0.03 + sold["quantity"] * 85 * 0.0003, abs=1e-4)
    # The same trades, and the charged run ends lower by what trading cost (and by the slightly
    # fewer shares the same dollars bought).
    assert free.equity[-1]["equity_usd"] > charged.equity[-1]["equity_usd"]
    assert free.trades[0]["realized_usd"] - charged.trades[0]["realized_usd"] == pytest.approx(paid, abs=0.05)
    assert metrics.summarize(charged.equity, fills=charged.fills)["costs_usd"] == pytest.approx(paid, abs=1e-4)


def test_a_stop_decided_on_a_close_fills_at_the_next_open_gap_and_all():
    # Bought at 100. Closes at 89, through the 10% stop; opens the next day at 80.
    bars = {"VTI": series([100, 100, (99, 89), (80, 82), 82, 82, 82, 82, 82, 82, 82, 82])}
    result = run(bars, signals=[bt.Signal(at(D[0], 8), "VTI", "BUY", confidence=90)])
    sold = result.fills[1]
    assert (sold["decided"], sold["date"], sold["price"], sold["reason"]) == (D[2], D[3], 80.0, "stop_loss")
    trade = result.trades[0]
    assert trade["return_pct"] == pytest.approx(-20.0) and trade["closed_for"] == "stop_loss"
    assert trade["realized_usd"] == pytest.approx(-100.0), "the gap is the strategy's loss, not the stop price"


def test_a_position_leaves_at_its_target_and_at_the_end_of_its_holding_period():
    rules = bt.Rules(universe=("VTI", "VXUS"), max_positions=2, position_pct=50.0, take_profit_pct=8.0,
                     max_holding_days=7)
    bars = {"VTI": series([100, 100, 105, 109, 109, 109, 109, 109, 109, 109, 109, 109]),
            "VXUS": series([50] * 12)}
    result = run(bars, rules=rules, signals=[bt.Signal(at(D[0], 8), "VTI", "BUY", confidence=90),
                                             bt.Signal(at(D[0], 8), "VXUS", "BUY", confidence=90)])
    closed = {trade["symbol"]: trade for trade in result.trades}
    assert closed["VTI"]["closed_for"] == "take_profit" and closed["VTI"]["closed_on"] == D[4]
    assert closed["VXUS"]["closed_for"] == "max_holding" and closed["VXUS"]["held_days"] >= 7


# ---------------------------------------------------------------- the strategy's own rules
def test_what_a_strategy_may_buy_how_many_and_how_large():
    bars = {symbol: series([100] * 12) for symbol in ("VTI", "VXUS", "BND", "AAPL")}
    rules = bt.Rules(universe=("VTI", "VXUS", "BND"), max_positions=2, position_pct=30.0, min_confidence=80)
    signals = [bt.Signal(at(D[0], 8, m), s, "BUY", confidence=c) for m, (s, c) in enumerate(
        [("AAPL", 99), ("VTI", 79), ("VTI", 85), ("VTI", 95), ("VXUS", 90), ("BND", 90)])]
    result = run(bars, rules=rules, signals=signals)
    assert [(f["symbol"], f["quantity"] * f["price"]) for f in result.fills] == [("VTI", 300.0), ("VXUS", 300.0)]
    why = [(row["symbol"], row["why"]) for row in result.skipped]
    assert why == [("AAPL", "outside what the strategy may buy"),
                   ("VTI", "confidence below the strategy's own bar"),
                   ("VTI", "already held, or already being bought"),
                   ("BND", "the strategy's positions are full")]
    # Each of those is in the record with what it would have needed.
    assert result.skipped[1]["confidence"] == 79 and result.skipped[1]["needed"] == 80
    # Research that gave no confidence at all is not research a purchase is made on.
    silent = run(bars, rules=bt.Rules(("VTI",), 1, 30.0), signals=[bt.Signal(at(D[0], 8), "VTI", "BUY")])
    assert silent.fills == [] and silent.skipped[0]["confidence"] is None


def test_the_cap_on_what_is_deployed_and_the_minimum_order_both_hold():
    bars = {symbol: series([100] * 12) for symbol in ("VTI", "VXUS")}
    signals = [bt.Signal(at(D[0], 8), "VTI", "BUY", confidence=90),
               bt.Signal(at(D[0], 8, 1), "VXUS", "BUY", confidence=90)]
    capped = run(bars, signals=signals, deploy_cap_usd=600.0)
    assert [round(f["quantity"] * f["price"], 2) for f in capped.fills] == [500.0, 100.0]
    # The second position would be $100, which is under this minimum: it is not opened small.
    floor = run(bars, signals=signals, deploy_cap_usd=600.0, min_order_usd=150.0)
    assert len(floor.fills) == 1 and "below the minimum order" in floor.skipped[0]["why"]
    assert floor.skipped[0]["room_usd"] == 100.0
    assert max(point["invested_usd"] for point in capped.equity) == 600.0


def test_research_that_says_sell_or_trim_is_acted_on_only_for_what_is_held():
    bars = {symbol: series([100] * 12) for symbol in ("VTI", "VXUS")}
    result = run(bars, signals=[bt.Signal(at(D[0], 8), "VTI", "BUY", confidence=90),
                                bt.Signal(at(D[2], 12), "VTI", "REDUCE"),
                                bt.Signal(at(D[3], 12), "VXUS", "SELL"),
                                bt.Signal(at(D[5], 12), "VTI", "SELL")])
    assert [(f["date"], f["side"], f["quantity"]) for f in result.fills] == [
        (D[0], "buy", 5.0), (D[3], "sell", 2.5), (D[6], "sell", 2.5)]
    assert [(row["symbol"], row["why"]) for row in result.skipped] == [("VXUS", "nothing is held to sell")]
    assert result.trades[0]["quantity"] == 5.0 and result.trades[0]["realized_usd"] == 0.0


# ---------------------------------------------------------------- money in is not money made
def test_a_deposit_and_a_withdrawal_change_the_account_and_not_the_return():
    bars = {"VTI": series([100] * 12)}
    result = run(bars, flows=[bt.Flow(D[3], 500.0), bt.Flow(D[7], -200.0)])
    by_day = {point["date"]: point for point in result.equity}
    assert by_day[D[2]]["equity_usd"] == 1000.0 and by_day[D[3]]["equity_usd"] == 1500.0
    assert by_day[D[7]]["equity_usd"] == 1300.0 and by_day[D[7]]["funded_usd"] == 1300.0
    summary = metrics.summarize(result.equity, opening_usd=1000.0)
    assert summary["net_return_pct"] == 0.0 and summary["max_drawdown_pct"] == 0.0
    assert summary["result_usd"] == 0.0 and summary["funded_usd"] == 1300.0


def test_a_deposit_on_a_day_the_market_was_shut_arrives_at_the_next_session():
    saturday = (date.fromisoformat(D[4]) + timedelta(days=1)).isoformat()
    result = run({"VTI": series([100] * 12)}, flows=[bt.Flow(saturday, 250.0)])
    by_day = {point["date"]: point["equity_usd"] for point in result.equity}
    assert by_day[D[4]] == 1000.0 and by_day[D[5]] == 1250.0


def test_a_gain_is_a_gain_whatever_was_deposited_around_it():
    bars = {"VTI": series([100, 100, 100, 100, 100, 110, 110, 110, 110, 110, 110, 110])}
    plain = run(bars, signals=[bt.Signal(at(D[0], 8), "VTI", "BUY", confidence=90)])
    funded = run(bars, signals=[bt.Signal(at(D[0], 8), "VTI", "BUY", confidence=90)],
                 flows=[bt.Flow(D[8], 5000.0)])
    a = metrics.summarize(plain.equity, opening_usd=1000.0)
    b = metrics.summarize(funded.equity, opening_usd=1000.0)
    assert a["net_return_pct"] == b["net_return_pct"] == pytest.approx(5.0)      # 10% on half the money
    assert b["equity_usd"] == a["equity_usd"] + 5000.0


# ---------------------------------------------------------------- what a strategy is compared with
def test_the_passive_answer_buys_the_same_funds_at_the_same_size_and_never_sells():
    bars = {"VTI": series([100, 100, 100, 70, 70, 70, 70, 70, 70, 70, 70, 70]), "VXUS": series([50] * 12)}
    passive = bt.passive_rules(RULES)
    assert (passive.stop_loss_pct, passive.max_holding_days, passive.take_profit_pct) == (0.0, 0, 0.0)
    result = run(bars, rules=passive, decide=bt.buy_and_hold(passive))
    assert [(f["date"], f["symbol"], f["side"]) for f in result.fills] == [
        (D[1], "VTI", "buy"), (D[1], "VXUS", "buy")]
    assert result.trades == [] and result.equity[-1]["positions"] == 2, "a 30% fall and it is still held"


def test_the_simple_rule_holds_above_its_average_and_leaves_below_it():
    rules = bt.Rules(universe=("VTI",), max_positions=1, position_pct=100.0)
    bars = {"VTI": series([100, 101, 102, 103, 104, 105, 99, 95, 94, 96, 104, 108])}
    result = run(bars, rules=rules, decide=bt.trend_filter(rules, window=3))
    # Above its three-session average on the third day; below it on the seventh; above again on
    # the tenth (94, 96 after 95: an average of 95). Each acted on at the next open.
    assert [(f["decided"], f["date"], f["side"]) for f in result.fills] == [
        (D[2], D[3], "buy"), (D[6], D[7], "sell"), (D[9], D[10], "buy")]
    assert result.fills[1]["reason"] == "below its 3-session average"


def test_the_reference_benchmark_is_bought_at_its_weights_and_held():
    bars = {"VTI": series([100] * 6 + [110] * 6), "BND": series([50] * 12)}
    result = bt.held_weights(bars=bars, start=D[0], end=D[-1], weights={"VTI": 60, "BND": 40},
                             opening_cash_usd=1000.0, costs=costs.FREE, flows=[bt.Flow(D[8], 500.0)])
    assert [(f["date"], f["symbol"], round(f["quantity"] * f["price"], 2)) for f in result.fills] == [
        (D[0], "VTI", 600.0), (D[0], "BND", 400.0), (D[8], "VTI", 300.0), (D[8], "BND", 200.0)]
    summary = metrics.summarize(result.equity, fills=result.fills, opening_usd=1000.0)
    assert summary["net_return_pct"] == pytest.approx(6.0), "10% on the 60%, and the deposit is not a gain"
    assert result.params["weights_pct"] == {"VTI": 60.0, "BND": 40.0}
    # It pays to trade like everything else.
    charged = bt.held_weights(bars={"VTI": series([100] * 12), "BND": series([50] * 12)}, start=D[0],
                              end=D[-1], weights={"VTI": 60, "BND": 40}, opening_cash_usd=1000.0)
    assert metrics.summarize(charged.equity, opening_usd=1000.0)["net_return_pct"] < 0
    # And when one of its funds has no market, it waits rather than buying half a portfolio.
    gap = bt.held_weights(bars={"VTI": series([100] * 12), "BND": series([50] * 12, skip=(D[0], D[1]))},
                          start=D[0], end=D[-1], weights={"VTI": 60, "BND": 40},
                          opening_cash_usd=1000.0, costs=costs.FREE)
    assert {f["date"] for f in gap.fills} == {D[2]}


# ---------------------------------------------------------------- the history an experiment reads
def _frame(rows):
    import pandas as pd

    return pd.DataFrame([{"Date": pd.Timestamp(day), "Open": opened, "Close": closed}
                         for day, opened, closed in rows])


def test_only_sessions_that_are_over_are_read():
    """The vendor publishes a bar for the day in progress whose close is only the last trade."""
    from zoneinfo import ZoneInfo

    from agentic_trading.auto import bars

    ny = ZoneInfo("America/New_York")
    tuesday = datetime(2026, 10, 6, tzinfo=ny)
    assert bars.last_completed_session(tuesday.replace(hour=11)) == "2026-10-05"
    assert bars.last_completed_session(tuesday.replace(hour=16, minute=5)) == "2026-10-05"
    assert bars.last_completed_session(tuesday.replace(hour=16, minute=20)) == "2026-10-06"
    assert bars.last_completed_session(datetime(2026, 10, 10, 12, tzinfo=ny)) == "2026-10-09"   # Saturday
    assert bars.last_completed_session(datetime(2026, 10, 12, 9, tzinfo=ny)) == "2026-10-09"    # Monday, early

    asked = []

    def load(symbol, end):
        asked.append((symbol, end))
        return _frame([("2026-10-01", 100.0, 101.0), ("2026-10-02", 101.0, 102.0),
                       ("2026-10-05", 102.0, 103.0), ("2026-10-06", 103.0, 99.5)])     # the last is partial

    read = bars.read(["vti", "VTI"], start="2026-10-02", end="2026-12-31", load=load,
                     now=tuesday.replace(hour=11))
    assert asked == [("VTI", "2026-10-05")], "asked for nothing past the last finished session, once"
    assert [(bar.date, bar.open, bar.close) for bar in read["VTI"]] == [
        ("2026-10-02", 101.0, 102.0), ("2026-10-05", 102.0, 103.0)]
    assert bars.CALIBER == "adjusted"


def test_a_fund_with_no_history_is_an_error_and_what_was_read_is_identified():
    from agentic_trading.auto import bars

    now = datetime(2026, 10, 6, 18, tzinfo=NY)

    def broken(_symbol, _end):
        raise ConnectionError("vendor down")

    with pytest.raises(bars.BarsUnavailable, match="VTI: ConnectionError"):
        bars.read(["VTI"], start="2026-10-01", end="2026-10-06", load=broken, now=now)
    with pytest.raises(bars.BarsUnavailable, match="no finished session"):
        bars.read(["VTI"], start="2026-10-01", end="2026-10-06", now=now, load=lambda *_a: _frame([]))
    # A row with no usable price is not a session.
    odd = bars.read(["VTI"], start="2026-10-01", end="2026-10-06", now=now, load=lambda *_a: _frame(
        [("2026-10-01", 100.0, 101.0), ("2026-10-02", 0.0, 101.5), ("2026-10-05", 102.0, float("nan"))]))
    assert [bar.date for bar in odd["VTI"]] == ["2026-10-01"]
    # The digest is of the data: the same bars the same, one changed price different.
    one = {"VTI": series([100, 101, 102])}
    assert bars.digest(one) == bars.digest({"VTI": series([100, 101, 102])})
    assert bars.digest(one) != bars.digest({"VTI": series([100, 101, 102.01])})
    assert bars.digest(one) != bars.digest({"VXUS": series([100, 101, 102])})


# ---------------------------------------------------------------- the record, and what is said of it
def test_the_same_inputs_give_the_same_ledger():
    bars = {"VTI": series([(100 + i % 5, 101 + i % 7) for i in range(12)]), "VXUS": series([50] * 12)}
    kwargs = {"decide": bt.trend_filter(RULES, window=3), "costs": costs.STANDARD,
              "signals": [bt.Signal(at(D[1], 12), "VXUS", "BUY", confidence=90)]}
    assert run(bars, **kwargs).as_dict() == run(bars, **kwargs).as_dict()


def test_what_is_still_waiting_at_the_end_is_said_and_not_assumed_filled():
    result = run({"VTI": series([100] * 12)},
                 signals=[bt.Signal(at(D[-1], 12), "VTI", "BUY", confidence=90)])
    assert result.fills == [], "there is no open after the last day to fill it at"
    quiet = run({"VTI": series([100] * 12)}, decide=bt.buy_and_hold(bt.Rules(("VTI",), 2, 50.0)),
                end=D[0])
    assert quiet.fills == [] and quiet.pending == [
        {"symbol": "VTI", "action": "BUY", "reason": "passive", "decided": D[0]}]


def test_a_figure_that_cannot_be_computed_is_absent_not_zero():
    empty = metrics.summarize([])
    assert empty["net_return_pct"] is None and empty["max_drawdown_pct"] is None
    assert empty["equity_usd"] is None and empty["turnover_pct"] is None
    one = metrics.summarize([{"date": D[0], "equity_usd": 1000.0, "funded_usd": 1000.0}])
    assert one["net_return_pct"] is None, "one point is not a return"
    assert one["win_rate_pct"] is None and "not given under 10" in one["win_rate_note"]
    trades = [{"realized_usd": 5.0}] * 7 + [{"realized_usd": -2.0}] * 3
    ten = metrics.summarize([{"date": D[0], "equity_usd": 1000.0, "funded_usd": 1000.0}], trades=trades)
    assert ten["win_rate_pct"] == 70.0 and ten["win_rate_note"] is None
    assert (ten["trades_won"], ten["trades_lost"], ten["worst_trade_usd"]) == (7, 3, -2.0)


def test_confidence_is_reported_beside_what_happened_and_never_as_a_probability():
    table = metrics.by_confidence([
        {"confidence": 88, "realized_usd": 4.0}, {"confidence": 81, "realized_usd": -9.0},
        {"confidence": 95, "realized_usd": 1.0}, {"confidence": None, "realized_usd": -1.0}])
    assert table["bands"] == {
        "80-89": {"trades": 2, "won": 1, "lost": 1, "realized_usd": -5.0},
        "90-99": {"trades": 1, "won": 1, "lost": 0, "realized_usd": 1.0},
        "not recorded": {"trades": 1, "won": 0, "lost": 1, "realized_usd": -1.0}}
    assert "不是盈利概率" in table["note"]["zh"] and "not a probability" in table["note"]["en"]
    # Nothing in it is a rate, a chance or an expectation: counts and sums only.
    for band in table["bands"].values():
        assert set(band) == {"trades", "won", "lost", "realized_usd"}
