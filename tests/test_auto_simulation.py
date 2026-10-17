"""The simulated account as a person runs it: started from a plan, run, paused, funded, adjusted.

The service every door goes through (docs/todo.md, P1.4 and P2.1): the API, ``ct-auto`` and the
background loop call the same methods, so what holds here holds for all three. The account is
the sandbox's own $100 Agentic ledger; the market is the test's (tests/auto_market.py).

What these hold that nothing below the service can: that starting a simulation changes nothing
real, that what is started is what was previewed, that a session counts toward the record only
when the account was actually run through it, and that money put in is never reported as made.
"""

from datetime import UTC, date, datetime, timedelta

import pytest

from agentic_trading import events, risk_scenarios
from agentic_trading.auto import cli, modes, paper, runner, store
from agentic_trading.portfolio import load_portfolio
from agentic_trading.profile import load_profiles
from agentic_trading.service import Service, ServiceConflict, ServiceError
from agentic_trading.service.shapes import MarketStatus
from tests.auto_market import scripted, write_research
from tests.test_api import sandbox_service  # noqa: F401 - shared isolated fixture

MICRO = "micro_live"


@pytest.fixture
def desk(sandbox_service, monkeypatch):  # noqa: F811
    """The sandbox deployment with a scripted market: (service, prices, runs directory)."""
    prices = scripted(monkeypatch, dict(risk_scenarios.PRICES))
    return sandbox_service, prices, sandbox_service.profiles().project_dir / "runs"


def start(service, **envelope):
    preview = service.auto_plan(**envelope)
    return preview, service.auto_start(plan_id=preview["plan_id"], identity="owner", **envelope)


def _real(service):
    """Everything about the real account a simulation must leave exactly as it found it."""
    profiles = service.profiles()
    config = {path.name: path.read_bytes() for path in sorted(profiles.config_dir.glob("*"))
              if path.is_file()}
    ledger = load_portfolio(profiles.project_dir / "state" / "micro_live_portfolio.yaml").model_dump()
    return config, ledger, [p.proposal_id for p in service._store(profiles).all()]


# ---------------------------------------------------------------- before anything is started
def test_nothing_exists_and_nothing_runs_until_a_person_starts_it(desk):
    service, _prices, _runs = desk
    root = paper.root_of(service.profiles())
    assert service.auto_simulation()["exists"] is False
    assert "还没有启动" in service.auto_simulation()["note"]["zh"]
    assert service.auto_tick() == {"ran": False, "skipped": "the simulated account has not been started"}
    status = service.auto_status()
    assert status["simulation"]["exists"] is False
    real = status["real_account"]
    assert (real["plan"], real["open"], real["request"], real["opened_modes"]) == (None, False, None, [])
    assert not any(row["open_for_real_money"] for row in status["modes"])
    for act in (lambda: service.auto_pause(reason="x"), lambda: service.auto_resume(note="x"),
                lambda: service.auto_stop(reason="x"), lambda: service.auto_adjust(strategy={"max_positions": 1}),
                lambda: service.auto_fund(amount_usd=10, kind="deposit")):
        with pytest.raises(ServiceError, match="has not been started"):
            act()
    assert not root.exists(), "asking about it does not bring it into being"


# ---------------------------------------------------------------- starting
def test_starting_puts_the_previewed_plan_in_force_and_changes_nothing_real(desk):
    service, _prices, _runs = desk
    before = _real(service)
    preview, started = start(service)
    assert started["started"] is True and started["scope"] == "paper"
    assert started["plan_id"] == preview["plan_id"] and started["account_opened"] is True
    assert started["opening_version"].startswith("sv-")

    sim = started["simulation"]
    assert sim["exists"] is True and sim["state"]["paused"] is False
    assert sim["plan"]["plan_id"] == preview["plan_id"]
    assert sim["plan"]["envelope"] == preview["envelope"] and sim["plan"]["strategy"] == preview["strategy"]
    assert sim["plan"]["confirmed_by"] == "owner" and sim["plan"]["managing"] == []
    # A record that has not been made yet: nothing is counted, nothing is verified.
    assert sim["version"]["validation"] == {"status": store.UNVALIDATED, "simulated_days": 0,
                                            "exits_verified": False, "inherited": False,
                                            "updated_at": None}
    assert sim["sessions"] == {"counted": 0, "needed_to_open": 30, "needed_to_replace": 180,
                               "recorded": 0, "not_counted": []}
    # It opens with what the real account holds, recorded as money put in.
    assert sim["funding"] == {"net_usd": 100.0, "deposits_usd": 100.0, "withdrawals_usd": 0.0,
                              "cash_usd": 100.0}
    assert sim["account"] is None and sim["positions"] == [] and sim["trades"] == []

    assert _real(service) == before, "the real configuration, ledger and proposals are untouched"
    assert store.active_plan(service.state_db()) is None, "no plan is in force on the real account"
    # The simulated account runs under the plan's settings; the real one still has its own.
    rehearsed = load_profiles(paper.root_of(service.profiles()) / "config")
    exit_switch = ("buckets", MICRO, "autonomy", "managed_exit", "enabled")
    assert rehearsed.risk_cfg(*exit_switch, default=None) is True
    assert rehearsed.risk_cfg("buckets", MICRO, "autonomy", "open", "enabled", default=None) is True
    assert rehearsed.risk_cfg("sizing", "fit_to_limits", default=None) is True
    assert service.profiles().risk_cfg(*exit_switch, default=None) is None
    assert service.profiles().risk_cfg("buckets", MICRO, "autonomy", "open", "enabled", default=None) is False


