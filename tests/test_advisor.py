"""Read-only account boundaries, sourced candidates, durable chat and reuse of full research."""
import json
import uuid
from types import SimpleNamespace

import pytest

from agentic_trading.advisor import (
    NOTES,
    READ_TOOLS,
    SNAPSHOT,
    AssistantReply,
    ObservationBroker,
    answer,
    fetch_snapshot,
    now,
    profile_context,
    read_only_research,
    run_task,
)
from agentic_trading.broker.base import BrokerUnavailable
from agentic_trading.broker.robinhood import RobinhoodBroker
from agentic_trading.records import write_record
from agentic_trading.research_jobs import ResearchJobManager
from agentic_trading.service import ServiceConflict, ServiceError
from tests.test_api import sandbox_service  # noqa: F401 - shared isolated fixture
from tests.test_robinhood import FakeMCP


def observation(profiles, responses):
    broker = ObservationBroker.__new__(ObservationBroker)
    RobinhoodBroker.__init__(broker, profiles, connect=False)
    broker.tool_candidates = READ_TOOLS
    broker.required = ["accounts", "positions", "portfolio", "quotes"]
    broker._session = FakeMCP([names[0] for names in READ_TOOLS.values()], responses)
    broker._portal = SimpleNamespace(call=lambda coro, timeout: coro, close=lambda: None)
    broker._resolve_tools()
    return broker


def snapshot():
    return {"fetched_at": now(), "complete": True, "errors": [], "source": "test",
            "accounts": [{"id": "a" * 20, "label": "••••1234", "complete": True,
                          "cash_usd": 900, "equity_usd": 1000,
                          # quote_at and weight_pct are written by the pricing pass, not by the
                          # construction above, and every stored position has them.
                          "positions": [{"symbol": "VTI", "quantity": 1, "average_cost": 90,
                                         "market_value_usd": 100, "price": 100,
                                         "quote_at": "2026-09-14T19:59:59Z", "weight_pct": 10.0}]}],
            "candidates": [{"symbol": "VTI", "sources": ["watchlists:My list"]}]}


def test_all_accounts_read_without_trade_permissions_and_with_pagination(profiles):
    responses = {
        "get_accounts": {"data": {"accounts": [{"account_number": "secret1234"},
                                                {"account_number": "secret5678", "agentic_allowed": True}]}},
        "get_portfolio": {"data": {"total_value": "1000", "cash": "800"}},
        "get_equity_positions": lambda args: {"data": {
            "positions": [{"symbol": "VTI" if not args.get("cursor") else "BND",
                           "quantity": "1", "average_buy_price": "80"}],
            "next": None if args.get("cursor") else "https://broker.test/page?cursor=page2"}},
        "get_equity_quotes": {"data": {"results": [{"quote": {"symbol": s, "last_trade_price": "100",
                                                      "venue_last_trade_time": now()}} for s in ("VTI", "BND")]}},
        "get_watchlists": {"data": {"watchlists": [{"id": "list1", "display_name": "My picks"}]}},
        "get_watchlist_items": {"data": {"items": [{"symbol": "VTI", "object_type": "instrument"},
                                                    {"symbol": "BTC", "object_type": "currency_pair"}]}},
        "get_scans": {"data": {"scans": [{"id": "scan1", "title": "Profitable"}]}},
        "run_scan": {"data": {"instruments": [{"ticker": "MSFT"}, {"ticker": "VTI"}]}},
    }
    broker = observation(profiles, responses)
    data = fetch_snapshot(profiles, broker_factory=lambda p: broker)
    assert data["complete"]
    assert len(data["accounts"]) == 2
    assert len(data["accounts"][0]["positions"]) == 2
    assert data["accounts"][0]["positions"][0]["unrealized_pct"] == 25
    symbols = [c["symbol"] for c in data["candidates"]]
    assert symbols.count("VTI") == 1 and "MSFT" in symbols and "BTC" not in symbols
    assert "secret1234" not in json.dumps(data)
    vti = next(c for c in data["candidates"] if c["symbol"] == "VTI")
    assert "scans:Profitable" in vti["sources"] and "watchlists:My picks" in vti["sources"]
    assert {name for name, _ in broker._session.calls} <= {ns[0] for ns in READ_TOOLS.values()}
    with pytest.raises(BrokerUnavailable, match="only supports reading"):
        broker._call("place_order", {})


