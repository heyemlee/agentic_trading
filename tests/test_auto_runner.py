"""What runs a pass and what it writes down afterwards (``auto.runner``).

The pass itself is the trader's and is tested with it. Here: which session a moment belongs to,
when the simulated book may be dated forward, what counts as something wrong with a pass, and
that a plan's settings can travel through the console's own confirmation flow, cooling period
and all, without that flow learning to create anything it was not told it may.
"""

from datetime import UTC, datetime, timedelta

import pytest

from agentic_trading import config_changes as cc
from agentic_trading import events, risk_scenarios
from agentic_trading.auto import paper, runner, store, trader
from agentic_trading.portfolio import load_portfolio
from agentic_trading.proposals import SUBMITTED
from agentic_trading.service import ServiceError
from tests.auto_market import scripted, write_research
from tests.test_api import sandbox_service  # noqa: F401 - shared isolated fixture

MICRO = "micro_live"


@pytest.fixture
def running(sandbox_service, monkeypatch):  # noqa: F811
    """A started simulation: (the real account's service, the simulated one, prices, runs)."""
    prices = scripted(monkeypatch, dict(risk_scenarios.PRICES))
    preview = sandbox_service.auto_plan()
    sandbox_service.auto_start(plan_id=preview["plan_id"], identity="owner")
    return (sandbox_service, sandbox_service._auto_paper(), prices,
            sandbox_service.profiles().project_dir / "runs")


# ---------------------------------------------------------------- which session
@pytest.mark.parametrize("moment, session", [
    ("2026-10-06T15:00:00+00:00", "2026-10-06"),      # a Tuesday morning in New York
    ("2026-10-07T02:30:00+00:00", "2026-10-06"),      # still Tuesday there, already Wednesday in UTC
    ("2026-10-10T16:00:00+00:00", "2026-10-09"),      # Saturday belongs to Friday's session
    ("2026-10-11T23:00:00+00:00", "2026-10-09"),      # and so does Sunday
    ("2026-10-12T13:00:00+00:00", "2026-10-12"),      # Monday is its own
])
def test_a_moment_belongs_to_the_markets_day_not_the_machines(moment, session):
    assert runner.session_of(datetime.fromisoformat(moment)) == session


# ---------------------------------------------------------------- dating the simulated book
def test_the_simulated_book_is_dated_forward_only_when_nothing_is_outstanding(running):
    _service, sim, _prices, _runs = running
    profiles = sim.profiles()
    path = paper.ledger_path(profiles)
    opened = load_portfolio(path).as_of
    later = (datetime.fromisoformat(opened) + timedelta(days=3)).date().isoformat()
    assert runner.confirm_book(sim, profiles, opened) is None, "already dated to the session"
    assert load_portfolio(path).as_of == opened

    # An order whose outcome is not on record: the book is about to change, so it is not dated.
    from agentic_trading.execution_scenarios import _proposal

    waiting = _proposal(ticker="VTI", bucket=MICRO, status=SUBMITTED)
    waiting.client_order_id = "ct-outstanding-1"
    store = sim._store(profiles)
    store.save(waiting)
    assert "outstanding" in runner.confirm_book(sim, profiles, later)
    assert load_portfolio(path).as_of == opened

    # An executed order the book never received: dating it would hide that it is behind.
    waiting.status, waiting.reconciled_at = "EXECUTED", "2026-10-06T15:00:00+00:00"
    store.save(waiting)
    refused = runner.confirm_book(sim, profiles, later)
    assert "never reached this ledger" in refused and load_portfolio(path).as_of == opened

    # Nothing outstanding and nothing missing: it is dated, and says what it was checked on.
    waiting.status, waiting.client_order_id = "REJECTED", None
    store.save(waiting)
    assert runner.confirm_book(sim, profiles, later) is None
    confirmed = load_portfolio(path)
    assert confirmed.as_of == later and "no venue to check against" in confirmed.notes