def test_starting_is_on_the_record_in_both_places(desk):
    service, _prices, _runs = desk
    preview, started = start(service)
    sim = service._auto_paper()
    for where in (service.state_db(), sim.state_db()):
        entry = events.query(where, kinds=("auto_plan_started",))["events"][0]
        assert entry["subject"] == preview["plan_id"] and entry["actor"] == "owner"
        assert entry["detail"]["scope"] == "paper"
        assert entry["detail"]["opening_version"] == started["opening_version"]


def test_what_is_started_is_what_was_previewed_or_nothing_is(desk):
    service, _prices, _runs = desk
    preview = service.auto_plan()
    root = paper.root_of(service.profiles())
    with pytest.raises(ServiceConflict, match="no longer the one that was previewed"):
        service.auto_start(plan_id="0" * 16, identity="owner")
    # The id of one envelope does not start another.
    with pytest.raises(ServiceConflict):
        service.auto_start(plan_id=preview["plan_id"], capital_usd=80.0, identity="owner")
    with pytest.raises(ServiceConflict):
        service.auto_start(plan_id=preview["plan_id"], loss_pause_pct=5.0, identity="owner")
    # Nor does it survive the configuration moving underneath it.
    risk = service.profiles().config_dir / "risk_profile.yaml"
    risk.write_text(risk.read_text().replace("min_order_usd: 25", "min_order_usd: 20"))
    with pytest.raises(ServiceConflict):
        service.auto_start(plan_id=preview["plan_id"], identity="owner")
    assert not root.exists(), "a refused start prepares nothing"


def test_a_strategy_handed_back_is_clamped_like_any_other(desk):
    service, _prices, _runs = desk
    preview = service.auto_plan()
    # Somebody edits the strategy block on its way back: more positions than the account allows,
    # a stop far past the bound, a fund nobody may buy automatically.
    wild = {**preview["strategy"], "suggested_by": "model", "max_positions": 40,
            "stop_loss_pct": 60.0, "min_confidence": 10, "universe": ["VTI", "AAPL"]}
    with pytest.raises(ServiceConflict):
        service.auto_start(plan_id=preview["plan_id"], strategy=wild, identity="owner")
    # Asked for as its own plan, what comes out is inside the bounds whatever went in.
    snap, _ = service._auto_snapshot(service.profiles())
    document = service._auto_document(service.profiles(), snap, mode="steady", capital_usd=None,
                                      loss_pause_pct=None, strategy=wild)
    kept = document["strategy"]
    assert kept["max_positions"] <= 3 and kept["stop_loss_pct"] <= 15.0
    assert kept["min_confidence"] >= 80 and kept["universe"] == ["VTI"]
    assert {note["field"] for note in document["clamps"]} >= {"max_positions", "stop_loss_pct",
                                                              "min_confidence", "universe"}


def test_the_real_account_cannot_be_started_and_the_refusal_says_why(desk):
    service, _prices, _runs = desk
    preview = service.auto_plan()
    with pytest.raises(ServiceError) as refused:
        service.auto_start(plan_id=preview["plan_id"], scope="live", identity="owner")
    assert "not open on the real account" in str(refused.value)
    assert "at least 30 valid simulated sessions" in str(refused.value)
    # The password does not come into it: what is missing is said first, and it is not asked for.
    with pytest.raises(ServiceError, match="not open on the real account"):
        service.auto_start(plan_id=preview["plan_id"], scope="live", identity="owner", confirmed=True)
    assert store.active_plan(service.state_db()) is None and store.start_request(service.state_db()) is None
    with pytest.raises(ServiceError, match="cannot be simulated yet"):
        service.auto_start(plan_id=preview["plan_id"], mode="intraday", identity="owner")
    assert not paper.root_of(service.profiles()).exists()


