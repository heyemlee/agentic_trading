"""Broker boundary.

The research and risk system never imports a broker SDK. It speaks to this small protocol,
so replacing Alpaca with another venue (Robinhood Agentic Trading is the likely future one)
is a new implementation of this file's protocol and nothing else.

Every method either returns a typed value or raises. There is no "probably fine" return: an
unclear broker state raises ``BrokerUnavailable`` and the caller fails closed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol


class BrokerError(RuntimeError):
    """Base for every broker failure."""


class BrokerUnavailable(BrokerError):
    """State could not be read, or was inconsistent or malformed. Never assume anything."""


class BrokerRejection(BrokerError):
    """The broker refused the order. Carries the venue's reason."""

    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        self.code = code


class DuplicateOrder(BrokerError):
    """An order with this client_order_id already exists. The prior order is attached."""

    def __init__(self, message: str, existing: BrokerOrder | None = None):
        super().__init__(message)
        self.existing = existing


@dataclass(frozen=True)
class Account:
    account_id: str
    cash: float
    buying_power: float
    equity: float
    currency: str = "USD"
    trading_blocked: bool = False
    shorting_enabled: bool = False
    multiplier: str = "1"
    options_level: int = 0
    fractional: bool = True
    as_of: datetime | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BrokerPosition:
    symbol: str
    quantity: float
    average_entry_price: float
    market_value: float
    unrealized_pl: float = 0.0
    side: str = "long"


@dataclass(frozen=True)
class BrokerOrder:
    order_id: str
    client_order_id: str
    symbol: str
    side: str                  # buy | sell
    quantity: float | None
    notional: float | None
    order_type: str            # market | limit
    status: str                # new | accepted | partially_filled | filled | canceled | rejected | expired
    filled_quantity: float = 0.0
    filled_avg_price: float | None = None
    limit_price: float | None = None
    submitted_at: datetime | None = None
    reason: str | None = None

    @property
    def is_open(self) -> bool:
        return self.status in ("new", "accepted", "pending_new", "partially_filled", "held")

    @property
    def is_terminal(self) -> bool:
        return self.status in ("filled", "canceled", "rejected", "expired", "done_for_day")


@dataclass(frozen=True)
class Quote:
    """Current price from the execution venue, with the timestamp needed to age-check it."""

    symbol: str
    price: float
    as_of: datetime
    source: str                # last_trade | last_quote_mid | position_mark
    bid: float | None = None
    ask: float | None = None

    @property
    def spread_pct(self) -> float | None:
        if self.bid and self.ask and self.bid > 0:
            return (self.ask - self.bid) / self.bid * 100.0
        return None


@dataclass(frozen=True)
class Clock:
    is_open: bool
    timestamp: datetime
    next_open: datetime | None = None
    next_close: datetime | None = None


@dataclass(frozen=True)
class OrderRequest:
    symbol: str
    side: str                       # buy | sell
    client_order_id: str
    quantity: float | None = None
    notional: float | None = None
    order_type: str = "limit"       # limit is the default; market only where the profile allows it
    limit_price: float | None = None
    time_in_force: str = "day"
    extended_hours: bool = False


class BrokerAdapter(Protocol):
    """The whole broker surface Stage 3 needs. Deliberately small."""

    name: str
    is_paper: bool

    def get_account(self) -> Account: ...
    def get_positions(self) -> list[BrokerPosition]: ...
    def get_open_orders(self) -> list[BrokerOrder]: ...
    def get_order_by_client_id(self, client_order_id: str) -> BrokerOrder | None: ...
    def get_clock(self) -> Clock: ...
    def get_quote(self, symbol: str) -> Quote: ...
    def submit_order(self, request: OrderRequest) -> BrokerOrder: ...
    def poll_order(self, order_id: str, timeout_seconds: float = 30.0,
                   interval_seconds: float = 2.0) -> BrokerOrder: ...
    def cancel_order(self, order_id: str) -> None: ...
