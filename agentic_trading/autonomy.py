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
            opened_today: int = 0, now: datetime | None = None,
            floor: PositionFloor | None = None) -> Decision:
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

    confidence_floor = cfg.get("min_confidence")
    if confidence_floor is None:
        return refuse(f"buckets.{bucket}.autonomy.{block} declares no min_confidence")
    # bool is a subclass of int, so `min_confidence: true` would otherwise read as 1.
    if isinstance(confidence, bool) or not isinstance(confidence, int):
        return refuse(f"the proposal carries no usable confidence ({confidence!r})")
    if int(confidence) < int(confidence_floor):
        return refuse(f"confidence {confidence} is below the autonomous floor of {int(confidence_floor)}; "
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
        # The floor under what a person bought. Halving is bounded per order and per day, and
        # neither stops code from walking the same position down over a week; this does.
        pct = cfg.get("min_remaining_pct_of_opened")
        if pct is None:
            return refuse(f"buckets.{bucket}.autonomy.reduce declares no "
                          "min_remaining_pct_of_opened")
        # A baseline is not yet a floor: the book has to be readable for the subtraction to
        # mean anything. Half a floor refuses, it does not become zero.
        if floor is None or floor.high_water is None or floor.floor is None or floor.headroom is None:
            return refuse("no floor could be derived for this position: "
                          + str((floor.reason if floor else None) or "nothing was derived"))
        headroom = floor.headroom
        if headroom <= QUANTITY_TOLERANCE:
            return refuse(f"the position is at its floor of {floor.floor:g} shares, {float(pct):g}% "
                          f"of the {floor.high_water:g} a person last held; going below it "
                          "belongs to a person")
        if requested_quantity is not None and requested_quantity > headroom + QUANTITY_TOLERANCE:
            return refuse(f"{requested_quantity:g} shares would take the position below its floor "
                          f"of {floor.floor:g}; at most {headroom:g} may be reduced without a person")
        reasons.append(f"reduction of at most half a holding and at most {headroom:g} shares above "
                       f"the {floor.floor:g} floor, confidence {confidence} at or above "
                       f"{int(confidence_floor)}, {reduced_today} of {int(per_day)} used today")
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
                   f"above {int(confidence_floor)}, {opened_today} of {int(per_day)} used today")
    return Decision(True, reasons)


#: What may appear in ``Proposal.approved_via``. A person's approval comes through the console
#: or the CLI; deterministic code's comes through the autonomous pass.
HUMAN_VIA = ("console", "cli")
AUTONOMOUS_VIA = "autonomy"


def was_autonomous(proposal) -> bool:
    """Whether deterministic code approved this, read from the record's own field.

    A record written before ``approved_via`` existed has no field, and the honest fallback is
    the old prefix — but only because of which way it errs here. This answer feeds rate limits
    and the blackout that applies to autonomous action, so counting an unattributable record as
    autonomous can only restrict code further. :func:`was_human` faces the opposite direction
    and therefore takes the opposite fallback.
    """
    via = getattr(proposal, "approved_via", None)
    if via:
        return via == AUTONOMOUS_VIA
    return str(getattr(proposal, "approved_by", None) or "").startswith("autonomy:")


def was_human(proposal) -> bool:
    """Whether a person approved this, read from the record's own field and nothing else.

    No fallback: a record that cannot be attributed to a person is not evidence of one. The
    position floor autonomous reductions may not cross is measured from what a person bought,
    so a record we cannot attribute must never raise it.
    """
    return getattr(proposal, "approved_via", None) in HUMAN_VIA


# ---------------------------------------------------------------------- the position floor
#
# Halving a holding is bounded per order and per day, and neither bound stops code from walking
# the same position down over a week until nothing is left. What stops that is a floor under the
# quantity a *person* bought, which code may never cross: getting below it is a decision only a
# person makes, for as long as the position lives.
#
# The floor is derived from the records rather than counted into a document, for the reason
# acted_today gives: a restart, a crash or a second process cannot lose a derivation. It is
# derived from the fills, because a fill is the only evidence that shares actually moved.

#: How many fill records a derivation may read. Past this the log cannot be replayed from its
#: beginning in one request, and a baseline that might be missing the first purchase is no
#: baseline: the answer is refused rather than guessed.
FILL_REPLAY_LIMIT = 5000

#: Quantities agree within the venue's own precision.
QUANTITY_TOLERANCE = 1e-6