def test_starting_again_under_a_new_plan_keeps_the_account_and_the_old_version(desk):
    service, _prices, _runs = desk
    _preview, first = start(service)
    service.auto_fund(amount_usd=40.0, kind="deposit")
    _again, second = start(service, loss_pause_pct=5.0)
    # The same envelope size and strategy, a different loss line: the same strategy version.
    assert second["account_opened"] is False and second["opening_version"] == first["opening_version"]
    assert second["simulation"]["funding"]["cash_usd"] == 140.0, "the account is not reopened"
    assert second["simulation"]["plan"]["envelope"]["loss_pause_pct"] == 5.0


# ---------------------------------------------------------------- running
def test_a_pass_trades_the_simulated_account_on_the_real_accounts_research(desk):
    service, _prices, runs = desk
    before = _real(service)
    start(service)
    run_id = write_research(runs, "VTI", confidence=88)
    result = service.auto_tick()
    assert result["ran"] is True and result["error"] is None
    assert result["signals"][0]["run_id"] == run_id
    assert result["signals"][0]["diagnosis"]["code"] == "proposal_automatic"
    assert result["approvals"]["approved"][0]["outcome"] == "SUBMITTED"
    assert result["recorded"]["problems"] == [] and result["recorded"]["market_open"] is True
    assert result["recorded"]["simulated_days"] == 0, "one pass is not a session watched"

    sim = service.auto_simulation()
    assert [(row["symbol"], row["strategy_version"]) for row in sim["positions"]] == [
        ("VTI", sim["plan"]["opening_version"])]
    assert sim["signals"][0]["ticker"] == "VTI" and sim["signals"][0]["final"] is True
    # $33 of the $100, as the plan said it would be.
    ledger = load_portfolio(paper.ledger_path(service._auto_paper().profiles()))
    assert ledger.buckets[MICRO].cash_usd == pytest.approx(67.0, abs=0.01)
    assert _real(service) == before, "and the real account has seen none of it"


def test_a_session_counts_only_when_it_was_watched_and_nothing_was_wrong(desk, monkeypatch):
    service, _prices, runs = desk
    _preview, started = start(service)
    sim = service._auto_paper()
    version = started["opening_version"]
    # Weekday sessions from the one the runner is in now (runner.session_of: New York's date, a
    # weekend belonging to the Friday). Counted from today in UTC, as they once were, a Saturday
    # or Sunday rolled day(0) on to Monday and day(1) onto the same Monday, whose ten-minute
    # passes then spoiled the session day(0) had counted -- 1 where 2 was right, on every
    # weekend in UTC (the pre-push gate met it at 21:16 PDT on 2026-10-16).
    first = datetime.combine(date.fromisoformat(runner.session_of(datetime.now(UTC))),
                             datetime.min.time(), tzinfo=UTC).replace(hour=15)

    def day(offset):
        """Two passes, an hour and a bit apart, in the session ``offset`` weekdays on."""
        moment, seen = first, 0
        while seen < offset:
            moment += timedelta(days=1)
            seen += moment.weekday() < 5
        while moment.weekday() >= 5:
            moment += timedelta(days=1)
        return moment

    def watch(moment, gap=61):
        runner.run(sim, research_from=runs, now=moment)
        return runner.run(sim, research_from=runs, now=moment + timedelta(minutes=gap))["recorded"]

    # Watched, and nothing wrong: it counts, though nothing was bought. Nobody trades for a count.
    counted = watch(day(0))
    assert counted["session_valid"] is True and counted["simulated_days"] == 1
    # One pass, or passes too close together to have watched anything: it does not.
    assert watch(day(1), gap=10)["session_valid"] is False
    # Paused for the session: the strategy was not running, so the session is not its record.
    service.auto_pause(reason="away", identity="owner")
    paused = watch(day(2))
    assert paused["problems"] == [runner.PAUSED] and paused["session_valid"] is False
    service.auto_resume(note="back", identity="owner")
    # The market shut: passes ran, and none of them was a pass through an open market.
    monkeypatch.setattr(Service, "market_status", lambda self: MarketStatus(is_open=False))
    assert watch(day(3))["session_valid"] is False
    monkeypatch.setattr(Service, "market_status", lambda self: MarketStatus(is_open=True))
    # A pass that could not read the book: the session it failed in is not a good one, and the
    # passes after it do not make it one again.
    broken = day(4)
    monkeypatch.setattr("agentic_trading.auto.trader.read_book",
                        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("prices unavailable")))
    failed = runner.run(sim, research_from=runs, now=broken)
    assert failed["error"].startswith("RuntimeError") and failed["recorded"]["problems"] == [runner.PASS_FAILED]
    monkeypatch.undo()
    scripted(monkeypatch, dict(risk_scenarios.PRICES))
    assert watch(broken + timedelta(minutes=5))["session_valid"] is False
    assert watch(day(5))["simulated_days"] == 2

    # The count is the record's own, and it is what the mode is held to.
    assert store.simulated_days(sim.state_db(), version) == 2
    assert store.version(sim.state_db(), version)["validation"]["simulated_days"] == 2
    view = service.auto_simulation()["sessions"]
    assert view["counted"] == 2 and view["recorded"] == 6
    assert {tuple(row["problems"]) for row in view["not_counted"]} == {(), ("paused",), ("pass_failed",)}
    steady = service.auto_status()["modes"][0]
    assert steady["simulated_days"] == 2 and steady["open_for_real_money"] is False
    assert "只有 2 个有效交易日" in steady["blockers"][0]["text"]["zh"]


