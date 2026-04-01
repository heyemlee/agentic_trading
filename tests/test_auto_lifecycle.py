"""Research -> buy -> hold -> reduce -> exit, and every way that can go wrong (docs/todo.md, P1.5).

The whole flow runs in the simulated account: the real account's rules and the real approval and
execution code, under a temporary directory, against a scripted paper venue. Nothing here is a
model of the pipeline -- every order below goes through ``Service.approve`` and
``execution.execute``, is revalidated, reserved, sent, recorded and reconciled by the code that
does that for a person's order.

What is held:

* a position code opens is bound to the strategy version that opened it, and is closed on that
  version's own conditions: a stop, a target, a longest holding period, or research saying sell;
* code sells what code bought and nothing else -- not a person's holding, and not the shares a
  person added to a holding code opened;
* a partial fill, a refused sale, an order nobody can account for and a restart each leave one
  order at the venue at most, never an oversell, and nothing retried blind.
"""

import json
import shutil
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest

from agentic_trading import autonomy, exits, risk_scenarios
from agentic_trading.auto import paper, positions, store, trader
from agentic_trading.portfolio import apply_fill_to_ledger, load_portfolio
from agentic_trading.proposals import (
    CANCELED,
    EXECUTED,
    INVALIDATED,
    PARTIALLY_FILLED,
    PROPOSED,
    REJECTED,
    UNKNOWN,
)
from agentic_trading.service import Service
from agentic_trading.service.auto import PLAN_CREATES
from agentic_trading.service.shapes import MarketStatus
from tests.auto_market import scripted, today, write_research
from tests.conftest import ROOT, copy_example_ledger

MICRO = "micro_live"
VTI = risk_scenarios.PRICES["VTI"]


def quicken(risk_profile, per_day):
    """Let the account open more than one position a day, in a copy of the configuration.

    The real account opens one a day and two a week, and most of what is tested here is held to
    exactly that. A few tests need a second opening before tomorrow -- to show research is acted
    on after a pause lifts -- and they say so by asking for this.
    """
    text = risk_profile.read_text(encoding="utf-8")
    for old, new, expected in (
            ("        max_new_positions_per_day: 1\n",
             f"        max_new_positions_per_day: {per_day}\n", 1),
            ("max_new_positions_per_day:  {value: TODO, recommended_default: 1}",
             f"max_new_positions_per_day:  {{value: {per_day}, recommended_default: 1}}", 3),
            ("max_new_positions_per_week: {value: TODO, recommended_default: 2}",
             f"max_new_positions_per_week: {{value: {per_day}, recommended_default: 2}}", 2)):
        assert text.count(old) == expected, f"the configuration no longer reads {old!r}"
        text = text.replace(old, new)
    risk_profile.write_text(text, encoding="utf-8")


class Desk:
    """The simulated account under a confirmed plan, with a venue and a market the test scripts."""

    def __init__(self, tmp_path, monkeypatch, *, cash=1000.0, openings_per_day=None):
        self.monkeypatch = monkeypatch
        self.prices = dict(risk_scenarios.PRICES)
        live_root = tmp_path / "live"
        (live_root / "state").mkdir(parents=True)
        (live_root / "runs").mkdir()
        shutil.copytree(ROOT / "config", live_root / "config")
        if openings_per_day:
            quicken(live_root / "config" / "risk_profile.yaml", openings_per_day)
        ledger = copy_example_ledger("micro_live_portfolio.example.yaml",
                                     live_root / "state" / "micro_live_portfolio.yaml", as_of=today())
        ledger.write_text(ledger.read_text().replace("cash_usd: 100", f"cash_usd: {cash:g}"))
        self.live = Service(live_root / "config")
        self.live_root = live_root
        self.runs = live_root / "runs"

        # Every read of the market is the test's own table: no vendor, no venue, no model.
        scripted(monkeypatch, self.prices)

        self.plan = self.live.auto_plan(capital_usd=cash)
        config = paper.ensure(self.live.profiles(), changes=self.plan["changes"],
                              creates=PLAN_CREATES, plan_id=self.plan["plan_id"])
        self.config = config
        self.sim = self.service()
        paper.open_account(self.sim.profiles(), cash_usd=cash, as_of=today())
        self.active = store.confirm_plan(self.sim.state_db(), self.sim.profiles(), self.plan,
                                         identity="owner", scope="paper")
        self.broker = self.venue()
        self.bind(self.sim)
        self.count = 0

    def service(self):
        return Service(self.config)

    def venue(self):
        """The simulated account's own venue, at the prices the test sets rather than a vendor's.

        Built the way the account builds it: cash and holdings from its ledger, its orders from
        the file beside its state. So a second one is what a restarted process would have.
        """
        return paper.venue(self.sim.profiles())

    def bind(self, service):
        """Hand a service the scripted venue and an open market."""
        self.monkeypatch.setattr(service, "_broker", lambda *_: self.broker)
        self.monkeypatch.setattr(service, "market_status", lambda: MarketStatus(is_open=True))
        return service

    # -- research, as the pipeline leaves it
    def research(self, ticker="VTI", *, rating="Buy", confidence=88, reviewer="BUY",
                 missing=(), age_minutes=30, bucket=MICRO):
        self.count += 1
        return write_research(self.runs, ticker, rating=rating, confidence=confidence,
                              reviewer=reviewer, missing=missing, age_minutes=age_minutes,
                              bucket=bucket, serial=self.count)

    def tick(self, service=None, **kwargs):
        return trader.tick(service or self.sim, runs_dir=self.runs, trade_date=today(), **kwargs)

    def move(self, symbol, factor):
        """The market moves: the book's valuation and the venue's feed both read this table."""
        self.prices[symbol] = round(self.prices[symbol] * factor, 4)

    # -- what is on record
    def proposals(self, service=None):
        service = service or self.sim
        return service._store(service.profiles()).all()

    def ledger(self):
        return load_portfolio(paper.ledger_path(self.sim.profiles()))

    def held(self, symbol="VTI"):
        position = self.ledger().position(symbol, MICRO)
        return float(position.quantity) if position else 0.0

    def managed(self, symbol="VTI"):
        service = self.sim
        profiles = service.profiles()
        return positions.managed(fills=service._fills_for_replay(profiles), proposals=self.proposals(),
                                 bucket=MICRO, symbol=symbol, ledger_quantity=self.held(symbol))

    def buy(self, ticker="VTI", **research):
        self.research(ticker, **research)
        result = self.tick()
        bought = next(p for p in self.proposals() if p.ticker == ticker and p.action == "BUY")
        assert bought.status == EXECUTED, (bought.status, result.as_dict())
        return bought


