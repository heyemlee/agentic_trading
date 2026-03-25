"""The market regime, read from the vendor (``risk.regime``).

A read of the index or volatility series that fails is asked again before the regime is called
unknown. The simulated account's first week is why: every session from 2026-10-07 to 10-13 was
kept out of the count as ``regime_unknown``, and on each of them the API's log has a SPY download
that came back empty in the same instant as a dropped Robinhood session -- a connection that
failed for a moment, which yfinance reports as "possibly delisted; no price data found". A pass
reads the regime for the session in progress, which upstream fetches again every fifteen
minutes, so about a hundred reads a day reach Yahoo, and one problem on one pass stays on the
whole session (``auto.store.mark_pass``). Nothing here loosens the rule itself: data that cannot
be read after the last attempt, or that is read and is not enough, is still UNKNOWN.
"""

import logging

import pandas as pd
import pytest
from tradingagents.dataflows.errors import NoMarketDataError

from agentic_trading.profile import load_profiles
from agentic_trading.risk import regime

INDEX, VOLATILITY = "SPY", "^VIX"


def series(trade_date: str, close: float, *, rows: int = 260) -> pd.DataFrame:
    """Daily bars up to ``trade_date`` as upstream's ``load_ohlcv`` returns them, flat at ``close``."""
    dates = pd.bdate_range(end=pd.Timestamp(trade_date), periods=rows)
    return pd.DataFrame({"Date": dates, "Close": close, "High": close, "Low": close,
                         "Open": close, "Volume": 0})


def vendor(failures: dict[str, int] | None = None, *, index: float = 500.0, vix: float = 14.0,
           rows: int = 260):
    """A lookup that fails each symbol the given number of times, then answers. Records each call."""
    left = dict(failures or {})
    calls: list[str] = []

    def lookup(symbol: str, trade_date: str) -> pd.DataFrame:
        calls.append(symbol)
        if left.get(symbol, 0) > 0:
            left[symbol] -= 1
            # What the read raises when yfinance swallowed a dropped connection and answered nothing.
            raise NoMarketDataError(symbol, symbol, "Yahoo Finance returned no rows")
        return series(trade_date, index if symbol == INDEX else vix, rows=rows)

    lookup.calls = calls
    return lookup


@pytest.fixture
def profiles(config_dir):
    return load_profiles(config_dir)


def test_an_index_read_that_fails_once_is_asked_again_not_called_unknown(profiles):
    lookup, pauses = vendor({INDEX: 1}), []
    result = regime.assess_regime(profiles, "2026-10-13", lookup, sleep=pauses.append)
    assert result.available is True and result.level == "LOW_RISK"
    assert result.index_date == result.vix_date == "2026-10-13"
    assert lookup.calls == [INDEX, INDEX, VOLATILITY]
    assert pauses == [regime.RETRY_PAUSES_SECONDS[0]], "it waits before asking again"


def test_the_volatility_read_is_given_the_same_second_chance(profiles):
    lookup, pauses = vendor({VOLATILITY: 2}), []
    result = regime.assess_regime(profiles, "2026-10-13", lookup, sleep=pauses.append)
    assert result.available is True
    assert lookup.calls == [INDEX, VOLATILITY, VOLATILITY, VOLATILITY]
    assert pauses == list(regime.RETRY_PAUSES_SECONDS)


def test_a_vendor_that_keeps_failing_is_still_unknown_and_says_why(profiles, caplog):
    lookup, pauses = vendor({INDEX: 99}), []
    with caplog.at_level(logging.WARNING, logger=regime.logger.name):
        result = regime.assess_regime(profiles, "2026-10-13", lookup, sleep=pauses.append)
    assert result.level == "UNKNOWN" and result.available is False
    assert "regime data unavailable" in result.detail and "returned no rows" in result.detail
    # Bounded: one first try and one more per pause, and the volatility series is never asked.
    assert lookup.calls == [INDEX] * (1 + len(regime.RETRY_PAUSES_SECONDS))
    assert pauses == list(regime.RETRY_PAUSES_SECONDS)
    # The pass keeps only "unavailable"; the reason has to be somewhere a person can find it.
    assert any("returned no rows" in record.getMessage() for record in caplog.records)


def test_a_series_that_is_read_and_is_too_short_is_an_answer_and_is_not_asked_again(profiles):
    lookup, pauses = vendor(rows=150), []
    result = regime.assess_regime(profiles, "2026-10-13", lookup, sleep=pauses.append)
    assert result.level == "UNKNOWN" and "shorter than 200 rows" in result.detail
    assert lookup.calls == [INDEX, VOLATILITY] and pauses == []


def test_old_data_is_still_too_old_however_it_was_read(profiles):
    lookup, pauses = vendor({INDEX: 1}), []
    result = regime.assess_regime(profiles, "2026-10-20", lambda symbol, _day: lookup(symbol, "2026-10-13"),
                                  sleep=pauses.append)
    assert result.level == "UNKNOWN" and "trading days old" in result.detail
