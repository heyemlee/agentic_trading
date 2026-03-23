"""Decision records: one JSON file per analysis that links every artifact needed
to answer later "why did the system recommend this, what did it see, what did
the risk layer do, what happened next".

Upstream already writes the per-agent report tree and the full-state log; the
record references those files instead of duplicating them.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import UPSTREAM_TRADINGAGENTS_COMMIT, __version__

RECORD_SCHEMA_VERSION = 1


def _data_sources(profiles: Any, llm_calls: list[dict[str, Any]],
                  source_readings: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """What the config says this system reads, beside what this run actually called.

    The two halves exist because they used to disagree invisibly: upstream's sentiment analyst
    binds no tools, so its StockTwits and Reddit reads never appear in ``llm_calls`` and never
    appeared anywhere else either. Declaring them makes the gap between declared and observed
    readable in every record instead of requiring someone to know the upstream source.

    ``research_sources`` is the third thing, for the sources this project watches itself
    (agentic_trading.sources): each read as it happened -- how it ended, what it covered, and
    for every item its link, the time the source gives and the time it was first seen here.
    A run that read none has an empty list, which is not the same as a list of failures.
    """
    from . import sources
    from .evidence import web_sources_from_calls

    tool_calls = [c for call in (llm_calls or ()) for c in (call.get("tool_calls") or ())]
    fetches, searched = web_sources_from_calls(tool_calls)
    observed = sorted({str(c.get("tool")) for c in tool_calls if c.get("tool")})
    web = (profiles.risk.get("research_web_access") or {})
    read = list(source_readings or [])
    settings = sources.load(profiles)
    return {
        "declared": profiles.investor.get("declared_data_sources") or [],
        "observed_tools": observed,
        "research_sources": {
            "switched_on": sorted(name for name, on in ((sources.REDDIT, settings.reddit.enabled),
                                                        (sources.FORM4, settings.form4.enabled)) if on),
            "summary": sources.summary(read),
            "readings": read,
        },
        "web_access": {
            "fetch_enabled": ((web.get("fetch") or {}).get("enabled")) is True,
            "search_enabled": ((web.get("search") or {}).get("enabled")) is True,
            "fetches": fetches,
            # True when the CLI's own web tools ran. There is deliberately no provenance list
            # here: that route runs inside the CLI and nothing in this codebase saw the URLs.
            "cli_web_search_used": searched,
        },
    }


def build_record(
    *,
    run_id: str,
    ticker: str,
    instrument_type: str,
    bucket: str,
    trade_date: str,
    profiles: Any,
    analysts: list[str],
    pm_signal: str,
    final_state: dict[str, Any],
    policy: Any,
    report_path: Path | None,
    state_log_path: Path | None,
    llm_usage: dict[str, Any],
    llm_calls: list[dict[str, Any]],
    elapsed_seconds: float,
    error: str | None = None,
    hard_risk: dict[str, Any] | None = None,
    final_recommendation: dict[str, Any] | None = None,
    portfolio: dict[str, Any] | None = None,
    source_readings: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    debate = final_state.get("investment_debate_state") or {}
    return {
        "schema_version": RECORD_SCHEMA_VERSION,
        "run_id": run_id,
        "created_at": datetime.now(UTC).isoformat(),
        "package_version": __version__,
        "upstream_tradingagents_commit": UPSTREAM_TRADINGAGENTS_COMMIT,
        "ticker": ticker,
        "instrument_type": instrument_type,
        "bucket": bucket,
        "trade_date": trade_date,
        "stage": profiles.stage,
        "execution_mode": profiles.bucket_modes.get(bucket, "recommend_only"),
        "orders_placed": False,
        "analysts": analysts,
        "models": {
            "deep": profiles.tradingagents.deep_think_llm,
            "quick": profiles.tradingagents.quick_think_llm,
            "billing": "claude_subscription_via_claude_code_cli",
        },
        "upstream_decision": {
            "portfolio_manager_signal": pm_signal,
            "research_manager_plan": final_state.get("investment_plan"),
            "trader_proposal": final_state.get("trader_investment_plan"),
            "portfolio_manager_decision": final_state.get("final_trade_decision"),
            "bull_case_excerpt": (debate.get("bull_history") or "")[:2000],
            "bear_case_excerpt": (debate.get("bear_history") or "")[:2000],
        },
        "policy": {
            "final_action": policy.final_action,
            "size_hint": policy.size_hint,
            "confidence": policy.confidence,
            "reviewer_action": policy.llm_action,
            "reasons": policy.reasons,
            # The same account as codes (agentic_trading.diagnosis); absent on a policy object
            # from before they existed, which is what a record reads as too.
            "reason_codes": list(getattr(policy, "reason_codes", None) or []),
            "proposal": policy.proposal,
            "error": policy.error,
        },
        "hard_risk": hard_risk or {"status": "not_evaluated"},
        "final_recommendation": final_recommendation,
        "portfolio": portfolio,
        "artifacts": {
            "report_tree": str(report_path.parent) if report_path else None,
            "complete_report": str(report_path) if report_path else None,
            "upstream_state_log": str(state_log_path) if state_log_path else None,
        },
        "llm_usage": llm_usage,
        "llm_calls": llm_calls,
        "data_sources": _data_sources(profiles, llm_calls, source_readings),
        "elapsed_seconds": round(elapsed_seconds, 1),
        "unresolved_profile_fields": list(profiles.unresolved),
        "error": error,
        "outcome": None,
    }


def write_record(path: Path, record: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(record, fh, indent=2, default=str)
    return path
