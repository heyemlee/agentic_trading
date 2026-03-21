"""What a record of trading came to, counted the same way whichever record it is.

One set of figures for a backtest, a comparator and the simulated account's own marks, so that a
table putting them side by side is comparing like with like (docs/todo.md, P2.4). Three rules:

**Money put in is not money made.** Return and drawdown are taken on a unit value that is
continuous across a deposit or a withdrawal -- the arithmetic the forward record already uses
(``evaluation._advance``), reused rather than written a second way.

**Costs are in the result and also shown beside it.** A net figure that hides how much of it was
the cost of trading cannot be compared with a strategy that trades less.

**A figure that cannot be computed is absent.** One point is not a return and no closed trade is
not a win rate. None means "not enough to say", never zero.

Nothing here is a judgement. A strategy's own confidence is reported, where it is reported at
all, next to what actually happened to the trades made at that confidence, and is never
described as a chance of profit.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from ..evaluation import STARTING_UNITS, _advance, _drawdown

#: Fewer closed trades than this and a win rate is a coincidence with a percent sign on it.
MIN_TRADES_FOR_A_RATE = 10


def unit_values(equity: list[dict[str, Any]], *, opening_usd: float | None = None) -> list[float]:
    """The value of one unit after each day, continuous across money in and out.

    ``opening_usd`` is what the account held before the first day's trading; given, the series
    starts from it, so what the first day cost is inside the return rather than before it.
    """
    values: list[float] = []
    units = STARTING_UNITS
    funded = None
    if opening_usd is not None and opening_usd > 0:
        values.append(opening_usd / units)
        funded = float(opening_usd)
    for row in equity:
        now_funded = float(row.get("funded_usd") or 0.0)
        flow = 0.0 if funded is None else now_funded - funded
        units, value = _advance(units, float(row["equity_usd"]), flow)
        values.append(value)
        funded = now_funded
    return values


def summarize(equity: list[dict[str, Any]], *, fills: Iterable[dict[str, Any]] = (),
              trades: Iterable[dict[str, Any]] = (), skipped: Iterable[dict[str, Any]] = (),
              opening_usd: float | None = None) -> dict[str, Any]:
    """The figures for one record: what it returned, what it risked, what it cost, what it did."""
    fills, trades, skipped = list(fills), list(trades), list(skipped)
    values = unit_values(equity, opening_usd=opening_usd)
    equities = [float(row["equity_usd"]) for row in equity]
    average = sum(equities) / len(equities) if equities else 0.0
    traded = sum(abs(float(f.get("quantity") or 0.0) * float(f.get("price") or 0.0)) for f in fills)
    paid = sum(float(f.get("cost_usd") or 0.0) for f in fills)
    realized = [float(t.get("realized_usd") or 0.0) for t in trades]
    wins, losses = sum(1 for r in realized if r > 0), sum(1 for r in realized if r < 0)
    exposure = [float(row.get("invested_usd") or 0.0) / float(row["equity_usd"])
                for row in equity if float(row["equity_usd"]) > 0]
    last = equity[-1] if equity else None
    return {
        "sessions": len(equity),
        "from": equity[0]["date"] if equity else None,
        "to": last["date"] if last else None,
        "net_return_pct": (round((values[-1] / values[0] - 1.0) * 100.0, 4)
                           if len(values) >= 2 and values[0] else None),
        "max_drawdown_pct": _drawdown(values) if len(values) >= 2 else None,
        "equity_usd": round(float(last["equity_usd"]), 2) if last else None,
        "funded_usd": round(float(last.get("funded_usd") or 0.0), 2) if last else None,
        "result_usd": (round(float(last["equity_usd"]) - float(last.get("funded_usd") or 0.0), 2)
                       if last else None),
        "average_exposure_pct": round(sum(exposure) / len(exposure) * 100.0, 2) if exposure else None,
        "fills": len(fills),
        "traded_usd": round(traded, 2),
        "turnover_pct": round(traded / average * 100.0, 2) if average else None,
        "costs_usd": round(paid, 4),
        "costs_pct_of_average_equity": round(paid / average * 100.0, 4) if average else None,
        "trades_closed": len(trades),
        "trades_won": wins,
        "trades_lost": losses,
        "realized_usd": round(sum(realized), 2),
        "worst_trade_usd": round(min(realized), 2) if realized else None,
        "win_rate_pct": (round(wins / len(realized) * 100.0, 1)
                         if len(realized) >= MIN_TRADES_FOR_A_RATE else None),
        "win_rate_note": (None if len(realized) >= MIN_TRADES_FOR_A_RATE else
                          f"{len(realized)} closed trade(s); a rate is not given under "
                          f"{MIN_TRADES_FOR_A_RATE}"),
        "came_to_nothing": len(skipped),
        "days_valued_on_a_stale_price": sum(1 for row in equity if row.get("stale")),
    }


def by_confidence(trades: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """What happened to the trades made at each level of the model's own confidence.

    Counts and sums, by band, and nothing derived from them. The confidence a research run
    reports is how sure the model said it was; whether that tracks what happens afterwards is
    exactly what this table exists to let somebody see, and until there are enough trades in a
    band to see it, the honest reading of a band is "not known". It is never a probability.
    """
    bands: dict[str, dict[str, Any]] = {}
    for trade in trades:
        confidence = trade.get("confidence")
        if confidence is None:
            key = "not recorded"
        else:
            low = int(confidence) // 10 * 10
            key = f"{low}-{min(low + 9, 100)}"
        band = bands.setdefault(key, {"trades": 0, "won": 0, "lost": 0, "realized_usd": 0.0})
        realized = float(trade.get("realized_usd") or 0.0)
        band["trades"] += 1
        band["won"] += realized > 0
        band["lost"] += realized < 0
        band["realized_usd"] = round(band["realized_usd"] + realized, 2)
    return {
        "bands": dict(sorted(bands.items())),
        "note": {"zh": "把握度是模型自己给出的评分，不是盈利概率。这里只列出各档实际发生的交易结果；"
                       "样本不足时不能据此推断任何概率。",
                 "en": "Confidence is the model's own rating, not a probability of profit. This "
                       "lists what actually happened to trades at each level; with few trades "
                       "in a band nothing about probability follows from it."},
    }
