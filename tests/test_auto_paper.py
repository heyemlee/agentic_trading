"""The simulated account is the real account's rules with no way to the real venue.

Two properties, and they are different in kind (docs/todo.md, P2.1):

* **its state is its own** -- a directory beside the real state, with its own database, so
  nothing it records can be read as the real account's;
* **its venue cannot be the real one** -- by a lock in the one place a real session is created,
  which holds whatever a caller does, and not only by each caller choosing the simulator.

Everything here runs in a temporary copy of the configuration. Offline.
"""

import shutil

import pytest

from agentic_trading import config_changes as cc
from agentic_trading.auto import paper
from agentic_trading.broker import BrokerError, SimulatedBroker
from agentic_trading.micro_live import bucket_is_executable
from agentic_trading.portfolio import load_portfolio
from agentic_trading.profile import load_profiles
from agentic_trading.proposals import SUBMITTED
from agentic_trading.service import Service
from tests.conftest import ROOT, copy_example_ledger, recent_session

MICRO = "micro_live"


@pytest.fixture
def live(tmp_path):
    """A stand-in for the deployment: its configuration, a state directory, a funded ledger."""
    root = tmp_path / "live"
    shutil.copytree(ROOT / "config", root / "config")
    (root / "state").mkdir()
    (root / "runs").mkdir()
    copy_example_ledger("micro_live_portfolio.example.yaml", root / "state" / "micro_live_portfolio.yaml")
    return load_profiles(root / "config")


@pytest.fixture
def sim(live):
    return load_profiles(paper.ensure(live))


def _files(directory):
    return {str(path.relative_to(directory)): path.read_bytes()
            for path in sorted(directory.rglob("*")) if path.is_file()}


# ---------------------------------------------------------------- its own place
def test_it_lives_beside_the_real_state_under_a_configuration_that_says_what_it_is(live):
    before = _files(live.config_dir)
    config = paper.ensure(live)
    root = live.project_dir / "state" / "paper"
    assert config == root / "config" and paper.root_of(live) == root
    assert all((root / name).is_dir() for name in ("config", "state", "runs"))
    assert _files(live.config_dir) == before, "the real configuration is read, never written"

    sim = load_profiles(config)
    assert paper.is_paper(sim) is True and paper.is_paper(live) is False
    assert sim.project_dir == root, "every state path it resolves hangs off its own directory"
    marker = sim.risk[paper.MARKER]
    assert marker["enabled"] is True and marker["venue"] == "simulated" and marker["plan_id"] == "none"
    # What it sets whatever the plan says: nothing real is switched on by the first of these.
    assert sim.risk["micro_live_execution"]["enabled"] is True
    assert sim.risk["notifications"]["channel"] == "none"
    assert sim.risk["research_schedule"]["enabled"] is False
    # And everything else is the real account's, rule for rule.
    for section in ("buckets", "sizing", "confidence_requirements", "drawdown_ladder", "permissions"):
        assert sim.risk[section] == live.risk[section], section


def test_its_database_and_ledger_are_not_the_real_ones(live, sim):
    real, simulated = Service(live.config_dir), Service(sim.config_dir)
    real_db, sim_db = real.state_db().path, simulated.state_db().path
    assert sim_db != real_db and paper.root_of(live) in sim_db.parents
    assert paper.root_of(live) not in real_db.parents
    assert paper.ledger_path(sim) == paper.root_of(live) / "state" / "micro_live_portfolio.yaml"
    assert paper.ledger_path(sim) != live.project_dir / "state" / "micro_live_portfolio.yaml"


def test_it_is_rebuilt_when_what_it_was_built_from_changes_and_only_then(live):
    config = paper.ensure(live)
    target = config / "risk_profile.yaml"
    first = target.read_text()
    stamp = target.stat().st_mtime_ns
    assert paper.ensure(live) == config and target.stat().st_mtime_ns == stamp, "nothing moved"

    # The plan it rehearses changes: its settings are the plan's, so it is rebuilt.
    change = {"file": "risk_profile.yaml", "path": "sizing.min_order_usd", "to": 5}
    paper.ensure(live, changes=[change], plan_id="plan-1")
    second = load_profiles(config)
    assert second.risk["sizing"]["min_order_usd"] == 5 and second.risk[paper.MARKER]["plan_id"] == "plan-1"
    assert live.risk["sizing"]["min_order_usd"] == 25, "the real account still has its own setting"

    # The real configuration changes: it follows.
    real = live.config_dir / "risk_profile.yaml"
    real.write_text(real.read_text() + "\n# a later edit\n")
    paper.ensure(load_profiles(live.config_dir), changes=[change], plan_id="plan-1")
    third = target.read_text()
    assert "# a later edit" in third and third != first
    assert third.count(f"\n{paper.MARKER}:") == 1


def test_it_cannot_be_nested_and_a_directory_without_the_marker_is_refused(live, sim):
    with pytest.raises(paper.PaperError, match="nested"):
        paper.ensure(sim)
    assert paper.is_paper(paper.profiles_of(live))
    # Somebody strips the marker from the generated file: it is no longer usable as the
    # simulated account, rather than quietly becoming a second configuration of the real one.
    target = sim.config_dir / "risk_profile.yaml"
    text = target.read_text()
    target.write_text(text[:text.index(f"\n{paper.MARKER}:")])
    with pytest.raises(paper.PaperError, match="marker"):
        paper.profiles_of(live)


def test_it_does_not_exist_until_it_is_prepared(live):
    assert paper.profiles_of(live) is None
    assert not paper.root_of(live).exists()


