"""A strategy beside what else the same money could have done, under the same conditions.

A return means nothing alone. Whether research-driven trading is worth its cost is a question
about a difference: against doing nothing clever, and against doing something simple. So every
result is put beside four others, and what makes the table worth reading is that all five were
given the same things (docs/todo.md, P2.4):

* the same opening cash, and the same deposits and withdrawals on the same days;
* the same capital to size positions by, and the same cap on what may be deployed;
* the same funds to choose from, the same number of positions, the same size of each;
* the same prices, the same fill rule and the same costs.

The rows:

``passive``      the strategy's own funds, bought at its own size and held. No exits.
``simple_rule``  the same, held only while a fund is above its own moving average. No model.
``benchmark``    the 60/40 portfolio the forward record already keeps
                 (``evaluation.benchmark``), fully invested and held: the reference from
                 outside the plan's own budget.
``cash``         the money left alone. Zero, by construction, and a line worth drawing.

and whatever strategy they are compared with, which the caller supplies -- the simulated
account's own record, or a replay of recorded research. The model's confidence appears nowhere
in this as a chance of anything.

Nothing here selects. Every comparator's whole ledger is returned, losing trades and unfilled
orders included, so a figure in the table can be recomputed from what it was made of.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from . import backtest as bt
from . import costs as cost_model
from . import metrics

#: The simple rule's one parameter, by mode, fixed here before anything is run and not tuned:
#: sessions of history a fund's close is compared with the average of.
AVERAGE_OVER = {"steady": 100, "swing": 20, "intraday": 20}
PASSIVE, SIMPLE_RULE, BENCHMARK, CASH = "passive", "simple_rule", "benchmark", "cash"
ROWS = (PASSIVE, SIMPLE_RULE, BENCHMARK, CASH)

def _both(zh: str, en: str) -> tuple[str, str]:
    return zh, en


SAYS = {
    PASSIVE: _both("被动持有：买入方案里同样的基金、同样的仓位，一直持有不卖。",
                   "Passive: the plan's own funds at the plan's own size, bought and held."),
    SIMPLE_RULE: _both("简单规则：同样的基金和仓位，只在收盘价高于自己 {window} 日均线时持有，不用模型。",
                       "Simple rule: the same funds and size, held only while the close is above "
                       "its own {window}-session average. No model."),
    BENCHMARK: _both("60/40 基准：{weights}，全部资金买入后持有（现有评估用的同一基准）。",
                     "Benchmark: {weights}, fully invested and held -- the one the forward record "
                     "keeps."),
    CASH: _both("现金：钱放着不动。", "Cash: the money left alone."),
}


def cash_only(sessions: list[str], *, opening_cash_usd: float, flows: Iterable[bt.Flow],
              start: str) -> list[dict[str, Any]]:
    """The equity of an account that does nothing, day by day, with the same money in and out."""
    money = sorted(flows, key=lambda f: f.date)
    held = float(opening_cash_usd)
    out = []
    for day in sessions:
        while money and money[0].date <= day:
            flow = money.pop(0)
            if flow.date >= start:
                held += flow.amount_usd
        out.append({"date": day, "equity_usd": round(held, 4), "cash_usd": round(held, 4),
                    "invested_usd": 0.0, "funded_usd": round(held, 4), "positions": 0, "stale": []})
    return out


def run(*, bars: dict[str, list[bt.Bar]], start: str, end: str, mode: str, rules: bt.Rules,
        capital_usd: float, opening_cash_usd: float, deploy_cap_usd: float,
        benchmark_weights: dict[str, float], flows: Iterable[bt.Flow] = (),
        costs: cost_model.Costs = cost_model.STANDARD, min_order_usd: float = 1.0,
        limit_class_of: Callable[[str], str | None] = lambda _symbol: None) -> dict[str, Any]:
    """Every comparator over one window, with the conditions they shared stated once.

    Returns ``conditions`` (what all of them were given), ``rows`` (the figures and a sentence
    for each) and ``ledgers`` (each one's whole record). A comparator that could not be run --
    a benchmark fund with no history in the window -- is a row with its reason and no figures,
    never a row of zeros.
    """
    flows = list(flows)
    window = AVERAGE_OVER.get(mode, AVERAGE_OVER["steady"])
    shared = {"bars": bars, "start": start, "end": end, "capital_usd": capital_usd, "costs": costs,
              "flows": flows, "opening_cash_usd": opening_cash_usd, "deploy_cap_usd": deploy_cap_usd,
              "min_order_usd": min_order_usd, "limit_class_of": limit_class_of}
    plain = bt.passive_rules(rules)
    ledgers: dict[str, bt.Result] = {
        PASSIVE: bt.run(rules=plain, decide=bt.buy_and_hold(plain), **shared),
        SIMPLE_RULE: bt.run(rules=plain, decide=bt.trend_filter(plain, window=window), **shared),
    }
    rows: dict[str, dict[str, Any]] = {}
    missing = [symbol for symbol in benchmark_weights if not bars.get(symbol.upper())]
    if missing:
        rows[BENCHMARK] = {"figures": None, "why_not": f"no history for {', '.join(missing)} in the window"}
    else:
        ledgers[BENCHMARK] = bt.held_weights(
            bars=bars, start=start, end=end, weights=benchmark_weights,
            opening_cash_usd=opening_cash_usd, flows=flows, costs=costs, limit_class_of=limit_class_of)
    sessions = ledgers[PASSIVE].sessions
    idle = cash_only(sessions, opening_cash_usd=opening_cash_usd, flows=flows, start=start)
    for name, ledger in ledgers.items():
        rows[name] = {"figures": metrics.summarize(ledger.equity, fills=ledger.fills,
                                                   trades=ledger.trades, skipped=ledger.skipped,
                                                   opening_usd=opening_cash_usd)}
    rows[CASH] = {"figures": metrics.summarize(idle, opening_usd=opening_cash_usd)}
    weights = "/".join(f"{symbol} {float(share):g}%" for symbol, share in benchmark_weights.items())
    for name in ROWS:
        zh, en = SAYS[name]
        rows.setdefault(name, {"figures": None})
        rows[name]["says"] = {"zh": zh.format(window=window, weights=weights),
                              "en": en.format(window=window, weights=weights)}
    return {
        "conditions": {
            "start": start, "end": end, "sessions": len(sessions),
            "opening_cash_usd": float(opening_cash_usd), "capital_usd": float(capital_usd),
            "deploy_cap_usd": float(deploy_cap_usd), "min_order_usd": float(min_order_usd),
            "flows": [{"date": f.date, "amount_usd": f.amount_usd} for f in flows],
            "universe": list(rules.universe), "max_positions": rules.max_positions,
            "position_pct": rules.position_pct, "costs": costs.describe(),
            "fill": "the next session's open after the decision",
            "simple_rule_average_over_sessions": window,
            "benchmark_weights_pct": {s.upper(): float(w) for s, w in benchmark_weights.items()},
            # What is the same for the two plan-shaped rows and not for the benchmark, said
            # here rather than left to be noticed.
            "note": {"zh": "被动持有和简单规则与方案用同样的资金上限、仓位数和仓位大小；"
                           "60/40 基准不受方案的上限约束，全部资金都投入，是方案之外的参照。",
                     "en": "Passive and the simple rule share the plan's cap, number of positions "
                           "and position size; the 60/40 benchmark is outside the plan's budget "
                           "and fully invested, as the reference from outside it."},
        },
        "rows": rows,
        "ledgers": {name: ledger.as_dict() for name, ledger in ledgers.items()} | {
            CASH: {"equity": idle, "fills": [], "trades": [], "skipped": []}},
    }


def strategy_row(equity: list[dict[str, Any]], *, fills: Iterable[dict[str, Any]] = (),
                 trades: Iterable[dict[str, Any]] = (), skipped: Iterable[dict[str, Any]] = (),
                 opening_usd: float | None, says: dict[str, str]) -> dict[str, Any]:
    """The strategy's own row, computed by the same arithmetic as everything it is beside."""
    return {"figures": metrics.summarize(equity, fills=fills, trades=trades, skipped=skipped,
                                         opening_usd=opening_usd), "says": says}


def differences(strategy: dict[str, Any], rows: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """The strategy's return and drawdown less each comparator's, where both exist.

    Percentage points, and absent wherever either side is: a difference from a figure that
    could not be computed is not a difference of zero.
    """
    mine = strategy.get("figures") or {}
    out: dict[str, Any] = {}
    for name, row in rows.items():
        theirs = row.get("figures") or {}
        pair = {}
        for key in ("net_return_pct", "max_drawdown_pct"):
            a, b = mine.get(key), theirs.get(key)
            pair[key.replace("_pct", "_points")] = None if a is None or b is None else round(a - b, 4)
        out[name] = pair
    return out
