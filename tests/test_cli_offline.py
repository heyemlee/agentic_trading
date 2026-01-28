"""Whole pipeline through the CLI on the fake model: portfolio -> graph -> policy -> Hard Risk -> record."""

import json
import shutil
from pathlib import Path

from agentic_trading import cli, risk_scenarios
from agentic_trading.portfolio import build_snapshot
from agentic_trading.profile import load_profiles
from agentic_trading.state_db import StateDB
from tests.conftest import copy_example_ledger

ROOT = Path(__file__).resolve().parents[1]
FAKE_IDENTITY = {"company_name": "Test Fund", "quote_type": "ETF", "exchange": "PCX"}


def test_ct_research_end_to_end_offline(fake_claude, tmp_path, monkeypatch, real_audit_guard):
    # isolated copies of config and state so nothing touches the real files
    cfg_dir = tmp_path / "config"
    shutil.copytree(ROOT / "config", cfg_dir)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    copy_example_ledger("portfolio.example.yaml", state_dir / "portfolio.yaml",
                        as_of=risk_scenarios.TRADE_DATE)
    profiles = load_profiles(cfg_dir)
    profiles.risk["portfolio_state"]["file"] = "state/portfolio.yaml"
    profiles.risk["portfolio_state"]["risk_state_file"] = "state/risk_state.json"

    # no network: synthetic prices, identities and regime; upstream results under tmp
    monkeypatch.setattr(cli, "build_snapshot",
                        lambda state, p, d: build_snapshot(state, p, d, risk_scenarios.price_lookup, risk_scenarios.identity_lookup))
    monkeypatch.setattr(cli, "assess_regime", lambda p, d: risk_scenarios.regime("NORMAL"))
    monkeypatch.setattr(cli, "upstream_identity_lookup", risk_scenarios.identity_lookup)
    monkeypatch.setattr("tradingagents.graph.trading_graph.resolve_instrument_identity", lambda t: FAKE_IDENTITY)
    monkeypatch.setattr("agentic_trading.profile.build_tradingagents_config",
                        lambda p, runs_dir=None: _cfg(p, tmp_path))
    monkeypatch.setattr("agentic_trading.graph.build_tradingagents_config",
                        lambda p, runs_dir=None: _cfg(p, tmp_path))

    code = cli.run_one(profiles, "VTI", risk_scenarios.TRADE_DATE, "core", "etf", tmp_path / "runs", debug=False)
    assert code == 0

    records = list((tmp_path / "runs").glob("*/decision_record.json"))
    assert len(records) == 1
    rec = json.loads(records[0].read_text())
    assert rec["orders_placed"] is False and rec["final_recommendation"]["executable"] is False
    assert rec["hard_risk"]["verdict"] in ("APPROVE", "REJECT", "NO_ACTION")
    assert rec["hard_risk"]["ladder"]["effective_level_for_bucket"] == "NORMAL"
    assert rec["hard_risk"]["regime"]["level"] == "NORMAL"
    assert "PORTFOLIO SNAPSHOT" in rec["portfolio"]["snapshot_text"]
    # the fake reviewer returns confidence 0 -> policy NO_TRADE -> Hard Risk NO_ACTION, never an approval
    assert rec["policy"]["final_action"] == "NO_TRADE" and rec["hard_risk"]["verdict"] == "NO_ACTION"
    assert rec["final_recommendation"]["action"] == "NO_TRADE"
    assert StateDB(state_dir / "risk_state.json").read_document(state_dir / "risk_state.json") is not None

    # The console reads this file, and only this file, while a run is still working: the
    # report tree beside it is written in one go at the end and says nothing before that.
    events = [json.loads(line) for line in
              (records[0].parent / "progress.jsonl").read_text().splitlines()]
    opened, closed = events[0], events[-1]
    assert opened["kind"] == "run_started"
    assert opened["ticker"] == "VTI" and opened["analysts"] == ["market", "news"]
    # The last word belongs to the deterministic side, never to a desk.
    assert closed["kind"] == "run_finished"
    assert closed["policy_action"] == "NO_TRADE" and closed["hard_risk"] == "NO_ACTION"
    assert closed["final_action"] == "NO_TRADE" and closed["proposal_id"] is None
    desks = {e["role"] for e in events if e["kind"] == "call_finished"}
    assert desks == {"market", "news", "bull", "bear", "manager", "trader",
                     "aggressive", "neutral", "conservative", "portfolio"}


def _cfg(profiles, tmp_path):
    from copy import deepcopy

    from tradingagents.default_config import DEFAULT_CONFIG

    cfg = deepcopy(DEFAULT_CONFIG)
    cfg.update(profiles.investor.get("tradingagents") or {})
    cfg["memory_log_path"] = None
    cfg["checkpoint_enabled"] = False
    cfg["results_dir"] = str(tmp_path / "runs" / "tradingagents")
    cfg["data_cache_dir"] = str(tmp_path / "cache")
    return cfg
