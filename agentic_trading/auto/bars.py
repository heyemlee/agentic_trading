"""Daily bars for experiments, from the data layer the rest of the system already reads.

One reader, so a backtest and a research run are looking at the same history, and one statement
of what that history is: prices **adjusted** for dividends and splits by the vendor. That is the
right series for a return over months -- a fund's distributions are in it -- and it means a bar's
price is not the price that was printed on the day once a dividend has since been paid. Every
record made from these bars says so (``caliber``); the live simulation, which fills at the day's
own prices and does not credit dividends, says the other thing, and the two are never added
together.

Only sessions that are over are returned. The vendor publishes a bar for the session in
progress whose close is whatever the last trade was, and an experiment that read it would be
valuing a day that has not finished.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import UTC, datetime, time, timedelta
from typing import Any

from .backtest import NEW_YORK, Bar

CALIBER = "adjusted"
SOURCE = "yfinance daily bars via the upstream data layer (auto_adjust)"
#: When a session's bar can be taken as final: its close, and the vendor a little time after it.
FINAL_AFTER = time(16, 20)


class BarsUnavailable(RuntimeError):
    """A symbol's history could not be read, and why."""


def last_completed_session(now: datetime | None = None) -> str:
    """The newest date whose daily bar is final: today once the close has settled, else earlier.

    By the calendar of weekdays. A holiday has no bar at all, which the caller sees as a session
    that is simply not there.
    """
    moment = (now or datetime.now(UTC)).astimezone(NEW_YORK)
    day = moment.date()
    if moment.time() < FINAL_AFTER:
        day -= timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day.isoformat()


def _vendor(symbol: str, end: str) -> Any:
    from tradingagents.dataflows.stockstats_utils import load_ohlcv

    return load_ohlcv(symbol, end)


def read(symbols: Iterable[str], *, start: str, end: str,
         load: Callable[[str, str], Any] | None = None, now: datetime | None = None) -> dict[str, list[Bar]]:
    """Bars for each symbol from ``start`` to ``end`` inclusive, never past the last finished session.

    A symbol that cannot be read raises: an experiment missing one of its funds is not a smaller
    experiment, it is a different one, and it is for the caller to decide whether to run that.
    """
    final = min(end, last_completed_session(now))
    out: dict[str, list[Bar]] = {}
    for symbol in dict.fromkeys(str(s).upper() for s in symbols):
        try:
            frame = (load or _vendor)(symbol, final)
        except Exception as exc:
            raise BarsUnavailable(f"{symbol}: {type(exc).__name__}: {exc}"[:300]) from exc
        rows: list[Bar] = []
        if frame is not None and not getattr(frame, "empty", True):
            for record in frame.to_dict("records"):
                stamp = record.get("Date")
                day = stamp.date().isoformat() if hasattr(stamp, "date") else str(stamp)[:10]
                try:
                    opened, closed = float(record["Open"]), float(record["Close"])
                except (KeyError, TypeError, ValueError):
                    continue
                if start <= day <= final and opened > 0 and closed > 0:
                    rows.append(Bar(day, opened, closed))
        if not rows:
            raise BarsUnavailable(f"{symbol}: no finished session between {start} and {final}")
        out[symbol] = sorted(rows, key=lambda bar: bar.date)
    return out


def digest(bars: dict[str, list[Bar]]) -> str:
    """What exactly was read, so a record can say which data its figures came from.

    It identifies one read, not one window. The vendor re-adjusts history when a dividend is
    paid, so the same window read a quarter later is not the same data. And its adjusted prices
    are not stable to the last digit even between two downloads minutes apart: on 2026-10-06 a
    close of 211.659897 came back as 211.659882 on the next request, about one part in ten
    million, on hundreds of days of every fund. So a digest that differs says "another download",
    not "the history was revised"; reads served by one cached download have the same digest,
    and the upstream cache fetches again each day, and through the day for a window that ends
    today. Nothing compares a stored digest with a fresh one.
    """
    import hashlib

    blob = hashlib.sha256()
    for symbol in sorted(bars):
        for bar in bars[symbol]:
            blob.update(f"{symbol}|{bar.date}|{bar.open:.6f}|{bar.close:.6f}\n".encode())
    return blob.hexdigest()[:16]
