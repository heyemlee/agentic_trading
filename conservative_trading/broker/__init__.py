import logging
from pathlib import Path

from .base import (
    Account,
    BrokerAdapter,
    BrokerError,
    BrokerOrder,
    BrokerPosition,
    BrokerRejection,
    BrokerUnavailable,
    Clock,
    DuplicateOrder,
    OrderRequest,
)
from .simulated import SimulatedBroker

__all__ = [
    "Account", "BrokerAdapter", "BrokerError", "BrokerOrder", "BrokerPosition", "BrokerRejection",
    "BrokerUnavailable", "Clock", "DuplicateOrder", "OrderRequest", "Quote", "SimulatedBroker",
]


def make_broker(profiles, **kwargs) -> BrokerAdapter:
    """Build the broker named in the risk profile. Paper only below stage 5."""
    cfg = profiles.risk.get("broker") or {}
    name_early = str(cfg.get("adapter", "simulated")).lower()
    if name_early == "robinhood":
        # Robinhood Agentic Trading is real money. It is gated by its own master switch, not by
        # the paper/live environment flag, and never reached below the Micro-Live stage.
        micro = profiles.risk.get("micro_live_execution") or {}
        if not bool(micro.get("enabled", False)):
            raise BrokerError("Robinhood is a real-money venue and micro_live_execution.enabled is false")
    name = name_early
    if name == "simulated":
        # Seeded from the bucket ledger so the simulator mirrors the configured portfolio,
        # and allowed to price unseen symbols from the upstream data layer.
        if "cash" not in kwargs and "positions" not in kwargs:
            try:
                from ..portfolio import load_portfolio

                ps = profiles.risk.get("portfolio_state") or {}
                ledger = load_portfolio(profiles.project_dir / str(ps.get("file", "state/portfolio.yaml")))
                kwargs.setdefault("cash", sum(b.cash_usd for b in ledger.buckets.values()))
                kwargs.setdefault("positions", {p.symbol: (p.quantity, p.cost_basis_per_share)
                                                for p in ledger.positions})
            except Exception as exc:  # noqa: BLE001 - an unreadable ledger leaves the simulator empty
                logging.getLogger(__name__).debug("simulator could not seed from the ledger: %s", exc)
        kwargs.setdefault("autoprice", True)
        state_file = profiles.project_dir / str(cfg.get("state_file", "state/proposals.json"))
        kwargs.setdefault("order_state_path", Path(state_file).with_name("simulated_orders.json"))
        return SimulatedBroker(**kwargs)
    if name == "robinhood":
        from .robinhood import RobinhoodBroker

        return RobinhoodBroker(profiles, **kwargs)
    raise BrokerError(f"unknown broker adapter {name!r}")
