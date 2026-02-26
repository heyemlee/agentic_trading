"""Results of costly reads, kept between polls and refreshed off the request path.

Three console reads used to repeat their whole cost on every poll: the market regime fetched
from the data vendor for ``/health``, ``/risk`` and the notification pass; the Robinhood account
behind ``/micro-live``; and ``/portfolio``, which priced every holding, looked each one up and
rewrote the drawdown ladders each time a screen asked. None of their inputs changes that often.

A :class:`Kept` holds the last result of one such read with the inputs it was computed from
(its *key*). A request is answered from it while the key still matches and the result is
younger than ``serve_for``; otherwise the request computes it, so a change to the inputs -- a
ledger written, the configuration edited, a new trading day -- is never answered with a result
from before it. :meth:`Kept.refresh`, called by the API's refresh loop, recomputes it once it
is ``refresh_after`` old, so a request normally finds a result ready, but only while somebody is
reading: a result nobody asked for within ``watch_for`` is left to age rather than recomputed
for a screen that is closed.

Every result carries the moment it was read (``checked_at``), and the console judges freshness
from that rather than from how recently a poll came back.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class _Result[T]:
    value: T
    key: object
    taken_at: float


class Kept[T]:
    """The last result of one costly read, with the inputs it was computed from.

    ``compute`` produces the result; ``key`` describes its inputs cheaply and must change
    whenever they do. ``refresh_after`` and ``serve_for`` may be numbers or functions of the
    result, so that a failed read can be retried sooner than a good one is renewed.
    """

    def __init__(self, compute: Callable[[], T], *, key: Callable[[], object],
                 refresh_after: float | Callable[[T], float],
                 serve_for: float | Callable[[T], float], watch_for: float | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._compute = compute
        self._key = key
        self._refresh_after = refresh_after
        self._serve_for = serve_for
        self.watch_for = watch_for
        self._clock = clock
        self._result: _Result[T] | None = None
        self._read_at: float | None = None
        self._computing = threading.Lock()

    def get(self) -> T:
        """The kept result if its inputs are unchanged and it is young enough, else a new one."""
        self._read_at = self._clock()
        key = self._key()
        result = self._result
        if self._usable(result, key, self._serve_for):
            return result.value
        with self._computing:
            result = self._result          # another request may have computed it meanwhile
            if self._usable(result, key, self._serve_for):
                return result.value
            return self._take(key).value

    def refresh(self) -> bool:
        """Recompute if the result is due and somebody is reading it. Never waits for a
        computation already under way, and raises what ``compute`` raised, keeping the old
        result for requests to judge by its age."""
        if self.watch_for is not None and (
                self._read_at is None or self._clock() - self._read_at > self.watch_for):
            return False
        if not self._computing.acquire(blocking=False):
            return False
        try:
            key = self._key()
            if self._usable(self._result, key, self._refresh_after):
                return False
            self._take(key)
            return True
        finally:
            self._computing.release()

    def _usable(self, result: _Result[T] | None, key: object,
                limit: float | Callable[[T], float]) -> bool:
        if result is None or result.key != key:
            return False
        seconds = limit(result.value) if callable(limit) else limit
        return self._clock() - result.taken_at < seconds

    def _take(self, key: object) -> _Result[T]:
        # Aged from when the read began, which is the older and therefore the honest end of it.
        started = self._clock()
        self._result = _Result(self._compute(), key, started)
        return self._result
