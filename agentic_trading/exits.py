"""What a sell would actually do: how much can leave, what would remain, and what blocks it.

The contract is docs/SELL_CONTRACT_2026-09-19.md. Everything here is a pure function of state
that was already read: the ledger, the venue reading the console keeps between polls, the
proposal store. Nothing opens a session, nothing writes, and nothing here places an order --
``service/approvals.py`` remains the only module that can.

Two asymmetries run through all of it, and both exist because the wrong answer is not
symmetrical:

**What may be sold takes the smaller of the two books; what would remain takes the larger.**
Sizing an order off the larger would oversell, which cannot be undone. Reporting the remainder
off the smaller would claim a position was closed while shares sat in the other book. So the two
figures come from different sources on purpose, and each says which one it used.

**An answer that cannot be established is absent, never zero.** A venue that will not answer, a
reading old enough to be about a different world, a ledger nothing has confirmed: each of those
makes the remainder and "is it closed" unknown. A screen can say it cannot confirm. It cannot
say a position is closed on the strength of a number nobody read.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal
from typing import Any

from .proposals import (
    APPROVED,
    PENDING_EXECUTION,
    PROPOSED,
    UNKNOWN,
    Proposal,
    needs_reconciliation,
)

#: The actions that take risk off rather than on. Both are exits; SELL is the whole holding.
REDUCING = ("SELL", "REDUCE")

#: The smallest quantity the venue will trade. Robinhood accepts six decimal places, and
#: execution quantizes down to the same place before it submits.
MIN_INCREMENT = 0.000001

#: How old a venue reading may be and still count as a source. Past this it is reported with its
#: time and excluded from every figure, because a quantity read ten minutes ago cannot establish
#: what is there now. Matches the threshold the console already judges venue reads by.
VENUE_MAX_AGE_SECONDS = 180.0

#: Two figures agree when they differ by less than this. Six decimal places is the venue's own
#: precision, so anything smaller is not a disagreement about shares.
TOLERANCE = 1e-6

#: Where a figure came from. Sizing and the remainder use different ones on purpose.
BOTH, SMALLER, LARGER, LEDGER_ONLY, VENUE_ONLY, NONE, UNKNOWN_BASIS = (
    "both_sources_agree", "smaller_of_two", "larger_of_two", "ledger_only", "venue_only",
    "none", "unknown")

#: Why an exit, or the continuation of one, cannot proceed. A closed set: the console localises
#: these, so a new one is a change to the contract and not an implementation detail.
NOT_ATTEMPTED = "not_attempted"
ORDER_WORKING = "order_working"
RECONCILIATION_UNRESOLVED = "reconciliation_unresolved"
ACTIVE_ORDER = "active_order"
ALLOWANCE_SPENT = "allowance_spent"
POSITION_GONE = "position_gone"
QUANTITY_UNKNOWN = "quantity_unknown"
BELOW_PRECISION = "below_precision"
RESEARCH_EXPIRED = "research_expired"
#: A continuation of this chain is already waiting for a person. Added to the set the contract
#: settled, because two of its requirements meet here: one chain may hold one continuation, and
#: the to-do item must disappear once a continuation exists. Without it a continuation for less
#: than the whole remaining allowance would leave the chain looking continuable again.
CONTINUATION_WAITING = "continuation_waiting"


def floor_to_increment(quantity: float, increment: float = MIN_INCREMENT) -> float:
    """Round down to what the venue will accept. Down, so a figure never grows by rounding."""
    step = Decimal(str(increment))
    return float((Decimal(str(quantity)) / step).quantize(Decimal(1), rounding=ROUND_DOWN) * step)


@dataclass(frozen=True)
class Sources:
    """The two books' answers about one holding, and what is already committed against it.

    ``venue_quantity`` is reported even when the reading is too old to use, because a screen
    showing two figures and the time each was read is more use than a screen showing one.
    ``effective_venue`` is what the arithmetic may use, which is None once it is stale.
    """

    ledger_quantity: float | None
    venue_quantity: float | None
    venue_as_of: str | None = None
    venue_stale: bool | None = None
    committed_quantity: float = 0.0
    note: str | None = None

    @property
    def effective_venue(self) -> float | None:
        return None if self.venue_stale else self.venue_quantity

    @property
    def valid(self) -> list[float]:
        return [q for q in (self.ledger_quantity, self.effective_venue) if q is not None]

    @property
    def both(self) -> bool:
        return self.ledger_quantity is not None and self.effective_venue is not None

    @property
    def agrees(self) -> bool | None:
        if not self.both:
            return None
        return abs(self.ledger_quantity - self.effective_venue) <= TOLERANCE


def from_reading(*, ledger_quantity: float | None, reading: Any, symbol: str,
                 committed_quantity: float = 0.0, now: datetime | None = None) -> Sources:
    """Read one holding out of the ledger and the kept venue reading.

    A reading that could not fetch positions at all is not a book that holds nothing: the venue
    figure stays None and the note says which it was, so nothing downstream reads "no answer" as
    "no shares".
    """
    venue_quantity, venue_as_of, stale, note = None, None, None, None
    if reading is not None and getattr(reading, "connected", False):
        venue_as_of = getattr(reading, "checked_at", None)
        positions = getattr(reading, "positions", None)
        if positions is None:
            note = ("the venue's positions could not be read: "
                    + str(getattr(reading, "positions_error", None) or "no reason recorded"))
        else:
            venue_quantity = float(positions.get(symbol.upper(), 0.0))
            stale = _older_than(venue_as_of, VENUE_MAX_AGE_SECONDS, now)
            if stale:
                note = (f"the venue reading is older than {VENUE_MAX_AGE_SECONDS:.0f}s and is not "
                        "used for any figure")
    elif reading is not None:
        note = "the venue is not connected: " + str(getattr(reading, "connect_error", None) or "no session")
    sources = Sources(ledger_quantity=ledger_quantity, venue_quantity=venue_quantity,
                      venue_as_of=venue_as_of, venue_stale=stale,
                      committed_quantity=committed_quantity, note=note)
    if sources.agrees is False and not note:
        note = (f"the two sources disagree: the ledger holds {ledger_quantity:g} and the venue "
                f"{venue_quantity:g}; the smaller sizes the order and the larger says what would remain")
        sources = Sources(ledger_quantity=ledger_quantity, venue_quantity=venue_quantity,
                          venue_as_of=venue_as_of, venue_stale=stale,
                          committed_quantity=committed_quantity, note=note)
    return sources


def _older_than(stamp: str | None, seconds: float, now: datetime | None = None) -> bool:
    if not stamp:
        return True
    try:
        when = datetime.fromisoformat(str(stamp))
    except ValueError:
        return True
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return ((now or datetime.now(UTC)) - when).total_seconds() > seconds


def sellable(sources: Sources) -> dict[str, Any]:
    """How much of this holding may be sold now, and which book said so.

    The smaller of the two books, less whatever an order already working has committed. Smaller,
    because the difference between the books is a share this side cannot prove it owns, and an
    order for it would be an oversell.
    """
    valid = sources.valid
    if not valid:
        basis, quantity = NONE, None
    else:
        quantity = max(0.0, min(valid) - sources.committed_quantity)
        if sources.both:
            basis = BOTH if sources.agrees else SMALLER
        else:
            basis = LEDGER_ONLY if sources.ledger_quantity is not None else VENUE_ONLY
    return {"quantity": quantity, "basis": basis,
            "ledger_quantity": sources.ledger_quantity, "venue_quantity": sources.venue_quantity,
            "venue_as_of": sources.venue_as_of, "venue_stale": sources.venue_stale,
            "committed_quantity": sources.committed_quantity, "agrees": sources.agrees,
            "note": sources.note}


def expectations(sources: Sources, *, authorised: float | None,
                 increment: float = MIN_INCREMENT) -> dict[str, Any]:
    """What this order would submit and what it would leave, each from its own source.

    The remainder and "does this close the position" require both books. With one, an order can
    still be estimated -- and is labelled as an estimate -- but nothing may be said about what is
    left, because the book nobody read is exactly where the remainder would be hiding.
    """
    book = sellable(sources)
    quantity = book["quantity"]
    submit = None
    if quantity is not None and authorised is not None:
        submit = floor_to_increment(min(float(authorised), quantity), increment)
    remaining, basis, closes = None, UNKNOWN_BASIS, None
    if sources.both and submit is not None:
        # Rounded to the venue's own precision, not floored: floating point turns 0.3 - 0.2 into
        # 0.09999999999999998, and flooring a remainder would understate what is left, which is
        # the one direction this figure must not err in.
        remaining = max(0.0, round(max(sources.valid) - submit, 6))
        basis = BOTH if sources.agrees else LARGER
        closes = remaining <= TOLERANCE
    return {"expected_submit_quantity": submit,
            # An estimate exactly when fewer than two books answered. Two books that disagree
            # still give a definite order -- the smaller of them -- so that is not an estimate.
            "expected_is_estimate": book["basis"] not in (BOTH, SMALLER),
            "expected_remaining": remaining, "expected_remaining_basis": basis,
            "expected_closes_position": closes}


def confirmation(sources: Sources) -> dict[str, Any]:
    """What is left after an attempt, and whether the position is closed, from a fresh reading.

    Three states, and the third is the point: a position is closed only when both books were
    read again and both said zero. Anything else is "cannot confirm", which a screen may say
    and a zero may not.
    """
    remaining, basis, closed = None, UNKNOWN_BASIS, None
    if sources.both:
        remaining = round(max(sources.valid), 6)
        basis = BOTH if sources.agrees else LARGER
        closed = remaining <= TOLERANCE
    return {"confirmed_remaining": remaining, "confirmed_remaining_basis": basis,
            "position_closed": closed}


def members_of(proposal: Proposal, everything: list[Proposal]) -> list[Proposal]:
    """One exit's whole chain: the root proposal and every continuation of it, oldest first."""
    by_id = {p.proposal_id: p for p in everything}
    root = proposal
    seen = {root.proposal_id}
    while getattr(root, "continuation_of", None) and root.continuation_of in by_id:
        root = by_id[root.continuation_of]
        if root.proposal_id in seen:                 # a cycle cannot be walked; stop where it closes
            break
        seen.add(root.proposal_id)
    chain = [root]
    changed = True
    while changed:
        changed = False
        for candidate in everything:
            if (getattr(candidate, "continuation_of", None) in {m.proposal_id for m in chain}
                    and candidate.proposal_id not in {m.proposal_id for m in chain}):
                chain.append(candidate)
                changed = True
    # The root is first by identity, not by timestamp. A continuation inherits the root's
    # ``created_at`` -- that is the research clock, and creating a record must not restart it --
    # so ordering by time alone would leave which member is the root to a tie-break.
    return [root, *sorted(chain[1:], key=lambda p: (p.continued_at or p.created_at))]