@pytest.mark.parametrize("value", [None, {}, {"enabled": False}, {"enabled": "true"}, {"enabled": 1}, True])
def test_only_the_marker_exactly_makes_a_configuration_the_simulated_one(live, value):
    if value is None:
        live.risk.pop(paper.MARKER, None)
    else:
        live.risk[paper.MARKER] = value
    assert paper.is_paper(live) is False
    assert paper.is_paper(None) is False


def test_the_console_cannot_turn_the_real_configuration_into_the_simulated_one(live):
    """The marker is a top-level section, and the console can neither add one nor create into one."""
    path = live.config_dir / "risk_profile.yaml"
    text = path.read_text()
    with pytest.raises(cc.ConfigChangeError):
        cc.set_values(text, {f"{paper.MARKER}.enabled": True},
                      create=frozenset({f"{paper.MARKER}.enabled"}))
    with pytest.raises(cc.ConfigChangeError, match="top-level"):
        cc.validate(live.config_dir, "risk_profile.yaml",
                    text + f"\n{paper.MARKER}:\n  enabled: true\n")


# ---------------------------------------------------------------- no way to the real venue
def test_a_session_with_the_real_venue_cannot_be_created_under_it(sim, monkeypatch):
    from agentic_trading.broker import robinhood

    def touched(*_args, **_kwargs):
        raise AssertionError("the refusal has to come before anything of the real venue is read")

    monkeypatch.setattr(robinhood.FileTokenStorage, "__init__", touched)
    monkeypatch.setattr(robinhood, "_Portal", touched)
    for kwargs in ({}, {"connect": False}):
        with pytest.raises(BrokerError, match="simulated account"):
            robinhood.RobinhoodBroker(sim, **kwargs)


def test_every_account_it_is_asked_for_is_the_simulator(sim, monkeypatch):
    from agentic_trading.broker import robinhood

    monkeypatch.setattr(robinhood.RobinhoodBroker, "__init__", lambda *_a, **_k: pytest.fail(
        "the simulated account reached for the real venue"))
    service = Service(sim.config_dir)
    for bucket in (MICRO, "core", "sandbox"):
        venue = service._broker(service.profiles(), bucket)
        assert isinstance(venue, SimulatedBroker) and venue.name == "simulated"
        assert venue._quantity_increment == paper.QUANTITY_INCREMENT
    # The binding between account and venue is turned round for it, and only for it.
    assert bucket_is_executable(sim, MICRO, "simulated")[0] is True
    refused, why = bucket_is_executable(sim, MICRO, "robinhood")
    assert refused is False and "simulated account" in why


def test_the_real_account_is_bound_as_it_always_was(live):
    assert bucket_is_executable(live, MICRO, "simulated")[0] is False
    assert bucket_is_executable(live, MICRO, "robinhood")[0] is True
    assert bucket_is_executable(live, "core", "robinhood")[0] is False
    with pytest.raises(paper.PaperError):
        paper.venue(live)                      # the simulated venue is not built for a real account
    with pytest.raises(paper.PaperError):
        paper.open_account(live, cash_usd=100.0, as_of=recent_session())


def test_reading_and_reconciling_never_leave_the_simulator(sim, monkeypatch):
    from agentic_trading.broker import robinhood
    from agentic_trading.execution_scenarios import _proposal

    monkeypatch.setattr(robinhood.RobinhoodBroker, "__init__", lambda *_a, **_k: pytest.fail(
        "the simulated account reached for the real venue"))
    paper.open_account(sim, cash_usd=1000.0, as_of=recent_session())
    service = Service(sim.config_dir)
    reading = service._read_venue_account()
    assert reading.connected is True and reading.account.cash == 1000.0 and reading.positions == {}

    # An order whose outcome is not on record is looked up at the simulator, not at Robinhood.
    waiting = _proposal(ticker="VTI", bucket=MICRO, status=SUBMITTED)
    waiting.client_order_id = "ct-paper-order-1"
    service._store(service.profiles()).save(waiting)
    result = service.reconcile_orders()
    assert [row["proposal_id"] for row in result["orders"]] == [waiting.proposal_id]


# ---------------------------------------------------------------- its money
def test_it_opens_once_with_a_deposit_that_is_funding_and_not_a_gain(live, sim):
    real_ledger = live.project_dir / "state" / "micro_live_portfolio.yaml"
    before = real_ledger.read_bytes()
    opened = paper.open_account(sim, cash_usd=1000.0, as_of=recent_session())
    assert opened == {"opened": True, "cash_usd": 1000.0}
    ledger = load_portfolio(paper.ledger_path(sim))
    assert ledger.buckets[MICRO].cash_usd == 1000.0 and ledger.positions == []
    assert [(flow.kind, flow.amount_usd, flow.bucket) for flow in ledger.cash_flows] == [
        ("deposit", 1000.0, MICRO)]
    # A second opening changes nothing: the balance is whatever trading has since made of it.
    assert paper.open_account(sim, cash_usd=5000.0, as_of=recent_session()) == {
        "opened": False, "cash_usd": 1000.0}
    assert len(load_portfolio(paper.ledger_path(sim)).cash_flows) == 1
    assert real_ledger.read_bytes() == before, "the real ledger is not what was opened"


@pytest.mark.parametrize("amount", [0, -50.0, True, "1000", None])
def test_it_needs_a_real_opening_balance(sim, amount):
    with pytest.raises(paper.PaperError, match="positive opening balance"):
        paper.open_account(sim, cash_usd=amount, as_of=recent_session())


def test_its_venue_starts_from_its_own_book_and_keeps_its_orders_beside_it(sim):
    paper.open_account(sim, cash_usd=750.0, as_of=recent_session())
    venue = paper.venue(sim, prices={"VTI": 100.0}, autoprice=False)
    assert venue.get_account().cash == 750.0 and venue.get_positions() == []
    assert venue._order_state_path == sim.project_dir / "state" / "paper_orders.json"
