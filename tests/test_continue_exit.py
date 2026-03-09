"""Taking up the unsold part of an exit. Fully offline.

An exit that stopped half way is a second decision, so this endpoint prepares the proposal that
decision is made about and executes nothing. Every test here is really about one question: can a
chain ever hold two things that could each sell the same shares. The answer has to stay no under
a repeated tap, a second tab, a restart, and a venue that has not finished.
"""

import uuid
from dataclasses import replace
from datetime import timedelta

import pytest
import yaml

from agentic_trading import autonomy, exits
from agentic_trading.broker import SimulatedBroker
from agentic_trading.execution_scenarios import PRICES, TRADE_DATE, _profiles, _proposal
from agentic_trading.proposals import (
    CANCELED,
    EXECUTED,
    PROPOSED,
    SUBMITTED,
    UNKNOWN,
    ProposalStore,
    utcnow,
)
from agentic_trading.service import Service, ServiceConflict, ServiceError
from agentic_trading.service.shapes import MarketStatus

MICRO = "micro_live"
AUTHORITY = "authority-under-test"
HELD = 0.2


@pytest.fixture
def setup(tmp_path, monkeypatch):
    profiles = _profiles(tmp_path)
    ledger = tmp_path / "micro_live_portfolio.yaml"
    ledger.write_text(yaml.safe_dump({
        "as_of": TRADE_DATE, "currency": "USD", "buckets": {MICRO: {"cash_usd": 40.0}},
        "positions": [{"symbol": "VTI", "bucket": MICRO, "quantity": HELD,
                       "cost_basis_per_share": 370.0}], "recent_opens": []}), encoding="utf-8")
    profiles.risk["micro_live_execution"] = {
        **(profiles.risk.get("micro_live_execution") or {}),
        "enabled": True, "ledger_file": str(ledger)}
    store = ProposalStore(tmp_path / "proposals.json")
    service = Service()
    monkeypatch.setattr(service, "profiles", lambda: profiles)
    monkeypatch.setattr(service, "_store", lambda _=None: store)
    monkeypatch.setattr(service, "market_status", lambda: MarketStatus(is_open=True))
    monkeypatch.setattr(autonomy, "note_version", lambda *_: AUTHORITY)
    # Nothing here may reach a venue: continuing an exit creates a record and stops.
    monkeypatch.setattr(service, "_broker",
                        lambda *_a, **_k: pytest.fail("continuing an exit opened a venue session"))
    return service, profiles, store, ledger


def root(store, *, authorised=0.2, filled=0.12, status=CANCELED, attempted=True,
         reconciled=True, age_minutes=30.0, error=None, ticker="VTI"):
    """An exit that reached the venue and stopped: the shape a continuation is asked for."""
    p = _proposal(bucket=MICRO, action="REDUCE", ticker=ticker, notional=authorised * PRICES[ticker],
                  limit_class="broad_diversified_etf", status=status, age_minutes=age_minutes)
    p.sell_quantity = authorised
    p.filled_quantity = filled
    p.authority_version = AUTHORITY
    p.requires_human_approval = True
    if attempted:
        p.client_order_id = f"co-{uuid.uuid4().hex[:8]}"
        p.broker_order_id = f"bo-{uuid.uuid4().hex[:8]}"
    if reconciled:
        p.reconciled_at = utcnow().isoformat()
    p.reconciliation_error = error
    store.save(p)
    return p


def ask(service, proposal, **over):
    kw = {"request_id": str(uuid.uuid4()), "identity": "investor"}
    kw.update(over)
    return service.continue_exit(proposal.proposal_id, **kw)


# ------------------------------------------------------------------ the eligible case

