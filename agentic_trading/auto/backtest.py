"""A backtest that cannot see what came next, does not trade on data that is not there, and pays to trade.

Three ways a backtest lies are closed here by construction rather than by care
(docs/todo.md, P2.3):

**It cannot use a price from before it could have known to trade.** Everything is decided on
closes that have already happened and filled at the next session's open. A conclusion that
existed at a moment ``T`` -- a research run recorded live -- fills at the first open strictly
after ``T``; a rule that reads today's close trades tomorrow. A rule is handed a view that
holds nothing after the day it is deciding on, so it cannot peek even by mistake, and the test
suite checks the property that matters: changing any bar after a date changes nothing dated
on or before it.

**It does not fill where there was no market.** A symbol with no bar on a session does not
trade that session. An order waits a few sessions for one and is then dropped and listed, with
the reason. A holding with no bar is valued at its last close and the day says so.

**It pays to trade.** Every fill goes through the same cost model the live simulation's fills
do (``auto.costs``): the worse side of the price, by the spread and slippage assumed and the
fees documented.

**It does not act on research the live path would have let lapse.** A recorded conclusion has
the lifetime the live path gives it (``research_lifetime_minutes``), and a replay must say what
that is. What counts is how old it was at the first moment the live path could have acted: the
moment it concluded, when the market was open then, and otherwise the next open -- so research
finished on a Friday evening is two and a half days old by Monday's open and lapses under a
two-day lifetime, as it does live. A conclusion reached while the market was open is acted on
live that same session; with daily bars there is no price after it until the close, so here it
still fills at the next open, which is the one place the replay is later than the live path.

What it models is a strategy's own rules -- which funds, how many, how large, when to leave --
inside a capital figure and a cap on what may be deployed. It does not replay the Hard Risk
engine's every rule, and a result says so. It is deterministic: the same inputs give the same
ledger, which is what lets a recorded result be checked against the artefacts it claims
(``auto.experiments``).

Pure arithmetic over what it is given. It reads no market and places nothing.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from . import costs as cost_model

NEW_YORK = ZoneInfo("America/New_York")
OPENS_AT = time(9, 30)
#: The regular close. A half day closes earlier; the bars do not say which days those are, so a
#: conclusion in the afternoon of one is taken as reached while the market was still open.
CLOSES_AT = time(16, 0)
BUY, SELL, REDUCE = "BUY", "SELL", "REDUCE"
#: How many sessions an order waits for a bar before it is given up on.
MAX_WAIT_SESSIONS = 3
#: How many sessions a conclusion to buy waits for its fund to be above its average, under a
#: strategy with a trend rule. Research lives two days; so does the wait.
TREND_WAIT_SESSIONS = 2
INCREMENT = 0.000001


@dataclass(frozen=True)
class Bar:
    """One session of one symbol. ``close`` is the price a day is valued and decided at."""

    date: str
    open: float
    close: float


@dataclass(frozen=True)
class Signal:
    """A conclusion that existed at a moment: research recorded live, replayed as it was."""

    at: datetime
    symbol: str
    action: str
    confidence: int | None = None
    source: str | None = None


@dataclass(frozen=True)
class Flow:
    """Money put in (positive) or taken out (negative) on a date. Funding, never result."""

    date: str
    amount_usd: float


@dataclass(frozen=True)
class Intent:
    """What a rule wants done, decided on a close and carried out at the next open."""

    symbol: str
    action: str
    reason: str = "rule"


@dataclass(frozen=True)
class Rules:
    """A strategy's own rules, as a strategy version records them (``auto.store.BEHAVIOUR``)."""

    universe: tuple[str, ...]
    max_positions: int
    position_pct: float
    min_confidence: int = 0
    stop_loss_pct: float = 0.0
    take_profit_pct: float = 0.0
    max_holding_days: int = 0
    #: Sessions in the strategy's own trend average: research is acted on only while the fund's
    #: last finished close is above it, and a close below it ends the position. Zero for none.
    trend_window: int = 0

    @classmethod
    def of(cls, strategy: dict[str, Any]) -> Rules:
        return cls(universe=tuple(str(s).upper() for s in strategy.get("universe") or ()),
                   max_positions=int(strategy.get("max_positions") or 0),
                   position_pct=float(strategy.get("position_pct") or 0.0),
                   min_confidence=int(strategy.get("min_confidence") or 0),
                   stop_loss_pct=float(strategy.get("stop_loss_pct") or 0.0),
                   take_profit_pct=float(strategy.get("take_profit_pct") or 0.0),
                   max_holding_days=int(strategy.get("max_holding_days") or 0),
                   trend_window=int(strategy.get("trend_window") or 0))