@pytest.fixture
def desk(tmp_path, monkeypatch, offline_market_reads):
    return Desk(tmp_path, monkeypatch)


@pytest.fixture
def quick_desk(tmp_path, monkeypatch, offline_market_reads):
    """An account that may open three positions in a day (see :func:`quicken`)."""
    return Desk(tmp_path, monkeypatch, openings_per_day=3)


def sold(broker, symbol="VTI"):
    return sum(o.filled_quantity for o in broker._orders.values() if o.side == "sell" and o.symbol == symbol)


def bought(broker, symbol="VTI"):
    return sum(o.filled_quantity for o in broker._orders.values() if o.side == "buy" and o.symbol == symbol)


# ---------------------------------------------------------------- the whole way round
def test_research_to_purchase_to_hold_to_exit_in_the_simulated_account(desk):
    # 1. Research concludes Buy at confidence 88. Nothing else is asked of anyone.
    run_id = desk.research("VTI", rating="Buy", confidence=88)
    first = desk.tick()
    signal = first.signals[0]
    assert signal["run_id"] == run_id and signal["diagnosis"]["code"] == "proposal_automatic"
    opened = desk.proposals()[0]
    assert (opened.action, opened.status, opened.bucket) == ("BUY", EXECUTED, MICRO)
    # Sized by the plan and cut to the tier's ticket: 33% of $1,000 is $330, the ticket is $200.
    assert opened.notional_usd == 200.0 and opened.weight_override_pct == pytest.approx(33.0)
    assert opened.strategy_version == desk.active["opening_version"]
    assert (opened.approved_via, opened.approved_by) == (autonomy.AUTONOMOUS_VIA, "autonomy:open")
    assert first.approvals["approved"][0]["outcome"] == "SUBMITTED"
    quantity = desk.held()
    # In the venue's own increment, rounded down: a dollar amount never buys more than it pays for.
    assert quantity == exits.floor_to_increment(200.0 / VTI) and quantity * VTI <= 200.0
    assert desk.ledger().position("VTI", MICRO).opened_by == "autonomy:open"

    # 2. It is bound to the version that opened it, with that version's own way out.
    mine = desk.managed()
    assert mine.quantity == pytest.approx(quantity) and mine.version_id == opened.strategy_version
    assert mine.cost_basis == pytest.approx(VTI) and mine.other_quantity == 0.0
    version = store.version(desk.sim.state_db(), mine.version_id)
    assert version["strategy"]["stop_loss_pct"] == 10.0 and version["strategy"]["max_holding_days"] == 90

    # 3. Held: nothing has met a condition, so a pass does nothing at all.
    quiet = desk.tick()
    assert [row["exit"]["due"] for row in quiet.exits] == [False]
    assert quiet.exits[0]["exit"]["exit_by"] is not None and quiet.signals == []
    assert len(desk.proposals()) == 1 and len(desk.broker._orders) == 1

    # 4. The price falls 12%, past the version's 10% stop. Code sells what code bought.
    desk.move("VTI", 0.88)
    stopped = desk.tick()
    assert stopped.exits[0]["exit"]["reason"] == positions.STOP_LOSS
    assert "止损" in stopped.exits[0]["exit"]["text"]["zh"]
    closing = next(p for p in desk.proposals() if p.action == "SELL")
    assert closing.status == EXECUTED and closing.approved_by == "autonomy:exit"
    assert closing.strategy_version == opened.strategy_version
    assert closing.sell_quantity == pytest.approx(quantity, abs=1e-6)
    assert desk.held() == 0.0 and desk.ledger().position("VTI", MICRO) is None
    assert sold(desk.broker) == pytest.approx(bought(desk.broker), abs=1e-6)
    # The loss is on the record as a loss, and the plan's own account of itself says so.
    realized = desk.ledger().realized[-1]
    assert realized.realized_usd < 0 and realized.symbol == "VTI"
    after = desk.tick()
    assert after.exits == [] and after.loss["realized_usd"] == pytest.approx(realized.realized_usd, abs=0.01)
    assert after.loss["reached"] is False


def test_research_saying_trim_halves_what_code_holds_and_saying_sell_closes_it(desk):
    desk.buy()
    quantity = desk.held()
    desk.research("VTI", rating="Underweight", confidence=70, reviewer="REDUCE")
    trimmed = desk.tick()
    assert trimmed.signals[0]["diagnosis"]["code"] == "proposal_automatic"
    reduce = next(p for p in desk.proposals() if p.action == "REDUCE")
    assert reduce.status == EXECUTED and reduce.sell_quantity == pytest.approx(quantity / 2, abs=1e-6)
    assert desk.held() == pytest.approx(quantity / 2, abs=1e-6)
    assert desk.managed().quantity == pytest.approx(quantity / 2, abs=1e-6)

    desk.research("VTI", rating="Sell", confidence=75, reviewer="SELL")
    desk.tick()
    assert desk.held() == 0.0
    assert sold(desk.broker) == pytest.approx(bought(desk.broker), abs=1e-6), "never more than was bought"


def test_a_position_is_closed_when_its_holding_period_ends_and_at_its_target(desk):
    desk.buy()
    later = datetime.now(UTC).date() + timedelta(days=91)
    aged = desk.tick(today=later)
    assert aged.exits[0]["exit"]["reason"] == positions.MAX_HOLDING
    assert aged.exits[0]["exit"]["facts"] == {"held_days": 91, "limit_days": 90}
    assert desk.held() == 0.0


def test_a_target_closes_a_position_only_when_the_version_has_one(desk):
    opened = desk.buy()
    desk.move("VTI", 1.30)
    assert desk.tick().exits[0]["exit"]["due"] is False        # this version sets no target
    view = desk.managed()
    with_target = {"strategy": {**store.version(desk.sim.state_db(), opened.strategy_version)["strategy"],
                                "take_profit_pct": 20.0}}
    call = positions.exit_call(view, with_target, price=desk.prices["VTI"],
                               today=datetime.now(UTC).date())
    assert call.due and call.reason == positions.TAKE_PROFIT and "止盈" in call.text()["zh"]


