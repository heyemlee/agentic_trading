"""Real money, and everything that stands in front of it (docs/todo.md, P3.2 and P3.5).

The account here is the real one as the code sees it: not the simulated account's marked copy of
the configuration but ``config/`` itself, with real-money execution on, in a temporary directory.
Its venue is the simulator answering to the real venue's name (the same double the exit tests
use), so every order below goes down the path a real one would -- approval, revalidation,
reservation, submission, reconciliation -- and none of it leaves the process.

What is held:

* nothing can be started on the real account until every condition holds, and the conditions
  are about the exact strategy version that would run -- not a neighbour of it;
* a number of days is reported as a number of days, in words that say it proves nothing about
  profit, and no session is ever made to count by trading;
* confirming a plan takes the console password and then *waits* for the plan's settings, which
  go through the console's own cooling period and can be taken back;
* the plan goes into force only when nothing is waiting and every condition holds at that
  moment, and stops opening the moment the code it was validated on is no longer the code;
* once in force it trades through the one approval path, and can always be stopped without a
  password.
"""

import re
import shutil
from datetime import UTC, datetime, timedelta

import pytest

from agentic_trading import autonomy, cli, events, ledger_confirm, portfolio, risk_scenarios
from agentic_trading import config_changes as cc
from agentic_trading.auto import gates as gating
from agentic_trading.auto import modes, paper, positions, runner, store, trader
from agentic_trading.broker import SimulatedBroker
from agentic_trading.ledger_confirm import confirm_ledgers
from agentic_trading.portfolio import load_portfolio
from agentic_trading.proposals import EXECUTED, PROPOSED, utcnow
from agentic_trading.service import Service, ServiceConflict, ServiceError
from agentic_trading.service.approvals import FIRST_REAL_ORDER_WAITS
from tests.auto_market import scripted, today, write_research
from tests.conftest import ROOT, copy_example_ledger, research_as_written

MICRO = "micro_live"
PASSWORD = "Test-Only-Console-Pw1"


class VenueLike(SimulatedBroker):
    """A simulated venue that answers to the real-money binding (see tests/test_exit_result.py)."""

    name = "robinhood"


def _real_money(risk_profile, on):
    """Set the account's real-money switch in a copied configuration, whatever the machine's is."""
    text, count = re.subn(r"(micro_live_execution:\n(?:.*\n)*?  enabled: )(?:true|false)",
                          rf"\1{'true' if on else 'false'}", risk_profile.read_text(), count=1)
    assert count == 1, "the configuration carries no micro-live switch"
    risk_profile.write_text(text)


def _busier(risk_profile, per_day):
    """Let the account trade more often than it really may, in a copy of the configuration.

    For the tests that have to put months of a plan's life into one day -- a strategy validated,
    then another -- and would otherwise be stopped by the account opening one position a day.
    """
    from tests.test_auto_lifecycle import quicken

    quicken(risk_profile, per_day)
    weekly = "max_orders_per_week:        {value: TODO, recommended_default: 4}"
    text = risk_profile.read_text(encoding="utf-8")
    assert text.count(weekly) == 1, "the configuration no longer reads as this helper expects"
    risk_profile.write_text(text.replace(weekly, f"max_orders_per_week:        {per_day * 4}"),
                            encoding="utf-8")