@dataclass
class _Lot:
    quantity: float
    spent_usd: float
    opened_on: str
    bought: float = 0.0
    proceeds_usd: float = 0.0
    last_close: float | None = None
    reason: str = ""

    @property
    def cost(self) -> float:
        return self.spent_usd / self.bought if self.bought else 0.0


class View:
    """What a rule may read when it decides on one day's close: that day, and nothing after it."""

    def __init__(self, day: str, history: dict[str, list[Bar]], held: dict[str, _Lot], cash: float):
        self.date = day
        self._history = history
        self._held = held
        self.cash = cash

    def closes(self, symbol: str) -> list[float]:
        """Every close of this symbol up to and including the day being decided, oldest first."""
        return [bar.close for bar in self._history.get(symbol.upper(), [])]

    def bars(self, symbol: str) -> list[Bar]:
        return list(self._history.get(symbol.upper(), []))

    def holds(self, symbol: str) -> bool:
        return symbol.upper() in self._held

    @property
    def held(self) -> tuple[str, ...]:
        return tuple(self._held)


Decide = Callable[[View], Iterable[Intent]]


@dataclass
class Result:
    """The whole ledger of a run. Everything a figure about it is computed from is in here."""

    start: str
    end: str
    sessions: list[str]
    equity: list[dict[str, Any]]
    fills: list[dict[str, Any]]
    trades: list[dict[str, Any]]
    #: Orders and conclusions that came to nothing, each with why. As much a part of the record
    #: as the fills: what a strategy did not do is information about it.
    skipped: list[dict[str, Any]]
    pending: list[dict[str, Any]]
    missing: dict[str, list[str]]
    params: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"start": self.start, "end": self.end, "sessions": self.sessions,
                "equity": self.equity, "fills": self.fills, "trades": self.trades,
                "skipped": self.skipped, "pending": self.pending, "missing": self.missing,
                "params": self.params}


def opens_at(session: str) -> datetime:
    """The moment a session's first tradable price exists."""
    return datetime.combine(date.fromisoformat(session), OPENS_AT, tzinfo=NEW_YORK)


def closes_at(session: str) -> datetime:
    """The moment a session's regular trading ends."""
    return datetime.combine(date.fromisoformat(session), CLOSES_AT, tzinfo=NEW_YORK)


def _floor(quantity: float) -> float:
    return math.floor(quantity / INCREMENT + 1e-9) * INCREMENT


