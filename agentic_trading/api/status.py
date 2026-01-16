"""Product-level status vocabulary.

The engine has many internal states. A person looking at a screen needs a small number of
unambiguous answers: is there a recommendation, does it need me, is it still current, who
allowed or blocked it, did it execute. These enums are that vocabulary, and they are the only
状态 the frontend should ever branch on.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from ..proposals import (
    APPROVED,
    CANCELED,
    EXECUTED,
    EXPIRED,
    INVALIDATED,
    NEEDS_REANALYSIS,
    NO_ACTION,
    PARTIALLY_FILLED,
    PROPOSED,
    REJECTED,
    STALE,
    SUBMITTED,
    SUBMITTING,
    UNKNOWN,
    VALID,
    Proposal,
)


class SystemStatus(str, Enum):
    """What the system as a whole can do right now."""

    READY = "READY"                            # able to research and to accept approvals
    PAUSED = "PAUSED"                          # kill switch on: no orders, research still allowed
    HALTED = "HALTED"                          # a drawdown ladder halt; needs a manual reset
    CLAUDE_LIMIT_REACHED = "CLAUDE_LIMIT_REACHED"
    BROKER_UNAVAILABLE = "BROKER_UNAVAILABLE"
    CONFIG_ERROR = "CONFIG_ERROR"


class MicroLiveStatus(str, Enum):
    """Where the real-money workflow stands. These are the states a dashboard shows."""

    DISABLED = "DISABLED"                      # master switch off: no real order can be placed
    NOT_CONNECTED = "NOT_CONNECTED"            # no Robinhood session
    NOT_READY = "NOT_READY"                    # connected, but a preflight gate is failing
    READY = "READY"                            # every gate green; awaiting an opportunity
    WAITING_FOR_PROPOSAL = "WAITING_FOR_PROPOSAL"
    WAITING_FOR_APPROVAL = "WAITING_FOR_APPROVAL"
    STALE = "STALE"
    REANALYSIS_REQUIRED = "REANALYSIS_REQUIRED"
    HARD_RISK_REJECTED = "HARD_RISK_REJECTED"
    BROKER_REVIEW_REJECTED = "BROKER_REVIEW_REJECTED"
    EXECUTED = "EXECUTED"
    SUBMITTED = "SUBMITTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    UNKNOWN = "UNKNOWN"
    CANCELED = "CANCELED"


class ProposalStatus(str, Enum):
    """One proposal's product state."""

    SUBMITTED = "SUBMITTED"
    UNKNOWN = "UNKNOWN"
    CANCELED = "CANCELED"
    WAITING_FOR_APPROVAL = "WAITING_FOR_APPROVAL"
    APPROVED_AWAITING_EXECUTION = "APPROVED_AWAITING_EXECUTION"
    EXECUTED = "EXECUTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    STALE = "STALE"
    REANALYSIS_REQUIRED = "REANALYSIS_REQUIRED"
    INVALIDATED = "INVALIDATED"
    HARD_RISK_REJECTED = "HARD_RISK_REJECTED"
    BROKER_REVIEW_REJECTED = "BROKER_REVIEW_REJECTED"
    REJECTED = "REJECTED"
    NO_TRADE = "NO_TRADE"


# Execution outcomes -> the product state a frontend should show.
OUTCOME_TO_PROPOSAL_STATUS = {
    "SUBMITTED": ProposalStatus.SUBMITTED,
    "DUPLICATE_PREVENTED": ProposalStatus.UNKNOWN,  # a duplicate alone proves no fill
    "ALREADY_EXECUTED": ProposalStatus.EXECUTED,
    "STALE": ProposalStatus.STALE,
    "REANALYZE": ProposalStatus.REANALYSIS_REQUIRED,
    "INVALIDATED": ProposalStatus.INVALIDATED,
    "REJECTED": ProposalStatus.HARD_RISK_REJECTED,
    "BROKER_REJECTED": ProposalStatus.BROKER_REVIEW_REJECTED,
    "BROKER_REVIEW_ALERT": ProposalStatus.BROKER_REVIEW_REJECTED,
    "PREFLIGHT_FAILED": ProposalStatus.HARD_RISK_REJECTED,
    "REVIEW_FAILED": ProposalStatus.BROKER_REVIEW_REJECTED,
    "BROKER_UNAVAILABLE": ProposalStatus.INVALIDATED,
    "SUBMIT_UNCERTAIN": ProposalStatus.UNKNOWN,
    "RECONCILIATION_REQUIRED": ProposalStatus.UNKNOWN,
    "ATTEMPTS_EXHAUSTED": ProposalStatus.INVALIDATED,
}


