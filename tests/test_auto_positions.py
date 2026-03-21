"""Which shares are code's to sell, which strategy they answer to, and what the plan remembers.

Units under the lifecycle in tests/test_auto_lifecycle.py: the replay that says how much of a
holding code itself bought (``auto.positions``), the conditions a position is closed on, the
authority the gate gives an exit (``autonomy.permits(managed=...)``, the engine's eligibility),
and the records a plan keeps (``auto.store``). Offline, and nothing here needs a venue.
"""

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest

from agentic_trading import autonomy
from agentic_trading.auto import positions, store
from agentic_trading.execution_scenarios import _profiles
from agentic_trading.state_db import StateDB

MICRO = "micro_live"
V1, V2 = "sv-aaaaaaaaaaaa", "sv-bbbbbbbbbbbb"


class Log:
    """Fills and the approvals behind them, in the order they happened."""

    def __init__(self):
        self.fills: list[dict] = []
        self.proposals: list = []
        self._minute = 0

    def _add(self, side, quantity, *, via, version, price, symbol, bucket):
        self._minute += 1
        proposal = SimpleNamespace(proposal_id=f"p{self._minute}", approved_via=via,
                                   approved_by="autonomy:open" if via == "autonomy" else "owner",
                                   strategy_version=version)
        self.proposals.append(proposal)
        self.fills.append({"at": f"2026-10-06T14:{self._minute:02d}:00+00:00",
                           "proposal_id": proposal.proposal_id, "bucket": bucket, "symbol": symbol,
                           "side": side, "quantity": quantity, "price": price})
        return self

    def code_buys(self, quantity, version=V1, price=100.0, symbol="VTI", bucket=MICRO):
        return self._add("buy", quantity, via="autonomy", version=version, price=price,
                         symbol=symbol, bucket=bucket)

    def code_sells(self, quantity, version=V1, price=100.0, symbol="VTI", bucket=MICRO):
        return self._add("sell", quantity, via="autonomy", version=version, price=price,
                         symbol=symbol, bucket=bucket)

    def person_buys(self, quantity, price=100.0, symbol="VTI", bucket=MICRO):
        return self._add("buy", quantity, via="console", version=None, price=price,
                         symbol=symbol, bucket=bucket)

    def person_sells(self, quantity, price=100.0, symbol="VTI", bucket=MICRO):
        return self._add("sell", quantity, via="console", version=None, price=price,
                         symbol=symbol, bucket=bucket)

    def view(self, held, symbol="VTI", bucket=MICRO):
        return positions.managed(fills=self.fills, proposals=self.proposals, bucket=bucket,
                                 symbol=symbol, ledger_quantity=held)


# ---------------------------------------------------------------- whose shares
def test_what_code_bought_is_what_code_manages():
    view = Log().code_buys(2.0, price=100.0).code_buys(1.0, price=130.0).view(3.0)
    assert (view.quantity, view.version_id, view.other_quantity) == (3.0, V1, 0.0)
    assert view.cost_basis == pytest.approx(110.0) and view.opened_at.startswith("2026-10-06T14:01")
    assert view.known and view.reason is None


def test_a_persons_purchase_into_the_same_holding_stays_theirs():
    view = Log().code_buys(1.0).person_buys(2.0).view(3.0)
    assert (view.quantity, view.other_quantity, view.version_id) == (1.0, 2.0, V1)
    # Bought the other way round, the answer is the same: it is provenance, not order.
    assert Log().person_buys(2.0).code_buys(1.0).view(3.0).quantity == 1.0


def test_a_holding_nobody_attributed_to_a_strategy_is_not_managed_at_all():
    assert Log().person_buys(3.0).view(3.0).quantity == 0.0
    # Nor is one code opened under the older switch, before positions were bound to a version:
    # it has no exit conditions of its own, so there is nothing for code to close it on.
    legacy = Log()._add("buy", 3.0, via="autonomy", version=None, price=100.0, symbol="VTI",
                        bucket=MICRO).view(3.0)
    assert (legacy.quantity, legacy.other_quantity, legacy.version_id) == (0.0, 3.0, None)


