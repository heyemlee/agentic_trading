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
