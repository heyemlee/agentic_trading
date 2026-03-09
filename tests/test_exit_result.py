"""What an exit did, as the answer to the approval carries it, and the to-do it leaves. Offline.

Two things are under test. The block the approval returns must describe the position **after** the
order, which is why the venue is read again while the session is still open: a remainder read
before the order would be the world as it was. And the to-do that follows an exit nobody finished
must appear and disappear on exactly the condition the endpoint and the button use, so a person
cannot be told to continue something the endpoint would refuse.
"""

import uuid

import pytest
import yaml

from agentic_trading import autonomy, execution, exits
from agentic_trading.broker import SimulatedBroker
from agentic_trading.execution_scenarios import PRICES, TRADE_DATE, _profiles, _proposal
from agentic_trading.portfolio import snapshot_from_broker
from agentic_trading.proposals import (
    EXECUTED,
    PROPOSED,
    ProposalStore,
    positions_fingerprint,
    utcnow,
)
from agentic_trading.risk_scenarios import identity_lookup, regime
from agentic_trading.service import Service
from agentic_trading.service.shapes import MarketStatus

MICRO = "micro_live"
AUTHORITY = "authority-under-test"


@pytest.fixture(autouse=True)
def offline_market(monkeypatch):
    monkeypatch.setattr(execution, "assess_regime", lambda *args: regime("NORMAL"))
    monkeypatch.setattr(execution, "snapshot_from_broker",
                        lambda *args: snapshot_from_broker(*args, identity_lookup=identity_lookup))
    monkeypatch.setattr(autonomy, "note_version", lambda *_: AUTHORITY)


class VenueLike(SimulatedBroker):
    """A simulated venue that answers to the real-money binding.

    ``micro_live`` refuses any broker that is not named for the venue, which is a rule worth
    keeping; a double that passes it is how the exit path gets tested without a real account.
    """

    name = "robinhood"


@pytest.fixture
def world(tmp_path, monkeypatch):
    profiles = _profiles(tmp_path)
    ledger = tmp_path / "micro_live_portfolio.yaml"
    ledger.write_text(yaml.safe_dump({
        "as_of": TRADE_DATE, "currency": "USD", "buckets": {MICRO: {"cash_usd": 40.0}},
        "positions": [{"symbol": "VTI", "bucket": MICRO, "quantity": 0.2,
                       "cost_basis_per_share": 370.0}], "recent_opens": []}), encoding="utf-8")
    profiles.risk["micro_live_execution"] = {
        **(profiles.risk.get("micro_live_execution") or {}),
        "enabled": True, "ledger_file": str(ledger)}
    broker = VenueLike(prices=dict(PRICES), cash=40.0, positions={"VTI": (0.2, 370.0)})
    store = ProposalStore(tmp_path / "proposals.json")
    service = Service()
    monkeypatch.setattr(service, "profiles", lambda: profiles)
    monkeypatch.setattr(service, "_store", lambda _=None: store)
    monkeypatch.setattr(service, "_broker", lambda *_a, **_k: broker)
    monkeypatch.setattr(service, "market_status", lambda: MarketStatus(is_open=True))
    return service, profiles, store, broker, ledger


BOOK = positions_fingerprint({"VTI": 0.2})


def exit_proposal(store, *, quantity=0.1, action="REDUCE"):
    p = _proposal(bucket=MICRO, action=action, ticker="VTI", notional=quantity * PRICES["VTI"],
                  limit_class="broad_diversified_etf", status=PROPOSED, fingerprint=BOOK)
    p.sell_quantity = quantity
    p.approved_at = None
    p.authority_version = AUTHORITY
    store.save(p)
    return p


def approve(service, p):
    return service.approve(
        p.proposal_id, identity="verified-owner", version=p.version(),
        request_id=str(uuid.uuid4()),
        terms={"ticker": p.ticker, "bucket": p.bucket, "action": p.action,
               "notional_usd": p.notional_usd, "sell_quantity": p.sell_quantity},
        authority_version=AUTHORITY)


# ------------------------------------------------------------------ the block on the answer

def test_the_answer_says_what_the_attempt_did_to_the_position(world):
    service, _, store, _broker, _ = world
    p = exit_proposal(store)
    answer = approve(service, p)
    assert answer["status"] == "EXECUTED", answer
    block = answer["exit_result"]
    assert block["requested_quantity"] == pytest.approx(0.1)
    assert block["submitted_quantity"] == pytest.approx(0.1)
    assert block["filled_quantity"] == pytest.approx(0.1)
    # Both books were read again after the fill and both say 0.1 is left, so the position is
    # confirmed open rather than closed.
    assert block["confirmed_remaining"] == pytest.approx(0.1)
    assert block["confirmed_remaining_basis"] == exits.BOTH
    assert block["position_closed"] is False
    assert set(block) == {"requested_quantity", "submitted_quantity", "filled_quantity",
                          "confirmed_remaining", "confirmed_remaining_basis", "position_closed",
                          "chain", "continuable"}


