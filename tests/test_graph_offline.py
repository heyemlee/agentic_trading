"""End-to-end plumbing test of the upstream graph on the fake CLI: no network, no subscription usage."""

import json

from tradingagents.agents.utils.rating import RATINGS_5_TIER
from tradingagents.graph.analyst_execution import ANALYST_NODE_SPECS

from agentic_trading import agent_roster
from agentic_trading.graph import AgenticTradingGraph
from agentic_trading.llm.claude_code import (
    USAGE_LOG,
    ClaudeCodeChatModel,
    reset_usage_log,
    usage_summary,
)
from agentic_trading.policy import run_policy
from agentic_trading.profile import build_tradingagents_config
from agentic_trading.records import build_record, write_record

FAKE_IDENTITY = {"company_name": "Test Fund", "sector": "Financial", "industry": "Asset Management",
                 "exchange": "PCX", "quote_type": "ETF"}


def test_full_run_offline(fake_claude, profiles, tmp_path, monkeypatch, real_audit_guard):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-must-never-leak")
    monkeypatch.setattr("tradingagents.graph.trading_graph.resolve_instrument_identity", lambda t: FAKE_IDENTITY)
    cfg = build_tradingagents_config(profiles, runs_dir=tmp_path / "runs")
    cfg["data_cache_dir"] = str(tmp_path / "cache")
    cfg["checkpoint_enabled"] = False

    reset_usage_log()
    run_dir = tmp_path / "run"
    graph = AgenticTradingGraph(profiles, instrument_type="etf", bucket="core", config=cfg,
                                run_dir=run_dir)
    state, signal = graph.propagate("VTI", "2026-09-02", asset_type="stock")

    assert signal in RATINGS_5_TIER
    assert "CONSERVATIVE MANDATE" in state["instrument_context"]
    assert "CORE bucket" in state["instrument_context"] and "Test Fund" in state["instrument_context"]
    assert state["market_report"].startswith("FAKE RESPONSE") and state["news_report"].startswith("FAKE RESPONSE")
    assert state["fundamentals_report"] == ""  # ETF mode skips the fundamentals analyst
    assert "**Rating**:" in state["final_trade_decision"]
    assert usage_summary()["calls"] >= 8 and all(u["model"] in {"claude-opus-5", "claude-sonnet-5"} for u in USAGE_LOG)
    deep_calls = [u for u in USAGE_LOG if u["model"] == "claude-opus-5"]
    assert len(deep_calls) == 2  # research manager + portfolio manager

    audit = tmp_path / "runs" / "llm_calls.jsonl"
    assert audit.exists()  # the offline run audits into its own temp runs/ (real-trail check: real_audit_guard)
    assert len(audit.read_text().splitlines()) == usage_summary()["calls"]

    # Progress events: the console's only signal while a run is still in flight, since the
    # report tree below is written in one go once the whole graph has finished.
    events = [json.loads(line)
              for line in (run_dir / "progress.jsonl").read_text().splitlines()]
    assert {e["kind"] for e in events} == {"call_started", "call_finished"}
    assert all(e["role"] for e in events), "every model call must name the desk it came from"
    assert len([e for e in events if e["kind"] == "call_finished"]) == usage_summary()["calls"]
    worked = {e["role"] for e in events}
    assert worked == {"market", "news", "bull", "bear", "manager", "trader",
                      "aggressive", "neutral", "conservative", "portfolio"}
    assert not worked & {"social", "fundamentals"}  # ETF mode never seats these two

    # The roster is the one table mapping upstream node names onto desks. If an upstream bump
    # renames a node, this fails here rather than leaving a desk permanently dark.
    built = set(graph.graph.get_graph().nodes) - {"__start__", "__end__"}
    agent_nodes = {n for n in built if not n.startswith(("Msg Clear", "tools_"))}
    assert agent_nodes <= set(agent_roster.BY_NODE), (
        f"roster is missing upstream node(s): {sorted(agent_nodes - set(agent_roster.BY_NODE))}")
    assert {spec.agent_node for spec in ANALYST_NODE_SPECS.values()} <= set(agent_roster.BY_NODE)

    report = graph.save_reports(state, "VTI", tmp_path / "reports")
    assert report.name == "complete_report.md" and (tmp_path / "reports" / "1_analysts" / "market.md").exists()

    policy_llm = ClaudeCodeChatModel(model="claude-opus-5", role="policy",
                                     work_dir=str(tmp_path / "runs" / ".claude_workdir"))
    policy = run_policy(profiles, state, ticker="VTI", instrument_type="etf", bucket="core",
                        pm_rating=signal, trade_date="2026-09-02", llm=policy_llm)
    record = build_record(run_id="r1", ticker="VTI", instrument_type="etf", bucket="core", trade_date="2026-09-02",
                          profiles=profiles, analysts=["market", "news"], pm_signal=signal, final_state=state,
                          policy=policy, report_path=report, state_log_path=None, llm_usage=usage_summary(),
                          llm_calls=list(USAGE_LOG), elapsed_seconds=1.0)
    path = write_record(tmp_path / "decision_record.json", record)
    assert path.exists() and record["orders_placed"] is False and record["policy"]["final_action"] == "NO_TRADE"