@dataclass
class PositionFloor:
    """The quantity autonomous reductions may not take a position below, and how it was derived.

    ``high_water`` is None whenever the derivation could not establish one, and ``reason`` then
    says which of the ways it failed. None is not zero: a floor of zero would mean code could
    sell everything, which is the opposite of what a missing baseline should allow.
    """

    high_water: float | None = None
    high_water_at: str | None = None
    high_water_via: str | None = None
    cycle_started_at: str | None = None
    current: float | None = None
    floor: float | None = None
    headroom: float | None = None
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"position_high_water_quantity": self.high_water,
                "high_water_set_at": self.high_water_at,
                "high_water_source": self.high_water_via,
                "cycle_started_at": self.cycle_started_at,
                "current_quantity": self.current,
                "floor_quantity": self.floor,
                "headroom_quantity": self.headroom}


def position_floor(*, fills: list[dict[str, Any]], proposals, bucket: str, symbol: str,
                   ledger_quantity: float | None, pct: float | None) -> PositionFloor:
    """The floor under one position, replayed from the fills that built it.

    The fills are read oldest first and netted. Every purchase a person approved raises a high
    water mark, and the mark only ever rises while the position lives; a sale that takes the
    netted quantity to zero ends that life, and the next purchase starts a new one with a mark
    of its own. So a person buying one share into a position code has already reduced to its
    floor moves the mark not at all, and code regains exactly that one share rather than a
    fresh quarter of the position.

    Three ways this refuses rather than answers, and each is a case where a number could be
    produced but should not be trusted:

    * the log is longer than can be replayed, so the first purchase may be out of sight;
    * the netted quantity does not match the book, so there are shares the log cannot explain
      and a mark derived from the log would be a mark for a different position;
    * no purchase in this position's life was approved by a person, so there is nothing a floor
      could be measured from.

    A zero that ends a life is one the fills themselves record. A venue that answers zero, or a
    reading too old to use, cannot end one: nothing would stop a bad read from clearing the mark
    and handing code the position.
    """
    symbol = str(symbol).upper()
    if len(fills) >= FILL_REPLAY_LIMIT:
        return PositionFloor(reason=f"more than {FILL_REPLAY_LIMIT} fills on record; the log "
                                    "cannot be replayed from the position's first purchase")
    by_proposal = {p.proposal_id: p for p in proposals}
    rows = [row for row in sorted(fills, key=lambda r: str(r.get("at") or ""))
            if str(row.get("bucket")) == bucket and str(row.get("symbol") or "").upper() == symbol]
    running = 0.0
    high = high_at = high_via = cycle_at = None
    for row in rows:
        side = str(row.get("side") or "").lower()
        quantity = float(row.get("quantity") or 0.0)
        if side == "buy":
            if running <= QUANTITY_TOLERANCE:
                cycle_at = str(row.get("at") or "") or None
            running += quantity
        elif side == "sell":
            running -= quantity
        else:
            return PositionFloor(reason=f"a fill records an unrecognised side {side!r}; the "
                                        "position cannot be replayed")
        if running <= QUANTITY_TOLERANCE:
            # The position ended here. Whatever was bought before this belongs to a life that
            # is over and cannot bound what code may do to the next one.
            running, high, high_at, high_via, cycle_at = 0.0, None, None, None, None
            continue
        approver = by_proposal.get(str(row.get("proposal_id")))
        if side == "buy" and was_human(approver) and (high is None or running > high):
            high, high_at = running, str(row.get("at") or "") or None
            high_via = getattr(approver, "approved_via", None)
    if ledger_quantity is None:
        return PositionFloor(high_water=high, high_water_at=high_at, high_water_via=high_via,
                             cycle_started_at=cycle_at,
                             reason="the book could not be read, so the replay cannot be checked "
                                    "against it")
    if abs(running - float(ledger_quantity)) > QUANTITY_TOLERANCE:
        return PositionFloor(current=float(ledger_quantity), cycle_started_at=cycle_at,
                             reason=f"the fills account for {running:g} shares and the book holds "
                                    f"{float(ledger_quantity):g}; there are shares the log cannot "
                                    "explain, so it cannot say what a person bought")
    if high is None:
        return PositionFloor(current=float(ledger_quantity), cycle_started_at=cycle_at,
                             reason="no purchase in this position's life was approved by a person")
    if pct is None:
        return PositionFloor(high_water=high, high_water_at=high_at, high_water_via=high_via,
                             cycle_started_at=cycle_at, current=float(ledger_quantity),
                             reason="no min_remaining_pct_of_opened is declared")
    floor = high * float(pct) / 100.0
    return PositionFloor(high_water=high, high_water_at=high_at, high_water_via=high_via,
                         cycle_started_at=cycle_at, current=float(ledger_quantity), floor=floor,
                         headroom=max(0.0, float(ledger_quantity) - floor))


