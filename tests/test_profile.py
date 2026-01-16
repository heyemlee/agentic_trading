import shutil

import pytest
import yaml

from agentic_trading.profile import ProfileError, build_tradingagents_config, load_profiles


def test_real_profiles_load_in_execution_capable_stage(profiles):
    # Stage 3 permits human_approval; only micro_live has a venue, and real money stays gated
    # behind the Micro-Live master switch regardless of this stage.
    assert profiles.stage == 3
    assert profiles.bucket_modes == {"core": "recommend_only", "sandbox": "recommend_only",
                                     "micro_live": "human_approval"}
    assert profiles.llm.backend == "claude_code_cli" and profiles.llm.never_use_api_key
    assert profiles.tradingagents.llm_provider == "claude_code"
    assert profiles.analysts_for("etf") == ["market", "news"]
    assert "investor_profile.horizon.investment_horizon" in profiles.unresolved
    assert len(profiles.unresolved) == len(set(profiles.unresolved)) == 68  # one entry per TODO node (measured)
    assert "CONSERVATIVE MANDATE" in profiles.mandate_context()
    assert "Capital preservation" in profiles.mandate_full() or "capital preservation" in profiles.mandate_full()


def test_upstream_config_merge(profiles):
    cfg = build_tradingagents_config(profiles)
    assert cfg["llm_provider"] == "claude_code"
    assert cfg["deep_think_llm"] == "claude-opus-5" and cfg["quick_think_llm"] == "claude-sonnet-5"
    assert cfg["memory_log_path"] is None and cfg["checkpoint_enabled"] is True
    assert cfg["data_vendors"]["macro_data"] == "fred" and cfg["data_vendors"]["core_stock_apis"] == "yfinance"
    assert cfg["results_dir"].endswith("runs/tradingagents")
    assert cfg["max_debate_rounds"] == 2


def test_todo_values_fail_closed_unless_defaults_accepted(profiles):
    node = {"value": "TODO", "recommended_default": 5}
    with pytest.raises(ProfileError):
        profiles.resolve(node, path="x")
    profiles.accept_defaults = True
    assert profiles.resolve(node, path="x") == 5


def _tampered(config_dir, tmp_path, file, mutate):
    dst = tmp_path / "config"
    shutil.copytree(config_dir, dst)
    doc = yaml.safe_load((dst / file).read_text())
    mutate(doc)
    (dst / file).write_text(yaml.safe_dump(doc))
    for extra in ("mandate_context.md",):
        shutil.copy(config_dir / extra, dst / extra)
    return dst


def test_keyed_provider_is_refused(config_dir, tmp_path):
    dst = _tampered(config_dir, tmp_path, "investor_profile.yaml",
                    lambda d: d["tradingagents"].__setitem__("llm_provider", "anthropic"))
    with pytest.raises(ProfileError, match="API key"):
        load_profiles(dst)


def test_execution_mode_beyond_stage_is_refused(config_dir, tmp_path):
    dst = _tampered(config_dir, tmp_path, "risk_profile.yaml",
                    lambda d: d["execution_policy"]["buckets"]["core"].__setitem__("execution_mode", "autonomous"))
    with pytest.raises(ProfileError, match="not allowed at stage 3"):
        load_profiles(dst)


def test_recommended_defaults_are_usable_but_recorded(profiles):
    """Research must keep working on documented defaults; every use is recorded for the record."""
    profiles.risk_cfg("buckets", "core", "position_limits_pct_of_core", "individual_stock")
    assert any("position_limits_pct_of_core" in d for d in profiles.defaults_used)


def test_live_flag_before_stage_5_is_refused(config_dir, tmp_path):
    dst = _tampered(config_dir, tmp_path, "risk_profile.yaml",
                    lambda d: d["execution_policy"].__setitem__("live_trading_approved", True))
    with pytest.raises(ProfileError, match="stage 5"):
        load_profiles(dst)