class Real:
    """The real account in a temporary directory, with the simulated one beside it."""

    def __init__(self, tmp_path, monkeypatch, *, real_money=True, openings_per_day=None,
                 configure=None):
        self.monkeypatch = monkeypatch
        self.prices = dict(risk_scenarios.PRICES)
        root = tmp_path / "account"
        (root / "state").mkdir(parents=True)
        (root / "runs").mkdir()
        shutil.copytree(ROOT / "config", root / "config")
        # Which research the owner has switched on today is not this account's to inherit.
        research_as_written(root / "config" / "risk_profile.yaml")
        _real_money(root / "config" / "risk_profile.yaml", real_money)
        if openings_per_day:
            _busier(root / "config" / "risk_profile.yaml", openings_per_day)
        if configure is not None:
            # A decision of the owner's that a test needs made, made in the copy and nowhere else.
            configure(root / "config" / "risk_profile.yaml")
        copy_example_ledger("portfolio.example.yaml", root / "state" / "portfolio.yaml")
        copy_example_ledger("micro_live_portfolio.example.yaml",
                            root / "state" / "micro_live_portfolio.yaml", as_of=today())
        scripted(monkeypatch, self.prices)
        monkeypatch.setattr(cli, "upstream_identity_lookup", risk_scenarios.identity_lookup)
        # The daily check of the book against the venue asks the data vendor what each holding
        # is. Here it is told, by the same table every other read in these tests uses.
        checked = portfolio.confirm_from_broker
        monkeypatch.setattr(ledger_confirm, "confirm_from_broker", lambda *a, **k: checked(
            *a, **{**k, "identity_lookup": risk_scenarios.identity_lookup}))
        self.root, self.runs = root, root / "runs"
        self.service = Service(root / "config")
        # The account as its venue has it: the ledger's $100, in the increment the venue trades.
        self.venue = VenueLike(prices=self.prices, cash=100.0, quantity_increment=0.000001)
        monkeypatch.setattr(self.service, "_broker", lambda *_a, **_k: self.venue)
        # The attention pass asks whether the venue session works; here there is no session.
        monkeypatch.setattr(self.service, "_broker_connect_error", lambda: None)
        self.count = 0

    # -- reading
    @property
    def db(self):
        return self.service.state_db()

    def config(self):
        return (self.root / "config" / "risk_profile.yaml").read_text(encoding="utf-8")

    def ledger(self):
        return load_portfolio(self.root / "state" / "micro_live_portfolio.yaml")

    def held(self, symbol="VTI"):
        position = self.ledger().position(symbol, MICRO)
        return float(position.quantity) if position else 0.0

    def proposals(self):
        return self.service._store(self.service.profiles()).all()

    def sim(self):
        return self.service._auto_paper()

    def candidate(self):
        return self.service.auto_live()["candidate"]

    def gates(self):
        return {row["code"]: row for row in self.candidate()["gates"]}

    # -- the market
    def move(self, symbol, factor):
        self.prices[symbol] = round(self.prices[symbol] * factor, 4)
        self.venue.set_price(symbol, self.prices[symbol])

    def research(self, ticker="VTI", **research):
        self.count += 1
        return write_research(self.runs, ticker, serial=self.count, **research)

    def forget_research(self):
        for run in self.runs.glob("*_*_*"):
            if (run / "decision_record.json").exists():
                shutil.rmtree(run)

    # -- the simulated account's record
    def simulate(self, sessions=modes.SIMULATION_DAYS_TO_OPEN, **envelope):
        preview = self.service.auto_plan(**envelope)
        started = self.service.auto_start(plan_id=preview["plan_id"], identity="owner", **envelope)
        self.record(started["opening_version"], sessions)
        return started["opening_version"]

    def record(self, version, sessions):
        """Sessions as the runner records them: two open-market passes ninety minutes apart, and
        nothing wrong on either. Written through the function the runner writes them with."""
        db = self.sim().state_db()
        day = datetime.now(UTC).replace(hour=15, minute=0, second=0, microsecond=0) - timedelta(days=1)
        done = 0
        while done < sessions:
            if day.weekday() < 5:
                for at in (day, day + timedelta(minutes=90)):
                    store.mark_pass(db, version, day.date().isoformat(), at=at, market_open=True,
                                    problems=[])
                done += 1
            day -= timedelta(days=1)

    def exercise_exit(self):
        """Carry one position from code's purchase to code's sale in the simulated account."""
        self.research("VTI")
        self.service.auto_tick()
        self.move("VTI", 0.88)
        self.service.auto_tick()
        self.move("VTI", 1 / 0.88)
        self.forget_research()
        sim = self.sim()
        version = store.active_plan(sim.state_db())["opening_version"]
        assert runner.exits_verified(sim, sim.profiles(), version), "the simulated exit did not happen"

    def by_hand(self, proposal):
        """The owner approves a waiting proposal in the console, through the ordinary path."""
        answer = self.service.approve(
            proposal.proposal_id, identity="owner", version=proposal.version(),
            request_id=f"owner-{proposal.proposal_id}", authority_version=proposal.authority_version,
            terms={"ticker": proposal.ticker, "bucket": proposal.bucket, "action": proposal.action,
                   "notional_usd": proposal.notional_usd, "sell_quantity": proposal.sell_quantity})
        assert answer["submitted"] is True, answer
        return next(p for p in self.proposals() if p.proposal_id == proposal.proposal_id)

    def first_purchase(self, symbol, **research):
        """The plan's first real-money purchase (contract 2.5): written by the pass, left by code
        for the owner, and approved by them."""
        self.research(symbol, **research)
        self.service.auto_tick()
        report = self.service.autonomous_reductions()
        waiting = next(p for p in self.proposals() if p.ticker == symbol and p.status == PROPOSED
                       and p.action in ("BUY", "ADD"))
        assert {"proposal_id": waiting.proposal_id, "reasons": [FIRST_REAL_ORDER_WAITS]} in report["declined"], report
        return self.by_hand(waiting)

    def confirm_book(self):
        """The daily check of the ledger against the venue, run now."""
        profiles = self.service.profiles()
        return confirm_ledgers(profiles, self.service._store(profiles), ledger=MICRO,
                               trade_date=today(), broker_factory=lambda _p: self.venue)

    def ready(self, **envelope):
        """Every condition met except the plan's own settings: a record, an exit, an agreed book."""
        version = self.simulate(**envelope)
        self.exercise_exit()
        assert self.confirm_book()["confirmed"] is True
        return version

    # -- starting
    def start(self, *, confirmed=True):
        return self.service.auto_start(scope="live", identity="owner", confirmed=confirmed,
                                       **self.candidate()["inputs"])

    def cool(self):
        """A day passes: what was cooling comes due, and the loop that applies it runs."""
        items = cc.records(self.db)
        for item in items:
            if item["status"] == cc.PENDING:
                item["apply_after"] = (utcnow() - timedelta(minutes=1)).isoformat()
        cc.save(self.db, items)
        return self.service.apply_config_changes()

    def in_force(self, **envelope):
        """A plan put in force the way a person would: confirmed, cooled, and then a pass."""
        self.ready(**envelope)
        self.start()
        self.cool()
        tick = self.service.auto_tick()
        assert tick["live"]["activation"]["request"] == "activated", tick["live"]
        return store.active_plan(self.db)


@pytest.fixture
def real(tmp_path, monkeypatch, offline_market_reads):
    return Real(tmp_path, monkeypatch)


# ---------------------------------------------------------------- the conditions, as lines
FACTS = {"real_money_on": True, "halted": None, "autonomy_withdrawn": None, "unresolved_orders": [],
         "book": {"confirmed": True, "as_of": "2026-10-05", "error": None}, "unexplained": [],
         "any_order": True, "why_no_order": None}
RECORD = {"exists": True, "counted": 30, "exits_verified": True, "simulated_version": "sv-a",
          "simulated_version_days": 30}


def _evaluate(profiles, **over):
    return gating.evaluate(profiles, mode=over.pop("mode", modes.STEADY), version_id="sv-a",
                           record={**RECORD, **over.pop("record", {})},
                           account={**FACTS, **over.pop("account", {})}, **over)


def test_everything_in_order_is_open_and_any_one_thing_out_of_order_is_not(profiles):
    assert gating.is_open(_evaluate(profiles)) is True
    for name, wrong, code in (
            ("record", {"counted": 29}, "simulation_record"),
            ("record", {"exits_verified": False}, "exit_exercised"),
            ("record", {"exists": False, "counted": 0}, "simulation_record"),
            ("account", {"real_money_on": False}, "real_money_on"),
            ("account", {"halted": "kill switch engaged"}, "not_halted"),
            ("account", {"autonomy_withdrawn": "an order could not be accounted for"}, "autonomy_intact"),
            ("account", {"unresolved_orders": ["p-1"]}, "orders_settled"),
            ("account", {"book": {"confirmed": False, "as_of": None, "error": "cash differs"}}, "book_confirmed"),
            ("account", {"unexplained": ["VTI"]}, "positions_explained"),
            ("account", {"any_order": False, "why_no_order": {"zh": "太小", "en": "too small"}}, "order_possible")):
        found = _evaluate(profiles, **{name: wrong})
        failing = [gate.code for gate in gating.failing(found)]
        assert failing == [code], (wrong, failing)
        assert gating.is_open(found) is False and gating.may_confirm(found) is False
        line = next(gate for gate in found if gate.code == code)
        assert line.zh and line.en, "every refusal says why, in both languages"


def test_settings_still_to_land_are_the_one_thing_confirming_may_start(profiles):
    found = _evaluate(profiles, settings_waiting=("buckets.micro_live.autonomy.open.enabled",))
    assert gating.may_confirm(found) is True and gating.is_open(found) is False
    line = next(gate for gate in found if gate.code == gating.SETTINGS_PENDING)
    assert "24" in line.zh and "控制台密码" in line.zh and "withdrawn" in line.en


def test_thirty_days_is_said_to_be_thirty_days_and_not_proof_of_profit(profiles):
    line = _evaluate(profiles)[0]
    assert line.code == "simulation_record" and line.ok is True
    assert "30" in line.zh and "不代表策略已被证明能盈利" in line.zh
    assert "not evidence that the strategy makes money" in line.en
    # And a day short is a day short, with the reason nothing is traded to make it up.
    short = _evaluate(profiles, record={"counted": 29})[0]
    assert short.ok is False and "等待样本" in short.zh and "不会为了凑天数而交易" in short.zh
    waiting = _evaluate(profiles, record={"exits_verified": False})[1]
    assert waiting.code == "exit_exercised" and "不会为了验证而强行交易" in waiting.zh