def unfinished(proposal: Proposal) -> bool:
    """Whether this member could still consume allowance: awaiting a decision, or working."""
    return proposal.status in (PROPOSED, APPROVED) or proposal.status in PENDING_EXECUTION


def open_continuation(proposal: Proposal, everything: list[Proposal]) -> Proposal | None:
    """The continuation of this chain that has not finished, if there is one.

    One chain holds one at a time. The check is by ``continuation_of`` rather than by counting,
    so the root itself -- which is nobody's continuation -- never answers this.
    """
    for member in members_of(proposal, everything):
        if member.continuation_of and unfinished(member):
            return member
    return None


def chain_of(proposal: Proposal, everything: list[Proposal]) -> dict[str, Any]:
    """The allowance this exit was given, what it has spent, and what is still held against it.

    ``committed`` counts only the part of an unfinished member that has **not** filled. Counting
    a whole authorisation while also deducting its fills would deduct a partial fill twice, and
    the remaining allowance could come out negative.
    """
    chain = members_of(proposal, everything)
    root = chain[0]
    allowance = float(root.sell_quantity or 0.0)
    filled = sum(float(m.filled_quantity or 0.0) for m in chain)
    committed = sum(max(0.0, float(m.sell_quantity or 0.0) - float(m.filled_quantity or 0.0))
                    for m in chain if unfinished(m))
    # Rounded to the venue's six places, as every other quantity in this module is. Two sums of
    # floats put 0.08000000000000002 on the wire, and a screen is not the place to explain that.
    # The refusals here are all tolerance-based, so rounding changes no decision.
    return {
        "root_proposal_id": root.proposal_id,
        "allowance": round(allowance, 6),
        "filled": round(filled, 6),
        "committed": round(committed, 6),
        "remaining_allowance": round(max(0.0, allowance - filled - committed), 6),
        "members": [{"proposal_id": m.proposal_id, "status": m.status,
                     "authorised_quantity": m.sell_quantity,
                     "filled_quantity": m.filled_quantity} for m in chain],
    }


