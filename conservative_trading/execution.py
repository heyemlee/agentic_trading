"""Approval-time revalidation and paper execution.

The rule this module exists to enforce: approval is permission to execute the idea only if
the current world still supports it. Nothing here trusts the proposal's own numbers. Every
fast-moving input (price, cash, positions, open orders, session, drawdown, regime) is read
again, the deterministic Hard Risk Layer runs again against that fresh state, and only then
may an order be sent.

Outcomes: VALID, REDUCED, STALE, INVALIDATED, REANALYZE, REJECTED.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC
from pathlib import Path
from typing import Any

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
    EXECUTED,
    EXPIRED,
    INVALIDATED,
    NEEDS_REANALYSIS,
    REJECTED,
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
    if as_of is not None:
        if as_of.tzinfo is None:
            as_of = as_of.replace(tzinfo=UTC)
        age = (utcnow() - as_of).total_seconds()
        if age > max_age:
            return None, (f"the venue's price for {symbol} is {age:.0f}s old (limit {max_age:.0f}s); "
                          "refusing to price an order from stale data")
    spread = quote.spread_pct
    detail = f"venue price {quote.price:,.4f} ({quote.source}"
    detail += f", spread {spread:.2f}%)" if spread is not None else ")"
    return quote.price, detail


def revalidate(profiles: Profiles, proposal: Proposal, broker: BrokerAdapter, *,
               ledger_path: str | None = None, now=None) -> Revalidation:
    """Re-read the world, rerun Hard Risk, and decide whether the old idea may still execute."""
    rv = Revalidation(outcome=OUT_INVALIDATED)

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
    ladders = load_ladder_states(ladder_path, profiles)
    micro_equity = snap.buckets[MICRO_BUCKET].equity if MICRO_BUCKET in snap.buckets else None
    evaluate_ladders(ladders, profiles, proposal.trade_date,
                     snap.total_equity if "core" in snap.buckets else (micro_equity or snap.total_equity),
                     snap.buckets["core"].equity if "core" in snap.buckets else (micro_equity or 0.0),
                     snap.buckets["sandbox"].equity if "sandbox" in snap.buckets else 0.0,
                     micro_live_equity=micro_equity)
    save_ladder_states(ladder_path, ladders)
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
    )
    result = HardRiskEngine(profiles).evaluate(risk_proposal, snap, ladders, regime)
    rv.hard_risk = result.as_dict()
    if result.verdict != "APPROVE":
        rv.outcome = OUT_REJECTED
        rv.reasons.append(f"Hard Risk rejects at execution time: {result.verdict}"
                          + (f" ({', '.join(r.rule_id for r in result.failed_rules)})" if result.failed_rules else ""))
        return rv

    # ---- 6. size: the fresh sizing wins, and may only shrink the approved idea
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


def execute(profiles: Profiles, proposal: Proposal, broker: BrokerAdapter, store: ProposalStore, *,
            ledger_path: str | None = None, dry_run: bool = False) -> ExecutionResult:
    """Revalidate, then submit at most one order. Idempotent by construction."""
    if proposal.status == EXECUTED:
        return ExecutionResult(False, "ALREADY_EXECUTED", proposal.proposal_id, {},
                               duplicate_prevented=True,
                               error=f"already executed as order {proposal.broker_order_id}")

    rv = revalidate(profiles, proposal, broker, ledger_path=ledger_path)
    if not rv.may_execute:
        proposal.status = {OUT_STALE: EXPIRED, OUT_REANALYZE: NEEDS_REANALYSIS,
                           OUT_INVALIDATED: INVALIDATED, OUT_REJECTED: REJECTED}.get(rv.outcome, INVALIDATED)
        proposal.note("revalidation_failed", f"{rv.outcome}: {rv.reasons[-1] if rv.reasons else ''}",
                      outcome=rv.outcome, changes=rv.changes)
        store.save(proposal)
        return ExecutionResult(False, rv.outcome, proposal.proposal_id, rv.as_dict())

    # Real money gets one more gate: every preflight check must pass, re-evaluated now.
    if getattr(broker, "name", "") == "robinhood":
        from .micro_live import run_preflight

        pf = run_preflight(profiles, broker)
        if not pf.ready:
            failed = "; ".join(f"{c.check_id} {c.name}: {c.detail}" for c in pf.failures)
            proposal.note("preflight_failed", failed)
            store.save(proposal)
            return ExecutionResult(False, "PREFLIGHT_FAILED", proposal.proposal_id, rv.as_dict(), error=failed)

    if dry_run:
        proposal.note("dry_run", f"{rv.outcome}: would submit ${rv.executable_notional_usd:,.2f}")
        store.save(proposal)
        return ExecutionResult(False, f"DRY_RUN_{rv.outcome}", proposal.proposal_id, rv.as_dict())

    idem = profiles.risk.get("idempotency") or {}
    attempt = proposal.submit_attempts + 1
    if attempt > int(idem.get("max_submit_attempts", 1)):
        proposal.note("submit_blocked", "submission attempts exhausted; resolve the order state by hand")
        store.save(proposal)
        return ExecutionResult(False, "ATTEMPTS_EXHAUSTED", proposal.proposal_id, rv.as_dict(),
                               duplicate_prevented=True)
    coid = client_order_id(profiles, proposal, attempt)

    # Duplicate protection: ask the broker before writing anything.
    if bool(idem.get("check_broker_before_submit", True)):
        try:
            existing = broker.get_order_by_client_id(coid)
        except BrokerError as exc:
            proposal.note("submit_blocked", f"could not confirm order state before submitting: {exc}")
            store.save(proposal)
            return ExecutionResult(False, "BROKER_UNAVAILABLE", proposal.proposal_id, rv.as_dict(), error=str(exc))
        if existing is not None:
            proposal.status = EXECUTED
            proposal.client_order_id, proposal.broker_order_id = coid, existing.order_id
            proposal.note("duplicate_prevented", f"an order with {coid} already exists: {existing.order_id}")
            store.save(proposal)
            return ExecutionResult(False, "DUPLICATE_PREVENTED", proposal.proposal_id, rv.as_dict(),
                                   order=_order_dict(existing), duplicate_prevented=True)

    proposal.submit_attempts = attempt
    proposal.client_order_id = coid
    proposal.note("submitting", f"${rv.executable_notional_usd:,.2f} limit {rv.limit_price} as {coid}")
    store.save(proposal)   # persisted before the call, so a crash mid-flight is recoverable

    request = _build_order_request(profiles, proposal, rv, coid)

    # The venue's own pre-trade review runs before the order, as another layer on top of Hard
    # Risk rather than a replacement. It returns a live quote and the venue's alerts.
    review = None
    if bool((profiles.risk.get("robinhood") or {}).get("use_broker_pretrade_review", True)) and hasattr(broker, "review_order"):
        try:
            review = broker.review_order(request)
        except BrokerError as exc:
            proposal.note("review_failed", str(exc))
            store.save(proposal)
            return ExecutionResult(False, "REVIEW_FAILED", proposal.proposal_id, rv.as_dict(), error=str(exc))
        blocking = [a for a in review.get("alerts", []) if a]
        if blocking:
            proposal.status = REJECTED
            proposal.note("broker_review_alerts", "; ".join(blocking))
            store.save(proposal)
            return ExecutionResult(False, "BROKER_REVIEW_ALERT", proposal.proposal_id, rv.as_dict(),
                                   error="the venue's pre-trade review raised: " + "; ".join(blocking))
        proposal.note("broker_review", "no alerts from the venue's pre-trade review")

    try:
        order = broker.submit_order(request)
    except DuplicateOrder as exc:
        proposal.status = EXECUTED
        proposal.broker_order_id = exc.existing.order_id if exc.existing else None
        proposal.note("duplicate_prevented", str(exc))
        store.save(proposal)
        return ExecutionResult(False, "DUPLICATE_PREVENTED", proposal.proposal_id, rv.as_dict(),
                               order=_order_dict(exc.existing) if exc.existing else None, duplicate_prevented=True)
    except BrokerRejection as exc:
        proposal.status = REJECTED
        proposal.note("broker_rejected", str(exc))
        store.save(proposal)
        return ExecutionResult(False, "BROKER_REJECTED", proposal.proposal_id, rv.as_dict(), error=str(exc))
    except BrokerUnavailable as exc:
        # The order may or may not exist. Never retry blindly; a human resolves it.
        proposal.note("submit_uncertain", f"{exc}; look up {coid} at the broker before any retry")
        store.save(proposal)
        return ExecutionResult(False, "SUBMIT_UNCERTAIN", proposal.proposal_id, rv.as_dict(), error=str(exc))

    # Follow the order to a terminal state using the venue's own status tool, then reconcile
    # against what actually filled. A pending or partially filled order is recorded as such,
    # never assumed complete.
    try:
        order = broker.poll_order(order.order_id) if hasattr(broker, "poll_order") else order
    except BrokerError as exc:
        proposal.note("poll_failed", f"{exc}; the order exists, its final state is unknown")

    proposal.status = EXECUTED
    proposal.executed_at = utcnow().isoformat()
    proposal.broker_order_id = order.order_id
    proposal.executed_notional_usd = (order.filled_quantity or 0.0) * (order.filled_avg_price or 0.0) or None
    proposal.note("executed", f"order {order.order_id} status {order.status} "
                              f"filled {order.filled_quantity:g}/{order.quantity or 0:g}")
    if not order.is_terminal:
        proposal.note("pending", f"order {order.order_id} is still {order.status}; the ledger records only "
                                 "what has filled so far, and the rest is reconciled on the next run")
    if order.status == "rejected":
        proposal.status = REJECTED
        proposal.note("broker_rejected_after_submit", order.reason or "the venue rejected the order")

    # Portfolio update: record what actually filled in the bucket ledger so the next
    # revalidation can attribute the position. A failure here is loud, never silent.
    ledger_update = None
    if order.filled_quantity and order.filled_avg_price:
        target = ledger_path or _ledger_for(profiles, proposal.bucket)
        try:
            apply_fill_to_ledger(target, symbol=order.symbol, bucket=proposal.bucket, side=order.side,
                                 quantity=order.filled_quantity, price=order.filled_avg_price,
                                 trade_date=proposal.trade_date)
            ledger_update = (f"ledger updated: {order.side} {order.filled_quantity:g} {order.symbol} "
                             f"@ {order.filled_avg_price:,.4f} in {proposal.bucket}")
        except PortfolioError as exc:
            ledger_update = (f"LEDGER NOT UPDATED: {exc}. The order exists at the broker; reconcile the "
                             "ledger by hand before the next run.")
        proposal.note("ledger", ledger_update)
        _append_fill(profiles, proposal, order)
    elif order.is_terminal and order.status != "rejected":
        proposal.note("no_fill", f"order reached {order.status} with nothing filled; the ledger is unchanged")

    store.save(proposal)
    result = ExecutionResult(True, "SUBMITTED", proposal.proposal_id, rv.as_dict(), order=_order_dict(order))
    if ledger_update:
        result.revalidation.setdefault("reasons", []).append(ledger_update)
    if review is not None and review.get("available"):
        result.revalidation.setdefault("reasons", []).append("venue pre-trade review passed with no alerts")
    return result


def _append_fill(profiles: Profiles, proposal: Proposal, order: BrokerOrder) -> None:
    """One line per fill, for the evaluation harness to read later."""
    import json

    configured = Path(str((profiles.risk.get("broker") or {}).get("fills_file", "state/fills.jsonl")))
    path = configured if configured.is_absolute() else profiles.project_dir / configured
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "at": utcnow().isoformat(), "proposal_id": proposal.proposal_id, "run_id": proposal.run_id,
                "bucket": proposal.bucket, "strategy": proposal.strategy, "symbol": order.symbol,
                "side": order.side, "quantity": order.filled_quantity, "price": order.filled_avg_price,
                "notional": (order.filled_quantity or 0) * (order.filled_avg_price or 0),
                "order_id": order.order_id, "client_order_id": order.client_order_id, "status": order.status,
            }) + "\n")
    except OSError:
        pass  # auditing must never break execution that already happened


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
        quantity = round(notional / (rv.current_price or 1), 6)
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