def test_missing_data_is_not_a_zero_balance_and_foreign_rows_are_rejected(profiles):
    broker = observation(profiles, {
        "get_accounts": {"data": {"accounts": [{"account_number": "secret1234"}]}},
        "get_portfolio": {"data": {"total_value": "nan"}},
        "get_equity_positions": {"data": {"positions": [{"account_number": "wrong-account",
                                                            "symbol": "VTI", "quantity": "1"}]}},
    })
    data = fetch_snapshot(profiles, broker_factory=lambda p: broker)
    assert not data["complete"] and data["errors"]
    account = data["accounts"][0]
    assert not account["complete"] and account["equity_usd"] is None and account["cash_usd"] is None
    assert not account["positions"]


@pytest.fixture
def assistant_service(request, monkeypatch):
    service = request.getfixturevalue("sandbox_service")
    monkeypatch.setattr(ResearchJobManager, "dispatch", lambda self: [])
    service.state_db().write_meta(SNAPSHOT, snapshot())
    return service


def test_chat_request_is_idempotent_and_conversations_are_serial(assistant_service):
    s = assistant_service
    request_id = str(uuid.uuid4())
    first = s.start_advisor(kind="chat", request_id=request_id, message="Explain my VTI position")
    again = s.start_advisor(kind="chat", request_id=request_id, message="Explain my VTI position")
    assert first["run_key"] == again["run_key"] and again["deduplicated"]
    with pytest.raises(ServiceConflict):
        s.start_advisor(kind="chat", request_id=request_id, message="A different request")
    with pytest.raises(ServiceConflict, match="still answering"):
        s.start_advisor(kind="chat", request_id=str(uuid.uuid4()), message="Another question")
    second = s.start_advisor(kind="chat", request_id=str(uuid.uuid4()), message="Different conversation",
                            conversation_id="other")
    assert second["run_key"] != first["run_key"]


def test_the_assistant_screen_answers_before_any_refresh_has_run(assistant_service):
    """Cold start: nothing observed yet, and the screen still has to render.

    The snapshot is a stored document that does not exist until the first refresh succeeds —
    which is exactly what an empty meta document reads back as. Requiring any of its fields
    would make the assistant's own screen 500 on a fresh deployment, the one moment the owner
    is most likely to be looking at it.
    """
    from agentic_trading.advisor import SNAPSHOT

    assistant_service.state_db().write_meta(SNAPSHOT, {})

    state = assistant_service.advisor_state()

    assert state.snapshot.fetched_at is None
    assert state.snapshot.accounts == [] and state.snapshot.errors == []
    assert state.stale is True                      # never fetched reads as stale, not as fresh
    assert state.notes.revision == 0 and state.notes.text == ""
    assert state.jobs == [] and state.decisions == []
    assert state.coverage                            # the scope disclosure is never conditional


def test_a_stored_snapshot_keeps_keys_this_version_never_declared(assistant_service):
    """AdvisorSnapshot allows extras, and the fixture above is why.

    The snapshot is written whole by the refresh task and read back later, so it carries
    whatever the version that wrote it chose to put there — the test fixture itself has a
    top-level `complete` no model declares. The task runner also splices `stale` and an
    explanation into a snapshot it could not refresh, rather than discarding the last good
    read. Forbidding extras would quietly drop both.
    """
    state = assistant_service.advisor_state()

    assert state.snapshot.model_extra.get("complete") is True    # undeclared, and still here
    assert state.snapshot.source == "test"
    account = state.snapshot.accounts[0]
    assert account.label == "••••1234" and account.positions[0].symbol == "VTI"
    assert account.positions[0].unrealized_pct is None           # absent, not computed to zero
    assert [c.symbol for c in state.snapshot.candidates] == ["VTI"]