# ---------------------------------------------------------------- only what code bought
def _human_buy(desk, symbol="VTI", dollars=100.0):
    """A purchase a person approved in the console, through the ordinary approval path."""
    from agentic_trading.execution_scenarios import _proposal

    proposal = _proposal(ticker=symbol, bucket=MICRO, notional=dollars, status=PROPOSED,
                         price=desk.prices[symbol])
    proposal.trade_date = today()
    proposal.evidence["price_date"] = today()
    proposal.reference_price_date = today()
    proposal.positions_fingerprint = None
    proposal.authority_version = autonomy.note_version(desk.sim.profiles(), desk.sim.state_db())
    store_ = desk.sim._store(desk.sim.profiles())
    store_.save(proposal)
    answer = desk.sim.approve(
        proposal.proposal_id, identity="owner", version=proposal.version(),
        request_id=f"human-{proposal.proposal_id}", authority_version=proposal.authority_version,
        terms={"ticker": proposal.ticker, "bucket": proposal.bucket, "action": proposal.action,
               "notional_usd": proposal.notional_usd, "sell_quantity": proposal.sell_quantity})
    assert answer["submitted"] is True, answer
    return store_.get(proposal.proposal_id)


def test_a_holding_a_person_bought_is_never_sold_by_code(desk):
    _human_buy(desk, dollars=150.0)
    quantity = desk.held()
    assert quantity > 0 and desk.managed().quantity == 0.0
    desk.move("VTI", 0.70)                                       # far past any stop
    result = desk.tick()
    assert result.exits == [], "a person's holding has no exit condition of code's"
    desk.research("VTI", rating="Sell", confidence=90, reviewer="SELL")
    told = desk.tick().signals[0]
    assert told["diagnosis"]["code"] == "not_managed" and "你来确认" in told["diagnosis"]["summary"]["zh"]
    assert desk.held() == quantity and sold(desk.broker) == 0.0


def test_shares_a_person_added_stay_theirs_when_code_closes_its_own(desk):
    desk.buy()
    own = desk.held()
    _human_buy(desk, dollars=100.0)
    total = desk.held()
    theirs = total - own
    view = desk.managed()
    assert view.quantity == pytest.approx(own, abs=1e-6) and view.other_quantity == pytest.approx(theirs, abs=1e-6)

    desk.move("VTI", 0.85)
    desk.tick()
    closing = next(p for p in desk.proposals() if p.action == "SELL")
    assert closing.status == EXECUTED and closing.sell_quantity == pytest.approx(own, abs=1e-6)
    assert desk.held() == pytest.approx(theirs, abs=1e-6), "what the person bought is still there"
    assert desk.managed().quantity == 0.0
    # And asked for the rest, code is refused by the count of what it bought.
    refused = autonomy.permits(desk.sim.profiles(), bucket=MICRO, action="SELL", confidence=None,
                               requested_quantity=theirs, managed=desk.managed())
    assert refused.permitted is False and "belongs to a person" in refused.reasons[0]


def test_shares_the_log_cannot_explain_leave_code_with_authority_over_none(desk):
    desk.buy()
    view = positions.managed(
        fills=desk.sim._fills_for_replay(desk.sim.profiles()), proposals=desk.proposals(),
        bucket=MICRO, symbol="VTI", ledger_quantity=desk.held() + 1.0)
    assert view.quantity is None and "cannot explain" in view.reason
    decision = autonomy.permits(desk.sim.profiles(), bucket=MICRO, action="SELL", confidence=None,
                                requested_quantity=0.1, managed=view)
    assert decision.permitted is False and "could not be established" in decision.reasons[0]


def test_the_way_out_is_its_own_switch_and_it_is_off_unless_the_plan_turned_it_on(desk):
    desk.buy()
    view = desk.managed()
    profiles = desk.sim.profiles()
    assert autonomy.permits(profiles, bucket=MICRO, action="SELL", confidence=None,
                            requested_quantity=view.quantity, managed=view).permitted is True
    # The real account's configuration has never declared it, so there it permits nothing.
    live = desk.live.profiles()
    assert live.risk_cfg("buckets", MICRO, "autonomy", "managed_exit", default=None) is None
    off = autonomy.permits(live, bucket=MICRO, action="SELL", confidence=None,
                           requested_quantity=view.quantity, managed=view)
    assert off.permitted is False and "managed_exit.enabled is not true" in off.reasons[0]
    # And without the claim of a managed exit, a SELL is what it always was.
    plain = autonomy.permits(profiles, bucket=MICRO, action="SELL", confidence=99)
    assert plain.permitted is False and "never autonomous" in plain.reasons[0]


def _forged_exit(desk, *, version, quantity):
    """A SELL written to look like code's own exit: no person required, a strategy version named."""
    from agentic_trading.execution_scenarios import _proposal

    forged = _proposal(ticker="VTI", bucket=MICRO, action="SELL", status=PROPOSED,
                       price=desk.prices["VTI"])
    forged.trade_date = today()
    forged.sell_quantity = quantity
    forged.requires_human_approval = False
    forged.strategy_version = version
    forged.positions_fingerprint = None
    forged.authority_version = autonomy.note_version(desk.sim.profiles(), desk.sim.state_db())
    desk.sim._store(desk.sim.profiles()).save(forged)
    return forged


def test_saying_a_sale_is_codes_own_does_not_make_it_so(desk):
    """The claim is the proposal's; what code actually bought is read from the fills."""
    _human_buy(desk, dollars=150.0)
    quantity = desk.held()
    # With the name of a real strategy version on it, and with none at all.
    claimed = _forged_exit(desk, version=desk.active["opening_version"], quantity=quantity)
    bare = _forged_exit(desk, version=None, quantity=quantity)
    report = desk.sim.autonomous_reductions()
    declined = {row["proposal_id"]: row["reasons"][0] for row in report["declined"]}
    assert report["approved"] == [] and report["considered"] == 2
    assert "code holds none of this position" in declined[claimed.proposal_id]
    assert "never autonomous" in declined[bare.proposal_id]
    assert desk.held() == quantity and sold(desk.broker) == 0.0
    # Both are still there for a person, which is what a sale of a person's holding is for.
    assert {p.status for p in desk.proposals() if p.action == "SELL"} == {PROPOSED}


def test_a_claim_for_more_than_code_bought_is_refused_whole_not_trimmed(desk):
    desk.buy()
    own = desk.held()
    _human_buy(desk, dollars=100.0)
    everything = desk.held()
    grab = _forged_exit(desk, version=desk.active["opening_version"], quantity=everything)
    report = desk.sim.autonomous_reductions()
    assert report["approved"] == []
    assert "belongs to a person" in report["declined"][0]["reasons"][0]
    assert report["declined"][0]["proposal_id"] == grab.proposal_id
    assert desk.held() == everything and desk.managed().quantity == pytest.approx(own)