def test_a_stopped_exit_becomes_a_proposal_a_person_must_approve(setup):
    service, _profiles, store, _ = setup
    first = root(store)
    answer = ask(service, first)

    assert answer["deduplicated"] is False
    assert answer["continuation_of"] == first.proposal_id
    assert answer["root_proposal_id"] == first.proposal_id
    # 0.2 was authorised and 0.12 filled, so 0.08 of the authorisation is left; the book holds
    # 0.2, so the authorisation is what binds.
    assert answer["authorised_quantity"] == pytest.approx(0.08)

    made = store.get(answer["proposal"]["proposal_id"])
    assert made.status == PROPOSED and made.requires_human_approval is True
    # What the contract calls the status: the stored value is PROPOSED, and the one the API
    # publishes is the display status every other waiting proposal has.
    assert answer["proposal"]["status"] == "WAITING_FOR_APPROVAL"
    assert made.continuation_of == first.proposal_id
    assert made.sell_quantity == pytest.approx(0.08)
    assert made.action == "REDUCE" and made.ticker == "VTI" and made.bucket == MICRO
    assert (made.run_id, made.strategy, made.instrument_type) == (
        first.run_id, first.strategy, first.instrument_type)
    assert made.authority_version == AUTHORITY
    # Nothing of the earlier attempt came across, or the chain would count its fills twice.
    assert made.client_order_id is None and made.broker_order_id is None
    assert made.filled_quantity == 0.0 and made.approved_at is None and made.approved_via is None
    assert made.reconciled_at is None and made.order_status is None
    assert made.notional_usd == pytest.approx(first.notional_usd * 0.08 / 0.2)
    entry = made.history[-1]
    assert entry["event"] == "exit_continued" and entry["purpose"] == "exit_review"
    assert entry["root_proposal_id"] == first.proposal_id


def test_the_research_clock_is_inherited_and_not_restarted(setup):
    """Creating a record must not wash stale research into something that looks fresh."""
    service, _profiles, store, _ = setup
    first = root(store, age_minutes=600.0)
    answer = ask(service, first)
    made = store.get(answer["proposal"]["proposal_id"])
    assert made.created_at == first.created_at
    assert answer["research_anchor_at"] == first.created_at
    assert made.age_minutes() == pytest.approx(first.age_minutes(), abs=0.5)
    # What the record's own age is, the record says separately.
    assert made.continued_at is not None and made.continued_at > first.created_at
    assert answer["continued_at"] == made.continued_at


def test_a_cancel_with_no_fills_returns_the_whole_allowance(setup):
    service, _, store, _ = setup
    first = root(store, filled=0.0)
    answer = ask(service, first)
    assert answer["authorised_quantity"] == pytest.approx(0.2)


def test_the_book_binds_when_it_holds_less_than_the_allowance(setup):
    service, _, store, ledger = setup
    doc = yaml.safe_load(ledger.read_text(encoding="utf-8"))
    doc["positions"][0]["quantity"] = 0.05
    ledger.write_text(yaml.safe_dump(doc), encoding="utf-8")
    answer = ask(service, root(store, filled=0.0))
    assert answer["authorised_quantity"] == pytest.approx(0.05)


# ------------------------------------------------------------------ one chain, one branch

def test_a_repeated_tap_returns_the_same_continuation(setup):
    service, _, store, _ = setup
    first = root(store)
    request = str(uuid.uuid4())
    made = ask(service, first, request_id=request)
    again = ask(service, first, request_id=request)
    assert again["deduplicated"] is True
    assert again["proposal"]["proposal_id"] == made["proposal"]["proposal_id"]
    assert len([p for p in store.all() if p.continuation_of]) == 1


def test_a_second_tab_with_its_own_id_gets_the_one_that_exists(setup):
    """The guard is the chain, not the request id: a fresh id must not make a second branch."""
    service, _, store, _ = setup
    first = root(store)
    made = ask(service, first)
    again = ask(service, first)
    assert again["deduplicated"] is True
    assert again["proposal"]["proposal_id"] == made["proposal"]["proposal_id"]
    assert len([p for p in store.all() if p.continuation_of]) == 1


def test_continuing_the_continuation_reaches_the_same_one(setup):
    """From the continuation's own screen the chain is the same chain."""
    service, _, store, _ = setup
    first = root(store)
    made = ask(service, first)
    continuation = store.get(made["proposal"]["proposal_id"])
    again = service.continue_exit(continuation.proposal_id, request_id=str(uuid.uuid4()))
    assert again["deduplicated"] is True
    assert again["proposal"]["proposal_id"] == continuation.proposal_id


def test_one_request_id_cannot_be_pointed_at_another_exit(setup):
    service, _, store, _ = setup
    first, other = root(store), root(store, ticker="VOO")
    request = str(uuid.uuid4())
    ask(service, first, request_id=request)
    with pytest.raises(ServiceConflict) as exc:
        ask(service, other, request_id=request)
    assert "another exit" in str(exc.value)
    assert len([p for p in store.all() if p.continuation_of]) == 1