def test_a_priced_holding_keeps_the_fields_the_pricing_pass_wrote(assistant_service):
    """The holding as it is actually stored, not as fetch_snapshot first builds it.

    Positions are created without a price and enriched afterwards by the quote pass, which adds
    quote_at and weight_pct. Every stored position therefore carries them and no fixture here
    did, so a model that forbade extras passed this suite and would have answered the assistant
    screen — polled every five seconds — with a 500 against the deployment's own saved data.
    That is the shape of mistake a test over invented data cannot find, which is why the rule
    for everything inside the snapshot is that it may carry more than this layer declares.
    """
    from agentic_trading.advisor import SNAPSHOT

    stored = snapshot()
    stored["accounts"][0]["positions"][0]["a_field_from_a_later_version"] = "kept"
    assistant_service.state_db().write_meta(SNAPSHOT, stored)

    held = assistant_service.advisor_state().snapshot.accounts[0].positions[0]

    assert held.quote_at == "2026-09-14T19:59:59Z" and held.weight_pct == 10.0
    assert held.model_extra["a_field_from_a_later_version"] == "kept"
    assert held.symbol == "VTI" and held.price == 100


def test_an_account_that_could_not_be_totalled_reports_no_money_rather_than_none_of_it(
        assistant_service):
    """A figure the venue would not give is None. Zero would be a claim about the money."""
    from agentic_trading.advisor import SNAPSHOT

    blank = snapshot()
    blank["accounts"][0].update(complete=False, equity_usd=None, cash_usd=None)
    blank["errors"] = ["Account totals are incomplete for ••••1234."]
    assistant_service.state_db().write_meta(SNAPSHOT, blank)

    account = assistant_service.advisor_state().snapshot.accounts[0]
    assert account.complete is False
    assert account.equity_usd is None and account.cash_usd is None
    assert account.positions                          # the holdings were still readable


def test_notes_require_current_revision_and_do_not_change_risk_config(assistant_service):
    s = assistant_service
    path = s.config_dir / "risk_profile.yaml"
    before = path.read_bytes()
    result = s.advisor_notes("Need this money for a home in 2 years.", 0)
    assert result.revision == 1
    with pytest.raises(ServiceConflict):
        s.advisor_notes("Stale edit", 0)
    assert path.read_bytes() == before


def test_chat_survives_reload_and_uses_profile_history_and_existing_decisions(assistant_service, monkeypatch):
    s = assistant_service
    captured = {}

    def reply(profiles, context, message, history, language):
        captured.update(context=context, message=message, history=history)
        return {"answer": "I see your home purchase plan.", "research_candidates": []}

    monkeypatch.setattr("agentic_trading.advisor.answer", reply)
    s.advisor_notes("Home purchase in 2 years", 0)
    job = s.start_advisor(kind="chat", request_id=str(uuid.uuid4()), message="What fits my plan?")
    assert run_task(s.profiles(), job) == 0
    s._research_manager(s.profiles()).store.update(job["run_key"], {"state": "COMPLETE"})
    assert captured["context"]["profile"]["personal_notes"] == "Home purchase in 2 years"
    assert captured["context"]["snapshot"]["accounts"][0]["id"] == "a" * 20
    assert captured["history"] == []
    messages = s.advisor_conversation().messages
    assert [m.role for m in messages] == ["user", "assistant"]
    assert "home purchase" in messages[1].content
    next_job = s.start_advisor(kind="chat", request_id=str(uuid.uuid4()), message="And if my timing changes?")
    assert run_task(s.profiles(), next_job) == 0
    assert len(captured["history"]) == 2
    assert not s._store(s.profiles()).all()


def test_shortlist_only_returns_candidates_from_sources(assistant_service, monkeypatch):
    s = assistant_service
    monkeypatch.setattr("agentic_trading.advisor.answer", lambda *args: {
        "answer": "Research priorities", "research_candidates": [
            {"symbol": "VTI", "reason": "Existing universe"}, {"symbol": "MADEUP", "reason": "invented"}]})
    job = s.start_advisor(kind="shortlist", request_id=str(uuid.uuid4()), message="Find candidates")
    assert run_task(s.profiles(), job) == 0
    assert [c["symbol"] for c in s._advisor_result(job["run_key"])["research_candidates"]] == ["VTI"]


