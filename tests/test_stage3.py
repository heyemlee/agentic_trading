"""Stage 3: proposal lifecycle, freshness, revalidation and paper execution. Fully offline."""

import pytest

from agentic_trading.broker import SimulatedBroker
from agentic_trading.broker.base import BrokerRejection, DuplicateOrder, OrderRequest
from agentic_trading.proposals import (
    EXPIRED,
    STALE,
    VALID,
    ProposalStore,
    client_order_id,
    positions_fingerprint,
)
from agentic_trading.stage3_scenarios import (
    _proposal,
    build_cases,
    run_case,
    run_idempotency_case,
    run_interrupted_session_case,
)


@pytest.mark.parametrize("case", build_cases(), ids=lambda c: c.key)
def test_stage3_case(case):
    outcome = run_case(case)
    assert outcome.outcome == case.expect_outcome, (case.title, outcome.detail)
    assert outcome.submitted == case.expect_submitted, case.title
    if not case.expect_submitted:
        assert outcome.orders_at_broker == 0, f"{case.title}: an order reached the broker"


def test_duplicate_submission_creates_exactly_one_order():
    outcome = run_idempotency_case()
    assert outcome.ok and outcome.orders_at_broker == 1


def test_interrupted_claude_session_leaves_no_proposal_and_no_order():
    outcome = run_interrupted_session_case()
    assert outcome.ok and outcome.orders_at_broker == 0


def test_freshness_windows_differ_by_strategy(profiles):
    core = _proposal(strategy="long_only_diversified", age_minutes=120)
    event = _proposal(strategy="event_driven", age_minutes=120)
    momentum = _proposal(strategy="momentum", age_minutes=120)
    assert core.freshness(profiles)[0] == VALID          # 2 hours is nothing for a core thesis
    assert momentum.freshness(profiles)[0] == EXPIRED    # 90 minute lifetime
    assert event.freshness(profiles)[0] == EXPIRED       # 45 minute lifetime
    assert core.freshness_class(profiles) == "core_conservative"
    assert event.freshness_rules(profiles)["max_price_drift_pct"] == 1.0


def test_core_proposal_becomes_stale_before_it_expires(profiles):
    assert _proposal(age_minutes=1000).freshness(profiles)[0] == STALE
    assert _proposal(age_minutes=3000).freshness(profiles)[0] == EXPIRED


def test_approval_itself_expires(profiles):
    fresh = _proposal(approved_minutes_ago=5)
    old = _proposal(approved_minutes_ago=180)
    assert fresh.approval_expired(profiles)[0] is False
    assert old.approval_expired(profiles)[0] is True


def test_client_order_id_is_deterministic_per_attempt(profiles):
    p = _proposal()
    assert client_order_id(profiles, p, 1) == client_order_id(profiles, p, 1)
    assert client_order_id(profiles, p, 1) != client_order_id(profiles, p, 2)
    assert client_order_id(profiles, p, 1).startswith("ct-")


def test_broker_refuses_a_duplicate_client_order_id():
    broker = SimulatedBroker(prices={"VTI": 376.88}, cash=3000.0)
    req = OrderRequest(symbol="VTI", side="buy", client_order_id="ct-x-1", notional=100.0,
                       order_type="limit", limit_price=378.0)
    broker.submit_order(req)
    with pytest.raises(DuplicateOrder):
        broker.submit_order(req)


def test_broker_refuses_a_short_sale():
    broker = SimulatedBroker(prices={"VTI": 376.88}, cash=3000.0)
    with pytest.raises(BrokerRejection, match="shorting disabled"):
        broker.submit_order(OrderRequest(symbol="VTI", side="sell", client_order_id="ct-y-1",
                                         quantity=1.0, order_type="limit", limit_price=376.0))


def test_positions_fingerprint_detects_a_changed_book():
    assert positions_fingerprint({}) == "(empty)"
    assert positions_fingerprint({"VTI": 1.0}) != positions_fingerprint({"VTI": 2.0})
    assert positions_fingerprint({"VTI": 1.0, "BND": 2.0}) == positions_fingerprint({"BND": 2.0, "VTI": 1.0})


def test_store_round_trip(tmp_path, profiles):
    store = ProposalStore(tmp_path / "proposals.json")
    p = _proposal()
    store.save(p)
    back = store.get(p.proposal_id)
    assert back is not None and back.ticker == "VTI" and back.notional_usd == 875.0
    assert len(store.open_proposals(profiles)) == 1