def run(*, bars: dict[str, list[Bar]], start: str, end: str, capital_usd: float, rules: Rules,
        costs: cost_model.Costs = cost_model.STANDARD, signals: Iterable[Signal] = (),
        research_lifetime_minutes: float | None = None,
        decide: Decide | None = None, flows: Iterable[Flow] = (),
        opening_cash_usd: float | None = None, deploy_cap_usd: float | None = None,
        min_order_usd: float = 1.0,
        limit_class_of: Callable[[str], str | None] = lambda _symbol: None) -> Result:
    """Run a strategy's rules over daily bars from ``start`` to ``end`` inclusive.

    ``bars`` may reach back before ``start``: earlier bars are what a rule's history is made of
    and nothing else -- no session before ``start`` is traded or valued. ``capital_usd`` is the
    plan's capital, which positions are sized as a share of; ``opening_cash_usd`` is what the
    account starts with (the capital, unless told otherwise) and ``deploy_cap_usd`` what may be
    invested at cost at any one time (the capital, unless told otherwise).

    ``research_lifetime_minutes`` is required whenever ``signals`` are given: a replay that
    left it out would act on research the live path had let lapse, which is the flattering
    kind of mistake. ``math.inf`` says "never lapses" in so many words.
    """
    table = {symbol.upper(): sorted(rows, key=lambda bar: bar.date) for symbol, rows in bars.items()}
    by_day = {symbol: {bar.date: bar for bar in rows} for symbol, rows in table.items()}
    sessions = sorted({bar.date for rows in table.values() for bar in rows if start <= bar.date <= end})
    trading_days = {bar.date for rows in table.values() for bar in rows}
    cap = float(capital_usd if deploy_cap_usd is None else deploy_cap_usd)
    cash = float(capital_usd if opening_cash_usd is None else opening_cash_usd)
    funded = cash
    held: dict[str, _Lot] = {}
    queue: list[dict[str, Any]] = []
    waiting = sorted(signals, key=lambda s: s.at)
    if waiting and research_lifetime_minutes is None:
        raise ValueError("a replay of research needs the research's lifetime "
                         "(research_lifetime_minutes); math.inf if it never lapses")
    lifetime = math.inf if research_lifetime_minutes is None else float(research_lifetime_minutes)
    money = sorted(flows, key=lambda f: f.date)
    out = Result(start=start, end=end, sessions=sessions, equity=[], fills=[], trades=[], skipped=[],
                 pending=[], missing={}, params={
                     "capital_usd": float(capital_usd), "opening_cash_usd": cash, "deploy_cap_usd": cap,
                     "min_order_usd": float(min_order_usd), "costs": costs.name,
                     "rules": {"universe": list(rules.universe), "max_positions": rules.max_positions,
                               "position_pct": rules.position_pct, "min_confidence": rules.min_confidence,
                               "stop_loss_pct": rules.stop_loss_pct,
                               "take_profit_pct": rules.take_profit_pct,
                               "max_holding_days": rules.max_holding_days,
                               "trend_window": rules.trend_window},
                     "research_lifetime_minutes": None if math.isinf(lifetime) else lifetime,
                     "fill": "the next session's open after the decision",
                     "decided_on": "closes on or before the day of the decision"})

    def invested_at_cost() -> float:
        return sum(lot.cost * lot.quantity for lot in held.values())

    def skip(day: str, symbol: str, action: str, why: str, **more: Any) -> None:
        out.skipped.append({"date": day, "symbol": symbol, "action": action, "why": why, **more})

    def fill(day: str, order: dict[str, Any], bar: Bar) -> None:
        nonlocal cash
        symbol, action = order["symbol"], order["action"]
        klass = limit_class_of(symbol)
        when = date.fromisoformat(day)
        if action == BUY:
            room = min(cash, cap - invested_at_cost())
            dollars = min(float(order["dollars"]), room)
            if dollars < min_order_usd - 1e-9:
                skip(day, symbol, action, "below the minimum order once cash and the cap were "
                                         "counted", wanted_usd=round(float(order["dollars"]), 2),
                     room_usd=round(max(room, 0.0), 2))
                return
            priced = costs.fill(cost_model.BUY, bar.open, max(dollars / bar.open, INCREMENT),
                                limit_class=klass, on=when)
            quantity = _floor(dollars / priced.price)
            if quantity <= 0:
                skip(day, symbol, action, "smaller than the smallest quantity that trades")
                return
            priced = costs.fill(cost_model.BUY, bar.open, quantity, limit_class=klass, on=when)
            spent = quantity * priced.price
            cash -= spent
            lot = held.get(symbol)
            if lot is None:
                lot = held[symbol] = _Lot(0.0, 0.0, day, reason=order.get("reason", ""))
            lot.quantity += quantity
            lot.bought += quantity
            lot.spent_usd += spent
        else:
            lot = held.get(symbol)
            if lot is None:
                skip(day, symbol, action, "nothing is held to sell")
                return
            quantity = lot.quantity if action == SELL else _floor(lot.quantity / 2.0)
            if quantity <= 0:
                skip(day, symbol, action, "smaller than the smallest quantity that trades")
                return
            priced = costs.fill(cost_model.SELL, bar.open, quantity, limit_class=klass, on=when)
            received = quantity * priced.price
            cash += received
            lot.proceeds_usd += received
            lot.quantity = round(lot.quantity - quantity, 8)
            if lot.quantity <= INCREMENT:
                spent, proceeds = lot.spent_usd, lot.proceeds_usd
                out.trades.append({
                    "symbol": symbol, "opened_on": lot.opened_on, "closed_on": day,
                    "quantity": round(lot.bought, 8), "spent_usd": round(spent, 2),
                    "proceeds_usd": round(proceeds, 2), "realized_usd": round(proceeds - spent, 2),
                    "return_pct": round((proceeds / spent - 1.0) * 100.0, 4) if spent else None,
                    "held_days": (when - date.fromisoformat(lot.opened_on)).days,
                    "opened_for": lot.reason, "closed_for": order.get("reason", "")})
                del held[symbol]
        out.fills.append({
            "date": day, "symbol": symbol, "side": "buy" if action == BUY else "sell",
            "quantity": round(quantity, 8), "price": priced.price, "open": bar.open,
            "cost_usd": round(abs(priced.price - bar.open) * quantity, 6),
            "fees_usd": priced.fees_usd, "reason": order.get("reason", ""),
            "decided": order.get("decided")})

    def want_to_buy(symbol: str, reason: str, decided: str, day: str, *, confidence: int | None = None,
                    needs_confidence: bool = False) -> None:
        """Queue a purchase if the strategy's own rules allow one. Says why not, otherwise."""
        if symbol not in rules.universe:
            return skip(day, symbol, BUY, "outside what the strategy may buy", decided=decided)
        if needs_confidence and (confidence is None or int(confidence) < rules.min_confidence):
            return skip(day, symbol, BUY, "confidence below the strategy's own bar",
                        confidence=confidence, needed=rules.min_confidence, decided=decided)
        buying = {order["symbol"] for order in queue if order["action"] == BUY}
        if symbol in held or symbol in buying:
            return skip(day, symbol, BUY, "already held, or already being bought", decided=decided)
        if len(held) + len(buying) >= rules.max_positions:
            return skip(day, symbol, BUY, "the strategy's positions are full", decided=decided)
        queue.append({"symbol": symbol, "action": BUY, "reason": reason, "decided": decided,
                      "dollars": float(capital_usd) * rules.position_pct / 100.0, "waited": 0})
        return None

    def above_its_average(symbol: str, before: str) -> bool | None:
        """Whether the last close before ``before`` is above the strategy's trend average.

        None when the fund has too little history for the average: not known, which a strategy
        with a trend rule does not buy on.
        """
        closes = [bar.close for bar in table.get(symbol, []) if bar.date < before]
        if len(closes) < rules.trend_window:
            return None
        return closes[-1] > sum(closes[-rules.trend_window:]) / rules.trend_window

    def market_open_at(moment: datetime) -> bool:
        """Whether a session was trading at ``moment``: the live path could have acted then."""
        day = moment.astimezone(NEW_YORK).date().isoformat()
        return day in trading_days and opens_at(day) <= moment < closes_at(day)

    held_over: list[tuple[Signal, int, datetime | None]] = []
    for index, day in enumerate(sessions):
        # 1. Money in or out, before the open. A date that was not a session lands on the next.
        while money and money[0].date <= day:
            flow = money.pop(0)
            if flow.date < start:
                continue
            cash += flow.amount_usd
            funded += flow.amount_usd
        # 2. Conclusions that existed before this open become orders now -- under a strategy
        #    with a trend rule, only once the fund is above its average, and only while the
        #    research is still current.
        opening = opens_at(day)
        arrived: list[tuple[Signal, int, datetime | None]] = held_over
        held_over = []
        while waiting and waiting[0].at < opening:
            signal = waiting.pop(0)
            # The first moment the live path could have acted on it: the moment it concluded,
            # when the market was open then, and otherwise this open.
            first = signal.at if market_open_at(signal.at) else opening
            arrived.append((signal, index + TREND_WAIT_SESSIONS, first))
        for signal, gives_up_at, first in arrived:
            symbol = signal.symbol.upper()
            decided = signal.at.isoformat()
            if signal.at.astimezone(NEW_YORK).date().isoformat() < start:
                continue                         # from before the window: not this run's to act on
            # A conclusion carried over to wait for the trend is looked at again at this open.
            age = ((first or opening) - signal.at).total_seconds() / 60.0
            if age > lifetime + 1e-9:
                skip(day, symbol, signal.action,
                     "past the research's lifetime " + ("before the market opened" if first
                                                        else "while it waited for the trend"),
                     decided=decided, age_minutes=round(age), lifetime_minutes=lifetime)
                continue
            if signal.action == BUY:
                # Only a purchase the strategy would otherwise make waits on the trend: one it
                # would refuse anyway is refused now, for the reason that actually applies.
                wanted = (symbol in rules.universe and signal.confidence is not None
                          and int(signal.confidence) >= rules.min_confidence)
                if wanted and rules.trend_window and not above_its_average(symbol, day):
                    if index >= gives_up_at:
                        skip(day, symbol, BUY, "never above its own average while the research "
                                               "was current", decided=decided)
                    else:
                        held_over.append((signal, gives_up_at, None))
                    continue
                want_to_buy(symbol, f"research:{signal.source or ''}".rstrip(":"), decided, day,
                            confidence=signal.confidence, needs_confidence=True)
            elif signal.action in (SELL, REDUCE):
                if symbol in held:
                    queue.append({"symbol": symbol, "action": signal.action, "decided": decided,
                                  "reason": f"research:{signal.source or ''}".rstrip(":"), "waited": 0})
                else:
                    skip(day, symbol, signal.action, "nothing is held to sell", decided=decided)
        # 3. Orders fill at this open -- sales first, so what they free can be spent -- or wait.
        still: list[dict[str, Any]] = []
        for order in sorted(queue, key=lambda o: o["action"] == BUY):
            bar = by_day.get(order["symbol"], {}).get(day)
            if bar is None or not (bar.open > 0):
                order["waited"] += 1
                if order["waited"] >= MAX_WAIT_SESSIONS:
                    skip(day, order["symbol"], order["action"],
                         f"no bar for {MAX_WAIT_SESSIONS} sessions; the order was given up on",
                         decided=order.get("decided"))
                else:
                    still.append(order)
                continue
            fill(day, order, bar)
        queue = still
        # 4. The day is valued at its close. A holding with no bar keeps its last close and
        #    the day says which.
        stale, invested = [], 0.0
        for symbol, lot in held.items():
            bar = by_day.get(symbol, {}).get(day)
            if bar is not None and bar.close > 0:
                lot.last_close = bar.close
            else:
                stale.append(symbol)
                out.missing.setdefault(symbol, []).append(day)
            invested += lot.quantity * (lot.last_close if lot.last_close is not None else lot.cost)
        out.equity.append({"date": day, "equity_usd": round(cash + invested, 4),
                           "cash_usd": round(cash, 4), "invested_usd": round(invested, 4),
                           "funded_usd": round(funded, 4), "positions": len(held), "stale": stale})
        # 5. Decisions on this close, to be carried out at the next open.
        leaving = {order["symbol"] for order in queue if order["action"] != BUY}
        for symbol, lot in list(held.items()):
            if symbol in leaving:
                continue
            bar = by_day.get(symbol, {}).get(day)
            days = (date.fromisoformat(day) - date.fromisoformat(lot.opened_on)).days
            why = None
            if rules.max_holding_days and days >= rules.max_holding_days:
                why = "max_holding"
            elif bar is not None and lot.cost > 0:
                move = (bar.close / lot.cost - 1.0) * 100.0
                if rules.stop_loss_pct and move <= -rules.stop_loss_pct + 1e-9:
                    why = "stop_loss"
                elif rules.take_profit_pct and move >= rules.take_profit_pct - 1e-9:
                    why = "take_profit"
            if why is None and rules.trend_window and bar is not None:
                closes = [row.close for row in table.get(symbol, []) if row.date <= day]
                if (len(closes) >= rules.trend_window
                        and closes[-1] < sum(closes[-rules.trend_window:]) / rules.trend_window):
                    why = "trend_break"
            if why:
                queue.append({"symbol": symbol, "action": SELL, "reason": why, "decided": day,
                              "waited": 0})
        if decide is not None:
            history = {symbol: [bar for bar in rows if bar.date <= day] for symbol, rows in table.items()}
            for intent in decide(View(day, history, dict(held), cash)):
                symbol = intent.symbol.upper()
                if intent.action == BUY:
                    want_to_buy(symbol, intent.reason, day, day)
                elif intent.action in (SELL, REDUCE) and symbol in held and symbol not in {
                        order["symbol"] for order in queue if order["action"] != BUY}:
                    queue.append({"symbol": symbol, "action": intent.action, "reason": intent.reason,
                                  "decided": day, "waited": 0})
    out.pending = [{key: order[key] for key in ("symbol", "action", "reason", "decided")}
                   for order in queue]
    return out


