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


#: Attributes of the provenance envelope agentic_trading.llm.web_fetch puts in front of every
#: page it returns. Parsed out of the audit copy of the tool result, which is why that envelope
#: header has to be the first bytes of the tool's return value.
_ENVELOPE_RE = re.compile(r'<web_source\s+([^>]*)>')
_ATTR_RE = re.compile(r'(\w+)="([^"]*)"')


def web_sources_from_calls(tool_calls):
    """Structured provenance for every page the allowlisted fetch tool returned.

    Only route (a) appears here. The CLI's own WebSearch and WebFetch run inside the CLI
    process, so nothing in this codebase saw the URL, the redirect chain or the bytes, and
    there is no provenance record to write down. That is why a fact that exists only because
    WebSearch found it can inform a report but can never be evidence: the asymmetry is a
    property of this function, not a policy statement.
    """
    sources, searched = [], False
    for call in tool_calls or ():
        tool = str((call or {}).get("tool") or "")
        if tool in ("WebSearch", "WebFetch"):
            searched = True
            continue
        if not tool.endswith("fetch_url"):
            continue
        record = {"tool": tool, "requested": (call.get("input") or {}).get("url")}
        match = _ENVELOPE_RE.search(str(call.get("result_excerpt") or ""))
        if match:
            record.update(dict(_ATTR_RE.findall(match.group(1))))
        sources.append(record)
    return sources, searched


def collect_evidence(symbol, trade_date, *, reports=None, required_reports=(), price_lookup=None,
                     bucket=None, known_buckets=(), llm_calls=()):
    evidence = {"symbol": symbol.upper(), "source": "TradingAgents daily OHLCV",
                "observed_at": datetime.now(UTC).isoformat(),
                "missing_required_reports": [
                    key for key in required_reports if not str((reports or {}).get(key) or "").strip()
                ]}
    tool_calls = [c for call in (llm_calls or ()) for c in (call.get("tool_calls") or ())]
    evidence["web_sources"], evidence["web_search_used"] = web_sources_from_calls(tool_calls)
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


def check_web_sources(evidence, max_age_minutes, now=None):
    """Provenance and retrieval age of web-sourced evidence.

    The clock is the moment *this system read the page*, taken from the envelope our own code
    wrote. It is deliberately not the page's own publication date: a scraped date is untrusted
    content, and treating it as a limit would make a model-visible string into a safety
    boundary. So this bounds how stale our reading is, not how stale the news is.

    Empty ``web_sources`` passes. The yfinance-only path must be unaffected by this rule.
    """
    sources = evidence.get("web_sources") or []
    if not sources:
        return True, "no web-sourced evidence in this proposal"
    moment = now or datetime.now(UTC)
    ages = []
    for source in sources:
        missing = [k for k in ("url", "domain", "retrieved_at", "sha256") if not source.get(k)]
        if missing:
            return False, (f"a web source is missing {', '.join(missing)}; it did not come back "
                           f"through the allowlisted fetch tool ({source.get('requested')!r})")
        try:
            retrieved = datetime.fromisoformat(str(source["retrieved_at"]))
        except ValueError:
            return False, f"web source {source['url']} has an unreadable retrieval time"
        if retrieved.tzinfo is None:
            return False, f"web source {source['url']} has no timezone on its retrieval time"
        age = (moment - retrieved).total_seconds() / 60.0
        if age < -5:
            return False, f"web source {source['url']} claims to have been read in the future"
        ages.append((age, source["url"]))
    age, url = max(ages)
    if age > max_age_minutes:
        return False, (f"web evidence is {age:.0f} min old, past the {max_age_minutes:.0f} min "
                       f"window for this strategy ({url})")
    return True, (f"{len(sources)} web source(s), the oldest read {age:.0f} min ago "
                  f"(limit {max_age_minutes:.0f} min)")


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
