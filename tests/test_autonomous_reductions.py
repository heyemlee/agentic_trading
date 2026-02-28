"""The pass that takes the reductions the gate permits. Fully offline.

Built on Service.approve rather than beside it, so what is tested here is mostly that the pass
declines correctly and gets out of the way: an autonomous approval is a real approval and takes
the same locked, recorded, revalidated path as a person's.
"""

import uuid

import pytest
import yaml

from agentic_trading import autonomy
from agentic_trading.broker import SimulatedBroker
from agentic_trading.execution_scenarios import PRICES, TRADE_DATE, _profiles, _proposal
from agentic_trading.proposals import PROPOSED, ProposalStore
from agentic_trading.service import Service, ServiceConflict
from agentic_trading.service.shapes import MarketStatus

MICRO = "micro_live"
#: The authority version of the moment, held constant: what is under test is the pass, and a
#: proposal born under the same authority is the ordinary case. One test below moves it.
AUTHORITY = "authority-under-test"


@pytest.fixture
def setup(tmp_path, monkeypatch):
    profiles = _profiles(tmp_path)
    ledger = tmp_path / "micro_live_portfolio.yaml"
    ledger.write_text(yaml.safe_dump({
        "as_of": TRADE_DATE, "currency": "USD", "buckets": {MICRO: {"cash_usd": 40.0}},
        "positions": [{"symbol": "VTI", "bucket": MICRO, "quantity": 0.2,
                       "cost_basis_per_share": 370.0}], "recent_opens": []}), encoding="utf-8")
    profiles.risk["micro_live_execution"] = {
        **(profiles.risk.get("micro_live_execution") or {}),
        "enabled": True, "ledger_file": str(ledger)}
    profiles.risk["buckets"][MICRO]["autonomy"] = {
        "reduce": {"enabled": True, "min_confidence": 80, "max_positions_reduced_per_day": 1}}

    store = ProposalStore(tmp_path / "proposals.json")
    broker = SimulatedBroker(prices=dict(PRICES), cash=40.0)
    service = Service()
    monkeypatch.setattr(service, "profiles", lambda: profiles)
    monkeypatch.setattr(service, "_store", lambda _: store)
    monkeypatch.setattr(service, "_broker", lambda *_: broker)
    monkeypatch.setattr(service, "market_status", lambda: MarketStatus(is_open=True))
    monkeypatch.setattr(autonomy, "note_version", lambda *_: AUTHORITY)
    return service, profiles, store


def reduction(**over):
    kw = {"bucket": MICRO, "action": "REDUCE", "ticker": "VTI", "notional": 0.0,
          "limit_class": "broad_diversified_etf"}
    kw.update(over)
    p = _proposal(**kw)
    p.requires_human_approval = False
    p.sell_quantity = 0.1
    p.status = PROPOSED
    p.authority_version = AUTHORITY
    return p


@pytest.mark.parametrize("born_under", ["an-earlier-authority", None])
def test_a_proposal_born_under_another_authority_waits_for_a_person(setup, monkeypatch, born_under):
    """Tightened since the research, loosened since, or never recorded: code does not weigh
    which, it only acts where nothing has moved. The proposal stays in the queue for a person."""
    service, _, store = setup
    p = reduction()
    p.authority_version = born_under
    store.save(p)
    monkeypatch.setattr(service, "_broker", lambda *_: pytest.fail("no venue session for a declined proposal"))
    report = service.autonomous_reductions()
    assert report["approved"] == [] and len(report["declined"]) == 1
    assert "authority changed since research" in report["declined"][0]["reasons"][0]
    assert store.get(p.proposal_id).status == PROPOSED


def test_a_closed_market_costs_nothing_and_opens_nothing(setup, monkeypatch):
    service, _, store = setup
    store.save(reduction())
    monkeypatch.setattr(service, "market_status", lambda: MarketStatus(is_open=False))
    monkeypatch.setattr(service, "_broker", lambda *_: pytest.fail("no venue session when closed"))
    assert service.autonomous_reductions()["skipped"] == "the market is closed"


def test_nothing_happens_while_the_switch_is_off(setup, monkeypatch):
    service, profiles, store = setup
    profiles.risk["buckets"][MICRO]["autonomy"]["reduce"]["enabled"] = False
    store.save(reduction())
    monkeypatch.setattr(service, "_broker", lambda *_: pytest.fail("no venue session when off"))
    report = service.autonomous_reductions()
    assert report["skipped"] == "no autonomy is switched on" and report["considered"] == 0