# ---------------------------------------------------------------- rules to compare a strategy with
def buy_and_hold(rules: Rules) -> Decide:
    """Buy the strategy's funds, in its own order and at its own size, and never sell.

    The passive answer to the same question: the same funds, the same capital, the same cap on
    what is deployed -- with the stops and the holding period taken out by whoever calls this
    (:func:`passive_rules`), because a portfolio that sells at a stop is not passive.
    """
    def decide(view: View) -> list[Intent]:
        return [Intent(symbol, BUY, "passive") for symbol in rules.universe if not view.holds(symbol)]
    return decide


def passive_rules(rules: Rules) -> Rules:
    return Rules(universe=rules.universe, max_positions=rules.max_positions,
                 position_pct=rules.position_pct)


def trend_filter(rules: Rules, *, window: int) -> Decide:
    """Hold a fund while its close is above its own average over ``window`` sessions; else not.

    The simple rule a strategy has to beat to be worth its research: no model, no judgement,
    one parameter fixed before anything is run. A fund without ``window`` closes of history has
    no average yet, and is neither bought nor sold on one.
    """
    def decide(view: View) -> list[Intent]:
        wanted: list[Intent] = []
        for symbol in rules.universe:
            closes = view.closes(symbol)
            if len(closes) < window:
                continue
            average = sum(closes[-window:]) / window
            if closes[-1] > average and not view.holds(symbol):
                wanted.append(Intent(symbol, BUY, f"above its {window}-session average"))
            elif closes[-1] < average and view.holds(symbol):
                wanted.append(Intent(symbol, SELL, f"below its {window}-session average"))
        return wanted
    return decide


