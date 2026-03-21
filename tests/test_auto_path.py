"""One way for an order to be placed, whoever decided it (docs/todo.md, P3.3).

A plan does not get a road of its own to the venue. What it decides becomes a proposal like any
other and is approved by ``Service.autonomous_reductions`` under the name of what it was, through
``Service.approve`` and ``execution.execute`` -- the path a person's approval takes, with the same
revalidation, reservation and reconciliation. ``tests/test_money_paths.py`` holds that nothing
else in the package can reach a venue; this file holds what follows from sharing the path:

* a conclusion the plan may act on is written once, by the plan, and not also by the research
  run that reached it;
* while a plan is in force, a purchase that is not the plan's own waits for a person, and so
  does one left over from a strategy version that no longer opens positions;
* a sale waiting for a person never holds back a stop on shares code holds, and neither can
  sell a share twice.

The account is the simulated one: the real account's code and rules over a scripted venue.
"""

import json
import shutil

import pytest

from agentic_trading import autonomy, cli, diagnosis, risk_scenarios
from agentic_trading.auto import positions, store, trader
from agentic_trading.execution_scenarios import _proposal
from agentic_trading.policy import PolicyDecision, decide
from agentic_trading.profile import load_profiles
from agentic_trading.proposals import EXECUTED, PROPOSED
from agentic_trading.service import Service
from tests.auto_market import scripted, today
from tests.conftest import ROOT, copy_example_ledger
from tests.test_auto_lifecycle import (  # noqa: F401 - the shared simulated desk and its fixtures
    MICRO,
    _human_buy,
    bought,
    desk,
    quick_desk,
    sold,
)


def _written_elsewhere(desk, symbol="BND", *, action="BUY", dollars=100.0, version=None,  # noqa: F811
                       needs_person=False, quantity=None):
    """A proposal that is in the queue but is not the plan's: written by a research run, say."""
    made = _proposal(ticker=symbol, bucket=MICRO, action=action, notional=dollars, status=PROPOSED,
                     price=desk.prices[symbol], confidence=95)
    made.trade_date = today()
    made.evidence["price_date"] = today()
    made.reference_price_date = today()
    made.positions_fingerprint = None
    made.requires_human_approval = needs_person
    made.strategy_version = version
    made.sell_quantity = quantity
    made.authority_version = autonomy.note_version(desk.sim.profiles(), desk.sim.state_db())
    desk.sim._store(desk.sim.profiles()).save(made)
    return made


def _approve_by_hand(desk, proposal):  # noqa: F811
    return desk.sim.approve(
        proposal.proposal_id, identity="owner", version=proposal.version(),
        request_id=f"by-hand-{proposal.proposal_id}", authority_version=proposal.authority_version,
        terms={"ticker": proposal.ticker, "bucket": proposal.bucket, "action": proposal.action,
               "notional_usd": proposal.notional_usd, "sell_quantity": proposal.sell_quantity})


# ---------------------------------------------------------------- what code may approve
def test_the_plans_own_purchase_carries_what_it_was_approved_under(desk):  # noqa: F811
    opened = desk.buy()
    authority = autonomy.note_version(desk.sim.profiles(), desk.sim.state_db())
    assert opened.status == EXECUTED and opened.client_order_id
    assert opened.strategy_version == desk.active["opening_version"]
    assert opened.authority_version == authority
    assert (opened.approved_via, opened.approved_by) == (autonomy.AUTONOMOUS_VIA, "autonomy:open")
    assert opened.approval_request_id, "an immutable approval record, like a person's"
    # And the order it became is the one order on record for it.
    assert [o.client_order_id for o in desk.broker._orders.values()] == [opened.client_order_id]