def test_a_withdrawn_autonomy_says_why_and_does_nothing(setup):
    service, profiles, store = setup
    store.save(reduction())
    autonomy.trip(profiles, "an order we cannot account for")
    report = service.autonomous_reductions()
    assert "needs a manual reset" in report["skipped"]
    assert "an order we cannot account for" in report["skipped"]


def test_only_proposals_the_engine_marked_eligible_are_considered(setup):
    service, _, store = setup
    gated = reduction()
    gated.requires_human_approval = True          # what the engine stamps for everything else
    store.save(gated)
    assert service.autonomous_reductions()["considered"] == 0


def test_an_opening_proposal_is_declined_while_only_reductions_are_armed(setup):
    service, _, store = setup
    opening = reduction(action="BUY", notional=35.0)
    opening.sell_quantity = None
    store.save(opening)
    report = service.autonomous_reductions()
    assert report["approved"] == []
    assert "autonomy.open.enabled is not true" in report["declined"][0]["reasons"][-1]


def test_low_conviction_is_declined_and_left_for_a_person(setup):
    service, _, store = setup
    proposal = reduction(confidence=70)
    store.save(proposal)
    report = service.autonomous_reductions()
    assert report["approved"] == []
    assert "a person decides this one" in report["declined"][0]["reasons"][-1]
    # Declined is not rejected: the proposal is untouched and still waiting.
    assert store.get(proposal.proposal_id).status == PROPOSED


def test_the_daily_budget_is_spent_across_passes(setup, monkeypatch):
    service, _profiles, store = setup
    store.save(reduction())
    # Stand in for a reduction already taken today rather than executing one, so this test is
    # about the budget and not about the venue.
    monkeypatch.setattr(autonomy, "reduced_today", lambda *a, **k: 1)
    report = service.autonomous_reductions()
    assert report["approved"] == []
    assert "at the limit of 1" in report["declined"][0]["reasons"][-1]


def test_a_person_winning_the_race_is_recorded_and_not_repaired(setup, monkeypatch):
    service, _, store = setup
    proposal = reduction()
    store.save(proposal)

    def approve(*_a, **_k):
        raise ServiceConflict("proposal version or status changed; review it again")

    monkeypatch.setattr(service, "approve", approve)
    report = service.autonomous_reductions()
    assert report["approved"] == []
    assert "superseded" in report["declined"][0]["reasons"][0]


def test_an_unaccountable_outcome_withdraws_autonomy_and_stops_the_pass(setup, monkeypatch):
    service, profiles, store = setup
    store.save(reduction())
    store.save(reduction(ticker="BND"))
    monkeypatch.setattr(service, "approve", lambda *a, **k: {
        "outcome": "SUBMIT_UNCERTAIN", "submitted": False, "error": "venue timed out"})
    report = service.autonomous_reductions()
    assert len(report["approved"]) == 1, "the pass stops at the first unaccountable outcome"
    assert report["approved"][0]["autonomy_withdrawn"] is True
    assert autonomy.load_downgrade(profiles).tripped
    assert "venue timed out" in autonomy.load_downgrade(profiles).reason


def test_an_ordinary_not_now_leaves_autonomy_armed(setup, monkeypatch):
    service, profiles, store = setup
    store.save(reduction())
    monkeypatch.setattr(service, "approve", lambda *a, **k: {
        "outcome": "STALE", "submitted": False, "error": None})
    report = service.autonomous_reductions()
    assert report["approved"][0]["autonomy_withdrawn"] is False
    assert autonomy.is_armed(profiles, MICRO)


def test_the_approval_is_recorded_under_its_own_identity(setup, monkeypatch):
    service, _, store = setup
    proposal = reduction()
    store.save(proposal)
    seen = {}

    def approve(proposal_id, note=None, **kw):
        seen.update(kw, note=note)
        return {"outcome": "SUBMITTED", "submitted": True, "error": None}

    monkeypatch.setattr(service, "approve", approve)
    service.autonomous_reductions()
    assert seen["identity"] == "autonomy:reduce", "an autonomous approval must be tellable apart"
    assert seen["terms"]["action"] == "REDUCE" and seen["terms"]["bucket"] == MICRO
    assert uuid.UUID(seen["request_id"]), "the approval protocol needs a fresh request id"
    assert "confidence" in seen["note"], "the gate's reasoning is kept with the approval"