def test_a_position_answers_to_the_version_that_opened_it_after_the_plan_changes(quick_desk):
    """docs/todo.md: switching leaves old positions with the strategy that opened them."""
    desk = quick_desk
    opened = desk.buy("VTI")
    first = opened.strategy_version
    # A new plan with a tighter stop and a short holding period: a different strategy, so a new
    # version opens from here on, and the first one goes on managing what it bought.
    tighter = {**desk.plan, "plan_id": "plan-second",
               "strategy": {**desk.plan["strategy"], "stop_loss_pct": 3.0, "max_holding_days": 20}}
    active = store.confirm_plan(desk.sim.state_db(), desk.sim.profiles(), tighter,
                                identity="owner", scope="paper")
    second = active["opening_version"]
    assert second != first and active["managing"] == [first]
    assert store.version(desk.sim.state_db(), second)["validation"]["simulated_days"] == 0

    # Down 5%: past the new plan's stop, inside the one this position was opened under.
    desk.move("VTI", 0.95)
    held = desk.tick().exits[0]
    assert held["strategy_version"] == first and held["exit"]["due"] is False
    assert desk.held("VTI") > 0
    # What is bought now is bought under the new version, and held to the new stop.
    bought_now = desk.buy("VXUS")
    assert bought_now.strategy_version == second
    desk.move("VXUS", 0.96)
    rows = {row["symbol"]: row for row in desk.tick().exits}
    assert rows["VXUS"]["exit"]["reason"] == positions.STOP_LOSS and desk.held("VXUS") == 0.0
    assert rows["VTI"]["exit"]["due"] is False and desk.held("VTI") > 0
    # And the old position leaves on its own version's terms when it meets them.
    desk.move("VTI", 0.90)
    assert desk.tick().exits[0]["exit"]["facts"]["limit_pct"] == "10" and desk.held("VTI") == 0.0


def test_withdrawing_the_plan_stops_openings_and_leaves_its_positions_with_a_way_out(desk):
    opened = desk.buy("VTI")
    store.withdraw_plan(desk.sim.state_db(), identity="owner", reason="stopping")
    # Research that would have been bought yesterday is not read at all now.
    desk.research("VXUS", confidence=95)
    quiet = desk.tick()
    assert quiet.signals == [] and quiet.loss is None and "no plan is in force" in quiet.skipped
    assert desk.held("VXUS") == 0.0 and [p.ticker for p in desk.proposals()] == ["VTI"]
    assert trader.seen_signals(desk.sim.state_db()).keys() == {opened.run_id}
    # The position is still code's by the fills, and still held to the stop it was opened under:
    # ending a plan does not leave what it bought with nobody watching it.
    view = desk.managed()
    assert view.quantity == desk.held() and view.version_id in store.known_versions(desk.sim.state_db())
    assert quiet.exits[0]["exit"]["due"] is False
    desk.move("VTI", 0.85)
    stopped = desk.tick()
    assert stopped.exits[0]["exit"]["reason"] == positions.STOP_LOSS
    assert desk.held("VTI") == 0.0 and sold(desk.broker) == pytest.approx(bought(desk.broker))
    assert desk.tick().exits == []


def test_nothing_the_simulated_account_does_reaches_the_real_books(desk):
    real_ledger = desk.live_root / "state" / "micro_live_portfolio.yaml"
    before = load_portfolio(real_ledger)
    desk.buy("VTI")
    desk.move("VTI", 0.88)
    desk.tick()
    assert len(desk.proposals()) == 2 and len(desk.broker._orders) == 2
    # The real account: no proposal, no fill, no order, no plan, and the ledger as it was.
    live = desk.live
    assert live._store(live.profiles()).all() == []
    assert live._fills_for_replay(live.profiles()) == []
    assert store.active_plan(live.state_db()) is None and store.versions(live.state_db()) == []
    after = load_portfolio(real_ledger)
    assert after.model_dump() == before.model_dump()
    assert after.buckets[MICRO].cash_usd == 1000.0 and after.positions == [] and after.realized == []
    assert not (desk.live_root / "state" / "paper_orders.json").exists()
    assert (desk.live_root / "state" / "paper" / "state" / "paper_orders.json").exists()


# ---------------------------------------------------------------- when it does not go cleanly
def test_a_partly_filled_exit_is_finished_by_a_second_order_for_what_is_left(desk):
    desk.buy()
    quantity = desk.held()
    desk.move("VTI", 0.88)
    desk.broker._fill_mode = "partial"
    desk.tick()
    first = next(p for p in desk.proposals() if p.action == "SELL")
    assert first.status == PARTIALLY_FILLED and first.filled_quantity == pytest.approx(quantity / 2, abs=1e-6)
    assert desk.held() == pytest.approx(quantity / 2, abs=1e-5)

    # While that order is still working, nothing more is sent however many passes run.
    for _ in range(3):
        waiting = desk.tick()
        assert waiting.exits[0]["diagnosis"]["code"] == "unresolved_order"
    assert len([o for o in desk.broker._orders.values() if o.side == "sell"]) == 1

    # The venue cancels the rest. Reconciliation records it; only then is the remainder sold.
    desk.broker.cancel_order(desk.broker._orders[first.client_order_id].order_id)
    desk.sim.reconcile_orders()
    assert next(p for p in desk.proposals() if p.proposal_id == first.proposal_id).status == CANCELED
    desk.broker._fill_mode = "full"
    desk.tick()
    sells = [p for p in desk.proposals() if p.action == "SELL"]
    assert len(sells) == 2 and sells[-1].status == EXECUTED
    assert sells[-1].sell_quantity == pytest.approx(quantity / 2, abs=1e-5)
    assert desk.held() == 0.0
    assert sold(desk.broker) == pytest.approx(bought(desk.broker), abs=1e-6), "not one share oversold"