def position_floor_for(profiles: Profiles, *, fills: list[dict[str, Any]], proposals, bucket: str,
                       symbol: str, ledger_quantity: float | None) -> PositionFloor:
    """``position_floor`` with the percentage read from the bucket's own configuration.

    The percentage lives in one place and is read in one place. A caller that had to fetch it
    itself would be a second reader to keep in step with the block's key name.
    """
    return position_floor(fills=fills, proposals=proposals, bucket=bucket, symbol=symbol,
                         ledger_quantity=ledger_quantity,
                         pct=_cfg(profiles, bucket, "reduce").get("min_remaining_pct_of_opened"))


def reduce_block(profiles: Profiles, bucket: str, floor: PositionFloor) -> dict[str, Any]:
    """What code may do to this one position, for the exit view to show beside the account's own.

    Only the reasons that are about this position or about the reduce block's own configuration.
    The kill switch, the real-money switch, preflight and a ladder halt stop everyone, and live
    in ``/config/authority.actions.REDUCE`` where a screen reads them once for the account
    rather than once per position.
    """
    cfg = _cfg(profiles, bucket, "reduce")
    pct = cfg.get("min_remaining_pct_of_opened")
    blocked: list[dict[str, str]] = []
    if cfg.get("enabled") is not True:
        blocked.append({"code": "not_enabled",
                        "detail": f"buckets.{bucket}.autonomy.reduce.enabled is not true"})
    if cfg.get("min_confidence") is None:
        blocked.append({"code": "no_min_confidence",
                        "detail": f"buckets.{bucket}.autonomy.reduce declares no min_confidence"})
    if pct is None:
        blocked.append({"code": "no_min_remaining",
                        "detail": f"buckets.{bucket}.autonomy.reduce declares no "
                                  "min_remaining_pct_of_opened; code may not reduce a position "
                                  "whose floor nobody has declared"})
    withdrawn = load_downgrade(profiles)
    if withdrawn.tripped:
        blocked.append({"code": "withdrawn",
                        "detail": f"autonomy was withdrawn and needs a manual reset: {withdrawn.reason}"})
    if floor.high_water is None or (pct is not None and floor.headroom is None):
        blocked.append({"code": "no_opened_baseline",
                        "detail": floor.reason or "no baseline could be derived"})
    elif floor.headroom is not None and floor.headroom <= QUANTITY_TOLERANCE:
        blocked.append({"code": "below_floor",
                        "detail": f"the position is at its floor of {floor.floor:g} shares, "
                                  f"{float(pct):g}% of the {floor.high_water:g} a person last "
                                  "held; going below it belongs to a person"})
    return {"permitted": not blocked, "min_remaining_pct_of_opened": pct,
            **floor.as_dict(), "blocked_by": blocked}


def acted_today(proposals, bucket: str, actions, now: datetime | None = None) -> int:
    """How many distinct positions code has acted on by itself in this bucket in the last 24h.

    Counted from the approval records themselves rather than from a counter, so a restart, a
    crash or a second process cannot lose the count.
    """
    moment = now or datetime.now(UTC)
    wanted = set(actions)
    seen: set[str] = set()
    for p in proposals:
        if p.bucket != bucket or not was_autonomous(p):
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


# ---------------------------------------------------------------------------- effective authority
#
# Three places used to decide, each from its own slice of the configuration, whether an order
# needs a person: preflight PF04 reads ``micro_live_execution``, ``permits`` above reads
# ``buckets.<bucket>.autonomy``, and Hard Risk stamps ``requires_human_approval`` on a proposal.
# Nothing read all three at once, so the settings screen, the risk screen and the loop could each
# tell a different story. What follows computes one object from all of them, deterministically
# and without the venue, and every reader takes that object rather than its own slice.
#
# The object carries a version: a hash of the inputs it was computed from. Research records the
# version it was submitted under, a proposal records the one it was born under, and an approval
# compares both against the version of the moment, so an authority that was tightened after the
# research is never acted on as if it were still in force, and one that was loosened is at least
# looked at by a person first.

#: The state-database document that remembers every version the authority has passed through.
AUTHORITY_VERSIONS = "authority_versions"
MAX_VERSIONS = 50

#: How an authority changed between two versions. A person must acknowledge the last three.
TIGHTENED, LOOSENED, MIXED, UNRELATED, UNKNOWN_CHANGE = (
    "tightened", "loosened", "mixed", "unrelated", "unknown")
