"""Conceptual portfolio state and mark-to-market snapshot (Stage 2, no broker).

The portfolio is stored transactionally in SQLite; an existing YAML ledger is imported once.
Prices, instrument identity and sector come from the upstream TradingAgents data layer
(look-ahead safe, cached, keyless). The snapshot is the single portfolio view that the agents'
prompts, the policy step and the Hard Risk Layer all consume.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import yaml
from pydantic import BaseModel, ConfigDict, Field

from .profile import Profiles
from .state_db import StateDB


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


class RealizedTrade(BaseModel):
    """What a sale actually gained or lost, recorded when it happened.

    The ledger kept unrealized marks and nothing else, so "how much has today cost" had no
    answer, and every rule that needs one — a daily loss gate above all — could not be written
    at all. Written at the moment of the fill, against the cost basis the position has right
    then, because closing a position discards that basis and averaging down moves it.
    """

    model_config = ConfigDict(extra="forbid")
    date: str
    symbol: str
    bucket: str
    quantity: float
    price: float
    cost_basis_per_share: float
    realized_usd: float                   # negative is a loss
    client_order_id: str | None = None


class CashFlow(BaseModel):
    """Money entering or leaving a bucket, which is never a gain or a loss.

    Without this record the only way to explain a rise in equity is to call it performance, and
    a funded account then reports its own deposits as return.
    """

    model_config = ConfigDict(extra="forbid")
    date: str
    bucket: str
    amount_usd: float                     # positive funds the bucket, negative takes money out
    kind: str = "deposit"
    note: str | None = None


class PortfolioState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    as_of: str
    currency: str = "USD"
    buckets: dict[str, BucketCash]
    positions: list[Position] = Field(default_factory=list)
    recent_opens: list[RecentOpen] = Field(default_factory=list)
    realized: list[RealizedTrade] = Field(default_factory=list)
    cash_flows: list[CashFlow] = Field(default_factory=list)
    notes: str | None = None
    applied_orders: dict[str, Any] = Field(default_factory=dict)

    def position(self, symbol: str, bucket: str | None = None) -> Position | None:
        for p in self.positions:
            if p.symbol.upper() == symbol.upper() and (bucket is None or p.bucket == bucket):
                return p
        return None


def load_portfolio(path: str | Path, required_buckets: tuple[str, ...] | None = None) -> PortfolioState:
    """Load a bucket ledger. Each venue has its own; the conceptual portfolio and the
    Micro-Live account are never mixed into one cash reconciliation."""
    path = Path(path)
    stored = StateDB(path).read_document(path)
    if stored is not None:
        data = stored
    elif path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    else:
        raise PortfolioError(f"portfolio state file not found: {path}")
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


def migrate_portfolio(path: str | Path) -> PortfolioState:
    """Copy a legacy YAML ledger into the canonical database once."""
    path = Path(path)
    db = StateDB(path)
    with db.transaction() as conn:
        stored = db.read_document(path)
        if stored is not None:
            return PortfolioState(**stored)
        state = load_portfolio(path)
        raw = path.read_bytes() if path.exists() else b""
        db.write_document(path, state.model_dump(mode="json"), conn)
        db.mark_migrated(conn, path, "portfolio", raw)
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

    identity = dict(resolve_instrument_identity(symbol) or {})
    # Upstream deliberately returns a compact identity. Product-structure screening also
    # needs the fund category/name fields from the same underlying yfinance source.
    try:
        import yfinance as yf
        from tradingagents.dataflows.symbol_utils import normalize_symbol

        info = yf.Ticker(normalize_symbol(symbol)).info or {}
        for key in ("longName", "shortName", "category", "fundFamily", "legalType", "quoteType"):
            if info.get(key) not in (None, ""):
                identity[key] = info[key]
        identity.setdefault("quote_type", info.get("quoteType", ""))
    except Exception:  # noqa: BLE001 - missing metadata becomes unverified or uses the explicit allowlist
        return identity
    return identity


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
    product_flags: tuple[str, ...] = ()
    identity_verified: bool = False


def classify_instrument(symbol: str, profiles: Profiles, identity_lookup: IdentityLookup) -> InstrumentClass:
    upper = symbol.upper()
    if upper.endswith("-USD") or "/" in upper:
        return InstrumentClass(upper, "crypto", None, None, "crypto")
    group = next(
        (g for g, tickers in profiles.exposure_groups().items() if upper in {t.upper() for t in tickers}), None
    )
    identity = identity_lookup(symbol) or {}
    quote_type = str(identity.get("quote_type", "")).upper()
    def asserted(value: Any) -> bool:
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "y")
        return value is True or isinstance(value, (int, float)) and value != 0

    flags = []
    for key in ("leveraged", "inverse", "single_stock", "crypto_etf", "commodity_futures", "covered_call"):
        if asserted(identity.get(key)) or asserted(identity.get("is_" + key)):
            flags.append(key)
    identity_text = " ".join(str(identity.get(key, "")) for key in (
        "company_name", "longName", "shortName", "name", "category", "fundFamily", "legalType", "description"
    )).lower()
    text_markers = {
        "leveraged": ("leveraged", "ultrapro", "2x daily", "3x daily"),
        "inverse": ("inverse", "ultrashort", "-1x", "-2x", "-3x"),
        "single_stock": ("single-stock", "single stock"),
        "crypto_etf": ("digital assets", "bitcoin etf", "ether etf", "ethereum etf"),
        "commodity_futures": ("commodity futures", "managed futures", "commodities focused"),
        "covered_call": ("covered call", "derivative income"),
    }
    for flag, markers in text_markers.items():
        if any(marker in identity_text for marker in markers):
            flags.append(flag)
    leverage = identity.get("leverage", identity.get("leverage_ratio", 1))
    try:
        if abs(float(leverage)) != 1 or float(leverage) < 0:
            flags.append("leverage")
    except (TypeError, ValueError):
        flags.append("unknown_leverage")
    if quote_type == "CRYPTOCURRENCY":
        return InstrumentClass(upper, "crypto", None, None, "crypto")
    if group is not None or quote_type == "ETF":
        sleeve = profiles.sleeve_of_group(group or "unknown_etf")
        return InstrumentClass(upper, "etf", group, sleeve, LIMIT_CLASS_BY_SLEEVE.get(sleeve, "sector_factor_thematic_etf"),
                               product_flags=tuple(dict.fromkeys(flags)), identity_verified=group is not None)
    # Unknown identity resolves to the tightest class (stock) on purpose: fail toward caution.
    return InstrumentClass(upper, "stock", None, profiles.sleeve_of_group("individual_stock"),
                           "individual_stock", identity.get("sector"), tuple(dict.fromkeys(flags)),
                           quote_type in ("EQUITY", "STOCK"))


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
    classes_pct: dict[str, float] = field(default_factory=dict)      # of allocation, by limit class
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
        if profiles.risk_cfg("buckets", b.name, "sizing_basis", default="") == "live_equity":
            # Every limit in the engine is a percentage of allocation_usd, so pointing that at
            # the money actually in the account is the whole change. Caps then follow a deposit
            # without a config edit, and — the half that matters more — they shrink on their own
            # after a loss, with nobody having to remember to tighten them.
            b.allocation_usd = b.equity
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
        b.classes_pct = dict(classes)
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
                           cash_flows=ledger.cash_flows, applied_orders=ledger.applied_orders,
                           realized=ledger.realized, notes="reconciled from broker state")
    return build_snapshot(state, profiles, trade_date,
                          price_lookup=lambda sym, _d: (prices[sym.upper()], trade_date),
                          identity_lookup=identity_lookup)


def realized_on(state: PortfolioState, bucket: str, trade_date: str) -> float:
    """Realized profit and loss for one bucket on one day. Negative is a loss.

    Deliberately realized only. A daily loss gate measured on unrealized marks would fire on a
    market that moved, which is not something stopping trading fixes; measured on closed trades
    it fires on what this system actually did.
    """
    return round(sum(r.realized_usd for r in state.realized
                     if r.bucket == bucket and r.date == trade_date), 6)


def cash_flow_summary(state: PortfolioState) -> dict[str, float]:
    """Net deposits per bucket, from the flows actually recorded. Empty when none were."""
    totals: dict[str, float] = defaultdict(float)
    for flow in state.cash_flows:
        totals[flow.bucket] += float(flow.amount_usd)
    return {bucket: round(value, 2) for bucket, value in totals.items()}


def record_cash_flow(path: str | Path, *, bucket: str, amount_usd: float, kind: str = "deposit",
                     date: str | None = None, note: str = "") -> PortfolioState:
    """Fund or defund a bucket in one durable commit, moving cash and recording the reason."""
    import math

    path = Path(path)
    if kind not in ("deposit", "withdrawal"):
        raise ValueError("kind must be deposit or withdrawal")
    if not math.isfinite(amount_usd) or amount_usd <= 0:
        raise ValueError("amount must be a positive finite number of dollars")
    signed = amount_usd if kind == "deposit" else -amount_usd
    db = StateDB(path)
    with db.transaction() as conn:
        state = load_portfolio(path)
        if bucket not in state.buckets:
            raise KeyError(f"unknown bucket {bucket!r}")
        moved = round(state.buckets[bucket].cash_usd + signed, 2)
        if moved < 0:
            raise ValueError(f"{bucket} holds ${state.buckets[bucket].cash_usd:,.2f}; "
                             f"cannot withdraw ${amount_usd:,.2f}")
        state.buckets[bucket].cash_usd = moved
        state.cash_flows.append(CashFlow(
            date=date or datetime.now(UTC).date().isoformat(), bucket=bucket,
            amount_usd=round(signed, 2), kind=kind, note=note or None))
        state.notes = f"{kind} of ${amount_usd:,.2f} into {bucket}"
        db.write_document(path, state.model_dump(mode="json"), conn)
        if not db.migrated(path):
            raw = path.read_bytes() if path.exists() else b""
            db.mark_migrated(conn, path, "portfolio", raw)
        return state


def apply_fill_to_ledger(path: str | Path, *, symbol: str, bucket: str, side: str, quantity: float,
                         price: float, trade_date: str, client_order_id: str | None = None) -> PortfolioState:
    """Apply a fill, or a cumulative order observation, in one durable ledger commit.

    The cumulative cursor, cash and positions live in the same SQLite transaction as the
    proposal and fill audit event when this is called by the execution reconciler.
    """
    import math

    path = Path(path)
    db = StateDB(path)
    with db.transaction() as conn:
        state = load_portfolio(path)
        symbol = symbol.upper()
        if side not in ("buy", "sell") or not all(math.isfinite(x) and x > 0 for x in (quantity, price)):
            raise PortfolioError("fill requires a valid side, positive finite quantity and price")
        if bucket not in state.buckets:
            raise PortfolioError(f"cannot record a fill for unknown bucket {bucket!r}")
        total_quantity, total_notional = quantity, quantity * price
        previous = state.applied_orders.get(client_order_id, {}) if client_order_id else {}
        if previous:
            if (previous["symbol"], previous["bucket"], previous["side"]) != (symbol, bucket, side):
                raise PortfolioError("order identity changed during reconciliation")
            quantity -= previous["quantity"]
            delta_notional = total_notional - previous["notional"]
            if quantity < -1e-8 or delta_notional < -0.005:
                raise PortfolioError("broker cumulative fill moved backwards; manual reconciliation required")
            if quantity <= 1e-8:
                if abs(delta_notional) > 0.005:
                    raise PortfolioError("broker corrected a filled price; manual reconciliation required")
                return state
            if delta_notional <= 0:
                raise PortfolioError("additional fill has no positive consideration")
            price = delta_notional / quantity
        notional = quantity * price
        existing = state.position(symbol)
        if existing is not None and existing.bucket != bucket:
            raise PortfolioError("fill belongs to a different bucket than the existing position")
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
                existing.quantity = round(total, 8)
        else:
            if existing is None or quantity > existing.quantity + 1e-8:
                raise PortfolioError(f"cannot record sell: insufficient {symbol} held in {bucket}")
            state.buckets[bucket].cash_usd = round(state.buckets[bucket].cash_usd + notional, 2)
            # Before the position is touched: the basis this sale is measured against is the one
            # it carries right now, and the next two lines can discard it.
            state.realized.append(RealizedTrade(
                date=trade_date, symbol=symbol, bucket=bucket, quantity=quantity, price=price,
                cost_basis_per_share=existing.cost_basis_per_share,
                realized_usd=round((price - existing.cost_basis_per_share) * quantity, 6),
                client_order_id=client_order_id))
            remaining = round(existing.quantity - quantity, 8)
            if remaining <= 1e-8:
                state.positions = [p for p in state.positions if p.symbol.upper() != symbol]
            else:
                existing.quantity = remaining
        if state.buckets[bucket].cash_usd < -1e-6:
            raise PortfolioError(f"recording fill would push {bucket} cash negative")
        if client_order_id:
            events = list(previous.get("events", []))
            events.append({"fill_id": f"{client_order_id}:{total_quantity:.8f}",
                           "quantity": quantity, "price": price, "notional": notional,
                           "trade_date": trade_date})
            state.applied_orders[client_order_id] = {
                "symbol": symbol, "bucket": bucket, "side": side,
                "quantity": total_quantity, "notional": total_notional, "events": events,
            }
        state.as_of = trade_date
        state.notes = f"updated by {side} fill of {quantity:g} {symbol} @ {price:,.4f}"
        db.write_document(path, state.model_dump(mode="json"), conn)
        if not db.migrated(path):
            raw = path.read_bytes() if path.exists() else b""
            db.mark_migrated(conn, path, "portfolio", raw)
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