def test_a_sale_the_venue_refuses_stops_automatic_orders_until_a_person_looks(desk):
    desk.buy()
    quantity = desk.held()
    desk.move("VTI", 0.88)
    desk.broker._reject_reason = "simulated venue refusal"
    refused = desk.tick()
    first = next(p for p in desk.proposals() if p.action == "SELL")
    assert first.status == REJECTED
    assert refused.approvals["approved"][0] == {
        "proposal_id": first.proposal_id, "outcome": "BROKER_REJECTED", "submitted": False,
        "autonomy_withdrawn": True}
    assert autonomy.load_downgrade(desk.sim.profiles()).tripped is True

    # The venue recovers, and still nothing is sent: the withdrawal is sticky by design.
    desk.broker._reject_reason = None
    for _ in range(3):
        later = desk.tick()
        assert later.approvals["skipped"].startswith("autonomy was withdrawn")
    sells = [p for p in desk.proposals() if p.action == "SELL"]
    # One fresh exit waits where a person can see and approve it; it is not rewritten each pass.
    assert [p.status for p in sells] == [REJECTED, PROPOSED]
    assert desk.held() == quantity and sold(desk.broker) == 0.0

    # A person looks and restores it. The exit that was waiting was written while autonomy was
    # withdrawn, which is not the authority that now stands, so code does not carry it out: it
    # is withdrawn, the stop is read again, and the exit written now is the one that is sent.
    autonomy.reset(desk.sim.profiles(), "looked at the refusal; the venue is answering again")
    resumed = desk.tick()
    assert resumed.exits[0]["withdrawn"] == [sells[1].proposal_id]
    sells = [p for p in desk.proposals() if p.action == "SELL"]
    assert [p.status for p in sells] == [REJECTED, INVALIDATED, EXECUTED]
    assert sells[1].history[-1]["event"] == "auto_exit_withdrawn"
    assert desk.held() == 0.0 and sold(desk.broker) == pytest.approx(quantity, abs=1e-6)


def test_an_exit_a_person_has_taken_up_is_not_withdrawn_from_under_them(desk):
    desk.buy()
    desk.move("VTI", 0.88)
    autonomy.trip(desk.sim.profiles(), "a person withdrew it to look at something")
    desk.tick()
    waiting = next(p for p in desk.proposals() if p.action == "SELL")
    assert waiting.status == PROPOSED
    # The person approves it themselves, in the console, while autonomy is still withdrawn.
    answer = desk.sim.approve(
        waiting.proposal_id, identity="owner", version=waiting.version(), request_id="by-hand",
        authority_version=waiting.authority_version,
        terms={"ticker": "VTI", "bucket": MICRO, "action": "SELL",
               "notional_usd": waiting.notional_usd, "sell_quantity": waiting.sell_quantity})
    assert answer["submitted"] is True and desk.held() == 0.0
    autonomy.reset(desk.sim.profiles(), "done")
    after = desk.tick()
    assert after.exits == [] and len([p for p in desk.proposals() if p.action == "SELL"]) == 1
    # A person sold code's shares, so they came out of code's count and nothing is left to manage.
    assert desk.managed().quantity == 0.0 and sold(desk.broker) == pytest.approx(bought(desk.broker))


def test_a_purchase_is_not_left_waiting_while_autonomy_is_withdrawn(desk):
    autonomy.trip(desk.sim.profiles(), "an earlier order could not be accounted for")
    run_id = desk.research("VTI", confidence=90)
    for _ in range(2):
        held_back = desk.tick().signals[0]
        assert held_back["diagnosis"]["code"] == "autonomy_withdrawn"
        assert "等你检查后恢复" in held_back["diagnosis"]["summary"]["zh"]
    # Nothing was written to wait for a person, and the research was not written off either.
    assert desk.proposals() == []
    kept = trader.seen_signals(desk.sim.state_db())[run_id]
    assert kept["final"] is False and kept["diagnosis"]["code"] == "autonomy_withdrawn"
    autonomy.reset(desk.sim.profiles(), "looked; nothing was outstanding")
    assert desk.tick().signals[0]["diagnosis"]["code"] == "proposal_automatic"
    assert desk.held() > 0 and trader.seen_signals(desk.sim.state_db())[run_id]["final"] is True


def test_an_order_nobody_can_account_for_is_reconciled_and_never_retried_blind(desk, monkeypatch):
    desk.buy()
    quantity = desk.held()
    desk.move("VTI", 0.88)
    real_submit = desk.broker.submit_order
    sent = []

    def vanish(request):
        sent.append(request)
        raise TimeoutError("the venue did not answer")

    monkeypatch.setattr(desk.broker, "submit_order", vanish)
    desk.tick()
    first = next(p for p in desk.proposals() if p.action == "SELL")
    assert first.status == UNKNOWN and first.client_order_id
    assert autonomy.load_downgrade(desk.sim.profiles()).tripped is True

    # Pass after pass: the intent is on record and unresolved, so nothing new is written or sent.
    monkeypatch.setattr(desk.broker, "submit_order", real_submit)
    for _ in range(3):
        waiting = desk.tick()
        assert waiting.exits[0]["diagnosis"]["code"] == "unresolved_order"
        desk.sim.reconcile_orders()
    assert len(sent) == 1 and len([p for p in desk.proposals() if p.action == "SELL"]) == 1
    assert desk.held() == quantity and sold(desk.broker) == 0.0
    # New research is not acted on either while that order stands.
    desk.research("VXUS", rating="Buy", confidence=90)
    held_back = desk.tick().signals[0]
    assert held_back["diagnosis"]["code"] == "unresolved_order" and held_back["proposal_id"] is None


def test_a_restart_changes_nothing_and_repeats_nothing(desk):
    desk.buy()
    quantity = desk.held()
    before = [(p.proposal_id, p.status) for p in desk.proposals()]
    # A new process: nothing kept in memory, everything read back from the state directory --
    # the service, and the venue with the cash, the holding and the order it already took.
    desk.broker = desk.venue()
    assert desk.broker.get_account().cash == pytest.approx(1000.0 - quantity * VTI, abs=0.01)
    assert [(p.symbol, p.quantity) for p in desk.broker.get_positions()] == [("VTI", quantity)]
    again = desk.bind(desk.service())
    assert store.active_plan(again.state_db())["plan_id"] == desk.plan["plan_id"]
    result = desk.tick(again)
    assert result.signals == [], "the research already acted on is not acted on again"
    assert [(p.proposal_id, p.status) for p in desk.proposals(again)] == before
    assert len(desk.broker._orders) == 1 and desk.held() == quantity
    # And the position is still code's to close, derived from the same records.
    desk.move("VTI", 0.88)
    desk.tick(again)
    assert desk.held() == 0.0 and sold(desk.broker) == pytest.approx(quantity, abs=1e-6)


def test_the_same_research_is_decided_once_however_often_it_is_read(desk):
    run_id = desk.research("VTI", confidence=88)
    for _ in range(4):
        desk.tick()
    assert len(desk.proposals()) == 1 and len(desk.broker._orders) == 1
    seen = trader.seen_signals(desk.sim.state_db())
    assert list(seen) == [run_id] and seen[run_id]["proposal_id"] == desk.proposals()[0].proposal_id


