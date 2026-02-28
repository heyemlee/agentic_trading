"""The effective authority: one object, from every rule that decides what may happen to the
real-money account, and the version that lets an approval notice the rules moved. Offline.

Three readers used to decide "does this need a person" from three slices of the configuration:
preflight PF04, autonomy.permits and Hard Risk. The tests here hold that the one object agrees
with the gate wherever they overlap, and names the disagreement as data where they cannot.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
import yaml

from agentic_trading import autonomy
from agentic_trading import config_changes as cc
from agentic_trading.broker import SimulatedBroker
from agentic_trading.execution_scenarios import PRICES, TRADE_DATE, _profiles, _proposal
from agentic_trading.micro_live import run_preflight
from agentic_trading.proposals import PROPOSED, ProposalStore
from agentic_trading.service import Service, ServiceConflict, ServiceError
from agentic_trading.service.shapes import MarketStatus
from agentic_trading.state_db import StateDB

MICRO = "micro_live"
OPEN = {"enabled": False, "max_pct_of_account": 20,
        "classes": ["broad_diversified_etf", "bond_or_cash_like_etf"], "max_new_positions_per_day": 1}
REDUCE = {"enabled": False, "min_confidence": 80, "max_positions_reduced_per_day": 1}


@pytest.fixture
def world(profiles_with_state):
    """The deployment's own rules in a temp directory, with real money switched on in memory.

    On in memory, deliberately: whether this machine is armed today is an operational fact the
    suite must not depend on either way (see test_api.disarm), and the authority of a disarmed
    deployment is the one uninteresting case, where nothing is permitted to anyone.
    """
    profiles = profiles_with_state
    profiles.risk["micro_live_execution"] = {
        **(profiles.risk.get("micro_live_execution") or {}),
        "enabled": True, "execution_mode": "human_approval", "allow_autonomous_opening": False}
    profiles.risk["buckets"][MICRO]["autonomy"] = {"open": dict(OPEN), "reduce": dict(REDUCE)}
    return profiles, StateDB(profiles.project_dir / "state" / "proposals.json")


def authority(profiles, db, **over):
    inputs = autonomy.authority_inputs(profiles, db)
    version = autonomy.version_of(inputs)
    history = autonomy.record_version(db, inputs, version)
    kw = {"inputs": inputs, "version": version, "history": history, "proposals": [],
          "preflight": None, "preflight_stale": True, "ladder_halted": False, "pending_changes": []}
    kw.update(over)
    return autonomy.effective_authority(profiles, **kw)


def codes(side):
    return {b["code"] for b in side["blocked_by"]}


# ------------------------------------------------------------------ the object
def test_todays_rules_put_every_action_in_front_of_a_person(world):
    profiles, db = world
    a = authority(profiles, db)
    assert a["real_money_enabled"] and a["execution_mode"] == "human_approval"
    assert a["account"] == {"scope": "agentic", "account_id": MICRO, "venue": "robinhood",
                            "is_real_money": True}
    for action in autonomy.ACTIONS:
        block = a["actions"][action]
        assert block["mode"] == "human_approval", action
        assert block["human_approval"] == {"permitted": True, "blocked_by": []}
        assert block["autonomous"]["permitted"] is False
    assert {"not_enabled", "no_min_confidence"} <= codes(a["actions"]["BUY"]["autonomous"])
    assert codes(a["actions"]["ADD"]["autonomous"]) == codes(a["actions"]["BUY"]["autonomous"])
    assert "not_enabled" in codes(a["actions"]["REDUCE"]["autonomous"])
    assert a["actions"]["REDUCE"]["limits"]["max_fraction_of_holding"] == 0.5
    # Closing a position outright is the one thing that is never code's, and nothing else is
    # said against it: the stage and the withdrawal flag are about what code may do.
    assert codes(a["actions"]["SELL"]["autonomous"]) == {"never_autonomous"}
    assert a["actions"]["SELL"]["limits"]["quantity_basis"].startswith("full ledger holding")
    names = {c["code"] for c in a["consistency"]}
    assert {"open_without_min_confidence", "execution_policy_autonomy_unread"} <= names
    micro = profiles.risk["micro_live_execution"]
    assert a["limits"]["capital"]["max_capital_usd"] == micro["max_capital_usd"]
    assert a["limits"]["tiers"]["low"]["max_order_usd"] == 200
    assert a["limits"]["event_blackouts"]["macro"]["maintained_through"] == "2026-12-10"
    assert a["preflight"] == {"ready": None, "blocking": [], "checked_at": None, "stale": True}


@pytest.mark.parametrize("arrange, code", [
    (lambda p: autonomy.halt_file(p).write_text("2026-09-18T20:00:00+00:00 looking\n"), "kill_switch"),
    (lambda p: p.risk["micro_live_execution"].__setitem__("enabled", False), "real_money_disabled"),
    (lambda p: p.bucket_modes.__setitem__(MICRO, "recommend_only"), "mode_recommend_only"),
])
def test_what_stops_a_person_stops_code_too(world, arrange, code):
    profiles, db = world
    arrange(profiles)
    a = authority(profiles, db)
    for action in autonomy.ACTIONS:
        block = a["actions"][action]
        assert block["mode"] == "blocked", action
        assert block["human_approval"]["permitted"] is False and code in codes(block["human_approval"])
        assert block["autonomous"]["permitted"] is False and code in codes(block["autonomous"])


def test_a_failing_preflight_and_a_halted_ladder_block_everyone(world):
    profiles, db = world
    preflight = run_preflight(profiles, None)          # no venue: PF01 and PF02 cannot pass
    assert not preflight.ready
    a = authority(profiles, db, preflight=preflight, preflight_stale=True, ladder_halted=True)
    human = a["actions"]["REDUCE"]["human_approval"]
    assert human["permitted"] is False and {"preflight", "ladder_halt"} <= codes(human)
    assert a["preflight"]["ready"] is False and {"PF01", "PF02"} <= set(a["preflight"]["blocking"])
    assert any("reading is stale" in b["detail"] for b in human["blocked_by"] if b["code"] == "preflight")


@pytest.mark.parametrize("pf04_human", [True, False])
@pytest.mark.parametrize("open_enabled", [True, False])
@pytest.mark.parametrize("has_floor", [True, False])
def test_preflight_the_gate_and_the_authority_agree_or_the_disagreement_is_named(
        world, pf04_human, open_enabled, has_floor):
    """The eight combinations of PF04's inputs, the open switch and its confidence floor.

    PF04 reads micro_live_execution alone and the gate reads buckets.*.autonomy alone, so they
    can pass and permit independently. The authority object follows the gate, which is what
    would act, and names every combination where preflight would tell a different story.
    """
    profiles, db = world
    profiles.stage = 6                                  # the one stage that allows autonomy at all
    profiles.risk["micro_live_execution"]["allow_autonomous_opening"] = not pf04_human
    opening = profiles.risk["buckets"][MICRO]["autonomy"]["open"]
    opening["enabled"] = open_enabled
    if has_floor:
        opening["min_confidence"] = 80
    pf04 = next(c for c in run_preflight(profiles, None).checks if c.check_id == "PF04")
    assert pf04.passed is pf04_human

    a = authority(profiles, db)
    expected = open_enabled and has_floor
    assert a["actions"]["BUY"]["autonomous"]["permitted"] is expected
    assert a["actions"]["BUY"]["mode"] == ("autonomous" if expected else "human_approval")
    gate = autonomy.permits(profiles, bucket=MICRO, action="BUY", confidence=99,
                            limit_class="broad_diversified_etf")
    assert gate.permitted is expected, gate.reasons
    names = {c["code"] for c in a["consistency"]}
    assert ("open_enabled_unseen_by_preflight" in names) is (open_enabled and pf04_human)
    assert ("open_without_min_confidence" in names) is (not has_floor)


def test_autonomy_switched_on_below_the_stage_that_allows_it_is_named(world):
    profiles, db = world
    profiles.risk["buckets"][MICRO]["autonomy"]["reduce"]["enabled"] = True
    a = authority(profiles, db)
    assert profiles.stage < 6
    assert "stage" in codes(a["actions"]["REDUCE"]["autonomous"])
    assert "autonomy_not_gated_by_stage" in {c["code"] for c in a["consistency"]}


def test_a_change_still_cooling_is_shown_against_the_switch_it_would_move(world):
    profiles, db = world
    pending = [{"id": "chg_1", "file": "risk_profile.yaml", "status": cc.PENDING,
                "paths": ["buckets.micro_live.autonomy.reduce.enabled"],
                "apply_after": "2026-09-19T20:58:00+00:00"}]
    a = authority(profiles, db, pending_changes=pending)
    reduce = a["actions"]["REDUCE"]["autonomous"]
    assert "pending_change" in codes(reduce) and "chg_1" in next(
        b["detail"] for b in reduce["blocked_by"] if b["code"] == "pending_change")
    assert "pending_change" not in codes(a["actions"]["BUY"]["autonomous"])
    assert a["pending_changes"] == pending


def test_the_kill_switch_reads_since_and_reason_from_the_halt_file(world):
    profiles, _ = world
    assert autonomy.kill_switch_state(profiles) == {"engaged": False, "since": None, "reason": None}
    autonomy.halt_file(profiles).write_text("2026-09-18T20:00:00+00:00 paused from the interface\n")
    assert autonomy.kill_switch_state(profiles) == {
        "engaged": True, "since": "2026-09-18T20:00:00+00:00", "reason": "paused from the interface"}


# ------------------------------------------------------------------ the version
def test_the_version_moves_with_the_inputs_and_the_history_says_which_way(world):
    profiles, db = world
    v0 = autonomy.note_version(profiles, db)
    assert autonomy.note_version(profiles, db) == v0 and len(autonomy.version_history(db)) == 1

    autonomy.halt_file(profiles).write_text("2026-09-18T20:00:00+00:00 pause\n")
    v1 = autonomy.note_version(profiles, db)
    assert v1 != v0
    last = autonomy.version_history(db)[-1]
    assert last["version"] == v1 and last["changed_inputs"] == ["halt_file"]
    assert last["classification"] == autonomy.TIGHTENED and last["paths"] == ["kill_switch"]

    autonomy.halt_file(profiles).unlink()
    assert autonomy.note_version(profiles, db) == v0          # the same inputs, the same version
    assert autonomy.version_history(db)[-1]["classification"] == autonomy.LOOSENED

    autonomy.trip(profiles, "an order we cannot account for")
    autonomy.note_version(profiles, db)
    assert autonomy.version_history(db)[-1]["classification"] == autonomy.TIGHTENED
    autonomy.reset(profiles, "looked into it")
    autonomy.note_version(profiles, db)
    assert autonomy.version_history(db)[-1]["classification"] == autonomy.LOOSENED
    assert len(autonomy.version_history(db)) == 5


def test_a_configuration_edit_is_classed_by_the_console_record_or_as_unknown(world):
    profiles, db = world
    autonomy.note_version(profiles, db)
    risk = profiles.config_dir / "risk_profile.yaml"

    risk.write_text(risk.read_text() + "\n# edited by hand\n")
    autonomy.note_version(profiles, db)
    last = autonomy.version_history(db)[-1]
    assert last["changed_inputs"] == ["risk_profile"]
    assert last["classification"] == autonomy.UNKNOWN_CHANGE
    assert last["paths"] == ["risk_profile.yaml: (edited outside the console)"]

    record = {"id": "chg1", "file": "risk_profile.yaml", "status": cc.APPLIED,
              "applied_at": datetime.now(UTC).isoformat(),
              "paths": ["buckets.micro_live.tiers.low.max_order_usd"],
              "changes": [{"path": "buckets.micro_live.tiers.low.max_order_usd",
                           "from": 200, "to": 150, "effect": cc.TIGHTENING}]}
    cc.save(db, [record])
    risk.write_text(risk.read_text() + "# and again\n")
    autonomy.note_version(profiles, db)
    last = autonomy.version_history(db)[-1]
    assert last["classification"] == autonomy.TIGHTENED
    assert last["paths"] == ["buckets.micro_live.tiers.low.max_order_usd"]

    cooling = {**record, "id": "chg2", "status": cc.PENDING, "applied_at": None,
               "apply_after": "2099-01-01T00:00:00+00:00"}
    cc.save(db, [record, cooling])
    autonomy.note_version(profiles, db)
    last = autonomy.version_history(db)[-1]
    assert last["changed_inputs"] == ["pending_changes"]
    assert last["classification"] == autonomy.UNRELATED


def test_the_change_between_two_versions_is_the_sum_of_the_steps():
    history = [{"version": "a", "at": "1", "paths": [], "classification": "unrelated"},
               {"version": "b", "at": "2", "paths": ["kill_switch"], "classification": "tightened"},
               {"version": "c", "at": "3", "paths": ["autonomy.withdrawn"], "classification": "loosened"},
               {"version": "a", "at": "4", "paths": ["kill_switch"], "classification": "loosened"}]
    assert autonomy.authority_change(history, "a", "a") is None
    assert autonomy.authority_change(history, "b", "c")["classification"] == autonomy.LOOSENED
    both = autonomy.authority_change(history, "a", "c")
    assert both["classification"] == autonomy.MIXED and both["at"] == "3"
    assert both["paths"] == ["kill_switch", "autonomy.withdrawn"]
    # A version that recurs is read from its latest standing, not its first.
    assert autonomy.authority_change(history, "c", "a")["classification"] == autonomy.LOOSENED
    legacy = autonomy.authority_change(history, None, "a")
    assert legacy["classification"] == autonomy.UNKNOWN_CHANGE and "not recorded" in legacy["detail"]
    assert autonomy.authority_change(history, "zz", "a")["classification"] == autonomy.UNKNOWN_CHANGE
    assert autonomy.NEEDS_ACKNOWLEDGEMENT == {"loosened", "mixed", "unknown"}


# ------------------------------------------------------------------ the approval
@pytest.fixture
def desk(tmp_path, monkeypatch):
    """A person approving a reduction at a simulated venue, offline, the recovery tests' way."""
    profiles = _profiles(tmp_path)
    ledger = tmp_path / "micro_live_portfolio.yaml"
    ledger.write_text(yaml.safe_dump({
        "as_of": TRADE_DATE, "currency": "USD", "buckets": {MICRO: {"cash_usd": 40.0}},
        "positions": [{"symbol": "VTI", "bucket": MICRO, "quantity": 0.4,
                       "cost_basis_per_share": 370.0}], "recent_opens": []}), encoding="utf-8")
    profiles.risk["micro_live_execution"] = {
        **(profiles.risk.get("micro_live_execution") or {}),
        "enabled": True, "ledger_file": str(ledger)}
    store = ProposalStore(tmp_path / "proposals.json")
    broker = SimulatedBroker(prices=dict(PRICES), cash=40.0)
    service = Service()
    monkeypatch.setattr(service, "profiles", lambda: profiles)
    monkeypatch.setattr(service, "_store", lambda _: store)
    monkeypatch.setattr(service, "_broker", lambda *_: broker)
    monkeypatch.setattr(service, "market_status", lambda: MarketStatus(is_open=True))
    return service, profiles, store


