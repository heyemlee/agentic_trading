"""Deterministic, keyless market-regime flag from the upstream OHLCV loader.

Inputs: the index's close versus its 200-day average and the VIX level. This
is intentionally crude. Its job is not to forecast; it is to stop the system
from opening broad-market risk into an obviously stressed tape, and to fail
closed when the data needed to tell is missing.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

import pandas as pd

from ..portfolio import trading_days_between
from ..profile import Profiles

logger = logging.getLogger(__name__)

LEVELS = ("LOW_RISK", "NORMAL", "ELEVATED_RISK", "HIGH_RISK", "UNKNOWN")

#: How long to wait before asking the vendor again after a read that failed, one pause per
#: further attempt. A read of the session in progress is a fresh download every fifteen minutes
#: (upstream's same-day cache rule), and now and then one meets a connection that has just
#: dropped -- yfinance swallows the error and answers with no rows. Asked again a moment later
#: it answers. Only a read that raised is asked again: a series that came back and is too short
#: or too old is an answer, and data that still cannot be read after the last pause is UNKNOWN.
RETRY_PAUSES_SECONDS = (2.0, 5.0)


@dataclass
class RegimeResult:
    level: str
    available: bool
    detail: str
    index_symbol: str | None = None
    index_close: float | None = None
    index_sma200: float | None = None
    index_date: str | None = None
    vix: float | None = None
    vix_date: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


OhlcvLookup = Callable[[str, str], pd.DataFrame]


def upstream_ohlcv(symbol: str, as_of: str) -> pd.DataFrame:
    from tradingagents.dataflows.stockstats_utils import load_ohlcv

    return load_ohlcv(symbol, as_of)


def _unknown(trade_date: str, detail: str, *fields: Any) -> RegimeResult:
    # Whoever acts on the result keeps only "unknown"; the reason is written down here.
    logger.warning("market regime for %s is UNKNOWN: %s", trade_date, detail)
    return RegimeResult("UNKNOWN", False, detail, *fields)


def _read(ohlcv_lookup: OhlcvLookup, symbol: str, trade_date: str,
          sleep: Callable[[float], None]) -> pd.DataFrame:
    """One series from the vendor, asked again after each pause if the read raised."""
    for pause in RETRY_PAUSES_SECONDS:
        try:
            return ohlcv_lookup(symbol, trade_date)
        except Exception as exc:  # noqa: BLE001 - whatever failed, the last attempt decides
            logger.info("reading %s for the market regime failed, asking again in %.0fs: %s: %s",
                        symbol, pause, type(exc).__name__, exc)
            sleep(pause)
    return ohlcv_lookup(symbol, trade_date)


def assess_regime(profiles: Profiles, trade_date: str, ohlcv_lookup: OhlcvLookup = upstream_ohlcv, *,
                  sleep: Callable[[float], None] = time.sleep) -> RegimeResult:
    cfg = profiles.risk.get("market_regime") or {}
    index_symbol = str(cfg.get("index_symbol", "SPY"))
    vol_symbol = str(cfg.get("volatility_symbol", "^VIX"))
    max_age = int(cfg.get("max_data_age_trading_days", 3))
    try:
        from stockstats import wrap

        idx = _read(ohlcv_lookup, index_symbol, trade_date, sleep)
        vol = _read(ohlcv_lookup, vol_symbol, trade_date, sleep)
        if idx is None or idx.empty or vol is None or vol.empty or len(idx) < 200:
            return _unknown(trade_date, "index or volatility series empty or shorter than 200 rows")
        stats = wrap(idx.copy())
        stats["close_200_sma"]
        index_close = float(idx.iloc[-1]["Close"])
        index_sma = float(stats.iloc[-1]["close_200_sma"])
        index_date = str(pd.to_datetime(idx.iloc[-1]["Date"]).date())
        vix = float(vol.iloc[-1]["Close"])
        vix_date = str(pd.to_datetime(vol.iloc[-1]["Date"]).date())
    except Exception as exc:  # noqa: BLE001 - any data failure is UNKNOWN, never a guess
        return _unknown(trade_date, f"regime data unavailable: {type(exc).__name__}: {exc}")

    age = max(trading_days_between(index_date, trade_date), trading_days_between(vix_date, trade_date))
    if age > max_age:
        return _unknown(trade_date, f"regime data is {age} trading days old (limit {max_age})",
                        index_symbol, index_close, index_sma, index_date, vix, vix_date)

    below_sma = index_close < index_sma
    if vix >= float(cfg.get("vix_high_risk_at_or_above", 30)) or (
        below_sma and vix >= float(cfg.get("high_risk_if_index_below_200sma_and_vix_at_or_above", 25))
    ):
        level = "HIGH_RISK"
    elif vix >= float(cfg.get("vix_elevated_at_or_above", 20)) or (
        below_sma and bool(cfg.get("index_below_200sma_is_elevated", True))
    ):
        level = "ELEVATED_RISK"
    elif vix < float(cfg.get("vix_low_risk_below", 15)):
        level = "LOW_RISK"
    else:
        level = "NORMAL"
    detail = (f"{index_symbol} {index_close:.2f} {'below' if below_sma else 'above'} 200-day average {index_sma:.2f} "
              f"({index_date}); {vol_symbol} {vix:.2f} ({vix_date})")
    return RegimeResult(level, True, detail, index_symbol, index_close, index_sma, index_date, vix, vix_date)