def continuable(proposal: Proposal, everything: list[Proposal], *, sources: Sources,
                chain: dict[str, Any], account_has_active_order: bool,
                research_expired: bool, increment: float = MIN_INCREMENT) -> dict[str, Any]:
    """Whether the unsold part of this exit may be taken up again, and what stops it.

    Every condition here is a reason the venue might still act on the original order, or a
    reason this side cannot say what is left. A continuation created while any of them holds
    would sell shares twice.
    """
    blocked: list[dict[str, str]] = []
    chain_members = members_of(proposal, everything)
    # Two different facts, and the contract keeps them apart because they read differently on a
    # screen. "Working" is every state in which the venue may still act on the order, which is
    # why an unknown one counts: not knowing is precisely when a second sell is dangerous.
    # "Unresolved" is a positive signal that this side cannot account for what happened.
    if any(m.status in PENDING_EXECUTION for m in chain_members):
        blocked.append({"code": ORDER_WORKING,
                        "detail": "an order from this exit is still working at the venue; it may "
                                  "yet fill what is left"})
    if any(m.reconciliation_error or m.status == UNKNOWN
           or (needs_reconciliation(m) and m.status not in PENDING_EXECUTION)
           for m in chain_members):
        blocked.append({"code": RECONCILIATION_UNRESOLVED,
                        "detail": "the previous attempt is unresolved; reconciliation continues"})
    if account_has_active_order:
        blocked.append({"code": ACTIVE_ORDER,
                        "detail": f"an order is already working in {proposal.bucket}; one account "
                                  "holds one active order"})
    waiting = open_continuation(proposal, everything)
    if waiting is not None and waiting.proposal_id != proposal.proposal_id:
        blocked.append({"code": CONTINUATION_WAITING,
                        "detail": f"a continuation of this exit ({waiting.proposal_id}) is already "
                                  "waiting for a person; one chain holds one at a time"})
    # Two ways nothing has been attempted yet, and the answer to both is the same: approve this
    # one. The chain has never reached the venue at all, or this member has not -- which is what
    # a continuation still waiting for a person looks like from its own screen.
    if (not any(m.client_order_id for m in chain_members)
            or (unfinished(proposal) and not proposal.client_order_id)):
        blocked.append({"code": NOT_ATTEMPTED,
                        "detail": "this exit has not been attempted yet; approve it rather than "
                                  "continuing it"})
    elif chain["remaining_allowance"] <= TOLERANCE:
        blocked.append({"code": ALLOWANCE_SPENT,
                        "detail": f"the original authorisation of {chain['allowance']:g} is fully "
                                  "accounted for; a position acquired since is a new idea and "
                                  "needs its own proposal"})
    book = sellable(sources)
    if book["quantity"] is None:
        blocked.append({"code": QUANTITY_UNKNOWN,
                        "detail": sources.note or "the quantity cannot be established"})
    elif book["quantity"] <= TOLERANCE and not sources.committed_quantity:
        blocked.append({"code": POSITION_GONE, "detail": "the position is gone; nothing to continue"})
    if research_expired:
        blocked.append({"code": RESEARCH_EXPIRED,
                        "detail": "the research behind this exit has expired; request fresh research"})
    quantity = None
    if book["quantity"] is not None:
        quantity = floor_to_increment(min(chain["remaining_allowance"], book["quantity"]), increment)
        if not blocked and quantity < increment:
            blocked.append({"code": BELOW_PRECISION,
                            "detail": f"what remains is smaller than the venue's {increment:g} increment"})
    return {"eligible": not blocked, "quantity": quantity, "blocked_by": blocked}


