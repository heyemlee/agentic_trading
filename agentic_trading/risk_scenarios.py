"""Validation scenarios for the Hard Risk Layer. Shared by the test suite and ``ct-risk-scenarios``.

Every scenario is fully synthetic: prices, identities and regime inputs are injected, so it runs
offline, deterministically, and without a single model call.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .env import load_env
from .evidence import collect_evidence
from .portfolio import PortfolioState, build_snapshot, classify_instrument
from .profile import Profiles, load_profiles
from .risk import HardRiskEngine, LadderStates, Proposal, RegimeResult, evaluate_ladders
from .risk.ladder import LadderState

TRADE_DATE = "2026-09-02"
PRICES = {"VTI": 376.88, "VOO": 703.10, "VXUS": 87.99, "BND": 71.83, "VGIT": 57.99, "SGOV": 100.42,
          "AAPL": 232.75, "MSFT": 505.00, "USMV": 101.89, "IAU": 84.23, "BTC-USD": 81004.2}
IDENTITY = {
    "VTI": {"quote_type": "ETF"}, "VOO": {"quote_type": "ETF"}, "VXUS": {"quote_type": "ETF"},
    "BND": {"quote_type": "ETF"}, "VGIT": {"quote_type": "ETF"}, "SGOV": {"quote_type": "ETF"},
    "USMV": {"quote_type": "ETF"}, "IAU": {"quote_type": "ETF"},
    "AAPL": {"quote_type": "EQUITY", "sector": "Technology"},
    "MSFT": {"quote_type": "EQUITY", "sector": "Technology"},
}
GOOD_STOCK_INFO = {"marketCap": 3.4e12, "freeCashflow": 9.5e10, "netIncomeToCommon": 9.4e10, "debtToEquity": 140.0, "currentRatio": 1.1}


def price_lookup(symbol: str, as_of: str) -> tuple[float, str]:
    return PRICES[symbol.upper()], TRADE_DATE


def identity_lookup(symbol: str) -> dict[str, Any]:
    return IDENTITY.get(symbol.upper(), {})


#: Far enough from TRADE_DATE that the earnings blackout is not what these scenarios test. A
#: scenario that wants to exercise HR18 states its own date rather than relying on this.
EARNINGS_DATES = {"AAPL": "2026-10-29", "MSFT": "2026-10-27"}


def earnings_lookup(symbol: str) -> tuple[Any, bool]:
    """Deterministic stand-in for the yfinance earnings calendar. Offline by construction."""
    from datetime import date

    value = EARNINGS_DATES.get(symbol.upper())
    return (date.fromisoformat(value) if value else None), True


def make_state(core_cash: float = 2500.0, sandbox_cash: float = 500.0, positions: list[dict] | None = None,
               recent_opens: list[dict] | None = None, as_of: str = TRADE_DATE) -> PortfolioState:
    return PortfolioState(as_of=as_of, buckets={"core": {"cash_usd": core_cash}, "sandbox": {"cash_usd": sandbox_cash}},
                          positions=positions or [], recent_opens=recent_opens or [])


def make_ladders(total_equity: float, core_equity: float, sandbox_equity: float, profiles: Profiles,
                 hwm_total: float = 3000.0, hwm_core: float = 2500.0, hwm_sandbox: float = 500.0) -> LadderStates:
    ladders = LadderStates(LadderState(hwm_total), LadderState(hwm_core), LadderState(hwm_sandbox))
    return evaluate_ladders(ladders, profiles, TRADE_DATE, total_equity, core_equity, sandbox_equity)


def regime(level: str = "NORMAL") -> RegimeResult:
    if level == "UNKNOWN":
        return RegimeResult("UNKNOWN", False, "regime data unavailable: simulated VIX outage")
    return RegimeResult(level, True, f"simulated {level}", "SPY", 765.16, 708.78, TRADE_DATE, 15.2, TRADE_DATE)


@dataclass
class Scenario:
    key: str
    title: str
    ticker: str
    bucket: str
    action: str
    size_hint: str
    confidence: int | None
    state: PortfolioState
    regime_level: str = "NORMAL"
    data_quality: str = "good"
    missing: tuple[str, ...] = ()
    equity_override: tuple[float, float, float] | None = None   # (total, core, sandbox) for ladder evaluation
    expect_verdict: str = "APPROVE"
    expect_rules_failed: tuple[str, ...] = ()
    stock_info: dict[str, Any] | None = None
    strategy: str = "long_only_diversified"
    expect_human_approval: bool = True


def build_scenarios() -> list[Scenario]:
    held_vti = [{"symbol": "VTI", "bucket": "core", "quantity": 2.32, "cost_basis_per_share": 370.0}]   # ~ $874, 35%
    return [
        Scenario("S1", "Acceptable recommendation passes", "VTI", "core", "BUY", "normal", 80, make_state(),
                 expect_verdict="APPROVE"),
        Scenario("S2a", "Attractive but rejected: overlapping exposure group", "VOO", "core", "BUY", "normal", 92,
                 make_state(core_cash=2500 - 874.4, positions=held_vti), expect_verdict="REJECT",
                 expect_rules_failed=("HR09",)),
        Scenario("S2b", "Attractive but rejected: stock sleeve and sector concentration", "MSFT", "core", "BUY", "normal", 90,
                 make_state(core_cash=2500 - 874.4 - 375.0, positions=held_vti + [
                     {"symbol": "AAPL", "bucket": "core", "quantity": 1.611, "cost_basis_per_share": 232.75}]),
                 expect_verdict="REJECT", expect_rules_failed=("HR11", "HR11b"), stock_info=GOOD_STOCK_INFO),
        # Sandbox holds USMV worth ~$407 (cost $400) and has $100 cash left; core has $2,500 idle.
        # A $150 crypto buy must fail on sandbox cash alone: core cash is never a funding source.
        Scenario("S3a", "Sandbox out of cash cannot borrow from core", "BTC-USD", "sandbox", "BUY", "normal", 85,
                 make_state(sandbox_cash=100.0, positions=[
                     {"symbol": "USMV", "bucket": "sandbox", "quantity": 4.0, "cost_basis_per_share": 100.0}]),
                 expect_verdict="REJECT", expect_rules_failed=("HR07", "HR07c")),
        # Sandbox has the cash for a $150 buy, but its deployed cost basis would reach $510 > $500 cap.
        Scenario("S3b", "Sandbox tries to exceed its $500 deployed cap", "IAU", "sandbox", "BUY", "normal", 85,
                 make_state(sandbox_cash=150.0, positions=[
                     {"symbol": "USMV", "bucket": "sandbox", "quantity": 3.5, "cost_basis_per_share": 102.86}]),
                 expect_verdict="REJECT", expect_rules_failed=("HR07c",)),
        Scenario("S4a", "Drawdown WARNING: same VTI buy still allowed at confidence 80", "VTI", "core", "BUY", "normal", 80,
                 make_state(), equity_override=(2880.0, 2400.0, 480.0), expect_verdict="APPROVE"),
        Scenario("S4b", "Drawdown WARNING: stock open rejected", "AAPL", "core", "BUY", "normal", 80,
                 make_state(), equity_override=(2880.0, 2400.0, 480.0), expect_verdict="REJECT",
                 expect_rules_failed=("HR02",), stock_info=GOOD_STOCK_INFO),
        Scenario("S4c", "Drawdown RISK_REDUCTION: every opening rejected", "VTI", "core", "BUY", "normal", 90,
                 make_state(), equity_override=(2790.0, 2325.0, 465.0), expect_verdict="REJECT",
                 expect_rules_failed=("HR02",)),
        Scenario("S5a", "Regime data unavailable: fail closed", "VTI", "core", "BUY", "normal", 85, make_state(),
                 regime_level="UNKNOWN", expect_verdict="REJECT", expect_rules_failed=("HR03",)),
        Scenario("S5b", "Policy reviewer rated the evidence poor: fail closed", "VTI", "core", "BUY", "normal", 85,
                 make_state(), data_quality="poor", missing=("FRED macro series unavailable",),
                 expect_verdict="REJECT", expect_rules_failed=("HR05b",)),
        Scenario("S5c", "Stale portfolio state: fail closed before any rule runs", "VTI", "core", "BUY", "normal", 85,
                 make_state(as_of="2026-08-01"), expect_verdict="REJECT", expect_rules_failed=("HR00",)),
        Scenario("S6a", "NO TRADE stays a valid outcome", "VTI", "core", "NO_TRADE", "none", 44, make_state(),
                 expect_verdict="NO_ACTION"),
        Scenario("S6b", "HARD_HALT: risk-reducing sell of a held position still allowed", "VTI", "core", "REDUCE", "none", 70,
                 make_state(core_cash=2500 - 874.4, positions=held_vti), equity_override=(2700.0, 2250.0, 450.0),
                 expect_verdict="APPROVE"),
        # --- strategy policy (2026-09-03 directive) ---
        Scenario("S7a", "Core rejects a momentum-trading thesis", "VTI", "core", "BUY", "normal", 90, make_state(),
                 strategy="momentum_trading", expect_verdict="REJECT", expect_rules_failed=("HR15",)),
        Scenario("S7b", "Core rejects a short strategy outright", "VTI", "core", "BUY", "normal", 95, make_state(),
                 strategy="short", expect_verdict="REJECT", expect_rules_failed=("HR15",)),
        Scenario("S7c", "Sandbox momentum is not permitted yet and is permanently human-gated", "USMV", "sandbox",
                 "BUY", "normal", 90, make_state(), strategy="momentum", expect_verdict="REJECT",
                 expect_rules_failed=("HR15",)),
        Scenario("S7d", "Micro-Live bucket is architecture only and cannot deploy capital", "VTI", "micro_live",
                 "BUY", "normal", 95, make_state(), expect_verdict="REJECT", expect_rules_failed=("HR00",)),
        Scenario("S7e", "Core naked short is structurally impossible: SELL needs an existing position", "AAPL", "core",
                 "SELL", "none", 95, make_state(), expect_verdict="REJECT", expect_rules_failed=("HR14",)),
    ]


@dataclass
class Outcome:
    scenario: Scenario
    verdict: str
    final_action: str
    notional: float
    ladder_level: str
    failed: list[str]
    passed_expectation: bool
    advisories: list[str]
    requires_human_approval: bool = True


def run_scenario(sc: Scenario, profiles: Profiles) -> Outcome:
    try:
        snap = build_snapshot(sc.state, profiles, TRADE_DATE, price_lookup, identity_lookup)
    except Exception as exc:  # noqa: BLE001 - a stale/invalid state is itself a fail-closed outcome
        failed = ["HR00"]
        return Outcome(sc, "REJECT", "NO_TRADE", 0.0, "n/a", failed,
                       sc.expect_verdict == "REJECT" and set(sc.expect_rules_failed) <= set(failed),
                       [f"fail closed: {exc}"], True)
    if sc.equity_override:
        ladders = make_ladders(*sc.equity_override, profiles)
    else:
        ladders = make_ladders(snap.total_equity, snap.buckets["core"].equity, snap.buckets["sandbox"].equity, profiles)
    cls = classify_instrument(sc.ticker, profiles, identity_lookup)
    proposal = Proposal(ticker=sc.ticker, bucket=sc.bucket, action=sc.action, size_hint=sc.size_hint,
                        confidence=sc.confidence, trade_date=TRADE_DATE, cls=cls, data_quality=sc.data_quality,
                        missing_information=list(sc.missing), source="scenario", strategy=sc.strategy,
                        evidence=collect_evidence(sc.ticker, TRADE_DATE, price_lookup=price_lookup))
    engine = HardRiskEngine(profiles, halt_file="/nonexistent/HALT",
                            stock_info_lookup=lambda s: sc.stock_info or {},
                            earnings_lookup=earnings_lookup)
    result = engine.evaluate(proposal, snap, ladders, regime(sc.regime_level))
    failed = [r.rule_id for r in result.failed_rules]
    ok = result.verdict == sc.expect_verdict and set(sc.expect_rules_failed) <= set(failed)
    return Outcome(sc, result.verdict, result.final_action, result.proposed_notional_usd,
                   result.ladder.get("effective_level_for_bucket", "n/a"), failed, ok, result.advisories,
                   result.requires_human_approval)


def main(argv: list[str] | None = None) -> int:
    load_env()
    profiles = load_profiles("config")
    outcomes = [run_scenario(sc, profiles) for sc in build_scenarios()]
    width = max(len(o.scenario.title) for o in outcomes)
    print(f"{'id':4s} {'scenario':{width}s}  {'proposal':26s} {'ladder':15s} {'verdict':9s} {'final':9s} {'notional':>10s}  failed rules     ok")
    for o in outcomes:
        sc = o.scenario
        prop = f"{sc.action} {sc.ticker} {sc.bucket} c={sc.confidence}"
        print(f"{sc.key:4s} {sc.title:{width}s}  {prop:26s} {o.ladder_level:15s} {o.verdict:9s} {o.final_action:9s} "
              f"{o.notional:10,.2f}  {','.join(o.failed) or '-':16s} {'yes' if o.passed_expectation else 'NO'}")
    unstamped = [o.scenario.key for o in outcomes if not o.requires_human_approval]
    print(f"\nevery outcome carries requires_human_approval=True: {'yes' if not unstamped else 'NO: ' + ','.join(unstamped)}")
    bad = [o for o in outcomes if not o.passed_expectation]
    print(f"\n{len(outcomes) - len(bad)}/{len(outcomes)} scenarios behaved as expected"
          + (f"; recommended defaults used: {len(profiles.defaults_used)}" if profiles.defaults_used else ""))
    for o in outcomes:
        for a in o.advisories:
            if o.scenario.key.startswith(("S4", "S6b")):
                print(f"  [{o.scenario.key}] ! {a}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
