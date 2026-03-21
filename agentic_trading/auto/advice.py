"""A model's suggestion for a plan's strategy. A suggestion, and nothing more than that.

The model is asked one thing: given the envelope a person set and the bounds the risk profile
leaves, which strategy parameters would it choose, and why, in a sentence. It is handed no tool
and no order surface, and what it returns is a ``plan.Strategy`` -- which ``plan.clamp`` then
holds to the bounds before anything reads it. A figure outside them does not become the plan
with a warning attached; it becomes the bound, and the plan records that it did.

A model that cannot be reached is not an error for the plan: the template stands in, and the
plan says the suggestion was unavailable and why. Whether to ask at all is the caller's choice,
because asking spends the same subscription the research runs on.
"""

from __future__ import annotations

import json
from typing import Any

from ..llm.claude_code import ClaudeCodeChatModel
from ..profile import Profiles
from . import modes
from .plan import Bounds, Envelope, Strategy

SYSTEM = (
    "You suggest parameters for a small, conservative, long-only automatic trading plan in "
    "US-listed ETFs. You are given the envelope the account owner set, the bounds each parameter "
    "must stay inside, and what the account could actually order. Choose values inside the "
    "bounds. You cannot change the envelope, the bounds or any risk limit, and anything you "
    "return outside the bounds is replaced by the bound. Prefer fewer, larger positions when "
    "the capital is small, because a position below the minimum order cannot be placed. Do not "
    "promise returns and do not describe confidence as a probability of profit. Give the reason "
    "for your choice in one or two short sentences, in Chinese (rationale_zh) and in English "
    "(rationale_en). You have no tools; use only what is in the prompt."
)


def brief(envelope: Envelope, bounds: Bounds, facts: dict[str, Any]) -> str:
    """What the model is told. Kept as its own function so a test can read exactly that."""
    mode = modes.TABLE[envelope.mode]
    return json.dumps({
        "mode": {"key": mode.key, "cadence": mode.cadence, "signal": mode.signal,
                 "holding": mode.holding[1]},
        "envelope": envelope.as_dict(),
        "bounds": bounds.as_dict(),
        "account": facts.get("account"),
        "as_configured_today": facts.get("today"),
        "under_the_template": facts.get("under_plan"),
        "template": facts.get("template"),
    }, ensure_ascii=False, indent=2, default=str)


def suggest(profiles: Profiles, envelope: Envelope, bounds: Bounds, facts: dict[str, Any], *,
            llm: Any | None = None) -> Strategy:
    """Ask the model for a strategy. Raises what the adapter raises; the caller decides."""
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
    messages = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": brief(envelope, bounds, facts)}]
    return llm.with_structured_output(Strategy).invoke(messages)