def test_a_real_accounts_book_is_never_dated_from_here(running):
    service, _sim, _prices, _runs = running
    profiles = service.profiles()
    path = profiles.project_dir / "state" / "micro_live_portfolio.yaml"
    before = path.read_bytes()
    assert runner.confirm_book(service, profiles, "2030-01-01") is None
    assert path.read_bytes() == before


def test_a_book_left_alone_for_days_does_not_stop_the_simulation(running):
    """Nothing trades for two weeks. The book's date is what would otherwise fail it closed."""
    _service, sim, _prices, runs = running
    for days in (5, 12):
        result = runner.run(sim, research_from=runs, now=datetime.now(UTC) + timedelta(days=days))
        assert result["error"] is None and result["ran"] is True and result["book_unconfirmed"] is None
    assert load_portfolio(paper.ledger_path(sim.profiles())).as_of == result["session"]


# ---------------------------------------------------------------- what is wrong with a pass
def _tick(**over):
    health = {"regime_available": True, "unknown_orders": [], "autonomy_withdrawn": False}
    return trader.Tick(health={**health, **over.pop("health", {})}, **over)


@pytest.mark.parametrize("tick, error, unconfirmed, found", [
    (_tick(), None, None, []),
    (None, "RuntimeError: prices unavailable", None, [runner.PASS_FAILED]),
    (_tick(), None, "an order is still outstanding against the book", [runner.BOOK_UNCONFIRMED]),
    (_tick(paused={"reason": "away", "kind": "person", "at": "x"}), None, None, [runner.PAUSED]),
    (_tick(health={"autonomy_withdrawn": True}), None, None, [runner.AUTONOMY_WITHDRAWN]),
    (_tick(health={"unknown_orders": ["p1"]}), None, None, [runner.UNKNOWN_ORDER]),
    (_tick(loss={"known": False, "unpriced": ["VTI"]}), None, None, [runner.RESULT_UNKNOWN]),
    (_tick(health={"regime_available": False}), None, None, [runner.REGIME_UNKNOWN]),
    (_tick(paused={"reason": "x", "kind": "loss_line", "at": "x"}, health={"unknown_orders": ["p"]}),
     None, None, [runner.PAUSED, runner.UNKNOWN_ORDER]),
])
def test_each_thing_that_can_be_wrong_with_a_pass_has_a_name(tick, error, unconfirmed, found):
    assert runner.problems_of(tick, error=error, unconfirmed=unconfirmed) == found
    assert set(found) <= set(runner.PROBLEMS)


def test_one_failed_read_of_the_index_does_not_cost_the_session(running, monkeypatch):
    """Every session from 2026-10-07 to 10-13 was kept out of the count as ``regime_unknown``.

    A pass reads the regime for the session in progress, which upstream fetches from Yahoo again
    every fifteen minutes, and on each of those days one to four of the fetches failed for a
    moment (tests/test_market_regime.py). One pass's problem is the session's, so one read was
    the day.
    """
    from tradingagents.dataflows import stockstats_utils

    from agentic_trading import cli
    from agentic_trading.risk import regime
    from tests.test_market_regime import vendor

    _service, sim, _prices, runs = running
    flaky = vendor({"SPY": 1})
    monkeypatch.setattr(stockstats_utils, "load_ohlcv", flaky)
    monkeypatch.setattr(cli, "assess_regime", regime.assess_regime)
    monkeypatch.setattr(regime, "RETRY_PAUSES_SECONDS", (0.0, 0.0))
    first = runner.run(sim, research_from=runs)
    assert first["health"]["regime_available"] is True
    assert first["recorded"]["problems"] == [] and flaky.calls[:2] == ["SPY", "SPY"]

    # A vendor that does not answer at all is what it always was: unknown, and the session says so.
    monkeypatch.setattr(stockstats_utils, "load_ohlcv", vendor({"SPY": 99}))
    second = runner.run(sim, research_from=runs)
    assert second["health"]["regime_available"] is False
    assert second["recorded"]["problems"] == [runner.REGIME_UNKNOWN]