def test_a_person_selling_takes_their_own_shares_first_and_only_then_codes():
    mixed = Log().code_buys(1.0).person_buys(1.0)
    half = mixed.person_sells(0.5).view(1.5)
    assert (half.quantity, half.other_quantity) == (1.0, 0.5)
    more = mixed.person_sells(1.0).view(0.5)                 # the rest of theirs, and half of code's
    assert (more.quantity, more.other_quantity) == (0.5, 0.0)
    # Code is never left believing it owns shares that are gone.
    assert more.quantity + more.other_quantity == 0.5


def test_code_selling_takes_only_its_own_and_keeps_its_cost():
    view = Log().code_buys(1.0, price=100.0).person_buys(1.0, price=50.0).code_sells(0.4).view(1.6)
    assert (view.quantity, view.other_quantity) == (0.6, 1.0)
    assert view.cost_basis == pytest.approx(100.0), "the person's cheaper shares are not averaged in"


def test_a_position_ends_and_the_next_one_belongs_to_whichever_version_opens_it():
    log = Log().code_buys(1.0, V1, price=100.0).code_sells(1.0, V1)
    assert log.view(0.0).quantity == 0.0 and log.view(0.0).version_id is None
    again = log.code_buys(2.0, V2, price=80.0).view(2.0)
    assert (again.quantity, again.version_id) == (2.0, V2)
    assert again.cost_basis == pytest.approx(80.0), "the earlier life's cost is not carried in"


def test_codes_share_ending_ends_its_life_even_while_a_person_still_holds():
    log = Log().code_buys(1.0, V1).person_buys(1.0).code_sells(1.0, V1)
    over = log.view(1.0)
    assert (over.quantity, over.other_quantity, over.version_id) == (0.0, 1.0, None)
    renewed = log.code_buys(0.5, V2, price=90.0).view(1.5)
    assert (renewed.quantity, renewed.other_quantity, renewed.version_id) == (0.5, 1.0, V2)


def test_adding_under_a_new_version_does_not_rebind_a_position_that_is_still_open():
    """A position answers to the version that opened it until it is gone (docs/todo.md, P3.6)."""
    view = Log().code_buys(1.0, V1).code_buys(1.0, V2).view(2.0)
    assert (view.quantity, view.version_id) == (2.0, V1)


def test_another_symbol_and_another_account_are_other_positions():
    log = Log().code_buys(1.0).code_buys(5.0, symbol="VXUS").code_buys(7.0, bucket="core")
    assert log.view(1.0).quantity == 1.0
    assert log.view(5.0, symbol="VXUS").quantity == 5.0
    assert log.view(7.0, bucket="core").quantity == 7.0


@pytest.mark.parametrize("held, says", [(None, "could not be read"), (2.5, "cannot explain"),
                                        (0.5, "cannot explain")])
def test_a_book_that_cannot_confirm_the_replay_leaves_the_answer_unknown(held, says):
    view = Log().code_buys(1.0).view(held)
    assert view.quantity is None and not view.known and says in view.reason


def test_a_log_too_long_to_replay_refuses_rather_than_guessing(monkeypatch):
    monkeypatch.setattr(autonomy, "FILL_REPLAY_LIMIT", 2)
    view = Log().code_buys(1.0).code_buys(1.0).view(2.0)
    assert view.quantity is None and "cannot be replayed" in view.reason


def test_a_fill_with_an_unrecognised_side_refuses():
    log = Log().code_buys(1.0)
    log.fills.append({**log.fills[0], "side": "transfer", "at": "2026-10-06T15:00:00+00:00"})
    assert log.view(1.0).quantity is None


def test_the_book_lists_what_code_holds_and_what_it_cannot_account_for():
    log = Log().code_buys(1.0).person_buys(4.0, symbol="BND").code_buys(2.0, symbol="VXUS")
    ledger = SimpleNamespace(positions=[
        SimpleNamespace(symbol="VTI", bucket=MICRO, quantity=1.0),
        SimpleNamespace(symbol="BND", bucket=MICRO, quantity=4.0),        # a person's: not listed
        SimpleNamespace(symbol="VXUS", bucket=MICRO, quantity=9.0),       # unexplained: listed
        SimpleNamespace(symbol="VOO", bucket="core", quantity=1.0)])      # another account
    found = {view.symbol: view.quantity for view in positions.book(
        fills=log.fills, proposals=log.proposals, ledger=ledger, bucket=MICRO)}
    assert found == {"VTI": 1.0, "VXUS": None}


