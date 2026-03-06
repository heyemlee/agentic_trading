"""What a sell would do: the two books, the chain's allowance, and what blocks continuing it.

Offline and pure: every case builds the two sources by hand, because the whole point of these
functions is what they answer when one of the books is missing, stale or disagrees, and a fixture
that reached a venue could not exercise that.

The cases mirror the samples in docs/SELL_CONTRACT_2026-09-19.md §4 by letter.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from agentic_trading import exits
from agentic_trading.execution_scenarios import _proposal
from agentic_trading.proposals import (
    APPROVED,
    CANCELED,
    EXECUTED,
    PARTIALLY_FILLED,
    PROPOSED,
    SUBMITTED,
)

NOW = datetime(2026, 9, 19, 15, 5, 0, tzinfo=UTC)
FRESH = (NOW - timedelta(seconds=30)).isoformat()
OLD = (NOW - timedelta(seconds=600)).isoformat()


def sources(ledger=0.2, venue=0.2, *, stale=False, committed=0.0, as_of=FRESH):
    return exits.Sources(ledger_quantity=ledger, venue_quantity=venue, venue_as_of=as_of,
                         venue_stale=stale, committed_quantity=committed)


def sell(*, quantity=0.2, status=PROPOSED, filled=0.0, ordered=False, action="SELL",
         proposal_id=None, continuation_of=None, created="2026-09-19T14:00:00+00:00"):
    p = _proposal(bucket="micro_live", action=action, ticker="VTI", notional=0.0,
                  limit_class="broad_diversified_etf")
    p.sell_quantity = quantity
    p.status = status
    p.filled_quantity = filled
    p.created_at = created
    p.continuation_of = continuation_of
    if proposal_id:
        p.proposal_id = proposal_id
    if ordered:
        p.client_order_id = f"ct-{p.proposal_id}-1"
        p.reconciled_at = "2026-09-19T15:00:00+00:00"
    return p


def view(proposal, everything=None, *, src=None, active=False, expired=False):
    return exits.view(proposal, everything or [proposal], sources=src or sources(),
                      account_has_active_order=active, research_expired=expired)


# ------------------------------------------------------------------ the two books
def test_the_smaller_book_sizes_the_order_and_the_larger_says_what_remains():
    """Sample F: both books valid and disagreeing, which is the only case where both bases show."""
    disagree = sources(ledger=0.2, venue=0.3)
    book = exits.sellable(disagree)
    assert book["quantity"] == 0.2 and book["basis"] == exits.SMALLER
    assert book["agrees"] is False
    expected = exits.expectations(disagree, authorised=0.3)
    assert expected["expected_submit_quantity"] == 0.2
    # The remainder comes from the larger book and is rounded to the venue's own precision.
    assert expected["expected_remaining"] == 0.1
    assert expected["expected_remaining_basis"] == exits.LARGER
    # A disagreement can never read as a closed position: the order cannot exceed the smaller
    # figure, so the difference is always left over and something is unaccounted for.
    assert expected["expected_closes_position"] is False
    # Two books that disagree still give a definite order, so this is not an estimate.
    assert expected["expected_is_estimate"] is False


def test_agreement_is_the_only_way_to_expect_a_closed_position():
    """Sample A."""
    expected = exits.expectations(sources(0.2, 0.2), authorised=0.2)
    assert expected["expected_remaining"] == 0.0
    assert expected["expected_closes_position"] is True
    assert expected["expected_remaining_basis"] == exits.BOTH
    # Authorised for less than is held: closes nothing, and says so.
    partial = exits.expectations(sources(0.2, 0.2), authorised=0.1)
    assert partial["expected_submit_quantity"] == 0.1 and partial["expected_remaining"] == 0.1
    assert partial["expected_closes_position"] is False


@pytest.mark.parametrize("src, basis", [
    (sources(ledger=0.2, venue=0.3, stale=True), exits.LEDGER_ONLY),
    (sources(ledger=None, venue=0.3), exits.VENUE_ONLY),
    (sources(ledger=None, venue=None), exits.NONE),
])
def test_without_two_books_nothing_is_said_about_what_remains(src, basis):
    """Samples D and E: one book still sizes an estimate, neither book says what is left."""
    assert exits.sellable(src)["basis"] == basis
    expected = exits.expectations(src, authorised=0.2)
    assert expected["expected_remaining"] is None
    assert expected["expected_remaining_basis"] == exits.UNKNOWN_BASIS
    assert expected["expected_closes_position"] is None      # never False, never 0
    assert expected["expected_is_estimate"] is True


def test_a_stale_venue_reading_is_reported_and_used_for_nothing():
    """Sample E: the figure and its time are shown; the arithmetic ignores it."""
    reading = type("R", (), {"connected": True, "checked_at": OLD, "positions": {"VTI": 0.3},
                             "positions_error": None})()
    src = exits.from_reading(ledger_quantity=0.2, reading=reading, symbol="VTI", now=NOW)
    assert src.venue_quantity == 0.3 and src.venue_stale is True
    assert src.effective_venue is None and src.valid == [0.2]
    assert "older than 180s" in src.note
    assert exits.sellable(src)["quantity"] == 0.2           # the ledger still sizes it
    assert exits.expectations(src, authorised=0.2)["expected_closes_position"] is None


def test_positions_that_could_not_be_read_are_not_a_book_holding_nothing():
    reading = type("R", (), {"connected": True, "checked_at": FRESH, "positions": None,
                             "positions_error": "BrokerUnavailable: token expired"})()
    src = exits.from_reading(ledger_quantity=0.2, reading=reading, symbol="VTI", now=NOW)
    assert src.venue_quantity is None and "could not be read" in src.note
    assert exits.sellable(src)["basis"] == exits.LEDGER_ONLY

    nothing_held = type("R", (), {"connected": True, "checked_at": FRESH, "positions": {},
                                  "positions_error": None})()
    empty = exits.from_reading(ledger_quantity=0.2, reading=nothing_held, symbol="VTI", now=NOW)
    assert empty.venue_quantity == 0.0                      # an empty book is an answer
    assert exits.sellable(empty)["basis"] == exits.SMALLER


def test_an_order_already_working_commits_the_shares_it_holds():
    """Sample B: what a working order holds is not available to a second one."""
    src = sources(ledger=0.08, venue=0.08, committed=0.08)
    assert exits.sellable(src)["quantity"] == 0.0
    assert exits.expectations(src, authorised=0.2)["expected_submit_quantity"] == 0.0


def test_a_confirmed_close_needs_both_books_read_again():
    assert exits.confirmation(sources(0.0, 0.0))["position_closed"] is True
    assert exits.confirmation(sources(0.0, 0.08))["position_closed"] is False
    assert exits.confirmation(sources(0.0, 0.08))["confirmed_remaining"] == 0.08
    unreadable = exits.confirmation(sources(0.0, None))
    assert unreadable["position_closed"] is None and unreadable["confirmed_remaining"] is None


def test_quantities_are_floored_to_what_the_venue_accepts():
    assert exits.floor_to_increment(0.12345678) == 0.123456
    assert exits.floor_to_increment(0.0000009) == 0.0
    src = sources(ledger=0.12345678, venue=0.12345678)
    assert exits.expectations(src, authorised=1.0)["expected_submit_quantity"] == 0.123456


# ------------------------------------------------------------------ the chain
def test_the_allowance_counts_fills_and_only_the_unfilled_part_of_what_is_working():
    """Sample B's arithmetic: authorised 0.2, filled 0.12, still working, so committed is 0.08.

    Counting the whole authorisation of a working member while also deducting its fills would
    deduct the partial fill twice and the remaining allowance could come out negative.
    """
    working = sell(quantity=0.2, status=PARTIALLY_FILLED, filled=0.12, ordered=True)
    chain = exits.chain_of(working, [working])
    assert chain["allowance"] == 0.2 and chain["filled"] == 0.12
    assert chain["committed"] == pytest.approx(0.08)
    assert chain["remaining_allowance"] == pytest.approx(0.0)


def test_a_cancellation_with_nothing_filled_returns_the_whole_allowance():
    cancelled = sell(quantity=0.2, status=CANCELED, filled=0.0, ordered=True)
    chain = exits.chain_of(cancelled, [cancelled])
    assert chain["filled"] == 0.0 and chain["committed"] == 0.0
    assert chain["remaining_allowance"] == 0.2


def test_a_cancellation_after_a_partial_fill_returns_only_what_did_not_fill():
    """Sample C."""
    cancelled = sell(quantity=0.2, status=CANCELED, filled=0.12, ordered=True)
    chain = exits.chain_of(cancelled, [cancelled])
    assert chain["remaining_allowance"] == pytest.approx(0.08)


def test_the_chain_is_walked_from_any_member_and_the_allowance_is_the_roots():
    root = sell(quantity=0.2, status=CANCELED, filled=0.12, ordered=True, proposal_id="p_root",
                created="2026-09-19T14:00:00+00:00")
    follow = sell(quantity=0.08, status=PROPOSED, filled=0.0, proposal_id="p_follow",
                  continuation_of="p_root", created="2026-09-19T15:00:00+00:00")
    for member in (root, follow):
        chain = exits.chain_of(member, [follow, root])
        assert chain["root_proposal_id"] == "p_root"
        assert [m["proposal_id"] for m in chain["members"]] == ["p_root", "p_follow"]
        # The root's authorisation bounds the chain; the follow-up holds the unfilled part.
        assert chain["allowance"] == 0.2 and chain["filled"] == 0.12
        assert chain["committed"] == pytest.approx(0.08)
        assert chain["remaining_allowance"] == pytest.approx(0.0)


def test_a_chain_whose_allowance_is_fully_filled_is_closed():
    """Sample G: a position acquired since is a new idea, not more of this authorisation."""
    done = sell(quantity=0.2, status=EXECUTED, filled=0.2, ordered=True)
    chain = exits.chain_of(done, [done])
    assert chain["remaining_allowance"] == 0.0
    onward = exits.continuable(done, [done], sources=sources(0.15, 0.15), chain=chain,
                               account_has_active_order=False, research_expired=False)
    assert onward["eligible"] is False
    assert [b["code"] for b in onward["blocked_by"]] == [exits.ALLOWANCE_SPENT]
    assert "needs its own proposal" in onward["blocked_by"][0]["detail"]


# ------------------------------------------------------------------ continuing an exit
def test_a_working_order_is_never_continued_however_much_is_left():
    """Sample B: PARTIALLY_FILLED is a pending state, so the venue may yet fill the rest."""
    working = sell(quantity=0.2, status=PARTIALLY_FILLED, filled=0.12, ordered=True)
    onward = view(working, src=sources(0.08, 0.08, committed=0.08), active=True)["continuable"]
    assert onward["eligible"] is False
    codes = [b["code"] for b in onward["blocked_by"]]
    assert exits.ORDER_WORKING in codes and exits.ACTIVE_ORDER in codes


def test_an_unresolved_attempt_is_never_continued():
    from agentic_trading.proposals import UNKNOWN

    unresolved = sell(quantity=0.2, status=UNKNOWN, filled=0.0, ordered=True)
    unresolved.reconciliation_error = "the venue returned no status; lookup required"
    onward = view(unresolved, src=sources(0.2, 0.2))["continuable"]
    assert onward["eligible"] is False
    codes = [b["code"] for b in onward["blocked_by"]]
    assert exits.ORDER_WORKING in codes and exits.RECONCILIATION_UNRESOLVED in codes


def test_a_cancelled_and_reconciled_exit_may_be_continued():
    """Sample C: the one shape that is eligible."""
    cancelled = sell(quantity=0.2, status=CANCELED, filled=0.12, ordered=True)
    onward = view(cancelled, src=sources(0.08, 0.08))["continuable"]
    assert onward["eligible"] is True and onward["quantity"] == pytest.approx(0.08)
    assert onward["blocked_by"] == []


def test_an_exit_nobody_has_attempted_is_approved_rather_than_continued():
    """Sample A: not a spent allowance, and not a working order either."""
    onward = view(sell())["continuable"]
    assert onward["eligible"] is False
    assert [b["code"] for b in onward["blocked_by"]] == [exits.NOT_ATTEMPTED]
    assert "approve it rather than continuing it" in onward["blocked_by"][0]["detail"]


def test_a_continuation_is_bounded_by_the_smaller_of_the_allowance_and_what_is_held():
    cancelled = sell(quantity=0.2, status=CANCELED, filled=0.0, ordered=True)
    # The allowance is 0.2 but only 0.05 is still held: the holding wins.
    onward = view(cancelled, src=sources(0.05, 0.05))["continuable"]
    assert onward["eligible"] is True and onward["quantity"] == 0.05
    # And the other way round.
    partly = sell(quantity=0.2, status=CANCELED, filled=0.18, ordered=True)
    assert view(partly, src=sources(0.5, 0.5))["continuable"]["quantity"] == pytest.approx(0.02)


def test_a_position_that_is_gone_and_a_quantity_nobody_knows_are_told_apart():
    cancelled = sell(quantity=0.2, status=CANCELED, filled=0.12, ordered=True)
    gone = view(cancelled, src=sources(0.0, 0.0))["continuable"]
    assert [b["code"] for b in gone["blocked_by"]] == [exits.POSITION_GONE]
    unknown = view(cancelled, src=sources(None, None))["continuable"]
    assert [b["code"] for b in unknown["blocked_by"]] == [exits.QUANTITY_UNKNOWN]
    assert unknown["quantity"] is None                      # not 0


def test_a_remainder_below_the_venues_precision_cannot_be_continued():
    """A venue that only trades whole shares cannot sell the half share that is left.

    Exercised with a whole-share increment on purpose: with the fractional increment Robinhood
    allows, a remainder too small to trade is also too small to tell from a closed position, and
    `position_gone` answers first. The branch exists for a venue that cannot go that small.
    """
    cancelled = sell(quantity=3.0, status=CANCELED, filled=2.5, ordered=True)
    chain = exits.chain_of(cancelled, [cancelled])
    assert chain["remaining_allowance"] == 0.5
    onward = exits.continuable(cancelled, [cancelled], sources=sources(0.5, 0.5), chain=chain,
                              account_has_active_order=False, research_expired=False,
                              increment=1.0)
    assert onward["eligible"] is False and onward["quantity"] == 0.0
    assert [b["code"] for b in onward["blocked_by"]] == [exits.BELOW_PRECISION]


def test_expired_research_stops_a_continuation_and_says_where_to_go():
    cancelled = sell(quantity=0.2, status=CANCELED, filled=0.12, ordered=True)
    onward = view(cancelled, src=sources(0.08, 0.08), expired=True)["continuable"]
    assert onward["eligible"] is False
    assert [b["code"] for b in onward["blocked_by"]] == [exits.RESEARCH_EXPIRED]
    assert "request fresh research" in onward["blocked_by"][0]["detail"]


# ------------------------------------------------------------------ the whole block
def test_the_block_names_which_authority_action_the_screen_must_read():
    """The account's own blocks live under that key; everything in this block is the position's."""
    assert view(sell(action="SELL"))["authority_action"] == "SELL"
    assert view(sell(action="SELL"))["intent"] == "close_position"
    reduce = view(sell(action="REDUCE", quantity=0.1))
    assert reduce["authority_action"] == "REDUCE" and reduce["intent"] == "reduce_position"


def test_the_positions_own_blocks_are_separate_from_the_chains():
    """exit.blocked_by is about this position; a spent allowance is only a continuation's problem."""
    done = sell(quantity=0.2, status=EXECUTED, filled=0.2, ordered=True)
    block = view(done, src=sources(0.15, 0.15))
    assert [b["code"] for b in block["continuable"]["blocked_by"]] == [exits.ALLOWANCE_SPENT]
    assert block["blocked_by"] == []                        # nothing is wrong with the position
    working = view(sell(status=SUBMITTED, ordered=True), src=sources(0.2, 0.2), active=True)
    assert {b["code"] for b in working["blocked_by"]} == {exits.ORDER_WORKING, exits.ACTIVE_ORDER}


def test_the_block_carries_what_the_screen_needs_and_nothing_it_must_guess():
    block = view(sell(status=APPROVED, quantity=0.2))
    assert set(block) == {"action", "authority_action", "intent", "authorised_quantity",
                          "sellable", "expected_submit_quantity", "expected_is_estimate",
                          "expected_remaining", "expected_remaining_basis",
                          "expected_closes_position", "min_sellable_increment", "already_filled",
                          "chain", "continuable", "blocked_by"}
    assert set(block["sellable"]) == {"quantity", "basis", "ledger_quantity", "venue_quantity",
                                      "venue_as_of", "venue_stale", "committed_quantity",
                                      "agrees", "note"}
    assert set(block["chain"]) == {"root_proposal_id", "allowance", "filled", "committed",
                                   "remaining_allowance", "members"}
    assert block["min_sellable_increment"] == exits.MIN_INCREMENT