def test_a_purchase_carried_to_a_sale_is_what_verifies_the_exit(desk):
    service, prices, runs = desk
    start(service)
    write_research(runs, "VTI", confidence=88)
    bought = service.auto_tick()["recorded"]
    assert bought["exits_verified"] is False
    assert any(b["code"] == "exit_not_exercised" for b in service.auto_status()["modes"][0]["blockers"])
    prices["VTI"] = round(prices["VTI"] * 0.88, 4)                   # past the 10% stop
    sold = service.auto_tick()
    assert sold["exits"][0]["exit"]["reason"] == "stop_loss" and sold["recorded"]["exits_verified"] is True
    sim = service.auto_simulation()
    assert sim["positions"] == [] and sim["version"]["validation"]["exits_verified"] is True
    trade = sim["trades"][0]
    assert (trade["symbol"], trade["closed_by"], trade["version"]) == (
        "VTI", "code", sim["plan"]["opening_version"])
    assert trade["realized_usd"] == pytest.approx(-3.96, abs=0.02) and trade["return_pct"] < -11.9
    blockers = {b["code"] for b in service.auto_status()["modes"][0]["blockers"]}
    assert "exit_not_exercised" not in blockers and "simulation_record_short" in blockers


def test_a_position_a_person_sold_out_of_does_not_verify_codes_exit(desk):
    service, _prices, runs = desk
    start(service)
    write_research(runs, "VTI", confidence=88)
    service.auto_tick()
    sim = service._auto_paper()
    profiles = sim.profiles()
    # Autonomy is withdrawn and a person sells the position by hand in the simulated account.
    from agentic_trading import autonomy

    autonomy.trip(profiles, "looking at something")
    prices = sim._store(profiles)
    held = load_portfolio(paper.ledger_path(profiles)).position("VTI", MICRO)
    from agentic_trading.execution_scenarios import _proposal
    from agentic_trading.proposals import PROPOSED
    from tests.auto_market import today

    manual = _proposal(ticker="VTI", bucket=MICRO, action="SELL", status=PROPOSED)
    manual.trade_date, manual.sell_quantity, manual.positions_fingerprint = today(), held.quantity, None
    manual.authority_version = autonomy.note_version(profiles, sim.state_db(profiles))
    prices.save(manual)
    answer = sim.approve(manual.proposal_id, identity="owner", version=manual.version(),
                         request_id="by-hand", authority_version=manual.authority_version,
                         terms={"ticker": "VTI", "bucket": MICRO, "action": "SELL",
                                "notional_usd": manual.notional_usd, "sell_quantity": manual.sell_quantity})
    assert answer["submitted"] is True
    autonomy.reset(profiles, "done")
    after = service.auto_tick()["recorded"]
    assert after["exits_verified"] is False
    assert service.auto_simulation()["trades"][0]["closed_by"] == "person"


# ---------------------------------------------------------------- pausing and stopping
def test_pause_resume_and_stop_through_the_service(desk):
    service, prices, runs = desk
    start(service)
    paused = service.auto_pause(reason="  stepping away  ", identity="owner")
    assert paused["state"] == {"paused": True, "reason": "stepping away", "kind": "person",
                               "at": paused["state"]["at"], "by": "owner", "loss_baseline_usd": 0.0}
    write_research(runs, "VTI", confidence=88)
    assert service.auto_tick()["signals"][0]["diagnosis"]["code"] == "plan_paused"
    waiting = service.auto_simulation()["signals"][0]
    assert waiting["final"] is False, "held back, not written off"

    resumed = service.auto_resume(note="back", identity="owner")
    assert resumed["changed"] is True and resumed["loss_line_moved"] is None
    assert service.auto_resume(note="again")["changed"] is False
    assert service.auto_tick()["signals"][0]["diagnosis"]["code"] == "proposal_automatic"

    stopped = service.auto_stop(reason="done for now", identity="owner")
    assert stopped["stopped"] is True and stopped["simulation"]["plan"] is None
    assert service.auto_stop(reason="again") == {"stopped": False, "scope": "paper",
                                                 "reason": "no plan is in force"}
    # Stopped is not abandoned: what it bought is still closed at its stop.
    prices["VTI"] = round(prices["VTI"] * 0.85, 4)
    after = service.auto_tick()
    assert after["exits"][0]["exit"]["reason"] == "stop_loss" and after["signals"] == []
    assert service.auto_simulation()["positions"] == []
    kinds = [entry["kind"] for entry in events.query(service._auto_paper().state_db())["events"]]
    assert {"auto_plan_started", "auto_paused", "auto_resumed", "auto_plan_stopped"} <= set(kinds)