# ---------------------------------------------------------------- when to leave
def _held(**over):
    base = {"symbol": "VTI", "bucket": MICRO, "quantity": 1.0, "version_id": V1,
            "opened_at": "2026-10-06T14:00:00+00:00", "cost_basis": 100.0}
    return positions.Managed(**{**base, **over})


RULES = {"strategy": {"stop_loss_pct": 10.0, "take_profit_pct": 25.0, "max_holding_days": 30}}
DAY = date(2026, 10, 16)


@pytest.mark.parametrize("price, reason", [(90.0, positions.STOP_LOSS), (85.0, positions.STOP_LOSS),
                                           (125.0, positions.TAKE_PROFIT), (140.0, positions.TAKE_PROFIT)])
def test_a_position_is_closed_at_its_stop_and_at_its_target(price, reason):
    call = positions.exit_call(_held(), RULES, price=price, today=DAY)
    assert call.due and call.reason == reason and call.exit_by == "2026-11-05"
    assert set(call.text()) == {"zh", "en"}


@pytest.mark.parametrize("price", [90.01, 100.0, 124.99])
def test_between_the_two_it_is_held(price):
    call = positions.exit_call(_held(), RULES, price=price, today=DAY)
    assert not call.due and call.reason is None and call.text() is None


def test_it_is_closed_when_its_holding_period_ends_whatever_the_price():
    call = positions.exit_call(_held(), RULES, price=None, today=date(2026, 11, 5))
    assert call.due and call.reason == positions.MAX_HOLDING
    assert call.facts == {"held_days": 30, "limit_days": 30}
    assert not positions.exit_call(_held(), RULES, price=100.0, today=date(2026, 11, 4)).due


def test_a_condition_the_version_does_not_set_never_fires():
    bare = {"strategy": {"stop_loss_pct": None, "take_profit_pct": None, "max_holding_days": None}}
    for price in (1.0, 100.0, 10_000.0):
        call = positions.exit_call(_held(), bare, price=price, today=date(2030, 1, 1))
        assert not call.due and call.exit_by is None


@pytest.mark.parametrize("position, version, price, says", [
    (_held(), None, 50.0, "not on record"),
    (_held(quantity=None, reason="the fills cannot explain it"), RULES, 50.0, "cannot explain"),
    (_held(quantity=0.0), RULES, 50.0, "holds none"),
    (_held(), RULES, None, "no current price"),
    (_held(cost_basis=None), RULES, 50.0, "no cost on record"),
])
def test_nothing_is_sold_on_a_condition_that_could_not_be_read(position, version, price, says):
    call = positions.exit_call(position, version, price=price, today=DAY)
    assert not call.due and says in call.undecided


# ---------------------------------------------------------------- the authority to sell
@pytest.fixture
def armed(tmp_path):
    profiles = _profiles(tmp_path)
    profiles.risk["buckets"][MICRO]["autonomy"] = {"managed_exit": {"enabled": True}}
    return profiles


def ask(profiles, managed, **over):
    args = {"bucket": MICRO, "action": "SELL", "confidence": None,
            "requested_quantity": 1.0, "managed": managed}
    return autonomy.permits(profiles, **{**args, **over})


def test_code_may_sell_what_it_bought_up_to_what_it_bought(armed):
    assert ask(armed, _held(quantity=1.0)).permitted
    assert ask(armed, _held(quantity=1.0), requested_quantity=0.4, action="REDUCE").permitted
    over = ask(armed, _held(quantity=1.0), requested_quantity=1.000002)
    assert not over.permitted and "belongs to a person" in over.reasons[0]
    # No confidence is asked of an exit: a stop is not an opinion.
    assert ask(armed, _held(), confidence=None).permitted


@pytest.mark.parametrize("managed, says", [
    (_held(quantity=None, reason="shares the log cannot explain"), "could not be established"),
    (_held(quantity=0.0, version_id=None), "holds none"),
    (_held(version_id=None), "no strategy version"),
])
def test_a_claim_the_fills_do_not_bear_out_permits_nothing(armed, managed, says):
    decision = ask(armed, managed)
    assert not decision.permitted and says in decision.reasons[0]


@pytest.mark.parametrize("quantity", [None, 0, -1.0, True])
def test_an_exit_without_a_quantity_is_refused(armed, quantity):
    assert not ask(armed, _held(), requested_quantity=quantity).permitted


