"""Robinhood adapter and Micro-Live gates, tested offline against a fake MCP session.

No network, no OAuth, no real account. What is proven here: tool discovery and the refusal to
guess a missing capability, Agentic-account identification, response mapping, the capital
boundary, the real-money master switch, and the bucket-to-venue binding.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from agentic_trading.broker.base import (
    BrokerError,
    BrokerUnavailable,
    DuplicateOrder,
    OrderRequest,
)
from agentic_trading.broker.robinhood import (
    FileTokenStorage,
    RobinhoodBroker,
    _prime_refresh_endpoint,
)
from agentic_trading.micro_live import bucket_is_executable, run_preflight

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


def token(access="access", refresh="refresh", expires_in=3600):
    from mcp.shared.auth import OAuthToken

    return OAuthToken(access_token=access, refresh_token=refresh, expires_in=expires_in)


def test_token_storage_records_when_the_relative_lifetime_began(tmp_path):
    path = tmp_path / "token.json"
    storage = FileTokenStorage(path)
    before = datetime.now(UTC).timestamp()

    asyncio.run(storage.set_tokens(token(expires_in=3600)))

    saved = json.loads(path.read_text())
    assert before + 3599 <= saved["expires_at"] <= datetime.now(UTC).timestamp() + 3601
    assert asyncio.run(storage.get_tokens()).access_token == "access"


def test_expired_access_token_is_withheld_so_the_mcp_client_uses_its_refresh_token(tmp_path):
    path = tmp_path / "token.json"
    path.write_text(json.dumps({
        "tokens": token(expires_in=60).model_dump(mode="json"),
        "updated_at": (datetime.now(UTC) - timedelta(minutes=2)).isoformat(),
    }))

    restored = asyncio.run(FileTokenStorage(path).get_tokens())

    assert restored.access_token == ""
    assert restored.refresh_token == "refresh"
    # Reading an expired credential must not destroy the persisted token before the refresh
    # response arrives; a transient network failure should still be retryable later.
    assert json.loads(path.read_text())["tokens"]["access_token"] == "access"


def test_expired_token_discovers_robinhoods_real_refresh_endpoint_before_the_sdk_guesses(tmp_path):
    path = tmp_path / "token.json"
    path.write_text(json.dumps({
        "tokens": token(expires_in=60).model_dump(mode="json"),
        "updated_at": (datetime.now(UTC) - timedelta(minutes=2)).isoformat(),
    }))
    seen = []
    metadata = {
        "issuer": "https://agent.robinhood.com/mcp/trading",
        "authorization_endpoint": "https://robinhood.com/oauth",
        "token_endpoint": "https://api.robinhood.com/oauth2/token/",
        "response_types_supported": ["code"],
    }

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return metadata

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url):
            seen.append(url)
            return Response()

    httpx2 = SimpleNamespace(AsyncClient=lambda **_kwargs: Client())
    auth = SimpleNamespace(context=SimpleNamespace(oauth_metadata=None, auth_server_url=None))

    asyncio.run(_prime_refresh_endpoint(
        auth, FileTokenStorage(path), httpx2,
        "https://agent.robinhood.com/mcp/trading", 10))

    assert seen == ["https://agent.robinhood.com/.well-known/oauth-authorization-server/mcp/trading"]
    assert str(auth.context.oauth_metadata.token_endpoint) == metadata["token_endpoint"]


def quotes_now():
    """A quote stamped when it is asked for, never when this module was imported.

    execution._venue_price refuses a quote older than data_freshness.fast.max_age_seconds, so a
    timestamp fixed at import time turns every test that prices an order into a race against how
    long the rest of the suite takes. It lost at 953s against a 120s limit.
    """
    return {"data": {"quotes": [{"symbol": "VTI", "last_trade_price": "376.88",
                                 "bid_price": "376.86", "ask_price": "376.90",
                                 "last_trade_at": datetime.now(UTC).isoformat()}]}}


class FakeMCP:
    """Stands in for the Robinhood MCP session: same call surface, scripted answers."""

    def __init__(self, tools, responses=None, error_tools=(), error_text="venue said no"):
        self._tools = list(tools)
        self._responses = dict(responses or {})
        self._error_tools = set(error_tools)
        self._error_text = error_text
        self.calls: list[tuple[str, dict]] = []

    def list_tools(self):
        return SimpleNamespace(tools=[SimpleNamespace(name=t) for t in self._tools])

    def call_tool(self, name, arguments):
        self.calls.append((name, dict(arguments)))
        if name in self._error_tools:
            return SimpleNamespace(isError=True, content=[SimpleNamespace(text=self._error_text)],
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
    stamp = datetime.now(UTC).replace(microsecond=0)
    quotes = {"data": {"quotes": [{"symbol": "VTI", "last_trade_price": "380.93", "bid_price": "380.90", "ask_price": "380.96"}]}}
    quotes["data"]["quotes"][0]["last_trade_at"] = stamp.isoformat()
    broker = make_broker(profiles, FakeMCP(CURRENT_TOOLS, {"get_accounts": ACCOUNTS, "get_equity_quotes": quotes}))
    q = broker.get_quote("VTI")
    assert q.price == 380.93 and q.source == "last_trade" and q.spread_pct is not None
    assert q.as_of == stamp and q.received_at >= stamp


def test_quote_never_substitutes_local_receipt_time_for_missing_market_time(profiles):
    quotes = {"data": {"quotes": [{"symbol": "VTI", "last_trade_price": "380.93",
                                      "bid_price": "380.90", "ask_price": "380.96"}]}}
    broker = make_broker(profiles, FakeMCP(CURRENT_TOOLS, {"get_accounts": ACCOUNTS,
                                                           "get_equity_quotes": quotes}))
    q = broker.get_quote("VTI")
    assert q.as_of is None and q.received_at is not None


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
        broker.pre_submit_check(OrderRequest(symbol="VTI", side="buy", client_order_id="ct-x-1",
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

    # A retry carries the same ref_id, and the venue refusing it on that ground is a duplicate,
    # not a rejection of the idea. (Execution looks the id up before it ever gets this far.)
    fake._error_tools, fake._error_text = {"place_equity_order"}, "an order with this ref_id already exists"
    with pytest.raises(DuplicateOrder):
        broker.submit_order(OrderRequest(symbol="VTI", side="buy", client_order_id="ct-x-1",
                                         notional=35.0, order_type="market"))
    retried = [a for n, a in fake.calls if n == "place_equity_order"]
    assert len(retried) == 2 and retried[1]["ref_id"] == ref_id


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


TRADABLE = {"data": {"tradability": [{"symbol": "VTI", "tradable": True, "fractional": True}]}}


@pytest.mark.parametrize("tool", ["get_accounts", "get_equity_orders", "get_equity_tradability"])
def test_a_read_that_errors_is_unavailable_never_a_rejection(profiles, tool):
    """Execution closes a proposal for good on a BrokerRejection. A read answering with an error
    refused nothing, and used to be reported exactly like a refused order."""
    fake = FakeMCP(CURRENT_TOOLS, {"get_accounts": ACCOUNTS, "get_portfolio": PORTFOLIO,
                                   "get_equity_orders": EMPTY_ORDERS, "get_equity_tradability": TRADABLE},
                   error_tools={tool})
    broker = make_broker(profiles, fake, enabled=True)
    request = OrderRequest(symbol="VTI", side="buy", client_order_id="ct-x-3", notional=35.0, order_type="market")
    with pytest.raises(BrokerUnavailable, match="venue said no"):
        if tool == "get_equity_orders":
            broker.get_order_by_client_id(request.client_order_id)
        else:
            broker.pre_submit_check(request)


def test_placing_an_order_reads_nothing_from_the_venue_first(profiles):
    """Every read before placement now happens before the intent is recorded; one that failed
    after it could not be told apart from an order that may have been placed."""
    placed = {"data": {"orders": [{"id": "rh-2", "symbol": "VTI", "side": "buy", "state": "queued",
                                   "quantity": "0.0918", "cumulative_quantity": "0", "type": "market"}]}}
    fake = FakeMCP(CURRENT_TOOLS, {"get_accounts": ACCOUNTS, "get_portfolio": PORTFOLIO,
                                   "get_equity_tradability": TRADABLE, "place_equity_order": placed})
    broker = make_broker(profiles, fake, enabled=True)
    request = OrderRequest(symbol="VTI", side="buy", client_order_id="ct-x-4", notional=35.0,
                           order_type="market")
    broker.pre_submit_check(request)          # the order execution calls them in
    fake.calls.clear()
    broker.submit_order(request)
    assert [name for name, _ in fake.calls] == ["place_equity_order"]


def test_polling_follows_the_order_it_was_asked_about(profiles):
    rows = {"data": {"orders": [
        {"id": "rh-other", "symbol": "VTI", "side": "buy", "state": "filled", "quantity": "1",
         "cumulative_quantity": "1", "average_price": "370"},
        {"id": "rh-mine", "symbol": "VTI", "side": "buy", "state": "confirmed", "quantity": "0.09",
         "cumulative_quantity": "0"}]}}
    broker = make_broker(profiles, FakeMCP(CURRENT_TOOLS, {"get_accounts": ACCOUNTS,
                                                           "get_equity_orders": rows}), enabled=True)
    order = broker.poll_order("rh-mine", timeout_seconds=0)
    assert order.order_id == "rh-mine" and order.status == "accepted"
    with pytest.raises(BrokerUnavailable, match="no order"):
        broker.poll_order("rh-missing", timeout_seconds=0)


def _unreachable(_arguments):
    raise ConnectionError("connection reset by peer")


@pytest.mark.parametrize("failure", [
    pytest.param({"error_tools": {"get_equity_tradability"}}, id="tool answered with an error"),
    pytest.param({"responses": {"get_equity_tradability": _unreachable}}, id="call never answered"),
])
def test_a_venue_check_that_fails_before_placement_leaves_no_order_intent(profiles, tmp_path, monkeypatch,
                                                                          failure):
    """The tool-error case used to close the approved proposal as REJECTED; the transport case left
    it UNKNOWN with an intent recovery could never find at the venue, which in turn refused every
    later order for the account."""
    import yaml

    from agentic_trading.broker.base import Clock
    from agentic_trading.execution import execute, needs_reconciliation
    from agentic_trading.execution_scenarios import _proposal
    from agentic_trading.proposals import PROPOSED, ProposalStore
    from agentic_trading.risk_scenarios import regime as fake_regime

    ledger = tmp_path / "micro_live_portfolio.yaml"
    ledger.write_text(yaml.safe_dump({"as_of": "2026-09-02", "currency": "USD",
                                      "buckets": {"micro_live": {"cash_usd": 100.0}},
                                      "positions": [], "recent_opens": []}))
    _isolate_state(profiles, tmp_path, ledger)
    responses = {"get_accounts": ACCOUNTS, "get_portfolio": PORTFOLIO, "get_equity_positions": EMPTY_POSITIONS,
                 "get_equity_quotes": quotes_now(), "get_equity_orders": EMPTY_ORDERS,
                 "review_equity_order": {"data": {"alerts": []}}, "get_equity_tradability": TRADABLE,
                 "place_equity_order": {"data": {"orders": []}}}
    responses.update(failure.get("responses", {}))
    fake = FakeMCP(CURRENT_TOOLS, responses, error_tools=failure.get("error_tools", ()))
    broker = make_broker(profiles, fake, enabled=True)
    monkeypatch.setattr("agentic_trading.execution.assess_regime", lambda p, d: fake_regime("NORMAL"))
    monkeypatch.setattr(broker, "get_clock", lambda: Clock(is_open=True, timestamp=datetime.now(UTC)))

    proposal = _proposal(bucket="micro_live", notional=35.0, price=376.88, confidence=80)
    proposal.limit_class = "broad_diversified_etf"
    store = ProposalStore(tmp_path / "proposals.json")
    store.save(proposal)
    result = execute(profiles, proposal, broker, store, ledger_path=str(ledger))

    assert result.outcome == "PRE_SUBMIT_REFUSED" and not result.submitted
    assert not any(name == "place_equity_order" for name, _ in fake.calls)
    stored = store.get(proposal.proposal_id)
    assert stored.status == PROPOSED and stored.client_order_id is None
    assert not needs_reconciliation(stored)
    counts = store.db.status()["counts"]
    assert counts["orders"] == counts["reservations"] == 0


def test_only_the_micro_live_bucket_reaches_the_real_money_venue(profiles):
    assert bucket_is_executable(profiles, "micro_live", "robinhood")[0] is True
    assert bucket_is_executable(profiles, "core", "robinhood")[0] is False
    assert bucket_is_executable(profiles, "sandbox", "robinhood")[0] is False
    assert bucket_is_executable(profiles, "micro_live", "simulated")[0] is False


def test_preflight_blocks_while_the_switch_is_off_and_lists_why(profiles_with_state):
    # Stated here rather than read from the deployment: whether this machine is currently
    # trading real money is an operational decision, and this test is about the gate, not
    # about today's setting. PF10 reads a state directory for the same reason it takes a
    # copied one — see profiles_with_state.
    profiles = profiles_with_state
    profiles.risk["micro_live_execution"]["enabled"] = False
    pf = run_preflight(profiles, broker=None)
    assert pf.ready is False
    ids = {c.check_id for c in pf.failures}
    assert "PF00" in ids and "PF01" in ids
    passed = {c.check_id for c in pf.checks if c.passed}
    for required in ("PF03", "PF04", "PF05", "PF06", "PF07", "PF09", "PF10", "PF11"):
        assert required in passed


def test_preflight_passes_every_gate_with_a_funded_agentic_account(profiles_with_state):
    profiles = profiles_with_state
    fake = FakeMCP(CURRENT_TOOLS, {"get_accounts": ACCOUNTS, "get_portfolio": PORTFOLIO})
    broker = make_broker(profiles, fake, enabled=True)
    pf = run_preflight(profiles, broker)
    assert pf.ready is True, [c.detail for c in pf.failures]
    assert pf.account["equity"] == 100.0


def test_the_account_the_console_receives_is_the_one_preflight_builds(profiles):
    """MicroLiveAccount forbids extra fields, so a key added to the preflight account has to be
    added to the model too, or /micro-live starts answering 500 the moment a venue connects.

    That trade is deliberate — a dropped field would reach the screen as a silently missing
    number about real money — but it only holds if something exercises the connected path.
    Nothing else in the suite builds a funded account dict, so the guard lives here rather than
    beside the other response-shape tests, where the broker is always absent.
    """
    from agentic_trading.service.shapes import MicroLiveAccount

    fake = FakeMCP(CURRENT_TOOLS, {"get_accounts": ACCOUNTS, "get_portfolio": PORTFOLIO})
    pf = run_preflight(profiles, make_broker(profiles, fake, enabled=True))
    assert pf.account is not None

    account = MicroLiveAccount(**pf.account)
    assert set(account.model_dump()) == set(pf.account)
    assert account.equity == 100.0
    assert account.account_id_masked.startswith("••••")   # a mask, never a number


def test_research_runtime_cannot_reach_a_trading_tool(profiles):
    pf = run_preflight(profiles, broker=None)
    check = next(c for c in pf.checks if c.check_id == "PF11")
    assert check.passed and "strict MCP config" in check.detail


def test_micro_live_uses_its_own_ledger(profiles):
    from agentic_trading.execution import _ledger_for

    assert _ledger_for(profiles, "micro_live").name == "micro_live_portfolio.yaml"
    assert _ledger_for(profiles, "core").name == "portfolio.yaml"


def test_end_to_end_micro_live_order_through_the_full_pipeline(profiles, tmp_path, monkeypatch):
    """A funded Micro-Live proposal: approval, revalidation, Hard Risk, preflight, then one order."""
    import yaml

    from agentic_trading.execution import execute
    from agentic_trading.execution_scenarios import _proposal
    from agentic_trading.proposals import ProposalStore

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
        "get_equity_positions": EMPTY_POSITIONS, "get_equity_quotes": quotes_now(),
        "get_equity_orders": EMPTY_ORDERS, "review_equity_order": {"data": {"alerts": []}},
        "get_equity_tradability": {"data": {"tradability": [{"symbol": "VTI", "tradable": True, "fractional": True}]}},
        "place_equity_order": placed,
    })
    broker = make_broker(profiles, fake, enabled=True)
    from datetime import UTC, datetime

    from agentic_trading.broker.base import Clock
    from agentic_trading.risk_scenarios import regime as fake_regime

    monkeypatch.setattr("agentic_trading.execution.assess_regime", lambda p, d: fake_regime("NORMAL"))
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
    from agentic_trading.portfolio import load_portfolio

    updated = load_portfolio(ledger)
    assert updated.positions[0].symbol == "VTI"
    assert updated.buckets["micro_live"].cash_usd < 100.0

    second = execute(profiles, store.get(proposal.proposal_id), broker, store, ledger_path=str(ledger))
    assert second.submitted is False and second.duplicate_prevented
    assert sum(1 for n, _ in fake.calls if n == "place_equity_order") == 1


def test_market_session_comes_from_the_exchange_calendar(profiles):
    """Holidays and half days come from exchange_calendars, not hand-maintained assumptions."""
    import pandas as pd

    from agentic_trading.broker.robinhood import _nyse_calendar

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
    import agentic_trading.broker.robinhood as R

    monkeypatch.setattr(R, "_nyse_calendar", lambda: (_ for _ in ()).throw(RuntimeError("no calendar")))
    broker = make_broker(profiles, FakeMCP(CURRENT_TOOLS, {"get_accounts": ACCOUNTS}))
    assert broker.get_clock().is_open is False


def _order_flow_broker(profiles, order_row, orders_after=None):
    fake = FakeMCP(CURRENT_TOOLS, {
        "get_accounts": ACCOUNTS, "get_portfolio": PORTFOLIO, "get_equity_positions": EMPTY_POSITIONS,
        "get_equity_quotes": quotes_now(), "get_equity_orders": orders_after or EMPTY_ORDERS,
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

    from agentic_trading.broker.base import Clock
    from agentic_trading.execution import execute
    from agentic_trading.execution_scenarios import _proposal
    from agentic_trading.proposals import ProposalStore
    from agentic_trading.risk_scenarios import regime as fake_regime

    ledger = tmp_path / "micro_live_portfolio.yaml"
    ledger.write_text(yaml.safe_dump({"as_of": "2026-09-02", "currency": "USD",
                                      "buckets": {"micro_live": {"cash_usd": 100.0}},
                                      "positions": [], "recent_opens": []}))
    _isolate_state(profiles, tmp_path, ledger)
    partial = {"id": "rh-p", "symbol": "VTI", "side": "buy", "state": "partially_filled",
               "quantity": "0.0929", "cumulative_quantity": "0.0400", "average_price": "378.39"}
    broker, _ = _order_flow_broker(profiles, partial,
                                   orders_after={"data": {"orders": []}})
    monkeypatch.setattr("agentic_trading.execution.assess_regime", lambda p, d: fake_regime("NORMAL"))
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
    from agentic_trading.portfolio import load_portfolio

    updated = load_portfolio(ledger)
    assert updated.positions[0].quantity == 0.04, "only the filled quantity is recorded"
    assert updated.buckets["micro_live"].cash_usd == pytest.approx(100 - 0.04 * 378.39, abs=0.01)


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


def test_closing_a_portal_gives_its_descriptors_back():
    """A stopped event loop keeps its kqueue and self-pipe; only close() hands them back.

    Every Robinhood session opens one portal, so while close() merely stopped the loop the
    API leaked three descriptors per session. On 2026-09-15 that ended with sqlite unable to
    open the state database at all, which is why this counts descriptors rather than asserting
    the call was made: the call was always made, it just did not release anything.
    """
    import os

    from agentic_trading.broker.robinhood import _Portal

    def open_descriptors() -> int:
        return len(os.listdir("/dev/fd"))

    baseline = open_descriptors()
    for _ in range(8):
        portal = _Portal()
        portal.close()
        assert portal._loop.is_closed()
    # Without the fix these eight portals leak twenty-four descriptors. The small allowance
    # covers the directory handle the count itself opens.
    assert open_descriptors() <= baseline + 2


def _expired_token_file(path, *, client=True):
    from mcp.shared.auth import OAuthClientInformationFull

    stored = {
        "tokens": token(access="old", refresh="refresh-me", expires_in=60).model_dump(mode="json"),
        "updated_at": (datetime.now(UTC) - timedelta(minutes=2)).isoformat(),
    }
    if client:
        stored["client"] = OAuthClientInformationFull(
            client_id="cid", redirect_uris=["http://localhost:8765/callback"],
            grant_types=["authorization_code", "refresh_token"], response_types=["code"],
            token_endpoint_auth_method="none").model_dump(mode="json")
    path.write_text(json.dumps(stored))
    return FileTokenStorage(path)


def _provider(storage, server):
    from mcp.client.auth import OAuthClientProvider
    from mcp.shared.auth import OAuthClientMetadata

    # No redirect or callback handler, exactly as the unattended service builds it: reaching the
    # browser flow raises instead of hanging, which is what a scheduled run would hit.
    return OAuthClientProvider(
        server_url=server, storage=storage, redirect_handler=None, callback_handler=None,
        client_metadata=OAuthClientMetadata(
            client_name="agentic-trading (test)",
            redirect_uris=["http://localhost:8765/callback"],
            grant_types=["authorization_code", "refresh_token"], response_types=["code"],
            token_endpoint_auth_method="none"))


def _metadata_module(metadata, seen):
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return metadata

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url):
            seen.append(url)
            return Response()

    return SimpleNamespace(AsyncClient=lambda **_kwargs: Client())


def test_an_expired_token_refreshes_itself_without_a_browser(tmp_path):
    """The whole unattended path, end to end and offline: the case Friday 09:02 will hit.

    The access token expires about every 3.3 days while research runs weekly, so a scheduled
    run meets an expired credential more often than a fresh one. On 2026-09-14 that ended in
    `OAuthFlowError: No redirect handler provided for authorization code grant`, fifteen hours
    of disconnection and a descriptor leak on every retry. The pieces are each covered above;
    this asserts they combine into a refresh rather than a browser.
    """
    import httpx2

    server = "https://agent.robinhood.test/mcp/trading"
    endpoint = "https://api.robinhood.test/oauth2/token/"
    storage = _expired_token_file(tmp_path / "token.json")
    auth = _provider(storage, server)
    seen: list[str] = []

    async def drive():
        await _prime_refresh_endpoint(
            auth, storage,
            _metadata_module({"issuer": server, "authorization_endpoint": "https://rh.test/oauth",
                              "token_endpoint": endpoint, "response_types_supported": ["code"]},
                             seen),
            server, 10)
        flow = auth.async_auth_flow(httpx2.Request("POST", server))
        first = await flow.__anext__()
        refreshed = httpx2.Response(
            200, request=first,
            json={"access_token": "fresh", "token_type": "Bearer", "expires_in": 3600,
                  "refresh_token": "refresh-me"})
        retried = await flow.asend(refreshed)
        await flow.aclose()
        return first, retried

    first, retried = asyncio.run(drive())

    # The advertised endpoint, not the origin-relative /token the SDK guesses without metadata.
    assert first.method == "POST" and str(first.url) == endpoint
    assert b"grant_type=refresh_token" in first.content
    # The original call is then retried with the new credential, and the new one is persisted.
    assert retried.headers["authorization"] == "Bearer fresh"
    assert json.loads((tmp_path / "token.json").read_text())["tokens"]["access_token"] == "fresh"


def test_without_the_discovered_endpoint_the_refresh_is_aimed_at_a_guess(tmp_path):
    """Why the priming exists: unprimed, the SDK posts the refresh where Robinhood has nothing."""
    import httpx2

    server = "https://agent.robinhood.test/mcp/trading"
    storage = _expired_token_file(tmp_path / "token.json")
    auth = _provider(storage, server)

    async def drive():
        flow = auth.async_auth_flow(httpx2.Request("POST", server))
        first = await flow.__anext__()
        await flow.aclose()
        return first

    first = asyncio.run(drive())
    assert str(first.url) == "https://agent.robinhood.test/token"


def test_the_acknowledgement_gate_counts_what_the_profile_scan_found(profiles):
    """PF13 must read the same fact the console reads, or the two describe opposite systems.

    It counted defaults_used, which fills in as values are read and is therefore empty when
    preflight runs. Over a profile carrying 68 unresolved fields the gate announced "all risk
    parameters are explicit values" and passed, while the console announced that real money was
    blocked. Neither sentence was true.
    """
    fake = FakeMCP(CURRENT_TOOLS, {"get_accounts": ACCOUNTS, "get_portfolio": PORTFOLIO})
    broker = make_broker(profiles, fake, enabled=True)
    profiles.unresolved = ["risk_profile.buckets.micro_live.position_limits.max_open_positions"]
    profiles.defaults_used = []          # nothing read yet: exactly the state preflight sees

    profiles.accept_defaults = False
    gate = next(c for c in run_preflight(profiles, broker).checks if c.check_id == "PF13")
    assert gate.passed is False and "accept_all_recommended_defaults" in gate.detail

    profiles.accept_defaults = True
    accepted = next(c for c in run_preflight(profiles, broker).checks if c.check_id == "PF13")
    assert accepted.passed is True and "accepted via" in accepted.detail

    profiles.unresolved = []
    explicit = next(c for c in run_preflight(profiles, broker).checks if c.check_id == "PF13")
    assert explicit.passed is True and explicit.detail == "all risk parameters are explicit values"


# ---------------------------------------------------------------- the session kept for reads
def test_a_transport_failure_retires_the_session_and_a_tool_error_does_not(profiles):
    """How a kept read session knows to replace itself: the venue answering with an error says
    nothing about the session, a call that never got an answer says it cannot be trusted."""
    class Dropping(FakeMCP):
        def call_tool(self, name, arguments):
            if name == "get_equity_positions":
                raise ConnectionError("connection reset by peer")
            return super().call_tool(name, arguments)

    fake = Dropping(CURRENT_TOOLS, {"get_accounts": ACCOUNTS, "get_portfolio": PORTFOLIO},
                    error_tools={"get_equity_orders"})
    broker = make_broker(profiles, fake, enabled=True)
    assert broker.usable
    with pytest.raises(BrokerUnavailable, match="returned an error"):
        broker.get_open_orders()
    assert broker.usable
    with pytest.raises(BrokerUnavailable, match="ConnectionError"):
        broker.get_positions()
    assert not broker.usable

    broker = make_broker(profiles, FakeMCP(CURRENT_TOOLS, {"get_accounts": ACCOUNTS}), enabled=True)

    def timed_out(coro, timeout):
        raise TimeoutError

    broker._portal = SimpleNamespace(call=timed_out, close=lambda: None)
    with pytest.raises(BrokerUnavailable, match="timed out"):
        broker.get_account()
    assert not broker.usable


def test_a_session_whose_tools_cannot_be_bound_is_closed_not_abandoned(profiles, monkeypatch):
    """A kept read session reconnects on the next read after a failure, so a venue missing a
    capability must not leave a portal thread and its descriptors behind on every poll."""
    from agentic_trading.broker import robinhood

    closed = []

    class Portal:
        def call(self, coro, timeout):
            if asyncio.iscoroutine(coro):
                coro.close()            # _aconnect; the session is put in place below instead
                return None
            return coro

        def close(self):
            closed.append(True)

    monkeypatch.setattr(robinhood, "_Portal", Portal)
    broker = RobinhoodBroker(profiles, connect=False)
    broker.storage = SimpleNamespace(connected=True)
    broker._session = FakeMCP(["get_accounts"])
    with pytest.raises(BrokerUnavailable, match="does not expose a tool"):
        broker.connect()
    assert closed == [True] and not broker.usable


def test_the_session_is_renewed_exactly_when_a_new_one_would_refresh_the_token(profiles, tmp_path):
    """Kept past this moment a session carries a token the MCP client will not refresh; opened
    before it, a new session would carry the same old token and change nothing."""
    storage = FileTokenStorage(tmp_path / "token.json")
    asyncio.run(storage.set_tokens(token(expires_in=3600)))
    broker = RobinhoodBroker(profiles, connect=False)
    broker.storage = storage
    stored = json.loads((tmp_path / "token.json").read_text())
    assert broker.credential_deadline() == stored["expires_at"] - 60
    assert asyncio.run(storage.get_tokens()).access_token == "access"

    # Inside that last minute the stored token is withheld, so a session opened then refreshes.
    stored["expires_at"] = datetime.now(UTC).timestamp() + 59
    (tmp_path / "token.json").write_text(json.dumps(stored))
    assert datetime.now(UTC).timestamp() >= broker.credential_deadline()
    assert asyncio.run(storage.get_tokens()).access_token == ""

    broker.storage = FileTokenStorage(tmp_path / "missing.json")
    assert broker.credential_deadline() is None


def test_micro_live_status_is_read_through_one_kept_session(profiles_with_state, tmp_path, monkeypatch):
    """Every screen showing the real-money account polls /micro-live. Each poll used to open a
    Robinhood session and close it again, three to thirteen seconds apiece. Now a poll is
    answered from a reading of the account, and the reading is renewed through a kept session."""
    from agentic_trading.broker import robinhood
    from agentic_trading.proposals import ProposalStore
    from agentic_trading.service import Service

    profiles = profiles_with_state
    profiles.risk["micro_live_execution"]["enabled"] = True
    fake = FakeMCP(CURRENT_TOOLS, {"get_accounts": ACCOUNTS, "get_portfolio": PORTFOLIO})
    opened = []

    def connect(p):
        opened.append(True)
        return make_broker(p, fake, enabled=p.risk["micro_live_execution"]["enabled"])

    monkeypatch.setattr(robinhood, "RobinhoodBroker", connect)
    service = Service()
    monkeypatch.setattr(service, "profiles", lambda: profiles)
    monkeypatch.setattr(service, "_store", lambda _: ProposalStore(tmp_path / "proposals.json"))

    first, second = service.micro_live(), service.micro_live()
    assert first.connected and second.connected
    assert first.account.equity == second.account.equity == 100.0
    assert second.checked_at == first.checked_at
    assert len(opened) == 1
    assert [name for name, _ in fake.calls].count("get_portfolio") == 1

    # Once the reading is due it is renewed, through the session that is already open.
    later = time.monotonic() + 61
    monkeypatch.setattr(service._venue_reading, "_clock", lambda: later)
    assert service._venue_reading.refresh() is True
    assert len(opened) == 1
    assert [name for name, _ in fake.calls].count("get_portfolio") == 2

    # Switching real money off replaces the session rather than reading on under the old config.
    profiles.risk["micro_live_execution"]["enabled"] = False
    third = service.micro_live()
    assert third.enabled is False and len(opened) == 2

    service.close()
    assert not service._venue_reads.is_open


def test_a_venue_that_cannot_be_reached_still_reads_as_disconnected(profiles_with_state, tmp_path,
                                                                     monkeypatch):
    from agentic_trading.broker import robinhood
    from agentic_trading.proposals import ProposalStore
    from agentic_trading.service import Service

    def refuse(_profiles):
        raise robinhood.RobinhoodNotConnected("no Robinhood OAuth token. Run `uv run ct-robinhood connect`")

    monkeypatch.setattr(robinhood, "RobinhoodBroker", refuse)
    service = Service()
    monkeypatch.setattr(service, "profiles", lambda: profiles_with_state)
    monkeypatch.setattr(service, "_store", lambda _: ProposalStore(tmp_path / "proposals.json"))
    status = service.micro_live()
    assert status.connected is False and status.account is None
    assert status.connect_error.startswith("no Robinhood OAuth token")
    assert "PF01" in status.blocking


def test_probe_prints_a_read_tools_answer_verbatim_and_refuses_anything_that_acts(profiles, monkeypatch, capsys):
    """Most of the venue's tools have never been called from this code. Before any of them is
    parsed, its real answer is looked at once through ``ct-robinhood probe`` -- which must be
    unable to send a name that places, cancels or edits, however it is spelled."""
    from agentic_trading import robinhood_cli

    fake = FakeMCP([*CURRENT_TOOLS, "get_equity_news"],
                   {"get_equity_news": {"data": {"news": [{"headline": "Apple reports"}]}}})
    broker = make_broker(profiles, fake)
    monkeypatch.setattr("agentic_trading.broker.robinhood.RobinhoodBroker", lambda p: broker)
    config = ["--config-dir", str(profiles.config_dir)]

    assert robinhood_cli.main(["probe", "get_equity_news", "--args", '{"symbols": ["AAPL"]}', *config]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["payload"] == {"data": {"news": [{"headline": "Apple reports"}]}} and out["is_error"] is False
    assert fake.calls == [("get_equity_news", {"symbols": ["AAPL"]})]

    monkeypatch.setattr("agentic_trading.broker.robinhood.RobinhoodBroker",
                        lambda p: pytest.fail("a refused name must not open a session"))
    for name in ("place_equity_order", "cancel_equity_order", "create_alert", "update_watchlist",
                 "exercise_option", "get_accounts; place_equity_order"):
        assert robinhood_cli.main(["probe", name, *config]) == 2
        assert "refused" in capsys.readouterr().out
    assert robinhood_cli.main(["probe", "get_accounts", "--args", "[1]", *config]) == 2
    assert robinhood_cli.main(["probe", *config]) == 2
    assert fake.calls == [("get_equity_news", {"symbols": ["AAPL"]})]


def _kept_micro_live(profiles, tmp_path, monkeypatch, fake):
    from agentic_trading.broker import robinhood
    from agentic_trading.proposals import ProposalStore
    from agentic_trading.service import Service

    opened = []

    def connect(p):
        opened.append(True)
        return make_broker(p, fake, enabled=p.risk["micro_live_execution"]["enabled"])

    monkeypatch.setattr(robinhood, "RobinhoodBroker", connect)
    service = Service()
    monkeypatch.setattr(service, "profiles", lambda: profiles)
    monkeypatch.setattr(service, "_store", lambda _: ProposalStore(tmp_path / "proposals.json"))
    return service, opened


def test_only_the_venues_answer_is_kept_and_the_configuration_gates_stay_live(
        profiles_with_state, tmp_path, monkeypatch):
    """Accepting the documented risk defaults shows on the next poll, not a reading later."""
    profiles = profiles_with_state
    profiles.risk["micro_live_execution"]["enabled"] = True
    profiles.risk_cfg("buckets", "core", "position_limits_pct_of_core", "individual_stock")
    fake = FakeMCP(CURRENT_TOOLS, {"get_accounts": ACCOUNTS, "get_portfolio": PORTFOLIO})
    service, opened = _kept_micro_live(profiles, tmp_path, monkeypatch, fake)

    profiles.accept_defaults = False
    before = service.micro_live()
    assert "PF13" in before.blocking and before.preflight_ready is False
    profiles.accept_defaults = True
    after = service.micro_live()
    assert "PF13" not in after.blocking
    assert after.checked_at == before.checked_at
    assert len(opened) == 1 and [n for n, _ in fake.calls].count("get_portfolio") == 1


def test_an_account_the_venue_would_not_read_is_reported_from_the_reading(
        profiles_with_state, tmp_path, monkeypatch):
    profiles = profiles_with_state
    profiles.risk["micro_live_execution"]["enabled"] = True
    fake = FakeMCP(CURRENT_TOOLS, {"get_accounts": ACCOUNTS}, error_tools={"get_portfolio"})
    service, _opened = _kept_micro_live(profiles, tmp_path, monkeypatch, fake)

    def depth(tb):
        return 0 if tb is None else 1 + depth(tb.tb_next)

    service.micro_live()
    kept = service._venue_reading.get().account_error
    frames = depth(kept.__traceback__)
    for _ in range(3):
        status = service.micro_live()
        gate = next(c for c in status.checks if c.id == "PF01")
        assert status.connected and status.account is None
        assert gate.passed is False and "returned an error" in gate.detail
    assert [n for n, _ in fake.calls].count("get_portfolio") == 1
    # Raised from a copy each time: the kept error served to every poll gathers no tracebacks.
    assert depth(kept.__traceback__) == frames


def test_the_agentic_holdings_are_valued_at_live_quotes_on_the_kept_reading(
        profiles_with_state, tmp_path, monkeypatch):
    """The Assets screen shows the real-money account as the venue has it now, not as the ledger
    was priced at a close. On 2026-09-23 a vendor's missing bar blanked the ledger's figures for
    a day while the account itself was plainly readable. The holdings ride the reading the
    account is read in, and a quote that cannot be read leaves a gap, never the cost."""
    profiles = profiles_with_state
    profiles.risk["micro_live_execution"]["enabled"] = True
    positions = {"data": {"positions": [
        {"symbol": "BND", "quantity": "0.5", "average_buy_price": "71.00", "account_number": "999"},
        {"symbol": "VTI", "quantity": "0.25", "average_buy_price": "376.00", "account_number": "999"},
    ]}}

    def quotes(arguments):
        return quotes_now() if arguments["symbols"] == ["VTI"] else {"data": {"quotes": []}}

    fake = FakeMCP(CURRENT_TOOLS, {"get_accounts": ACCOUNTS, "get_portfolio": PORTFOLIO,
                                   "get_equity_positions": positions, "get_equity_quotes": quotes})
    service, opened = _kept_micro_live(profiles, tmp_path, monkeypatch, fake)

    status = service.micro_live()
    assert status.account.equity == 100.0
    vti, bnd = status.holdings                                   # priced first, unpriced last
    assert (vti.symbol, vti.quantity, vti.average_cost, vti.price) == ("VTI", 0.25, 376.0, 376.88)
    assert vti.market_value_usd == round(0.25 * 376.88, 2)
    assert vti.unrealized_pct == round((376.88 / 376.0 - 1) * 100, 2) and vti.price_at
    assert (bnd.symbol, bnd.average_cost) == ("BND", 71.0)
    assert (bnd.price, bnd.market_value_usd, bnd.unrealized_pct) == (None, None, None)
    assert status.holdings_error == "no usable quote for BND"

    # One reading, one session: a second poll inside the minute asks the venue nothing new.
    service.micro_live()
    assert len(opened) == 1
    assert [n for n, _ in fake.calls].count("get_equity_quotes") == 2


def test_holdings_the_venue_would_not_list_are_absent_not_empty(profiles_with_state, tmp_path,
                                                                monkeypatch):
    profiles = profiles_with_state
    profiles.risk["micro_live_execution"]["enabled"] = True
    fake = FakeMCP(CURRENT_TOOLS, {"get_accounts": ACCOUNTS, "get_portfolio": PORTFOLIO},
                   error_tools={"get_equity_positions"})
    service, _opened = _kept_micro_live(profiles, tmp_path, monkeypatch, fake)
    status = service.micro_live()
    assert status.account.equity == 100.0, "the account is still read and still gates preflight"
    assert status.holdings is None and status.holdings_error
    assert [n for n, _ in fake.calls].count("get_equity_quotes") == 0

    empty = FakeMCP(CURRENT_TOOLS, {"get_accounts": ACCOUNTS, "get_portfolio": PORTFOLIO,
                                    "get_equity_positions": EMPTY_POSITIONS})
    service, _opened = _kept_micro_live(profiles, tmp_path, monkeypatch, empty)
    status = service.micro_live()
    assert status.holdings == [] and status.holdings_error is None