def test_resuming_from_the_loss_line_says_where_the_line_now_starts(desk):
    service, prices, runs = desk
    start(service, loss_pause_pct=2.0)                              # a $2 line on $100
    write_research(runs, "VTI", confidence=88)
    service.auto_tick()
    prices["VTI"] = round(prices["VTI"] * 0.88, 4)                  # the stop loses about $3.96
    hit = service.auto_tick()
    assert hit["paused"]["kind"] == "loss_line" and hit["loss"]["reached"] is True
    lost = service.auto_tick()["loss"]["total_usd"]
    resumed = service.auto_resume(note="reviewed", identity="owner")
    assert resumed["loss_line_moved"] == {"from": 0.0, "to": lost}
    assert resumed["state"]["loss_baseline_usd"] == lost and lost < -2.0
    after = service.auto_tick()
    assert after["paused"] is None and after["loss"]["since_usd"] == 0.0


# ---------------------------------------------------------------- money in and out
def test_money_put_in_is_never_reported_as_money_made(desk):
    service, _prices, _runs = desk
    start(service)
    deposited = service.auto_fund(amount_usd=50.0, kind="deposit", note="topping up", identity="owner")
    assert deposited == {"recorded": True, "kind": "deposit", "amount_usd": 50.0, "cash_usd": 150.0}
    account = service.auto_tick()["account"]
    assert account["equity_usd"] == 150.0 and account["funded_usd"] == 150.0
    assert account["result_usd"] == 0.0 and account["result_pct"] == 0.0, "a deposit is not a gain"

    service.auto_fund(amount_usd=30.0, kind="withdrawal", identity="owner")
    sim = service.auto_simulation()
    assert sim["funding"] == {"net_usd": 120.0, "deposits_usd": 150.0, "withdrawals_usd": 30.0,
                              "cash_usd": 120.0}
    again = service.auto_tick()["account"]
    assert again["equity_usd"] == 120.0 and again["result_usd"] == 0.0, "nor a withdrawal a loss"
    ledger = load_portfolio(paper.ledger_path(service._auto_paper().profiles()))
    assert [(flow.kind, flow.amount_usd) for flow in ledger.cash_flows] == [
        ("deposit", 100.0), ("deposit", 50.0), ("withdrawal", -30.0)]
    # What the account was worth is kept per session, with the funding beside it.
    marks = store.equity_marks(service._auto_paper().state_db())
    assert list(marks.values())[-1]["funded_usd"] == 120.0


def test_only_money_that_is_there_and_free_can_be_taken_out(desk):
    service, _prices, _runs = desk
    start(service)
    with pytest.raises(ServiceError, match="cannot be taken out"):
        service.auto_fund(amount_usd=100.01, kind="withdrawal")
    sim = service._auto_paper()
    with sim.state_db().transaction() as conn:
        conn.execute("INSERT INTO reservations VALUES(?,?,?,?,?,'ACTIVE',?)",
                     ("ct-working-1", "p-working", MICRO, 35.0, 0.0, "2026-10-06T00:00:00+00:00"))
    with pytest.raises(ServiceError, match=r"\$35\.00 of it held"):
        service.auto_fund(amount_usd=70.0, kind="withdrawal")
    assert service.auto_fund(amount_usd=65.0, kind="withdrawal")["cash_usd"] == 35.0
    for amount, kind in ((0, "deposit"), (-5, "deposit"), (True, "deposit"), ("9", "deposit"),
                         (10, "income"), (10, "gift")):
        with pytest.raises(ServiceError):
            service.auto_fund(amount_usd=amount, kind=kind)