def test_another_versions_days_are_named_as_another_versions(profiles):
    line = _evaluate(profiles, record={"counted": 0, "simulated_version": "sv-b",
                                       "simulated_version_days": 45})[0]
    assert line.ok is False
    assert "sv-b" in line.zh and "45" in line.zh and "不能算在它名下" in line.zh
    assert "another version's record is not its record" in line.en


def test_modes_open_in_order_and_what_the_owner_has_not_decided_stays_shut(profiles):
    swing = {gate.code: gate for gate in _evaluate(profiles, mode=modes.SWING)}
    assert swing["earlier_mode_opened"].ok is False and "稳健" in swing["earlier_mode_opened"].zh
    assert swing["swing_policy_undecided"].ok is False
    # Steady having been started there is what lets swing be considered at all.
    after = {gate.code: gate for gate in _evaluate(profiles, mode=modes.SWING, opened=(modes.STEADY,))}
    assert "earlier_mode_opened" not in after and after["swing_policy_undecided"].ok is False
    intraday = [gate.code for gate in gating.failing(_evaluate(profiles, mode=modes.INTRADAY,
                                                               opened=(modes.STEADY, modes.SWING)))]
    assert "intraday_not_built" in intraday and "strategy_always_needs_a_person" in intraday


def test_no_argument_to_the_gates_skips_one():
    import inspect

    accepted = set(inspect.signature(gating.evaluate).parameters)
    assert accepted == {"profiles", "mode", "version_id", "record", "account", "opened",
                        "settings_waiting", "cooling_hours"}
    assert not {"force", "skip", "override", "ignore"} & set(inspect.signature(Service.auto_start).parameters)


# ---------------------------------------------------------------- before anything can be started
def test_nothing_is_offered_until_a_simulation_is_running(real):
    live = real.service.auto_live()
    assert live["plan"] is None and live["request"] is None and live["positions"] == []
    assert live["candidate"]["available"] is False and "先启动模拟" in live["candidate"]["why"]["zh"]
    assert real.service.auto_tick() == {"ran": False, "skipped": "the simulated account has not been started"}
    preview = real.service.auto_plan()
    with pytest.raises(ServiceError, match="not open on the real account"):
        real.service.auto_start(plan_id=preview["plan_id"], scope="live", identity="owner",
                                confirmed=True, mode="steady", strategy=preview["strategy"])


def test_a_record_that_is_short_refuses_the_start_and_writes_nothing(real):
    before = real.config()
    real.simulate(sessions=29)
    gates = real.gates()
    assert gates["simulation_record"]["ok"] is False and "29" in gates["simulation_record"]["text"]["zh"]
    assert gates["exit_exercised"]["ok"] is False and gates["book_confirmed"]["ok"] is False
    assert gates["real_money_on"]["ok"] is True and gates["orders_settled"]["ok"] is True
    assert real.candidate()["may_confirm"] is False and real.candidate()["open"] is False
    with pytest.raises(ServiceError) as refused:
        real.start()
    said = str(refused.value)
    assert "not open on the real account" in said and "29 valid simulated session" in said
    assert "has not yet carried one position" in said and "has not been confirmed against the venue" in said
    # Refused before the password was looked at, and with nothing left behind.
    assert store.start_request(real.db) is None and store.active_plan(real.db) is None
    assert cc.records(real.db) == [] and real.config() == before

    # The thirtieth session, the exit and the check arrive; only the plan's settings remain.
    version = store.active_plan(real.sim().state_db())["opening_version"]
    real.record(version, 30)
    real.exercise_exit()
    real.confirm_book()
    gates = real.gates()
    assert [code for code, row in gates.items() if not row["ok"]] == [gating.SETTINGS_PENDING]
    assert real.candidate()["may_confirm"] is True and real.candidate()["open"] is False
    assert real.candidate()["version_id"] == version


def test_days_are_not_carried_to_a_strategy_that_was_changed(real):
    first = real.ready()
    assert real.gates()["simulation_record"]["ok"] is True
    adjusted = real.service.auto_adjust(strategy={"stop_loss_pct": 8.0}, reason="a tighter stop")
    second = adjusted["opening_version"]
    assert second != first
    gates = real.gates()
    assert real.candidate()["version_id"] == second
    assert gates["simulation_record"]["ok"] is False and gates["exit_exercised"]["ok"] is False
    assert "0 valid simulated session" in gates["simulation_record"]["text"]["en"]
    with pytest.raises(ServiceError, match="not open on the real account"):
        real.start()
    # The first version's thirty days are still its own, on its own record.
    assert store.simulated_days(real.sim().state_db(), first) == 30


def test_real_money_switched_off_is_a_condition_nothing_here_changes(tmp_path, monkeypatch,
                                                                    offline_market_reads):
    real = Real(tmp_path, monkeypatch, real_money=False)
    real.ready()
    gates = real.gates()
    assert gates["real_money_on"]["ok"] is False and "不会替你打开" in gates["real_money_on"]["text"]["zh"]
    with pytest.raises(ServiceError, match="switched off"):
        real.start()
    assert "enabled: false" in re.search(r"micro_live_execution:\n(?:.*\n)*?  enabled: \w+", real.config())[0]


def test_the_account_has_to_be_in_order(real):
    real.ready()
    autonomy.trip(real.service.profiles(), "an earlier order could not be accounted for")
    assert real.gates()["autonomy_intact"]["ok"] is False
    with pytest.raises(ServiceError, match="withdrawn"):
        real.start()
    autonomy.reset(real.service.profiles(), "looked")
    assert real.gates()["autonomy_intact"]["ok"] is True
    # A book the venue no longer agrees with: somebody bought something in the venue's own app.
    real.venue.set_cash(60.0)
    assert real.confirm_book()["confirmed"] is False
    gate = real.gates()["book_confirmed"]
    assert gate["ok"] is False and "CashMismatch" in gate["text"]["en"]
    with pytest.raises(ServiceError, match="has not been confirmed against the venue"):
        real.start()