@pytest.mark.parametrize("action", ["BUY", "ADD", "HOLD"])
def test_a_managed_position_is_only_ever_sold_from(armed, action):
    decision = ask(armed, _held(), action=action)
    assert not decision.permitted and "not an exit" in decision.reasons[0]


@pytest.mark.parametrize("setting", [None, {}, {"enabled": False}, {"enabled": "true"}, {"enabled": 1}])
def test_the_switch_is_off_unless_it_is_exactly_on(tmp_path, setting):
    profiles = _profiles(tmp_path)
    autonomy_cfg = {"reduce": {"enabled": True, "min_confidence": 80, "max_positions_reduced_per_day": 1,
                               "min_remaining_pct_of_opened": 25},
                    "open": {"enabled": True}}
    if setting is not None:
        autonomy_cfg["managed_exit"] = setting
    profiles.risk["buckets"][MICRO]["autonomy"] = autonomy_cfg
    decision = ask(profiles, _held())
    assert not decision.permitted and "managed_exit.enabled is not true" in decision.reasons[0]


def test_the_other_switches_do_not_reach_a_sell(tmp_path):
    """Reducing and opening are on; closing a position a person bought is still a person's."""
    profiles = _profiles(tmp_path)
    profiles.risk["buckets"][MICRO]["autonomy"] = {
        "reduce": {"enabled": True, "min_confidence": 80, "max_positions_reduced_per_day": 1,
                   "min_remaining_pct_of_opened": 25},
        "open": {"enabled": True, "min_confidence": 80, "classes": ["broad_diversified_etf"],
                 "max_new_positions_per_day": 1},
        "managed_exit": {"enabled": True}}
    plain = autonomy.permits(profiles, bucket=MICRO, action="SELL", confidence=99,
                             requested_quantity=1.0, held_quantity=1.0)
    assert not plain.permitted and "never autonomous" in plain.reasons[0]


def test_a_withdrawal_stops_exits_too_and_a_daily_limit_holds_when_one_is_set(armed):
    armed.risk["buckets"][MICRO]["autonomy"]["managed_exit"]["max_exits_per_day"] = 2
    assert ask(armed, _held(), exited_today=1).permitted
    spent = ask(armed, _held(), exited_today=2)
    assert not spent.permitted and "limit of 2" in spent.reasons[0]
    autonomy.trip(armed, "an order nobody could account for")
    stopped = ask(armed, _held())
    assert not stopped.permitted and "withdrawn" in stopped.reasons[0]
    assert autonomy.is_armed(armed, MICRO) is False


def test_the_exit_switch_alone_arms_the_pass(armed):
    assert autonomy.is_armed(armed, MICRO) is True
    armed.risk["buckets"][MICRO]["autonomy"]["managed_exit"]["enabled"] = False
    assert autonomy.is_armed(armed, MICRO) is False


def test_the_engine_makes_a_managed_exit_eligible_and_nothing_else(profiles):
    """Eligibility only: ``requires_human_approval`` false is what lets the pass look at it."""
    from agentic_trading.portfolio import PortfolioState, build_snapshot, classify_instrument
    from agentic_trading.risk import HardRiskEngine, Proposal
    from agentic_trading.risk.ladder import LadderState, LadderStates, evaluate_ladders
    from agentic_trading.risk_scenarios import TRADE_DATE, identity_lookup, price_lookup, regime

    state = PortfolioState(as_of=TRADE_DATE, buckets={MICRO: {"cash_usd": 500.0}}, recent_opens=[],
                           positions=[{"symbol": "VTI", "bucket": MICRO, "quantity": 1.0,
                                       "cost_basis_per_share": 380.0, "opened_on": "2026-08-01"}])
    snap = build_snapshot(state, profiles, TRADE_DATE, price_lookup, identity_lookup)
    ladders = LadderStates(LadderState(3000.0), LadderState(2500.0), LadderState(500.0),
                           LadderState(1000.0))
    evaluate_ladders(ladders, profiles, TRADE_DATE, None, None, None,
                     micro_live_equity=snap.buckets[MICRO].equity)

    def gated(action, source, switch):
        block = profiles.risk["buckets"][MICRO]["autonomy"]
        block.pop("managed_exit", None)
        if switch is not None:
            block["managed_exit"] = {"enabled": switch}
        asked = Proposal(ticker="VTI", bucket=MICRO, action=action, size_hint="none", confidence=None,
                         trade_date=TRADE_DATE, cls=classify_instrument("VTI", profiles, identity_lookup),
                         source=source, strategy="de_risk", sell_quantity=1.0 if action == "SELL" else None)
        result = HardRiskEngine(profiles, halt_file="/nonexistent/HALT").evaluate(
            asked, snap, ladders, regime())
        assert result.verdict == "APPROVE", result.as_dict()
        return result.requires_human_approval

    assert gated("SELL", autonomy.MANAGED_EXIT_SOURCE, True) is False
    assert gated("REDUCE", autonomy.MANAGED_EXIT_SOURCE, True) is False
    # The claim without the switch, the switch without the claim, and anything short of `true`.
    assert gated("SELL", autonomy.MANAGED_EXIT_SOURCE, None) is True
    assert gated("SELL", autonomy.MANAGED_EXIT_SOURCE, False) is True
    assert gated("SELL", autonomy.MANAGED_EXIT_SOURCE, "true") is True
    assert gated("SELL", "console", True) is True
    assert gated("SELL", "execution_revalidation", True) is True