def test_batch_review_reports_deferred_budget_and_idempotent_retry(assistant_service):
    s = assistant_service
    snap = snapshot()
    snap["accounts"][0]["positions"] = [{"symbol": f"ETF{i}", "quantity": 1} for i in range(8)]
    s.state_db().write_meta(SNAPSHOT, snap)
    request_id = str(uuid.uuid4())
    first = s.review_observed_holdings(request_id)
    again = s.review_observed_holdings(request_id)
    assert len(first["started"]) == 6 and len(first["deferred"]) == 2
    assert again == first
    assert len(s._research_manager(s.profiles()).store.list()) == 6


def test_readonly_analysis_reuses_upstream_graph_and_cannot_create_proposals(
        assistant_service, fake_claude, monkeypatch, real_audit_guard):
    s = assistant_service
    monkeypatch.setattr("agentic_trading.graph.classify_instrument", lambda _, **kwargs: "etf")
    monkeypatch.setattr("tradingagents.graph.trading_graph.resolve_instrument_identity", lambda _: {
        "company_name": "Test ETF", "quote_type": "ETF", "exchange": "PCX"})
    monkeypatch.setattr("agentic_trading.proposals.ProposalStore.save",
                        lambda *a, **k: pytest.fail("read-only research created an order proposal"))
    job = s.start_advisor(kind="analysis", request_id=str(uuid.uuid4()), ticker="VTI", account_id="a" * 20)
    record = read_only_research(s.profiles(), job, snapshot(), "My home purchase")
    assert record["advisory"] and record["account_id"] == "a" * 20
    assert record["orders_placed"] is False and "proposal_id" not in record
    assert record["hard_risk"]["verdict"] == "READ_ONLY"
    assert record["llm_usage"]["calls"] >= 8
    assert '"quantity": 1' in record["portfolio"]["snapshot_text"]
    write_record(s.profiles().project_dir / "runs" / job["run_key"] / "decision_record.json", record)
    s._research_manager(s.profiles()).store.update(job["run_key"], {"state": "COMPLETE"})
    assert s.advisor_decisions()[0].advisory


def test_readonly_analysis_requires_the_selected_holding_to_exist(assistant_service):
    job = assistant_service.start_advisor(kind="analysis", request_id=str(uuid.uuid4()),
                                         ticker="MSFT", account_id="a" * 20)
    with pytest.raises(ValueError, match="no longer present"):
        read_only_research(assistant_service.profiles(), job, snapshot(), "")


@pytest.mark.parametrize("quote_type", ["MUTUALFUND", "OPTION", ""])
def test_advisory_classification_rejects_unsupported_or_unknown_funds(quote_type, monkeypatch):
    from agentic_trading.graph import classify_instrument

    monkeypatch.setattr("tradingagents.agents.utils.agent_utils.resolve_instrument_identity",
                        lambda _: {"quote_type": quote_type})
    assert classify_instrument("TEST", strict=True) == "unsupported"


def test_assistant_reuses_cli_and_only_upstream_read_tools(assistant_service, fake_claude):
    result = answer(assistant_service.profiles(), {"snapshot": snapshot()}, "My risks?", [])
    assert AssistantReply.model_validate(result).answer
    from agentic_trading.llm.claude_code import ClaudeCodeChatModel

    command = ClaudeCodeChatModel()._build_command("personal context")
    assert "--strict-mcp-config" in command and command[command.index("--tools") + 1] == ""


def test_profile_does_not_turn_an_unconfirmed_horizon_into_a_fact(profiles):
    context = profile_context(profiles)
    assert context["horizon"] == profiles.investor["horizon"]


