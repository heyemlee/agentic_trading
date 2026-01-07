"""Whole pipeline through the CLI on the fake model: portfolio -> graph -> policy -> Hard Risk -> record."""

import json
import shutil
from pathlib import Path

from conservative_trading import cli, scenarios
from conservative_trading.portfolio import build_snapshot
from conservative_trading.profile import load_profiles

ROOT = Path(__file__).resolve().parents[1]
FAKE_IDENTITY = {"company_name": "Test Fund", "quote_type": "ETF", "exchange": "PCX"}


def test_ct_research_end_to_end_offline(fake_claude, tmp_path, monkeypatch, real_audit_guard):
    # isolated copies of config and state so nothing touches the real files
    cfg_dir = tmp_path / "config"
    shutil.copytree(ROOT / "config", cfg_dir)
    shutil.copy(ROOT / "MANDATE.md", tmp_path / "MANDATE.md")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    shutil.copy(ROOT / "state" / "portfolio.example.yaml", state_dir / "portfolio.yaml")
    profiles = load_profiles(cfg_dir)
    profiles.risk["portfolio_state"]["file"] = "state/portfolio.yaml"
    profiles.risk["portfolio_state"]["risk_state_file"] = "state/risk_state.json"

    # no network: synthetic prices, identities and regime; upstream results under tmp
    monkeypatch.setattr(cli, "build_snapshot",
                        lambda state, p, d: build_snapshot(state, p, d, scenarios.price_lookup, scenarios.identity_lookup))
    monkeypatch.setattr(cli, "assess_regime", lambda p, d: scenarios.regime("NORMAL"))
    monkeypatch.setattr(cli, "upstream_identity_lookup", scenarios.identity_lookup)
    monkeypatch.setattr("tradingagents.graph.trading_graph.resolve_instrument_identity", lambda t: FAKE_IDENTITY)
    monkeypatch.setattr("conservative_trading.profile.build_tradingagents_config",
                        lambda p, runs_dir=None: _cfg(p, tmp_path))
    monkeypatch.setattr("conservative_trading.graph.build_tradingagents_config",
                        lambda p, runs_dir=None: _cfg(p, tmp_path))

    code = cli.run_one(profiles, "VTI", scenarios.TRADE_DATE, "core", "etf", tmp_path / "runs", debug=False)
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
    assert (state_dir / "risk_state.json").exists()


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