# ---------------------------------------------------------------- what a plan remembers
@pytest.fixture
def db(tmp_path):
    return StateDB(tmp_path / "state" / "proposals.json")


STRATEGY = {"template": "trend_hold", "max_positions": 3, "position_pct": 33.0, "min_confidence": 80,
            "stop_loss_pct": 10.0, "take_profit_pct": None, "max_holding_days": 90,
            "universe": ["VTI", "VXUS", "BND"], "rationale": "a first plan", "suggested_by": "template"}


def _plan(plan_id="plan-1", mode="steady", **strategy):
    return {"plan_id": plan_id, "account": {"as_of": "2026-10-06"},
            "envelope": {"mode": mode, "capital_usd": 1000.0, "loss_pause_pct": 10.0,
                         "loss_pause_usd": 100.0},
            "strategy": {**STRATEGY, **strategy}, "changes": []}


def test_a_version_is_everything_that_decides_what_it_does(profiles):
    code = store.code_identity(profiles)
    base = store.version_id("steady", STRATEGY, code)
    assert base.startswith("sv-") and base == store.version_id("steady", dict(STRATEGY), dict(code))
    # Words about a strategy are not the strategy.
    assert store.version_id("steady", {**STRATEGY, "rationale": "said differently"}, code) == base
    assert store.version_id("steady", {**STRATEGY, "suggested_by": "model"}, code) == base
    # Any parameter, the mode, the models and the code each are.
    different = {store.version_id("swing", STRATEGY, code)}
    for key, value in (("template", "other"), ("max_positions", 2), ("position_pct", 30.0),
                       ("min_confidence", 85), ("stop_loss_pct", 8.0), ("take_profit_pct", 20.0),
                       ("max_holding_days", 60), ("universe", ["VTI", "VXUS"])):
        different.add(store.version_id("steady", {**STRATEGY, key: value}, code))
    assert set(code) == {"package_version", "upstream_commit", "deep_model", "quick_model",
                         "logic", "mandate"}
    for key in code:
        different.add(store.version_id("steady", STRATEGY, {**code, key: "changed"}))
    assert base not in different and len(different) == 15


def test_the_trading_logic_is_identified_by_what_it_does_not_by_how_it_reads(tmp_path, monkeypatch):
    """A comment is not a new strategy; a changed number is. The package's version says nothing."""
    package = tmp_path / "agentic_trading"
    for name in store.LOGIC:
        (package / name).parent.mkdir(parents=True, exist_ok=True)
        (package / name).write_text("LIMIT = 10\n\n\ndef decide(x):\n    return x > LIMIT\n")
    (package / "auto" / "store.py").write_text("")
    monkeypatch.setattr(store, "__file__", str(package / "auto" / "store.py"))

    def digest():
        store.logic_digest.cache_clear()
        return store.logic_digest()

    base = digest()
    target = package / "risk" / "engine.py"
    target.write_text("# a note for the next reader\nLIMIT = 10\n\n\ndef decide(x):\n"
                      "    return x > LIMIT      # reflowed\n")
    assert digest() == base, "comments and layout do not make a new version of anything"
    target.write_text("LIMIT = 11\n\n\ndef decide(x):\n    return x > LIMIT\n")
    changed = digest()
    assert changed != base, "a changed number is changed logic"
    target.write_text("LIMIT = 10\n\n\ndef decide(x):\n    return x >= LIMIT\n")
    assert digest() not in (base, changed)
    # A module that cannot be read or parsed is part of the answer, never skipped over.
    target.write_text("def broken(:\n")
    unparsable = digest()
    target.unlink()
    assert len({base, changed, unparsable, digest()}) == 4
    store.logic_digest.cache_clear()


