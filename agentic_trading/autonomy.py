"""Deterministic gate for reducing a position without asking first.

Autonomy here does exactly one thing: it lets deterministic code give the approval a person
would otherwise give, for a risk-reducing action and for nothing else. Everything downstream is
unchanged — the proposal is approved, revalidated, re-run through the Hard Risk Layer, reserved,
submitted and reconciled by the same code as a human-approved one. This module cannot widen a
limit; it can only decide which of the two allowed ways an order may start.

Three things shape it, and each is a deliberate answer to a question that has a tempting wrong
answer.

**A refusal here is not a refusal to trade.** It means the proposal waits for a person, which
is what happens to everything today. So the bar can be set high without costing anything: the
failure mode of refusing is a delay, and the failure mode of granting wrongly is an order.

**Only ``REDUCE`` qualifies, never ``SELL``.** The engine already gives those two precise
meanings — REDUCE is half the holding, SELL is all of it — so "at most half, and closing a
position needs a person" falls straight out of the action, with no arithmetic of our own to get
wrong.

**The drawdown ladder is not required to be NORMAL.** That condition exists in the config for
autonomous *opening*, where an escalating ladder is a reason to stop. Applying it to a reduction
would withdraw the ability to cut risk at precisely the moment the ladder says risk should be
cut. The same reasoning runs through the event blackouts, which never gate a sell.

A trip is sticky and clears only by hand, mirroring ``LadderState.manual_reset_required``. It
fires on the narrow family of anomalies that mean *we do not know what state we are in* — a
venue error, an unresolved order, a ledger that will not reconcile. It deliberately does not
fire on an ordinary refusal or a closed market, because a gate that withdraws itself every
Saturday is a gate nobody can rely on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .profile import Profiles
from .state_db import StateDB

#: The one action deterministic code may approve. SELL closes a position outright and always
#: belongs to a person; the neutral and opening actions are not autonomy's business at all.
AUTONOMOUS_ACTION = "REDUCE"

STATE_DOCUMENT = "autonomy.json"

#: Outcomes that mean the world is not as we believe it to be. Anything else — a rejection, a
#: closed market, a stale proposal — is an ordinary "not now" and leaves autonomy armed.
TRIPPING_OUTCOMES = frozenset({
    "SUBMIT_UNCERTAIN", "RECONCILIATION_REQUIRED", "BROKER_UNAVAILABLE", "BROKER_REJECTED",
    "RESERVATION_FAILED", "PREFLIGHT_FAILED", "STATE_CONFIGURATION_ERROR", "BROKER_REVIEW_ALERT",
})


@dataclass
class Decision:
    permitted: bool
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"permitted": self.permitted, "reasons": list(self.reasons)}


@dataclass
class Downgrade:
    """Why autonomy was withdrawn, and the fact that only a person can restore it."""

    tripped: bool = False
    reason: str | None = None
    at: str | None = None
    history: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        # Bounded: the first trip explains the state, and recent entries explain how it got
        # here. An unbounded list in a single document grows without anyone ever reading it.
        return {"tripped": self.tripped, "reason": self.reason, "at": self.at,
                "history": self.history[:1] + self.history[-19:] if len(self.history) > 20
                else list(self.history)}


def _cfg(profiles: Profiles, bucket: str) -> dict[str, Any]:
    return (profiles.risk_cfg("buckets", bucket, "autonomy", "reduce", default={}) or {})


def is_armed(profiles: Profiles, bucket: str) -> bool:
    """Whether anything could happen: the switch is on and no trip is standing.

    Both halves, deliberately. A boolean that answers only "is the switch on" is a footgun in
    a caller that then opens a venue session.
    """
    return _cfg(profiles, bucket).get("enabled") is True and not load_downgrade(profiles).tripped


def state_path(profiles: Profiles) -> Path:
    """Beside the conceptual risk state, so autonomy shares its SQLite database."""
    configured = Path(str((profiles.risk.get("portfolio_state") or {}).get(
        "risk_state_file", "state/risk_state.json")))
    base = configured if configured.is_absolute() else profiles.project_dir / configured
    return base.with_name(STATE_DOCUMENT)


def load_downgrade(profiles: Profiles) -> Downgrade:
    path = state_path(profiles)
    stored = StateDB(path).read_document(path)
    if not isinstance(stored, dict):
        return Downgrade()
    return Downgrade(tripped=bool(stored.get("tripped", False)), reason=stored.get("reason"),
                     at=stored.get("at"), history=list(stored.get("history") or []))


def _save(profiles: Profiles, state: Downgrade) -> Downgrade:
    path = state_path(profiles)
    StateDB(path).write_document(path, state.as_dict())
    return state


def trip(profiles: Profiles, reason: str) -> Downgrade:
    """Withdraw autonomy until a person restores it. The first reason is the one kept."""
    state = load_downgrade(profiles)
    now = datetime.now(UTC).isoformat()
    state.history.append({"at": now, "event": "tripped", "reason": reason})
    if not state.tripped:
        state.tripped, state.reason, state.at = True, reason, now
    return _save(profiles, state)


def reset(profiles: Profiles, note: str) -> Downgrade:
    """Restore autonomy. The history is kept, so a reset is never invisible."""
    state = load_downgrade(profiles)
    state.history.append({"at": datetime.now(UTC).isoformat(), "event": "reset", "note": note,
                          "cleared": state.reason})
    state.tripped, state.reason, state.at = False, None, None
    return _save(profiles, state)


def trip_for_outcome(profiles: Profiles, bucket: str, outcome: str, detail: str = "") -> bool:
    """Withdraw autonomy if an attempt ended in a state we cannot account for."""
    if outcome not in TRIPPING_OUTCOMES:
        return False
    trip(profiles, f"an autonomous reduction in {bucket} ended in {outcome}"
                   + (f": {detail}" if detail else ""))
    return True


def permits(profiles: Profiles, *, bucket: str, action: str, confidence: int | None,
            held_quantity: float | None, requested_quantity: float | None,
            reduced_today: int, now: datetime | None = None) -> Decision:
    """Whether deterministic code may give this reduction its approval.

    Every refusal names the condition, because the caller records it and the next reader has to
    be able to tell "autonomy declined" from "the trade was refused".
    """
    reasons: list[str] = []

    def refuse(reason: str) -> Decision:
        reasons.append(reason)
        return Decision(False, reasons)

    cfg = _cfg(profiles, bucket)
    if cfg.get("enabled") is not True:
        return refuse(f"buckets.{bucket}.autonomy.reduce.enabled is not true")
    if action != AUTONOMOUS_ACTION:
        return refuse(f"{action} is not autonomous; only {AUTONOMOUS_ACTION} is, because closing "
                      "a position outright belongs to a person")

    downgrade = load_downgrade(profiles)
    if downgrade.tripped:
        return refuse(f"autonomy was withdrawn and needs a manual reset: {downgrade.reason}")

    floor = cfg.get("min_confidence")
    if floor is None:
        return refuse(f"buckets.{bucket}.autonomy.reduce declares no min_confidence")
    # bool is a subclass of int, so `min_confidence: true` would otherwise read as 1.
    if isinstance(confidence, bool) or not isinstance(confidence, int):
        return refuse(f"the proposal carries no usable confidence ({confidence!r})")
    if int(confidence) < int(floor):
        return refuse(f"confidence {confidence} is below the autonomous floor of {int(floor)}; "
                      "a person decides this one")

    per_day = cfg.get("max_positions_reduced_per_day")
    if per_day is None:
        return refuse(f"buckets.{bucket}.autonomy.reduce declares no "
                      "max_positions_reduced_per_day")
    if reduced_today >= int(per_day):
        return refuse(f"{reduced_today} position(s) already reduced autonomously today, at the "
                      f"limit of {int(per_day)}")

    # Belt and braces over the engine's own REDUCE semantics: even if a caller hands over a
    # quantity of its own, it may not be the whole holding.
    if (held_quantity is not None and requested_quantity is not None
            and requested_quantity > held_quantity / 2.0 + 1e-9):
        return refuse(f"{requested_quantity:g} of {held_quantity:g} shares is more than half "
                      "the holding; a larger reduction belongs to a person")

    reasons.append(f"reduction of at most half a holding, confidence {confidence} at or above "
                   f"{int(floor)}, {reduced_today} of {int(per_day)} used today")
    return Decision(True, reasons)


def reduced_today(proposals, bucket: str, now: datetime | None = None) -> int:
    """How many positions this bucket has already had reduced autonomously in the last 24h.

    Counted from the approvals themselves rather than from a counter, so a restart, a crash or
    a second process cannot lose the count.
    """
    moment = now or datetime.now(UTC)
    seen: set[str] = set()
    for p in proposals:
        if p.bucket != bucket or not str(p.approved_by or "").startswith("autonomy:"):
            continue
        approved = p.approved_at
        if not approved:
            continue
        try:
            when = datetime.fromisoformat(approved)
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        if moment - when <= timedelta(days=1):
            seen.add(p.ticker.upper())
    return len(seen)