# ---------------------------------------------------------------- confirming, and waiting
def test_confirming_takes_the_password_and_then_waits_for_the_settings(real):
    real.ready()
    before = real.config()
    candidate = real.candidate()
    assert candidate["plan"]["landing"]["needs_password"] is True
    with pytest.raises(ServiceError, match="needs the console password"):
        real.start(confirmed=False)
    assert store.start_request(real.db) is None and cc.records(real.db) == []

    answer = real.start()
    assert answer["started"] is False and answer["opening_version"] is None
    waiting = answer["waiting"]
    assert waiting["status"] == "waiting" and waiting["requested_by"] == "owner"
    assert waiting["version_id"] == candidate["version_id"]
    assert set(waiting["waiting_for"]) == {change["path"] for change in candidate["plan"]["changes"]}
    # Nothing is in force and nothing has been written to the configuration.
    assert store.active_plan(real.db) is None and real.config() == before
    # What was submitted is one ordinary console change, cooling, where Settings shows it.
    change = cc.records(real.db)[0]
    assert len(cc.records(real.db)) == 1 and change["status"] == cc.PENDING
    assert change["classification"] == cc.COOLING and change["requested_by"] == "owner"
    assert candidate["plan"]["plan_id"] in change["reason"]
    assert set(change["set"]) == set(waiting["waiting_for"])
    assert "buckets.micro_live.autonomy.managed_exit.enabled" in change["create"]
    assert [row["id"] for row in real.service.config_overview()["pending"]] == [change["id"]]
    entry = events.query(real.db, kinds=("auto_plan_requested",))["events"][0]
    assert entry["actor"] == "owner" and entry["detail"]["changes"] == [change["id"]]

    # While it waits, a pass looks, finds the settings not in force, and starts nothing.
    real.research("VXUS", confidence=90)
    tick = real.service.auto_tick()
    assert tick["live"]["activation"]["request"] == "waiting"
    assert "not in force" in tick["live"]["activation"]["reason"]
    assert store.active_plan(real.db) is None and real.proposals() == []
    assert real.service.auto_live()["request"]["last_reason"]


def test_the_plan_goes_into_force_when_its_settings_land_and_everything_still_holds(real):
    real.ready()
    candidate = real.candidate()
    real.start()
    applied = real.cool()
    assert [row["status"] for row in applied] == [cc.APPLIED]
    profiles = real.service.profiles()
    assert profiles.risk_cfg("buckets", MICRO, "autonomy", "open", "enabled") is True
    assert profiles.risk_cfg("buckets", MICRO, "autonomy", "managed_exit", "enabled") is True
    assert store.active_plan(real.db) is None, "a setting landing is not the plan starting"

    tick = real.service.auto_tick()
    assert tick["live"]["activation"]["request"] == "activated"
    active = store.active_plan(real.db)
    assert active["scope"] == "live" and active["confirmed_by"] == "owner"
    assert active["plan_id"] == candidate["plan"]["plan_id"]
    assert active["opening_version"] == candidate["version_id"]
    assert store.start_request(real.db) is None
    assert store.request_history(real.db)[-1]["status"] == "activated"
    started = events.query(real.db, kinds=("auto_plan_started",))["events"][0]
    assert started["detail"]["scope"] == "live" and started["detail"]["how"] == "settings_in_force"

    status = real.service.auto_status()
    assert status["real_account"]["open"] is True and status["real_account"]["opened_modes"] == ["steady"]
    assert [row["key"] for row in status["modes"] if row["open_for_real_money"]] == ["steady"]
    live = real.service.auto_live()
    assert live["plan"]["opening_version"] == active["opening_version"] and live["request"] is None
    assert live["last_pass"]["error"] is None
    # With nothing left to change, the same plan confirmed again would simply be in force.
    assert [row["code"] for row in live["candidate"]["gates"] if not row["ok"]] == []


def test_a_start_does_not_outlive_a_condition_that_stopped_holding(real):
    real.ready()
    real.start()
    real.cool()
    # The day the settings land, an order somewhere could not be accounted for.
    autonomy.trip(real.service.profiles(), "an order could not be accounted for")
    tick = real.service.auto_tick()
    assert tick["live"]["activation"]["request"] == "waiting"
    assert "withdrawn" in tick["live"]["activation"]["reason"]
    assert store.active_plan(real.db) is None
    # Nobody restores it in time: the confirmation runs out, and a person is asked again.
    pending = store.start_request(real.db)
    store.write_request(real.db, {**pending, "expires_at": (utcnow() - timedelta(minutes=1)).isoformat()})
    lapsed = real.service.auto_tick()["live"]["activation"]
    assert lapsed["request"] == "lapsed" and "ran out" in lapsed["reason"]
    assert store.start_request(real.db) is None and store.active_plan(real.db) is None
    ended = events.query(real.db, kinds=("auto_plan_request_ended",))["events"][0]
    assert ended["outcome"] == "lapsed"
    # Restoring autonomy afterwards starts nothing: the confirmation is gone, and with it
    # everything of the real account's that a pass would look at.
    autonomy.reset(real.service.profiles(), "looked")
    assert "live" not in real.service.auto_tick()
    assert store.active_plan(real.db) is None and store.versions(real.db) == []


def test_a_waiting_start_can_be_taken_back_with_the_settings_it_submitted(real):
    real.ready()
    before = real.config()
    real.start()
    change = cc.records(real.db)[0]
    answer = real.service.auto_withdraw_request(reason="changed my mind", identity="owner")
    assert answer == {"withdrawn": True, "plan_id": answer["plan_id"],
                      "cancelled_changes": [change["id"]], "left_in_force": []}
    assert cc.records(real.db)[0]["status"] == cc.CANCELLED
    assert store.start_request(real.db) is None
    assert real.cool() == [] and real.config() == before
    assert real.service.auto_tick().get("live") is None, "nothing is left of it to look at"
    assert real.service.auto_withdraw_request() == {"withdrawn": False, "reason": "no start is waiting"}


def test_a_setting_withdrawn_in_settings_ends_the_start_it_was_for(real):
    real.ready()
    real.start()
    real.service.cancel_config_change(cc.records(real.db)[0]["id"], identity="owner")
    ended = real.service.auto_tick()["live"]["activation"]
    assert ended["request"] == "lapsed" and "withdrawn or could not be applied" in ended["reason"]
    assert store.active_plan(real.db) is None and store.start_request(real.db) is None


def test_stopping_takes_a_waiting_start_with_it(real):
    real.ready()
    real.start()
    stopped = real.service.auto_stop(scope="live", reason="not now", identity="owner")
    assert stopped["stopped"] is True and stopped["request_withdrawn"]["withdrawn"] is True
    assert store.start_request(real.db) is None and cc.records(real.db)[0]["status"] == cc.CANCELLED


def test_what_is_confirmed_is_what_was_shown_or_nothing_is(real):
    real.ready()
    inputs = real.candidate()["inputs"]
    with pytest.raises(ServiceConflict, match="no longer the one that was previewed"):
        real.service.auto_start(scope="live", identity="owner", confirmed=True,
                                **{**inputs, "capital_usd": 80.0})
    with pytest.raises(ServiceConflict):
        real.service.auto_start(scope="live", identity="owner", confirmed=True,
                                **{**inputs, "strategy": {**inputs["strategy"], "stop_loss_pct": 14.0}})
    assert store.start_request(real.db) is None and cc.records(real.db) == []