NEEDS_ACKNOWLEDGEMENT = frozenset({LOOSENED, MIXED, UNKNOWN_CHANGE})

#: Reasons an action cannot be taken by anyone, a person included.
HUMAN_BLOCKS = ("kill_switch", "real_money_disabled", "mode_recommend_only", "stage", "preflight",
                "ladder_halt", "config_error")
#: Reasons only deterministic code may not take it. A person still can.
AUTONOMOUS_BLOCKS = ("not_enabled", "no_min_confidence", "no_classes", "withdrawn",
                     "never_autonomous", "pending_change", "authority_changed")

ACTIONS = ("BUY", "ADD", "REDUCE", "SELL")


def halt_file(profiles: Profiles) -> Path:
    """Where the kill switch lives. One resolution, shared by every reader of it."""
    return profiles.project_dir / str(
        profiles.risk_cfg("execution_policy", "kill_switch", "halt_file", default="state/HALT"))


def kill_switch_state(profiles: Profiles) -> dict[str, Any]:
    """Whether the kill switch is engaged, since when, and why, from the halt file itself."""
    path = halt_file(profiles)
    flagged = bool(profiles.risk_cfg("execution_policy", "kill_switch", "trading_halted",
                                     default=False))
    if not path.exists():
        return {"engaged": flagged, "since": None,
                "reason": "execution_policy.kill_switch.trading_halted is true" if flagged else None}
    since, reason = None, None
    try:
        first = path.read_text(encoding="utf-8").splitlines()[0] if path.stat().st_size else ""
        stamp, _, rest = first.partition(" ")
        since, reason = (stamp or None), (rest.strip() or None)
    except (OSError, IndexError):
        pass
    return {"engaged": True, "since": since, "reason": reason}


def authority_inputs(profiles: Profiles, db) -> dict[str, Any]:
    """Everything the authority is computed from that can change without a release.

    Kept small and cheap on purpose: the two configuration files' revisions, the stage they
    declare, the withdrawal flag, the kill switch and the set of changes still cooling. No venue
    read is among them, so the version is the same whether or not the broker answers.
    """
    from . import config_changes as cc

    revisions = {}
    for name in ("risk_profile.yaml", "investor_profile.yaml"):
        try:
            revisions[name] = cc.revision((profiles.config_dir / name).read_text(encoding="utf-8"))
        except OSError:
            revisions[name] = None
    downgrade = load_downgrade(profiles)
    pending = sorted(r["id"] for r in cc.records(db) if r.get("status") == cc.PENDING)
    return {
        "risk_profile": revisions["risk_profile.yaml"],
        "investor_profile": revisions["investor_profile.yaml"],
        "stage": int(profiles.stage),
        "autonomy_json": {"tripped": downgrade.tripped, "reason": downgrade.reason, "at": downgrade.at},
        "halt_file": kill_switch_state(profiles),
        "pending_changes": pending,
    }


