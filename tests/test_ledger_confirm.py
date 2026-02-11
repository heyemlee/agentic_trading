"""Confirming a ledger: the way out of a fail-closed as_of is a check, not a stamp.

`portfolio_state.max_age_days` fails a ledger closed once nothing has confirmed it for a week.
That guard is only worth having if renewing the date costs something — if it can be cleared by
writing today's date over a book nobody read back, the guard measures paperwork rather than
truth. These tests hold the two confirmations to their checks: the venue-backed book against
the account's own cash and positions, the paper book against this system's executed orders.
"""
from __future__ import annotations

import json
import shutil

import pytest
import yaml

from agentic_trading import state_cli
from agentic_trading.broker.base import Account, BrokerPosition
from agentic_trading.execution_scenarios import _ledger, _profiles, _proposal
from agentic_trading.portfolio import (
    PortfolioError,
    confirm_from_broker,
    confirm_unbacked,
    last_session_date,
    load_portfolio,
)
from agentic_trading.proposals import PARTIALLY_FILLED, PROPOSED, ProposalStore
from agentic_trading.risk_scenarios import PRICES, TRADE_DATE, identity_lookup
from agentic_trading.state_db import StateDB
from tests.conftest import ROOT, copy_example_ledger

TODAY = last_session_date()


def account(cash: float, equity: float | None = None) -> Account:
    return Account(account_id="AG123456", cash=cash, buying_power=cash,
                   equity=equity if equity is not None else cash)


def held(symbol: str = "VTI", quantity: float = 4.0) -> BrokerPosition:
    price = PRICES[symbol]
    return BrokerPosition(symbol=symbol, quantity=quantity, average_entry_price=price,
                          market_value=price * quantity)


def stale_ledger(tmp_path, **kwargs):
    """A ledger dated TRADE_DATE, which is old enough that max_age_days has failed it closed."""
    path = _ledger(tmp_path, **kwargs)
    assert load_portfolio(path).as_of == TRADE_DATE
    return path


def test_a_venue_backed_ledger_is_confirmed_against_the_account(tmp_path):
    profiles = _profiles(tmp_path)
    path = stale_ledger(tmp_path, core_cash=2500.0, sandbox_cash=500.0,
                        positions=[{"symbol": "VTI", "bucket": "core", "quantity": 4.0,
                                    "cost_basis_per_share": PRICES["VTI"]}])
    before = load_portfolio(path)
    before.applied_orders = {"ct-1": {"symbol": "VTI", "bucket": "core"}}
    StateDB(path).write_document(path, before.model_dump(mode="json"))

    snapshot = confirm_from_broker(path, account=account(3000.0), broker_positions=[held()],
                                   profiles=profiles, trade_date=TODAY,
                                   identity_lookup=identity_lookup)

    assert snapshot.state.as_of == TODAY
    stored = load_portfolio(path)
    assert stored.as_of == TODAY
    assert "confirmed against the venue" in stored.notes
    # The order history is what every rate limit counts; confirming must not drop it.
    assert stored.applied_orders == {"ct-1": {"symbol": "VTI", "bucket": "core"}}


def test_cash_the_venue_does_not_report_refuses_the_date(tmp_path):
    profiles = _profiles(tmp_path)
    path = stale_ledger(tmp_path)                      # the book says $3,000 across two buckets

    with pytest.raises(PortfolioError, match="cash mismatch"):
        confirm_from_broker(path, account=account(2400.0), broker_positions=[],
                            profiles=profiles, trade_date=TODAY, identity_lookup=identity_lookup)

    assert load_portfolio(path).as_of == TRADE_DATE, "a refused confirmation must leave the date alone"


def test_a_position_the_ledger_cannot_attribute_refuses_the_date(tmp_path):
    profiles = _profiles(tmp_path)
    path = stale_ledger(tmp_path)

    with pytest.raises(PortfolioError, match="bucket ledger does not"):
        confirm_from_broker(path, account=account(3000.0), broker_positions=[held("VTI")],
                            profiles=profiles, trade_date=TODAY, identity_lookup=identity_lookup)

    assert load_portfolio(path).as_of == TRADE_DATE


def test_the_paper_book_is_held_against_the_orders_this_system_executed(tmp_path):
    """Core and Sandbox have no account to read back, so its own record is the whole check."""
    path = stale_ledger(tmp_path)

    with pytest.raises(PortfolioError, match="never reached this ledger"):
        confirm_unbacked(path, trade_date=TODAY, executed_orders={"ct-7"})
    assert load_portfolio(path).as_of == TRADE_DATE

    state = load_portfolio(path)
    state.applied_orders = {"ct-7": {"symbol": "VTI", "bucket": "core"}}
    StateDB(path).write_document(path, state.model_dump(mode="json"))

    confirmed = confirm_unbacked(path, trade_date=TODAY, executed_orders={"ct-7"})
    assert confirmed.as_of == TODAY
    assert load_portfolio(path).as_of == TODAY
    assert "no venue" in load_portfolio(path).notes