# ---------------------------------------------------------------- in force
@pytest.fixture
def busier(tmp_path, monkeypatch, offline_market_reads):
    """The real account allowed three new positions a day, for the tests that need a second."""
    return Real(tmp_path, monkeypatch, openings_per_day=3)


def test_the_real_account_trades_down_the_one_approval_path(busier):
    real = busier
    active = real.in_force()
    profiles = real.service.profiles()
    # The research run now steps aside for the plan (tests/test_auto_path.py).
    assert cli._deferred_to_plan(real.db, MICRO, "VXUS", "BUY")["plan_id"] == active["plan_id"]

    # The plan's first real purchase is the owner's to send (contract 2.5) -- and once they
    # have, it is the plan's like any other, held to its stop (2.3).
    first = real.first_purchase("BND", confidence=90)
    assert first.status == EXECUTED and (first.approved_via, first.approved_by) == ("console", "owner")
    assert first.strategy_version == active["opening_version"]

    real.research("VXUS", confidence=90)
    tick = real.service.auto_tick()
    assert tick["live"]["pass"]["signals"][0]["diagnosis"]["code"] == "proposal_automatic"
    # The pass wrote a proposal and approved nothing: that is the approval loop's, and only its.
    assert tick["live"]["pass"]["approvals"] is None
    waiting = next(p for p in real.proposals() if p.ticker == "VXUS")
    assert waiting.status == PROPOSED and waiting.strategy_version == active["opening_version"]
    assert "VXUS" not in {o.symbol for o in real.venue._orders.values()} and real.held("VXUS") == 0.0

    report = real.service.autonomous_reductions()
    assert [row["outcome"] for row in report["approved"]] == ["SUBMITTED"]
    opened = next(p for p in real.proposals() if p.ticker == "VXUS")
    assert opened.status == EXECUTED and opened.client_order_id in real.venue._orders
    assert (opened.approved_via, opened.approved_by) == (autonomy.AUTONOMOUS_VIA, "autonomy:open")
    assert opened.authority_version == autonomy.note_version(profiles, real.db)
    assert real.held("VXUS") > 0 and real.ledger().position("VXUS", MICRO).opened_by == "autonomy:open"
    views = {view["symbol"]: view for view in real.service.auto_live()["positions"]}
    assert views["VXUS"]["strategy_version"] == views["BND"]["strategy_version"] == active["opening_version"]

    # Its stop: written by the pass, sent by the loop, never more than code bought.
    real.move("VXUS", 0.85)
    stopped = real.service.auto_tick()["live"]["pass"]
    assert {row["symbol"]: row["exit"]["reason"] for row in stopped["exits"]}["VXUS"] == "stop_loss"
    real.service.autonomous_reductions()
    closing = next(p for p in real.proposals() if p.action == "SELL")
    assert closing.status == EXECUTED and closing.approved_by == "autonomy:exit"
    assert real.held("VXUS") == 0.0
    trade = real.service.auto_live()["trades"][0]
    assert trade["closed_by"] == "code" and trade["realized_usd"] < 0


def test_the_plans_first_real_purchase_waits_for_the_owner_and_only_the_first(busier):
    """Contract 2.5. Derived from the records every time: a purchase the owner approved that has
    filled, written for this plan. Until then code approves none of the plan's purchases."""
    real = busier
    active = real.in_force()
    real.research("VXUS", confidence=90)
    real.service.auto_tick()
    waiting = next(p for p in real.proposals() if p.ticker == "VXUS")
    for _ in range(2):                                        # asked again, the answer is the same
        report = real.service.autonomous_reductions()
        assert report["approved"] == [] and report["declined"] == [
            {"proposal_id": waiting.proposal_id, "reasons": [FIRST_REAL_ORDER_WAITS]}]
    assert real.venue._orders == {}
    # Rejected by the owner, it is not the first purchase: the next one still waits.
    real.service.reject(waiting.proposal_id, reason="not this one")
    real.forget_research()
    first = real.first_purchase("BND", confidence=90)
    assert autonomy.first_purchase_seen(real.proposals(), plan_id=active["plan_id"], bucket=MICRO)
    assert first.status == EXECUTED
    # After it, code approves the plan's purchases.
    real.research("VGIT", confidence=90)
    real.service.auto_tick()
    assert [row["outcome"] for row in real.service.autonomous_reductions()["approved"]] == ["SUBMITTED"]
    assert real.held("VGIT") > 0
    # The simulated account never waits: it is what the rehearsal is for.
    sim = real.sim()
    assert not any(FIRST_REAL_ORDER_WAITS in reason for row in sim.autonomous_reductions()["declined"]
                   for reason in row["reasons"])


def test_a_purchase_written_before_a_pause_waits_for_a_person_in_the_real_account(real):
    """Found in review on 2026-10-14: the fifteen-second loop sent it anyway."""
    real.in_force()
    real.research("VXUS", confidence=90)
    real.service.auto_tick()
    waiting = next(p for p in real.proposals() if p.ticker == "VXUS")
    assert waiting.status == PROPOSED
    real.service.auto_pause(scope="live", reason="stepping away", identity="owner")
    report = real.service.autonomous_reductions()
    assert report["approved"] == []
    assert report["declined"] == [{"proposal_id": waiting.proposal_id, "reasons": [
        "the automatic-trading plan is paused (stepping away); a purchase it wrote waits for a person"]}]
    assert real.venue._orders == {} and real.held("VXUS") == 0.0
    # Resumed, code still leaves it: it is the plan's first real purchase, and the owner's.
    real.service.auto_resume(scope="live", note="back", identity="owner", confirmed=True)
    report = real.service.autonomous_reductions()
    assert report["approved"] == [] and report["declined"][0]["reasons"] == [FIRST_REAL_ORDER_WAITS]
    real.by_hand(waiting)
    assert real.held("VXUS") > 0


def test_pausing_and_stopping_take_no_password_and_resuming_does(real):
    real.in_force()
    paused = real.service.auto_pause(scope="live", reason="stepping away", identity="owner")
    assert paused["paused"] is True and paused["scope"] == "live"
    real.research("VXUS", confidence=90)
    held_back = real.service.auto_tick()["live"]["pass"]["signals"][0]
    assert held_back["diagnosis"]["code"] == "plan_paused" and real.proposals() == []
    with pytest.raises(ServiceError, match="needs the console password"):
        real.service.auto_resume(scope="live", note="back", identity="owner")
    resumed = real.service.auto_resume(scope="live", note="back", identity="owner", confirmed=True)
    assert resumed["changed"] is True and resumed["scope"] == "live"
    # The simulated account's pause is its own: neither act above touched it.
    assert real.service.auto_simulation()["state"]["paused"] is False

    stopped = real.service.auto_stop(scope="live", reason="done", identity="owner")
    assert stopped["stopped"] is True and store.active_plan(real.db) is None
    assert real.service.auto_live()["ended"]["withdrawn_by"] == "owner"
    with pytest.raises(ServiceError, match="no plan is in force on the real account"):
        real.service.auto_pause(scope="live")


