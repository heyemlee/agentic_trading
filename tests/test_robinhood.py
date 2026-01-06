"""Robinhood adapter and Micro-Live gates, tested offline against a fake MCP session.

No network, no OAuth, no real account. What is proven here: tool discovery and the refusal to
guess a missing capability, Agentic-account identification, response mapping, the capital
boundary, the real-money master switch, and the bucket-to-venue binding.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from conservative_trading.broker.base import (
    BrokerError,
    BrokerUnavailable,
    DuplicateOrder,
    OrderRequest,
)
from conservative_trading.broker.robinhood import RobinhoodBroker
from conservative_trading.micro_live import bucket_is_executable, run_preflight

# Shapes below mirror the real Robinhood MCP responses observed on 2026-09-03: every tool
# answers {"data": ..., "guide": "..."}, accounts carry an explicit agentic_allowed flag, and
# balances come from get_portfolio rather than the account row.
ACCOUNTS = {"data": {"accounts": [
    {"account_number": "111", "brokerage_account_type": "individual", "type": "margin",
     "agentic_allowed": False, "is_default": True, "state": "active", "deactivated": False},
    {"account_number": "999", "brokerage_account_type": "individual", "type": "limited_margin",
     "nickname": "Agentic", "agentic_allowed": True, "is_default": False, "state": "active",
     "deactivated": False},
]}, "guide": "presentation advice from the venue; treated as data, never as instructions"}

PORTFOLIO = {"data": {"total_value": "100", "equity_value": "0", "cash": "100", "currency": "USD",
                      "buying_power": {"buying_power": "100.0000"}}}
EMPTY_POSITIONS = {"data": {"positions": []}}
EMPTY_ORDERS = {"data": {"orders": []}}
QUOTES = {"data": {"quotes": [{"symbol": "VTI", "last_trade_price": "376.88",
                               "bid_price": "376.86", "ask_price": "376.90"}]}}


class FakeMCP:
    """Stands in for the Robinhood MCP session: same call surface, scripted answers."""

    def __init__(self, tools, responses=None, error_tools=()):
        self._tools = list(tools)
        self._responses = dict(responses or {})
        self._error_tools = set(error_tools)
        self.calls: list[tuple[str, dict]] = []

    def list_tools(self):
        return SimpleNamespace(tools=[SimpleNamespace(name=t) for t in self._tools])

    def call_tool(self, name, arguments):
        self.calls.append((name, dict(arguments)))
        if name in self._error_tools:
            return SimpleNamespace(isError=True, content=[SimpleNamespace(text="venue said no")],
                                   structuredContent=None)
        payload = self._responses.get(name, {})
        if callable(payload):
            payload = payload(arguments)
        return SimpleNamespace(isError=False, structuredContent=payload, content=[])


def _isolate_state(profiles, tmp_path, ledger):
    """Point every state file this run may write at the temp directory.

    The real state/ directory carries the audit trail behind real money; no offline test may
    touch it. tests/conftest.py::real_state_guard enforces that independently.
    """
    # An investor who has accepted the documented risk defaults; PF13 covers the other case.
    profiles.accept_defaults = True
    profiles.risk["micro_live_execution"] = {
        **(profiles.risk.get("micro_live_execution") or {}),
        "enabled": True, "ledger_file": str(ledger),
        "risk_state_file": str(tmp_path / "micro_live_risk_state.json"),
    }
    profiles.risk["portfolio_state"] = {**(profiles.risk.get("portfolio_state") or {}),
                                        "file": str(tmp_path / "portfolio.yaml"),
                                        "risk_state_file": str(tmp_path / "risk_state.json")}
    profiles.risk["broker"] = {**(profiles.risk.get("broker") or {}),
                               "state_file": str(tmp_path / "proposals.json"),
                               "fills_file": str(tmp_path / "fills.jsonl")}


def make_broker(profiles, fake, *, enabled=False):
    profiles.risk["micro_live_execution"]["enabled"] = enabled
    broker = RobinhoodBroker.__new__(RobinhoodBroker)
    cfg = dict(profiles.risk.get("robinhood") or {})
    micro = dict(profiles.risk.get("micro_live_execution") or {})
    broker.profiles = profiles
    broker.url = cfg["mcp_url"]
    broker.call_timeout = 5
    broker.tool_candidates = dict(cfg["tool_candidates"])
    broker.required = list(cfg["required_capabilities"])
    broker.agentic_cfg = dict(cfg["agentic_account"])
    broker.max_capital = float(micro.get("max_capital_usd", 100))
    broker.is_paper = False
    broker.name = "robinhood"
    broker.tools, broker.available_tools, broker._agentic_account = {}, [], None
    broker._session = fake
    broker._portal = SimpleNamespace(call=lambda coro, timeout: coro, close=lambda: None)
    broker._exit_stack = None
    broker._resolve_tools()
    return broker


CURRENT_TOOLS = ["get_accounts", "get_portfolio", "get_equity_positions", "get_equity_quotes",
                 "get_equity_orders", "place_equity_order", "cancel_equity_order",
                 "review_equity_order", "get_equity_tradability"]


def test_tool_discovery_binds_current_names(profiles):
    broker = make_broker(profiles, FakeMCP(CURRENT_TOOLS))
    assert broker.tools["place_order"] == "place_equity_order"
    assert broker.tools["quotes"] == "get_equity_quotes"
    assert broker.tools["accounts"] == "get_accounts"


def test_renamed_tools_still_bind_through_candidates(profiles):
    """Robinhood has renamed tools before; the candidate lists absorb a rename."""
    renamed = ["get_account", "get_portfolios", "get_positions", "get_quotes", "list_orders",
               "place_order", "cancel_order", "review_order"]
    broker = make_broker(profiles, FakeMCP(renamed, {"get_account": ACCOUNTS}))
    assert broker.tools["place_order"] == "place_order"
    assert broker.tools["orders"] == "list_orders"
    assert broker.tools["accounts"] == "get_account"
    assert broker.tools["review_order"] == "review_order"


def test_missing_capability_fails_closed_instead_of_being_assumed(profiles):
    with pytest.raises(BrokerUnavailable, match="does not expose a tool"):
        make_broker(profiles, FakeMCP(["get_accounts", "get_equity_quotes"]))


def test_agentic_account_is_identified_and_others_are_not_tradeable(profiles):
    broker = make_broker(profiles, FakeMCP(CURRENT_TOOLS, {"get_accounts": ACCOUNTS, "get_portfolio": PORTFOLIO}))
    assert broker.agentic_account()["account_number"] == "999"
    account = broker.get_account()
    assert account.account_id == "999" and account.equity == 100.0
    assert account.shorting_enabled is False and account.multiplier == "1"


def test_ambiguous_account_refuses_to_guess(profiles):
    rows = ACCOUNTS["data"]["accounts"]
    two_agentic = {"data": {"accounts": [dict(rows[1]), {**rows[1], "account_number": "998"}]}}
    broker = make_broker(profiles, FakeMCP(CURRENT_TOOLS, {"get_accounts": two_agentic}))
    with pytest.raises(BrokerUnavailable, match="exactly one account with agentic_allowed"):
        broker.agentic_account()


def test_positions_from_other_accounts_are_excluded(profiles):
    # get_equity_positions is account-scoped at the venue, so only the Agentic account's rows
    # come back; the filter is a second line of defence, not the only one.
    positions = {"data": {"positions": [
        {"symbol": "VTI", "quantity": "0.25", "average_buy_price": "376.00", "market_value": "94.00", "account_number": "999"},
        {"symbol": "AAPL", "quantity": "10", "average_buy_price": "200.00", "market_value": "2320.00", "account_number": "111"},
    ]}}
    broker = make_broker(profiles, FakeMCP(CURRENT_TOOLS,
                                           {"get_accounts": ACCOUNTS, "get_portfolio": PORTFOLIO,
                                            "get_equity_positions": positions}))
    symbols = [p.symbol for p in broker.get_positions()]
    assert symbols == ["VTI"], "only the Agentic account's positions are tradeable"


def test_quote_mapping_prefers_last_trade(profiles):
    quotes = {"data": {"quotes": [{"symbol": "VTI", "last_trade_price": "380.93", "bid_price": "380.90", "ask_price": "380.96"}]}}
    broker = make_broker(profiles, FakeMCP(CURRENT_TOOLS, {"get_accounts": ACCOUNTS, "get_equity_quotes": quotes}))
    q = broker.get_quote("VTI")
    assert q.price == 380.93 and q.source == "last_trade" and q.spread_pct is not None


def test_real_money_is_blocked_while_the_master_switch_is_off(profiles):
    broker = make_broker(profiles, FakeMCP(CURRENT_TOOLS, {"get_accounts": ACCOUNTS, "get_portfolio": PORTFOLIO}), enabled=False)
    with pytest.raises(BrokerError, match="enabled is false"):
        broker.submit_order(OrderRequest(symbol="VTI", side="buy", client_order_id="ct-x-1",
                                         notional=35.0, order_type="limit", limit_price=381.0))


def test_over_funded_account_halts_trading(profiles):
    rich_portfolio = {"data": {**PORTFOLIO["data"], "total_value": "5000", "cash": "5000"}}
    broker = make_broker(profiles, FakeMCP(CURRENT_TOOLS, {"get_accounts": ACCOUNTS,
                                                           "get_portfolio": rich_portfolio,
                                                           "get_equity_orders": EMPTY_ORDERS}), enabled=True)
    with pytest.raises(BrokerError, match="above the"):
        broker.submit_order(OrderRequest(symbol="VTI", side="buy", client_order_id="ct-x-1",
                                         notional=35.0, order_type="market"))


def test_order_submission_maps_and_prevents_duplicates(profiles):
    placed = {"data": {"orders": [{"id": "rh-1", "symbol": "VTI", "side": "buy", "state": "filled",
                                   "quantity": "0.0918", "cumulative_quantity": "0.0918",
                                   "average_price": "381.00", "type": "market"}]}}
    fake = FakeMCP(CURRENT_TOOLS, {"get_accounts": ACCOUNTS, "get_portfolio": PORTFOLIO,
                                   "get_equity_orders": EMPTY_ORDERS,
                                   "get_equity_tradability": {"data": {"tradability": [
                                       {"symbol": "VTI", "tradable": True, "fractional": True}]}},
                                   "review_equity_order": {"data": {"alerts": []}},
                                   "place_equity_order": placed})
    broker = make_broker(profiles, fake, enabled=True)
    order = broker.submit_order(OrderRequest(symbol="VTI", side="buy", client_order_id="ct-x-1",
                                             notional=35.0, order_type="market"))
    assert order.order_id == "rh-1" and order.status == "filled" and order.filled_quantity == 0.0918
    sent = dict(next(a for n, a in fake.calls if n == "place_equity_order"))
    assert sent["account_number"] == "999"
    # the venue's own parameter names and vocabulary, not ours
    assert sent["type"] == "market" and sent["time_in_force"] == "gfd"
    assert sent["market_hours"] == "regular_hours" and sent["dollar_amount"] == "35.00"
    ref_id = sent["ref_id"]
    assert len(ref_id) == 36, "ref_id must be a UUID, the venue's idempotency key"

    # the same logical order now exists at the venue, so a retry is refused
    fake._responses["get_equity_orders"] = {"data": {"orders": [
        {"id": "rh-1", "ref_id": ref_id, "symbol": "VTI", "side": "buy", "state": "filled",
         "quantity": "0.0918", "cumulative_quantity": "0.0918", "average_price": "381.00"}]}}
    with pytest.raises(DuplicateOrder):
        broker.submit_order(OrderRequest(symbol="VTI", side="buy", client_order_id="ct-x-1",
                                         notional=35.0, order_type="market"))


def test_venue_error_becomes_a_rejection_not_a_silent_pass(profiles):
    fake = FakeMCP(CURRENT_TOOLS, {"get_accounts": ACCOUNTS, "get_portfolio": PORTFOLIO,
                                   "get_equity_orders": EMPTY_ORDERS,
                                   "get_equity_tradability": {"data": {"tradability": [
                                       {"symbol": "VTI", "tradable": True, "fractional": True}]}}},
                   error_tools={"place_equity_order"})
    broker = make_broker(profiles, fake, enabled=True)
    with pytest.raises(Exception, match="venue said no"):
        broker.submit_order(OrderRequest(symbol="VTI", side="buy", client_order_id="ct-x-2",
                                         notional=35.0, order_type="market"))


def test_only_the_micro_live_bucket_reaches_the_real_money_venue(profiles):
    assert bucket_is_executable(profiles, "micro_live", "robinhood")[0] is True
    assert bucket_is_executable(profiles, "core", "robinhood")[0] is False
    assert bucket_is_executable(profiles, "sandbox", "robinhood")[0] is False
    assert bucket_is_executable(profiles, "micro_live", "simulated")[0] is False


def test_preflight_blocks_while_the_switch_is_off_and_lists_why(profiles):
    pf = run_preflight(profiles, broker=None)
    assert pf.ready is False
    ids = {c.check_id for c in pf.failures}
    assert "PF00" in ids and "PF01" in ids
    passed = {c.check_id for c in pf.checks if c.passed}
    for required in ("PF03", "PF04", "PF05", "PF06", "PF07", "PF09", "PF10", "PF11"):
        assert required in passed


def test_preflight_passes_every_gate_with_a_funded_agentic_account(profiles):
    fake = FakeMCP(CURRENT_TOOLS, {"get_accounts": ACCOUNTS, "get_portfolio": PORTFOLIO})
    broker = make_broker(profiles, fake, enabled=True)
    pf = run_preflight(profiles, broker)
    assert pf.ready is True, [c.detail for c in pf.failures]
    assert pf.account["equity"] == 100.0


def test_research_runtime_cannot_reach_a_trading_tool(profiles):
    pf = run_preflight(profiles, broker=None)
    check = next(c for c in pf.checks if c.check_id == "PF11")
    assert check.passed and "strict MCP config" in check.detail


def test_micro_live_uses_its_own_ledger(profiles):
    from conservative_trading.execution import _ledger_for

    assert _ledger_for(profiles, "micro_live").name == "micro_live_portfolio.yaml"
    assert _ledger_for(profiles, "core").name == "portfolio.yaml"


def test_end_to_end_micro_live_order_through_the_full_pipeline(profiles, tmp_path, monkeypatch):
    """A funded Micro-Live proposal: approval, revalidation, Hard Risk, preflight, then one order."""
    import yaml

    from conservative_trading.execution import execute
    from conservative_trading.proposals import ProposalStore
    from conservative_trading.stage3_scenarios import _proposal

    ledger = tmp_path / "micro_live_portfolio.yaml"
    ledger.write_text(yaml.safe_dump({
        "as_of": "2026-09-02", "currency": "USD",
        "buckets": {"micro_live": {"cash_usd": 100.0}}, "positions": [], "recent_opens": [],
    }))
    _isolate_state(profiles, tmp_path, ledger)

    placed = {"data": {"orders": [{"id": "rh-9", "symbol": "VTI", "side": "buy", "state": "filled",
                                   "quantity": "0.0929", "cumulative_quantity": "0.0929",
                                   "average_price": "378.39", "type": "market"}]}}
    fake = FakeMCP(CURRENT_TOOLS, {
        "get_accounts": ACCOUNTS, "get_portfolio": PORTFOLIO,
        "get_equity_positions": EMPTY_POSITIONS, "get_equity_quotes": QUOTES,
        "get_equity_orders": EMPTY_ORDERS, "review_equity_order": {"data": {"alerts": []}},
        "get_equity_tradability": {"data": {"tradability": [{"symbol": "VTI", "tradable": True, "fractional": True}]}},
        "place_equity_order": placed,
    })
    broker = make_broker(profiles, fake, enabled=True)
    from datetime import UTC, datetime

    from conservative_trading.broker.base import Clock
    from conservative_trading.scenarios import regime as fake_regime

    monkeypatch.setattr("conservative_trading.execution.assess_regime", lambda p, d: fake_regime("NORMAL"))
    monkeypatch.setattr(broker, "get_clock", lambda: Clock(is_open=True, timestamp=datetime.now(UTC)))

    proposal = _proposal(bucket="micro_live", notional=35.0, price=376.88, confidence=80)
    proposal.limit_class = "broad_diversified_etf"
    store = ProposalStore(tmp_path / "proposals.json")
    store.save(proposal)

    result = execute(profiles, proposal, broker, store, ledger_path=str(ledger))
    assert result.submitted is True, result.revalidation.get("reasons")
    assert result.order["order_id"] == "rh-9"
    sent = dict(next(a for n, a in fake.calls if n == "place_equity_order"))
    # market + dollar_amount + regular hours is what Robinhood requires for a fractional buy
    assert sent["account_number"] == "999" and sent["side"] == "buy"
    assert sent["type"] == "market" and sent["dollar_amount"] == "35.00"
    assert sent["time_in_force"] == "gfd" and sent["market_hours"] == "regular_hours"
    assert "ref_id" in sent
    assert any(n == "review_equity_order" for n, _ in fake.calls), "the venue pre-trade review must run first"
    updated = yaml.safe_load(ledger.read_text())
    assert updated["positions"][0]["symbol"] == "VTI"
    assert updated["buckets"]["micro_live"]["cash_usd"] < 100.0

    second = execute(profiles, store.get(proposal.proposal_id), broker, store, ledger_path=str(ledger))
    assert second.submitted is False and second.duplicate_prevented
    assert sum(1 for n, _ in fake.calls if n == "place_equity_order") == 1


def test_market_session_comes_from_the_exchange_calendar(profiles):
    """Holidays and half days come from exchange_calendars, not hand-maintained assumptions."""
    import pandas as pd

    from conservative_trading.broker.robinhood import _nyse_calendar

    cal = _nyse_calendar()
    assert cal.is_session(pd.Timestamp("2026-09-04")) is True        # an ordinary Friday
    assert cal.is_session(pd.Timestamp("2026-12-25")) is False       # Christmas
    assert cal.is_session(pd.Timestamp("2026-07-03")) is False       # observed Independence Day
    assert cal.is_session(pd.Timestamp("2026-09-05")) is False       # Saturday

    broker = make_broker(profiles, FakeMCP(CURRENT_TOOLS, {"get_accounts": ACCOUNTS}))
    clock = broker.get_clock()
    assert isinstance(clock.is_open, bool)
    assert clock.next_open is not None or clock.next_close is not None


def test_calendar_failure_reports_the_market_closed(profiles, monkeypatch):
    """An unavailable calendar must never permit an order."""
    import conservative_trading.broker.robinhood as R

    monkeypatch.setattr(R, "_nyse_calendar", lambda: (_ for _ in ()).throw(RuntimeError("no calendar")))
    broker = make_broker(profiles, FakeMCP(CURRENT_TOOLS, {"get_accounts": ACCOUNTS}))
    assert broker.get_clock().is_open is False


def _order_flow_broker(profiles, order_row, orders_after=None):
    fake = FakeMCP(CURRENT_TOOLS, {
        "get_accounts": ACCOUNTS, "get_portfolio": PORTFOLIO, "get_equity_positions": EMPTY_POSITIONS,
        "get_equity_quotes": QUOTES, "get_equity_orders": orders_after or EMPTY_ORDERS,
        "review_equity_order": {"data": {"alerts": []}},
        "get_equity_tradability": {"data": {"tradability": [{"symbol": "VTI", "tradable": True, "fractional": True}]}},
        "place_equity_order": {"data": {"orders": [order_row]}},
    })
    return make_broker(profiles, fake, enabled=True), fake


@pytest.mark.parametrize("state,filled,terminal", [
    ("queued", "0", False), ("confirmed", "0", False), ("partially_filled", "0.05", False),
    ("filled", "0.0929", True), ("cancelled", "0", True), ("rejected", "0", True),
])
def test_every_order_state_maps_and_terminality_is_correct(profiles, state, filled, terminal):
    row = {"id": "rh-s", "symbol": "VTI", "side": "buy", "state": state,
           "quantity": "0.0929", "cumulative_quantity": filled, "average_price": "378.39"}
    broker, _ = _order_flow_broker(profiles, row)
    order = broker._order(row)
    assert order.is_terminal is terminal
    assert order.filled_quantity == float(filled)


def test_partial_fill_reconciles_only_what_filled(profiles, tmp_path, monkeypatch):
    """A partially filled order updates the ledger with the filled part and stays flagged pending."""
    from datetime import UTC, datetime

    import yaml

    from conservative_trading.broker.base import Clock
    from conservative_trading.execution import execute
    from conservative_trading.proposals import ProposalStore
    from conservative_trading.scenarios import regime as fake_regime
    from conservative_trading.stage3_scenarios import _proposal

    ledger = tmp_path / "micro_live_portfolio.yaml"
    ledger.write_text(yaml.safe_dump({"as_of": "2026-09-02", "currency": "USD",
                                      "buckets": {"micro_live": {"cash_usd": 100.0}},
                                      "positions": [], "recent_opens": []}))
    _isolate_state(profiles, tmp_path, ledger)
    partial = {"id": "rh-p", "symbol": "VTI", "side": "buy", "state": "partially_filled",
               "quantity": "0.0929", "cumulative_quantity": "0.0400", "average_price": "378.39"}
    broker, _ = _order_flow_broker(profiles, partial,
                                   orders_after={"data": {"orders": []}})
    monkeypatch.setattr("conservative_trading.execution.assess_regime", lambda p, d: fake_regime("NORMAL"))
    monkeypatch.setattr(broker, "get_clock", lambda: Clock(is_open=True, timestamp=datetime.now(UTC)))
    monkeypatch.setattr(broker, "poll_order", lambda oid, **kw: broker._order(partial))

    proposal = _proposal(bucket="micro_live", notional=35.0, price=376.88, confidence=80)
    proposal.limit_class = "broad_diversified_etf"
    store = ProposalStore(tmp_path / "proposals.json")
    store.save(proposal)
    result = execute(profiles, proposal, broker, store, ledger_path=str(ledger))

    assert result.submitted is True
    assert result.order["status"] == "partially_filled"
    saved = store.get(proposal.proposal_id)
    assert any(h["event"] == "pending" for h in saved.history), "a partial fill must be flagged as pending"
    updated = yaml.safe_load(ledger.read_text())
    assert updated["positions"][0]["quantity"] == 0.04, "only the filled quantity is recorded"
    assert updated["buckets"]["micro_live"]["cash_usd"] == pytest.approx(100 - 0.04 * 378.39, abs=0.01)


def test_unacknowledged_risk_defaults_block_real_money(profiles):
    """Documented defaults are fine for research; a live order needs the investor's acceptance."""
    fake = FakeMCP(CURRENT_TOOLS, {"get_accounts": ACCOUNTS, "get_portfolio": PORTFOLIO})
    broker = make_broker(profiles, fake, enabled=True)
    profiles.accept_defaults = False
    profiles.risk_cfg("buckets", "core", "position_limits_pct_of_core", "individual_stock")  # touch a default

    pf = run_preflight(profiles, broker)
    gate = next(c for c in pf.checks if c.check_id == "PF13")
    assert gate.passed is False and "accept_all_recommended_defaults" in gate.detail
    assert pf.ready is False

    profiles.accept_defaults = True
    assert next(c for c in run_preflight(profiles, broker).checks if c.check_id == "PF13").passed is True