def test_every_module_that_decides_an_order_is_part_of_the_logic():
    from pathlib import Path

    import agentic_trading

    package = Path(agentic_trading.__file__).parent
    assert all((package / name).is_file() for name in store.LOGIC), "a renamed module left the list"
    assert {"policy.py", "sizing.py", "risk/engine.py", "execution.py", "autonomy.py",
            "auto/trader.py", "auto/positions.py"} <= set(store.LOGIC)
    # What only words or records a result is deliberately not: it would restart every validation
    # on every release for nothing.
    assert not {"diagnosis.py", "auto/store.py", "auto/paper.py"} & set(store.LOGIC)
    assert agentic_trading.__version__ == "0.1.0", (
        "if the package starts being versioned, say in code_identity what that number now means")


def test_what_the_model_is_told_is_part_of_the_model(tmp_path, profiles):
    import shutil

    copied = tmp_path / "config"
    shutil.copytree(profiles.config_dir, copied)
    stand_in = SimpleNamespace(config_dir=copied)
    base = store.mandate_digest(stand_in)
    assert base == store.mandate_digest(profiles)
    (copied / "MANDATE.md").write_text((copied / "MANDATE.md").read_text() + "\nPrefer momentum.\n")
    assert store.mandate_digest(stand_in) != base
    (copied / "MANDATE.md").unlink()
    assert store.mandate_digest(stand_in) not in (base, store.mandate_digest(profiles))


def test_the_same_strategy_confirmed_again_keeps_its_record_and_a_new_one_starts_empty(db, profiles):
    first = store.confirm_plan(db, profiles, _plan(), identity="owner", scope="paper")
    v1 = first["opening_version"]
    store.update_validation(db, v1, status=store.SIMULATING, simulated_days=12, exits_verified=True)
    store.add_evidence(db, v1, {"kind": "simulation", "days": 12})

    again = store.confirm_plan(db, profiles, _plan(plan_id="plan-1b"), identity="owner", scope="paper")
    assert again["opening_version"] == v1 and again["managing"] == []
    assert store.version(db, v1)["validation"]["simulated_days"] == 12, "not restarted by re-confirming"

    changed = store.confirm_plan(db, profiles, _plan(plan_id="plan-2", stop_loss_pct=8.0),
                                 identity="owner", scope="paper")
    v2 = changed["opening_version"]
    fresh = store.version(db, v2)
    assert v2 != v1 and fresh["parent"] == v1
    assert fresh["validation"] == {"status": store.UNVALIDATED, "simulated_days": 0,
                                   "exits_verified": False, "inherited": False, "updated_at": None}
    assert fresh["evidence"] == [], "a predecessor's evidence is not this version's"
    # What the first earned is still the first's, and moving the second never touches it.
    store.update_validation(db, v2, simulated_days=1)
    kept = store.version(db, v1)
    assert kept["validation"]["simulated_days"] == 12 and len(kept["evidence"]) == 1
    assert store.update_validation(db, "sv-nosuchversion", simulated_days=99) is None
    assert store.add_evidence(db, "sv-nosuchversion", {"kind": "backtest"}) is None


def test_a_new_plan_leaves_the_old_version_looking_after_what_it_opened(db, profiles):
    v1 = store.confirm_plan(db, profiles, _plan(), identity="owner", scope="paper")["opening_version"]
    second = store.confirm_plan(db, profiles, _plan(plan_id="plan-2", mode="swing", max_holding_days=20),
                                identity="owner", scope="paper")
    v2 = second["opening_version"]
    assert second["managing"] == [v1] and store.known_versions(db) == {v1, v2}
    # Going back to the first strategy makes it the opener again, not a third version.
    third = store.confirm_plan(db, profiles, _plan(plan_id="plan-3"), identity="owner", scope="paper")
    assert third["opening_version"] == v1 and third["managing"] == [v2]
    record = store.plan_record(db)
    assert [item["status"] for item in record["history"]] == ["superseded", "superseded"]
    assert store.active_plan(db)["plan_id"] == "plan-3"

    ended = store.withdraw_plan(db, identity="owner", reason="stopping for now")
    assert ended["status"] == "withdrawn" and store.active_plan(db) is None
    # Nothing opens now, and every version that might still own a position is still known.
    assert store.known_versions(db) == {v1, v2}
    assert store.withdraw_plan(db, identity="owner", reason="again") is None