# ---------------------------------------------------------------- adjusting inside the envelope
def test_adjusting_the_strategy_is_a_new_version_inside_the_same_envelope(desk):
    service, _prices, runs = desk
    preview, started = start(service)
    write_research(runs, "VTI", confidence=88)
    service.auto_tick()
    first = started["opening_version"]
    changed = service.auto_adjust(strategy={"stop_loss_pct": 8.0, "max_holding_days": 60},
                                  reason="tighter stop", identity="owner")
    second = changed["opening_version"]
    assert changed["changed"] is True and second != first and changed["replaces"] == first
    assert changed["managing"] == [first] and changed["clamps"] == []
    assert (changed["strategy"]["stop_loss_pct"], changed["strategy"]["max_holding_days"]) == (8.0, 60)
    sim = service.auto_simulation()
    # The person's part has not moved: the same plan, the same envelope, the same settings.
    assert sim["plan"]["plan_id"] == preview["plan_id"] and sim["plan"]["envelope"] == preview["envelope"]
    assert sim["plan"]["adjusted_by"] == "owner"
    # A new version starts with nothing on its record, whatever the one before it had.
    assert sim["version"]["version_id"] == second and sim["version"]["parent"] == first
    assert sim["version"]["validation"]["simulated_days"] == 0
    # And what the first one bought is still the first one's to manage.
    assert sim["positions"][0]["strategy_version"] == first
    # Nobody edited a file for any of this.
    live = service.profiles()
    assert live.risk_cfg("buckets", MICRO, "autonomy", "managed_exit", "enabled", default=None) is None
    assert service.auto_adjust(strategy={"stop_loss_pct": 8.0})["changed"] is False, "the same strategy"


def test_an_adjustment_is_clamped_and_may_not_need_anything_the_plan_did_not_carry(desk):
    service, _prices, _runs = desk
    start(service)
    wide = service.auto_adjust(strategy={"stop_loss_pct": 60.0, "universe": ["VTI", "AAPL"]})
    assert wide["strategy"]["stop_loss_pct"] == 15.0 and wide["strategy"]["universe"] == ["VTI"]
    assert {note["field"] for note in wide["clamps"]} == {"stop_loss_pct", "universe"}
    # Surer than the configuration requires needs no setting changed; less sure is held to it.
    assert service.auto_adjust(strategy={"min_confidence": 90})["strategy"]["min_confidence"] == 90
    assert service.auto_adjust(strategy={"min_confidence": 10})["strategy"]["min_confidence"] == 80
    # Positions so small their half size falls under the minimum order would need that minimum
    # lowered, which is a setting, which is the person's: refused, not quietly done.
    before = service.auto_simulation()["plan"]["opening_version"]
    with pytest.raises(ServiceError, match=r"sizing\.min_order_usd.*new plan for you to confirm"):
        service.auto_adjust(strategy={"position_pct": 20.0})
    assert service.auto_simulation()["plan"]["opening_version"] == before
    with pytest.raises(ServiceError, match="say what to change"):
        service.auto_adjust(strategy={})
    with pytest.raises(ServiceError):
        service.auto_adjust(strategy={"max_positions": "many"})


def test_an_account_that_grew_does_not_turn_an_adjustment_into_a_new_plan(desk):
    """The allowance is a dollar cap and a share. A deposit shrinks the share the plan needs."""
    service, _prices, runs = desk
    start(service)
    service.auto_fund(amount_usd=100.0, kind="deposit")              # $200 in the account now
    changed = service.auto_adjust(strategy={"stop_loss_pct": 8.0})
    assert changed["changed"] is True
    # And the deposit widened nothing: a position is still a third of the $100 that was confirmed.
    write_research(runs, "VTI", confidence=88)
    service.auto_tick()
    sim = service._auto_paper()
    bought = sim._store(sim.profiles()).all()[0]
    assert bought.notional_usd == pytest.approx(33.0, abs=0.01)
    assert load_portfolio(paper.ledger_path(sim.profiles())).buckets[MICRO].cash_usd == pytest.approx(167.0, abs=0.01)


def test_a_model_that_cannot_be_asked_adjusts_nothing(desk, monkeypatch):
    from agentic_trading.auto import advice
    from agentic_trading.llm.claude_code import ClaudeCodeError

    service, _prices, _runs = desk
    _preview, started = start(service)

    def expired(*_args, **_kwargs):
        raise ClaudeCodeError("OAuth session expired", login_expired=True)

    monkeypatch.setattr(advice, "suggest", expired)
    with pytest.raises(ServiceError, match="login on this Mac has expired"):
        service.auto_adjust(ask_model=True)
    assert service.auto_simulation()["plan"]["opening_version"] == started["opening_version"]


