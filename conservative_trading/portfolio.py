"""Conceptual portfolio state and mark-to-market snapshot (Stage 2, no broker).

The portfolio lives in a small YAML file the investor edits by hand. Prices,
instrument identity and sector come from the upstream TradingAgents data layer
(look-ahead safe, cached, keyless). The snapshot is the single portfolio view
that the agents' prompts, the policy step and the Hard Risk Layer all consume.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd
import yaml
from pydantic import BaseModel, ConfigDict, Field

from .profile import Profiles


class PortfolioError(ValueError):
    """Portfolio state is missing, malformed, stale or inconsistent: fail closed."""


class BucketCash(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cash_usd: float = Field(ge=0)


class Position(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str
    bucket: str
    quantity: float = Field(gt=0)
    cost_basis_per_share: float = Field(gt=0)
    opened_on: str | None = None


class RecentOpen(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str
    bucket: str
    date: str


class PortfolioState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    as_of: str
    currency: str = "USD"
    buckets: dict[str, BucketCash]
    positions: list[Position] = Field(default_factory=list)
    recent_opens: list[RecentOpen] = Field(default_factory=list)
    notes: str | None = None

    def position(self, symbol: str, bucket: str | None = None) -> Position | None:
        for p in self.positions:
            if p.symbol.upper() == symbol.upper() and (bucket is None or p.bucket == bucket):
                return p
        return None


def load_portfolio(path: str | Path, required_buckets: tuple[str, ...] | None = None) -> PortfolioState:
    """Load a bucket ledger. Each venue has its own; the conceptual portfolio and the
    Micro-Live account are never mixed into one cash reconciliation."""
    path = Path(path)
    if not path.exists():
        raise PortfolioError(f"portfolio state file not found: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise PortfolioError(f"portfolio state is not a mapping: {path}")
    data["as_of"] = str(data.get("as_of", ""))
    try:
        state = PortfolioState(**data)
    except Exception as exc:
        raise PortfolioError(f"invalid portfolio state: {exc}") from exc
    if not state.buckets:
        raise PortfolioError("portfolio state defines no buckets")
    for name in (required_buckets or ()):
        if name not in state.buckets:
            raise PortfolioError(f"portfolio state must define bucket {name!r}")
    seen: dict[str, str] = {}
    for p in state.positions:
        if p.bucket not in state.buckets:
            raise PortfolioError(f"position {p.symbol} references unknown bucket {p.bucket!r}")
        prior = seen.setdefault(p.symbol.upper(), p.bucket)
        if prior != p.bucket:
            raise PortfolioError(f"symbol {p.symbol} appears in both {prior} and {p.bucket}; one symbol, one bucket")
    return state


PriceLookup = Callable[[str, str], tuple[float, str]]
IdentityLookup = Callable[[str], dict[str, Any]]


def upstream_price_lookup(symbol: str, as_of: str) -> tuple[float, str]:
    """Latest close on or before ``as_of`` from the upstream cached OHLCV loader."""
    from tradingagents.dataflows.stockstats_utils import load_ohlcv

    df = load_ohlcv(symbol, as_of)
    if df is None or df.empty:
        raise PortfolioError(f"no price data for {symbol}")
    last = df.iloc[-1]
    return float(last["Close"]), str(pd.to_datetime(last["Date"]).date())


def upstream_identity_lookup(symbol: str) -> dict[str, Any]:
    from tradingagents.agents.utils.agent_utils import resolve_instrument_identity

    return resolve_instrument_identity(symbol) or {}


# Sleeve -> the position-limit class used by the risk profile. Structural, not policy.
LIMIT_CLASS_BY_SLEEVE = {
    "broad_equity_etf": "broad_diversified_etf",
    "bond_etf": "bond_or_cash_like_etf",
    "cash_like_etf": "bond_or_cash_like_etf",
}


@dataclass(frozen=True)
class InstrumentClass:
    symbol: str
    instrument_type: str           # etf | stock | crypto
    exposure_group: str | None     # from the investor universe, None if outside it
    sleeve: str | None             # broad_equity_etf | bond_etf | cash_like_etf | satellites | None (crypto)
    limit_class: str               # broad_diversified_etf | bond_or_cash_like_etf | sector_factor_thematic_etf | individual_stock | crypto
    sector: str | None = None


def classify_instrument(symbol: str, profiles: Profiles, identity_lookup: IdentityLookup) -> InstrumentClass:
    upper = symbol.upper()
    if upper.endswith("-USD") or "/" in upper:
        return InstrumentClass(upper, "crypto", None, None, "crypto")
    group = next(
        (g for g, tickers in profiles.exposure_groups().items() if upper in {t.upper() for t in tickers}), None
    )
    identity = identity_lookup(symbol) or {}
    quote_type = str(identity.get("quote_type", "")).upper()
    if quote_type == "CRYPTOCURRENCY":
        return InstrumentClass(upper, "crypto", None, None, "crypto")
    if group is not None or quote_type == "ETF":
        sleeve = profiles.sleeve_of_group(group or "unknown_etf")
        return InstrumentClass(upper, "etf", group, sleeve, LIMIT_CLASS_BY_SLEEVE.get(sleeve, "sector_factor_thematic_etf"))
    # Unknown identity resolves to the tightest class (stock) on purpose: fail toward caution.
    return InstrumentClass(upper, "stock", None, profiles.sleeve_of_group("individual_stock"),
                           "individual_stock", identity.get("sector"))


@dataclass
class PositionView:
    symbol: str
    bucket: str
    quantity: float
    price: float
    price_date: str
    market_value: float
    cost_basis: float
    unrealized_pct: float
    weight_pct_of_bucket: float
    cls: InstrumentClass


@dataclass
class BucketView:
    name: str
    allocation_usd: float
    cash_usd: float
    market_value: float
    equity: float
    deployed_cost_usd: float
    positions: list[PositionView] = field(default_factory=list)
    sleeves_pct: dict[str, float] = field(default_factory=dict)      # of allocation, cash included
    groups_held: dict[str, list[str]] = field(default_factory=dict)  # exposure group -> symbols
    sectors_pct: dict[str, float] = field(default_factory=dict)      # direct sector exposure (stocks + sector ETFs)
    stocks_pct: float = 0.0
    limit_class_pct: dict[str, float] = field(default_factory=dict)

    @property
    def n_positions(self) -> int:
        return len(self.positions)

    def weight_of(self, symbol: str) -> float:
        return next((p.weight_pct_of_bucket for p in self.positions if p.symbol == symbol.upper()), 0.0)

    def market_value_of(self, symbol: str) -> float:
        return next((p.market_value for p in self.positions if p.symbol == symbol.upper()), 0.0)


@dataclass
class Snapshot:
    as_of: str
    trade_date: str
    total_equity: float
    total_cash: float
    buckets: dict[str, BucketView]
    state: PortfolioState
    max_price_age_trading_days: int
    warnings: list[str] = field(default_factory=list)

    def has_position(self, symbol: str, bucket: str | None = None) -> bool:
        return self.state.position(symbol, bucket) is not None


def _trading_days_between(earlier: str, later: str) -> int:
    a, b = pd.to_datetime(earlier), pd.to_datetime(later)
    if a >= b:
        return 0
    return max(0, len(pd.bdate_range(a, b)) - 1)


def build_snapshot(
    state: PortfolioState,
    profiles: Profiles,
    trade_date: str,
    price_lookup: PriceLookup = upstream_price_lookup,
    identity_lookup: IdentityLookup = upstream_identity_lookup,
) -> Snapshot:
    ps = profiles.risk.get("portfolio_state") or {}
    max_age_days = int(ps.get("max_age_days", 7))
    max_price_age = int(ps.get("max_price_age_trading_days", 3))
    age_days = (pd.to_datetime(trade_date) - pd.to_datetime(state.as_of)).days
    if age_days > max_age_days:
        raise PortfolioError(f"portfolio state as_of {state.as_of} is {age_days} days older than {trade_date} (limit {max_age_days})")
    if age_days < -1:
        raise PortfolioError(f"portfolio state as_of {state.as_of} is after the trade date {trade_date}")

    buckets: dict[str, BucketView] = {}
    for name, cash in state.buckets.items():
        buckets[name] = BucketView(name=name, allocation_usd=profiles.capital_allocation(name),
                                   cash_usd=float(cash.cash_usd), market_value=0.0, equity=float(cash.cash_usd),
                                   deployed_cost_usd=0.0)
    warnings: list[str] = []
    max_price_age_seen = 0
    for p in state.positions:
        price, price_date = price_lookup(p.symbol, trade_date)
        age = _trading_days_between(price_date, trade_date)
        max_price_age_seen = max(max_price_age_seen, age)
        if age > max_price_age:
            raise PortfolioError(f"price for {p.symbol} is {age} trading days old (limit {max_price_age})")
        cls = classify_instrument(p.symbol, profiles, identity_lookup)
        mv = price * p.quantity
        cost = p.cost_basis_per_share * p.quantity
        view = PositionView(symbol=p.symbol.upper(), bucket=p.bucket, quantity=p.quantity, price=price,
                            price_date=price_date, market_value=mv, cost_basis=cost,
                            unrealized_pct=(mv / cost - 1.0) * 100.0 if cost else 0.0,
                            weight_pct_of_bucket=0.0, cls=cls)
        b = buckets[p.bucket]
        b.positions.append(view)
        b.market_value += mv
        b.deployed_cost_usd += cost

    for b in buckets.values():
        b.equity = b.cash_usd + b.market_value
        basis = b.allocation_usd if b.allocation_usd > 0 else b.equity
        sleeves: dict[str, float] = defaultdict(float)
        sectors: dict[str, float] = defaultdict(float)
        classes: dict[str, float] = defaultdict(float)
        groups: dict[str, list[str]] = defaultdict(list)
        for v in b.positions:
            v.weight_pct_of_bucket = v.market_value / basis * 100.0 if basis else 0.0
            if v.cls.sleeve:
                sleeves[v.cls.sleeve] += v.weight_pct_of_bucket
            classes[v.cls.limit_class] += v.weight_pct_of_bucket
            if v.cls.exposure_group:
                groups[v.cls.exposure_group].append(v.symbol)
            if v.cls.limit_class in ("individual_stock", "sector_factor_thematic_etf"):
                sectors[v.cls.sector or f"unclassified:{v.symbol}"] += v.weight_pct_of_bucket
        sleeves["cash"] = b.cash_usd / basis * 100.0 if basis else 0.0
        b.sleeves_pct = dict(sleeves)
        b.sectors_pct = dict(sectors)
        b.limit_class_pct = dict(classes)
        b.groups_held = dict(groups)
        b.stocks_pct = classes.get("individual_stock", 0.0)
        if b.name == "sandbox" and b.deployed_cost_usd > b.allocation_usd + 1e-6:
            warnings.append(f"sandbox deployed cost {b.deployed_cost_usd:.2f} exceeds its {b.allocation_usd:.0f} cap")

    total_equity = sum(b.equity for b in buckets.values())
    total_cash = sum(b.cash_usd for b in buckets.values())
    return Snapshot(as_of=state.as_of, trade_date=trade_date, total_equity=total_equity, total_cash=total_cash,
                    buckets=buckets, state=state, max_price_age_trading_days=max_price_age_seen, warnings=warnings)


def snapshot_from_broker(
    account,
    broker_positions,
    ledger: PortfolioState,
    profiles: Profiles,
    trade_date: str,
    identity_lookup: IdentityLookup = upstream_identity_lookup,
) -> Snapshot:
    """Build the snapshot from live broker state, with bucket attribution from the local ledger.

    From Stage 3 on, the broker is the source of truth for cash, quantities and prices; the
    ledger only answers the question the broker cannot: which bucket does this symbol belong
    to. Any disagreement fails closed rather than being papered over:

    * a position at the broker that the ledger does not know -> error
    * broker cash that does not match the sum of the ledger's bucket cash -> error
    """
    bucket_of = {p.symbol.upper(): p.bucket for p in ledger.positions}
    unknown = [p.symbol for p in broker_positions if p.symbol.upper() not in bucket_of]
    if unknown:
        raise PortfolioError(
            f"broker holds {', '.join(unknown)} but the bucket ledger does not; assign the symbol to a "
            "bucket in the ledger before trading (fail closed: bucket attribution cannot be guessed)"
        )
    ledger_cash = sum(b.cash_usd for b in ledger.buckets.values())
    tolerance = max(1.0, abs(account.cash) * 0.001)
    if abs(ledger_cash - account.cash) > tolerance:
        raise PortfolioError(
            f"cash mismatch: broker reports ${account.cash:,.2f}, the ledger's buckets sum to "
            f"${ledger_cash:,.2f} (tolerance ${tolerance:,.2f}); reconcile the ledger before trading"
        )

    positions, prices = [], {}
    for bp in broker_positions:
        symbol = bp.symbol.upper()
        positions.append(Position(symbol=symbol, bucket=bucket_of[symbol], quantity=bp.quantity,
                                  cost_basis_per_share=bp.average_entry_price))
        prices[symbol] = bp.market_value / bp.quantity if bp.quantity else bp.average_entry_price
    state = PortfolioState(as_of=trade_date, currency=ledger.currency, buckets=ledger.buckets,
                           positions=positions, recent_opens=ledger.recent_opens,
                           notes="reconciled from broker state")
    return build_snapshot(state, profiles, trade_date,
                          price_lookup=lambda sym, _d: (prices[sym.upper()], trade_date),
                          identity_lookup=identity_lookup)


def apply_fill_to_ledger(path: str | Path, *, symbol: str, bucket: str, side: str, quantity: float,
                        price: float, trade_date: str) -> PortfolioState:
    """Record a fill in the bucket ledger: bucket attribution and cash, written atomically.

    Quantities and prices are re-read from the broker on every later snapshot, so the ledger's
    job is only to answer what the broker cannot: which bucket owns the symbol, and how the
    cash is divided between buckets. Without this step the next revalidation would fail closed
    on an unattributed position, which is safe but would stop the pipeline after one fill.
    """
    path = Path(path)
    state = load_portfolio(path)
    symbol = symbol.upper()
    notional = quantity * price
    if bucket not in state.buckets:
        raise PortfolioError(f"cannot record a fill for unknown bucket {bucket!r}")

    existing = next((p for p in state.positions if p.symbol.upper() == symbol), None)
    if side == "buy":
        state.buckets[bucket].cash_usd = round(state.buckets[bucket].cash_usd - notional, 2)
        if existing is None:
            state.positions.append(Position(symbol=symbol, bucket=bucket, quantity=quantity,
                                            cost_basis_per_share=price, opened_on=trade_date))
            state.recent_opens.append(RecentOpen(symbol=symbol, bucket=bucket, date=trade_date))
        else:
            total = existing.quantity + quantity
            existing.cost_basis_per_share = round(
                (existing.quantity * existing.cost_basis_per_share + notional) / total, 6)
            existing.quantity = round(total, 6)
    else:
        if existing is None:
            raise PortfolioError(f"cannot record a sell of {symbol}: the ledger has no such position")
        state.buckets[bucket].cash_usd = round(state.buckets[bucket].cash_usd + notional, 2)
        remaining = round(existing.quantity - quantity, 6)
        if remaining <= 1e-6:
            state.positions = [p for p in state.positions if p.symbol.upper() != symbol]
        else:
            existing.quantity = remaining
    if state.buckets[bucket].cash_usd < -1e-6:
        raise PortfolioError(f"recording this fill would push {bucket} cash negative; refusing to write the ledger")

    state.as_of = trade_date
    payload = {
        "as_of": state.as_of, "currency": state.currency,
        "buckets": {name: {"cash_usd": b.cash_usd} for name, b in state.buckets.items()},
        "positions": [{"symbol": p.symbol, "bucket": p.bucket, "quantity": p.quantity,
                       "cost_basis_per_share": p.cost_basis_per_share, **({"opened_on": p.opened_on} if p.opened_on else {})}
                      for p in state.positions],
        "recent_opens": [{"symbol": o.symbol, "bucket": o.bucket, "date": o.date} for o in state.recent_opens[-20:]],
        "notes": f"updated by a {side} fill of {quantity:g} {symbol} @ {price:,.4f} on {trade_date}",
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    tmp.replace(path)
    return state


def render_snapshot(snap: Snapshot, ladder: dict[str, Any] | None = None, regime: dict[str, Any] | None = None) -> str:
    """Compact, deterministic text for agent prompts and the decision record."""
    lines = [f"PORTFOLIO SNAPSHOT (as of {snap.as_of}, priced for {snap.trade_date}): total equity ${snap.total_equity:,.2f}, cash ${snap.total_cash:,.2f}"]
    for b in snap.buckets.values():
        lines.append(f"- {b.name.upper()} bucket: allocation ${b.allocation_usd:,.0f}, cash ${b.cash_usd:,.2f}, "
                     f"positions market value ${b.market_value:,.2f}, {b.n_positions} position(s)")
        for v in b.positions:
            lines.append(f"    {v.symbol}: {v.quantity:g} @ ${v.price:,.2f} = ${v.market_value:,.2f} "
                         f"({v.weight_pct_of_bucket:.1f}% of allocation, {v.unrealized_pct:+.1f}% vs cost, "
                         f"{v.cls.limit_class}{', ' + v.cls.exposure_group if v.cls.exposure_group else ''})")
        if b.positions:
            sl = ", ".join(f"{k} {v:.1f}%" for k, v in sorted(b.sleeves_pct.items()))
            lines.append(f"    sleeves: {sl}")
    if ladder:
        lines.append(f"- Drawdown ladder: {ladder.get('total', {}).get('level', 'n/a')} (total drawdown "
                     f"{ladder.get('total', {}).get('drawdown_pct', 0.0):+.2f}% from high-water mark)")
    if regime:
        lines.append(f"- Market regime (deterministic, keyless): {regime.get('level')} — {regime.get('detail', '')}")
    if snap.warnings:
        lines.append("- WARNINGS: " + "; ".join(snap.warnings))
    return "\n".join(lines)
