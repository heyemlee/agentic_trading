"""The simulated account's venue: fills that cost something, at prices that are what they say.

The simulator the scenarios use (``broker.SimulatedBroker``) fills at whatever price it is told,
stamps it with its own clock and charges nothing, which is right for proving what the pipeline
does with a rejection or a partial fill and wrong for saying what a strategy would have made.
This is the same simulator with three things replaced, and it is what the simulated account
trades at (docs/todo.md, P2.3):

**A price carries its own time.** The quote this venue gives is the newest bar of the current
session and is stamped with that bar's market time (``auto.quotes``), not with the moment it
was asked. The execution path's freshness check then means here what it means against a real
venue: a quote too old is no quote, and nothing is sent.

**No price, no fill.** When the feed has nothing from the current session, or what it has is
older than the account allows a quote to be, this venue does not fill. It does not fall back to
an earlier close. That is checked here as well as by the caller, so it holds for any caller.

**A fill pays to trade.** A purchase fills above the quote and a sale below it, by the spread
and slippage the cost model assumes and the fees the venue's schedule documents
(``auto.costs``). What each fill gave up is kept beside the order, so a result can be shown
with its costs counted and say how much of it they were.

Its clock is the exchange calendar's, not a flag. Nothing else differs: the same checks on
buying power and holdings, the same order records, the same failure modes a test can ask for.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..broker.base import BrokerOrder, BrokerUnavailable, Clock, OrderRequest, Quote
from ..broker.simulated import SimulatedBroker
from ..storage import atomic_write
from . import costs as cost_model
from . import quotes


class PaperVenue(SimulatedBroker):
    """The simulated account's venue. Built by ``auto.paper.venue`` and by nothing else."""

    name = "simulated"
    is_paper = True

    def __init__(self, *, feed: Callable[[str], quotes.Price] = quotes.DEFAULT,
                 costs: cost_model.Costs = cost_model.STANDARD,
                 limit_class_of: Callable[[str], str | None] = lambda _symbol: None,
                 clock: Callable[[], Clock] | None = None, max_quote_age_seconds: float = 120.0,
                 fill_record_path: str | Path | None = None, **kwargs: Any):
        kwargs.pop("prices", None)              # this venue has no price table to be told
        kwargs.pop("autoprice", None)
        super().__init__(**kwargs)
        self._feed = feed
        self._costs = costs
        self._class_of = limit_class_of
        self._clock = clock
        self._max_quote_age = float(max_quote_age_seconds)
        self._fill_record_path = Path(fill_record_path) if fill_record_path else None
        self._pricing: dict[str, tuple[cost_model.Fill, quotes.Price]] = {}

    # -- the market, as this venue knows it -----------------------------------
    def get_clock(self) -> Clock:
        self._guard()
        if self._clock is not None:
            return self._clock()
        from ..broker.robinhood import market_clock  # the exchange calendar; opens nothing

        return market_clock()

    def _latest(self, symbol: str) -> quotes.Price:
        """The newest price of the current session, or no price at all."""
        try:
            return self._feed(symbol)
        except quotes.FeedError as exc:
            raise BrokerUnavailable(str(exc)) from exc

    def _fillable(self, symbol: str) -> quotes.Price:
        """A price young enough to fill on. Older than the account allows is not a price."""
        price = self._latest(symbol)
        age = price.age_seconds(datetime.now(UTC))
        if age > self._max_quote_age:
            raise BrokerUnavailable(
                f"the newest price for {symbol.upper()} is {age:.0f}s old (limit "
                f"{self._max_quote_age:.0f}s); the simulated venue does not fill on stale data")
        return price

    def _price(self, symbol: str) -> float:
        return self._latest(symbol).price

    def get_quote(self, symbol: str) -> Quote:
        self._guard()
        price = self._latest(symbol)
        half = self._costs.half_spread(self._class_of(symbol)) / 10_000.0
        return Quote(symbol.upper(), price.price, price.as_of, price.source,
                     bid=round(price.price * (1.0 - half), 6), ask=round(price.price * (1.0 + half), 6),
                     received_at=price.fetched_at)

    # -- orders ----------------------------------------------------------------
    def _load_orders(self):
        super()._load_orders()
        self._expire_ended_orders()

    def _expire_ended_orders(self) -> None:
        """Cancel what was still working when the session it was placed in ended.

        A day order can only have been placed while the market was open, so its session is over
        once the market is shut or the market's date has moved on. Read from the clock rather
        than from a wall time: a half day ends when the calendar says it does.
        """
        working = [key for key, order in self._orders.items()
                   if order.is_open and order.submitted_at is not None]
        if not working or getattr(self, "_feed", None) is None:      # not yet fully constructed
            return
        try:
            still_open = bool(self.get_clock().is_open)
        except Exception:  # noqa: BLE001 - an unreadable calendar ends nothing
            return
        today = datetime.now(quotes.NEW_YORK).date()
        changed = False
        for key in working:
            order = self._orders[key]
            placed = order.submitted_at.astimezone(quotes.NEW_YORK).date()
            if today > placed or not still_open:
                self._orders[key] = replace(order, status="canceled")
                changed = True
        if changed:
            self._save_orders()

    def _submit_order(self, request: OrderRequest) -> BrokerOrder:
        self._market_open = bool(self.get_clock().is_open)
        order = super()._submit_order(request)
        self._record_fill(order)
        return order

    def _execution_price(self, request: OrderRequest) -> float:
        price = self._fillable(request.symbol)
        asked = (request.quantity if request.quantity is not None
                 else (request.notional or 0.0) / price.price)
        fill = self._costs.fill(request.side, price.price, max(float(asked), 1e-9),
                                limit_class=self._class_of(request.symbol),
                                on=price.as_of.astimezone(quotes.NEW_YORK).date())
        self._pricing[request.client_order_id] = (fill, price)
        return fill.price

    def _filled_quantity(self, request: OrderRequest, quantity: float, price: float) -> float:
        limit = request.limit_price
        if limit and ((request.side == "buy" and price > limit + 1e-9)
                      or (request.side == "sell" and price < limit - 1e-9)):
            # Accepted and resting: at what it would really cost, the limit is not reached.
            return 0.0
        return super()._filled_quantity(request, quantity, price)

    # -- what each fill gave up -------------------------------------------------
    def _record_fill(self, order: BrokerOrder) -> None:
        priced = self._pricing.pop(order.client_order_id, None)
        if priced is None or self._fill_record_path is None or not order.filled_quantity:
            return
        fill, price = priced
        try:
            rows = json.loads(self._fill_record_path.read_text()) if self._fill_record_path.exists() else {}
        except (OSError, json.JSONDecodeError):
            rows = {}
        rows[order.client_order_id] = {
            "symbol": order.symbol, "side": order.side, "quantity": order.filled_quantity,
            "quote_as_of": price.as_of.isoformat(), "quote_source": price.source,
            "filled_at": (order.submitted_at or datetime.now(UTC)).isoformat(),
            "cost_usd": round(abs(fill.price - fill.quote) * order.filled_quantity, 6),
            "model": self._costs.name, **fill.as_dict()}
        atomic_write(self._fill_record_path, json.dumps(rows, allow_nan=False))


def fill_costs(path: str | Path) -> dict[str, dict[str, Any]]:
    """What each simulated fill gave up to the quote, by client order id. Empty if none yet."""
    try:
        return dict(json.loads(Path(path).read_text()))
    except (OSError, json.JSONDecodeError):
        return {}