# ---------------------------------------------------------------- where the research comes from
def test_a_plan_whose_funds_nobody_researches_says_so(desk):
    service, _prices, _runs = desk
    _preview, started = start(service)
    coverage = started["simulation"]["research"]
    universe = started["simulation"]["plan"]["strategy"]["universe"]
    # Research studies what the owner holds and what the owner added; in the copy this starts
    # from, the candidate study that would reach these funds is switched off.
    assert coverage["universe"] == universe and coverage["candidates_enabled"] is False
    assert coverage["missing"] == universe and coverage["covered"] == []
    assert "Robinhood 自选" in coverage["text"]["zh"] and "打开“候选 ETF”研究" in coverage["text"]["zh"]
    assert "switch on the candidate-ETF study" in coverage["text"]["en"]
    # Switched on in the real configuration, the study reaches the first of them.
    risk = service.profiles().config_dir / "risk_profile.yaml"
    risk.write_text(risk.read_text().replace(
        "candidates: {period: weekly, max_tickers_per_run: 3, enabled: false}",
        "candidates: {period: weekly, max_tickers_per_run: 3, enabled: true}"))
    reached = service.auto_simulation()["research"]
    assert reached["candidates_enabled"] is True and reached["covered"]
    assert set(reached["covered"]) | set(reached["missing"]) == set(universe)
    # The study takes one fund from each group, so some of the plan's funds are still not
    # studied. What is said then names what the study does cover and no longer tells the owner
    # to switch on something that is on: the watchlist is the one way left.
    assert reached["missing"], "the plan may buy more funds than the study has groups"
    for lang, already, again in (("zh", "已经打开", "打开“候选 ETF”研究（"),
                                 ("en", "is on and covers", "switch on the candidate-ETF study")):
        said = reached["text"][lang]
        assert already in said and again not in said, said
        assert all(symbol in said for symbol in reached["missing"]), said
        assert any(symbol in said for symbol in reached["covered"]), said
    assert "Robinhood 自选" in reached["text"]["zh"] and "watchlist" in reached["text"]["en"]


def test_what_is_called_studied_is_what_the_schedule_would_study(desk):
    """Cut by both caps and less what is held, because that is how the scheduler cuts it.

    The notice once read the candidate task's cap in place of the schedule's, so with the task's
    the larger it named funds no run would ever reach. It now asks the scheduler.
    """
    from agentic_trading import schedule

    service, _prices, _runs = desk
    _preview, started = start(service)
    universe = started["simulation"]["plan"]["strategy"]["universe"]
    root = service.profiles().project_dir
    risk = root / "config" / "risk_profile.yaml"
    risk.write_text(risk.read_text().replace(
        "candidates: {period: weekly, max_tickers_per_run: 3, enabled: false}",
        "candidates: {period: weekly, max_tickers_per_run: 5, enabled: true}"))

    def would_study():
        profiles = service.profiles()
        cfg = schedule.settings(profiles)
        task = next(t for t in schedule.tasks(cfg, bucket="micro_live") if t.kind == schedule.CANDIDATES)
        assert task.max_tickers == 5 > int(cfg["max_tickers_per_run"]), "the task's cap is the larger"
        return schedule.subjects_for(profiles, task)[:task.max_tickers], int(cfg["max_tickers_per_run"])

    studied, schedule_cap = would_study()
    assert len(studied) == schedule_cap, "the schedule's cap still binds"
    reached = service.auto_simulation()["research"]
    assert reached["covered"] == [s for s in universe if s in studied]
    assert reached["missing"] == [s for s in universe if s not in studied] and reached["missing"]
    assert all(symbol not in reached["text"]["zh"].split("不包括")[0] for symbol in reached["missing"])

    # A fund the account holds is set aside by the study and examined by the holdings review:
    # the next fund takes its place in the study, and the held one is not called unstudied.
    held = studied[0]
    ledger = root / "state" / "micro_live_portfolio.yaml"
    ledger.write_text(ledger.read_text().replace(
        "positions: []", f"positions:\n  - symbol: {held}\n    bucket: micro_live\n"
                         "    quantity: 0.1\n    cost_basis_per_share: 100.0"))
    after, _cap = would_study()
    assert held not in after and len(after) == schedule_cap
    reached = service.auto_simulation()["research"]
    assert held in reached["covered"] and held not in reached["missing"]
    assert reached["covered"] == [s for s in universe if s in after or s == held]
    assert reached["candidates_enabled"] is True