def test_the_waiting_continuation_shows_on_the_root_s_screen(setup):
    """The to-do item and the button read one predicate, so both stop once one exists."""
    service, _, store, _ = setup
    first = root(store)
    made = ask(service, first)
    onward = service.proposal(first.proposal_id).exit["continuable"]
    assert onward["eligible"] is False
    codes = [b["code"] for b in onward["blocked_by"]]
    # The actionable reason comes first. The second is true as well and says so carefully: the
    # allowance is accounted for rather than filled, because the waiting continuation holds it.
    assert codes[0] == exits.CONTINUATION_WAITING
    assert codes == [exits.CONTINUATION_WAITING, exits.ALLOWANCE_SPENT]
    assert made["proposal"]["proposal_id"] in onward["blocked_by"][0]["detail"]
    assert "accounted for" in onward["blocked_by"][1]["detail"]
    chain = service.proposal(first.proposal_id).exit["chain"]
    assert chain["committed"] == pytest.approx(0.08) and chain["filled"] == pytest.approx(0.12)
    assert chain["remaining_allowance"] == pytest.approx(0.0)


def test_a_continuation_waiting_for_a_person_is_approved_not_continued(setup):
    service, _, store, _ = setup
    made = ask(service, root(store))
    continuation = store.get(made["proposal"]["proposal_id"])
    onward = service.proposal(continuation.proposal_id).exit["continuable"]
    assert onward["eligible"] is False
    assert exits.NOT_ATTEMPTED in [b["code"] for b in onward["blocked_by"]]


# ------------------------------------------------------------------ the refusals

def test_an_order_still_working_refuses(setup):
    service, _, store, _ = setup
    with pytest.raises(ServiceConflict) as exc:
        ask(service, root(store, status=SUBMITTED, reconciled=False))
    assert str(exc.value) == ("an order from this exit is still working at the venue; "
                              "reconciliation decides what remains")


@pytest.mark.parametrize("over", [{"error": "the venue disagrees about the fill"},
                                  {"status": UNKNOWN, "reconciled": False}])
def test_an_unresolved_attempt_refuses(setup, over):
    service, _, store, _ = setup
    with pytest.raises((ServiceConflict, ServiceError)) as exc:
        ask(service, root(store, **over))
    assert "unresolved" in str(exc.value) or "still working" in str(exc.value)


def test_a_fully_filled_authorisation_refuses(setup):
    service, _, store, _ = setup
    with pytest.raises(ServiceError) as exc:
        ask(service, root(store, filled=0.2, status=EXECUTED))
    assert str(exc.value) == ("the original authorisation is fully filled; a position acquired "
                              "since is a new idea and needs its own proposal")


def test_expired_research_refuses_even_though_the_root_reads_as_executed(setup):
    """The case the lifecycle short circuit hid: an EXECUTED root is never reported as stale."""
    service, profiles, store, _ = setup
    ttl = float((profiles.risk["proposal_freshness"]["classes"]["core_conservative"])["ttl_minutes"])
    first = root(store, status=EXECUTED, age_minutes=ttl + 60.0)
    assert first.freshness(profiles)[0] == EXECUTED, "the status wins there, which is the point"
    assert first.research_expired(profiles) is True
    with pytest.raises(ServiceError) as exc:
        ask(service, first)
    assert str(exc.value) == "the research behind this exit has expired; request fresh research"


def test_a_position_that_is_gone_refuses(setup):
    service, _, store, ledger = setup
    doc = yaml.safe_load(ledger.read_text(encoding="utf-8"))
    doc["positions"] = []
    ledger.write_text(yaml.safe_dump(doc), encoding="utf-8")
    with pytest.raises(ServiceError) as exc:
        ask(service, root(store, filled=0.0))
    assert str(exc.value) == "the position is gone; nothing to continue"


def test_an_exit_never_attempted_is_approved_not_continued(setup):
    service, _, store, _ = setup
    with pytest.raises(ServiceError) as exc:
        ask(service, root(store, status=PROPOSED, attempted=False, filled=0.0, reconciled=False))
    assert "approve it rather than continuing it" in str(exc.value)


def test_a_remainder_below_the_venue_s_increment_refuses(setup):
    service, _, store, ledger = setup
    doc = yaml.safe_load(ledger.read_text(encoding="utf-8"))
    doc["positions"][0]["quantity"] = 0.0000001
    ledger.write_text(yaml.safe_dump(doc), encoding="utf-8")
    with pytest.raises(ServiceError) as exc:
        ask(service, root(store, filled=0.0))
    assert "increment" in str(exc.value) or "gone" in str(exc.value)


def test_only_an_exit_has_a_chain(setup):
    service, _, store, _ = setup
    buy = _proposal(bucket=MICRO, action="BUY", ticker="VTI", notional=70.0,
                    limit_class="broad_diversified_etf")
    buy.authority_version = AUTHORITY
    store.save(buy)
    with pytest.raises(ServiceError) as exc:
        ask(service, buy)
    assert "no exit chain" in str(exc.value)


