"""Command line entry points.

``ct-research``   research run: TradingAgents -> Conservative Policy -> Hard Risk -> proposal
``ct-proposals``  list open proposals with their freshness
``ct-approve``    approve a proposal, then revalidate against the current world and execute
``ct-risk-check`` what-if check against the current portfolio, no model call

Approval never submits the stored order directly. It authorises the idea, and execution only
proceeds if the world still supports it (see agentic_trading.execution).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from . import autonomy, diagnosis
from . import sources as research_sources
from .broker import make_broker
from .env import load_env
from .evidence import collect_evidence, foreign_bucket_mentions
from .execution import execute as execute_proposal
from .execution import revalidate
from .graph import AgenticTradingGraph, classify_instrument
from .llm.claude_code import (
    USAGE_LOG,
    ClaudeCodeError,
    append_progress,
    reset_usage_log,
    usage_summary,
)
from .policy import PolicyDecision, run_policy
from .portfolio import (
    PortfolioError,
    Snapshot,
    build_snapshot,
    load_portfolio,
    render_snapshot,
    upstream_identity_lookup,
)
from .portfolio import (
    classify_instrument as classify_holding,
)
from .profile import ProfileError, Profiles, load_profiles
from .proposals import (
    APPROVED,
    PROPOSED,
    ProposalStore,
    new_proposal,
    positions_fingerprint,
    utcnow,
)
from .records import build_record, write_record
from .risk import (
    HardRiskEngine,
    LadderStates,
    Proposal,
    assess_regime,
    evaluate_ladders,
    load_ladder_states,
    save_ladder_states,
)
from .sources import readings as source_readings
from .state_db import StateDB


def _print(msg: str) -> None:
    print(msg, flush=True)


def _today() -> str:
    return datetime.now(tz=ZoneInfo("America/New_York")).date().isoformat()


def portfolio_context(profiles: Profiles, trade_date: str, bucket: str = "core", *,
                      regime=None) -> tuple[Snapshot, LadderStates, object, str]:
    """Load the ledger that owns this bucket, price it, evaluate the ladders and the regime.

    Each portfolio is separate: the conceptual core/sandbox book, and the real-money Micro-Live
    account. They never share a cash reconciliation or a drawdown high-water mark.

    ``regime``, when given, is used instead of assessing it again for this trade date: the
    console's portfolio discards the regime, and assessing it asks the data vendor.
    """
    from .micro_live import BUCKET as MICRO_BUCKET

    ps = profiles.risk.get("portfolio_state") or {}
    if bucket == MICRO_BUCKET:
        micro = profiles.risk.get("micro_live_execution") or {}
        ledger_file = str(micro.get("ledger_file", "state/micro_live_portfolio.yaml"))
        ladder_file = str(micro.get("risk_state_file", "state/micro_live_risk_state.json"))
        required = (MICRO_BUCKET,)
    else:
        ledger_file = str(ps.get("file", "state/portfolio.yaml"))
        ladder_file = str(ps.get("risk_state_file", "state/risk_state.json"))
        required = ("core", "sandbox")
    state = load_portfolio(profiles.project_dir / ledger_file, required_buckets=required)
    snap = build_snapshot(state, profiles, trade_date)
    ladder_path = profiles.project_dir / ladder_file
    with StateDB(ladder_path).transaction():
        ladders = load_ladder_states(ladder_path, profiles)
        micro_equity = snap.buckets[MICRO_BUCKET].equity if MICRO_BUCKET in snap.buckets else None
        evaluate_ladders(ladders, profiles, trade_date,
                         snap.total_equity if "core" in snap.buckets else None,
                         snap.buckets["core"].equity if "core" in snap.buckets else None,
                         snap.buckets["sandbox"].equity if "sandbox" in snap.buckets else None,
                         micro_live_equity=micro_equity)
        save_ladder_states(ladder_path, ladders)
    if regime is None:
        regime = assess_regime(profiles, trade_date)
    governing = ladders.governing(bucket)
    text = render_snapshot(snap, {"total": {"level": governing.level, "drawdown_pct": governing.drawdown_pct}},
                           regime.as_dict())
    return snap, ladders, regime, text


def hard_risk_for(profiles: Profiles, policy: PolicyDecision, ticker: str, bucket: str, trade_date: str,
                  snap: Snapshot, ladders: LadderStates, regime, final_state=None,
                  llm_calls=()) -> tuple[Proposal, object]:
    cls = classify_holding(ticker, profiles, upstream_identity_lookup)
    evidence = {}
    if policy.final_action in ("BUY", "ADD"):
        keys = {"market": "market_report", "news": "news_report", "fundamentals": "fundamentals_report", "social": "sentiment_report"}
        required = [keys[name] for name in profiles.analysts_for(cls.instrument_type) if name in keys]
        evidence = collect_evidence(ticker, trade_date, reports=final_state,
                                    required_reports=required, bucket=bucket,
                                    known_buckets=tuple(snap.buckets), llm_calls=llm_calls)
    proposal = Proposal(
        ticker=ticker.upper(), bucket=bucket, action=policy.final_action, size_hint=policy.size_hint,
        confidence=policy.confidence, trade_date=trade_date, cls=cls,
        data_quality=(policy.proposal or {}).get("data_quality"),
        missing_information=list((policy.proposal or {}).get("missing_information") or []),
        evidence=evidence,
    )
    return proposal, HardRiskEngine(profiles).evaluate(proposal, snap, ladders, regime)


def _final(result, proposal: Proposal) -> dict:
    return {
        "action": result.final_action,
        "ticker": result.ticker,
        "bucket": result.bucket,
        "verdict": result.verdict,
        "notional_usd": result.proposed_notional_usd,
        "target_weight_pct_of_allocation": result.target_weight_pct_of_allocation,
        "post_trade_weight_pct": result.post_trade_weight_pct,
        "executable": False,
        "summary": (f"{result.verdict}: {result.final_action} {result.ticker} in {result.bucket}"
                    + (f" for ${result.proposed_notional_usd:,.2f}" if result.proposed_notional_usd else "")
                    + (f"; failed {', '.join(r.rule_id for r in result.failed_rules)}" if result.failed_rules else "")),
        "failed_rules": [{"id": r.rule_id, "name": r.name, "detail": r.detail} for r in result.failed_rules],
    }


def _deferred_to_plan(db, bucket: str, ticker: str, action: str | None) -> dict[str, Any] | None:
    """Whether this conclusion belongs to an automatic-trading plan in force, and which.

    Only a purchase, only in the account a plan is for, and only of a fund the plan may buy.
    Everything else -- a sale, another account, a name outside the plan -- is written here as it
    always was. With no plan in force this is one small read that finds nothing.
    """
    from .auto import store as auto_store
    from .micro_live import BUCKET as MICRO_BUCKET

    if bucket != MICRO_BUCKET or action not in autonomy.OPENING_ACTIONS:
        return None
    try:
        plan = auto_store.active_plan(db)
    except Exception:  # noqa: BLE001 - an unreadable plan record defers nothing
        return None
    universe = {str(s).upper() for s in ((plan or {}).get("strategy") or {}).get("universe") or []}
    if plan is None or ticker.upper() not in universe:
        return None
    return {"deferred": True, "plan_id": plan["plan_id"], "opening_version": plan["opening_version"]}


def run_one(profiles: Profiles, ticker: str, trade_date: str, bucket: str, instrument_type: str,
            runs_dir: Path, debug: bool, *, run_id: str | None = None,
            authority_version_at_submit: str | None = None, purpose: str | None = None) -> int:
    reset_usage_log()
    started = time.time()
    started_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    if instrument_type == "auto":
        instrument_type = classify_instrument(ticker)
    asset_type = "crypto" if instrument_type == "crypto" else "stock"
    analysts = profiles.analysts_for(instrument_type)
    run_id = run_id or f"{trade_date}_{ticker.upper().replace('/', '-')}_{uuid.uuid4().hex[:8]}"
    run_dir = runs_dir / run_id
    # The authority this research is made under: the version its job was submitted with, or,
    # for a run started by hand, the version now. A proposal carries this one, not the version
    # at the moment it is written, so an authority that moved while the research ran is seen
    # as a change by the approval and never as "nothing moved" by the autonomous loop.
    store = _store(profiles)
    research_authority = authority_version_at_submit or autonomy.note_version(profiles, store.db)
    _print(f"[{ticker}] {instrument_type} | bucket={bucket} | analysts={analysts} | run={run_id}")
    progress_path = str(run_dir / "progress.jsonl")
    # Written before the portfolio snapshot, so a run that fails closed before any model call
    # still tells the console which desks it was going to use.
    append_progress(progress_path, {"kind": "run_started", "ticker": ticker, "bucket": bucket,
                                    "instrument_type": instrument_type, "analysts": analysts,
                                    "trade_date": trade_date})

    try:
        snap, ladders, regime, portfolio_text = portfolio_context(profiles, trade_date, bucket)
    except PortfolioError as exc:
        _print(f"[{ticker}] STOPPED (fail closed): portfolio context unavailable: {exc}")
        return diagnosis.EXIT_FAIL_CLOSED
    gov = ladders.for_bucket(bucket) if bucket != "core" else ladders.total
    _print(f"[{ticker}] ladder={gov.level} ({gov.drawdown_pct:+.2f}%) | regime={regime.level} | bucket cash=${snap.buckets[bucket].cash_usd:,.2f}")

    graph = AgenticTradingGraph(profiles, instrument_type=instrument_type, bucket=bucket, debug=debug,
                                     portfolio_text=portfolio_text, run_dir=run_dir)
    try:
        final_state, pm_signal = graph.propagate(ticker, trade_date, asset_type=asset_type)
    except ClaudeCodeError as exc:
        if exc.rate_limited:
            _print(f"[{ticker}] STOPPED: Claude subscription limit reached ({exc}). "
                   "Wait for the reset and rerun the same ticker and date; the run resumes from its checkpoint. "
                   "No API fallback is configured.")
            return diagnosis.EXIT_LIMIT
        if exc.login_expired:
            _print(f"[{ticker}] STOPPED: the Claude login on this Mac has expired ({exc}). "
                   "Sign in again (run `claude`, then /login); the run is retried, and nothing "
                   "was analysed or spent in the meantime.")
            return diagnosis.EXIT_LOGIN
        _print(f"[{ticker}] FAILED: LLM backend error: {exc}")
        return diagnosis.EXIT_PIPELINE

    report_path = graph.save_reports(final_state, ticker, run_dir / "reports")
    state_log = (
        Path(graph.config["results_dir"]) / ticker / "TradingAgentsStrategy_logs" / f"full_states_log_{trade_date}.json"
    )
    try:
        policy = run_policy(
            profiles, final_state, ticker=ticker, instrument_type=instrument_type, bucket=bucket,
            pm_rating=pm_signal, trade_date=trade_date, has_position=snap.has_position(ticker, bucket),
            portfolio_text=portfolio_text,
        )
    except ClaudeCodeError as exc:
        _print(f"[{ticker}] STOPPED during policy step: {exc}")
        return diagnosis.EXIT_LOGIN if exc.login_expired else diagnosis.EXIT_LIMIT

    proposal_obj, result = hard_risk_for(profiles, policy, ticker, bucket, trade_date, snap, ladders,
                                         regime, final_state, llm_calls=list(USAGE_LOG))
    final = _final(result, proposal_obj)
    # Checked on every run, not only the ones that propose a trade. The run this check was
    # written for recommended NO_TRADE, so the evidence path — which is only walked for BUY and
    # ADD — would never have looked at it. A report about the wrong account is worth knowing
    # about whatever it concluded, because the same reports are what a human reads afterwards.
    off_book = foreign_bucket_mentions(final_state, tuple(snap.buckets))

    record = build_record(
        run_id=run_id, ticker=ticker, instrument_type=instrument_type, bucket=bucket,
        trade_date=trade_date, profiles=profiles, analysts=analysts, pm_signal=pm_signal,
        final_state=final_state, policy=policy, report_path=report_path,
        state_log_path=state_log if state_log.exists() else None,
        llm_usage=usage_summary(), llm_calls=list(USAGE_LOG), elapsed_seconds=time.time() - started,
        hard_risk=result.as_dict(), final_recommendation=final,
        portfolio={"snapshot_text": portfolio_text, "as_of": snap.as_of, "total_equity": snap.total_equity},
        # A graph that keeps no log of its reads (a stand-in for one) simply has none to show.
        source_readings=source_readings(getattr(graph, "sources_log", None)),
    )
    proposal = None
    authority_now = autonomy.note_version(profiles, store.db)
    authority_moved = authority_now != research_authority
    record["authority"] = {"research": research_authority, "proposal": authority_now,
                           "changed_during_research": authority_moved}
    # Named in the record rather than inferred from it later: which route produced this, and
    # why it was asked for. A future skill route writes its own name and version here.
    record["route"] = {"name": "tradingagents", "skill_version": None}
    record["purpose"] = purpose
    # Where an automatic-trading plan is in force for this account, a purchase of one of its
    # funds is the plan's to write, not this run's: its proposal carries the strategy version
    # the position will answer to, which is what lets code close what code opened. Two
    # proposals for one conclusion would also hold each other up until both expired. The
    # conclusion is all here in the record, where the plan's next pass reads it.
    deferred = _deferred_to_plan(store.db, bucket, ticker, result.final_action) if (
        result.verdict == "APPROVE") else None
    if deferred is not None:
        record["auto_plan"] = deferred
    if result.verdict == "APPROVE" and deferred is None:
        proposal = new_proposal(
            authority_version=research_authority,
            authority_changed_during_research=authority_moved,
            run_id=run_id, ticker=ticker, bucket=bucket, action=result.final_action,
            strategy=proposal_obj.strategy, instrument_type=instrument_type,
            limit_class=proposal_obj.cls.limit_class, size_hint=policy.size_hint,
            confidence=policy.confidence, notional_usd=result.proposed_notional_usd,
            sell_quantity=result.proposed_quantity,
            target_weight_pct=result.target_weight_pct_of_allocation, trade_date=trade_date,
            # An authority that moved while the research ran is not weighed here: the proposal
            # goes to a person, who sees the change (ProposalDetail.authority_changed) and
            # acknowledges it if it loosened. Code never treats it as "nothing moved".
            requires_human_approval=result.requires_human_approval or authority_moved,
            reference_price=proposal_obj.evidence.get("price") or _reference_price(snap, ticker, trade_date),
            reference_price_date=proposal_obj.evidence.get("price_date", trade_date), ladder_level_at_proposal=ladders.governing(bucket).level,
            regime_at_proposal=regime.level, portfolio_equity_at_proposal=snap.total_equity,
            bucket_cash_at_proposal=snap.buckets[bucket].cash_usd,
            positions_fingerprint=positions_fingerprint({p.symbol: p.quantity for p in snap.state.positions}),
            data_quality=(policy.proposal or {}).get("data_quality"),
            evidence=proposal_obj.evidence, missing_information=proposal_obj.missing_information,
        )
        if authority_moved:
            proposal.note("authority_changed_during_research",
                          f"researched under authority {research_authority}, written under "
                          f"{authority_now}; a person decides this one")
        store.save(proposal)
        record["proposal_id"] = proposal.proposal_id
        record["proposal"] = proposal.as_dict()
    record["bucket_context"] = {"analysed": bucket, "snapshot_buckets": sorted(snap.buckets),
                                "foreign_mentions": off_book}
    record["llm_calls_audit_file"] = str(runs_dir / "llm_calls.jsonl")
    record["started_at"] = started_at
    # Why it ended as it did, named once and kept with the record: every reader of "no trade"
    # was left to work out which of five different things had happened.
    # Under its own key: ``outcome`` is reserved for what came of the decision afterwards.
    outcome = diagnosis.classify_record(record)
    record["diagnosis"] = outcome.as_dict()
    record_path = write_record(run_dir / "decision_record.json", record)
    # The last line the console reads for this run: what the deterministic gate decided, which
    # is never what an agent decided.
    append_progress(progress_path, {"kind": "run_finished", "policy_action": policy.final_action,
                                    "confidence": policy.confidence,
                                    "hard_risk": result.verdict,
                                    "final_action": final["action"],
                                    "proposal_id": record.get("proposal_id"),
                                    "diagnosis": {"category": outcome.category,
                                                  "code": outcome.code}})
    usage = record["llm_usage"]
    _print(f"[{ticker}] PM rating: {pm_signal} | policy: {policy.final_action} ({policy.size_hint}) "
           f"| confidence: {policy.confidence} | reviewer: {policy.llm_action}")
    for reason in policy.reasons:
        _print(f"[{ticker}]   - {reason}")
    if off_book:
        _print(f"[{ticker}] BUCKET CONTEXT: the reports reason about "
               f"{', '.join(name.upper() for name in off_book)}, which was not in the snapshot "
               f"given to them (this analysis is {bucket.upper()}). Read them with that in mind.")
    _print(f"[{ticker}] HARD RISK: {final['summary']}")
    for r in result.failed_rules:
        _print(f"[{ticker}]   x {r.rule_id} {r.name}: {r.detail}")
    for a in result.advisories:
        _print(f"[{ticker}]   ! {a}")
    _print(f"[{ticker}] FINAL RECOMMENDATION: {final['action']}")
    _print(f"[{ticker}] WHY: {outcome.category} / {outcome.code}: {outcome.lead.text()['en']}")
    if proposal is not None:
        state, why = proposal.freshness(profiles)
        _print(f"[{ticker}] PROPOSAL {proposal.proposal_id} created ({state}; {why})")
        _print(f"[{ticker}]   approve with: uv run ct-approve {proposal.proposal_id}")
    else:
        _print(f"[{ticker}] no proposal created (nothing actionable); no broker activity")
    _print(f"[{ticker}] llm calls={usage['calls']} in={usage['input_tokens']} out={usage['output_tokens']} "
           f"cache_w={usage['cache_creation_input_tokens']} cache_r={usage['cache_read_input_tokens']} "
           f"tools={usage['tool_calls']} wall={record['elapsed_seconds']}s")
    _print(f"[{ticker}] record: {record_path}")
    _print(f"[{ticker}] report: {report_path}")
    return 0


def _store(profiles: Profiles) -> ProposalStore:
    path = str((profiles.risk.get("broker") or {}).get("state_file", "state/proposals.json"))
    return ProposalStore(profiles.project_dir / path)


def _reference_price(snap, ticker: str, trade_date: str) -> float | None:
    """The price the analysis was anchored to, for the later drift check."""
    for bucket in snap.buckets.values():
        for pos in bucket.positions:
            if pos.symbol == ticker.upper():
                return pos.price
    try:
        from tradingagents.dataflows.stockstats_utils import load_ohlcv

        df = load_ohlcv(ticker, trade_date)
        return float(df.iloc[-1]["Close"]) if df is not None and not df.empty else None
    except Exception:  # noqa: BLE001 - a missing reference only disables the drift check
        return None


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--date", default=_today(), help="analysis date, yyyy-mm-dd (default: today in US market time)")
    parser.add_argument("--bucket", choices=["core", "sandbox", "micro_live"], default="core")
    parser.add_argument("--config-dir", default="config")


def _load(config_dir: str) -> Profiles | None:
    try:
        profiles = load_profiles(config_dir)
    except ProfileError as exc:
        _print(f"profile error: {exc}")
        return None
    if profiles.stage > 2:
        _print(f"this tool is for research and recommendation stages; execution_policy.stage is {profiles.stage}.")
        return None
    return profiles


def main(argv: list[str] | None = None) -> int:
    load_env()
    parser = argparse.ArgumentParser(prog="ct-research",
                                     description="TradingAgents -> Conservative Policy -> Hard Risk. No broker, no orders.")
    parser.add_argument("tickers", nargs="+")
    _common(parser)
    parser.add_argument("--instrument-type", choices=["auto", "etf", "stock", "crypto"], default="auto")
    parser.add_argument("--runs-dir", default="runs")
    parser.add_argument("--debug", action="store_true", help="stream upstream messages while running")
    parser.add_argument("--quiet", action="store_true", help="hide upstream INFO log lines")
    parser.add_argument("--without-sources", default="", metavar="NAMES",
                        help="leave these outside sources out of this run (reddit, sec_form4; comma "
                             "separated). It can only leave out one the profile has on: a control "
                             "run beside one that reads them, with nothing else changed")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING if args.quiet else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    left_out = sorted({name.strip() for name in args.without_sources.split(",") if name.strip()})
    unknown = [name for name in left_out if name not in (research_sources.REDDIT, research_sources.FORM4)]
    if unknown:
        _print(f"--without-sources names no such source: {', '.join(unknown)} "
               f"(known: {research_sources.REDDIT}, {research_sources.FORM4})")
        return 2
    if left_out:
        os.environ[research_sources.WITHOUT_ENV] = ",".join(left_out)

    profiles = _load(args.config_dir)
    if profiles is None:
        return 2
    if profiles.unresolved:
        _print(f"note: {len(profiles.unresolved)} profile fields are TODO; recommended defaults are used at this stage "
               "and every use is listed in the decision record.")
    runs_dir = Path(args.runs_dir)
    worst = 0
    for ticker in args.tickers:
        code = run_one(profiles, ticker, args.date, args.bucket, args.instrument_type, runs_dir, args.debug)
        worst = max(worst, code)
        if code == 3:
            break
    return worst


def risk_check_main(argv: list[str] | None = None) -> int:
    load_env()
    parser = argparse.ArgumentParser(prog="ct-risk-check",
                                     description="Evaluate a hypothetical proposal against the current portfolio. No model call.")
    parser.add_argument("ticker")
    _common(parser)
    parser.add_argument("--action", choices=["BUY", "ADD", "HOLD", "REDUCE", "SELL", "NO_TRADE"], default="BUY")
    parser.add_argument("--size", choices=["normal", "small"], default="normal")
    parser.add_argument("--confidence", type=int, default=80)
    parser.add_argument("--data-quality", choices=["good", "partial", "poor"], default="good")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)
    profiles = _load(args.config_dir)
    if profiles is None:
        return 2
    try:
        snap, ladders, regime, text = portfolio_context(profiles, args.date, args.bucket)
    except PortfolioError as exc:
        _print(f"STOPPED (fail closed): {exc}")
        return 2
    _print(text)
    cls = classify_holding(args.ticker, profiles, upstream_identity_lookup)
    evidence = collect_evidence(args.ticker, args.date) if args.action in ("BUY", "ADD") else {}
    proposal = Proposal(ticker=args.ticker.upper(), bucket=args.bucket, action=args.action, size_hint=args.size,
                        confidence=args.confidence, trade_date=args.date, cls=cls, data_quality=args.data_quality,
                        source="ct-risk-check", evidence=evidence)
    result = HardRiskEngine(profiles).evaluate(proposal, snap, ladders, regime)
    _print("")
    _print(f"proposal: {args.action} {args.ticker.upper()} ({cls.limit_class}) in {args.bucket}, size {args.size}, confidence {args.confidence}")
    for r in result.rules:
        _print(f"  {'PASS' if r.passed else 'FAIL'} {r.rule_id:5s} {r.name}: {r.detail}")
    for a in result.advisories:
        _print(f"  !    {a}")
    _print(f"verdict: {result.verdict} -> {result.final_action}"
           + (f" ${result.proposed_notional_usd:,.2f} ({result.post_trade_weight_pct:.1f}% of allocation post-trade)" if result.proposed_notional_usd else ""))
    if result.defaults_used:
        _print(f"recommended defaults used: {len(result.defaults_used)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())


def proposals_main(argv: list[str] | None = None) -> int:
    load_env()
    parser = argparse.ArgumentParser(prog="ct-proposals", description="List proposals and their freshness.")
    parser.add_argument("--config-dir", default="config")
    parser.add_argument("--all", action="store_true", help="include closed proposals")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)
    profiles = _load(args.config_dir)
    if profiles is None:
        return 2
    store = _store(profiles)
    items = store.all() if args.all else store.open_proposals(profiles)
    if not items:
        _print("no proposals" + ("" if args.all else " open"))
        return 0
    _print(f"{'proposal id':46s} {'status':10s} {'freshness':10s} {'action':9s} {'bucket':8s} {'notional':>10s}  strategy")
    for p in sorted(items, key=lambda x: x.created_at):
        state, why = p.freshness(profiles)
        _print(f"{p.proposal_id:46s} {p.status:10s} {state:10s} {p.action:9s} {p.bucket:8s} "
               f"{p.notional_usd:10,.2f}  {p.strategy}")
        _print(f"    {why}" + (f" | approval: {p.approved_at}" if p.approved_at else ""))
    return 0


def approve_main(argv: list[str] | None = None) -> int:
    load_env()
    parser = argparse.ArgumentParser(
        prog="ct-approve",
        description="Approve a proposal, revalidate against the current world, then execute if it still holds.")
    parser.add_argument("proposal_id")
    parser.add_argument("--config-dir", default="config")
    parser.add_argument("--note", default=None, help="why you approved it, kept in the audit trail")
    parser.add_argument("--dry-run", action="store_true", help="revalidate and stop before submitting")
    parser.add_argument("--revalidate-only", action="store_true", help="show the revalidation and change nothing")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)
    profiles = _load(args.config_dir)
    if profiles is None:
        return 2
    store = _store(profiles)
    try:
        proposal = store.get(args.proposal_id)
    except ValueError as exc:
        _print(str(exc))
        return 2
    if proposal is None:
        _print(f"no proposal matches {args.proposal_id!r}")
        return 2

    try:
        broker = make_broker(profiles)
    except Exception as exc:  # noqa: BLE001 - a broker we cannot build means no execution
        _print(f"broker unavailable: {exc}")
        return 2
    _print(f"broker: {broker.name} (paper={broker.is_paper})")

    if args.revalidate_only:
        rv = revalidate(profiles, proposal, broker)
        _print(f"revalidation: {rv.outcome}")
        for r in rv.reasons:
            _print(f"  - {r}")
        return 0

    if proposal.status == PROPOSED:
        proposal.status = APPROVED
        proposal.approved_at = utcnow().isoformat()
        proposal.approved_by = "investor"
        proposal.approved_via = "cli"
        proposal.approval_note = args.note
        proposal.note("approved", args.note or "approved via ct-approve")
        store.save(proposal)
        _print(f"approved {proposal.proposal_id}; revalidating before execution")

    result = execute_proposal(profiles, proposal, broker, store, dry_run=args.dry_run)
    _print(f"outcome: {result.outcome} (order submitted: {'yes' if result.submitted else 'no'})")
    for r in (result.revalidation.get("reasons") or []):
        _print(f"  - {r}")
    if result.error:
        _print(f"  error: {result.error}")
    if result.order:
        o = result.order
        _print(f"  order {o['order_id']} {o['side']} {o['symbol']} status={o['status']} "
               f"filled={o['filled_quantity']:g} @ {o['filled_avg_price']}")
    if result.duplicate_prevented:
        _print("  duplicate submission prevented; no second order was created")
    return 0 if result.outcome in ("SUBMITTED", "DRY_RUN_VALID", "DRY_RUN_REDUCED") else 1