def test_a_closed_position_is_only_claimed_from_a_reading_taken_afterwards(world):
    """The whole reason the venue is read again: before the order both books said 0.2."""
    service, _, store, _broker, _ = world
    block = approve(service, exit_proposal(store, quantity=0.2, action="SELL"))["exit_result"]
    assert block["filled_quantity"] == pytest.approx(0.2)
    assert block["confirmed_remaining"] == pytest.approx(0.0)
    assert block["position_closed"] is True
    assert block["continuable"]["eligible"] is False
    assert exits.POSITION_GONE in [b["code"] for b in block["continuable"]["blocked_by"]]


def test_an_unreadable_venue_after_the_order_cannot_confirm_anything(world, monkeypatch):
    """A book nobody could read is not a book that says zero.

    The session drops after the order rather than before it, which is the case that matters: the
    shares are gone and the only question left is whether anything remains, and the honest answer
    to that is "cannot confirm".
    """
    service, _, store, broker, _ = world
    real = broker.get_positions

    def fails_once_the_order_is_in():
        if broker._orders:
            raise RuntimeError("session dropped")
        return real()

    monkeypatch.setattr(broker, "get_positions", fails_once_the_order_is_in)
    block = approve(service, exit_proposal(store, quantity=0.2, action="SELL"))["exit_result"]
    assert block["filled_quantity"] == pytest.approx(0.2)
    assert block["position_closed"] is None
    assert block["confirmed_remaining"] is None
    assert block["confirmed_remaining_basis"] == exits.UNKNOWN_BASIS


def test_buying_carries_no_exit_block(world):
    service, _, store, _, _ = world
    p = _proposal(bucket=MICRO, action="BUY", ticker="VTI", notional=20.0,
                  limit_class="broad_diversified_etf", status=PROPOSED, fingerprint=BOOK)
    p.approved_at = None
    p.authority_version = AUTHORITY
    store.save(p)
    assert "exit_result" not in approve(service, p)


def test_a_refused_approval_still_says_where_the_position_stands(world):
    """An exit that never reached the venue: nothing submitted, nothing filled, nothing lost."""
    service, profiles, store, _, _ = world
    profiles.risk["execution_policy"]["kill_switch"]["trading_halted"] = True
    answer = approve(service, exit_proposal(store))
    assert answer["outcome"] != "EXECUTED"
    block = answer["exit_result"]
    assert block["filled_quantity"] == 0.0 and block["submitted_quantity"] is None
    assert block["requested_quantity"] == pytest.approx(0.1)


# ------------------------------------------------------------------ the to-do that follows

def stopped_exit(store, *, authorised=0.2, filled=0.12, ticker="VTI"):
    """An exit that reached the venue, filled part of its authorisation and finished."""
    p = _proposal(bucket=MICRO, action="REDUCE", ticker=ticker, status=EXECUTED,
                  notional=authorised * PRICES[ticker], limit_class="broad_diversified_etf",
                  fingerprint=BOOK)
    p.sell_quantity = authorised
    p.filled_quantity = filled
    p.client_order_id = f"co-{uuid.uuid4().hex[:8]}"
    p.reconciled_at = utcnow().isoformat()
    p.authority_version = AUTHORITY
    store.save(p)
    return p


def todo(service, kind="exit_incomplete"):
    return [item for item in service.attention()["items"] if item["kind"] == kind]


def test_an_exit_that_stopped_part_way_asks_to_be_finished(world):
    service, _, store, _, _ = world
    first = stopped_exit(store)
    items = todo(service)
    assert len(items) == 1
    item = items[0]
    assert item["severity"] == "high" and item["priority"] == 3
    assert item["requires_person"] is True
    assert item["actions"] == ["continue_exit", "open"]
    assert item["subject"] == first.proposal_id
    assert item["scope"] == "agentic" and item["account_id"] == MICRO
    assert "0.12 of the 0.2 authorised sold" in item["detail"]
    assert item["link"]["page"] == "approvals"


def test_the_to_do_goes_quiet_once_a_continuation_exists(world):
    """The to-do, the button and the endpoint read one predicate, so they stop together."""
    service, _, store, _, _ = world
    first = stopped_exit(store)
    assert len(todo(service)) == 1
    service.continue_exit(first.proposal_id, request_id=str(uuid.uuid4()))
    assert todo(service) == []


def test_a_fully_filled_exit_asks_nothing(world):
    service, _, store, _, _ = world
    stopped_exit(store, filled=0.2)
    assert todo(service) == []