# ---------------------------------------------------------------- what stops it adding risk
def test_research_that_does_not_clear_the_plan_is_recorded_with_its_reason(desk):
    desk.research("VTI", rating="Buy", confidence=74)                    # under the plan's 80
    desk.research("VXUS", rating="Hold", confidence=60)
    desk.research("AAPL", rating="Buy", confidence=95)                   # not in the plan
    desk.research("BND", rating="Buy", confidence=90, missing=("FRED unavailable",))
    codes = {s["ticker"]: s["diagnosis"]["code"] for s in desk.tick().signals}
    assert codes == {"VTI": "confidence_below_floor", "VXUS": "rating_hold_no_position",
                     "AAPL": "not_in_plan", "BND": "material_information_missing"}
    assert desk.proposals() == [] and desk.broker._orders == {}
    # Each is decided once, with the reason kept against the run it came from.
    assert len(trader.seen_signals(desk.sim.state_db())) == 4
    assert desk.tick().signals == []


def test_a_second_conclusion_on_the_same_day_waits_for_the_next_and_is_not_thrown_away(desk):
    """The account opens one position a day. A week's research arrives together."""
    desk.buy("VTI")
    run_id = desk.research("VXUS", confidence=90)
    for _ in range(3):
        waiting = desk.tick().signals[0]
        assert waiting["diagnosis"]["code"] == "new_positions_per_day"
        assert waiting["diagnosis"]["reasons"][0]["rule_id"] == "HR12"
    # Nothing was written or sent, the limit was not touched, and the run is still open.
    assert [p.ticker for p in desk.proposals()] == ["VTI"] and desk.held("VXUS") == 0.0
    kept = trader.seen_signals(desk.sim.state_db())[run_id]
    assert kept["final"] is False and kept["diagnosis"]["code"] == "new_positions_per_day"
    # What ends the wait, if the day never comes, is the research's own lifetime.
    record_path = desk.runs / run_id / "decision_record.json"
    record = json.loads(record_path.read_text())
    record["created_at"] = (datetime.now(UTC) - timedelta(days=3)).isoformat()
    record_path.write_text(json.dumps(record))
    assert desk.tick().signals[0]["diagnosis"]["code"] == "signal_expired"
    assert trader.seen_signals(desk.sim.state_db())[run_id]["final"] is True
    assert desk.tick().signals == []


def test_every_reason_to_wait_is_a_reason_the_system_can_actually_give():
    """A code renamed in the diagnosis table would otherwise turn a wait into a verdict, silently."""
    from agentic_trading import diagnosis

    assert trader.NOT_NOW <= set(diagnosis._TEXT), sorted(trader.NOT_NOW - set(diagnosis._TEXT))
    # And a research verdict is never mistaken for one.
    for code in ("confidence_below_floor", "rating_hold_no_position", "not_in_plan",
                 "material_information_missing", "already_held", "plan_positions_full"):
        assert code in diagnosis._TEXT and code not in trader.NOT_NOW
    mixed = {"proposal_id": None, "diagnosis": {"code": "new_positions_per_day", "reasons": [
        {"code": "new_positions_per_day"}, {"code": "tier_cap"}]}}
    assert trader.is_final(mixed) is True, "one lasting reason among them makes it an answer"


def test_research_past_its_lifetime_is_not_acted_on(desk):
    desk.research("VTI", confidence=90, age_minutes=3 * 24 * 60)        # the class lives two days
    outcome = desk.tick().signals[0]
    assert outcome["diagnosis"]["code"] == "signal_expired" and desk.proposals() == []


def test_research_about_another_account_is_not_a_signal(desk):
    desk.research("VTI", confidence=90, bucket="core")
    assert desk.tick().signals == [] and desk.proposals() == []


def test_the_plan_holds_no_more_positions_than_it_says(desk):
    desk.active = store.confirm_plan(
        desk.sim.state_db(), desk.sim.profiles(),
        {**desk.plan, "strategy": {**desk.plan["strategy"], "max_positions": 1}},
        identity="owner", scope="paper")
    desk.buy("VTI")
    desk.research("VXUS", confidence=90)
    full = desk.tick().signals[0]
    assert full["diagnosis"]["code"] == "plan_positions_full" and "1" in full["diagnosis"]["summary"]["zh"]


def test_a_pause_stops_openings_and_leaves_the_way_out_open(quick_desk):
    desk = quick_desk
    desk.buy("VTI")
    store.pause(desk.sim.state_db(), reason="stepping away", by="owner")
    desk.research("VXUS", confidence=90)
    paused = desk.tick()
    assert paused.paused["reason"] == "stepping away"
    assert paused.signals[0]["diagnosis"]["code"] == "plan_paused"
    assert desk.held("VXUS") == 0.0
    # The position it already has is still closed on its own condition.
    desk.move("VTI", 0.88)
    desk.tick()
    assert desk.held("VTI") == 0.0
    # Resumed while the research is still fresh, it is acted on: a pause says "not now", and is
    # not a verdict on what was concluded in the meantime.
    lost = desk.tick().loss["total_usd"]
    trader.resume(desk.sim, note="back", by="owner", trade_date=today())
    assert desk.tick().signals[0]["diagnosis"]["code"] == "proposal_automatic"
    assert desk.held("VXUS") > 0
    # And pausing and resuming by hand moved nothing about where the loss line is measured from.
    assert lost < 0 and store.state(desk.sim.state_db())["loss_baseline_usd"] == 0.0


@pytest.mark.parametrize("kind, by", [(store.PERSON, "owner"), (store.LOSS_LINE, "auto:loss_line")])
def test_a_pause_also_holds_back_a_purchase_written_before_it(quick_desk, monkeypatch, kind, by):
    """Found in review on 2026-10-14: a purchase the plan wrote overnight was bought at the open
    although the plan had been paused in between -- by a person, or at its loss line."""
    desk = quick_desk
    monkeypatch.setattr(desk.sim, "market_status", lambda: MarketStatus(is_open=False))
    desk.research("VXUS", confidence=90)
    desk.tick()
    queued = next(p for p in desk.proposals() if p.ticker == "VXUS")
    assert queued.status == PROPOSED, "written while the market was shut, waiting for the open"
    store.pause(desk.sim.state_db(), reason="stepping away", by=by, kind=kind)
    monkeypatch.setattr(desk.sim, "market_status", lambda: MarketStatus(is_open=True))
    report = desk.sim.autonomous_reductions()
    assert report["approved"] == [] and "paused (stepping away)" in report["declined"][0]["reasons"][0]
    desk.tick()
    assert next(p for p in desk.proposals() if p.ticker == "VXUS").status == PROPOSED
    assert desk.held("VXUS") == 0.0 and not [o for o in desk.broker._orders.values() if o.symbol == "VXUS"]
    # Resumed while the purchase is still fresh, code carries it out after all.
    trader.resume(desk.sim, note="back", by="owner", trade_date=today())
    desk.tick()
    assert next(p for p in desk.proposals() if p.ticker == "VXUS").status == EXECUTED
    assert desk.held("VXUS") > 0