def test_a_dry_run_reports_what_it_found_and_writes_nothing(tmp_path):
    profiles = _profiles(tmp_path)
    path = stale_ledger(tmp_path)

    snapshot = confirm_from_broker(path, account=account(3000.0), broker_positions=[],
                                   profiles=profiles, trade_date=TODAY,
                                   identity_lookup=identity_lookup, dry_run=True)
    assert snapshot.state.as_of == TODAY
    assert load_portfolio(path).as_of == TRADE_DATE

    assert confirm_unbacked(path, trade_date=TODAY, executed_orders=set(), dry_run=True).as_of == TODAY
    assert load_portfolio(path).as_of == TRADE_DATE


# ── through ct-state, which is the only sanctioned writer of state/ ──────────────────

@pytest.fixture
def project(tmp_path):
    cfg = tmp_path / "config"
    shutil.copytree(ROOT / "config", cfg)
    state = tmp_path / "state"
    state.mkdir()
    copy_example_ledger("portfolio.example.yaml", state / "portfolio.yaml", as_of=TRADE_DATE)
    ProposalStore(state / "proposals.json").save(_proposal(status=PROPOSED))
    return tmp_path


def confirm(project, *args) -> tuple[int, dict]:
    out = project / "out.json"
    with out.open("w") as handle:
        import contextlib

        with contextlib.redirect_stdout(handle):
            code = state_cli.main(["confirm", "--config-dir", str(project / "config"), *args])
    return code, json.loads(out.read_text())


def test_ct_state_confirm_renews_the_paper_book_and_reports_what_it_checked(project):
    code, report = confirm(project, "--ledger", "conceptual")
    assert code == 0, report
    row = report["ledgers"][0]
    assert row["confirmed"] is True
    assert row["as_of"] == report["date"] == last_session_date()
    assert "no venue behind this book" in row["checked_against"]
    assert load_portfolio(project / "state" / "portfolio.yaml").as_of == report["date"]


def test_ct_state_confirm_refuses_while_the_broker_still_owns_an_order(project):
    store = ProposalStore(project / "state" / "proposals.json")
    pending = _proposal(status=PARTIALLY_FILLED)
    pending.client_order_id = "ct-open-1"
    pending.order_status = "partially_filled"   # as execution records a partly filled order
    pending.reconciled_at = "2026-09-15T14:31:00+00:00"
    store.save(pending)

    code, report = confirm(project, "--ledger", "conceptual")
    assert code == 3
    assert report["confirmed"] is False and report["unsettled_orders"]
    assert load_portfolio(project / "state" / "portfolio.yaml").as_of == TRADE_DATE


def test_ct_state_confirm_reports_a_venue_it_cannot_read_instead_of_dating_the_book(project, monkeypatch):
    ledger = project / "state" / "micro_live_portfolio.yaml"
    ledger.write_text(yaml.safe_dump({
        "as_of": TRADE_DATE, "currency": "USD", "buckets": {"micro_live": {"cash_usd": 100.0}},
        "positions": [], "recent_opens": []}), encoding="utf-8")

    def unreachable(_profiles):
        raise ConnectionError("no Robinhood session")

    monkeypatch.setattr("agentic_trading.broker.robinhood.RobinhoodBroker", unreachable)
    code, report = confirm(project, "--ledger", "micro_live")

    assert code == 3
    assert "no Robinhood session" in report["ledgers"][0]["error"]
    assert load_portfolio(ledger).as_of == TRADE_DATE


class FakeVenue:
    """A broker session that records whether it was handed back."""

    def __init__(self, cash=100.0, positions=(), unreadable=False):
        self._cash, self._positions, self._unreadable = cash, list(positions), unreadable
        self.closed = False

    def get_account(self):
        return account(self._cash)

    def get_positions(self):
        if self._unreadable:
            raise ConnectionError("no Robinhood session")
        return self._positions

    def close(self):
        self.closed = True


def micro_project(project, cash=100.0):
    ledger = project / "state" / "micro_live_portfolio.yaml"
    ledger.write_text(yaml.safe_dump({
        "as_of": TRADE_DATE, "currency": "USD", "buckets": {"micro_live": {"cash_usd": cash}},
        "positions": [], "recent_opens": []}), encoding="utf-8")
    return ledger


@pytest.mark.parametrize("unreadable", [False, True])
def test_the_venue_session_is_handed_back_however_the_check_ends(project, unreadable):
    """Left open, the session is terminated from a finaliser at exit — after the thread pool
    that would carry the request is gone, which printed "cannot schedule new futures after
    shutdown" over the result of every confirm that reached the venue."""
    from agentic_trading.ledger_confirm import confirm_ledgers
    from agentic_trading.profile import load_profiles

    micro_project(project)
    venue = FakeVenue(unreadable=unreadable)
    report = confirm_ledgers(load_profiles(project / "config"),
                             ProposalStore(project / "state" / "proposals.json"),
                             ledger="micro_live", broker_factory=lambda _p: venue)

    assert venue.closed is True
    assert report["confirmed"] is not unreadable
