"""Which session research and pricing anchor to while the data vendor lags the close.

`_last_session` walks back from today to the newest session with a usable bar, asking
`_session_has_data` about each. That answer was cached for the life of the API process, "no"
included, so a session probed once inside the vendor's lag window stayed unusable until a restart
and everything the next day anchored a session behind.
"""
from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

from agentic_trading import service as service_module

SESSION = "2026-09-15"


@pytest.fixture(autouse=True)
def no_remembered_probes(monkeypatch):
    monkeypatch.setattr(service_module, "_session_probes", {}, raising=False)


@pytest.fixture
def clock(monkeypatch):
    now = [5_000.0]
    monkeypatch.setattr(service_module, "time", SimpleNamespace(monotonic=lambda: now[0]))
    return now


@pytest.fixture
def vendor(monkeypatch):
    """The upstream data layer, publishing only the sessions in `published`."""
    state = SimpleNamespace(published=set(), asked=[])

    def load_ohlcv(symbol, session):
        state.asked.append(session)
        if session not in state.published:
            raise RuntimeError("latest in-range OHLCV bar has no closing price")
        return pd.DataFrame({"Date": [session], "Close": [650.0]})

    monkeypatch.setattr("tradingagents.dataflows.stockstats_utils.load_ohlcv", load_ohlcv)
    return state


def test_a_session_probed_before_its_bar_was_published_is_asked_again(clock, vendor):
    assert service_module._session_has_data(SESSION) is False

    vendor.published.add(SESSION)
    # Within the retry window the "no" stands, so a console polling every few seconds does not
    # send each poll to the vendor.
    assert service_module._session_has_data(SESSION) is False
    assert vendor.asked == [SESSION]

    clock[0] += service_module.SESSION_PROBE_RETRY_SECONDS + 1
    assert service_module._session_has_data(SESSION) is True
    assert vendor.asked == [SESSION, SESSION]


def test_a_published_session_is_remembered(clock, vendor):
    vendor.published.add(SESSION)
    assert service_module._session_has_data(SESSION) is True
    clock[0] += 30 * 86_400
    assert service_module._session_has_data(SESSION) is True
    assert vendor.asked == [SESSION]
