"""Load and validate the personal investment profile and the risk policy.

Two YAML files, kept outside the upstream TradingAgents package:

* ``config/investor_profile.yaml``  intent: what the investor wants and permits
* ``config/risk_profile.yaml``      enforcement: limits deterministic code applies

Stage 1 (research only) validates the small subset it needs (LLM settings,
upstream config overrides, analyst selection, confidence bands, execution
stage). Everything else is loaded, scanned for unresolved ``TODO`` markers and
carried along untouched, so later stages validate it without reformatting.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict

TODO = "TODO"
Effort = Literal["low", "medium", "high", "xhigh", "max"]

# Execution modes a stage may run in. Stage 1 and 2 never execute anything.
STAGE_ALLOWED_MODES: dict[int, list[str]] = {
    1: ["recommend_only"],
    2: ["recommend_only"],
    3: ["recommend_only", "human_approval"],
    4: ["recommend_only", "human_approval"],
    5: ["recommend_only", "human_approval"],
    6: ["recommend_only", "human_approval", "autonomous"],
}

# LLM providers that would require an API key. Refused unless explicitly allowed,
# so a config edit can never silently move billing to pay-as-you-go.
KEYED_PROVIDERS = {
    "anthropic", "openai", "google", "xai", "deepseek", "qwen", "qwen-cn", "glm", "glm-cn",
    "minimax", "minimax-cn", "openrouter", "mistral", "kimi", "groq", "nvidia", "azure", "bedrock",
}


class ProfileError(ValueError):
    """A profile is missing, malformed, or inconsistent. The caller must fail closed."""


class LLMSettings(BaseModel):
    model_config = ConfigDict(extra="allow")

    backend: Literal["claude_code_cli"] = "claude_code_cli"
    deep_effort: Effort = "high"
    quick_effort: Effort = "medium"
    policy_effort: Effort = "high"
    timeout_seconds: int = 900
    max_turns: int = 12
    # Hard ceiling on the per-call turn budget, including any web allowance. A config typo of
    # extra_turns: 500 cannot produce a 500-turn call.
    max_turns_ceiling: int = 24
    claude_bin: str | None = None
    never_use_api_key: bool = True
    on_limit: Literal["wait_for_reset"] = "wait_for_reset"


class TradingAgentsSettings(BaseModel):
    """Only the upstream keys we depend on are typed; the rest pass through."""

    model_config = ConfigDict(extra="allow")

    llm_provider: str = "claude_code"
    deep_think_llm: str = "claude-opus-5"
    quick_think_llm: str = "claude-sonnet-5"
    memory_log_path: str | None = None
    checkpoint_enabled: bool = True
    max_debate_rounds: int = 1
    max_risk_discuss_rounds: int = 1


class AnalystSelection(BaseModel):
    etf: list[str] = ["market", "news"]
    stock: list[str] = ["market", "social", "news", "fundamentals"]
    crypto: list[str] = ["market", "social", "news"]


class ConfidenceRequirements(BaseModel):
    model_config = ConfigDict(extra="allow")

    bands: dict[str, Any] = {}
    minimum_to_open: dict[str, int] = {}

    @property
    def no_trade_below(self) -> int:
        return int(self.bands.get("no_trade_below", 50))

    def floor(self, bucket: str, instrument_type: str, size: str = "small") -> int:
        m = self.minimum_to_open
        if bucket == "sandbox":
            return int(m.get("sandbox", 65))
        if instrument_type == "stock":
            return int(m.get("core_individual_stock", 75))
        if instrument_type == "etf":
            key = "core_broad_etf_full_size" if size == "normal" else "core_broad_etf_half_size"
            return int(m.get(key, 75 if size == "normal" else 65))
        # crypto and anything else is never a core instrument
        return 101


@dataclass
class Profiles:
    config_dir: Path
    investor: dict[str, Any]
    risk: dict[str, Any]
    llm: LLMSettings
    tradingagents: TradingAgentsSettings
    analysts: AnalystSelection
    confidence: ConfidenceRequirements
    stage: int
    bucket_modes: dict[str, str]
    unresolved: list[str]
    accept_defaults: bool
    defaults_used: list[str] = field(default_factory=list)

    @property
    def project_dir(self) -> Path:
        return self.config_dir.parent

    def analysts_for(self, instrument_type: str) -> list[str]:
        return list(getattr(self.analysts, instrument_type, self.analysts.stock))

    def mandate_context(self) -> str:
        name = self.investor.get("objective", {}).get("mandate_context_file", "config/mandate_context.md")
        path = self.project_dir / name
        if not path.exists():
            raise ProfileError(f"mandate context file not found: {path}")
        return path.read_text(encoding="utf-8").strip()

    def mandate_full(self) -> str:
        name = self.investor.get("objective", {}).get("mandate_file", "config/MANDATE.md")
        path = self.project_dir / name
        if not path.exists():
            # MANDATE.md was delivered at the project root before config/ existed.
            alt = self.project_dir / "MANDATE.md"
            if alt.exists():
                return alt.read_text(encoding="utf-8").strip()
            raise ProfileError(f"mandate file not found: {path}")
        return path.read_text(encoding="utf-8").strip()

    def effective(self, node: Any, path: str) -> Any:
        """Effective value of a ``{value, recommended_default}`` node.

        An unresolved field falls back to its documented ``recommended_default``, and every
        such use is recorded in ``defaults_used`` so the decision record and the preflight show
        exactly which numbers are engineering defaults rather than the investor's own choices.
        A field with no default at all is still a hard failure.

        Real money is gated separately: the Micro-Live preflight refuses to arm execution while
        defaults are in play unless the investor has explicitly accepted them
        (``accept_all_recommended_defaults``). That keeps research and recommendation working
        without letting an unreviewed number reach a live order.
        """
        if isinstance(node, dict) and "value" in node:
            if node["value"] == TODO:
                if "recommended_default" not in node:
                    raise ProfileError(f"unresolved TODO at {path} has no recommended_default")
                if path not in self.defaults_used:
                    self.defaults_used.append(path)
                return copy.deepcopy(node["recommended_default"])
            return node["value"]
        return node

    def _walk(self, root: dict[str, Any], keys: tuple[str, ...], label: str, default: Any) -> Any:
        node: Any = root
        for i, key in enumerate(keys):
            if isinstance(node, dict) and "value" in node and key not in node:
                node = self.effective(node, f"{label}." + ".".join(keys[:i]))
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return self.effective(node, f"{label}." + ".".join(keys))

    def risk_cfg(self, *keys: str, default: Any = None) -> Any:
        return self._walk(self.risk, keys, "risk_profile", default)

    def investor_cfg(self, *keys: str, default: Any = None) -> Any:
        return self._walk(self.investor, keys, "investor_profile", default)

    def exposure_groups(self) -> dict[str, list[str]]:
        groups = self.investor_cfg("universe", "core", "etf_exposure_groups", default={}) or {}
        return {str(g): [str(t) for t in tickers] for g, tickers in groups.items()}

    def sleeve_of_group(self, group: str | None) -> str:
        mapping = self.risk.get("sleeve_of_exposure_group") or {}
        return str(mapping.get(group or "unknown_etf", mapping.get("unknown_etf", "satellites")))

    def capital_allocation(self, bucket: str) -> float:
        """Allocation for a bucket, from the investor profile, then the risk profile."""
        buckets = (self.investor.get("capital") or {}).get("buckets") or {}
        cfg = buckets.get(bucket) or {}
        value = cfg.get("allocation_usd", cfg.get("allocation_usd_max"))
        if value is None:
            risk_bucket = ((self.risk.get("buckets") or {}).get(bucket) or {})
            value = risk_bucket.get("allocation_usd", risk_bucket.get("allocation_usd_max", 0.0))
        return float(value or 0.0)

    def resolve(self, node: Any, *, path: str = "") -> Any:
        """Return the effective value of a ``{value, recommended_default}`` node.

        A TODO value is returned as the recommended default only when the
        investor set ``accept_all_recommended_defaults: true``; otherwise it
        raises so the caller fails closed instead of guessing.
        """
        if isinstance(node, dict) and "value" in node:
            if node["value"] == TODO:
                if self.accept_defaults and "recommended_default" in node:
                    return copy.deepcopy(node["recommended_default"])
                raise ProfileError(f"unresolved TODO at {path or 'node'}; approve or set a value")
            return node["value"]
        return node


def _find_todos(obj: Any, path: str, out: list[str]) -> None:
    if isinstance(obj, dict):
        if obj.get("value") == TODO:
            out.append(path)  # a {value, recommended_default} node counts once
            return
        for k, v in obj.items():
            _find_todos(v, f"{path}.{k}" if path else str(k), out)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _find_todos(v, f"{path}[{i}]", out)
    elif obj == TODO:
        out.append(path)


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ProfileError(f"missing profile file: {path}")
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ProfileError(f"profile file is not a mapping: {path}")
    return data


def load_profiles(config_dir: str | Path = "config") -> Profiles:
    config_dir = Path(config_dir).resolve()
    investor = _load_yaml(config_dir / "investor_profile.yaml")
    risk = _load_yaml(config_dir / "risk_profile.yaml")

    for name, doc in (("investor_profile", investor), ("risk_profile", risk)):
        if doc.get("unresolved_policy") != "fail_closed":
            raise ProfileError(f"{name}: unresolved_policy must be fail_closed")

    llm = LLMSettings(**(investor.get("llm") or {}))
    ta = TradingAgentsSettings(**(investor.get("tradingagents") or {}))
    analysts = AnalystSelection(**(investor.get("analyst_selection") or {}))
    confidence = ConfidenceRequirements(**(risk.get("confidence_requirements") or {}))

    # Billing guard: the only backend is the subscription-backed CLI.
    if llm.never_use_api_key and ta.llm_provider in KEYED_PROVIDERS:
        raise ProfileError(
            f"tradingagents.llm_provider={ta.llm_provider!r} needs an API key; this system runs on the "
            "Claude subscription only (llm_provider: claude_code). Set llm.never_use_api_key: false "
            "to override deliberately."
        )
    if ta.llm_provider != "claude_code":
        raise ProfileError(f"unsupported llm_provider {ta.llm_provider!r}; expected 'claude_code'")

    ep = risk.get("execution_policy") or {}
    stage = int(ep.get("stage", 1))
    if stage not in STAGE_ALLOWED_MODES:
        raise ProfileError(f"execution_policy.stage must be 1..6, got {stage}")
    bucket_modes = {
        name: str((cfg or {}).get("execution_mode", "recommend_only"))
        for name, cfg in (ep.get("buckets") or {}).items()
    }
    for bucket, mode in bucket_modes.items():
        if mode not in STAGE_ALLOWED_MODES[stage]:
            raise ProfileError(
                f"bucket {bucket!r} mode {mode!r} is not allowed at stage {stage}; "
                f"allowed: {STAGE_ALLOWED_MODES[stage]}"
            )
    if ep.get("live_trading_approved") and stage < 5:
        raise ProfileError("live_trading_approved cannot be true before stage 5")
    if ep.get("broker_environment", "paper") != "paper" and stage < 5:
        raise ProfileError("broker_environment must be paper before stage 5")

    # Risk profile must be at least as strict as the investor's absolute drawdown limit.
    investor_dd = float((investor.get("risk_tolerance") or {}).get("max_portfolio_drawdown_pct", 15))
    for level in ((risk.get("drawdown_ladder") or {}).get("total_portfolio") or {}).get("levels", []):
        if level.get("name") == "EMERGENCY":
            trig = level.get("trigger_pct")
            if isinstance(trig, dict):
                trig = trig.get("value") if trig.get("value") != TODO else trig.get("recommended_default")
            if trig is not None and abs(float(trig)) > investor_dd:
                raise ProfileError("risk_profile EMERGENCY trigger is looser than investor max drawdown")

    unresolved: list[str] = []
    _find_todos(investor, "investor_profile", unresolved)
    _find_todos(risk, "risk_profile", unresolved)

    return Profiles(
        config_dir=config_dir,
        investor=investor,
        risk=risk,
        llm=llm,
        tradingagents=ta,
        analysts=analysts,
        confidence=confidence,
        stage=stage,
        bucket_modes=bucket_modes,
        unresolved=unresolved,
        accept_defaults=bool(investor.get("accept_all_recommended_defaults", False)),
    )


def build_tradingagents_config(profiles: Profiles, runs_dir: str | Path | None = None) -> dict[str, Any]:
    """Upstream DEFAULT_CONFIG with the profile's ``tradingagents`` section merged in.

    Dict-valued keys (data_vendors, tool_vendors, benchmark_map) are merged one
    level deep, mirroring upstream ``set_config`` semantics.
    """
    from tradingagents.default_config import DEFAULT_CONFIG

    cfg = copy.deepcopy(DEFAULT_CONFIG)
    section = profiles.investor.get("tradingagents") or {}
    for key, value in section.items():
        if isinstance(value, dict) and isinstance(cfg.get(key), dict):
            cfg[key].update(value)
        else:
            cfg[key] = value
    # Decision memory stays off during research and recommendation stages: upstream
    # resolves outcomes after five trading days, a horizon that contradicts the mandate.
    if profiles.stage <= 2:
        cfg["memory_log_path"] = None
    runs = Path(runs_dir) if runs_dir else profiles.project_dir / "runs"
    cfg["results_dir"] = str(runs / "tradingagents")
    return cfg