def test_the_first_reason_for_a_pause_is_the_one_kept(db):
    assert store.state(db)["paused"] is False
    store.pause(db, reason="stepping away", by="owner")
    later = store.pause(db, reason="at the loss line", by="auto:loss_line", kind=store.LOSS_LINE)
    assert (later["reason"], later["kind"], later["by"]) == ("stepping away", store.PERSON, "owner")
    assert [entry["reason"] for entry in later["history"]] == ["stepping away", "at the loss line"]
    resumed = store.resume(db, note="back", by="owner", result_usd=-40.0)
    assert resumed["paused"] is False and resumed["reason"] is None
    assert resumed["history"][-1]["cleared"] == "stepping away"
    # It was a person's pause, so the loss line is measured from where it always was.
    assert resumed["loss_baseline_usd"] == 0.0


def test_only_a_resume_from_the_loss_line_moves_where_the_line_is_measured_from(db, profiles):
    store.pause(db, reason="at the loss line", by="auto:loss_line", kind=store.LOSS_LINE)
    assert store.resume(db, note="no figure to move it to", by="owner")["loss_baseline_usd"] == 0.0
    store.pause(db, reason="at the loss line", by="auto:loss_line", kind=store.LOSS_LINE)
    moved = store.resume(db, note="looked", by="owner", result_usd=-41.257)
    assert moved["loss_baseline_usd"] == -41.26
    assert moved["history"][-1]["loss_baseline_usd"] == {"from": 0.0, "to": -41.26}
    # A new plan's line is about what happens under that plan.
    store.confirm_plan(db, profiles, _plan(), identity="owner", scope="paper", result_usd=-55.0)
    assert store.state(db)["loss_baseline_usd"] == -55.0
    store.confirm_plan(db, profiles, _plan(plan_id="plan-2"), identity="owner", scope="paper")
    assert store.state(db)["loss_baseline_usd"] == -55.0, "left alone when no figure is given"


def test_the_history_stays_bounded(db):
    for index in range(store.MAX_HISTORY + 20):
        store.pause(db, reason=f"pause {index}", by="owner")
        store.resume(db, note="on", by="owner")
    assert len(store.state(db)["history"]) == store.MAX_HISTORY


def test_a_withdrawn_plans_versions_are_still_managing_under_the_next_one(db, profiles):
    v1 = store.confirm_plan(db, profiles, _plan(), identity="owner", scope="paper")["opening_version"]
    v2 = store.confirm_plan(db, profiles, _plan(plan_id="plan-2", stop_loss_pct=8.0),
                            identity="owner", scope="paper")["opening_version"]
    store.withdraw_plan(db, identity="owner", reason="a break")
    third = store.confirm_plan(db, profiles, _plan(plan_id="plan-3", max_holding_days=30),
                               identity="owner", scope="paper")
    assert third["opening_version"] not in (v1, v2)
    assert set(third["managing"]) == {v1, v2}, "either may still own a position it opened"
    assert store.known_versions(db) == {v1, v2, third["opening_version"]}


def test_adjusting_moves_the_strategy_and_nothing_the_person_confirmed(db, profiles):
    first = store.confirm_plan(db, profiles, _plan(), identity="owner", scope="paper")
    store.update_validation(db, first["opening_version"], simulated_days=20)
    changed = store.adjust_strategy(db, profiles, {**STRATEGY, "stop_loss_pct": 7.0},
                                    identity="auto:adjust", reason="a tighter stop")
    assert changed["opening_version"] != first["opening_version"]
    assert changed["managing"] == [first["opening_version"]]
    assert (changed["plan_id"], changed["envelope"], changed["changes"], changed["confirmed_by"]) == (
        first["plan_id"], first["envelope"], first["changes"], "owner")
    assert changed["adjusted_by"] == "auto:adjust"
    made = store.version(db, changed["opening_version"])
    assert made["parent"] == first["opening_version"] and made["reason"] == "a tighter stop"
    assert made["validation"]["simulated_days"] == 0, "twenty days were the old strategy's"
    assert store.plan_record(db)["history"][-1]["status"] == "adjusted"
    # The same strategy again is not an adjustment, and with no plan there is nothing to adjust.
    again = store.adjust_strategy(db, profiles, {**STRATEGY, "stop_loss_pct": 7.0},
                                  identity="auto:adjust", reason="again")
    assert again["opening_version"] == changed["opening_version"] and "adjusted_at" in again
    assert len(store.plan_record(db)["history"]) == 1
    store.withdraw_plan(db, identity="owner", reason="done")
    assert store.adjust_strategy(db, profiles, STRATEGY, identity="x", reason="y") is None