def test_every_problem_has_words_for_a_person():
    from agentic_trading.auto import cli

    assert set(cli.PROBLEM_TEXT) == set(runner.PROBLEMS)


def test_an_unsettled_order_is_reconciled_before_the_pass_decides_anything(running):
    _service, sim, _prices, runs = running
    write_research(runs, "VTI", confidence=88)
    first = runner.run(sim, research_from=runs)
    assert first["recorded"]["problems"] == [] and first["settled"] == 0
    # The record of that order loses track of it: reading the venue had failed.
    bought = sim._store(sim.profiles()).all()[0]
    bought.reconciliation_error = "order synchronization unavailable"
    sim._store(sim.profiles()).save(bought)
    second = runner.run(sim, research_from=runs)
    # Settled first -- the venue knew the order -- so the pass that followed ran on a book
    # nothing was outstanding against, and the session is not marked for it.
    assert second["settled"] == 1 and second["recorded"]["problems"] == []
    assert sim._store(sim.profiles()).all()[0].reconciliation_error is None


# ---------------------------------------------------------------- a plan's settings, confirmed
PATH = "buckets.micro_live.autonomy.managed_exit.enabled"


def test_a_plans_settings_go_through_the_consoles_own_confirmation(sandbox_service):  # noqa: F811
    """The live path a plan will take (docs/todo.md, P1.4): the same flow, password and day's wait."""
    service = sandbox_service
    profiles = service.profiles()
    current = (profiles.config_dir / "risk_profile.yaml").read_text()
    spec = cc.ChangeSpec(file="risk_profile.yaml", set={PATH: True, "sizing.fit_to_limits": True},
                         create=frozenset({PATH, "sizing.fit_to_limits"}))
    built = cc.build(current, spec)
    cc.validate(profiles.config_dir, "risk_profile.yaml", built)
    verdict = cc.classify("risk_profile.yaml", current, built)
    assert verdict["classification"] == cc.COOLING, "a new authority waits, like any loosening"
    record = cc.new_record(spec, base=cc.base_of(current, spec), verdict=verdict, identity="owner",
                           reason="plan confirmed")
    assert record["status"] == cc.PENDING and record["create"] == sorted([PATH, "sizing.fit_to_limits"])
    assert "create" not in cc.public(record), "the console is shown the change, not how it is built"

    # A day later it is applied from the stored record, which says again what it may create.
    restored = cc.spec_of(record)
    assert restored.create == spec.create and cc.moved(current, restored, record["base"]) is None
    assert cc.build(current, restored) == built


@pytest.mark.parametrize("path", ["sizing.made_up", "buckets.micro_live.autonomy.open.enabledd",
                                  "paper_trading.enabled", "buckets.core.autonomy.managed_exit.enabled"])
def test_only_the_named_settings_can_ever_be_created(sandbox_service, path):  # noqa: F811
    current = (sandbox_service.profiles().config_dir / "risk_profile.yaml").read_text()
    with pytest.raises(cc.ConfigChangeError, match="may not be created from here"):
        cc.build(current, cc.ChangeSpec(file="risk_profile.yaml", set={path: True},
                                        create=frozenset({path})))
    # Nor by a record somebody wrote into the database by hand.
    forged = {"file": "risk_profile.yaml", "set": {path: True}, "create": [path]}
    with pytest.raises(cc.ConfigChangeError):
        cc.build(current, cc.spec_of(forged))


def test_nothing_is_created_in_another_file_and_a_request_cannot_ask_for_it(sandbox_service):  # noqa: F811
    current = (sandbox_service.profiles().config_dir / "investor_profile.yaml").read_text()
    with pytest.raises(cc.ConfigChangeError, match="only ever created in risk_profile.yaml"):
        cc.build(current, cc.ChangeSpec(file="investor_profile.yaml", set={PATH: True},
                                        create=frozenset({PATH})))
    # The console's own endpoints build a spec from the request and have no way to say "create".
    with pytest.raises(ServiceError, match="no such setting"):
        sandbox_service.preview_config_change(file="risk_profile.yaml", set={PATH: True})
    with pytest.raises(TypeError):
        sandbox_service.submit_config_change(file="risk_profile.yaml", set={PATH: True},
                                             create=[PATH], confirmed=True)


