"""Which upstream analyst a bound tool set belongs to.

Web access is granted per analyst, but upstream hardcodes each analyst's tool list inside its
node function and passes nothing else down, so the bound names are the only signal available
without modifying a pinned dependency.

Matching is exact set equality, deliberately. If an upstream bump adds or renames a tool, the
signature stops matching, ``analyst_role`` returns None, and no web access is granted at all —
a test asserts the signatures still match the installed pin, so the change is loud rather than
silent. Substring matching on a name like ``get_news`` would instead have quietly handed web
access to the personal assistant in ``advisor.py``, which binds an overlapping list.
"""

from __future__ import annotations

from collections.abc import Iterable

# Verified against the pinned TradingAgents commit: market_analyst.py, news_analyst.py and
# fundamentals_analyst.py each build this exact list. sentiment_analyst.py binds no tools at
# all (it pre-fetches and calls bind_structured), so it can never match and can never be
# granted web access through this path.
ANALYST_SIGNATURES: dict[frozenset[str], str] = {
    frozenset({"get_stock_data", "get_indicators", "get_verified_market_snapshot"}): "market",
    frozenset({"get_news", "get_global_news", "get_macro_indicators",
               "get_prediction_markets"}): "news",
    frozenset({"get_fundamentals", "get_balance_sheet", "get_cashflow",
               "get_income_statement"}): "fundamentals",
}


def analyst_role(tool_names: Iterable[str]) -> str | None:
    return ANALYST_SIGNATURES.get(frozenset(tool_names))