def test_the_real_plan_stops_opening_when_the_code_it_was_validated_on_changes(real, monkeypatch):
    active = real.in_force()
    real.first_purchase("VXUS", confidence=90)
    quantity = real.held("VXUS")
    assert quantity > 0

    # A release changes the trading logic. Same parameters, different code: a different version.
    monkeypatch.setattr(store, "logic_digest", lambda: "a-later-release")
    real.research("BND", confidence=92)
    tick = real.service.auto_tick()
    moved = tick["live"]["pass"]["version_moved"]
    assert moved["action"] == "withdrawn" and moved["from"] == active["opening_version"]
    assert store.active_plan(real.db) is None
    ended = real.service.auto_live()["ended"]
    assert ended["withdrawn_by"] == runner.CODE_CHANGED and "no record of its own" in ended["withdrawn_reason"]
    assert [p.ticker for p in real.proposals() if p.action == "BUY"] == ["VXUS"], "nothing new was opened"
    # The simulated account goes on, under the new version, from nothing.
    assert tick["version_moved"]["action"] == "rebound"
    sim_plan = store.active_plan(real.sim().state_db())
    assert sim_plan["opening_version"] == moved["to"] and sim_plan["managing"] == [active["opening_version"]]
    assert store.simulated_days(real.sim().state_db(), moved["to"]) == 0
    # So the real account cannot simply be started again: the new version has no record.
    gates = real.gates()
    assert gates["simulation_record"]["ok"] is False and real.candidate()["version_id"] == moved["to"]
    with pytest.raises(ServiceError, match="not open on the real account"):
        real.start()

    # What it had bought is still code's to close, on the stop it was bought under.
    real.move("VXUS", 0.85)
    after = real.service.auto_tick()["live"]["pass"]
    assert after["exits"][0]["exit"]["reason"] == "stop_loss"
    real.service.autonomous_reductions()
    assert real.held("VXUS") == 0.0


def test_the_simulated_configuration_follows_the_real_one(real):
    real.simulate()
    sim_config = paper.root_of(real.service.profiles()) / "config" / "risk_profile.yaml"
    loose = "max_orders_per_week:        {value: TODO, recommended_default: 4}"
    assert real.config().count(loose) == 1, "the configuration no longer reads as this test expects"
    assert real.sim().profiles().effective(
        real.sim().profiles().risk_cfg("buckets", MICRO, "activity_limits", "max_orders_per_week"), "x") == 4
    stamp = sim_config.stat().st_mtime_ns
    real.service.auto_tick()
    assert sim_config.stat().st_mtime_ns == stamp, "nothing moved, so nothing is rewritten"
    # A limit is tightened in Settings on the real account.
    path = real.root / "config" / "risk_profile.yaml"
    path.write_text(real.config().replace(loose, "max_orders_per_week:        2"), encoding="utf-8")
    real.service.auto_tick()
    rehearsed = real.sim().profiles()
    assert rehearsed.risk_cfg("buckets", MICRO, "activity_limits", "max_orders_per_week") == 2
    assert paper.is_paper(rehearsed), "still the simulated account's, with its marker"
    assert rehearsed.risk_cfg("buckets", MICRO, "autonomy", "managed_exit", "enabled") is True
    assert real.service.profiles().risk_cfg("buckets", MICRO, "autonomy", "managed_exit", "enabled",
                                            default=None) is None


def test_what_the_account_has_come_to_is_read_from_its_own_marks(real):
    real.in_force()
    assert real.service.auto_live()["record"]["sessions"] == 1, "one pass has run, in one session"
    real.first_purchase("VXUS", confidence=90)
    real.move("VXUS", 0.97)
    real.service.auto_tick()
    live = real.service.auto_live()
    record, mark = live["record"], live["account_mark"]
    # The real ledger's $100 was never booked as a deposit, so no funding is on record. Its
    # result is then not "the whole $100": against funding it is unknown, and since the record
    # began it is what the position has lost.
    assert mark["funded_usd"] == 0.0 and mark["result_usd"] is None and mark["result_pct"] is None
    assert record["measured_from"] == "first_mark" and record["equity_usd"] == mark["equity_usd"]
    assert -2.0 < record["result_usd"] < 0, "about 3% of one $33 position, and nothing like $100"
    assert record["result_usd"] == pytest.approx(mark["equity_usd"] - 100.0, abs=0.01)
    assert record["max_drawdown_pct"] <= 0 and record["net_return_pct"] < 0
    assert live["plan"]["deployable_usd"] > 0
    # The simulated account was opened with a deposit, so it is measured against that, and a
    # later deposit there is not a gain either.
    sim = real.service.auto_simulation()["record"]
    assert sim["measured_from"] == "funding" and sim["equity_usd"] != record["equity_usd"]
    before = sim["result_usd"]
    real.service.auto_fund(amount_usd=50.0, kind="deposit")
    real.service.auto_tick()
    after = real.service.auto_simulation()["record"]
    assert after["funded_usd"] == 150.0 and after["result_usd"] == pytest.approx(before, abs=0.05)


def test_how_a_plans_settings_would_land_is_worked_out_once_and_again_when_the_configuration_moves(
        real, monkeypatch):
    real.simulate()
    first = real.service.auto_plan()["landing"]
    assert first["needs_password"] is True

    def must_not_run(*_a, **_k):
        raise AssertionError("the configuration has not moved; nothing is classified again")

    with monkeypatch.context() as patch:
        patch.setattr(cc, "classify", must_not_run)
        assert real.service.auto_plan()["landing"] == first
        assert real.service.auto_live()["candidate"]["plan"]["landing"] == first
    # What comes back is a copy: a caller that scribbles on it changes nobody else's answer.
    real.service.auto_plan()["landing"]["changes"].clear()
    assert real.service.auto_plan()["landing"] == first
    # The owner turns automatic buying on on Home, with its confidence: those two settings are
    # then in force, and are no longer among what the plan would change.
    switched = {"buckets.micro_live.autonomy.open.enabled", "buckets.micro_live.autonomy.open.min_confidence"}
    assert switched <= {row["path"] for row in first["changes"]}
    real.service.autonomy_switch("open", True, value=80, identity="owner", confirmed=True)
    after = real.service.auto_plan()["landing"]
    assert {row["path"] for row in after["changes"]} == {row["path"] for row in first["changes"]} - switched