# ---------------------------------------------------------------- the record of having run
AT = datetime(2026, 10, 6, 14, 0, tzinfo=UTC)


def _passes(db, session, minutes, *, market_open=True, problems=()):
    row = None
    for offset in minutes:
        row = store.mark_pass(db, V1, session, at=AT + timedelta(minutes=offset),
                              market_open=market_open, problems=list(problems))
    return row


def test_a_session_counts_once_it_was_watched_for_an_hour_with_nothing_wrong(db):
    assert store.simulated_days(db, V1) == 0
    assert store.session_valid(_passes(db, "2026-10-06", [0])) is False, "one pass"
    assert store.session_valid(_passes(db, "2026-10-06", [30, 59])) is False, "not yet an hour"
    row = _passes(db, "2026-10-06", [60])
    assert store.session_valid(row) is True and store.simulated_days(db, V1) == 1
    assert row["open_passes"] == 4 and row["passes"] == 4 and row["problems"] == []
    # Passes while the market was shut are passes, and do not count toward having watched it.
    assert store.session_valid(_passes(db, "2026-10-07", [0, 120], market_open=False)) is False
    # Another version's sessions are its own.
    assert store.simulated_days(db, V2) == 0 and store.simulated_days(db, None) == 0


def test_one_bad_pass_keeps_the_session_out_whatever_follows(db):
    _passes(db, "2026-10-06", [0])
    _passes(db, "2026-10-06", [20], problems=["unknown_order"])
    row = _passes(db, "2026-10-06", [90, 180, 240])
    assert row["problems"] == ["unknown_order"] and store.session_valid(row) is False
    assert store.simulated_days(db, V1) == 0
    _passes(db, "2026-10-07", [0, 30], problems=["paused"])
    both = _passes(db, "2026-10-07", [70], problems=["regime_unknown"])
    assert both["problems"] == ["paused", "regime_unknown"]


def test_a_quiet_session_is_not_rewritten_every_minute(db):
    _passes(db, "2026-10-06", [0, 61])
    written = store.sessions_of(db, V1)["2026-10-06"]["written"]
    for minute in (62, 63, 64):
        _passes(db, "2026-10-06", [minute])
    assert store.sessions_of(db, V1)["2026-10-06"]["written"] == written
    _passes(db, "2026-10-06", [61 + store.REMARK_SECONDS // 60 + 1])
    assert store.sessions_of(db, V1)["2026-10-06"]["written"] != written
    # A problem is written at once, however recently the record was.
    _passes(db, "2026-10-06", [70], problems=["pass_failed"])
    assert store.sessions_of(db, V1)["2026-10-06"]["problems"] == ["pass_failed"]


def test_what_the_account_was_worth_is_kept_per_session_and_the_last_figure_stands(db):
    store.mark_equity(db, "2026-10-06", at=AT, figures={"equity_usd": 100.0, "funded_usd": 100.0})
    store.mark_equity(db, "2026-10-06", at=AT + timedelta(minutes=30),
                      figures={"equity_usd": 98.5, "funded_usd": 100.0})
    store.mark_equity(db, "2026-10-07", at=AT + timedelta(days=1),
                      figures={"equity_usd": 101.0, "funded_usd": 100.0})
    marks = store.equity_marks(db)
    assert list(marks) == ["2026-10-06", "2026-10-07"]
    assert marks["2026-10-06"]["equity_usd"] == 98.5 and marks["2026-10-07"]["equity_usd"] == 101.0
    assert marks["2026-10-06"]["at"].startswith("2026-10-06T14:30")