def waiting(store, *, born_under):
    p = _proposal(bucket=MICRO, action="REDUCE", ticker="VTI", notional=0.0,
                  limit_class="broad_diversified_etf")
    p.sell_quantity = 0.1
    p.status, p.approved_at = PROPOSED, None
    p.authority_version = born_under
    store.save(p)
    return p


def args(p, **over):
    return {"identity": "owner", "version": p.version(), "request_id": str(uuid.uuid4()),
            "terms": {"ticker": p.ticker, "bucket": p.bucket, "action": p.action,
                      "notional_usd": p.notional_usd, "sell_quantity": p.sell_quantity}, **over}


def test_a_screen_that_showed_an_older_authority_is_sent_back_to_reload(desk, monkeypatch):
    service, profiles, store = desk
    p = waiting(store, born_under=autonomy.note_version(profiles, store.db))
    monkeypatch.setattr(service, "_broker", lambda *_: pytest.fail("refused before the venue is opened"))
    with pytest.raises(ServiceConflict, match="reload"):
        service.approve(p.proposal_id, **args(p, authority_version="what-the-screen-showed"))
    assert store.get(p.proposal_id).status == PROPOSED and store.get(p.proposal_id).approved_at is None


def test_nothing_moved_needs_no_acknowledgement_and_the_approval_names_the_version(desk):
    service, profiles, store = desk
    current = autonomy.note_version(profiles, store.db)
    p = waiting(store, born_under=current)
    response = service.approve(p.proposal_id, **args(p, authority_version=current))
    assert response["authority_changed"] is None
    approved = next(e for e in store.get(p.proposal_id).history if e["event"] == "approved")
    assert approved["authority_version"] == current and approved["authority_changed"] is None


