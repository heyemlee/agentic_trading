"""One venue session kept open between reads, instead of a handshake for every read.

A session with the Robinhood venue costs an OAuth check, an MCP ``initialize`` and a tool
listing before the one or two calls that were wanted. Opened for each read and closed after it,
that was three to thirteen seconds per ``/micro-live`` on the live API on 2026-09-16, polled by
every console screen that shows the real-money account, and the API's other reads slowed while
it ran.

This keeps one session per process for the reads that can safely share it and lends it to one
reader at a time. Which reads those are is the service's decision, and the rule is narrow: a
read that records nothing when the venue fails it. A read that fails on a kept session is run
once more on a fresh session, so a session the venue quietly ended is never reported as the
venue being down -- and anything the read had recorded about its failure would describe a
session, not the venue. So no order comes from here, and no reconciliation either, which marks
the order it was reading when the read fails.

A kept session is replaced, never repaired:

* after a call on it failed below the tool level, a timeout or a transport error, which leaves
  its state unknown (a tool that answered with an error leaves it as good as it was);
* when the configuration it was opened with has changed, the real-money switch included;
* once a session opened now would refresh the stored access token, or when the token on disk
  changed underneath it: the MCP client treats a token it restored from disk as valid for ever
  and answers its expiry with a browser sign-in a service cannot open (see
  ``FileTokenStorage.get_tokens``), so a fresh session is what refreshes it. The session names
  that moment itself, as ``credential_deadline()``;
* when it has been open for ``MAX_AGE_SECONDS``, or unused for ``IDLE_SECONDS``.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any, TypeVar

from .base import BrokerUnavailable

logger = logging.getLogger(__name__)

T = TypeVar("T")

#: Closed after this long unused: longer than the console's slowest poll of a venue read, so a
#: screen someone is watching keeps its session, and nothing stays open for a screen nobody is.
IDLE_SECONDS = 300.0
#: Replaced after this long however busy, which bounds anything a session remembers.
MAX_AGE_SECONDS = 900.0
#: How long a reader waits for the one before it before reporting no session to be had.
WAIT_SECONDS = 120.0


class SessionUnavailable(BrokerUnavailable):
    """No session could be had, so the venue was asked nothing. The reason is kept verbatim."""


class ReadSession:
    """At most one venue session, lent to one reader at a time. Safe to share across threads."""

    def __init__(self, *, idle_seconds: float = IDLE_SECONDS,
                 max_age_seconds: float = MAX_AGE_SECONDS,
                 wait_seconds: float = WAIT_SECONDS,
                 clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time) -> None:
        self.idle_seconds = idle_seconds
        self.max_age_seconds = max_age_seconds
        self.wait_seconds = wait_seconds
        self._clock = clock
        self._wall = wall
        self._lock = threading.Lock()
        self._broker: Any = None
        self._key: object = None
        self._deadline: float | None = None
        self._opened_at = 0.0
        self._used_at = 0.0

    @property
    def is_open(self) -> bool:
        return self._broker is not None

    def read(self, key: object, factory: Callable[[], Any], read: Callable[[Any], T]) -> T:
        """Run ``read`` on the kept session, opening one with ``factory`` when there is none.

        ``key`` stands for the configuration ``factory`` builds from; a different key replaces
        the session. Raises SessionUnavailable when no session could be opened, and otherwise
        returns or raises whatever ``read`` did on the last session it ran on.
        """
        if not self._lock.acquire(timeout=self.wait_seconds):
            raise SessionUnavailable(
                f"another venue read held the session for over {self.wait_seconds:.0f} seconds")
        try:
            self._retire_if_stale(key)
            repeated = False
            while True:
                reused = self._broker is not None
                if not reused:
                    self._open(key, factory)
                broker = self._broker
                try:
                    result, failure = read(broker), None
                except Exception as exc:  # noqa: BLE001 - re-raised below, once the session is settled
                    result, failure = None, exc
                self._used_at = self._clock()
                broke = not _usable(broker)
                if broke:
                    self._retire("a call on it failed")
                if broke and reused and not repeated:
                    # Repeated once, and only after a kept session broke: on a session opened for
                    # this read the same failure is the venue's, and is reported as it came.
                    logger.info("a kept venue session failed under a read; reading again on a "
                                "fresh session")
                    repeated = True
                    continue
                if failure is not None:
                    raise failure
                return result
        finally:
            self._lock.release()

    def release_idle(self) -> bool:
        """Close the session if it went unused or grew old. Never waits for a reader."""
        if not self._lock.acquire(blocking=False):
            return False
        try:
            if self._broker is None:
                return False
            now = self._clock()
            if now - self._used_at >= self.idle_seconds:
                self._retire("it was idle")
            elif now - self._opened_at >= self.max_age_seconds:
                self._retire("it reached its maximum age")
            else:
                return False
            return True
        finally:
            self._lock.release()

    def close(self, timeout: float = 10.0) -> None:
        """Close the kept session once the reader using it, if any, has finished.

        Waits ``timeout`` seconds at most: this runs as a process stops, and whatever stops the
        process will not wait for a venue that has stopped answering.
        """
        if not self._lock.acquire(timeout=timeout):
            logger.warning("the kept venue session was still in use when it was to be closed")
            return
        try:
            if self._broker is not None:
                self._retire("its owner closed it")
        finally:
            self._lock.release()

    # ------------------------------------------------------------------ with the lock held
    def _open(self, key: object, factory: Callable[[], Any]) -> None:
        try:
            broker = factory()
        except Exception as exc:
            raise SessionUnavailable(str(exc)) from exc
        self._broker, self._key = broker, key
        self._opened_at = self._used_at = self._clock()
        self._deadline = _credential_deadline(broker)

    def _retire_if_stale(self, key: object) -> None:
        if self._broker is None:
            return
        now = self._clock()
        reason = None
        if not _usable(self._broker):
            reason = "a call on it failed"
        elif key != self._key:
            reason = "the configuration it was opened with changed"
        elif now - self._opened_at >= self.max_age_seconds:
            reason = "it reached its maximum age"
        elif now - self._used_at >= self.idle_seconds:
            reason = "it was idle"
        else:
            deadline = _credential_deadline(self._broker)
            if deadline != self._deadline:
                reason = "the stored credential changed"
            elif deadline is not None and self._wall() >= deadline:
                reason = "its access token is due to be refreshed"
        if reason:
            self._retire(reason)

    def _retire(self, reason: str) -> None:
        broker, self._broker, self._key, self._deadline = self._broker, None, None, None
        logger.info("closing the kept venue session: %s", reason)
        close = getattr(broker, "close", None)
        if callable(close):
            try:
                close()
            except Exception as exc:  # noqa: BLE001 - a session that will not close is gone anyway
                logger.debug("error closing a kept venue session: %s", exc)


def _usable(broker: Any) -> bool:
    """A venue that keeps no session state, like the simulated one, is taken at its word."""
    return bool(getattr(broker, "usable", True))


def _credential_deadline(broker: Any) -> float | None:
    probe = getattr(broker, "credential_deadline", None)
    if not callable(probe):
        return None
    try:
        return probe()
    except Exception:  # noqa: BLE001 - an unreadable token file changes the deadline; that is all
        return None
