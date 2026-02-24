"""The venue session kept between reads: shared while it is sound, replaced when it is not.

Offline. The sessions here are stand-ins that break when told to and remember being closed.
What is proven: reads share one session; a kept session that breaks under a read is replaced and
that read run once more, while a failure on a session opened for the read is reported as it
came; a session that cannot be opened is reported verbatim and nothing is kept; configuration,
credential, age and idleness each retire a session before anyone reads through it again; one
reader at a time; and the sweep never waits for a reader.
"""

from __future__ import annotations

import threading
import time

import pytest

from agentic_trading.broker.base import BrokerUnavailable
from agentic_trading.broker.read_session import ReadSession, SessionUnavailable


class Session:
    """A stand-in venue session: breaks when told to, and remembers being closed."""

    def __init__(self, number, deadline=None):
        self.number = number
        self.deadline = deadline
        self.broken = False
        self.closed = False

    @property
    def usable(self):
        return not (self.broken or self.closed)

    def credential_deadline(self):
        return self.deadline

    def close(self):
        self.closed = True


class Venue:
    """Opens numbered sessions on request, or refuses to."""

    def __init__(self, deadline=None, refuse=None):
        self.opened: list[Session] = []
        self.deadline = deadline
        self.refuse = refuse

    def __call__(self):
        if self.refuse is not None:
            raise self.refuse
        session = Session(len(self.opened) + 1, self.deadline)
        self.opened.append(session)
        return session


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def kept(*, clock=None, wall=None, **limits):
    return ReadSession(clock=clock or Clock(1_000.0), wall=wall or Clock(1_700_000_000.0), **limits)


def number(session):
    return session.number


def test_reads_share_one_session():
    venue, reads = Venue(), kept()
    assert [reads.read("config", venue, number) for _ in range(3)] == [1, 1, 1]
    assert len(venue.opened) == 1 and not venue.opened[0].closed


def test_a_kept_session_that_breaks_under_a_read_is_replaced_and_the_read_run_again():
    """The venue may end a session it has not heard from. That is not the venue being down, and
    must not reach a screen as though it were."""
    venue, reads = Venue(), kept()
    reads.read("config", venue, number)

    def read(session):
        if session.number == 1:
            session.broken = True                       # a timeout, a dropped connection
            raise BrokerUnavailable("the Robinhood call get_portfolio failed: ReadError")
        return f"answered on session {session.number}"

    assert reads.read("config", venue, read) == "answered on session 2"
    assert venue.opened[0].closed and not venue.opened[1].closed


def test_a_read_that_absorbs_the_failure_is_run_again_too():
    """Preflight catches a failed account read and reports it as a failed gate. The session it
    broke is what says the failure belonged to the session."""
    venue, reads = Venue(), kept()
    reads.read("config", venue, number)

    def preflight(session):
        if session.number == 1:
            session.broken = True
            return "PF01 failed"
        return "PF01 passed"

    assert reads.read("config", venue, preflight) == "PF01 passed"


def test_a_failure_on_a_session_opened_for_the_read_is_the_venues_and_is_not_repeated():
    venue, reads = Venue(), kept()
    seen = []

    def read(session):
        seen.append(session.number)
        session.broken = True
        raise BrokerUnavailable("the venue is down")

    with pytest.raises(BrokerUnavailable, match="the venue is down"):
        reads.read("config", venue, read)
    assert seen == [1]
    assert venue.opened[0].closed and not reads.is_open


def test_a_read_is_run_again_at_most_once():
    venue, reads = Venue(), kept()
    reads.read("config", venue, number)
    seen = []

    def read(session):
        seen.append(session.number)
        session.broken = True
        raise BrokerUnavailable("still down")

    with pytest.raises(BrokerUnavailable, match="still down"):
        reads.read("config", venue, read)
    assert seen == [1, 2] and not reads.is_open


def test_a_tool_that_answers_with_an_error_leaves_the_session_kept():
    venue, reads = Venue(), kept()

    def read(session):
        raise BrokerUnavailable("Robinhood tool get_portfolio returned an error: not today")

    with pytest.raises(BrokerUnavailable, match="not today"):
        reads.read("config", venue, read)
    assert reads.read("config", venue, number) == 1 and len(venue.opened) == 1