def test_a_proposal_that_predates_the_record_asks_a_person_to_look(desk):
    service, profiles, store = desk
    p = waiting(store, born_under=None)
    with pytest.raises(ServiceConflict, match="unknown"):
        service.approve(p.proposal_id, **args(p))
    assert store.get(p.proposal_id).status == PROPOSED
    current = autonomy.note_version(profiles, store.db)
    response = service.approve(p.proposal_id, **args(p, acknowledge_authority_change=True,
                                                     authority_version=current))
    assert response["authority_changed"]["classification"] == autonomy.UNKNOWN_CHANGE
    assert "not recorded" in response["authority_changed"]["detail"]


def test_an_acknowledgement_is_of_a_particular_version(desk, monkeypatch):
    """Saying "I looked" without saying at what would cover a change that landed meanwhile."""
    service, profiles, store = desk
    p = waiting(store, born_under=None)
    monkeypatch.setattr(service, "_broker", lambda *_: pytest.fail("refused before the venue is opened"))
    with pytest.raises(ServiceError, match="requires the authority_version"):
        service.approve(p.proposal_id, **args(p, acknowledge_authority_change=True))
    # Acknowledged against a version that has since moved: back to the screen, not through.
    stale = autonomy.note_version(profiles, store.db)
    autonomy.trip(profiles, "moved while the person was reading")
    with pytest.raises(ServiceConflict, match="reload"):
        service.approve(p.proposal_id, **args(p, acknowledge_authority_change=True,
                                              authority_version=stale))
    assert store.get(p.proposal_id).status == PROPOSED


def test_a_tightened_authority_needs_no_acknowledgement_but_a_loosened_one_does(desk):
    service, profiles, store = desk
    p = waiting(store, born_under=autonomy.note_version(profiles, store.db))
    autonomy.trip(profiles, "something we could not account for")     # tightened since
    response = service.approve(p.proposal_id, **args(p))
    assert response["authority_changed"]["classification"] == autonomy.TIGHTENED
    assert response["authority_changed"]["paths"] == ["autonomy.withdrawn"]

    # Born while withdrawn and approved after the restore: loosened, so the person says so.
    q = waiting(store, born_under=autonomy.note_version(profiles, store.db))
    autonomy.reset(profiles, "looked into it")
    with pytest.raises(ServiceConflict, match="loosened"):
        service.approve(q.proposal_id, **args(q))
    assert store.get(q.proposal_id).status == PROPOSED
    current = autonomy.note_version(profiles, store.db)
    response = service.approve(q.proposal_id, **args(q, acknowledge_authority_change=True,
                                                     authority_version=current))
    assert response["authority_changed"]["classification"] == autonomy.LOOSENED
    assert response["authority_changed"]["paths"] == ["autonomy.withdrawn"]