def version_of(inputs: dict[str, Any]) -> str:
    import hashlib
    import json

    blob = json.dumps(inputs, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def current_version(profiles: Profiles, db) -> str:
    """The authority version of this moment, for a caller that needs only the version."""
    return version_of(authority_inputs(profiles, db))


def version_history(db) -> list[dict[str, Any]]:
    return list((db.read_meta(AUTHORITY_VERSIONS, {}) or {}).get("versions") or [])


def note_version(profiles: Profiles, db) -> str:
    """The version of this moment, written into the history so a later reader can name it.

    Every place that stamps a version on something -- a research job, a proposal, an approval
    -- goes through here, so the history always holds the versions those records refer to.
    """
    inputs = authority_inputs(profiles, db)
    version = version_of(inputs)
    record_version(db, inputs, version)
    return version


def record_version(db, inputs: dict[str, Any], version: str, *,
                   now: datetime | None = None) -> list[dict[str, Any]]:
    """Append this version to the history if it is new, saying what moved and which way.

    ``paths`` and ``classification`` come from the console's own change records where one
    applied since the previous version; a configuration edited any other way is recorded as
    such and classed as unknown, which an approval then treats as something to look at.
    """
    from . import config_changes as cc

    with db.transaction() as conn:
        record = db.read_meta(AUTHORITY_VERSIONS, {}) or {}
        history = list(record.get("versions") or [])
        if history and history[-1].get("version") == version:
            return history
        previous = history[-1] if history else None
        entry = {"version": version, "at": (now or datetime.now(UTC)).isoformat(),
                 "inputs": inputs, "changed_inputs": [], "paths": [], "classification": UNRELATED}
        if previous is not None:
            entry.update(_transition(previous.get("inputs") or {}, inputs,
                                     cc.records(db), since=str(previous.get("at") or "")))
        history.append(entry)
        record["versions"] = history[-MAX_VERSIONS:]
        db.write_meta(AUTHORITY_VERSIONS, record, conn)
        return record["versions"]


def _transition(before: dict[str, Any], after: dict[str, Any], records: list[dict[str, Any]],
                *, since: str) -> dict[str, Any]:
    changed = [key for key in ("risk_profile", "investor_profile", "stage", "autonomy_json",
                               "halt_file", "pending_changes") if before.get(key) != after.get(key)]
    effects: set[str] = set()
    paths: list[str] = []
    if "halt_file" in changed:
        effects.add(TIGHTENED if (after.get("halt_file") or {}).get("engaged") else LOOSENED)
        paths.append("kill_switch")
    if "autonomy_json" in changed:
        effects.add(TIGHTENED if (after.get("autonomy_json") or {}).get("tripped") else LOOSENED)
        paths.append("autonomy.withdrawn")
    if "pending_changes" in changed:
        effects.add(UNRELATED)
        paths.append("pending_changes")
    for key, file in (("risk_profile", "risk_profile.yaml"), ("investor_profile", "investor_profile.yaml")):
        if key not in changed:
            continue
        applied = [r for r in records if r.get("file") == file and r.get("status") == "applied"
                   and str(r.get("applied_at") or "") > since]
        if not applied:
            effects.add(UNKNOWN_CHANGE)
            paths.append(f"{file}: (edited outside the console)")
        for item in applied:
            paths.extend(str(p) for p in item.get("paths") or [])
            seen = {str(c.get("effect")) for c in item.get("changes") or []}
            if "loosening" in seen:
                effects.add(LOOSENED)
            if "tightening" in seen:
                effects.add(TIGHTENED)
            if "unknown" in seen:
                effects.add(UNKNOWN_CHANGE)
            if not seen & {"loosening", "tightening", "unknown"}:
                effects.add(UNRELATED)
    return {"changed_inputs": changed, "paths": paths, "classification": _aggregate(effects)}


def _aggregate(effects: set[str]) -> str:
    if UNKNOWN_CHANGE in effects:
        return UNKNOWN_CHANGE
    if TIGHTENED in effects and LOOSENED in effects:
        return MIXED
    if LOOSENED in effects:
        return LOOSENED
    if TIGHTENED in effects:
        return TIGHTENED
    return UNRELATED


def authority_change(history: list[dict[str, Any]], from_version: str | None,
                     to_version: str) -> dict[str, Any] | None:
    """What changed between the version something was made under and the version now.

    ``None`` when nothing did. A version the history does not hold, or none recorded at all,
    is an unknown change: the honest answer, and the one that asks a person to look.
    """
    if from_version == to_version:
        return None
    versions = [entry.get("version") for entry in history]
    if to_version in versions:
        # The same inputs hash to the same version, so a version can recur -- a kill switch
        # engaged and released lands back where it started. The steps that matter run from the
        # latest time the record stood at `from` to the latest time it stood at `to`.
        end = len(versions) - 1 - versions[::-1].index(to_version)
        earlier = [i for i, v in enumerate(versions[:end]) if v == from_version]
        if earlier:
            start = earlier[-1]
            steps = history[start + 1:end + 1]
            paths: list[str] = []
            for step in steps:
                paths.extend(p for p in step.get("paths") or [] if p not in paths)
            return {"from": from_version, "to": to_version, "at": steps[-1].get("at"),
                    "paths": paths,
                    "classification": _aggregate({str(s.get("classification")) for s in steps}),
                    "detail": None}
    return {"from": from_version, "to": to_version,
            "at": history[-1].get("at") if history else None, "paths": [],
            "classification": UNKNOWN_CHANGE,
            "detail": ("authority version not recorded at proposal time" if from_version is None
                       else "the recorded authority version is not in the history")}


def effective_authority(profiles: Profiles, *, inputs: dict[str, Any], version: str,
                        history: list[dict[str, Any]], proposals, preflight: Any | None,
                        preflight_stale: bool, ladder_halted: bool,
                        pending_changes: list[dict[str, Any]],
                        now: datetime | None = None) -> dict[str, Any]:
    """The one object every reader of "what may happen to the real-money account" takes.

    Deterministic, and computed without the venue: ``preflight`` is whatever reading the caller
    already holds, or None. A block that stops a person stops code too, so every human block is
    inherited by the autonomous side; the autonomous side then adds its own.
    """
    bucket = "micro_live"
    micro = profiles.risk.get("micro_live_execution") or {}
    stage_modes = profiles.risk_cfg("execution_policy", "stage_allowed_modes", default={}) or {}
    stage_allows = list(stage_modes.get(profiles.stage) or stage_modes.get(str(profiles.stage)) or [])
    bucket_mode = profiles.bucket_modes.get(bucket, "recommend_only")
    kill = inputs["halt_file"]
    withdrawn = inputs["autonomy_json"]
    real_money = bool(micro.get("enabled", False))

    human: list[dict[str, str]] = []
    if not real_money:
        human.append({"code": "real_money_disabled",
                      "detail": "micro_live_execution.enabled is false: no real-money order can be placed"})
    if kill.get("engaged"):
        human.append({"code": "kill_switch",
                      "detail": "the kill switch is engaged" + (f": {kill['reason']}" if kill.get("reason") else "")})
    if bucket_mode == "recommend_only":
        human.append({"code": "mode_recommend_only",
                      "detail": f"execution_policy.buckets.{bucket}.execution_mode is recommend_only"})
    if "human_approval" not in stage_allows:
        human.append({"code": "stage",
                      "detail": f"stage {profiles.stage} allows {stage_allows or 'nothing'}; human_approval needs stage 3"})
    if ladder_halted:
        human.append({"code": "ladder_halt",
                      "detail": f"the {bucket} drawdown ladder requires a manual reset"})
    if preflight is not None and not preflight.ready:
        failing = ", ".join(c.check_id for c in preflight.failures) or "unknown gate"
        human.append({"code": "preflight",
                      "detail": f"{failing} failing" + (" as of the last venue reading; reading is stale"
                                                        if preflight_stale else "")})

    def touching(prefix: str) -> list[dict[str, str]]:
        out = []
        for change in pending_changes:
            if any(str(p).startswith(prefix) for p in change.get("paths") or []):
                out.append({"code": "pending_change",
                            "detail": f"{change['id']} applies at {change.get('apply_after')}; "
                                      "withdrawable until applied"})
        return out

    reduced = reduced_today(proposals, bucket, now)
    opened = opened_today(proposals, bucket, now)
    actions: dict[str, Any] = {}
    for action in ACTIONS:
        own: list[dict[str, str]] = []
        limits: dict[str, Any]
        if action in OPENING_ACTIONS:
            cfg = _cfg(profiles, bucket, "open")
            if cfg.get("enabled") is not True:
                own.append({"code": "not_enabled", "detail": f"buckets.{bucket}.autonomy.open.enabled is not true"})
            if not isinstance(cfg.get("classes"), list) or not cfg.get("classes"):
                own.append({"code": "no_classes", "detail": f"buckets.{bucket}.autonomy.open declares no classes"})
            if cfg.get("min_confidence") is None:
                own.append({"code": "no_min_confidence",
                            "detail": f"buckets.{bucket}.autonomy.open declares no min_confidence"})
            own.extend(touching(f"buckets.{bucket}.autonomy.open"))
            limits = {"classes": list(cfg.get("classes") or []),
                      "max_pct_of_account": cfg.get("max_pct_of_account"),
                      "max_new_positions_per_day": cfg.get("max_new_positions_per_day"),
                      "min_confidence": cfg.get("min_confidence"), "used_today": opened}
        elif action == AUTONOMOUS_ACTION:
            cfg = _cfg(profiles, bucket, "reduce")
            if cfg.get("enabled") is not True:
                own.append({"code": "not_enabled", "detail": f"buckets.{bucket}.autonomy.reduce.enabled is not true"})
            if cfg.get("min_confidence") is None:
                own.append({"code": "no_min_confidence",
                            "detail": f"buckets.{bucket}.autonomy.reduce declares no min_confidence"})
            if cfg.get("min_remaining_pct_of_opened") is None:
                own.append({"code": "no_min_remaining",
                            "detail": f"buckets.{bucket}.autonomy.reduce declares no "
                                      "min_remaining_pct_of_opened; code may not reduce a "
                                      "position whose floor nobody has declared"})
            own.extend(touching(f"buckets.{bucket}.autonomy.reduce"))
            limits = {"min_confidence": cfg.get("min_confidence"),
                      "max_positions_reduced_per_day": cfg.get("max_positions_reduced_per_day"),
                      "max_fraction_of_holding": 0.5,
                      # The rule only. What it comes to for one position needs a ticker, so the
                      # numbers are in that proposal's exit.autonomous block.
                      "min_remaining_pct_of_opened": cfg.get("min_remaining_pct_of_opened"),
                      "min_remaining_basis": "position_high_water_quantity",
                      "used_today": reduced}
        else:
            own.append({"code": "never_autonomous",
                        "detail": "SELL is never autonomous; closing a position outright belongs to a person"})
            limits = {"quantity_basis": "full ledger holding (HR14, HR14b)",
                      "sell_quantity_validated_against": "ledger"}
        if withdrawn.get("tripped") and action != "SELL":
            own.append({"code": "withdrawn",
                        "detail": f"autonomy was withdrawn and needs a manual reset: {withdrawn.get('reason')}"})
        if "autonomous" not in stage_allows and action != "SELL":
            own.append({"code": "stage",
                        "detail": f"stage {profiles.stage} does not allow autonomous execution"})
        blocking_own = [b for b in own if b["code"] != "pending_change"]
        autonomous_ok = not human and not blocking_own
        human_ok = not human
        actions[action] = {
            "mode": "autonomous" if autonomous_ok else ("human_approval" if human_ok else "blocked"),
            "autonomous": {"permitted": autonomous_ok, "blocked_by": [*human, *own]},
            "human_approval": {"permitted": human_ok, "blocked_by": list(human)},
            "limits": limits,
        }

    consistency = _consistency(profiles, bucket, micro, stage_allows)
    return {
        "checked_at": (now or datetime.now(UTC)).isoformat(),
        "version": version,
        "history": [{k: v for k, v in entry.items() if k != "inputs"} for entry in history[-10:]],
        "config_revisions": {"risk_profile.yaml": inputs.get("risk_profile"),
                             "investor_profile.yaml": inputs.get("investor_profile")},
        "account": {"scope": "agentic", "account_id": bucket, "venue": "robinhood", "is_real_money": True},
        "stage": profiles.stage, "stage_allows": stage_allows,
        "real_money_enabled": real_money,
        "execution_mode": str(micro.get("execution_mode", "human_approval")),
        "kill_switch": dict(kill),
        "withdrawn": dict(withdrawn),
        "actions": actions,
        "limits": _account_limits(profiles, bucket, micro),
        "pending_changes": list(pending_changes),
        "preflight": {"ready": preflight.ready if preflight is not None else None,
                      "blocking": [c.check_id for c in preflight.failures] if preflight is not None else [],
                      "checked_at": None, "stale": preflight_stale},
        "consistency": consistency,
    }


def _consistency(profiles: Profiles, bucket: str, micro: dict[str, Any],
                 stage_allows: list[str]) -> list[dict[str, str]]:
    """Where the three readers of "does this need a person" can disagree, as data."""
    out: list[dict[str, str]] = []
    opening = _cfg(profiles, bucket, "open")
    reducing = _cfg(profiles, bucket, "reduce")
    if opening and opening.get("min_confidence") is None:
        out.append({"code": "open_without_min_confidence",
                    "detail": f"buckets.{bucket}.autonomy.open has no min_confidence; PF04 does not read autonomy.open.enabled",
                    "effect": "enabling autonomy.open would pass preflight and still be refused by permits()"})
    pf04 = (str(micro.get("execution_mode", "human_approval")) == "human_approval"
            and not bool(micro.get("allow_autonomous_opening", False)))
    if opening.get("enabled") is True and pf04:
        out.append({"code": "open_enabled_unseen_by_preflight",
                    "detail": f"buckets.{bucket}.autonomy.open.enabled is true while PF04 passes on micro_live_execution alone",
                    "effect": "preflight reports human approval for opening while code may open on its own"})
    if any(block.get("enabled") is True for block in (opening, reducing)) and "autonomous" not in stage_allows:
        out.append({"code": "autonomy_not_gated_by_stage",
                    "detail": f"buckets.{bucket}.autonomy is switched on at stage {profiles.stage}, which allows {stage_allows}",
                    "effect": "permits() does not read execution_policy.stage; the switch alone decides"})
    if profiles.risk_cfg("execution_policy", "autonomy", default=None):
        out.append({"code": "execution_policy_autonomy_unread",
                    "detail": "execution_policy.autonomy.* is read by no code; only buckets.<bucket>.autonomy is",
                    "effect": "its switches change nothing"})
    return out


def _account_limits(profiles: Profiles, bucket: str, micro: dict[str, Any]) -> dict[str, Any]:
    cfg = profiles.risk_cfg
    tiers = {}
    for name, tier in (cfg("buckets", bucket, "tiers", default={}) or {}).items():
        tiers[name] = {"classes": list(tier.get("classes") or []),
                       "max_pct_of_account": tier.get("max_pct_of_account"),
                       "max_order_usd": tier.get("max_order_usd")}
    floors = profiles.confidence.minimum_to_open
    freshness_name = str(micro.get("freshness_class", (profiles.risk.get("proposal_freshness") or {})
                                   .get("default_class", "core_conservative")))
    freshness = ((profiles.risk.get("proposal_freshness") or {}).get("classes") or {}).get(freshness_name) or {}
    earnings = profiles.risk.get("event_rules", {}).get("earnings") or {}
    macro = profiles.risk.get("event_rules", {}).get("macro_blackout_autonomous_only") or {}
    return {
        "capital": {"max_capital_usd": micro.get("max_capital_usd"),
                    "tolerance_usd": micro.get("capital_tolerance_usd"),
                    "sizing_basis": cfg("buckets", bucket, "sizing_basis", default="bucket_allocation")},
        "tiers": tiers,
        "position_limits": {
            "max_single_position_pct": cfg("buckets", bucket, "position_limits",
                                           "max_single_position_pct_of_allocation", default=None),
            "max_open_positions": cfg("buckets", bucket, "position_limits", "max_open_positions", default=None)},
        "class_caps_pct": dict(cfg("buckets", bucket, "position_limits", "max_pct_of_allocation_by_class",
                                   default={}) or {}),
        "concentration": {
            "all_individual_stocks_pct": cfg("buckets", bucket, "concentration_pct_of_allocation",
                                             "all_individual_stocks_combined", default=None),
            "direct_sector_pct": cfg("buckets", bucket, "concentration_pct_of_allocation",
                                     "direct_sector_exposure", default=None),
            "max_etfs_per_exposure_group": cfg("buckets", bucket, "concentration_pct_of_allocation",
                                               "max_etfs_per_exposure_group", default=None)},
        "activity": {
            "max_new_positions_per_day": cfg("buckets", bucket, "activity_limits", "max_new_positions_per_day", default=None),
            "max_new_positions_per_week": cfg("buckets", bucket, "activity_limits", "max_new_positions_per_week", default=None),
            "max_orders_per_week": cfg("buckets", bucket, "activity_limits", "max_orders_per_week", default=None),
            "no_same_day_round_trips": cfg("buckets", bucket, "activity_limits", "no_same_day_round_trips", default=None)},
        "daily_loss_gate_pct": cfg("buckets", bucket, "loss_limits", "daily_loss_gate_pct_of_allocation", default=None),
        "confidence_floors": {
            "broad_etf_half_size": floors.get(f"{bucket}_broad_etf_half_size"),
            "broad_etf_full_size": floors.get(f"{bucket}_broad_etf_full_size"),
            "sector_factor_etf": floors.get(f"{bucket}_sector_factor_etf"),
            "individual_stock": floors.get(f"{bucket}_individual_stock"),
            "autonomous_any": floors.get("autonomous_any"),
            "no_trade_below": profiles.confidence.no_trade_below},
        "freshness_class": {"name": freshness_name, "ttl_minutes": freshness.get("ttl_minutes"),
                            "stale_after_minutes": freshness.get("stale_after_minutes"),
                            "max_price_drift_pct": freshness.get("max_price_drift_pct")},
        "permissions": dict(micro.get("permissions") or {}),
        "event_blackouts": {
            "macro": {"applies_to": "autonomous_opens_only",
                      "maintained_through": _calendar_horizon(profiles, macro)},
            "earnings": {"applies_to": str(earnings.get("applies_to", "every_buy")),
                         "days_before": cfg("event_rules", "earnings", "stock_no_new_position_trading_days_before", default=None),
                         "days_after_no_add": cfg("event_rules", "earnings", "stock_no_add_trading_days_after", default=None)}},
        "order_rules": {k: (micro.get("order_rules") or {}).get(k)
                        for k in ("order_type", "prefer_limit_in_whole_shares", "market_hours", "time_in_force")},
    }


def _calendar_horizon(profiles: Profiles, macro: dict[str, Any]) -> str | None:
    """How far the hand-kept macro calendar is good for, read from the file it names."""
    import yaml

    path = profiles.project_dir / str(macro.get("calendar_file", "config/event_calendar.yaml"))
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return None
    value = data.get("maintained_through")
    return str(value) if value is not None else None


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
        "limits": {k: cfg.get(k) for k in ("min_confidence", "max_positions_reduced_per_day",
                                           "min_remaining_pct_of_opened")},
        "withdrawn": state.as_dict(),
        "state_file": str(state_path(profiles)),
    }, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