# ---------------------------------------------------------------- the code that is running
def test_the_simulated_plan_follows_the_code_and_starts_that_codes_record_from_nothing(running, monkeypatch):
    """Same parameters on different trading logic are a different strategy version (P2.2)."""
    live, sim, _prices, _runs = running
    db = sim.state_db()
    first = store.active_plan(db)["opening_version"]
    at = datetime.now(UTC)
    for minutes in (0, 90):
        store.mark_pass(db, first, "2026-10-01", at=at - timedelta(days=5) + timedelta(minutes=minutes),
                        market_open=True, problems=[])
    assert store.simulated_days(db, first) == 1
    assert live.auto_tick()["version_moved"] is None, "nothing moved, so nothing is rebound"

    # A release changes the trading logic.
    monkeypatch.setattr(store, "logic_digest", lambda: "a-later-release")
    moved = live.auto_tick()["version_moved"]
    second = store.active_plan(db)["opening_version"]
    assert moved == {"from": first, "to": second, "action": "rebound"} and second != first
    plan = store.active_plan(db)
    assert plan["managing"] == [first], "the version it replaces still looks after what it bought"
    record = store.version(db, second)
    assert record["parent"] == first and record["strategy"] == store.version(db, first)["strategy"]
    assert record["code"]["logic"] == "a-later-release"
    assert record["validation"]["inherited"] is False
    # Its record is its own and starts empty; the first version keeps the day it earned.
    assert store.simulated_days(db, second) == 0 and store.simulated_days(db, first) == 1
    assert live.auto_simulation()["sessions"]["counted"] == 0
    entry = events.query(db, kinds=("auto_strategy_adjusted",))["events"][0]
    assert entry["actor"] == runner.CODE_CHANGED and entry["detail"]["replaces"] == first
    assert live.auto_tick()["version_moved"] is None, "moved once, not on every pass"


def test_a_pass_started_by_hand_from_other_code_does_not_move_the_plan(running, monkeypatch, capsys):
    """A terminal in a checkout may be running other code than the service, which runs a release."""
    live, sim, _prices, _runs = running
    db = sim.state_db()
    first = store.active_plan(db)["opening_version"]
    monkeypatch.setattr(store, "logic_digest", lambda: "the-checkout-has-moved-on")
    refused = live.auto_tick(real_account=False, rebind=False)
    assert refused["ran"] is False and refused["recorded"] is None
    assert "not the plan's strategy version" in refused["skipped"] and first in refused["skipped"]
    assert store.active_plan(db)["opening_version"] == first and store.sessions_of(db, first) == {}

    # From the command line that is the default, and saying so moves it.
    from agentic_trading.auto import cli as auto_cli

    monkeypatch.setattr("agentic_trading.service.Service", lambda _config: live)
    config = str(live.profiles().config_dir)
    assert auto_cli.main(["--config-dir", config, "tick"]) == 0
    assert "not the plan's strategy version" in capsys.readouterr().out
    assert store.active_plan(db)["opening_version"] == first
    assert auto_cli.main(["--config-dir", config, "tick", "--new-version"]) == 0
    assert store.active_plan(db)["opening_version"] != first


def test_the_closed_list_is_exactly_what_a_plan_names(sandbox_service):  # noqa: F811
    from agentic_trading.service.auto import PLAN_CREATES

    assert PLAN_CREATES is cc.CREATABLE
    plan = sandbox_service.auto_plan()
    created = {change["path"] for change in plan["changes"] if change["from"] is None}
    assert created <= cc.CREATABLE, sorted(created - cc.CREATABLE)
    # And each has a rule: none of them lands unclassified by accident.
    for path in cc.CREATABLE:
        assert cc._guarded("risk_profile.yaml", path) and cc._rule(path) is not None, path
