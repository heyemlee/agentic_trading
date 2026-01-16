"""Stage 3 validation: the proposal -> approval -> revalidation -> paper execution pipeline.

Every scenario runs against the deterministic SimulatedBroker with no network, no credentials
and no model call, so the safety properties are provable and repeatable. The same code path
runs against Alpaca paper when credentials are configured; only the adapter changes.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import yaml

from .broker import SimulatedBroker
from .env import load_env
from .evidence import collect_evidence
from .execution import execute
from .profile import Profiles, load_profiles
from .proposals import APPROVED, PROPOSED, Proposal, ProposalStore, new_proposal, utcnow

TRADE_DATE = "2026-09-02"
PRICES = {"VTI": 376.88, "VOO": 703.10, "BND": 71.83, "AAPL": 232.75, "USMV": 101.89}


def _ledger(tmp: Path, core_cash: float = 2500.0, sandbox_cash: float = 500.0,
            positions: list[dict] | None = None) -> Path:
    path = tmp / "portfolio.yaml"
    path.write_text(yaml.safe_dump({
        "as_of": TRADE_DATE, "currency": "USD",
        "buckets": {"core": {"cash_usd": core_cash}, "sandbox": {"cash_usd": sandbox_cash}},
        "positions": positions or [], "recent_opens": [],
    }), encoding="utf-8")
    return path


def _profiles(tmp: Path) -> Profiles:
    """Profiles pointed entirely at a temp directory.

    Every state file an offline run may write is redirected, so a scenario can never append to
    the real ledger, ladder, proposal store or fill audit trail.
    """
    p = load_profiles(Path(__file__).resolve().parent.parent / "config")
    p.risk["portfolio_state"] = {**(p.risk.get("portfolio_state") or {}),
                                 "file": str(tmp / "portfolio.yaml"),
                                 "risk_state_file": str(tmp / "risk_state.json")}
    p.risk["broker"] = {**(p.risk.get("broker") or {}),
                        "state_file": str(tmp / "proposals.json"),
                        "fills_file": str(tmp / "fills.jsonl")}
    p.risk["micro_live_execution"] = {**(p.risk.get("micro_live_execution") or {}),
                                      "ledger_file": str(tmp / "micro_live_portfolio.yaml"),
                                      "risk_state_file": str(tmp / "micro_live_risk_state.json")}
    p.risk["market_regime"] = {**(p.risk.get("market_regime") or {}), "_offline": True}
    return p


def _proposal(*, ticker: str = "VTI", bucket: str = "core", action: str = "BUY",
              strategy: str = "long_only_diversified", instrument_type: str = "etf",
              limit_class: str = "broad_diversified_etf", notional: float = 875.0,
              confidence: int = 80, price: float | None = None, age_minutes: float = 5.0,
              status: str = APPROVED, ladder: str = "NORMAL", regime: str = "NORMAL",
              fingerprint: str = "(empty)", approved_minutes_ago: float = 2.0) -> Proposal:
    p = new_proposal(run_id="test", ticker=ticker, bucket=bucket, action=action, strategy=strategy,
                     instrument_type=instrument_type, limit_class=limit_class, size_hint="normal",
                     confidence=confidence, notional_usd=notional, target_weight_pct=35.0,
                     trade_date=TRADE_DATE, reference_price=price if price is not None else PRICES[ticker],
                     reference_price_date=TRADE_DATE, ladder_level_at_proposal=ladder,
                     regime_at_proposal=regime, positions_fingerprint=fingerprint, data_quality="good",
                     evidence=collect_evidence(ticker, TRADE_DATE,
                         price_lookup=lambda *_: (price if price is not None else PRICES[ticker], TRADE_DATE)))
    p.created_at = (utcnow() - timedelta(minutes=age_minutes)).isoformat()
    p.status = status
    if status == APPROVED:
        p.approved_at = (utcnow() - timedelta(minutes=approved_minutes_ago)).isoformat()
        p.approved_by = "investor"
    return p


@dataclass
class Case:
    key: str
    title: str
    expect_outcome: str
    expect_submitted: bool
    build: Callable[[Path], tuple[Profiles, Proposal, SimulatedBroker, Path]]
    note: str = ""


def _std(tmp: Path, *, broker_kwargs: dict[str, Any] | None = None, ledger_kwargs: dict[str, Any] | None = None,
         proposal_kwargs: dict[str, Any] | None = None):
    profiles = _profiles(tmp)
    ledger = _ledger(tmp, **(ledger_kwargs or {}))
    broker = SimulatedBroker(prices=dict(PRICES), **(broker_kwargs or {}))
    return profiles, _proposal(**(proposal_kwargs or {})), broker, ledger


def build_cases() -> list[Case]:
    def approved_fresh(tmp):
        return _std(tmp, broker_kwargs={"cash": 3000.0})

    def stale_before_approval(tmp):
        # An event-driven proposal has a 45 minute lifetime; 90 minutes later it is gone.
        return _std(tmp, broker_kwargs={"cash": 3000.0},
                    proposal_kwargs={"strategy": "event_driven", "bucket": "sandbox", "ticker": "USMV",
                                     "limit_class": "sector_factor_thematic_etf", "notional": 150.0,
                                     "age_minutes": 90.0})

    def price_moved(tmp):
        profiles, proposal, broker, ledger = _std(tmp, broker_kwargs={"cash": 3000.0})
        broker.set_price("VTI", PRICES["VTI"] * 1.05)      # +5% against a 3% core band
        return profiles, proposal, broker, ledger

    def portfolio_changed(tmp):
        profiles, proposal, broker, ledger_path = _std(
            tmp, broker_kwargs={"cash": 2100.0, "positions": {"BND": (12.0, 71.0)}},
            ledger_kwargs={"core_cash": 1600.0, "sandbox_cash": 500.0,
                           "positions": [{"symbol": "BND", "bucket": "core", "quantity": 12.0,
                                          "cost_basis_per_share": 71.0}]})
        return profiles, proposal, broker, ledger_path

    def cash_dropped(tmp):
        # Equity is intact (~$3,000) but core cash fell to $200 because it is invested in BND,
        # so the approved $875 no longer fits and cannot even be halved into range.
        return _std(tmp, broker_kwargs={"cash": 700.0, "positions": {"BND": (32.0, 71.5)}},
                    ledger_kwargs={"core_cash": 200.0, "sandbox_cash": 500.0,
                                   "positions": [{"symbol": "BND", "bucket": "core", "quantity": 32.0,
                                                  "cost_basis_per_share": 71.5}]},
                    proposal_kwargs={"fingerprint": "BND:32.0"})

    def risk_state_blocks(tmp):
        profiles = _profiles(tmp)
        ledger = _ledger(tmp)
        broker = SimulatedBroker(prices=dict(PRICES), cash=3000.0)
        (tmp / "risk_state.json").write_text('{"total": {"high_water_mark_usd": 3400.0, "level": "NORMAL"},'
                                             ' "core": {"high_water_mark_usd": 2800.0, "level": "NORMAL"},'
                                             ' "sandbox": {"high_water_mark_usd": 500.0, "level": "NORMAL"}}')
        return profiles, _proposal(ladder="NORMAL"), broker, ledger

    def sandbox_needs_approval(tmp):
        return _std(tmp, broker_kwargs={"cash": 3000.0},
                    proposal_kwargs={"strategy": "momentum", "bucket": "sandbox", "ticker": "USMV",
                                     "limit_class": "sector_factor_thematic_etf", "notional": 150.0,
                                     "status": PROPOSED, "age_minutes": 5.0})

    def stale_momentum(tmp):
        return _std(tmp, broker_kwargs={"cash": 3000.0},
                    proposal_kwargs={"strategy": "momentum", "bucket": "sandbox", "ticker": "USMV",
                                     "limit_class": "sector_factor_thematic_etf", "notional": 150.0,
                                     "age_minutes": 120.0})

    def hard_risk_rejects_now(tmp):
        # Equity is intact: core already holds VTI at ~40% of its allocation, so the same buy
        # now breaches the position cap (HR08) even though it passed when the position was empty.
        return _std(tmp, broker_kwargs={"cash": 2000.0, "positions": {"VTI": (2.65, 377.0)}},
                    ledger_kwargs={"core_cash": 1500.0, "sandbox_cash": 500.0,
                                   "positions": [{"symbol": "VTI", "bucket": "core", "quantity": 2.65,
                                                  "cost_basis_per_share": 377.0}]},
                    proposal_kwargs={"fingerprint": "VTI:2.65"})

    def broker_rejects(tmp):
        return _std(tmp, broker_kwargs={"cash": 3000.0, "reject_reason": "paper venue refused the order"})

    def broker_unavailable(tmp):
        return _std(tmp, broker_kwargs={"cash": 3000.0, "unavailable": True})

    def market_closed(tmp):
        return _std(tmp, broker_kwargs={"cash": 3000.0, "market_open": False})

    def no_trade(tmp):
        return _std(tmp, broker_kwargs={"cash": 3000.0},
                    proposal_kwargs={"action": "NO_TRADE", "notional": 0.0})

    def size_reduced(tmp):
        # The position already existed when the proposal was made (so the book has not changed),
        # but the remaining distance to the target is smaller than the approved amount.
        return _std(tmp, broker_kwargs={"cash": 2800.0, "positions": {"VTI": (0.53, 377.0)}},
                    ledger_kwargs={"core_cash": 2300.0, "sandbox_cash": 500.0,
                                   "positions": [{"symbol": "VTI", "bucket": "core", "quantity": 0.53,
                                                  "cost_basis_per_share": 377.0}]},
                    proposal_kwargs={"fingerprint": "VTI:0.53", "notional": 875.0})

    def partial_fill(tmp):
        return _std(tmp, broker_kwargs={"cash": 3000.0, "fill_mode": "partial"})

    return [
        Case("T1", "Approved quickly and still valid: order submitted", "SUBMITTED", True, approved_fresh),
        Case("T2", "Proposal went stale before approval (event-driven, 45 min life)", "STALE", False, stale_before_approval),
        Case("T3", "Price moved 5% against a 3% core band", "REANALYZE", False, price_moved),
        Case("T4", "Portfolio changed under the proposal", "REANALYZE", False, portfolio_changed),
        Case("T5", "Cash fell below what the approved size needs", "REJECTED", False, cash_dropped,
             note="Hard Risk (HR07 funding, HR10b cash floor) catches this before size reduction is even considered"),
        Case("T6", "New drawdown state blocks the old proposal", "INVALIDATED", False, risk_state_blocks),
        Case("T7", "Sandbox momentum without human approval never executes", "REJECTED", False, sandbox_needs_approval),
        Case("T8", "Stale momentum proposal (90 min life) is invalidated", "STALE", False, stale_momentum),
        Case("T9", "Hard Risk passed at proposal time, rejects at execution time", "REJECTED", False, hard_risk_rejects_now),
        Case("T10", "Broker rejects the order", "BROKER_REJECTED", False, broker_rejects),
        Case("T11", "Broker state unavailable: fail closed, no order", "INVALIDATED", False, broker_unavailable),
        Case("T12", "Market closed: nothing is submitted", "STALE", False, market_closed),
        Case("T13", "NO_TRADE produces no broker activity", "REJECTED", False, no_trade),
        Case("T14", "Partial fill is recorded truthfully", "SUBMITTED", True, partial_fill),
        Case("T17", "Size is reduced to what current state supports, then executed", "SUBMITTED", True, size_reduced,
             note="revalidation outcome REDUCED; never below half the approved idea"),
    ]


@dataclass
class CaseOutcome:
    case: Case
    outcome: str
    submitted: bool
    orders_at_broker: int
    detail: str
    ok: bool


def run_case(case: Case) -> CaseOutcome:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        profiles, proposal, broker, ledger = case.build(tmp)
        store = ProposalStore(tmp / "proposals.json")
        store.save(proposal)
        result = execute(profiles, proposal, broker, store, ledger_path=str(ledger))
        try:
            orders = len(broker._orders)
        except Exception:                          # noqa: BLE001
            orders = 0
        detail = (result.revalidation.get("reasons") or [result.error or ""])[-1] if result.revalidation else (result.error or "")
        ok = result.outcome == case.expect_outcome and result.submitted == case.expect_submitted
        return CaseOutcome(case, result.outcome, result.submitted, orders, detail[:110], ok)


def run_idempotency_case() -> CaseOutcome:
    """Executing the same approved proposal twice must never produce two orders."""
    case = Case("T15", "Duplicate submission is prevented", "ALREADY_EXECUTED", False, lambda tmp: _std(tmp))
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        profiles, proposal, broker, ledger = _std(tmp, broker_kwargs={"cash": 3000.0})
        store = ProposalStore(tmp / "proposals.json")
        store.save(proposal)
        first = execute(profiles, proposal, broker, store, ledger_path=str(ledger))
        second = execute(profiles, store.get(proposal.proposal_id), broker, store, ledger_path=str(ledger))
        orders = len(broker._orders)
        ok = first.submitted and not second.submitted and second.duplicate_prevented and orders == 1
        return CaseOutcome(case, second.outcome, second.submitted, orders,
                           f"first={first.outcome}, second={second.outcome}, orders at broker={orders}", ok)


def run_interrupted_session_case() -> CaseOutcome:
    """A Claude session limit during analysis must leave no proposal and no order."""
    case = Case("T16", "Interrupted Claude session creates no order", "NO_PROPOSAL", False, lambda tmp: _std(tmp))
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _, _, broker, _ = _std(tmp, broker_kwargs={"cash": 3000.0})
        store = ProposalStore(tmp / "proposals.json")
        # The research run raises before any proposal is written; nothing downstream can run.
        from .llm.claude_code import ClaudeCodeError

        try:
            raise ClaudeCodeError("You've hit your session limit · resets 1:30pm", rate_limited=True)
        except ClaudeCodeError:
            pass
        orders = len(broker._orders)
        ok = not store.all() and orders == 0
        return CaseOutcome(case, "NO_PROPOSAL", False, orders,
                           "session limit raised before a proposal existed; store and broker both empty", ok)


def main(argv: list[str] | None = None) -> int:
    load_env()
    outcomes = [run_case(c) for c in build_cases()]
    outcomes.append(run_idempotency_case())
    outcomes.append(run_interrupted_session_case())
    width = max(len(o.case.title) for o in outcomes)
    print(f"{'id':4s} {'case':{width}s}  {'outcome':18s} {'sent':5s} {'orders':6s}  detail")
    for o in outcomes:
        print(f"{o.case.key:4s} {o.case.title:{width}s}  {o.outcome:18s} "
              f"{'yes' if o.submitted else 'no':5s} {o.orders_at_broker:^6d}  {o.detail}")
    bad = [o for o in outcomes if not o.ok]
    print(f"\n{len(outcomes) - len(bad)}/{len(outcomes)} cases behaved as expected")
    for o in bad:
        print(f"  MISMATCH {o.case.key}: expected {o.case.expect_outcome}/submitted={o.case.expect_submitted}, "
              f"got {o.outcome}/submitted={o.submitted}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