# ---------------------------------------------------------------- when it stops, a person is told
def _to_do(real, kind):
    return [item for item in real.service.attention()["items"] if item["kind"] == kind]


def _notices(real, prefix):
    return [event for event in real.service.collect_notifications() if event.key.startswith(prefix)]


def test_a_deployment_with_no_plan_raises_nothing_about_one(real):
    real.simulate()
    assert _to_do(real, "auto_trading_stopped") == [] and _to_do(real, "auto_trading_stalled") == []
    assert _notices(real, "auto_") == []


def test_going_into_force_is_announced_even_though_nobody_was_there(real):
    real.in_force()
    told = _notices(real, "auto_started:")
    assert len(told) == 1 and told[0].urgent is True
    # On the channel that already carries "a change you confirmed is now in force".
    assert told[0].kind == "config_change_applied" and "in force on the real account" in told[0].title
    assert _to_do(real, "auto_trading_stopped") == [], "an announcement, not something to do"
    assert _to_do(real, "auto_trading_stalled") == [], "it has just been run"


def test_a_plan_that_reaches_its_loss_line_stops_opening_and_says_so(real):
    real.in_force(loss_pause_pct=2.0)                         # a line of $2 on $100
    real.first_purchase("VXUS", confidence=90)
    assert real.held("VXUS") > 0
    real.move("VXUS", 0.80)
    passed = real.service.auto_tick()["live"]["pass"]
    assert passed["paused"]["kind"] == store.LOSS_LINE and passed["loss"]["reached"] is True

    items = _to_do(real, "auto_trading_stopped")
    assert len(items) == 1 and "paused itself at its loss line" in items[0]["title"]
    assert items[0]["requires_person"] is True and items[0]["severity"] == "high"
    assert items[0]["link"] == {"page": "auto", "params": {"scope": "live"}}
    # Said every time the line is mentioned: it is a check this machine makes, not a guarantee.
    assert "不是最大亏损的保证" in items[0]["detail_i18n"]["zh"]
    assert "not a limit on what can be lost" in items[0]["detail_i18n"]["en"]
    told = _notices(real, "auto_paused:")
    assert len(told) == 1 and (told[0].kind, told[0].urgent) == ("autonomy_withdrawn", True)

    # Paused is not abandoned. The position past its stop is still sold, and settled.
    real.service.autonomous_reductions()
    assert real.held("VXUS") == 0.0
    assert real.service.reconcile_orders() is not None
    # And nothing new is opened, however good the research.
    real.research("BND", confidence=95)
    held_back = real.service.auto_tick()["live"]["pass"]["signals"][0]
    assert held_back["diagnosis"]["code"] == "plan_paused" and real.held("BND") == 0.0

    # A person looks and resumes, with the password; the answer says where the line now starts.
    resumed = real.service.auto_resume(scope="live", note="looked", identity="owner", confirmed=True)
    assert resumed["loss_line_moved"]["from"] == 0.0 and resumed["loss_line_moved"]["to"] < -2.0
    assert _to_do(real, "auto_trading_stopped") == [] and _notices(real, "auto_paused:") == []


def test_an_exit_nobody_could_judge_for_more_than_a_session_is_a_to_do(real):
    """Contract 2.4 and 3.4. One session undecided is a vendor's bad day; past that, a stop of
    the plan's is not being applied, and only a person can find out why."""
    real.in_force()
    said = {"zh": "VTI 的价格是 10-14 的，太旧，今天不判断止损。",
            "en": "VTI is priced from 10-14, too old to judge its stop today."}
    seen = {"since": "2026-10-19", "last": "2026-10-19", "code": positions.PRICE_STALE,
            "detail": positions.PRICE_STALE, "price_date": "2026-10-14", "text": said,
            "strategy_version": "sv-1"}
    real.db.write_meta(trader.UNDECIDED, {"VTI": seen})
    assert _to_do(real, "auto_exit_undecided") == [], "a single session is not yet a to-do"

    real.db.write_meta(trader.UNDECIDED, {"VTI": {**seen, "last": "2026-10-20"}})
    items = _to_do(real, "auto_exit_undecided")
    assert len(items) == 1 and items[0]["subject"] == "VTI" and items[0]["actions"] == ["open"]
    assert (items[0]["severity"], items[0]["requires_person"]) == ("high", True)
    assert items[0]["link"] == {"page": "auto", "params": {"scope": "live"}}
    assert items[0]["detail_i18n"]["zh"].startswith(said["zh"]) and "止损暂时不起作用" in items[0]["detail_i18n"]["zh"]
    told = _notices(real, "auto_exit_undecided:")
    assert len(told) == 1 and told[0].urgent is True

    # The simulated account's are for its own page, never Home's.
    real.db.write_meta(trader.UNDECIDED, {})
    real.sim().state_db().write_meta(trader.UNDECIDED, {"VTI": {**seen, "last": "2026-10-21"}})
    assert _to_do(real, "auto_exit_undecided") == []


def test_a_plan_stopped_by_a_change_of_code_is_a_to_do_that_says_what_to_do(real, monkeypatch):
    real.in_force()
    monkeypatch.setattr(store, "logic_digest", lambda: "a-later-release")
    real.service.auto_tick()
    items = _to_do(real, "auto_trading_stopped")
    assert len(items) == 1 and "the trading logic changed" in items[0]["title"]
    assert "重新积累记录后，再由你确认启动" in items[0]["detail_i18n"]["zh"]
    assert _notices(real, "auto_stopped:")[0].kind == "autonomy_withdrawn"
    assert store.plan_record(real.db)["history"][-1]["withdrawn_by"] == runner.CODE_CHANGED


def test_a_plan_a_person_stopped_raises_nothing(real):
    real.in_force()
    real.service.auto_stop(scope="live", reason="done", identity="owner")
    assert _to_do(real, "auto_trading_stopped") == [] and _notices(real, "auto_stopped:") == []


def test_a_confirmation_that_ran_out_is_a_to_do(real):
    real.ready()
    real.start()
    pending = store.start_request(real.db)
    store.write_request(real.db, {**pending, "expires_at": (utcnow() - timedelta(minutes=1)).isoformat()})
    real.service.auto_tick()
    items = _to_do(real, "auto_trading_stopped")
    assert len(items) == 1 and "did not start" in items[0]["title"] and "ran out" in items[0]["detail"]