def proposal_status(proposal: Proposal, profiles: Any) -> ProposalStatus:
    """Map a stored proposal onto the vocabulary above, freshness included."""
    if proposal.reconciliation_error or proposal.status in (SUBMITTING, UNKNOWN):
        return ProposalStatus.UNKNOWN
    if proposal.status == SUBMITTED:
        return ProposalStatus.SUBMITTED
    if proposal.status == PARTIALLY_FILLED:
        return ProposalStatus.PARTIALLY_FILLED
    if proposal.status == CANCELED:
        return ProposalStatus.CANCELED
    if proposal.status == EXECUTED:
        if proposal.order_status is not None:
            return ProposalStatus.EXECUTED  # authoritative terminal status, not price/notional inference
        filled = proposal.executed_notional_usd  # legacy display compatibility only
        if filled is not None and proposal.notional_usd and filled < proposal.notional_usd * 0.99:
            return ProposalStatus.PARTIALLY_FILLED
        return ProposalStatus.EXECUTED
    if proposal.status == NEEDS_REANALYSIS:
        return ProposalStatus.REANALYSIS_REQUIRED
    if proposal.status == INVALIDATED:
        return ProposalStatus.INVALIDATED
    if proposal.status == REJECTED:
        return ProposalStatus.REJECTED
    if proposal.status == NO_ACTION:
        return ProposalStatus.NO_TRADE
    freshness, _ = proposal.freshness(profiles)
    if proposal.status == EXPIRED or freshness == EXPIRED:
        return ProposalStatus.STALE
    if proposal.status == APPROVED:
        return ProposalStatus.APPROVED_AWAITING_EXECUTION
    if proposal.status == PROPOSED:
        return ProposalStatus.WAITING_FOR_APPROVAL
    return ProposalStatus.INVALIDATED


def micro_live_status(*, enabled: bool, connected: bool, preflight_ready: bool,
                      open_proposal: Proposal | None, profiles: Any) -> MicroLiveStatus:
    """The single headline state for the real-money workflow."""
    if not enabled:
        return MicroLiveStatus.DISABLED
    if not connected:
        return MicroLiveStatus.NOT_CONNECTED
    if open_proposal is not None:
        status = proposal_status(open_proposal, profiles)
        mapped = {
            ProposalStatus.WAITING_FOR_APPROVAL: MicroLiveStatus.WAITING_FOR_APPROVAL,
            ProposalStatus.APPROVED_AWAITING_EXECUTION: MicroLiveStatus.WAITING_FOR_APPROVAL,
            ProposalStatus.STALE: MicroLiveStatus.STALE,
            ProposalStatus.REANALYSIS_REQUIRED: MicroLiveStatus.REANALYSIS_REQUIRED,
            ProposalStatus.HARD_RISK_REJECTED: MicroLiveStatus.HARD_RISK_REJECTED,
            ProposalStatus.BROKER_REVIEW_REJECTED: MicroLiveStatus.BROKER_REVIEW_REJECTED,
            ProposalStatus.EXECUTED: MicroLiveStatus.EXECUTED,
            ProposalStatus.PARTIALLY_FILLED: MicroLiveStatus.PARTIALLY_FILLED,
            ProposalStatus.SUBMITTED: MicroLiveStatus.SUBMITTED,
            ProposalStatus.UNKNOWN: MicroLiveStatus.UNKNOWN,
            ProposalStatus.CANCELED: MicroLiveStatus.CANCELED,
        }.get(status)
        if mapped is not None:
            return mapped
    if not preflight_ready:
        return MicroLiveStatus.NOT_READY
    return MicroLiveStatus.WAITING_FOR_PROPOSAL


# Freshness is exposed separately so a screen can show a countdown next to a proposal.
FRESHNESS_LABELS = {VALID: "current", STALE: "ageing, revalidation required", EXPIRED: "expired"}
