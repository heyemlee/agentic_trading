"""Deterministic evidence requirements, independent of the model's confidence score."""
import math
from datetime import UTC, datetime

from .portfolio import _trading_days_between, upstream_price_lookup


def collect_evidence(symbol, trade_date, *, reports=None, required_reports=(), price_lookup=None):
    evidence = {"symbol": symbol.upper(), "source": "TradingAgents daily OHLCV",
                "observed_at": datetime.now(UTC).isoformat(),
                "missing_required_reports": [
                    key for key in required_reports if not str((reports or {}).get(key) or "").strip()
                ]}
    try:
        price, date = (price_lookup or upstream_price_lookup)(symbol, trade_date)
        evidence.update(price=price, price_date=str(date))
    except Exception as exc:  # noqa: BLE001 - missing evidence is a rejection, never a guessed value
        evidence["error"] = f"market evidence unavailable: {type(exc).__name__}"
    return evidence


def check_evidence(evidence, symbol, trade_date, max_age_days=3):
    try:
        if evidence.get("error"):
            return False, str(evidence["error"])
        if evidence["symbol"] != symbol.upper() or not evidence["source"].strip():
            return False, "research price has a different symbol or no source"
        price = float(evidence["price"])
        if not math.isfinite(price) or price <= 0:
            return False, "research price must be positive and finite"
        date = datetime.fromisoformat(evidence["price_date"]).date()
        target = datetime.fromisoformat(trade_date).date()
        observed = datetime.fromisoformat(evidence["observed_at"])
        if observed.tzinfo is None or (observed - datetime.now(UTC)).total_seconds() > 5:
            return False, "research evidence has no valid collection timestamp"
        if date > target or _trading_days_between(str(date), trade_date) > max_age_days:
            return False, "research price is future-dated or too old for the analysis date"
        if evidence.get("missing_required_reports"):
            return False, "required reports missing: " + ", ".join(evidence["missing_required_reports"])
        return True, f"verified price {price:g} for {date}; source: {evidence['source']}"
    except (KeyError, TypeError, ValueError, AttributeError):
        return False, "required research price, date, source or collection timestamp is missing/invalid"