def test_a_plan_that_is_not_being_run_is_noticed_while_the_market_is_open(real, monkeypatch):
    real.in_force()
    assert _to_do(real, "auto_trading_stalled") == []
    # The loop stops: the last pass is a quarter of an hour old and the market is open.
    store.note_pass(real.db, at=datetime.now(UTC) - timedelta(minutes=15), error=None, market_open=True)
    items = _to_do(real, "auto_trading_stalled")
    assert len(items) == 1 and "not being run" in items[0]["title"]
    assert "不会检查持仓的止损和到期" in items[0]["detail_i18n"]["zh"]
    assert _notices(real, "auto_stalled:")[0].kind == "backup_overdue"
    # A pass that runs and fails is said to have failed, with why.
    store.note_pass(real.db, at=datetime.now(UTC), error="PortfolioError: the ledger is stale", market_open=True)
    failed = _to_do(real, "auto_trading_stalled")
    assert "last pass failed" in failed[0]["title"] and "the ledger is stale" in failed[0]["detail"]
    # With the market shut nothing is expected, and nothing is raised.
    from agentic_trading.service.shapes import MarketStatus

    monkeypatch.setattr(Service, "market_status", lambda self: MarketStatus(is_open=False))
    store.note_pass(real.db, at=datetime.now(UTC) - timedelta(hours=6), error=None, market_open=False)
    assert _to_do(real, "auto_trading_stalled") == []


def test_a_pass_that_fails_is_recorded_as_one_and_the_next_one_still_runs(real, monkeypatch):
    real.in_force()
    from agentic_trading.auto import trader

    def broken(*_a, **_k):
        raise RuntimeError("the book could not be priced")

    with monkeypatch.context() as patch:
        patch.setattr(trader, "tick", broken)
        failed = real.service.auto_tick()
    assert "the book could not be priced" in failed["live"]["pass"]["error"]
    assert store.last_pass(real.db)["error"].startswith("RuntimeError")
    assert failed["live"]["pass"]["recorded"]["problems"] == [runner.PASS_FAILED]
    assert "last pass failed" in _to_do(real, "auto_trading_stalled")[0]["title"]
    # Nothing was left half done, and the next pass is an ordinary one.
    again = real.service.auto_tick()["live"]["pass"]
    assert again["error"] is None and store.last_pass(real.db)["error"] is None
    assert _to_do(real, "auto_trading_stalled") == []


# ---------------------------------------------------------------- from a terminal
def test_a_terminal_can_read_and_stop_the_real_account_and_nothing_else(real, monkeypatch, capsys):
    from agentic_trading.auto import cli as auto_cli

    # The command builds its own service on the configuration; here that is the test's account.
    monkeypatch.setattr("agentic_trading.service.Service", lambda _config: real.service)

    def run(*args):
        code = auto_cli.main(["--config-dir", str(real.root / "config"), *args])
        return code, capsys.readouterr().out

    code, out = run("live")
    assert code == 0 and "没有方案生效" in out and "先启动模拟" in out
    real.ready()
    code, out = run("live")
    assert code == 0 and "✓" in out and "✗" in out
    assert "不代表策略已被证明能盈利" in out and "需要控制台密码，请在控制台里操作" in out
    assert "不是最大亏损的保证" in out, "what the loss line is not is said with it"
    # There is no way to start, resume or replace from here: the commands do not exist.
    for refused in (["start", "--live", "--plan-id", "x" * 16], ["resume", "--live", "--note", "x"],
                    ["replace"], ["evaluate"]):
        with pytest.raises(SystemExit):
            run(*refused)
        capsys.readouterr()
    assert store.active_plan(real.db) is None and store.start_request(real.db) is None

    real.start()
    real.cool()
    real.service.auto_tick()
    code, out = run("live")
    assert code == 0 and "方案生效中" in out and "steady" in out
    # A pass asked for from the terminal is the simulated account's alone.
    before = store.last_pass(real.db)
    code, out = run("tick")
    assert code == 0 and store.last_pass(real.db) == before, "the real account's pass was not run"
    code, out = run("pause", "--live", "--reason", "from the terminal")
    assert code == 0 and "已暂停" in out and store.state(real.db)["paused"] is True
    code, out = run("stop", "--live", "--reason", "from the terminal")
    assert code == 0 and "没有方案生效" in out and store.active_plan(real.db) is None
    assert run("pause", "--live", "--reason", "again")[0] == 2


# ---------------------------------------------------------------- over HTTP
def test_the_real_account_over_http(auth_client, real, monkeypatch):
    monkeypatch.setattr("agentic_trading.api.app._service", real.service)
    live = auth_client.get("/auto-trading/live")
    assert live.status_code == 200 and live.json()["candidate"]["available"] is False
    real.ready()
    inputs = auth_client.get("/auto-trading/live").json()["candidate"]["inputs"]
    body = {**inputs, "scope": "live"}
    bare = auth_client.post("/auto-trading/start", json=body)
    assert bare.status_code == 400 and "console password" in bare.json()["detail"]
    wrong = auth_client.post("/auto-trading/start", json={**body, "password": "not-it"})
    assert wrong.status_code == 400 and "did not match" in wrong.json()["detail"]
    assert store.start_request(real.db) is None
    for refused in ({**body, "confirmed": True}, {**body, "force": True}, {**body, "scope": "real"}):
        assert auth_client.post("/auto-trading/start", json=refused).status_code == 422, refused
    confirmed = auth_client.post("/auto-trading/start", json={**body, "password": PASSWORD})
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["started"] is False and confirmed.json()["waiting"]["requested_by"] == "test-owner"
    assert auth_client.get("/auto-trading/live").json()["request"]["status"] == "waiting"
    assert auth_client.get("/auto-trading").json()["real_account"]["request"]["status"] == "waiting"
    back = auth_client.post("/auto-trading/live/withdraw", json={"reason": "not yet"})
    assert back.status_code == 200 and back.json()["withdrawn"] is True
    # With nothing in force there, there is nothing to pause or stop, and each says so.
    nothing = auth_client.post("/auto-trading/pause", json={"scope": "live"})
    assert nothing.status_code == 400 and "no plan is in force on the real account" in nothing.json()["detail"]
    assert auth_client.post("/auto-trading/stop", json={"scope": "live"}).json()["stopped"] is False


def test_resuming_the_real_account_over_http_takes_the_password(auth_client, real, monkeypatch):
    monkeypatch.setattr("agentic_trading.api.app._service", real.service)
    real.in_force()
    assert auth_client.post("/auto-trading/pause", json={"scope": "live", "reason": "away"}).json()["paused"] is True
    bare = auth_client.post("/auto-trading/resume", json={"scope": "live", "reason": "back"})
    assert bare.status_code == 400 and "console password" in bare.json()["detail"]
    wrong = auth_client.post("/auto-trading/resume", json={"scope": "live", "password": "not-it"})
    assert wrong.status_code == 400 and "did not match" in wrong.json()["detail"]
    assert store.state(real.db)["paused"] is True
    back = auth_client.post("/auto-trading/resume", json={"scope": "live", "password": PASSWORD})
    assert back.status_code == 200 and back.json()["changed"] is True
    # Stopping needs none.
    assert auth_client.post("/auto-trading/stop", json={"scope": "live"}).json()["stopped"] is True
    assert store.active_plan(real.db) is None
