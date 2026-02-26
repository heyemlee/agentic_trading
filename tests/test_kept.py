"""A kept result: served while its inputs are unchanged and it is young, renewed off the request
path while somebody reads it, and never served across a change to what it was computed from.

Offline and clock-driven. The reads here count how often they ran.
"""

from __future__ import annotations

import threading
import time

import pytest

from agentic_trading.service.kept import Kept


class Clock:
    def __init__(self, now=1_000.0):
        self.now = now

    def __call__(self):
        return self.now


class Read:
    """A costly read: counts its runs and answers with the inputs it saw."""

    def __init__(self, inputs):
        self.inputs = inputs
        self.runs = 0

    def __call__(self):
        self.runs += 1
        return {"run": self.runs, "inputs": self.inputs["value"]}


def kept(read, inputs, clock, **timing):
    timing = {"refresh_after": 60.0, "serve_for": 120.0, **timing}
    return Kept(read, key=lambda: inputs["value"], clock=clock, **timing)


def test_polls_are_answered_from_one_read_while_it_is_young():
    clock, inputs = Clock(), {"value": "ledger r1"}
    read = Read(inputs)
    reading = kept(read, inputs, clock)
    assert reading.get()["run"] == 1
    clock.now += 119
    assert reading.get()["run"] == 1
    assert read.runs == 1


def test_a_change_to_the_inputs_is_never_answered_from_before_it():
    """A ledger written a second ago: the next request reads again, however young the result."""
    clock, inputs = Clock(), {"value": "ledger r1"}
    read = Read(inputs)
    reading = kept(read, inputs, clock)
    reading.get()
    inputs["value"] = "ledger r2"
    clock.now += 1
    assert reading.get() == {"run": 2, "inputs": "ledger r2"}


def test_a_request_reads_again_once_nothing_renewed_the_result_in_time():
    clock, inputs = Clock(), {"value": "config"}
    read = Read(inputs)
    reading = kept(read, inputs, clock)
    reading.get()
    clock.now += 120
    assert reading.get()["run"] == 2


def test_renewal_happens_off_the_request_path_only_when_due():
    clock, inputs = Clock(), {"value": "config"}
    read = Read(inputs)
    reading = kept(read, inputs, clock)
    assert reading.refresh() is True          # nothing kept yet
    clock.now += 59
    assert reading.refresh() is False
    clock.now += 1
    assert reading.refresh() is True
    assert reading.get()["run"] == 2 and read.runs == 2


def test_nothing_is_renewed_for_a_screen_nobody_has_open():
    clock, inputs = Clock(), {"value": "config"}
    read = Read(inputs)
    reading = kept(read, inputs, clock, watch_for=300.0)
    assert reading.refresh() is False         # never read
    reading.get()
    clock.now += 61
    assert reading.refresh() is True
    clock.now += 300
    assert reading.refresh() is False         # last read 361 s ago
    assert read.runs == 2


def test_how_long_a_result_lasts_can_depend_on_what_it_says():
    """A failed read is retried sooner than a good one is renewed."""
    clock, inputs = Clock(), {"value": "session"}
    answers = iter([{"available": False}, {"available": True}, {"available": True}])
    reading = Kept(lambda: next(answers), key=lambda: inputs["value"], clock=clock,
                   refresh_after=lambda r: 900.0 if r["available"] else 120.0,
                   serve_for=lambda r: 1800.0 if r["available"] else 300.0)
    assert reading.get() == {"available": False}
    clock.now += 120
    assert reading.refresh() is True
    clock.now += 600
    assert reading.refresh() is False and reading.get() == {"available": True}


def test_a_failed_renewal_keeps_the_last_result_to_be_judged_by_its_age():
    clock, inputs = Clock(), {"value": "config"}
    outcomes = iter([{"run": 1}, RuntimeError("the vendor is down"), {"run": 3}])

    def read():
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    reading = kept(read, inputs, clock)
    reading.get()
    clock.now += 60
    with pytest.raises(RuntimeError, match="vendor is down"):
        reading.refresh()
    assert reading.get() == {"run": 1}        # still young enough to serve
    clock.now += 60
    assert reading.get() == {"run": 3}        # too old: the request reads for itself


def test_concurrent_requests_share_one_read_and_renewal_never_waits_for_it():
    clock, inputs = Clock(), {"value": "config"}
    started, release = threading.Event(), threading.Event()
    runs = []

    def slow():
        runs.append(1)
        started.set()
        release.wait(5)
        return len(runs)

    reading = Kept(slow, key=lambda: inputs["value"], clock=clock, refresh_after=60.0,
                   serve_for=120.0)
    answers = []
    requests = [threading.Thread(target=lambda: answers.append(reading.get())) for _ in range(4)]
    requests[0].start()
    assert started.wait(5)
    for request in requests[1:]:
        request.start()
    begun = time.monotonic()
    assert reading.refresh() is False         # a read is under way; renewal does not queue
    assert time.monotonic() - begun < 1
    release.set()
    for request in requests:
        request.join(5)
    assert answers == [1, 1, 1, 1] and len(runs) == 1