def test_a_purchase_from_outside_the_plan_waits_for_a_person_while_a_plan_is_in_force(desk):  # noqa: F811
    outside = _written_elsewhere(desk, "BND")
    report = desk.sim.autonomous_reductions()
    assert report["considered"] == 1 and report["approved"] == []
    why = ("an automatic-trading plan is in force for this account; a purchase from outside it "
           "waits for a person")
    assert report["declined"] == [{"proposal_id": outside.proposal_id, "reasons": [why]}]
    assert desk.held("BND") == 0.0 and bought(desk.broker, "BND") == 0.0
    # Waiting is all it is: the person can still approve it, down the same path.
    still = next(p for p in desk.proposals() if p.proposal_id == outside.proposal_id)
    assert still.status == PROPOSED
    answer = _approve_by_hand(desk, still)
    assert answer["submitted"] is True and desk.held("BND") > 0
    # It is theirs, not code's: no strategy version, so nothing code may later sell.
    assert desk.managed("BND").quantity == 0.0


def test_with_no_plan_in_force_the_plan_rule_declines_nothing(desk):  # noqa: F811
    """The switches a person turned on before there were plans go on meaning what they meant."""
    store.withdraw_plan(desk.sim.state_db(), identity="owner", reason="stopping")
    outside = _written_elsewhere(desk, "BND")
    report = desk.sim.autonomous_reductions()
    reasons = [text for row in report["declined"] for text in row["reasons"]]
    assert not any("automatic-trading plan" in text or "no longer opens" in text for text in reasons)
    assert report["considered"] == 1
    assert outside.proposal_id in [row["proposal_id"] for row in report["approved"] + report["declined"]]


def test_a_purchase_written_under_a_version_that_no_longer_opens_waits_for_a_person(quick_desk):  # noqa: F811
    desk = quick_desk  # noqa: F811
    desk.research("VTI", confidence=90)
    written = desk.tick(approve=False)
    waiting = desk.proposals()[0]
    first = desk.active["opening_version"]
    assert written.signals[0]["diagnosis"]["code"] == "proposal_automatic"
    assert (waiting.status, waiting.strategy_version) == (PROPOSED, first)

    # The strategy is adjusted before anything approved it: a different version opens from here.
    changed = {**desk.active["strategy"], "stop_loss_pct": 6.0}
    updated = store.adjust_strategy(desk.sim.state_db(), desk.sim.profiles(), changed,
                                    identity="owner", reason="a tighter stop")
    second = updated["opening_version"]
    assert second != first
    report = desk.sim.autonomous_reductions()
    assert report["approved"] == []
    assert report["declined"][0]["reasons"] == [
        f"strategy version {first} no longer opens positions ({second} does)"]
    assert desk.held("VTI") == 0.0 and desk.broker._orders == {}

    # With the plan withdrawn altogether the same proposal is still not code's to approve.
    store.withdraw_plan(desk.sim.state_db(), identity="owner", reason="stopping")
    again = desk.sim.autonomous_reductions()
    assert again["approved"] == []
    assert again["declined"][0]["reasons"] == [
        f"strategy version {first} no longer opens positions; no plan is in force"]


def test_a_purchase_someone_else_has_waiting_is_not_doubled_by_the_plan(desk):  # noqa: F811
    theirs = _written_elsewhere(desk, "VTI", needs_person=True)
    run_id = desk.research("VTI", confidence=90)
    held_back = desk.tick().signals[0]
    assert held_back["diagnosis"]["code"] == "order_in_progress"
    assert [p.proposal_id for p in desk.proposals()] == [theirs.proposal_id]
    # Not written off: it is looked at again while the research is fresh.
    assert trader.seen_signals(desk.sim.state_db())[run_id]["final"] is False


# ---------------------------------------------------------------- a stop does not queue behind a person
def test_a_sale_waiting_for_a_person_does_not_hold_back_a_stop(desk):  # noqa: F811
    desk.buy()
    quantity = desk.held()
    # Research in the ordinary pipeline said sell, and that is waiting in the queue for a person.
    theirs = _written_elsewhere(desk, "VTI", action="SELL", needs_person=True, quantity=quantity)
    book = trader.read_book(desk.sim, desk.sim.profiles(), store.active_plan(desk.sim.state_db()),
                            today())
    assert trader.in_progress(book, "VTI") is True, "a purchase would wait behind it"
    assert trader.in_progress(book, "VTI", leaving=True) is False, "a stop does not"

    desk.move("VTI", 0.88)
    stopped = desk.tick()
    assert stopped.exits[0]["exit"]["reason"] == positions.STOP_LOSS
    closing = next(p for p in desk.proposals() if p.action == "SELL" and p.strategy_version)
    assert closing.status == EXECUTED and desk.held() == 0.0
    # The person's sale is still theirs to decide, and deciding it now cannot sell a share twice.
    left = next(p for p in desk.proposals() if p.proposal_id == theirs.proposal_id)
    assert left.status == PROPOSED
    late = _approve_by_hand(desk, left)
    assert late["submitted"] is False
    assert sold(desk.broker) == pytest.approx(bought(desk.broker), abs=1e-6)