def held_weights(*, bars: dict[str, list[Bar]], start: str, end: str, weights: dict[str, float],
                 opening_cash_usd: float, flows: Iterable[Flow] = (),
                 costs: cost_model.Costs = cost_model.STANDARD,
                 limit_class_of: Callable[[str], str | None] = lambda _symbol: None) -> Result:
    """A fixed-weight portfolio bought and held: the reference benchmark.

    Every dollar is invested at the weights, and money taken out is sold pro rata. No rule, no
    cap, no rebalancing: this is what doing nothing clever with the same money would have been,
    and it pays the same costs. It keeps the fill rule every other row keeps -- decided on a
    close, filled at the next open -- so the money it starts with is invested at the second
    session's open, as the passive row's is, and money that arrives is invested at the open
    after the session it arrived on. Until 2026-10-15 it bought at the first open, a session
    ahead of the rows it was set beside.
    """
    table = {symbol.upper(): {bar.date: bar for bar in rows} for symbol, rows in bars.items()}
    total = sum(float(v) for v in weights.values())
    shares = {symbol.upper(): float(weight) / total for symbol, weight in weights.items()}
    sessions = sorted({day for symbol in shares for day in table.get(symbol, {}) if start <= day <= end})
    out = Result(start=start, end=end, sessions=sessions, equity=[], fills=[], trades=[], skipped=[],
                 pending=[], missing={}, params={
                     "weights_pct": {s: round(w * 100, 4) for s, w in shares.items()},
                     "opening_cash_usd": float(opening_cash_usd), "costs": costs.name,
                     "fill": "the next session's open after the close the money was there on"})
    cash, funded = float(opening_cash_usd), float(opening_cash_usd)
    held = dict.fromkeys(shares, 0.0)
    last = dict.fromkeys(shares, 0.0)
    money = sorted(flows, key=lambda f: f.date)
    # What the last close decided: invest this much (positive) or raise this much (negative).
    decided, decided_on = 0.0, None
    for day in sessions:
        while money and money[0].date <= day:
            flow = money.pop(0)
            if flow.date >= start:
                cash += flow.amount_usd
                funded += flow.amount_usd
        todays = {symbol: table.get(symbol, {}).get(day) for symbol in shares}
        if decided and all(todays.values()):
            when = date.fromisoformat(day)
            # Only what was there at the close the decision was made on: money that arrived
            # before this open is decided on this session's close, like any other.
            if decided > 0 and cash > 0.005:
                budget = min(decided, cash)
                for symbol, share in shares.items():
                    bar = todays[symbol]
                    dollars = budget * share
                    priced = costs.fill(cost_model.BUY, bar.open, max(dollars / bar.open, INCREMENT),
                                        limit_class=limit_class_of(symbol), on=when)
                    quantity = _floor(dollars / priced.price)
                    if quantity <= 0:
                        continue
                    held[symbol] += quantity
                    cash -= quantity * priced.price
                    out.fills.append({"date": day, "symbol": symbol, "side": "buy",
                                      "quantity": round(quantity, 8), "price": priced.price,
                                      "open": bar.open, "fees_usd": priced.fees_usd,
                                      "cost_usd": round(abs(priced.price - bar.open) * quantity, 6),
                                      "reason": "benchmark", "decided": decided_on})
            elif decided < 0 and cash < -0.005:
                owed = min(-decided, -cash)
                value = sum(held[s] * todays[s].open for s in shares)
                for symbol in shares:
                    bar = todays[symbol]
                    quantity = _floor(min(held[symbol], held[symbol] * owed / value if value else 0.0))
                    if quantity <= 0:
                        continue
                    priced = costs.fill(cost_model.SELL, bar.open, quantity,
                                        limit_class=limit_class_of(symbol), on=when)
                    held[symbol] -= quantity
                    cash += quantity * priced.price
                    out.fills.append({"date": day, "symbol": symbol, "side": "sell",
                                      "quantity": round(quantity, 8), "price": priced.price,
                                      "open": bar.open, "fees_usd": priced.fees_usd,
                                      "cost_usd": round(abs(priced.price - bar.open) * quantity, 6),
                                      "reason": "benchmark", "decided": decided_on})
        stale, invested = [], 0.0
        for symbol in shares:
            bar = todays[symbol]
            if bar is not None and bar.close > 0:
                last[symbol] = bar.close
            elif held[symbol] > 0:
                stale.append(symbol)
                out.missing.setdefault(symbol, []).append(day)
            invested += held[symbol] * last[symbol]
        out.equity.append({"date": day, "equity_usd": round(cash + invested, 4),
                           "cash_usd": round(cash, 4), "invested_usd": round(invested, 4),
                           "funded_usd": round(funded, 4),
                           "positions": sum(1 for q in held.values() if q > 0), "stale": stale})
        # Decided on this close, carried out at the next open that has every fund's bar.
        decided, decided_on = (cash, day) if abs(cash) > 0.005 else (0.0, None)
    return out