def test_an_exit_whose_position_is_gone_asks_nothing(world):
    service, _, store, _, ledger = world
    doc = yaml.safe_load(ledger.read_text(encoding="utf-8"))
    doc["positions"] = []
    ledger.write_text(yaml.safe_dump(doc), encoding="utf-8")
    stopped_exit(store)
    assert todo(service) == []


def test_an_exit_still_working_at_the_venue_asks_nothing(world):
    """Not knowing what the venue did is a different to-do, and never an invitation to sell."""
    service, _, store, _, _ = world
    p = stopped_exit(store)
    p.status = "SUBMITTED"
    p.reconciled_at = None
    store.save(p)
    assert todo(service) == []


def test_an_exit_older_than_the_window_stops_asking(world):
    from datetime import timedelta

    service, _profiles, store, _, _ = world
    p = stopped_exit(store)
    p.reconciled_at = (utcnow() - timedelta(hours=73)).isoformat()
    store.save(p)
    assert todo(service) == []


def test_one_chain_asks_once(world):
    service, _, store, _, _ = world
    first = stopped_exit(store)
    made = service.continue_exit(first.proposal_id, request_id=str(uuid.uuid4()))
    continuation = store.get(made["proposal"]["proposal_id"])
    continuation.status = EXECUTED
    continuation.filled_quantity = 0.02
    continuation.client_order_id = "co-second"
    continuation.reconciled_at = utcnow().isoformat()
    store.save(continuation)
    items = todo(service)
    assert len(items) == 1, "two members, one exit"
    assert items[0]["subject"] == first.proposal_id
    assert "0.14 of the 0.2 authorised sold" in items[0]["detail"]


# ------------------------------------------------------------------ the whole round trip

def test_a_partly_filled_exit_can_be_continued_and_approved(world):
    """The case the whole chain exists for, from the first order to the last.

    This is what caught the position fingerprint: a continuation inherits the research, and the
    research was done against a book the chain's own fill has since changed. Stamping the
    inherited fingerprint on it made every continuation unapprovable.
    """
    service, profiles, store, broker, _ledger = world
    broker._fill_mode = "partial"
    first = exit_proposal(store, quantity=0.2, action="SELL")
    opening = approve(service, first)
    assert opening["exit_result"]["filled_quantity"] > 0
    filled = opening["exit_result"]["filled_quantity"]
    assert filled < 0.2, "a partial fill is the premise of this test"

    # The venue cancels the remainder, which is where a partial exit really stops, and this side
    # learns it the way it always does: by reconciling against the venue rather than by assuming.
    saved = store.get(first.proposal_id)
    broker.cancel_order(saved.broker_order_id or saved.client_order_id)
    execution.reconcile_proposal(profiles, store.get(first.proposal_id), broker, store)
    saved = store.get(first.proposal_id)
    assert not saved.reconciliation_error and saved.reconciled_at
    assert saved.filled_quantity == pytest.approx(filled)

    made = service.continue_exit(first.proposal_id, request_id=str(uuid.uuid4()))
    continuation = store.get(made["proposal"]["proposal_id"])
    assert continuation.positions_fingerprint is None, "our own fill is not a surprise"
    assert continuation.history[-1]["positions_fingerprint_at_analysis"] == BOOK
    assert continuation.sell_quantity == pytest.approx(round(0.2 - filled, 6))

    broker._fill_mode = "full"
    answer = approve(service, continuation)
    assert answer["status"] == "EXECUTED", answer
    assert answer["exit_result"]["filled_quantity"] == pytest.approx(continuation.sell_quantity)
    assert answer["exit_result"]["position_closed"] is True
    # The chain sold exactly what was authorised, over two orders and never more.
    chain = answer["exit_result"]["chain"]
    assert chain["filled"] == pytest.approx(0.2)
    assert chain["remaining_allowance"] == pytest.approx(0.0)
    assert len(chain["members"]) == 2
    assert todo(service) == [], "nothing left to finish"


def test_a_cancel_that_filled_nothing_keeps_the_book_it_was_analysed_against(world):
    """The fingerprint is dropped only where our own fill explains the change."""
    service, _, store, _, _ = world
    first = exit_proposal(store, quantity=0.2, action="SELL")
    first.status, first.filled_quantity = EXECUTED, 0.0
    first.client_order_id = "co-cancelled"
    first.reconciled_at = utcnow().isoformat()
    store.save(first)
    made = service.continue_exit(first.proposal_id, request_id=str(uuid.uuid4()))
    continuation = store.get(made["proposal"]["proposal_id"])
    assert continuation.positions_fingerprint == BOOK
    assert continuation.sell_quantity == pytest.approx(0.2)
    answer = approve(service, continuation)
    assert answer["status"] == "EXECUTED", answer