def test_assistant_removes_only_trailing_serialization_markers(profiles):
    class Model:
        def with_structured_output(self, schema):
            return self

        def invoke(self, messages):
            assert "unconfirmed suggestions" in messages[0]["content"]
            return AssistantReply(answer="A < B.\n</answer>\n</invoke>\n")

    assert answer(profiles, {}, "test", [], llm=Model())["answer"] == "A < B."


def test_assistant_routes_are_authenticated_and_reject_order_fields(assistant_service, auth_client, monkeypatch):
    from importlib import import_module

    monkeypatch.setattr(import_module("agentic_trading.api.app"), "_service", assistant_service)
    assert auth_client.get("/advisor").status_code == 200
    response = auth_client.post("/advisor/jobs", json={"kind": "chat", "request_id": str(uuid.uuid4()),
                                                     "message": "My risks?", "place_order": True})
    assert response.status_code == 422
    response = auth_client.post("/advisor/jobs", json={"kind": "refresh", "request_id": str(uuid.uuid4())})
    assert response.status_code == 200 and response.json()["proposal_created"] is False
    auth_client.cookies.clear()
    assert auth_client.get("/advisor").status_code == 401


def test_stale_snapshot_cannot_start_a_batch(assistant_service):
    snap = snapshot()
    snap["fetched_at"] = "2000-01-01T00:00:00+00:00"
    assistant_service.state_db().write_meta(SNAPSHOT, snap)
    with pytest.raises(ServiceError, match="Refresh"):
        assistant_service.review_observed_holdings(str(uuid.uuid4()))


def test_cancelled_chat_output_is_not_presented_as_a_completed_answer(assistant_service):
    s = assistant_service
    job = s.start_advisor(kind="chat", request_id=str(uuid.uuid4()), message="A question")
    write_record(s.profiles().project_dir / "runs" / job["run_key"] / "decision_record.json",
                 {"assistant_result": {"answer": "A cancelled answer"}})
    s._research_manager(s.profiles()).store.update(job["run_key"], {"state": "CANCELLED"})
    assert [m.role for m in s.advisor_conversation().messages] == ["user"]


def test_offline_chat_discloses_staleness_and_retains_saved_context(assistant_service, monkeypatch):
    s = assistant_service
    snap = snapshot()
    snap["fetched_at"] = "2000-01-01T00:00:00+00:00"
    s.state_db().write_meta(SNAPSHOT, snap)
    captured = {}

    def offline(*args):
        raise BrokerUnavailable("offline")

    def reply(profiles, context, *args):
        captured.update(context)
        return {"answer": "Saved data is stale", "research_candidates": []}

    monkeypatch.setattr("agentic_trading.advisor.fetch_snapshot", offline)
    monkeypatch.setattr("agentic_trading.advisor.answer", reply)
    job = s.start_advisor(kind="chat", request_id=str(uuid.uuid4()), message="My holdings?")
    assert run_task(s.profiles(), job) == 0
    assert captured["snapshot"]["stale"] and captured["snapshot"]["errors"]
    assert captured["snapshot"]["accounts"] == snap["accounts"]


def test_advisory_jobs_do_not_pollute_the_declared_investment_study():
    from agentic_trading.evaluation import _decisions

    data = _decisions([
        {"bucket": "advisory", "trade_date": "2026-09-09", "final_action": "BUY"},
        {"bucket": "core", "trade_date": "2026-09-09", "final_action": "HOLD"}], [
        {"bucket": "advisory", "created_at": "2026-09-09", "state": "COMPLETE"}], [], "2026-09-01")
    assert data["research_runs"] == 1 and data["run_outcomes"] == {"HOLD": 1}
    assert data["job_states"] == {}


def test_refresh_does_not_spend_research_allowance(assistant_service):
    from agentic_trading.schedule import analyses_today, settings

    s = assistant_service
    s.start_advisor(kind="refresh", request_id=str(uuid.uuid4()))
    s.start_advisor(kind="chat", request_id=str(uuid.uuid4()), message="Hello")
    jobs = s._research_manager(s.profiles()).store.list()
    assert analyses_today(jobs, settings(s.profiles())) == 0
    assert s.state_db().read_meta(NOTES, {}) == {}
