import re
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


# -- confidence floors --------------------------------------------------------------
#
# micro_live was not a case in floor() at all, so an order deploying real money was gated by
# the conceptual core book's numbers. These pin that it now has its own, that they are never
# looser, and that a deleted one stops trading instead of silently falling back.

CLASSES = ["broad_diversified_etf", "bond_or_cash_like_etf", "sector_factor_thematic_etf",
           "individual_stock"]


def test_real_money_floors_are_its_own_and_never_looser_than_core(profiles):
    cfg = profiles.confidence
    for limit_class in CLASSES:
        kind = "stock" if limit_class == "individual_stock" else "etf"
        for size in ("small", "normal"):
            core = cfg.floor("core", kind, size, limit_class=limit_class)
            real = cfg.floor("micro_live", kind, size, limit_class=limit_class)
            assert real >= core, f"{limit_class}/{size}: real money {real} is looser than core {core}"
    assert cfg.floor("micro_live", "stock", "normal", limit_class="individual_stock") == 85


def test_a_missing_real_money_floor_stops_trading_rather_than_borrowing_cores(profiles):
    profiles.confidence.minimum_to_open.pop("micro_live_individual_stock")
    assert profiles.confidence.floor("micro_live", "stock", "normal",
                                     limit_class="individual_stock") == 101
    # A missing core key keeps its documented default; only real money fails closed.
    profiles.confidence.minimum_to_open.pop("core_individual_stock")
    assert profiles.confidence.floor("core", "stock", "normal",
                                     limit_class="individual_stock") == 75


def test_an_autonomous_action_can_only_raise_the_floor(profiles):
    cfg = profiles.confidence
    for bucket in ("core", "sandbox", "micro_live"):
        for limit_class in CLASSES:
            kind = "stock" if limit_class == "individual_stock" else "etf"
            attended = cfg.floor(bucket, kind, "normal", limit_class=limit_class)
            alone = cfg.floor(bucket, kind, "normal", limit_class=limit_class, autonomous=True)
            assert alone >= attended
            assert alone >= min(80, 101), "autonomous_any is a floor under every autonomous action"
    assert cfg.floor("sandbox", "etf", "small", autonomous=True) == 80   # sandbox's own is 65


def test_the_sector_floor_finally_applies_and_only_ever_tightens(profiles):
    cfg = profiles.confidence
    for size in ("small", "normal"):
        unclassified = cfg.floor("core", "etf", size)
        sector = cfg.floor("core", "etf", size, limit_class="sector_factor_thematic_etf")
        assert sector >= unclassified, "applying the sector floor must not loosen anything"
    # core_sector_factor_etf is 70: it binds at half size, where the broad floor is 65.
    assert cfg.floor("core", "etf", "small", limit_class="sector_factor_thematic_etf") == 70
    assert cfg.floor("core", "etf", "normal", limit_class="sector_factor_thematic_etf") == 75


def test_an_unclassifiable_instrument_is_never_openable(profiles):
    for bucket in ("core", "micro_live"):
        assert profiles.confidence.floor(bucket, "crypto", "normal") == 101
        assert profiles.confidence.floor(bucket, "etf", "normal", limit_class="crypto") == 101


def test_the_old_positional_signature_still_means_what_it_did(profiles):
    # policy.py calls this without a limit_class; core behaviour there must not have moved.
    cfg = profiles.confidence
    assert cfg.floor("core", "etf", "normal") == 75
    assert cfg.floor("core", "etf", "small") == 65
    assert cfg.floor("core", "stock", "normal") == 75
    assert cfg.floor("sandbox", "etf", "small") == 65


# Top-level risk_profile keys that no code reads, each with the reason it is allowed to stay.
# The failure mode this guards is silent: a limit is tightened here, nothing reads it, and the
# profile goes on reading as though the system had been tightened.
UNREAD_RISK_KEYS = {
    "profile_name": "a label for this profile, not a limit",
    "fail_closed_conditions": "a declaration of intent; each condition is enforced in code under "
                              "its own name, not by walking this list",
}


def test_every_risk_profile_top_level_key_has_a_reader(config_dir):
    """A limit nobody reads is worse than a missing one: it reads as though it applied.

    Substring matching over the package is deliberately crude — a key named in a docstring
    counts as read. It is enough to catch a whole block that was declared and then never wired
    up, which has happened here more than once.
    """
    keys = yaml.safe_load((config_dir / "risk_profile.yaml").read_text())
    source = "\n".join(p.read_text() for p in (config_dir.parent / "agentic_trading").rglob("*.py"))
    unread = {k for k in keys if not re.search(rf"""["']{re.escape(k)}["']""", source)}

    assert unread == set(UNREAD_RISK_KEYS), (
        "risk_profile.yaml top-level keys with no reader changed.\n"
        f"newly unread: {sorted(unread - set(UNREAD_RISK_KEYS))}\n"
        f"now read, drop from UNREAD_RISK_KEYS: {sorted(set(UNREAD_RISK_KEYS) - unread)}")
