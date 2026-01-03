"""ConservativeTradingGraph: the upstream TradingAgentsGraph, unmodified, with

* the LLM layer swapped for the subscription-backed Claude Code adapter,
* the conservative mandate and bucket context injected into every agent's
  prompt through the upstream ``resolve_instrument_context`` hook, and
* analyst selection per instrument type (ETF runs skip the fundamentals
  analyst, whose tools return no statements for funds).

No upstream file is modified. The only intrusion is a scoped patch of the
``create_llm_client`` name inside ``tradingagents.graph.trading_graph`` while
the base class constructor runs, because upstream has no provider registration
hook yet (a small upstream PR would remove even that).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest import mock

from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.llm_clients.base_client import BaseLLMClient

from .llm.claude_code import ClaudeCodeChatModel
from .profile import Profiles, build_tradingagents_config


class _ClaudeCodeClient(BaseLLMClient):
    def __init__(self, model: str, base_url: str | None = None, *, llm: ClaudeCodeChatModel, **kwargs: Any):
        super().__init__(model, base_url, **kwargs)
        self._llm = llm

    def get_llm(self) -> Any:
        return self._llm

    def validate_model(self) -> bool:
        return True


def make_llm_factory(profiles: Profiles, work_dir: str | None = None):
    """Return a drop-in replacement for upstream ``create_llm_client``."""

    def factory(provider: str, model: str, base_url: str | None = None, **kwargs: Any) -> BaseLLMClient:
        role = "deep" if model == profiles.tradingagents.deep_think_llm else "quick"
        effort = profiles.llm.deep_effort if role == "deep" else profiles.llm.quick_effort
        llm = ClaudeCodeChatModel(
            model=model,
            effort=effort,
            timeout_seconds=profiles.llm.timeout_seconds,
            max_turns=profiles.llm.max_turns,
            claude_bin=profiles.llm.claude_bin,
            work_dir=work_dir or str(profiles.project_dir / "runs" / ".claude_workdir"),
            config_dir=str(profiles.config_dir),
            role=role,
            callbacks=kwargs.get("callbacks"),
        )
        return _ClaudeCodeClient(model, base_url, llm=llm)

    return factory


def classify_instrument(ticker: str) -> str:
    """etf | stock | crypto, from the upstream deterministic identity resolver."""
    upper = ticker.upper()
    if upper.endswith("-USD") or "/" in upper:
        return "crypto"
    from tradingagents.agents.utils.agent_utils import resolve_instrument_identity

    identity = resolve_instrument_identity(ticker) or {}
    quote_type = str(identity.get("quote_type", "")).upper()
    if quote_type == "ETF":
        return "etf"
    if quote_type == "CRYPTOCURRENCY":
        return "crypto"
    return "stock"


class ConservativeTradingGraph(TradingAgentsGraph):
    def __init__(
        self,
        profiles: Profiles,
        *,
        instrument_type: str = "stock",
        bucket: str = "core",
        debug: bool = False,
        config: dict[str, Any] | None = None,
        callbacks: list | None = None,
        llm_factory=None,
        portfolio_text: str = "",
    ):
        self.profiles = profiles
        self.portfolio_text = portfolio_text
        self.instrument_type = instrument_type
        self.bucket = bucket
        selected = profiles.analysts_for(instrument_type)
        config = config or build_tradingagents_config(profiles)
        factory = llm_factory or make_llm_factory(profiles)

        import tradingagents.graph.trading_graph as upstream_module

        # Keep the per-call audit trail beside this run's results, so an offline
        # test writing to a temp results_dir cannot append to the real runs/ log.
        if llm_factory is None:
            factory = make_llm_factory(
                profiles, work_dir=str(Path(config["results_dir"]).parent / ".claude_workdir")
            )

        with mock.patch.object(upstream_module, "create_llm_client", factory):
            super().__init__(selected_analysts=selected, debug=debug, config=config, callbacks=callbacks)

    # Upstream injects this string into every agent prompt (analysts, researchers,
    # managers, trader, risk debators, portfolio manager) and calls it from both the
    # CLI and propagate(), which makes it the one zero-modification place to add
    # the mandate and the portfolio context.
    def resolve_instrument_context(self, ticker: str, asset_type: str = "stock") -> str:
        base = super().resolve_instrument_context(ticker, asset_type)
        return f"{base}\n\n{self.profiles.mandate_context()}\n\n{self.portfolio_context()}"

    def portfolio_context(self) -> str:
        mode = self.profiles.bucket_modes.get(self.bucket, "recommend_only")
        snapshot = (self.portfolio_text + "\n") if self.portfolio_text else ""
        return snapshot + (
            f"PORTFOLIO CONTEXT: this analysis is for the {self.bucket.upper()} bucket "
            f"({self.instrument_type}); execution stage {self.profiles.stage}, mode {mode}. "
            "No order is placed from this analysis; it produces a recommendation record that "
            "deterministic risk rules and a human review before anything is ever executed."
        )
