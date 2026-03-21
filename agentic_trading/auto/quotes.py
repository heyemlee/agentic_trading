"""Where the simulated venue's prices come from, and exactly how old each one is.

The real venue quotes the market; the simulated one has to get a price from somewhere, and the
honest thing about wherever that is, is its timestamp. A price is only as recent as the bar it
came out of, so a price here always carries the bar's own market time -- never the moment it
was fetched, and never "now". Everything that decides whether a simulated order may fill reads
that time: the execution path's freshness check, the venue's own refusal to fill on stale data,
and the rule that an order is priced only by a market that has moved on from its research.

The source is the public one-minute bars the data vendor publishes for the current session.
They can lag the market, by a quarter of an hour on some listings, which is the reason the
simulated account allows its venue an older quote than the real account allows its own
(``auto.paper.OVERRIDES``) and says so. When there is no bar from the current session, there is
no price: this module never reaches back to an earlier close to have something to return.
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

NEW_YORK = ZoneInfo("America/New_York")
SOURCE = "vendor_minute_bar"
#: How long one fetched price is reused. One approval reads the venue several times over a
#: second or two -- the account, the positions, the quote, the order -- and those are one read.
REUSE_SECONDS = 30.0


class FeedError(RuntimeError):
    """There is no price from the current session for this symbol, and why."""


@dataclass(frozen=True)
class Price:
    symbol: str
    price: float
    #: The start of the bar the price is from, in market time. The older end of what the bar
    #: covers, so every age computed from it errs toward "too old", never toward "fresh".
    as_of: datetime
    fetched_at: datetime
    source: str = SOURCE

    def age_seconds(self, now: datetime | None = None) -> float:
        return ((now or datetime.now(UTC)) - self.as_of).total_seconds()


def _vendor_minutes(symbol: str) -> Any:
    """The current session's one-minute bars for one symbol, as the vendor publishes them."""
    import yfinance as yf

    return yf.download(symbol, period="1d", interval="1m", progress=False, auto_adjust=False,
                       multi_level_index=False, prepost=False, threads=False)


def last_minute(symbol: str, *, now: datetime | None = None,
                download: Callable[[str], Any] | None = None) -> Price:
    """The newest one-minute bar of the current session, with the bar's own time.

    Raises :class:`FeedError` rather than returning anything older: no bars, a last bar that
    belongs to an earlier session, a close that is not a positive finite number.
    """
    now = now or datetime.now(UTC)
    symbol = str(symbol).upper()
    try:
        frame = (download or _vendor_minutes)(symbol)
    except Exception as exc:
        raise FeedError(f"the price feed could not be read for {symbol}: "
                        f"{type(exc).__name__}: {exc}"[:300]) from exc
    if frame is None or getattr(frame, "empty", True) or "Close" not in getattr(frame, "columns", ()):
        raise FeedError(f"the price feed has no bars for {symbol} in the current session")
    closes = frame["Close"].dropna()
    if closes.empty:
        raise FeedError(f"the price feed has no completed bar for {symbol} in the current session")
    stamp = closes.index[-1]
    as_of = stamp.to_pydatetime() if hasattr(stamp, "to_pydatetime") else stamp
    if not isinstance(as_of, datetime) or as_of.tzinfo is None:
        raise FeedError(f"the price feed's bars for {symbol} carry no market timestamp")
    price = float(closes.iloc[-1])
    if not (math.isfinite(price) and price > 0):
        raise FeedError(f"the price feed returned an unusable price for {symbol}")
    if as_of.astimezone(NEW_YORK).date() != now.astimezone(NEW_YORK).date():
        raise FeedError(f"the newest bar for {symbol} is from "
                        f"{as_of.astimezone(NEW_YORK).date().isoformat()}, not the current session")
    if as_of - now > timedelta(seconds=90):
        raise FeedError(f"the newest bar for {symbol} is stamped in the future")
    return Price(symbol=symbol, price=price, as_of=as_of.astimezone(UTC), fetched_at=now)


class Feed:
    """A price feed that reads the vendor at most once per symbol per ``REUSE_SECONDS``.

    Failures are not kept: a feed that could not be read is asked again the next time.
    """

    def __init__(self, read: Callable[[str], Price] = last_minute, *, reuse_seconds: float = REUSE_SECONDS):
        self._read = read
        self._reuse = float(reuse_seconds)
        self._kept: dict[str, tuple[float, Price]] = {}
        self._lock = threading.Lock()

    def __call__(self, symbol: str) -> Price:
        symbol = str(symbol).upper()
        with self._lock:
            kept = self._kept.get(symbol)
            if kept is not None and time.monotonic() - kept[0] < self._reuse:
                return kept[1]
        price = self._read(symbol)
        with self._lock:
            self._kept[symbol] = (time.monotonic(), price)
        return price


#: The one feed the simulated venue reads by default. Shared, so that a venue rebuilt for every
#: approval does not turn one decision into a dozen requests to the vendor.
DEFAULT = Feed()