def test_an_exit_code_already_has_working_is_not_written_twice(desk):  # noqa: F811
    desk.buy()
    desk.move("VTI", 0.88)
    autonomy.trip(desk.sim.profiles(), "a person withdrew it to look at something")
    for _ in range(3):
        desk.tick()
    # Autonomy is withdrawn, so the exit waits for a person -- once, however many passes ran.
    assert [p.status for p in desk.proposals() if p.action == "SELL"] == [PROPOSED]


# ---------------------------------------------------------------- the research run steps aside
def _account(tmp_path, monkeypatch, cash=1000.0):
    """The Agentic account in a temporary directory, with every market read scripted."""
    root = tmp_path / "account"
    (root / "state").mkdir(parents=True)
    (root / "runs").mkdir()
    shutil.copytree(ROOT / "config", root / "config")
    ledger = copy_example_ledger("micro_live_portfolio.example.yaml",
                                 root / "state" / "micro_live_portfolio.yaml", as_of=today())
    ledger.write_text(ledger.read_text().replace("cash_usd: 100", f"cash_usd: {cash:g}"))
    scripted(monkeypatch, dict(risk_scenarios.PRICES))
    monkeypatch.setattr(cli, "upstream_identity_lookup", risk_scenarios.identity_lookup)
    return root


class _Graph:
    """The research graph, standing in for ten model calls: it concludes Buy and writes reports."""

    def __init__(self, _profiles, **kwargs):
        self.run_dir = kwargs["run_dir"]
        self.config = {"results_dir": str(self.run_dir / "upstream")}

    def propagate(self, ticker, _trade_date, **_kwargs):
        return ({"market_report": f"market report for {ticker}: the trend is intact.",
                 "news_report": f"news report for {ticker}: nothing material."}, "Buy")

    def save_reports(self, _state, _ticker, where):
        where.mkdir(parents=True, exist_ok=True)
        return where


def _concluding_buy(monkeypatch, confidence=88):
    def policy(profiles, _state, *, ticker, instrument_type, bucket, pm_rating, has_position, **_kw):
        action, size, reasons, codes = decide(
            pm_rating, confidence=confidence, llm_action="BUY", bucket=bucket,
            instrument_type=instrument_type, profiles=profiles, has_position=has_position)
        return PolicyDecision(final_action=action, size_hint=size, pm_rating=pm_rating,
                              llm_action="BUY", confidence=confidence, reasons=reasons,
                              proposal={"data_quality": "good", "missing_information": []},
                              reason_codes=codes)

    monkeypatch.setattr(cli, "AgenticTradingGraph", _Graph)
    monkeypatch.setattr(cli, "run_policy", policy)


def _records(root):
    return [json.loads(path.read_text()) for path in sorted((root / "runs").glob("*/decision_record.json"))]


