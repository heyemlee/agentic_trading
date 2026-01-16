"""Deterministic in-process paper broker.

Its purpose is validation, not convenience: it models the failure modes the pipeline must
survive (rejection, duplicate client order id, partial fill, unavailable state, closed
market) so every Stage 3 safety property can be proven offline, with no network and no
credentials. The Alpaca adapter implements the same protocol against the real paper venue.
"""

from __future__ import annotations

import itertools
import json
from contextlib import nullcontext
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from ..storage import atomic_write, file_lock
from .base import (
    Account,
    BrokerOrder,
    BrokerPosition,
    BrokerRejection,
    BrokerUnavailable,
    Clock,
    DuplicateOrder,
    OrderRequest,
    Quote,
)


class SimulatedBroker:
    name = "simulated"
    is_paper = True

    def __init__(self, *, cash: float = 3000.0, positions: dict[str, tuple[float, float]] | None = None,
                 prices: dict[str, float] | None = None, market_open: bool = True,
                 fill_mode: str = "full", reject_reason: str | None = None,
                 unavailable: bool = False, trading_blocked: bool = False, now: datetime | None = None,
                 autoprice: bool = False, order_state_path: str | Path | None = None):
        self._cash = cash
        self._positions = {s.upper(): (q, p) for s, (q, p) in (positions or {}).items()}
        self._prices = {s.upper(): p for s, p in (prices or {}).items()}
        self._market_open = market_open
        self._fill_mode = fill_mode           # full | partial | none (accepted but unfilled)
        self._reject_reason = reject_reason
        self._unavailable = unavailable
        self._trading_blocked = trading_blocked
        self._autoprice = autoprice
        self._now = now or datetime.now(UTC)
        self._orders: dict[str, BrokerOrder] = {}
        self._ids = itertools.count(1)
        self._order_state_path = Path(order_state_path) if order_state_path else None
        self._load_orders()

    def _load_orders(self):
        if self._order_state_path and self._order_state_path.exists():
            rows = json.loads(self._order_state_path.read_text())
            for row in rows:
                if row.get("submitted_at"):
                    row["submitted_at"] = datetime.fromisoformat(row["submitted_at"])
            self._orders = {row["client_order_id"]: BrokerOrder(**row) for row in rows}

    def _save_orders(self):
        if self._order_state_path:
            atomic_write(self._order_state_path, json.dumps(
                [asdict(order) for order in self._orders.values()], default=str, allow_nan=False))

    # -- test controls -------------------------------------------------------
    def set_price(self, symbol: str, price: float) -> None:
        self._prices[symbol.upper()] = price

    def set_cash(self, cash: float) -> None:
        self._cash = cash

    def set_market_open(self, is_open: bool) -> None:
        self._market_open = is_open

    def set_unavailable(self, flag: bool) -> None:
        self._unavailable = flag

    # -- protocol ------------------------------------------------------------
    def _guard(self) -> None:
        if self._unavailable:
            raise BrokerUnavailable("simulated broker: state unavailable")

    def get_account(self) -> Account:
        self._guard()
        equity = self._cash + sum(q * self._price(s) for s, (q, _) in self._positions.items())
        return Account(account_id="SIM-PAPER", cash=self._cash, buying_power=self._cash, equity=equity,
                       trading_blocked=self._trading_blocked, shorting_enabled=False, multiplier="1",
                       options_level=0, fractional=True, as_of=self._now)

    def get_positions(self) -> list[BrokerPosition]:
        self._guard()
        out = []
        for symbol, (qty, avg) in sorted(self._positions.items()):
            price = self._price(symbol)
            out.append(BrokerPosition(symbol, qty, avg, qty * price, (price - avg) * qty))
        return out

    def get_open_orders(self) -> list[BrokerOrder]:
        self._guard()
        self._load_orders()
        return [o for o in self._orders.values() if o.is_open]

    def get_order_by_client_id(self, client_order_id: str) -> BrokerOrder | None:
        self._guard()
        self._load_orders()
        return self._orders.get(client_order_id)

    def get_clock(self) -> Clock:
        self._guard()
        return Clock(is_open=self._market_open, timestamp=self._now)

    def get_quote(self, symbol: str) -> Quote:
        self._guard()
        price = self._price(symbol)
        return Quote(symbol.upper(), price, self._now, "last_trade",
                     bid=round(price * 0.9995, 4), ask=round(price * 1.0005, 4))

    def submit_order(self, request: OrderRequest) -> BrokerOrder:
        with file_lock(self._order_state_path) if self._order_state_path else nullcontext():
            self._load_orders()
            return self._submit_order(request)

    def _submit_order(self, request: OrderRequest) -> BrokerOrder:
        self._guard()
        if request.client_order_id in self._orders:
            raise DuplicateOrder(f"client_order_id {request.client_order_id} already exists",
                                 self._orders[request.client_order_id])
        if self._trading_blocked:
            raise BrokerRejection("account is blocked from trading", code="account_blocked")
        if self._reject_reason:
            raise BrokerRejection(self._reject_reason, code="simulated_rejection")
        if not self._market_open and request.time_in_force == "day":
            raise BrokerRejection("market is closed for a day order", code="market_closed")

        price = request.limit_price or self._price(request.symbol)
        qty = request.quantity if request.quantity is not None else (request.notional or 0.0) / price
        cost = qty * price
        if request.side == "buy" and cost > self._cash + 1e-6:
            raise BrokerRejection(f"insufficient buying power: need {cost:.2f}, have {self._cash:.2f}",
                                  code="insufficient_buying_power")
        if request.side == "sell":
            held = self._positions.get(request.symbol.upper(), (0.0, 0.0))[0]
            if qty > held + 1e-9:
                raise BrokerRejection(f"cannot sell {qty:.4f} of {request.symbol}: only {held:.4f} held "
                                      "(shorting disabled)", code="insufficient_position")

        order_id = f"sim-{uuid4().hex}" if self._order_state_path else f"sim-{next(self._ids):04d}"
        filled = {"full": qty, "partial": round(qty / 2, 6), "none": 0.0}[self._fill_mode]
        status = "filled" if filled >= qty - 1e-9 else ("partially_filled" if filled > 0 else "new")
        order = BrokerOrder(order_id=order_id, client_order_id=request.client_order_id, symbol=request.symbol.upper(),
                            side=request.side, quantity=qty, notional=request.notional, order_type=request.order_type,
                            status=status, filled_quantity=filled, filled_avg_price=price if filled else None,
                            limit_price=request.limit_price, submitted_at=self._now)
        self._orders[request.client_order_id] = order
        self._save_orders()  # a simulated venue must survive a crash before ledger accounting
        if filled:
            self._apply_fill(order.symbol, request.side, filled, price)
        return order

    def poll_order(self, order_id: str, timeout_seconds: float = 30.0,
                   interval_seconds: float = 2.0) -> BrokerOrder:
        self._guard()
        self._load_orders()
        for order in self._orders.values():
            if order.order_id == order_id:
                return order
        raise BrokerUnavailable(f"no order {order_id}")

    def cancel_order(self, order_id: str) -> None:
        self._guard()
        with file_lock(self._order_state_path) if self._order_state_path else nullcontext():
            self._load_orders()
            self._cancel_order(order_id)

    def _cancel_order(self, order_id: str):
        for key, order in self._orders.items():
            if order.order_id == order_id and order.is_open:
                self._orders[key] = replace(order, status="canceled")
                self._save_orders()
                return

    # -- internals -----------------------------------------------------------
    def _price(self, symbol: str) -> float:
        symbol = symbol.upper()
        if symbol in self._prices:
            return self._prices[symbol]
        if not self._autoprice:
            raise BrokerUnavailable(f"simulated broker has no price for {symbol}")
        # Simulate a venue quote by seeding from the upstream daily close once. The returned
        # Quote is stamped with the simulator's clock, because this stands in for a live venue
        # price; nothing here is used by the Alpaca adapter, which quotes the real market.
        try:
            from tradingagents.dataflows.stockstats_utils import load_ohlcv

            df = load_ohlcv(symbol, self._now.date().isoformat())
            if df is None or df.empty:
                raise ValueError("no rows")
            price = float(df.iloc[-1]["Close"])
        except Exception as exc:
            raise BrokerUnavailable(f"simulated broker could not price {symbol}: {exc}") from exc
        self._prices[symbol] = price
        return price

    def _apply_fill(self, symbol: str, side: str, qty: float, price: float) -> None:
        held, avg = self._positions.get(symbol, (0.0, 0.0))
        if side == "buy":
            new_qty = held + qty
            self._positions[symbol] = (new_qty, (held * avg + qty * price) / new_qty if new_qty else price)
            self._cash -= qty * price
        else:
            new_qty = max(0.0, held - qty)
            if new_qty <= 1e-9:
                self._positions.pop(symbol, None)
            else:
                self._positions[symbol] = (new_qty, avg)
            self._cash += qty * price