def test_reaching_the_loss_line_pauses_the_plan_until_a_person_resumes_it(quick_desk):
    desk = quick_desk
    desk.buy("VTI")
    # A loss line of $5 on this plan, so the stop's own loss is past it.
    tight = {**desk.plan, "envelope": {**desk.plan["envelope"], "loss_pause_usd": 5.0}}
    store.confirm_plan(desk.sim.state_db(), desk.sim.profiles(), tight, identity="owner", scope="paper")
    desk.move("VTI", 0.88)
    hit = desk.tick()
    assert hit.loss["reached"] is True and hit.paused["kind"] == "loss_line"
    assert desk.held("VTI") == 0.0, "the losing position is still closed"
    desk.research("VXUS", confidence=95)
    for _ in range(2):
        assert desk.tick().signals[0]["diagnosis"]["code"] == "plan_paused"
    state = store.state(desk.sim.state_db())
    assert state["paused"] and "loss line" in state["reason"]
    lost = desk.tick().loss
    assert lost["since_usd"] == lost["total_usd"] == pytest.approx(-24.0, abs=0.05)

    # A person looks and resumes. The loss is still there, so the line is measured from here on:
    # without that the next pass would pause it again and resuming would mean nothing.
    resumed = trader.resume(desk.sim, note="reviewed the stop", by="owner", trade_date=today())
    assert resumed["paused"] is False and resumed["loss_baseline_usd"] == lost["total_usd"]
    assert resumed["history"][-1]["loss_baseline_usd"] == {"from": 0.0, "to": lost["total_usd"]}
    after = desk.tick()
    assert after.paused is None and after.loss["since_usd"] == 0.0 and after.loss["reached"] is False
    assert after.signals[0]["diagnosis"]["code"] == "proposal_automatic" and desk.held("VXUS") > 0
    # A further loss of that size, measured from here, pauses it again. (Two whole shares at
    # the order's limit price: a whole share fits in $200, so the buy was a limit order.)
    desk.move("VXUS", 0.93)
    holding = desk.ledger().position("VXUS", MICRO)
    further = round((desk.prices["VXUS"] - holding.cost_basis_per_share) * holding.quantity, 2)
    again = desk.tick()
    assert further < -5.0 and again.loss["since_usd"] == pytest.approx(further, abs=0.01)
    assert again.loss["total_usd"] == pytest.approx(lost["total_usd"] + further, abs=0.01)
    assert again.loss["reached"] is True and again.paused["kind"] == "loss_line"
    assert desk.held("VXUS") == holding.quantity, "the line stops openings; it does not sell"


def _person_approves(desk, proposal):
    """A proposal its owner approved in the console, through the ordinary approval path."""
    answer = desk.sim.approve(
        proposal.proposal_id, identity="owner", version=proposal.version(),
        request_id=f"human-{proposal.proposal_id}", authority_version=proposal.authority_version,
        terms={"ticker": proposal.ticker, "bucket": proposal.bucket, "action": proposal.action,
               "notional_usd": proposal.notional_usd, "sell_quantity": proposal.sell_quantity})
    assert answer["submitted"] is True, answer
    return desk.sim._store(desk.sim.profiles()).get(proposal.proposal_id)


def test_a_stop_a_person_approved_is_still_the_plans_loss_and_reaches_its_line(quick_desk):
    """Review of 2026-10-14, item 2 (docs/AUTO_BASELINE_CONTRACT_2026-10-15.md, 2.2): the line
    counted only what code itself sold, so a stop the owner approved by hand took its loss off
    the line, and the pause it should have brought could never come."""
    desk = quick_desk
    desk.buy("VTI")
    desk.move("VTI", 0.88)                                  # past the version's 10% stop
    desk.tick(approve=False)                                # the stop is written, and waits
    stop = next(p for p in desk.proposals() if p.action == "SELL")
    assert stop.status == PROPOSED and stop.strategy_version
    closed = _person_approves(desk, stop)
    assert closed.status == EXECUTED and autonomy.was_human(closed) and desk.held("VTI") == 0.0

    lost = desk.tick().loss
    assert lost["realized_by"] == {"code": 0.0, "person": pytest.approx(-24.0, abs=0.05)}
    assert lost["realized_usd"] == lost["total_usd"] == pytest.approx(-24.0, abs=0.05)
    assert lost["known"] is True and lost["reached"] is False, "this plan's own line is wider"
    # Under a line of $5 the same loss is past it, and the plan pauses itself.
    tight = {**desk.plan, "envelope": {**desk.plan["envelope"], "loss_pause_usd": 5.0}}
    store.confirm_plan(desk.sim.state_db(), desk.sim.profiles(), tight, identity="owner", scope="paper")
    hit = desk.tick()
    assert hit.loss["reached"] is True and hit.paused["kind"] == "loss_line"


def _weekdays_before(day: str, count: int) -> str:
    out = date.fromisoformat(day)
    while count:
        out -= timedelta(days=1)
        count -= out.weekday() < 5
    return out.isoformat()


