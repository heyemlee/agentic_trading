"""Deterministic gate for acting without asking first.

Autonomy here does exactly one thing: it lets deterministic code give the approval a person
would otherwise give. Everything downstream is
unchanged — the proposal is approved, revalidated, re-run through the Hard Risk Layer, reserved,
submitted and reconciled by the same code as a human-approved one. This module cannot widen a
limit; it can only decide which of the two allowed ways an order may start.

Three things shape it, and each is a deliberate answer to a question that has a tempting wrong
answer.

**A refusal here is not a refusal to trade.** It means the proposal waits for a person, which
is what happens to everything today. So the bar can be set high without costing anything: the
failure mode of refusing is a delay, and the failure mode of granting wrongly is an order.

**Reducing and opening are separately switched, and reducing came first.** They are different
in kind: one cuts risk and one creates it, and the worst case of getting them wrong is not the
same. Opening additionally names which instrument classes it may buy at all, and an empty list
is not permission to buy anything.

**Only ``REDUCE`` reduces autonomously, never ``SELL``.** The engine already gives those two precise
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

#: The actions that create risk rather than cut it. Separately switched, because they are
#: different in kind and the worst case of getting them wrong is not the same.
OPENING_ACTIONS = ("BUY", "ADD")

STATE_DOCUMENT = "autonomy.json"

#: Outcomes that mean the world is not as we believe it to be. Anything else — a rejection, a
#: closed market, a stale proposal — is an ordinary "not now" and leaves autonomy armed.
TRIPPING_OUTCOMES = frozenset({
    "SUBMIT_UNCERTAIN", "RECONCILIATION_REQUIRED", "BROKER_UNAVAILABLE", "BROKER_REJECTED",
    "RESERVATION_FAILED", "PREFLIGHT_FAILED", "STATE_CONFIGURATION_ERROR", "BROKER_REVIEW_ALERT",
    "EXECUTION_FAILED", "PRE_SUBMIT_REFUSED",
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


def _cfg(profiles: Profiles, bucket: str, block: str = "reduce") -> dict[str, Any]:
    return (profiles.risk_cfg("buckets", bucket, "autonomy", block, default={}) or {})


def is_armed(profiles: Profiles, bucket: str) -> bool:
    """Whether anything could happen: some switch is on and no trip is standing.

    Both halves, deliberately. A boolean that answers only "is a switch on" is a footgun in a
    caller that then opens a venue session.
    """
    switched_on = any(_cfg(profiles, bucket, block).get("enabled") is True
                      for block in ("reduce", "open"))
    return switched_on and not load_downgrade(profiles).tripped


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
    trip(profiles, f"an autonomous action in {bucket} ended in {outcome}"
                   + (f": {detail}" if detail else ""))
    return True


def permits(profiles: Profiles, *, bucket: str, action: str, confidence: int | None,
            limit_class: str | None = None, held_quantity: float | None = None,
            requested_quantity: float | None = None, reduced_today: int = 0,
            opened_today: int = 0, now: datetime | None = None) -> Decision:
    """Whether deterministic code may give this proposal its approval.

    Two blocks, separately switched, because they are different in kind: reducing cuts risk and
    opening creates it. Every refusal names the condition, because the caller records it and the
    next reader has to be able to tell "autonomy declined" from "the trade was refused".
    """
    reasons: list[str] = []

    def refuse(reason: str) -> Decision:
        reasons.append(reason)
        return Decision(False, reasons)

    if action == AUTONOMOUS_ACTION:
        block = "reduce"
    elif action in OPENING_ACTIONS:
        block = "open"
    else:
        return refuse(f"{action} is never autonomous; closing a position outright and doing "
                      "nothing both belong to a person")

    cfg = _cfg(profiles, bucket, block)
    if cfg.get("enabled") is not True:
        return refuse(f"buckets.{bucket}.autonomy.{block}.enabled is not true")

    downgrade = load_downgrade(profiles)
    if downgrade.tripped:
        return refuse(f"autonomy was withdrawn and needs a manual reset: {downgrade.reason}")

    floor = cfg.get("min_confidence")
    if floor is None:
        return refuse(f"buckets.{bucket}.autonomy.{block} declares no min_confidence")
    # bool is a subclass of int, so `min_confidence: true` would otherwise read as 1.
    if isinstance(confidence, bool) or not isinstance(confidence, int):
        return refuse(f"the proposal carries no usable confidence ({confidence!r})")
    if int(confidence) < int(floor):
        return refuse(f"confidence {confidence} is below the autonomous floor of {int(floor)}; "
                      "a person decides this one")

    if block == "reduce":
        per_day = cfg.get("max_positions_reduced_per_day")
        if per_day is None:
            return refuse(f"buckets.{bucket}.autonomy.reduce declares no "
                          "max_positions_reduced_per_day")
        if reduced_today >= int(per_day):
            return refuse(f"{reduced_today} position(s) already reduced autonomously today, at "
                          f"the limit of {int(per_day)}")
        # Belt and braces over the engine's own REDUCE semantics: even if a caller hands over a
        # quantity of its own, it may not be the whole holding.
        if (held_quantity is not None and requested_quantity is not None
                and requested_quantity > held_quantity / 2.0 + 1e-9):
            return refuse(f"{requested_quantity:g} of {held_quantity:g} shares is more than half "
                          "the holding; a larger reduction belongs to a person")
        reasons.append(f"reduction of at most half a holding, confidence {confidence} at or "
                       f"above {int(floor)}, {reduced_today} of {int(per_day)} used today")
        return Decision(True, reasons)

    # Opening. The share of the account this may reach is not checked here — HR23 measures it
    # against live positions and their provenance, which this function does not have. What is
    # checked here is what may be bought at all, and how often.
    allowed = cfg.get("classes")
    if not isinstance(allowed, list) or not allowed:
        return refuse(f"buckets.{bucket}.autonomy.open declares no classes; an empty list of "
                      "what may be bought is not permission to buy anything")
    if limit_class not in allowed:
        return refuse(f"{limit_class} is not one of the classes code may open on its own "
                      f"({', '.join(str(c) for c in allowed)})")
    per_day = cfg.get("max_new_positions_per_day")
    if per_day is None:
        return refuse(f"buckets.{bucket}.autonomy.open declares no max_new_positions_per_day")
    if opened_today >= int(per_day):
        return refuse(f"{opened_today} position(s) already opened autonomously today, at the "
                      f"limit of {int(per_day)}")
    reasons.append(f"{limit_class} is openable without a person, confidence {confidence} at or "
                   f"above {int(floor)}, {opened_today} of {int(per_day)} used today")
    return Decision(True, reasons)


def acted_today(proposals, bucket: str, actions, now: datetime | None = None) -> int:
    """How many distinct positions code has acted on by itself in this bucket in the last 24h.

    Counted from the approval records themselves rather than from a counter, so a restart, a
    crash or a second process cannot lose the count.
    """
    moment = now or datetime.now(UTC)
    wanted = set(actions)
    seen: set[str] = set()
    for p in proposals:
        if p.bucket != bucket or not str(p.approved_by or "").startswith("autonomy:"):
            continue
        if p.action not in wanted:
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


def reduced_today(proposals, bucket: str, now: datetime | None = None) -> int:
    return acted_today(proposals, bucket, (AUTONOMOUS_ACTION,), now)


def opened_today(proposals, bucket: str, now: datetime | None = None) -> int:
    return acted_today(proposals, bucket, OPENING_ACTIONS, now)


def main(argv: list[str] | None = None) -> int:
    """``ct-autonomy``: inspect what autonomy may do, stop it, or restore it."""
    import argparse
    import json

    from .env import load_env
    from .profile import load_profiles

    load_env()
    # `stop` is why this is a command rather than a console button: it halts autonomous action
    # in one gesture without editing a live trading config, which is the last thing anyone
    # should be doing in a hurry. `resume` is deliberately separate and deliberately manual —
    # the sticky flag exists so that whatever caused it gets looked at first.
    parser = argparse.ArgumentParser(
        prog="ct-autonomy",
        description="Inspect, stop or restore autonomous reductions.")
    parser.add_argument("action", choices=("status", "stop", "resume"))
    parser.add_argument("--config-dir", default="config")
    parser.add_argument("--bucket", default="micro_live")
    parser.add_argument("--note", default="", help="why, recorded with the change")
    args = parser.parse_args(argv)

    profiles = load_profiles(args.config_dir)
    if args.action == "stop":
        if not args.note:
            parser.error("--note is required: the reason is the useful half of the record")
        state = trip(profiles, f"stopped by hand: {args.note}")
    elif args.action == "resume":
        if not args.note:
            parser.error("--note is required: say what was looked into before restoring")
        state = reset(profiles, args.note)
    else:
        state = load_downgrade(profiles)

    cfg = _cfg(profiles, args.bucket)
    print(json.dumps({
        "bucket": args.bucket,
        "action_permitted": AUTONOMOUS_ACTION,
        "enabled": cfg.get("enabled") is True,
        "armed": is_armed(profiles, args.bucket),
        "limits": {k: cfg.get(k) for k in ("min_confidence", "max_positions_reduced_per_day")},
        "withdrawn": state.as_dict(),
        "state_file": str(state_path(profiles)),
    }, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
