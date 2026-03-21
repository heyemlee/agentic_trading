"""Conservative Policy step: translate the upstream Portfolio Manager decision into
the mandate's proposal format, deterministically.

Two parts, deliberately separated:

1. One structured LLM call fills the mandate's proposal fields (bull case, bear
   case, thesis breakers, missing information, confidence as an internal
   decision-strength indicator). It reuses the upstream ``bind_structured``
   pattern through the same Claude Code adapter.
2. Deterministic code decides the final action. The LLM's suggested action and
   its confidence can only make the outcome more cautious, never less: the
   action comes from the Portfolio Manager's five-tier rating, confidence
   floors from the risk profile gate any opening action, and REVIEW, missing
   data, or any failure maps to NO_TRADE.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field

from .llm.claude_code import ClaudeCodeChatModel, ClaudeCodeError
from .profile import Profiles

Action = Literal["BUY", "ADD", "HOLD", "REDUCE", "SELL", "NO_TRADE"]

# Upstream five-tier rating -> (action with an existing position, action without one)
RATING_TO_ACTION: dict[str, tuple[str, str]] = {
    "Buy": ("ADD", "BUY"),
    "Overweight": ("ADD", "BUY"),
    "Hold": ("HOLD", "NO_TRADE"),
    "Underweight": ("REDUCE", "NO_TRADE"),
    "Sell": ("SELL", "NO_TRADE"),
}


class ConservativeProposal(BaseModel):
    """The mandate's trade-proposal format (config/MANDATE.md), filled by the reviewer."""

    ticker: str
    recommended_action: Action = Field(description="Your recommendation. NO_TRADE is expected when evidence is weak.")
    confidence: int = Field(ge=0, le=100, description="Internal decision-strength indicator, 0-100. Not a calibrated probability. Lower it whenever information is missing, stale, or contradictory.")
    market_risk_level: Literal["LOW_RISK", "NORMAL", "ELEVATED_RISK", "HIGH_RISK"]
    investment_thesis: str
    bull_case: str
    bear_case: str
    primary_risks: list[str]
    what_could_make_this_thesis_wrong: str
    entry_logic: str
    exit_logic: str
    expected_holding_period: str
    maximum_acceptable_loss: str
    reason_this_improves_the_portfolio: str
    alternative_why_doing_nothing_may_be_preferable: str
    missing_information: list[str] = Field(description="Material information that was unavailable, stale, or contradictory. Empty only if nothing material was missing.")
    data_quality: Literal["good", "partial", "poor"]


@dataclass
class PolicyDecision:
    final_action: str
    size_hint: str
    pm_rating: str
    llm_action: str | None
    confidence: int | None
    reasons: list[str] = field(default_factory=list)
    proposal: dict[str, Any] | None = None
    error: str | None = None
    #: Why the mapping ended in NO_TRADE, as codes from agentic_trading.diagnosis with the
    #: figures each was decided on. ``reasons`` is the same account in a sentence, kept for the
    #: log and the record; nothing reads a sentence to learn what was decided.
    reason_codes: list[dict[str, Any]] = field(default_factory=list)


def deterministic_action(
    pm_rating: str,
    *,
    confidence: int | None,
    llm_action: str | None,
    bucket: str,
    instrument_type: str,
    profiles: Profiles,
    has_position: bool = False,
) -> tuple[str, str, list[str]]:
    """Return (final_action, size_hint, reasons). Only ever more cautious than the PM."""
    action, size_hint, reasons, _codes = decide(
        pm_rating, confidence=confidence, llm_action=llm_action, bucket=bucket,
        instrument_type=instrument_type, profiles=profiles, has_position=has_position)
    return action, size_hint, reasons


def decide(
    pm_rating: str,
    *,
    confidence: int | None,
    llm_action: str | None,
    bucket: str,
    instrument_type: str,
    profiles: Profiles,
    has_position: bool = False,
) -> tuple[str, str, list[str], list[dict[str, Any]]]:
    """``deterministic_action``, with the reason for a NO_TRADE as a code beside its sentence."""
    reasons: list[str] = []
    codes: list[dict[str, Any]] = []

    def stop(code: str, **facts: Any) -> tuple[str, str, list[str], list[dict[str, Any]]]:
        codes.append({"code": code, **facts})
        return "NO_TRADE", "none", reasons, codes

    if pm_rating not in RATING_TO_ACTION:
        reasons.append(f"portfolio manager rating {pm_rating!r} is not tradeable -> NO_TRADE")
        return stop("rating_not_tradeable")

    with_pos, without_pos = RATING_TO_ACTION[pm_rating]
    action = with_pos if has_position else without_pos
    if not has_position and with_pos != without_pos:
        reasons.append(f"no existing position: rating {pm_rating} -> {action}")

    size_hint = "none"
    if action in ("BUY", "ADD"):
        if confidence is None:
            reasons.append("policy reviewer produced no confidence -> NO_TRADE")
            return stop("no_confidence")
        cfg = profiles.confidence
        if confidence < cfg.no_trade_below:
            reasons.append(f"confidence {confidence} below no-trade threshold {cfg.no_trade_below} -> NO_TRADE")
            return stop("confidence_below_no_trade", confidence=confidence,
                        threshold=cfg.no_trade_below)
        if instrument_type == "crypto" and bucket == "core":
            reasons.append("crypto is never a core instrument -> NO_TRADE")
            return stop("instrument_not_permitted")
        normal_floor = cfg.floor(bucket, instrument_type, "normal")
        small_floor = cfg.floor(bucket, instrument_type, "small")
        if confidence >= normal_floor:
            size_hint = "normal" if pm_rating == "Buy" else "small"
        elif confidence >= small_floor:
            size_hint = "small"
            reasons.append(f"confidence {confidence} below full-size floor {normal_floor} -> small position only")
        else:
            reasons.append(f"confidence {confidence} below opening floor {small_floor} for {bucket} {instrument_type} -> NO_TRADE")
            return stop("confidence_below_floor", confidence=confidence, floor=small_floor)
        if llm_action in ("NO_TRADE", "HOLD", "REDUCE", "SELL"):
            reasons.append(f"policy reviewer recommended {llm_action} despite PM {pm_rating} -> NO_TRADE (most cautious view wins)")
            return stop("reviewer_cautious", reviewer=llm_action)
    elif llm_action and llm_action != action:
        reasons.append(f"policy reviewer suggested {llm_action}; deterministic mapping keeps {action}")
    if action == "NO_TRADE":
        # Reached only with nothing held: a rating that says hold, trim or sell has nothing to
        # act on. Which of the two it was is the whole of what a person wants to know.
        codes.append({"code": "rating_hold_no_position"} if pm_rating == "Hold"
                     else {"code": "rating_bearish_no_position", "rating": pm_rating})
    return action, size_hint, reasons, codes