# ---------------------------------------------------------------- the other doors
def test_the_simulated_account_over_http(auth_client, desk, monkeypatch):
    service, _prices, runs = desk
    monkeypatch.setattr("agentic_trading.api.app._service", service)
    assert auth_client.get("/auto-trading/simulation").json()["exists"] is False
    preview = auth_client.post("/auto-trading/plan", json={}).json()
    wrong = auth_client.post("/auto-trading/start", json={"plan_id": "f" * 16})
    assert wrong.status_code == 409 and "previewed" in wrong.json()["detail"]
    live = auth_client.post("/auto-trading/start", json={"plan_id": preview["plan_id"], "scope": "live"})
    assert live.status_code == 400 and "not open on the real account" in live.json()["detail"]
    started = auth_client.post("/auto-trading/start", json={
        "plan_id": preview["plan_id"], "strategy": preview["strategy"]})
    assert started.status_code == 200, started.text
    assert started.json()["simulation"]["plan"]["confirmed_by"] == "test-owner"

    write_research(runs, "VTI", confidence=88)
    service.auto_tick()
    body = auth_client.get("/auto-trading/simulation").json()
    assert body["positions"][0]["symbol"] == "VTI" and body["account"]["funded_usd"] == 100.0
    assert auth_client.get("/auto-trading").json()["simulation"]["exists"] is True

    assert auth_client.post("/auto-trading/pause", json={"reason": "away"}).json()["paused"] is True
    assert auth_client.post("/auto-trading/resume", json={"reason": "back"}).json()["changed"] is True
    funded = auth_client.post("/auto-trading/fund", json={"kind": "deposit", "amount_usd": 25})
    assert funded.status_code == 200 and funded.json()["cash_usd"] == pytest.approx(92.0, abs=0.01)
    adjusted = auth_client.post("/auto-trading/adjust", json={"stop_loss_pct": 8})
    assert adjusted.status_code == 200 and adjusted.json()["changed"] is True
    assert auth_client.post("/auto-trading/stop", json={"reason": "done"}).json()["stopped"] is True
    # Unknown fields, and figures no envelope could carry, never reach the service.
    # ``confirmed`` above all: whether the password matched is the API's to say, never a caller's.
    for path, body in (("/auto-trading/start", {"plan_id": preview["plan_id"], "confirmed": True}),
                       ("/auto-trading/resume", {"reason": "x", "scope": "live", "confirmed": True}),
                       ("/auto-trading/pause", {"reason": "x", "scope": "everything"}),
                       ("/auto-trading/fund", {"kind": "income", "amount_usd": 5}),
                       ("/auto-trading/fund", {"kind": "deposit", "amount_usd": -5}),
                       ("/auto-trading/adjust", {"leverage": 2}),
                       ("/auto-trading/pause", {"reason": "x" * 501})):
        assert auth_client.post(path, json=body).status_code == 422, (path, body)


def test_a_signed_out_caller_reaches_none_of_it(desk, monkeypatch, console_auth):
    from fastapi.testclient import TestClient

    from agentic_trading.api.app import app

    service, _prices, _runs = desk
    monkeypatch.setattr("agentic_trading.api.app._service", service)
    headers = {"Origin": "https://testserver", "X-CT-Request": "1"}
    client = TestClient(app, base_url="https://testserver")
    assert client.get("/auto-trading/simulation").status_code == 401
    for path in ("start", "pause", "resume", "stop", "fund", "adjust"):
        assert client.post(f"/auto-trading/{path}", json={}, headers=headers).status_code == 401
    assert not paper.root_of(service.profiles()).exists()


def test_the_command_line_is_the_same_service(desk, capsys):
    service, _prices, runs = desk
    config = str(service.profiles().config_dir)

    def run(*args):
        code = cli.main(["--config-dir", config, *args])
        return code, capsys.readouterr().out

    code, out = run("sim")
    assert code == 0 and "模拟账户还没有启动" in out
    assert run("start", "--plan-id", "0" * 16)[0] == 2
    preview = service.auto_plan()
    code, out = run("start", "--plan-id", preview["plan_id"])
    assert code == 0 and preview["plan_id"] in out and "有效模拟交易日：0 / 30（等待样本）" in out
    assert "每周研究目前不会研究" in out
    write_research(runs, "VTI", confidence=88)
    code, out = run("tick")
    assert code == 0 and '"ran": true' in out
    code, out = run("status")
    assert code == 0 and "【自动交易状态】" in out and "持仓 VTI" in out and "未开放" in out
    code, out = run("--lang", "en", "sim")
    assert "[Simulated account]" in out and "Sessions counted: 0 of 30 needed" in out
    assert run("pause", "--reason", "away")[0] == 0
    code, out = run("sim")
    assert "已暂停新开仓：away" in out
    assert run("resume", "--note", "back")[0] == 0
    assert run("fund", "deposit", "20")[0] == 0
    assert run("adjust", "--stop-loss-pct", "8")[0] == 0
    code, out = run("adjust", "--position-pct", "20")
    assert code == 2 and "new plan for you to confirm" in out
    assert run("stop", "--reason", "done")[0] == 0
    assert "没有生效的方案" in run("sim")[1]


def test_the_modes_table_is_what_the_plan_preview_shows_too(desk):
    service, _prices, _runs = desk
    start(service)
    preview = service.auto_plan()
    assert preview["modes"] == service.auto_status()["modes"]
    assert [mode["key"] for mode in preview["modes"]] == list(modes.MODES)