def test_a_conclusion_the_plan_may_act_on_is_left_for_the_plan_to_write(tmp_path, monkeypatch,
                                                                         offline_market_reads):
    # $500, so that the run's own order (a share of the account) is inside the tier's ticket:
    # at $1,000 the ordinary pipeline is refused for size, which is P1.3's subject and not this.
    root = _account(tmp_path, monkeypatch, cash=500.0)
    _concluding_buy(monkeypatch)
    profiles = load_profiles(root / "config")
    service = Service(root / "config")

    # No plan: the run writes its proposal for a person, as it always has.
    assert cli.run_one(profiles, "VTI", today(), MICRO, "etf", root / "runs", debug=False) == 0
    before = _records(root)[-1]
    assert before["hard_risk"]["verdict"] == "APPROVE" and before["proposal_id"]
    assert "auto_plan" not in before
    first = service._store(profiles).all()
    assert [(p.ticker, p.action, p.strategy_version) for p in first] == [("VTI", "BUY", None)]

    # A plan is put in force for this account. (At the level of the record: what stands in front
    # of doing that for the real account is another file's subject.)
    document = service.auto_plan(capital_usd=500.0)
    active = store.confirm_plan(service.state_db(profiles), profiles, document, identity="owner",
                                scope="live")
    assert "VXUS" in active["strategy"]["universe"]
    assert cli.run_one(profiles, "VXUS", today(), MICRO, "etf", root / "runs", debug=False) == 0
    after = next(r for r in _records(root) if r["ticker"] == "VXUS")
    # The same conclusion, the same verdict from the Hard Risk Layer -- and no proposal from the
    # run: the plan's pass writes it, with the strategy version the position will answer to.
    assert after["hard_risk"]["verdict"] == "APPROVE" and after["policy"]["final_action"] == "BUY"
    assert after.get("proposal_id") is None and after.get("proposal") is None
    assert after["auto_plan"] == {"deferred": True, "plan_id": active["plan_id"],
                                  "opening_version": active["opening_version"]}
    assert after["diagnosis"]["code"] == "deferred_to_plan"
    assert "自动交易方案" in after["diagnosis"]["summary"]["zh"] and after["diagnosis"]["summary"]["en"]
    assert [p.ticker for p in service._store(profiles).all()] == ["VTI"]

    # A fund the plan may not buy is none of the plan's business: written here, for a person.
    assert "VOO" not in active["strategy"]["universe"]
    assert cli.run_one(profiles, "VOO", today(), MICRO, "etf", root / "runs", debug=False) == 0
    outside = next(r for r in _records(root) if r["ticker"] == "VOO")
    assert "auto_plan" not in outside
    assert outside["hard_risk"]["verdict"] == "APPROVE" and outside["proposal_id"]
    assert [(p.ticker, p.strategy_version) for p in service._store(profiles).all()] == [
        ("VTI", None), ("VOO", None)]


def test_only_a_purchase_in_the_plans_own_account_is_deferred(tmp_path, monkeypatch,
                                                              offline_market_reads):
    root = _account(tmp_path, monkeypatch)
    profiles = load_profiles(root / "config")
    service = Service(root / "config")
    db = service.state_db(profiles)
    assert cli._deferred_to_plan(db, MICRO, "VTI", "BUY") is None            # no plan
    active = store.confirm_plan(db, profiles, service.auto_plan(capital_usd=1000.0),
                                identity="owner", scope="live")
    assert cli._deferred_to_plan(db, MICRO, "vti", "BUY")["opening_version"] == active["opening_version"]
    assert cli._deferred_to_plan(db, MICRO, "VTI", "ADD") is not None
    for bucket, ticker, action in (("core", "VTI", "BUY"),                    # another account
                                   (MICRO, "AAPL", "BUY"),                    # not the plan's fund
                                   (MICRO, "VTI", "SELL"), (MICRO, "VTI", "REDUCE"),
                                   (MICRO, "VTI", "NO_TRADE"), (MICRO, "VTI", None)):
        assert cli._deferred_to_plan(db, bucket, ticker, action) is None, (bucket, ticker, action)
    store.withdraw_plan(db, identity="owner", reason="stopping")
    assert cli._deferred_to_plan(db, MICRO, "VTI", "BUY") is None


def test_a_deferred_conclusion_reads_as_handed_on_not_as_a_refusal():
    record = {"upstream_decision": {"portfolio_manager_signal": "Buy"},
              "policy": {"final_action": "BUY", "confidence": 88},
              "hard_risk": {"verdict": "APPROVE", "rules": []},
              "final_recommendation": {"action": "BUY"},
              "auto_plan": {"deferred": True, "plan_id": "plan-x", "opening_version": "sv-x"}}
    outcome = diagnosis.classify_record(record)
    assert (outcome.category, outcome.code) == (diagnosis.NOTHING_TO_DO, "deferred_to_plan")
    # Without the mark it is what it was: a proposal waiting.
    plain = diagnosis.classify_record({k: v for k, v in record.items() if k != "auto_plan"})
    assert plain.code != "deferred_to_plan"