def render_state_for_policy(final_state: dict[str, Any]) -> str:
    debate = final_state.get("investment_debate_state") or {}
    risk = final_state.get("risk_debate_state") or {}
    sections = [
        ("Market analyst report", final_state.get("market_report")),
        ("Sentiment analyst report", final_state.get("sentiment_report")),
        ("News analyst report", final_state.get("news_report")),
        ("Fundamentals analyst report", final_state.get("fundamentals_report")),
        ("Bull versus bear debate", debate.get("history")),
        ("Research manager plan", final_state.get("investment_plan")),
        ("Trader proposal", final_state.get("trader_investment_plan")),
        ("Risk debate", risk.get("history")),
        ("Portfolio manager decision", final_state.get("final_trade_decision")),
    ]
    out = []
    for title, body in sections:
        body = (body or "").strip()
        out.append(f"## {title}\n\n{body if body else '(not produced in this run)'}")
    return "\n\n".join(out)


def run_policy(
    profiles: Profiles,
    final_state: dict[str, Any],
    *,
    ticker: str,
    instrument_type: str,
    bucket: str,
    pm_rating: str,
    trade_date: str,
    has_position: bool = False,
    portfolio_text: str = "",
    llm: ClaudeCodeChatModel | None = None,
) -> PolicyDecision:
    llm = llm or ClaudeCodeChatModel(
        model=profiles.tradingagents.deep_think_llm,
        effort=profiles.llm.policy_effort,
        timeout_seconds=profiles.llm.timeout_seconds,
        max_turns=profiles.llm.max_turns,
        claude_bin=profiles.llm.claude_bin,
        work_dir=str(profiles.project_dir / "runs" / ".claude_workdir"),
        config_dir=str(profiles.config_dir),
        role="policy",
    )
    system = (
        "You are the Conservative Policy reviewer for a personal investment committee. "
        "You read the complete output of a multi-agent research workflow and fill the mandate's "
        "trade-proposal format. You do not have tools and must use only the evidence in the prompt. "
        "Missing, stale, or contradictory information must be listed and must lower your confidence. "
        "Confidence is an internal decision-strength indicator, never a probability, and never overrides "
        "the deterministic risk rules applied after you.\n\n"
        "MANDATE:\n\n" + profiles.mandate_full()
    )
    user = (
        f"Ticker: {ticker}\nInstrument type: {instrument_type}\nBucket: {bucket}\n"
        f"Analysis date: {trade_date}\nExisting position: {'yes' if has_position else 'no'}\n"
        f"Portfolio manager rating (upstream, five-tier): {pm_rating}\n\n"
        + (f"{portfolio_text}\n\n" if portfolio_text else "")
        + "Research workflow output follows.\n\n" + render_state_for_policy(final_state)
    )
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]

    proposal: ConservativeProposal | None = None
    error: str | None = None
    try:
        proposal = llm.with_structured_output(ConservativeProposal).invoke(messages)
    except ClaudeCodeError as exc:
        # Neither says anything about the idea: the subscription ran out, or this Mac's login
        # lapsed. Recording either as a NO_TRADE decision would file "nobody could ask the
        # reviewer" under "the reviewer said no", so the run stops and says which it was.
        if exc.rate_limited or exc.login_expired:
            raise
        error = f"policy reviewer call failed: {exc}"
    except Exception as exc:  # noqa: BLE001 - fail closed, never crash the run
        error = f"policy reviewer call failed: {type(exc).__name__}: {exc}"

    confidence = proposal.confidence if proposal else None
    llm_action = proposal.recommended_action if proposal else None
    action, size_hint, reasons, codes = decide(
        pm_rating, confidence=confidence, llm_action=llm_action, bucket=bucket,
        instrument_type=instrument_type, profiles=profiles, has_position=has_position,
    )
    if error:
        reasons.insert(0, error + " -> NO_TRADE")
        action, size_hint = "NO_TRADE", "none"
        codes = [{"code": "policy_review_failed"}]
    return PolicyDecision(
        final_action=action, size_hint=size_hint, pm_rating=pm_rating, llm_action=llm_action,
        confidence=confidence, reasons=reasons,
        proposal=proposal.model_dump() if proposal else None, error=error, reason_codes=codes,
    )
