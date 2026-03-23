"""AgenticTradingGraph: the upstream TradingAgentsGraph, unmodified, with

* the LLM layer swapped for the subscription-backed Claude Code adapter,
* the conservative mandate and bucket context injected into every agent's
  prompt through the upstream ``resolve_instrument_context`` hook, and
* analyst selection per instrument type (ETF runs skip the fundamentals
  analyst, whose tools return no statements for funds).

No upstream file is modified. There are two intrusions, both scoped patches of
one name each, put back when the call they wrap returns:

* ``create_llm_client`` inside ``tradingagents.graph.trading_graph`` while the
  base class constructor runs, because upstream has no provider registration
  hook yet (a small upstream PR would remove even that);
* ``fetch_reddit_posts`` inside upstream's sentiment analyst while a run is in
  progress, so that its Reddit read goes through ``sources.reddit`` -- which
  calls the same upstream function, and records how each request ended.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from unittest import mock

from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.llm_clients.base_client import BaseLLMClient

from .llm.claude_code import ClaudeCodeChatModel
from .llm.untrusted import UNTRUSTED_CONTENT_NOTICE
from .profile import Profiles, build_tradingagents_config
from .sources import LOG_NAME as SOURCES_LOG_NAME


class _ClaudeCodeClient(BaseLLMClient):
    def __init__(self, model: str, base_url: str | None = None, *, llm: ClaudeCodeChatModel, **kwargs: Any):
        super().__init__(model, base_url, **kwargs)
        self._llm = llm

    def get_llm(self) -> Any:
        return self._llm

    def validate_model(self) -> bool:
        return True


def make_llm_factory(profiles: Profiles, work_dir: str | None = None,
                     progress_path: str | None = None, sources_log: str | None = None):
    """Return a drop-in replacement for upstream ``create_llm_client``."""
    from . import sources
    from .llm.web_fetch import load_policy

    # Read once per run: a bad web configuration must stop the run here, not surface as a
    # refusal string inside an analyst's report halfway through.
    web_policy = load_policy(profiles).as_dict()
    # Which analyst is handed the Form 4 reader. Empty unless the source is switched on, and
    # then exactly one analyst by name.
    form4 = sources.load(profiles).form4
    sources_policy = ({"form4_analyst": form4.analyst, "contact_env": form4.contact_env}
                      if form4.enabled else {})

    def factory(provider: str, model: str, base_url: str | None = None, **kwargs: Any) -> BaseLLMClient:
        role = "deep" if model == profiles.tradingagents.deep_think_llm else "quick"
        effort = profiles.llm.deep_effort if role == "deep" else profiles.llm.quick_effort
        llm = ClaudeCodeChatModel(
            model=model,
            effort=effort,
            timeout_seconds=profiles.llm.timeout_seconds,
            max_turns=profiles.llm.max_turns,
            max_turns_ceiling=profiles.llm.max_turns_ceiling,
            claude_bin=profiles.llm.claude_bin,
            work_dir=work_dir or str(profiles.project_dir / "runs" / ".claude_workdir"),
            progress_path=progress_path,
            config_dir=str(profiles.config_dir),
            role=role,
            web_policy=web_policy,
            sources_policy=sources_policy,
            sources_log=sources_log,
            callbacks=kwargs.get("callbacks"),
        )
        return _ClaudeCodeClient(model, base_url, llm=llm)

    return factory


def classify_instrument(ticker: str, *, strict: bool = False) -> str:
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
    if strict and quote_type != "EQUITY":
        return "unsupported"
    return "stock"


class AgenticTradingGraph(TradingAgentsGraph):
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
        run_dir: Path | None = None,
        sources_log: Path | None = None,
    ):
        self.profiles = profiles
        self.portfolio_text = portfolio_text
        self.instrument_type = instrument_type
        self.bucket = bucket
        # Where this run's reads of outside sources are written down (agentic_trading.sources).
        # Beside the run's own results; None for a graph nobody keeps a record of.
        self.sources_log = sources_log or (run_dir / SOURCES_LOG_NAME if run_dir else None)
        selected = profiles.analysts_for(instrument_type)
        config = config or build_tradingagents_config(profiles)
        factory = llm_factory or make_llm_factory(profiles)

        import tradingagents.graph.trading_graph as upstream_module

        # Keep the per-call audit trail beside this run's results, so an offline
        # test writing to a temp results_dir cannot append to the real runs/ log.
        if llm_factory is None:
            factory = make_llm_factory(
                profiles, work_dir=str(Path(config["results_dir"]).parent / ".claude_workdir"),
                # Per run, beside that run's own reports: the console reads one file and
                # never has to separate two concurrent runs sharing a directory.
                progress_path=str(run_dir / "progress.jsonl") if run_dir else None,
                sources_log=str(self.sources_log) if self.sources_log else None,
            )

        with mock.patch.object(upstream_module, "create_llm_client", factory):
            super().__init__(selected_analysts=selected, debug=debug, config=config, callbacks=callbacks)

    def propagate(self, *args: Any, **kwargs: Any):
        """Upstream's run, with the outside sources it reads watched for its length.

        The date the run is an analysis of is put where a tool in another process can read it
        (``sources.AS_OF_ENV``), so that a source which must not read past that date does not
        depend on a model remembering to pass it. Put back when the run returns.
        """
        from .sources import AS_OF_ENV
        from .sources import reddit as reddit_source

        trade_date = kwargs.get("trade_date", args[1] if len(args) > 1 else None)
        before = os.environ.get(AS_OF_ENV)
        if trade_date:
            os.environ[AS_OF_ENV] = str(trade_date)
        try:
            with reddit_source.installed(self.profiles, log_path=self.sources_log):
                return super().propagate(*args, **kwargs)
        finally:
            if before is None:
                os.environ.pop(AS_OF_ENV, None)
            else:
                os.environ[AS_OF_ENV] = before

    # Upstream injects this string into every agent prompt (analysts, researchers,
    # managers, trader, risk debators, portfolio manager) and calls it from both the
    # CLI and propagate(), which makes it the one zero-modification place to add
    # the mandate and the portfolio context.
    def resolve_instrument_context(self, ticker: str, asset_type: str = "stock") -> str:
        base = super().resolve_instrument_context(ticker, asset_type)
        return (f"{base}\n\n{self.profiles.mandate_context()}\n\n{UNTRUSTED_CONTENT_NOTICE}"
                f"\n\n{self.portfolio_context()}")

    def portfolio_context(self) -> str:
        mode = self.profiles.bucket_modes.get(self.bucket, "recommend_only")
        snapshot = (self.portfolio_text + "\n") if self.portfolio_text else ""
        return snapshot + (
            f"PORTFOLIO CONTEXT: this analysis is for the {self.bucket.upper()} bucket "
            f"({self.instrument_type}); execution stage {self.profiles.stage}, mode {mode}. "
            "No order is placed from this analysis; it produces a recommendation record that "
            "deterministic risk rules and a human review before anything is ever executed."
        )