def test_an_authority_that_moved_under_the_screen_refuses(setup):
    service, _, store, _ = setup
    with pytest.raises(ServiceConflict) as exc:
        ask(service, root(store), authority_version="a-version-from-before")
    assert "reload" in str(exc.value)
    assert [p for p in store.all() if p.continuation_of] == [], "a refusal leaves no record"


def test_a_missing_proposal_refuses(setup):
    service, _, _, _ = setup
    with pytest.raises(ServiceError):
        service.continue_exit("no-such-proposal", request_id=str(uuid.uuid4()))


def test_the_inherited_clock_is_checked_again_at_approval(setup):
    """Two checks, as the contract asks: at creation here, and by execution's own gate.

    The second one is not new code, and that is the point of inheriting ``created_at``: the
    freshness gate every approval passes through reads the research's real age, so an exit whose
    research ran out while its continuation waited in the queue is refused there. The copy below
    is moved to a conceptual bucket for one reason only -- real money refuses a simulated venue
    before it reaches any clock -- and nothing else about it changes.
    """
    from agentic_trading.execution import OUT_STALE, revalidate
    from agentic_trading.proposals import EXPIRED as EXPIRED_FRESHNESS

    service, profiles, store, _ = setup
    made = ask(service, root(store))
    continuation = store.get(made["proposal"]["proposal_id"])
    ttl = float((profiles.risk["proposal_freshness"]["classes"]["core_conservative"])["ttl_minutes"])

    # The research runs out while the continuation sits in the queue.
    aged = replace(continuation, created_at=(utcnow() - timedelta(minutes=ttl + 30)).isoformat())
    assert aged.research_expired(profiles) is True
    assert aged.freshness(profiles)[0] == EXPIRED_FRESHNESS, "what the execution gate reads"

    preview = revalidate(profiles, replace(aged, bucket="core", status="APPROVED"),
                         SimulatedBroker(prices=dict(PRICES), cash=40.0), persist_risk=False)
    assert preview.outcome == OUT_STALE
    assert any("expired proposals are never executed" in r for r in preview.reasons)


def test_a_position_acquired_since_is_not_part_of_the_old_authorisation(setup):
    """The allowance is spent, and buying more does not refill it. That would be a new idea."""
    service, _, store, ledger = setup
    doc = yaml.safe_load(ledger.read_text(encoding="utf-8"))
    doc["positions"][0]["quantity"] = 5.0          # a great deal more than was ever authorised
    ledger.write_text(yaml.safe_dump(doc), encoding="utf-8")
    with pytest.raises(ServiceError) as exc:
        ask(service, root(store, filled=0.2, status=EXECUTED))
    assert "needs its own proposal" in str(exc.value)


def test_two_requests_at_once_still_make_one_continuation(setup):
    """Two tabs, two ids, the same instant. The lock is the whole guard, so it is tested as one."""
    import threading

    service, _, store, _ = setup
    first = root(store)
    answers, errors = [], []
    start = threading.Barrier(2)

    def attempt():
        start.wait(timeout=5)
        try:
            answers.append(ask(service, first))
        except Exception as exc:  # noqa: BLE001 - recorded and asserted on below
            errors.append(exc)

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == [], errors
    assert len({a["proposal"]["proposal_id"] for a in answers}) == 1
    assert [a["deduplicated"] for a in answers].count(True) == 1
    assert len([p for p in store.all() if p.continuation_of]) == 1


def test_a_restart_does_not_grow_a_second_branch(setup, tmp_path, monkeypatch):
    """A fresh process reads the same store and finds the continuation that already exists."""
    service, profiles, store, _ = setup
    first = root(store)
    made = ask(service, first)

    restarted = Service()
    reopened = ProposalStore(tmp_path / "proposals.json")
    monkeypatch.setattr(restarted, "profiles", lambda: profiles)
    monkeypatch.setattr(restarted, "_store", lambda _=None: reopened)
    monkeypatch.setattr(restarted, "market_status", lambda: MarketStatus(is_open=True))
    again = restarted.continue_exit(first.proposal_id, request_id=str(uuid.uuid4()))
    assert again["deduplicated"] is True
    assert again["proposal"]["proposal_id"] == made["proposal"]["proposal_id"]
    assert len([p for p in reopened.all() if p.continuation_of]) == 1