def test_a_session_that_cannot_open_is_reported_verbatim_and_nothing_is_kept():
    reason = "no Robinhood OAuth token. Run `uv run ct-robinhood connect` once"
    venue, reads = Venue(refuse=RuntimeError(reason)), kept()
    with pytest.raises(SessionUnavailable) as caught:
        reads.read("config", venue, lambda session: pytest.fail("there is nothing to read through"))
    assert str(caught.value) == reason
    assert isinstance(caught.value, BrokerUnavailable) and not reads.is_open

    venue.refuse = None
    assert reads.read("config", venue, number) == 1


def test_a_changed_configuration_replaces_the_session_before_anyone_reads_through_it():
    """The real-money switch, the account pin, the endpoint: a session built under the old
    settings is not carried on under the new ones."""
    venue, reads = Venue(), kept()
    reads.read('{"enabled": true}', venue, number)
    assert reads.read('{"enabled": false}', venue, number) == 2
    assert venue.opened[0].closed


def test_a_session_is_replaced_once_a_new_one_would_refresh_the_token():
    wall = Clock(1_700_000_000.0)
    venue, reads = Venue(deadline=wall.now + 600), kept(wall=wall)
    reads.read("config", venue, number)
    wall.now += 599
    assert reads.read("config", venue, number) == 1
    wall.now += 1
    assert reads.read("config", venue, number) == 2
    assert venue.opened[0].closed


def test_a_credential_replaced_on_disk_retires_the_session():
    """Another process refreshed the token, or the owner reconnected: the kept session still
    holds the old one."""
    venue, reads = Venue(deadline=1_800_000_000.0), kept()
    reads.read("config", venue, number)
    venue.opened[0].deadline = 1_800_003_600.0
    assert reads.read("config", venue, number) == 2


def test_age_and_idleness_retire_the_session_on_the_next_read():
    clock = Clock(1_000.0)
    venue, reads = Venue(), kept(clock=clock, idle_seconds=300, max_age_seconds=900)
    reads.read("config", venue, number)
    for _ in range(4):                  # read every four minutes: never idle, but growing old
        clock.now += 240
        reads.read("config", venue, number)
    assert [session.closed for session in venue.opened] == [True, False]
    clock.now += 300
    assert reads.read("config", venue, number) == 3


def test_the_sweep_closes_an_idle_session_and_never_waits_for_a_reader():
    clock = Clock(1_000.0)
    venue, reads = Venue(), kept(clock=clock, idle_seconds=300)
    reads.read("config", venue, number)
    clock.now += 299
    assert reads.release_idle() is False and reads.is_open
    clock.now += 1
    assert reads.release_idle() is True
    assert venue.opened[0].closed and not reads.is_open

    inside, leave = threading.Event(), threading.Event()

    def slow(session):
        inside.set()
        leave.wait(5)

    reader = threading.Thread(target=reads.read, args=("config", venue, slow))
    reader.start()
    try:
        assert inside.wait(5)
        clock.now += 1_000
        started = time.monotonic()
        assert reads.release_idle() is False
        assert time.monotonic() - started < 1
    finally:
        leave.set()
        reader.join(5)


def test_one_reader_at_a_time():
    venue, reads = Venue(), kept()
    guard, active, peaks = threading.Lock(), [0], []

    def read(session):
        with guard:
            active[0] += 1
            peaks.append(active[0])
        time.sleep(0.01)
        with guard:
            active[0] -= 1

    readers = [threading.Thread(target=reads.read, args=("config", venue, read)) for _ in range(8)]
    for reader in readers:
        reader.start()
    for reader in readers:
        reader.join(10)
    assert len(peaks) == 8 and max(peaks) == 1
    assert len(venue.opened) == 1


def test_a_reader_kept_waiting_too_long_is_told_no_session_was_had():
    venue, reads = Venue(), kept(wait_seconds=0.05)
    inside, leave = threading.Event(), threading.Event()

    def hold(session):
        inside.set()
        leave.wait(5)

    holder = threading.Thread(target=reads.read, args=("config", venue, hold))
    holder.start()
    try:
        assert inside.wait(5)
        with pytest.raises(SessionUnavailable, match="held the session"):
            reads.read("config", venue, lambda session: pytest.fail("must not run"))
    finally:
        leave.set()
        holder.join(5)


def test_close_retires_the_kept_session():
    venue, reads = Venue(), kept()
    reads.read("config", venue, number)
    reads.close()
    assert venue.opened[0].closed and not reads.is_open