def result(proposal: Proposal, everything: list[Proposal], *, sources: Sources,
           account_has_active_order: bool, research_expired: bool,
           submitted_quantity: float | None,
           increment: float = MIN_INCREMENT) -> dict[str, Any]:
    """What the attempt did to the position, and whether anything is left to take up.

    ``sources`` has to be a reading taken **after** the attempt. The contract will not call a
    position closed on figures read before the order went, and the venue is the only place a
    fresh one comes from -- which is why the caller reads it while the session is still open.

    ``continuable`` is the same function the screen and the to-do list read, deliberately: the
    answer a person gets immediately after an exit must be the answer they get when they come
    back to the page an hour later.
    """
    chain = chain_of(proposal, everything)
    return {
        "requested_quantity": proposal.sell_quantity,
        "submitted_quantity": submitted_quantity,
        "filled_quantity": float(proposal.filled_quantity or 0.0),
        **confirmation(sources),
        "chain": chain,
        "continuable": continuable(proposal, everything, sources=sources, chain=chain,
                                   account_has_active_order=account_has_active_order,
                                   research_expired=research_expired, increment=increment),
    }


def view(proposal: Proposal, everything: list[Proposal], *, sources: Sources,
         account_has_active_order: bool, research_expired: bool,
         autonomous: dict[str, Any] | None = None,
         increment: float = MIN_INCREMENT) -> dict[str, Any]:
    """The whole ``exit`` block for one reducing proposal, as the approvals screen reads it.

    ``autonomous`` is the floor under this one position, and belongs to ``REDUCE`` alone: ``SELL``
    is never autonomous, so a number there would invite a screen to reason about a limit that
    does not apply. It is passed in rather than derived, because deriving it needs the fills and
    the approval records, which this module deliberately does not read.
    """
    chain = chain_of(proposal, everything)
    expected = expectations(sources, authorised=proposal.sell_quantity, increment=increment)
    onward = continuable(proposal, everything, sources=sources, chain=chain,
                         account_has_active_order=account_has_active_order,
                         research_expired=research_expired, increment=increment)
    blocked = [item for item in onward["blocked_by"]
               if item["code"] in (ORDER_WORKING, RECONCILIATION_UNRESOLVED, ACTIVE_ORDER,
                                   POSITION_GONE, QUANTITY_UNKNOWN)]
    return {
        "action": proposal.action,
        # Which key of /config/authority.actions the screen must read for this one. The account's
        # own blocks live there; everything in this block is about the position.
        "authority_action": proposal.action,
        "intent": "close_position" if proposal.action == "SELL" else "reduce_position",
        "authorised_quantity": proposal.sell_quantity,
        "sellable": sellable(sources),
        **expected,
        "min_sellable_increment": increment,
        "already_filled": float(proposal.filled_quantity or 0.0),
        "chain": chain,
        "continuable": onward,
        "autonomous": autonomous if proposal.action == "REDUCE" else None,
        "blocked_by": blocked,
    }
