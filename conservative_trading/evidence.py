"""Deterministic evidence requirements, independent of the model's confidence score."""
import math
import re
from datetime import UTC, datetime

from .portfolio import _trading_days_between, upstream_price_lookup

#: How the buckets can appear in prose. Written as patterns because the model does not spell
#: them consistently: MICRO_LIVE, micro-live and "micro live" are the same account.
BUCKET_PATTERNS = {"core": r"core", "sandbox": r"sandbox", "micro_live": r"micro[_\s-]?live"}

#: A bucket name only means an account when it sits next to one of these. Without this, "a core
#: holding" and "the core reason" — ordinary English that appears in almost every report — would
#: read as claims about the CORE account. The list is deliberately short: "core position" and
#: "core equity exposure" are things a report says about a stock, not about an account, so the
#: words kept are only the ones that name a container of money.
ACCOUNT_WORDS = r"bucket|account|allocation|sleeve|book|balance|cash"


def _strings(value):
    """Every piece of prose in a report bundle, however deeply it is nested.

    The pipeline's state is not flat: the analyst reports are top-level strings, but the bull and
    bear cases and the risk debate arrive as dictionaries of transcripts. A check that read only
    the top level would miss the stage where this went wrong — the contamination that prompted
    all this first appears in the research debate.
    """
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for item in value.values() for text in _strings(item)]
    if isinstance(value, (list, tuple)):
        return [text for item in value for text in _strings(item)]
    return []


def foreign_bucket_mentions(reports, known_buckets):
    """Accounts the reports reason about that were never in the snapshot they were given.

    This exists because it happened. A core-bucket run on 2026-09-04 produced six reports that
    reasoned about "a $100 MICRO_LIVE bucket sitting at 100% cash" while the injected snapshot
    plainly said CORE, $2,500 — the portfolio manager's central sizing argument, that no tranche
    could buy a whole share, was false against the account actually under analysis. The injected
    context was correct and the eight other core runs were clean, so the wrong account came from
    the model, not from the pipeline.

    The test is deliberately not "names a bucket other than this one". A core run is handed a
    snapshot listing both CORE and SANDBOX, and writing "no SANDBOX action — this analysis is
    CORE-scoped only" is the correct thing for it to say, not a defect. What cannot be correct is
    an account the run was never shown: on the archived runs that separates the one contaminated
    report from the nine sound ones, which a plainer rule does not.

    Hard Risk would still have refused an order sized against the wrong account. What it cannot
    refuse is a proposal built on a false premise that happens to land inside the limits. The
    only reason anyone noticed this one was that the model volunteered the contradiction in its
    own missing-information list, which is luck rather than a control — so this is the control.
    """
    text = " ".join(_strings(reports))
    if not text:
        return []
    known = {str(name).lower() for name in (known_buckets or ())}
    found = []
    for name, pattern in BUCKET_PATTERNS.items():
        if name in known:
            continue
        # The name used as an account: next to an account word, either side, at most one word
        # in between ("the $100 MICRO_LIVE test bucket").
        near = (rf"\b(?:{pattern})\b(?:\W+\w+){{0,1}}\W+(?:{ACCOUNT_WORDS})\b"
                rf"|\b(?:{ACCOUNT_WORDS})\b(?:\W+\w+){{0,1}}\W+(?:{pattern})\b")
        if re.search(near, text, re.IGNORECASE):
            found.append(name)
    return sorted(found)


def collect_evidence(symbol, trade_date, *, reports=None, required_reports=(), price_lookup=None,
                     bucket=None, known_buckets=()):
    evidence = {"symbol": symbol.upper(), "source": "TradingAgents daily OHLCV",
                "observed_at": datetime.now(UTC).isoformat(),
                "missing_required_reports": [
                    key for key in required_reports if not str((reports or {}).get(key) or "").strip()
                ]}
    if bucket:
        evidence["analysed_bucket"] = bucket
        evidence["snapshot_buckets"] = sorted(known_buckets or (bucket,))
        evidence["foreign_bucket_mentions"] = foreign_bucket_mentions(
            reports, evidence["snapshot_buckets"])
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
        foreign = evidence.get("foreign_bucket_mentions") or []
        if foreign:
            # Refused, not merely flagged. Once the reports describe an account that was never in
            # the snapshot, there is no way to tell which of their conclusions were reached
            # against which capital, and the sizing argument is the part most likely to be wrong.
            # Re-running costs one research slot; acting on it costs more than that.
            return False, ("research reasons about the "
                           + ", ".join(name.upper() for name in foreign)
                           + " bucket, which was not in the snapshot given to it (analysis is for "
                           + f"{evidence['analysed_bucket'].upper()}); the reports do not "
                           "describe the account being traded")
        return True, f"verified price {price:g} for {date}; source: {evidence['source']}"
    except (KeyError, TypeError, ValueError, AttributeError):
        return False, "required research price, date, source or collection timestamp is missing/invalid"