def test_a_price_from_before_the_last_session_decides_no_exit_and_leaves_the_result_unknown(
        desk, monkeypatch):
    """Contract 2.4: the book accepts a price three weekdays old, and a stop used to be read on
    one as though it were today's. Too old for the session, nothing that reads a price decides."""
    from agentic_trading import cli, portfolio

    desk.buy("VTI")
    quantity = desk.held()
    old = _weekdays_before(today(), 3)              # inside the book's three, two sessions short
    priced = cli.build_snapshot
    stale = {"on": True}

    def snapshot(state, profiles, trade_date, price_lookup=None, identity_lookup=None):
        def lookup(symbol, as_of):
            return desk.prices[symbol.upper()], old if stale["on"] else as_of
        return priced(state, profiles, trade_date, price_lookup or lookup, identity_lookup)

    monkeypatch.setattr(portfolio, "build_snapshot", snapshot)
    monkeypatch.setattr(cli, "build_snapshot", snapshot)
    desk.move("VTI", 0.80)                                  # far past the stop
    desk.research("VXUS", confidence=95)
    result = desk.tick()
    call = result.exits[0]["exit"]
    assert call["due"] is False and call["undecided"] == positions.PRICE_STALE
    assert call["price_date"] == old and f"价格是 {old[5:]} 的，太旧" in call["text"]["zh"]
    assert desk.held() == quantity and sold(desk.broker) == 0.0, "not sold on a price that old"
    # What the plan has lost is not known either, so nothing new is opened.
    assert result.loss["known"] is False and result.loss["stale"] == ["VTI"]
    assert result.loss["unpriced"] == [] and result.loss["reached"] is False
    assert result.signals[0]["diagnosis"]["code"] == "plan_result_unknown"
    kept = trader.undecided_exits(desk.sim.state_db())
    assert kept["VTI"]["since"] == kept["VTI"]["last"] == today()
    assert kept["VTI"]["code"] == positions.PRICE_STALE and kept["VTI"]["price_date"] == old

    # A price for the session again: the stop is read, and met, and the record of it is gone.
    stale["on"] = False
    after = desk.tick()
    assert after.exits[0]["exit"]["reason"] == positions.STOP_LOSS and desk.held() == 0.0
    assert trader.undecided_exits(desk.sim.state_db()) == {}


def test_an_undecided_exit_keeps_the_session_it_began_in_until_it_is_decided(desk):
    db = desk.sim.state_db()
    row = {"symbol": "VTI", "strategy_version": "sv-1",
           "exit": {"due": False, "undecided": positions.PRICE_STALE, "price_date": "2026-10-14",
                    "text": {"zh": "VTI 的价格是 10-14 的，太旧，今天不判断止损。", "en": "old"}}}

    def on(session):
        return SimpleNamespace(db=db, snap=SimpleNamespace(trade_date=session))

    assert trader.remember_undecided(on("2026-10-19"), [row])["VTI"]["since"] == "2026-10-19"
    kept = trader.remember_undecided(on("2026-10-20"), [row])["VTI"]
    assert (kept["since"], kept["last"], kept["code"]) == ("2026-10-19", "2026-10-20", positions.PRICE_STALE)
    # Any other reason nothing was decided is kept as well, under its own words.
    other = {**row, "exit": {"due": False, "undecided": "the fills account for 2 shares and the book holds 3"}}
    assert trader.remember_undecided(on("2026-10-20"), [other])["VTI"]["code"] == "undecided"
    # Decided again -- or no longer held -- and the run is over; the next one starts afresh.
    decided = {**row, "exit": {"due": False, "undecided": None}}
    assert trader.remember_undecided(on("2026-10-21"), [decided]) == {}
    assert trader.remember_undecided(on("2026-10-22"), [row])["VTI"]["since"] == "2026-10-22"
    assert trader.remember_undecided(on("2026-10-22"), []) == {}


@pytest.mark.parametrize("price_date, trade_date, stale", [
    ("2026-10-14", "2026-10-15", False),   # yesterday's close, for today
    ("2026-10-15", "2026-10-15", False),
    ("2026-10-13", "2026-10-15", True),    # a whole session, the 14th, went unpriced
    ("2026-10-16", "2026-10-19", False),   # Friday's, on Monday: a weekend is not a missing day
    ("2026-10-15", "2026-10-19", True),    # Thursday's on Monday: Friday is missing
    ("2026-11-25", "2026-11-27", False),   # Wednesday's, the day after Thanksgiving
    ("2026-12-31", "2027-01-04", False),   # the year's last session, after New Year's Day
    ("2026-10-16", "2026-10-17", False),   # a Saturday pass, on Friday's
    ("2026-10-15", "2026-10-17", True),
    (None, "2026-10-15", True),            # a price nobody can date is old
    ("not a date", "2026-10-15", True),
])
def test_a_price_is_old_when_a_whole_session_went_by_without_one(price_date, trade_date, stale):
    assert trader.price_stale(price_date, trade_date) is stale


def test_without_the_exchange_calendar_a_price_is_aged_in_weekdays(monkeypatch):
    monkeypatch.setattr(trader, "_exchange_calendar", lambda: None)
    assert trader.price_stale("2026-10-14", "2026-10-15") is False
    assert trader.price_stale("2026-10-13", "2026-10-15") is True
    # Wrong only ever toward "old": the day after Thanksgiving reads Wednesday's close as stale.
    assert trader.price_stale("2026-11-25", "2026-11-27") is True


def test_a_position_that_cannot_be_accounted_for_stops_openings_and_is_never_sold(desk):
    desk.buy("VTI")
    quantity = desk.held()
    # Shares appear in the book that no fill on record explains.
    apply_fill_to_ledger(paper.ledger_path(desk.sim.profiles()), symbol="VTI", bucket=MICRO,
                         side="buy", quantity=0.25, price=VTI, trade_date=today())
    assert desk.managed().quantity is None
    desk.move("VTI", 0.80)                                  # far past the stop
    desk.research("VXUS", confidence=95)
    result = desk.tick()
    # What code holds is unknown, so nothing is sold on its behalf ...
    assert result.exits[0]["exit"]["due"] is False and "cannot explain" in result.exits[0]["exit"]["undecided"]
    assert desk.held() == pytest.approx(quantity + 0.25) and sold(desk.broker) == 0.0
    # ... and what the plan has lost is unknown too, so nothing is added either.
    assert result.loss["known"] is False and result.loss["unpriced"] == ["VTI"]
    assert result.signals[0]["diagnosis"]["code"] == "plan_result_unknown"
    assert desk.held("VXUS") == 0.0
    # Nor can a loss-line pause be lifted on a result nobody can read.
    store.pause(desk.sim.state_db(), reason="at the line", by="auto:loss_line", kind=store.LOSS_LINE)
    with pytest.raises(trader.TraderError, match="cannot be worked out"):
        trader.resume(desk.sim, note="carry on", by="owner", trade_date=today())
    assert store.state(desk.sim.state_db())["paused"] is True


def test_an_account_that_never_had_a_plan_costs_nothing_and_does_nothing(desk, monkeypatch):
    """The real account, today: this is every pass of the loop until a plan is confirmed there."""
    monkeypatch.setattr(trader, "read_book", lambda *_a, **_k: pytest.fail(
        "an account with no plan was priced"))
    never = Service(desk.live_root / "config")
    result = trader.tick(never, runs_dir=desk.runs, trade_date=today())
    assert result.ran is False and result.skipped == "no plan is in force"
    assert result.signals == [] and result.exits == [] and result.approvals is None
