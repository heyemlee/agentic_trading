"""Approval-time revalidation and paper execution.

The rule this module exists to enforce: approval is permission to execute the idea only if
the current world still supports it. Nothing here trusts the proposal's own numbers. Every
fast-moving input (price, cash, positions, open orders, session, drawdown, regime) is read
again, the deterministic Hard Risk Layer runs again against that fresh state, and only then
may an order be sent.

Outcomes: VALID, REDUCED, STALE, INVALIDATED, REANALYZE, REJECTED.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import asdict, dataclass, field
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .broker.base import (
    BrokerAdapter,
    BrokerError,
    BrokerOrder,
    BrokerRejection,
    BrokerUnavailable,
    DuplicateOrder,
    OrderRequest,
)
from .portfolio import (
    PortfolioError,
    Snapshot,
    apply_fill_to_ledger,
    classify_instrument,
    load_portfolio,
    snapshot_from_broker,
)
from .profile import Profiles
from .proposals import (
    APPROVED,
    CANCELED,
    EXECUTED,
    EXPIRED,
    INVALIDATED,
    NEEDS_REANALYSIS,
    PARTIALLY_FILLED,
    PENDING_EXECUTION,
    REJECTED,
    SUBMITTED,
    SUBMITTING,
    UNKNOWN,
    Proposal,
    ProposalStore,
    client_order_id,
    positions_fingerprint,
    utcnow,
)
from .risk import (
    HardRiskEngine,
    assess_regime,
    evaluate_ladders,
    load_ladder_states,
    save_ladder_states,
)
from .risk import Proposal as RiskProposal
from .risk.ladder import ORDER as LADDER_ORDER
from .state_db import StateConflict, StateDB
from .storage import file_lock

REGIME_ORDER = ["LOW_RISK", "NORMAL", "ELEVATED_RISK", "HIGH_RISK", "UNKNOWN"]

# Revalidation outcomes
OUT_VALID = "VALID"
OUT_REDUCED = "REDUCED"
OUT_STALE = "STALE"
OUT_INVALIDATED = "INVALIDATED"
OUT_REANALYZE = "REANALYZE"
OUT_REJECTED = "REJECTED"


@dataclass
class Revalidation:
    outcome: str
    reasons: list[str] = field(default_factory=list)
    refreshed: dict[str, Any] = field(default_factory=dict)
    changes: list[str] = field(default_factory=list)
    hard_risk: dict[str, Any] | None = None
    executable_notional_usd: float = 0.0
    executable_quantity: float | None = None
    limit_price: float | None = None
    current_price: float | None = None
    price_drift_pct: float | None = None

    @property
    def may_execute(self) -> bool:
        return self.outcome in (OUT_VALID, OUT_REDUCED) and self.executable_notional_usd > 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _venue_price(broker: BrokerAdapter, symbol: str, profiles: Profiles) -> tuple[float | None, str]:
    """Current price from the execution venue, age-checked against the fast-data window.

    Deliberately not the upstream daily close: a daily bar is the right input for research and
    the wrong input for pricing an order. If the venue cannot price the symbol, the caller
    fails closed rather than reaching for slower data.
    """
    fast = ((profiles.risk.get("data_freshness") or {}).get("fast") or {})
    max_age = float(fast.get("max_age_seconds", 120))
    try:
        quote = broker.get_quote(symbol)
    except BrokerError as exc:
        return None, f"the venue could not price {symbol}: {exc}"
    as_of = quote.as_of
    if as_of is None or as_of.tzinfo is None:
        return None, "venue quote has no timezone-aware market timestamp"
    age = (utcnow() - as_of).total_seconds()
    if age > max_age or age < -5:
        return None, f"venue market timestamp is stale or in the future ({age:.0f}s; limit {max_age:.0f}s)"
    if quote.symbol.upper() != symbol.upper() or not math.isfinite(quote.price) or quote.price <= 0:
        return None, "venue returned an invalid price or a different symbol"
    if (quote.bid is None or quote.ask is None or not all(math.isfinite(x) and x > 0 for x in (quote.bid, quote.ask))
            or quote.ask < quote.bid):
        return None, "venue returned missing, invalid or crossed bid/ask prices"
    spread = quote.spread_pct
    max_spread = float(fast.get("max_spread_pct", 1.0))
    if spread is None or spread > max_spread:
        return None, f"venue spread exceeds {max_spread:g}%"
    detail = f"venue price {quote.price:,.4f} ({quote.source}"
    detail += f", spread {spread:.2f}%)" if spread is not None else ")"
    return quote.price, detail


def revalidate(profiles: Profiles, proposal: Proposal, broker: BrokerAdapter, *,
               ledger_path: str | None = None, now=None, persist_risk: bool = True) -> Revalidation:
    """Re-read the world, rerun Hard Risk, and decide whether the old idea may still execute."""
    rv = Revalidation(outcome=OUT_INVALIDATED)
    halt = profiles.project_dir / str(profiles.risk_cfg(
        "execution_policy", "kill_switch", "halt_file", default="state/HALT"))
    if halt.exists() or profiles.risk_cfg("execution_policy", "kill_switch", "trading_halted", default=False):
        rv.outcome = OUT_REJECTED
        rv.reasons.append("execution is paused; no order may be submitted")
        return rv

    # The bucket decides which venue may execute it. A conceptual bucket must never reach a
    # real-money venue, and the real-money bucket must never execute anywhere else.
    from .micro_live import bucket_is_executable

    bound, bound_why = bucket_is_executable(profiles, proposal.bucket, getattr(broker, "name", "unknown"))
    rv.reasons.append(f"venue binding: {bound_why}")
    if not bound:
        rv.outcome = OUT_REJECTED
        return rv

    rules = proposal.freshness_rules(profiles)
    reval_cfg = profiles.risk.get("revalidation") or {}

    # ---- 1. the proposal's own clocks
    state, why = proposal.freshness(profiles, now)
    rv.reasons.append(f"freshness: {why}")
    if state == EXPIRED:
        rv.outcome = OUT_STALE
        rv.reasons.append("expired proposals are never executed; run a fresh analysis")
        return rv
    if proposal.status != APPROVED and proposal.requires_human_approval:
        rv.outcome = OUT_REJECTED
        rv.reasons.append(f"status is {proposal.status}: human approval is required before execution")
        return rv
    expired, approval_why = proposal.approval_expired(profiles, now)
    rv.reasons.append(f"approval: {approval_why}")
    if expired:
        rv.outcome = OUT_STALE
        rv.reasons.append("the approval itself went stale; re-approve against current conditions")
        return rv

    # ---- 2. fast-moving state, straight from the broker
    try:
        account = broker.get_account()
        positions = broker.get_positions()
        open_orders = broker.get_open_orders()
        clock = broker.get_clock()
    except BrokerError as exc:
        rv.outcome = OUT_INVALIDATED
        rv.reasons.append(f"broker state unavailable or inconsistent: {exc}")
        return rv
    rv.refreshed = {
        "account_cash": account.cash, "account_equity": account.equity, "buying_power": account.buying_power,
        "positions": {p.symbol: p.quantity for p in positions}, "open_orders": len(open_orders),
        "market_open": clock.is_open, "read_at": utcnow().isoformat(),
    }
    if account.trading_blocked:
        rv.reasons.append("the broker reports the account as blocked from trading")
        return rv
    if bool(rules.get("require_market_open", True)) and not clock.is_open:
        rv.outcome = OUT_STALE
        rv.reasons.append("the market is closed; a day order would be rejected, so nothing is submitted")
        return rv
    if any(o.symbol == proposal.ticker and o.is_open for o in open_orders):
        rv.reasons.append(f"an open order for {proposal.ticker} already exists at the broker")
        return rv

    # ---- 3. price drift against the class band
    price, price_detail = _venue_price(broker, proposal.ticker, profiles)
    rv.reasons.append(price_detail)
    if price is None:
        return rv
    rv.current_price = price
    rv.refreshed["price"] = price
    if proposal.action in ("BUY", "ADD") and (
            proposal.reference_price is None or not math.isfinite(proposal.reference_price)
            or proposal.reference_price <= 0):
        rv.outcome = OUT_REANALYZE
        rv.reasons.append("the proposal has no valid research reference price; create a fresh analysis")
        return rv
    if proposal.reference_price:
        drift = (price / proposal.reference_price - 1.0) * 100.0
        rv.price_drift_pct = drift
        band = float(rules.get("max_price_drift_pct", 3.0))
        rv.reasons.append(f"price {price:,.2f} vs {proposal.reference_price:,.2f} at analysis: {drift:+.2f}% (band ±{band:.1f}%)")
        if abs(drift) > band:
            rv.changes.append("price_drift_beyond_class_band")
            rv.outcome = OUT_REANALYZE
            rv.reasons.append("the move is larger than this strategy tolerates; the thesis needs a fresh analysis, "
                              "not a re-priced old order")
            return rv

    # ---- 4. portfolio, ladder and regime, rebuilt from broker truth
    ledger = load_portfolio(ledger_path or _ledger_for(profiles, proposal.bucket))
    try:
        snap: Snapshot = snapshot_from_broker(account, positions, ledger, profiles, proposal.trade_date)
    except PortfolioError as exc:
        rv.reasons.append(f"portfolio could not be reconciled with the broker: {exc}")
        return rv

    fingerprint = positions_fingerprint({p.symbol: p.quantity for p in positions})
    if proposal.positions_fingerprint and fingerprint != proposal.positions_fingerprint:
        rv.changes.append("position_appeared_or_disappeared")
        rv.reasons.append(f"the position book changed since the analysis ({proposal.positions_fingerprint} -> {fingerprint})")
        rv.outcome = OUT_REANALYZE
        return rv

    # Each portfolio keeps its own drawdown history: the conceptual core/sandbox book, and the
    # real-money Micro-Live account, which must never be judged against the other's high-water mark.
    from .micro_live import BUCKET as MICRO_BUCKET

    ps = profiles.risk.get("portfolio_state") or {}
    if proposal.bucket == MICRO_BUCKET:
        micro_cfg = profiles.risk.get("micro_live_execution") or {}
        ladder_path = profiles.project_dir / str(micro_cfg.get("risk_state_file", "state/micro_live_risk_state.json"))
    else:
        ladder_path = profiles.project_dir / str(ps.get("risk_state_file", "state/risk_state.json"))
    def refresh_ladders():
        current = load_ladder_states(ladder_path, profiles)
        micro_equity = snap.buckets[MICRO_BUCKET].equity if MICRO_BUCKET in snap.buckets else None
        evaluate_ladders(current, profiles, proposal.trade_date,
                         snap.total_equity if "core" in snap.buckets else (micro_equity or snap.total_equity),
                         snap.buckets["core"].equity if "core" in snap.buckets else (micro_equity or 0.0),
                         snap.buckets["sandbox"].equity if "sandbox" in snap.buckets else 0.0,
                         micro_live_equity=micro_equity)
        if persist_risk:
            save_ladder_states(ladder_path, current)
        return current

    if persist_risk:
        with StateDB(ladder_path).transaction():
            ladders = refresh_ladders()
    else:
        ladders = refresh_ladders()
    regime = assess_regime(profiles, proposal.trade_date)
    rv.refreshed["ladder_level"] = ladders.for_bucket(proposal.bucket).level if proposal.bucket != "core" else ladders.total.level
    rv.refreshed["regime"] = regime.level

    prior_ladder = proposal.ladder_level_at_proposal
    current_ladder = ladders.for_bucket(proposal.bucket).level if proposal.bucket != "core" else ladders.total.level
    if (prior_ladder in LADDER_ORDER and current_ladder in LADDER_ORDER
            and LADDER_ORDER.index(current_ladder) > LADDER_ORDER.index(prior_ladder)):
            rv.changes.append("ladder_level_escalated_since_proposal")
            rv.outcome = OUT_INVALIDATED
            rv.reasons.append(f"the {proposal.bucket} drawdown ladder escalated from "
                              f"{proposal.ladder_level_at_proposal} to {current_ladder} since the analysis")
            return rv
    if (proposal.regime_at_proposal in REGIME_ORDER and regime.level in REGIME_ORDER
            and REGIME_ORDER.index(regime.level) > REGIME_ORDER.index(proposal.regime_at_proposal)):
            rv.changes.append("regime_worsened_since_proposal")
            rv.outcome = OUT_INVALIDATED
            rv.reasons.append(f"the market regime worsened from {proposal.regime_at_proposal} to {regime.level}")
            return rv

    # ---- 5. Hard Risk again, against the fresh state
    cls = classify_instrument(proposal.ticker, profiles, lambda s: {
        "quote_type": "ETF" if proposal.instrument_type == "etf" else
                      ("CRYPTOCURRENCY" if proposal.instrument_type == "crypto" else "EQUITY")})
    risk_proposal = RiskProposal(
        ticker=proposal.ticker, bucket=proposal.bucket, action=proposal.action, size_hint=proposal.size_hint,
        confidence=proposal.confidence, trade_date=proposal.trade_date, cls=cls,
        data_quality=proposal.data_quality, source="execution_revalidation", strategy=proposal.strategy,
        sell_quantity=proposal.sell_quantity,
        missing_information=proposal.missing_information,
        evidence=proposal.evidence,
    )
    result = HardRiskEngine(profiles).evaluate(risk_proposal, snap, ladders, regime)
    rv.hard_risk = result.as_dict()
    if result.verdict != "APPROVE":
        rv.outcome = OUT_REJECTED
        rv.reasons.append(f"Hard Risk rejects at execution time: {result.verdict}"
                          + (f" ({', '.join(r.rule_id for r in result.failed_rules)})" if result.failed_rules else ""))
        return rv

    # ---- 6. size: the fresh sizing wins, and may only shrink the approved idea
    if proposal.action in ("SELL", "REDUCE"):
        approved_quantity = proposal.sell_quantity
        if approved_quantity is None and proposal.notional_usd > 0 and proposal.reference_price:
            approved_quantity = proposal.notional_usd / proposal.reference_price
        if not approved_quantity or approved_quantity <= 0:
            rv.outcome = OUT_REANALYZE
            rv.reasons.append("this legacy reduction has no approved share quantity; create a fresh proposal")
            return rv
        quantity = min(result.proposed_quantity or 0.0, approved_quantity)
        quantity = float(Decimal(str(quantity)).quantize(Decimal("0.000001"), rounding=ROUND_DOWN))
        if quantity <= 0:
            rv.reasons.append("no sellable quantity remains at the venue's supported precision")
            return rv
        rv.executable_quantity = quantity
        rv.executable_notional_usd = quantity * price
        rv.outcome = OUT_REDUCED if quantity < approved_quantity - 1e-6 else OUT_VALID
        band = float((profiles.risk.get("broker") or {}).get("marketable_limit_band_pct", {}).get(
            proposal.instrument_type, 0.5))
        rv.limit_price = round(price * (1 - band / 100), 2)
        rv.reasons.append(f"sell {quantity:g} shares, capped by approved and currently held quantities")
        return rv
    fresh_notional = result.proposed_notional_usd
    approved = proposal.notional_usd or fresh_notional
    executable = min(fresh_notional, approved)
    min_ratio = float(reval_cfg.get("min_reduction_ratio", 0.5))
    min_order = float((profiles.risk.get("sizing") or {}).get("min_order_usd", 25))
    if executable < min_order:
        rv.outcome = OUT_INVALIDATED
        rv.reasons.append(f"the executable size ${executable:,.2f} is below the ${min_order:,.0f} minimum")
        return rv
    if executable < approved * min_ratio:
        rv.outcome = OUT_INVALIDATED
        rv.reasons.append(f"only ${executable:,.2f} of the approved ${approved:,.2f} still fits "
                          f"({executable / approved:.0%} < {min_ratio:.0%}); this is a materially different "
                          "idea and needs a new proposal and approval")
        return rv

    rv.executable_notional_usd = round(executable, 2)
    rv.outcome = OUT_REDUCED if executable < approved - 0.01 else OUT_VALID
    if rv.outcome == OUT_REDUCED:
        rv.changes.append("size_reduced")
        rv.reasons.append(f"size reduced from ${approved:,.2f} to ${executable:,.2f} to fit current state")
    band = float((profiles.risk.get("broker") or {}).get("marketable_limit_band_pct", {}).get(
        proposal.instrument_type, 0.5))
    side_sign = 1.0 if proposal.action in ("BUY", "ADD") else -1.0
    rv.limit_price = round(price * (1 + side_sign * band / 100.0), 2)
    rv.reasons.append(f"marketable limit {rv.limit_price:,.2f} ({band:.1f}% band on {price:,.2f})")
    return rv


@dataclass
class ExecutionResult:
    submitted: bool
    outcome: str
    proposal_id: str
    revalidation: dict[str, Any]
    order: dict[str, Any] | None = None
    error: str | None = None
    duplicate_prevented: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def needs_reconciliation(proposal: Proposal) -> bool:
    return bool(proposal.client_order_id and (
        proposal.status in PENDING_EXECUTION or proposal.reconciliation_error
        or (not proposal.reconciled_at and proposal.status in (APPROVED, EXECUTED))))


def execute(profiles: Profiles, proposal: Proposal, broker: BrokerAdapter, store: ProposalStore, *,
            ledger_path: str | None = None, dry_run: bool = False) -> ExecutionResult:
    """Serialize approval/execution transitions; retries only look up an existing intent."""
    account_key = "micro_live" if proposal.bucket == "micro_live" else "conceptual"
    with file_lock(store.db.path.with_name(f"{account_key}.execution")):
        current = store.get(proposal.proposal_id)
        if current is not None:
            proposal = current
        if needs_reconciliation(proposal):
            return reconcile_proposal(profiles, proposal, broker, store, ledger_path=ledger_path)
        if proposal.status == EXECUTED:
            return ExecutionResult(False, "ALREADY_EXECUTED", proposal.proposal_id, {},
                                   duplicate_prevented=True)
        if proposal.status != APPROVED:
            return ExecutionResult(False, "REJECTED", proposal.proposal_id, {},
                                   error=f"proposal is {proposal.status}, not approved")
        # Never place another order while an earlier account order is unresolved.
        for other in store.all():
            same_venue = (other.bucket == "micro_live") == (proposal.bucket == "micro_live")
            if other.proposal_id != proposal.proposal_id and same_venue and needs_reconciliation(other):
                return ExecutionResult(False, "RECONCILIATION_REQUIRED", proposal.proposal_id, {},
                                       error=f"resolve pending order for {other.proposal_id} first")
        return _execute_new(profiles, proposal, broker, store, ledger_path=ledger_path, dry_run=dry_run)


def _execute_new(profiles, proposal, broker, store, *, ledger_path=None, dry_run=False):
    ledger = Path(ledger_path or _ledger_for(profiles, proposal.bucket)).resolve()
    configured_fills = Path(str((profiles.risk.get("broker") or {}).get(
        "fills_file", "state/fills.jsonl")))
    fills = configured_fills if configured_fills.is_absolute() else profiles.project_dir / configured_fills
    if proposal.bucket == "micro_live":
        risk_file = (profiles.risk.get("micro_live_execution") or {}).get(
            "risk_state_file", "state/micro_live_risk_state.json")
    else:
        risk_file = (profiles.risk.get("portfolio_state") or {}).get(
            "risk_state_file", "state/risk_state.json")
    ladder_path = Path(str(risk_file))
    ladder_path = ladder_path if ladder_path.is_absolute() else profiles.project_dir / ladder_path
    if any(StateDB(path).path != store.db.path for path in (ledger, fills, ladder_path)):
        return ExecutionResult(
            False, "STATE_CONFIGURATION_ERROR", proposal.proposal_id, {},
            error="proposal, portfolio, ladder and fill state must share one SQLite database directory",
        )
    rv = revalidate(profiles, proposal, broker, ledger_path=ledger_path)
    if not rv.may_execute:
        proposal.status = {OUT_STALE: EXPIRED, OUT_REANALYZE: NEEDS_REANALYSIS,
                           OUT_INVALIDATED: INVALIDATED, OUT_REJECTED: REJECTED}.get(rv.outcome, INVALIDATED)
        proposal.note("revalidation_failed", f"{rv.outcome}: {rv.reasons[-1] if rv.reasons else ''}",
                      outcome=rv.outcome, changes=rv.changes)
        store.save(proposal)
        return ExecutionResult(False, rv.outcome, proposal.proposal_id, rv.as_dict())
    if getattr(broker, "name", "") == "robinhood":
        from .micro_live import run_preflight

        pf = run_preflight(profiles, broker)
        if not pf.ready:
            failed = "; ".join(f"{c.check_id} {c.name}: {c.detail}" for c in pf.failures)
            proposal.note("preflight_failed", failed)
            store.save(proposal)
            return ExecutionResult(False, "PREFLIGHT_FAILED", proposal.proposal_id, rv.as_dict(), error=failed)
    if dry_run:
        return ExecutionResult(False, f"DRY_RUN_{rv.outcome}", proposal.proposal_id, rv.as_dict())

    attempt = proposal.submit_attempts + 1
    if attempt > int((profiles.risk.get("idempotency") or {}).get("max_submit_attempts", 1)):
        return ExecutionResult(False, "ATTEMPTS_EXHAUSTED", proposal.proposal_id, rv.as_dict(),
                               duplicate_prevented=True)
    coid = client_order_id(profiles, proposal, attempt)
    try:
        existing = broker.get_order_by_client_id(coid)
    except BrokerError as exc:
        return ExecutionResult(False, "BROKER_UNAVAILABLE", proposal.proposal_id, rv.as_dict(), error=str(exc))
    if existing is not None:
        proposal.client_order_id = coid
        proposal.execution_ledger_path = str(Path(ledger_path or _ledger_for(profiles, proposal.bucket)).resolve())
        proposal.status = UNKNOWN
        store.save(proposal)
        return _record_order(profiles, proposal, existing, store, submitted=False,
                             outcome="DUPLICATE_PREVENTED", revalidation=rv.as_dict())

    request = _build_order_request(profiles, proposal, rv, coid)
    if bool((profiles.risk.get("robinhood") or {}).get("use_broker_pretrade_review", True)) and hasattr(broker, "review_order"):
        try:
            review = broker.review_order(request)
        except BrokerError as exc:
            return ExecutionResult(False, "REVIEW_FAILED", proposal.proposal_id, rv.as_dict(), error=str(exc))
        blocking = [str(a) for a in review.get("alerts", []) if a]
        if blocking:
            proposal.status = REJECTED
            proposal.note("broker_review_alerts", "; ".join(blocking))
            store.save(proposal)
            return ExecutionResult(False, "BROKER_REVIEW_ALERT", proposal.proposal_id, rv.as_dict(),
                                   error="; ".join(blocking))
    # Persist the intent before entering the venue. A crash here is UNKNOWN, never retryable
    # by guessing that the broker did not receive it.
    proposal.submit_attempts = attempt
    proposal.client_order_id = coid
    proposal.status = SUBMITTING
    proposal.order_status = "submitting"
    proposal.order_request = asdict(request)
    proposal.execution_ledger_path = str(ledger)
    proposal.note("submitting", f"order intent {coid}")
    account_key = "micro_live" if proposal.bucket == "micro_live" else "conceptual"
    cash = rv.executable_notional_usd if request.side == "buy" else 0.0
    quantity = request.quantity or 0.0
    try:
        with store.db.transaction() as conn:
            # Check the canonical ledger and reserve capital in the same SQLite write
            # transaction as the order intent. A second process cannot observe spare cash
            # between these operations or commit a second unresolved order for the account.
            ledger = load_portfolio(proposal.execution_ledger_path)
            if request.side == "buy" and ledger.buckets[proposal.bucket].cash_usd + 1e-6 < cash:
                raise PortfolioError("canonical ledger cash is insufficient for this reservation")
            if request.side == "sell":
                held = ledger.position(proposal.ticker, proposal.bucket)
                if held is None or held.quantity + 1e-8 < quantity:
                    raise PortfolioError("canonical ledger position is insufficient for this reservation")
            store.save(proposal)
            conn.execute("INSERT INTO reservations VALUES(?,?,?,?,?,'ACTIVE',?)", (
                coid, proposal.proposal_id, account_key, cash, quantity, utcnow().isoformat()))
    except (PortfolioError, StateConflict, sqlite3.IntegrityError) as exc:
        # The transaction rolled the intent back, so the venue has not been called. Reloading
        # also discards the in-memory revision assigned inside the aborted transaction.
        proposal = store.get(proposal.proposal_id) or proposal
        return ExecutionResult(False, "RESERVATION_FAILED", proposal.proposal_id, rv.as_dict(),
                               error=f"capital reservation failed: {exc}", duplicate_prevented=True)
    try:
        order = broker.submit_order(request)
    except DuplicateOrder as exc:
        if exc.existing is not None:
            return _record_order(profiles, proposal, exc.existing, store, submitted=False,
                                 outcome="DUPLICATE_PREVENTED", revalidation=rv.as_dict())
        proposal.status = UNKNOWN
        proposal.reconciliation_error = "venue reported a duplicate without its order; lookup required"
        store.save(proposal)
        return ExecutionResult(False, "SUBMIT_UNCERTAIN", proposal.proposal_id, rv.as_dict(),
                               error=proposal.reconciliation_error, duplicate_prevented=True)
    except BrokerRejection as exc:
        proposal.status, proposal.order_status = REJECTED, "rejected"
        proposal.reconciled_at = utcnow().isoformat()
        proposal.note("broker_rejected", str(exc))
        store.save(proposal)
        return ExecutionResult(False, "BROKER_REJECTED", proposal.proposal_id, rv.as_dict(), error=str(exc))
    except Exception as exc:  # noqa: BLE001 - every ambiguous failure must preserve the intent
        proposal.status = UNKNOWN
        proposal.reconciliation_error = f"submission result unknown: {type(exc).__name__}; lookup required"
        proposal.note("submit_uncertain", proposal.reconciliation_error)
        store.save(proposal)
        return ExecutionResult(False, "SUBMIT_UNCERTAIN", proposal.proposal_id, rv.as_dict(),
                               error=proposal.reconciliation_error)
    proposal.broker_order_id = order.order_id
    proposal.status = SUBMITTED
    store.save(proposal)  # save venue identity before any polling or ledger operation
    try:
        order = broker.poll_order(order.order_id) if hasattr(broker, "poll_order") else order
    except BrokerError:
        pass  # the initial response is recorded; the worker will read the newer state
    return _record_order(profiles, proposal, order, store, submitted=True,
                         outcome="SUBMITTED", revalidation=rv.as_dict())


def reconcile_proposal(profiles: Profiles, proposal: Proposal, broker: BrokerAdapter,
                       store: ProposalStore, *, ledger_path: str | None = None) -> ExecutionResult:
    """Read and account for an existing order, even when trading is paused or disabled.

    This function never submits, reviews, or cancels an order.
    """
    with store.locked():
        proposal = store.get(proposal.proposal_id) or proposal
        if not proposal.client_order_id:
            return ExecutionResult(False, "RECONCILIATION_REQUIRED", proposal.proposal_id, {},
                                   error="there is no persisted client order id to recover")
        if proposal.order_status is None and proposal.executed_at:
            # Pre-upgrade orders did not store atomic fill cursors. Guessing whether an old
            # fill is already in the ledger would either duplicate or lose money.
            proposal.reconciliation_error = "legacy order has no fill cursor; verify its ledger manually"
            store.save(proposal)
            return ExecutionResult(False, "RECONCILIATION_REQUIRED", proposal.proposal_id, {},
                                   error=proposal.reconciliation_error)
        try:
            if proposal.broker_order_id:
                order = broker.poll_order(proposal.broker_order_id, timeout_seconds=0)
            else:
                order = broker.get_order_by_client_id(proposal.client_order_id)
            if order is None:
                raise BrokerUnavailable("order not yet found; keep the intent unresolved, do not resubmit")
            if ledger_path and not proposal.execution_ledger_path:
                proposal.execution_ledger_path = str(Path(ledger_path).resolve())
            return _record_order(profiles, proposal, order, store, submitted=False, outcome="RECONCILED")
        except Exception as exc:  # noqa: BLE001 - keep the order recoverable on any read failure
            proposal.status = UNKNOWN
            proposal.reconciliation_error = f"order synchronization failed: {exc}"
            proposal.note("reconciliation_failed", proposal.reconciliation_error)
            store.save(proposal)
            return ExecutionResult(False, "RECONCILIATION_REQUIRED", proposal.proposal_id, {},
                                   error=proposal.reconciliation_error)


def _record_order(profiles, proposal, order, store, *, submitted, outcome, revalidation=None):
    try:
        with store.db.transaction():
            return _record_order_transaction(profiles, proposal, order, store, submitted=submitted,
                                             outcome=outcome, revalidation=revalidation)
    except Exception as exc:  # noqa: BLE001 - accounting failure must never claim a completed fill
        proposal = store.get(proposal.proposal_id) or proposal
        proposal.status = UNKNOWN
        proposal.reconciliation_error = f"order exists but accounting is incomplete: {exc}"
        proposal.note("reconciliation_failed", proposal.reconciliation_error)
        store.save(proposal)
        return ExecutionResult(submitted, "RECONCILIATION_REQUIRED", proposal.proposal_id,
                               revalidation or {}, order=_order_dict(order), error=proposal.reconciliation_error)


def _record_order_transaction(profiles, proposal, order, store, *, submitted, outcome, revalidation=None):
    expected_side = "buy" if proposal.action in ("BUY", "ADD") else "sell"
    if (not order.order_id or order.symbol != proposal.ticker or order.side != expected_side
            or (proposal.broker_order_id and proposal.broker_order_id != order.order_id)):
        raise PortfolioError("venue returned a different order identity")
    if not math.isfinite(order.filled_quantity) or order.filled_quantity < 0:
        raise PortfolioError("venue returned an invalid filled quantity")
    if order.quantity is not None and (not math.isfinite(order.quantity) or
                                       order.filled_quantity > order.quantity + 1e-8):
        raise PortfolioError("filled quantity exceeds order quantity")
    prior_state = (proposal.order_status, proposal.filled_quantity)
    proposal.broker_order_id = order.order_id
    proposal.order_status = order.status
    if order.filled_quantity:
        if not order.filled_avg_price:
            raise PortfolioError("filled order has no execution price")
        ledger = proposal.execution_ledger_path or _ledger_for(profiles, proposal.bucket)
        state = apply_fill_to_ledger(
            ledger, symbol=order.symbol, bucket=proposal.bucket, side=order.side,
            quantity=order.filled_quantity, price=order.filled_avg_price,
            trade_date=utcnow().astimezone(ZoneInfo("America/New_York")).date().isoformat(),
            client_order_id=proposal.client_order_id)
        # These events are durable in the ledger and can be exported again after a crash.
        for event in state.applied_orders[proposal.client_order_id]["events"]:
            _append_fill(profiles, proposal, order, event)
    else:
        cursor = load_portfolio(proposal.execution_ledger_path or _ledger_for(profiles, proposal.bucket))
        previous = cursor.applied_orders.get(proposal.client_order_id, {})
        if proposal.filled_quantity > 0 or previous.get("quantity", 0) > 0:
            raise PortfolioError("venue lost a previously observed fill")
    proposal.filled_quantity = order.filled_quantity
    proposal.filled_avg_price = order.filled_avg_price
    proposal.executed_notional_usd = order.filled_quantity * (order.filled_avg_price or 0.0)
    terminal_status = {"filled": EXECUTED, "canceled": CANCELED, "expired": CANCELED,
                       "done_for_day": CANCELED, "rejected": REJECTED}.get(order.status)
    proposal.status = terminal_status or (
        (PARTIALLY_FILLED if order.filled_quantity else SUBMITTED) if order.is_open else UNKNOWN)
    if order.status == "filled" and order.filled_quantity <= 0:
        raise PortfolioError("venue reports filled with zero filled quantity")
    proposal.reconciled_at = utcnow().isoformat()
    proposal.reconciliation_error = None if proposal.status != UNKNOWN else "unrecognized venue order status"
    if order.is_terminal:
        proposal.executed_at = proposal.reconciled_at
    if prior_state != (order.status, order.filled_quantity):
        proposal.note("order_updated" if order.is_terminal else "pending",
                      f"order {order.order_id}: {order.status}, filled {order.filled_quantity:g}")
    store.save(proposal)
    return ExecutionResult(submitted, outcome, proposal.proposal_id, revalidation or {},
                           order=_order_dict(order), duplicate_prevented=not submitted)


def _append_fill(profiles, proposal, order, event):
    """Idempotent export of a durable ledger event; errors remain recoverable and visible."""
    configured = Path(str((profiles.risk.get("broker") or {}).get("fills_file", "state/fills.jsonl")))
    path = configured if configured.is_absolute() else profiles.project_dir / configured
    entry = {"at": utcnow().isoformat(), "proposal_id": proposal.proposal_id, "run_id": proposal.run_id,
                 "bucket": proposal.bucket, "strategy": proposal.strategy, "symbol": order.symbol,
                 "side": order.side, "order_id": order.order_id, "client_order_id": proposal.client_order_id,
                 "status": order.status, **event}
    StateDB(path).append_fill(entry, path)


def _order_rules(profiles: Profiles, bucket: str) -> dict[str, Any]:
    """Order shape for a bucket, taken from that bucket's venue rules.

    The Micro-Live rules are dictated by Robinhood: a fractional quantity or a dollar amount is
    accepted only on a market order in regular hours, and time in force is 'gfd' or 'gtc'. The
    conceptual buckets keep limit orders because their simulator supports them.
    """
    from .micro_live import BUCKET as MICRO_BUCKET

    if bucket == MICRO_BUCKET:
        micro = profiles.risk.get("micro_live_execution") or {}
        rules = dict(micro.get("order_rules") or {})
        return {"order_type": rules.get("order_type", "market"),
                "time_in_force": rules.get("time_in_force", "gfd"),
                "extended_hours": False,
                "use_notional": True}
    broker_cfg = profiles.risk.get("broker") or {}
    defaults = dict(broker_cfg.get("order_defaults") or {})
    return {"order_type": "limit", "time_in_force": defaults.get("time_in_force", "day"),
            "extended_hours": bool(defaults.get("extended_hours", False)), "use_notional": True}


def _build_order_request(profiles: Profiles, proposal: Proposal, rv: Revalidation, coid: str) -> OrderRequest:
    rules = _order_rules(profiles, proposal.bucket)
    buying = proposal.action in ("BUY", "ADD")
    notional = rv.executable_notional_usd
    quantity = None
    if not buying:
        quantity = rv.executable_quantity
        if quantity is None or quantity <= 0:
            raise ValueError("a sell order requires a revalidated share quantity")
        notional = None
    return OrderRequest(
        symbol=proposal.ticker, side="buy" if buying else "sell", client_order_id=coid,
        notional=notional if rules["use_notional"] else None, quantity=quantity,
        order_type=rules["order_type"],
        limit_price=rv.limit_price if rules["order_type"] in ("limit", "stop_limit") else None,
        time_in_force=rules["time_in_force"], extended_hours=rules["extended_hours"],
    )


def _ledger_for(profiles: Profiles, bucket: str):
    """Each venue reconciles against its own ledger.

    Core and Sandbox are the conceptual portfolio behind the simulator; Micro-Live is a real
    Robinhood account. Sharing one cash reconciliation between them would make every check
    fail, so the bucket selects the ledger.
    """
    from .micro_live import BUCKET as MICRO_BUCKET

    if bucket == MICRO_BUCKET:
        micro = profiles.risk.get("micro_live_execution") or {}
        return profiles.project_dir / str(micro.get("ledger_file", "state/micro_live_portfolio.yaml"))
    ps = profiles.risk.get("portfolio_state") or {}
    return profiles.project_dir / str(ps.get("file", "state/portfolio.yaml"))


def _order_dict(order: BrokerOrder | None) -> dict[str, Any] | None:
    if order is None:
        return None
    d = asdict(order)
    d["submitted_at"] = order.submitted_at.isoformat() if order.submitted_at else None
    return d
