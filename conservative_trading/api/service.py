"""Product operations, in plain Python.

Everything a frontend can do lives here. There is no HTTP in this module, so the CLI, a
scheduler and the API server all run the same code. Each function returns a plain dict shaped
for a screen, never an internal object, and every write path calls the same engine functions
the CLI uses so policy, Hard Risk, approval and revalidation cannot be bypassed.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
import threading
import uuid
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from ..broker import make_broker
from ..broker.base import BrokerError
from ..execution import execute as execute_proposal
from ..execution import revalidate
from ..micro_live import BUCKET as MICRO_BUCKET
from ..micro_live import run_preflight
from ..portfolio import PortfolioError
from ..profile import ProfileError, Profiles, load_profiles
from ..proposals import (
    APPROVED,
    INVALIDATED,
    NEEDS_REANALYSIS,
    PROPOSED,
    REJECTED,
    Proposal,
    ProposalStore,
    utcnow,
)
from .status import (
    FRESHNESS_LABELS,
    OUTCOME_TO_PROPOSAL_STATUS,
    ProposalStatus,
    SystemStatus,
    micro_live_status,
    proposal_status,
)

logger = logging.getLogger(__name__)

BUCKETS = ("core", "sandbox", MICRO_BUCKET)


class ServiceError(RuntimeError):
    """A product-level failure with a message meant for a person."""


class Service:
    """One instance per process. Cheap to construct; holds no broker session between calls."""

    def __init__(self, config_dir: str | Path = "config"):
        self.config_dir = Path(config_dir)

    # ---------------------------------------------------------------- profiles
    def profiles(self) -> Profiles:
        try:
            return load_profiles(self.config_dir)
        except ProfileError as exc:
            raise ServiceError(f"configuration error: {exc}") from exc

    def _store(self, profiles: Profiles) -> ProposalStore:
        path = str((profiles.risk.get("broker") or {}).get("state_file", "state/proposals.json"))
        return ProposalStore(profiles.project_dir / path)

    def _micro_enabled(self, profiles: Profiles) -> bool:
        return bool((profiles.risk.get("micro_live_execution") or {}).get("enabled", False))

    def _broker(self, profiles: Profiles, bucket: str):
        """The venue that owns a bucket. Real money only ever comes from the Micro-Live path."""
        if bucket == MICRO_BUCKET:
            from ..broker.robinhood import RobinhoodBroker

            if not self._micro_enabled(profiles):
                raise ServiceError("real-money execution is disabled (micro_live_execution.enabled is false)")
            return RobinhoodBroker(profiles)
        return make_broker(profiles)

    # ---------------------------------------------------------------- health
    def health(self) -> dict[str, Any]:
        """One call a dashboard can poll: can the system act, and what is blocking it."""
        warnings: list[str] = []
        try:
            profiles = self.profiles()
        except ServiceError as exc:
            return {"status": SystemStatus.CONFIG_ERROR, "detail": str(exc), "warnings": [],
                    "checked_at": utcnow().isoformat()}

        halt_file = profiles.project_dir / str(
            profiles.risk_cfg("execution_policy", "kill_switch", "halt_file", default="state/HALT"))
        paused = halt_file.exists() or bool(
            profiles.risk_cfg("execution_policy", "kill_switch", "trading_halted", default=False))

        risk = self.risk_state()
        halted = risk.get("total", {}).get("manual_reset_required", False)

        claude = self.claude_status()
        if claude["state"] == "LIMIT_REACHED":
            status = SystemStatus.CLAUDE_LIMIT_REACHED
        elif halted:
            status = SystemStatus.HALTED
        elif paused:
            status = SystemStatus.PAUSED
        else:
            status = SystemStatus.READY

        if not _fred_configured(profiles):
            warnings.append("FRED macro data is unavailable, so inflation, rate and labour context cannot be "
                            "verified. The system records the gap, lowers confidence and prefers NO_TRADE. "
                            "This is an optional enhancement, not a blocker.")
        if profiles.unresolved and not profiles.accept_defaults:
            warnings.append(f"{len(profiles.unresolved)} risk parameters are documented defaults you have not "
                            "explicitly accepted; real-money execution is blocked until you do.")

        market = self.market_status()
        return {
            "status": status,
            "detail": {
                SystemStatus.READY: "able to research and to accept approvals",
                SystemStatus.PAUSED: "kill switch engaged: no orders will be submitted",
                SystemStatus.HALTED: "a drawdown ladder halt is active and needs a manual reset",
                SystemStatus.CLAUDE_LIMIT_REACHED: claude["detail"],
                SystemStatus.CONFIG_ERROR: "configuration error",
            }[status],
            "paused": paused,
            "stage": profiles.stage,
            "market": market,
            "claude": claude,
            "micro_live_enabled": self._micro_enabled(profiles),
            "warnings": warnings,
            "checked_at": utcnow().isoformat(),
        }

    def claude_status(self) -> dict[str, Any]:
        """Subscription state, read from the most recent runs. Never exposes credentials."""
        profiles = self.profiles()
        audit = profiles.project_dir / "runs" / "llm_calls.jsonl"
        last_call, calls_today = None, 0
        if audit.exists():
            today = datetime.now(UTC).date().isoformat()
            try:
                for line in audit.read_text(encoding="utf-8").splitlines()[-500:]:
                    row = json.loads(line)
                    last_call = row.get("ts", last_call)
                    if str(row.get("ts", "")).startswith(today):
                        calls_today += 1
            except (OSError, json.JSONDecodeError):
                pass
        state, detail = "OK", "subscription-backed Claude Code runtime"
        latest = _latest_run_error(profiles)
        if latest and "limit" in latest.lower():
            state, detail = "LIMIT_REACHED", latest
        return {"state": state, "detail": detail, "billing": "claude_subscription_no_api_charges",
                "calls_today": calls_today, "last_call_at": last_call}

    def market_status(self) -> dict[str, Any]:
        try:
            import pandas as pd

            from ..broker.robinhood import _nyse_calendar

            cal = _nyse_calendar()
            now = pd.Timestamp.now(tz="UTC")
            is_open = bool(cal.is_open_on_minute(now, ignore_breaks=True))
            return {"is_open": is_open, "calendar": cal.name,
                    "next_open": str(cal.next_open(now)) if not is_open else None,
                    "next_close": str(cal.next_close(now)) if is_open else None}
        except Exception as exc:  # noqa: BLE001 - an unknown session is reported closed
            return {"is_open": False, "calendar": None, "error": f"{type(exc).__name__}: {exc}"}

    # ---------------------------------------------------------------- portfolio
    def portfolio(self) -> dict[str, Any]:
        """Every bucket a screen can show, each from its own ledger."""
        from ..cli import portfolio_context

        profiles = self.profiles()
        trade_date = _last_session()
        out: dict[str, Any] = {"as_of": trade_date, "buckets": {}, "errors": {}}
        for bucket, sample in (("core", "core"), (MICRO_BUCKET, MICRO_BUCKET)):
            try:
                snap, _ladders, _regime, _text = portfolio_context(profiles, trade_date, sample)
            except (PortfolioError, ServiceError, ProfileError) as exc:
                out["errors"][bucket] = str(exc)
                continue
            for name, view in snap.buckets.items():
                out["buckets"][name] = {
                    "allocation_usd": view.allocation_usd,
                    "cash_usd": round(view.cash_usd, 2),
                    "positions_value_usd": round(view.market_value, 2),
                    "equity_usd": round(view.equity, 2),
                    "position_count": view.n_positions,
                    "is_real_money": name == MICRO_BUCKET,
                    "venue": "robinhood" if name == MICRO_BUCKET else None,
                    "positions": [{
                        "symbol": p.symbol, "quantity": p.quantity, "price": p.price,
                        "market_value_usd": round(p.market_value, 2),
                        "cost_basis_usd": round(p.cost_basis, 2),
                        "unrealized_pct": round(p.unrealized_pct, 2),
                        "weight_pct_of_allocation": round(p.weight_pct_of_bucket, 2),
                        "classification": p.cls.limit_class,
                        "exposure_group": p.cls.exposure_group,
                    } for p in view.positions],
                    "sleeves_pct": {k: round(v, 2) for k, v in view.sleeves_pct.items()},
                }
        out["total_equity_usd"] = round(sum(b["equity_usd"] for b in out["buckets"].values()), 2)
        return out

    def risk_state(self) -> dict[str, Any]:
        """Drawdown ladders and the market regime, in display form."""
        from ..risk import assess_regime, load_ladder_states

        try:
            profiles = self.profiles()
        except ServiceError:
            return {}
        ps = profiles.risk.get("portfolio_state") or {}
        micro = profiles.risk.get("micro_live_execution") or {}
        out: dict[str, Any] = {}
        for key, path in (("conceptual", ps.get("risk_state_file", "state/risk_state.json")),
                          ("micro_live", micro.get("risk_state_file", "state/micro_live_risk_state.json"))):
            ladders = load_ladder_states(profiles.project_dir / str(path), profiles)
            data = ladders.as_dict()
            if key == "conceptual":
                out.update({k: v for k, v in data.items() if k in ("total", "core", "sandbox")})
            elif data.get("micro_live"):
                out["micro_live"] = data["micro_live"]
        try:
            regime = assess_regime(profiles, _last_session())
            out["regime"] = regime.as_dict()
        except Exception as exc:  # noqa: BLE001
            out["regime"] = {"level": "UNKNOWN", "available": False, "detail": str(exc)}
        return out

    # ---------------------------------------------------------------- micro-live
    def micro_live(self) -> dict[str, Any]:
        """Real-money workflow status, the preflight gates, and the capital boundary."""
        profiles = self.profiles()
        micro = profiles.risk.get("micro_live_execution") or {}
        enabled = self._micro_enabled(profiles)
        broker, connected, connect_error = None, False, None
        try:
            from ..broker.robinhood import RobinhoodBroker

            broker = RobinhoodBroker(profiles)
            connected = True
        except Exception as exc:  # noqa: BLE001 - not connected is a normal state, not a crash
            connect_error = str(exc)
        try:
            preflight = run_preflight(profiles, broker)
            open_proposal = next((p for p in self._store(profiles).open_proposals(profiles)
                                  if p.bucket == MICRO_BUCKET), None)
            status = micro_live_status(enabled=enabled, connected=connected,
                                       preflight_ready=preflight.ready, open_proposal=open_proposal,
                                       profiles=profiles)
            return {
                "status": status,
                "enabled": enabled,
                "connected": connected,
                "connect_error": connect_error,
                "capital_boundary_usd": float(micro.get("max_capital_usd", 100)),
                "account": preflight.account,
                "preflight_ready": preflight.ready,
                "checks": [{"id": c.check_id, "name": c.name, "passed": c.passed, "detail": c.detail}
                           for c in preflight.checks],
                "blocking": [c.check_id for c in preflight.failures],
                "permissions": dict(micro.get("permissions") or {}),
                "execution_mode": str(micro.get("execution_mode", "human_approval")),
            }
        finally:
            if broker is not None:
                broker.close()

    # ---------------------------------------------------------------- proposals
    def proposals(self, include_closed: bool = False) -> list[dict[str, Any]]:
        profiles = self.profiles()
        store = self._store(profiles)
        items = store.all() if include_closed else store.open_proposals(profiles)
        return [self._proposal_summary(p, profiles) for p in sorted(items, key=lambda x: x.created_at, reverse=True)]

    def _proposal_summary(self, p: Proposal, profiles: Profiles) -> dict[str, Any]:
        freshness, why = p.freshness(profiles)
        rules = p.freshness_rules(profiles)
        return {
            "proposal_id": p.proposal_id,
            "status": proposal_status(p, profiles),
            "ticker": p.ticker,
            "bucket": p.bucket,
            "is_real_money": p.bucket == MICRO_BUCKET,
            "action": p.action,
            "strategy": p.strategy,
            "notional_usd": p.notional_usd,
            "target_weight_pct": p.target_weight_pct,
            "confidence": p.confidence,
            "requires_human_approval": p.requires_human_approval,
            "created_at": p.created_at,
            "approved_at": p.approved_at,
            "freshness": {"state": freshness, "label": FRESHNESS_LABELS.get(freshness, freshness),
                          "detail": why, "class": p.freshness_class(profiles),
                          "ttl_minutes": rules.get("ttl_minutes"),
                          "age_minutes": round(p.age_minutes(), 1),
                          "max_price_drift_pct": rules.get("max_price_drift_pct")},
            "reference_price": p.reference_price,
            "run_id": p.run_id,
        }

    def proposal(self, proposal_id: str) -> dict[str, Any]:
        """Full detail: the reasoning, the risk rules, and the world the proposal was born into."""
        profiles = self.profiles()
        p = self._store(profiles).get(proposal_id)
        if p is None:
            raise ServiceError(f"no proposal {proposal_id!r}")
        detail = self._proposal_summary(p, profiles)
        detail["history"] = p.history
        detail["world_at_proposal"] = {
            "ladder_level": p.ladder_level_at_proposal, "regime": p.regime_at_proposal,
            "portfolio_equity_usd": p.portfolio_equity_at_proposal,
            "bucket_cash_usd": p.bucket_cash_at_proposal, "data_quality": p.data_quality,
        }
        record = _find_record(profiles, p.run_id)
        if record:
            policy = record.get("policy") or {}
            proposal_doc = policy.get("proposal") or {}
            detail["research"] = {
                "portfolio_manager_rating": (record.get("upstream_decision") or {}).get("portfolio_manager_signal"),
                "policy_action": policy.get("final_action"),
                "policy_reasons": policy.get("reasons"),
                "confidence": policy.get("confidence"),
                "data_quality": proposal_doc.get("data_quality"),
                "market_risk_level": proposal_doc.get("market_risk_level"),
                "investment_thesis": proposal_doc.get("investment_thesis"),
                "bull_case": proposal_doc.get("bull_case"),
                "bear_case": proposal_doc.get("bear_case"),
                "primary_risks": proposal_doc.get("primary_risks"),
                "what_could_make_this_wrong": proposal_doc.get("what_could_make_this_thesis_wrong"),
                "why_doing_nothing_may_be_better": proposal_doc.get("alternative_why_doing_nothing_may_be_preferable"),
                "missing_information": proposal_doc.get("missing_information"),
                "entry_logic": proposal_doc.get("entry_logic"),
                "exit_logic": proposal_doc.get("exit_logic"),
                "expected_holding_period": proposal_doc.get("expected_holding_period"),
            }
            detail["hard_risk"] = _hard_risk_view(record.get("hard_risk") or {})
        return detail

    # ---------------------------------------------------------------- actions
    def revalidate_proposal(self, proposal_id: str) -> dict[str, Any]:
        """Show what would happen on approval, changing nothing."""
        profiles = self.profiles()
        p = self._store(profiles).get(proposal_id)
        if p is None:
            raise ServiceError(f"no proposal {proposal_id!r}")
        broker = self._broker(profiles, p.bucket)
        try:
            rv = revalidate(profiles, p, broker)
        finally:
            _close(broker)
        return {"proposal_id": p.proposal_id, "outcome": rv.outcome, "reasons": rv.reasons,
                "changes": rv.changes, "current_price": rv.current_price,
                "price_drift_pct": rv.price_drift_pct,
                "executable_notional_usd": rv.executable_notional_usd,
                "would_execute": rv.may_execute}

    def approve(self, proposal_id: str, note: str | None = None) -> dict[str, Any]:
        """Authorise the idea, then execute only if the current world still supports it.

        This is the whole safety model in one call: approval never submits the stored order. The
        world is re-read, Hard Risk runs again, the venue reviews the order, and only then is
        anything sent.
        """
        profiles = self.profiles()
        store = self._store(profiles)
        p = store.get(proposal_id)
        if p is None:
            raise ServiceError(f"no proposal {proposal_id!r}")
        if p.status not in (PROPOSED, APPROVED):
            raise ServiceError(f"proposal {p.proposal_id} is {p.status} and cannot be approved")
        broker = self._broker(profiles, p.bucket)
        try:
            if p.status == PROPOSED:
                p.status = APPROVED
                p.approved_at = utcnow().isoformat()
                p.approved_by = "investor"
                p.approval_note = note
                p.note("approved", note or "approved through the application interface")
                store.save(p)
            result = execute_proposal(profiles, p, broker, store)
        finally:
            _close(broker)
        status = OUTCOME_TO_PROPOSAL_STATUS.get(result.outcome, ProposalStatus.INVALIDATED)
        return {
            "proposal_id": p.proposal_id, "status": status, "outcome": result.outcome,
            "submitted": result.submitted, "duplicate_prevented": result.duplicate_prevented,
            "order": result.order, "error": result.error,
            "reasons": (result.revalidation or {}).get("reasons", []),
            "message": _approval_message(status, result),
        }

    def reject(self, proposal_id: str, reason: str | None = None) -> dict[str, Any]:
        profiles = self.profiles()
        store = self._store(profiles)
        p = store.get(proposal_id)
        if p is None:
            raise ServiceError(f"no proposal {proposal_id!r}")
        p.status = REJECTED
        p.note("rejected_by_investor", reason or "rejected through the application interface")
        store.save(p)
        return {"proposal_id": p.proposal_id, "status": ProposalStatus.REJECTED, "message": "proposal rejected"}

    def request_reanalysis(self, proposal_id: str) -> dict[str, Any]:
        """Mark a proposal as needing fresh research, then start that research."""
        profiles = self.profiles()
        store = self._store(profiles)
        p = store.get(proposal_id)
        if p is None:
            raise ServiceError(f"no proposal {proposal_id!r}")
        p.status = NEEDS_REANALYSIS
        p.note("reanalysis_requested", "the investor asked for fresh research")
        store.save(p)
        run = self.start_research(p.ticker, bucket=p.bucket, trigger="reanalysis")
        return {"proposal_id": p.proposal_id, "status": ProposalStatus.REANALYSIS_REQUIRED, "run": run}

    def invalidate(self, proposal_id: str, reason: str) -> dict[str, Any]:
        profiles = self.profiles()
        store = self._store(profiles)
        p = store.get(proposal_id)
        if p is None:
            raise ServiceError(f"no proposal {proposal_id!r}")
        p.status = INVALIDATED
        p.note("invalidated", reason)
        store.save(p)
        return {"proposal_id": p.proposal_id, "status": ProposalStatus.INVALIDATED}

    # ---------------------------------------------------------------- research
    def start_research(self, ticker: str, *, bucket: str = "core", instrument_type: str = "auto",
                       trade_date: str | None = None, trigger: str = "manual") -> dict[str, Any]:
        """Start a research run in the background and return its handle immediately.

        A run takes minutes and many model calls, so the caller polls ``research_run`` rather
        than holding a request open. The run is the same ``ct-research`` pipeline the CLI uses.
        """
        if bucket not in BUCKETS:
            raise ServiceError(f"unknown bucket {bucket!r}")
        profiles = self.profiles()
        date = trade_date or _last_session()
        run_key = f"{date}_{ticker.upper()}_{uuid.uuid4().hex[:8]}"
        log_dir = profiles.project_dir / "runs" / "api"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{run_key}.log"
        command = [sys.executable, "-m", "conservative_trading.cli", ticker,
                   "--date", date, "--bucket", bucket, "--instrument-type", instrument_type, "--quiet"]

        def _run() -> None:
            env = dict(_scrubbed_env())
            with log_path.open("w", encoding="utf-8") as fh:
                fh.write(f"# trigger={trigger} started={utcnow().isoformat()}\n")
                fh.flush()
                subprocess.run(command, stdout=fh, stderr=subprocess.STDOUT, check=False,
                               cwd=profiles.project_dir, env=env)

        threading.Thread(target=_run, name=f"research-{run_key}", daemon=True).start()
        return {"run_key": run_key, "ticker": ticker.upper(), "bucket": bucket, "trade_date": date,
                "trigger": trigger, "state": "RUNNING", "log": str(log_path)}

    def research_run(self, run_key: str) -> dict[str, Any]:
        profiles = self.profiles()
        log_path = profiles.project_dir / "runs" / "api" / f"{run_key}.log"
        if not log_path.exists():
            raise ServiceError(f"no research run {run_key!r}")
        text = log_path.read_text(encoding="utf-8")
        finished = "record:" in text or "FAILED" in text or "STOPPED" in text
        state = "RUNNING"
        if "STOPPED" in text and "limit" in text.lower():
            state = "CLAUDE_LIMIT_REACHED"
        elif "FAILED" in text:
            state = "FAILED"
        elif finished:
            state = "COMPLETE"
        return {"run_key": run_key, "state": state, "log_tail": text[-4000:],
                "proposal_created": "PROPOSAL" in text and "no proposal" not in text}

    # ---------------------------------------------------------------- trace

    #: Report-tree stage and file names, mapped to something a person reads.
    _TRACE_GROUPS = {
        "1_analysts": "Analysts",
        "2_research": "Research debate",
        "3_trading": "Trader",
        "4_risk": "Risk debate",
        "5_portfolio": "Portfolio manager",
    }
    _TRACE_STEPS = {
        "market": "Market analyst", "news": "News analyst",
        "social": "Social analyst", "fundamentals": "Fundamentals analyst",
        "bull": "Bull case", "bear": "Bear case", "manager": "Research manager",
        "trader": "Trade plan",
        "aggressive": "Aggressive risk analyst", "neutral": "Neutral risk analyst",
        "conservative": "Conservative risk analyst",
        "decision": "Portfolio decision",
    }
    _TRACE_CHAR_CAP = 24_000

    def research_trace(self, run_id: str, full: bool = True) -> dict[str, Any]:
        """One run's agent trace: the decision record plus every stage report it wrote.

        The pipeline leaves a report per agent under ``runs/<run_id>/reports``. That tree is
        the closest thing the system has to a step-by-step account of its own reasoning, so a
        frontend can render the run as a sequence rather than as one opaque verdict.
        """
        if not re.fullmatch(r"[A-Za-z0-9._-]+", run_id):
            raise ServiceError(f"invalid run id {run_id!r}")
        profiles = self.profiles()
        root = (profiles.project_dir / "runs" / run_id).resolve()
        runs_root = (profiles.project_dir / "runs").resolve()
        if runs_root not in root.parents or not root.is_dir():
            raise ServiceError(f"no research run {run_id!r}")

        record: dict[str, Any] = {}
        record_path = root / "decision_record.json"
        if record_path.exists():
            try:
                record = json.loads(record_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                record = {}

        steps: list[dict[str, Any]] = []
        reports = root / "reports"
        if reports.is_dir():
            for group_dir in sorted(d for d in reports.iterdir() if d.is_dir()):
                for path in sorted(group_dir.glob("*.md")):
                    try:
                        text = path.read_text(encoding="utf-8")
                    except OSError:
                        continue
                    steps.append({
                        "order": len(steps) + 1,
                        "group": group_dir.name,
                        "group_label": self._TRACE_GROUPS.get(group_dir.name,
                                                              group_dir.name.replace("_", " ")),
                        "key": path.stem,
                        "label": self._TRACE_STEPS.get(path.stem, path.stem.replace("_", " ")),
                        "chars": len(text),
                        "truncated": full and len(text) > self._TRACE_CHAR_CAP,
                        "markdown": text[: self._TRACE_CHAR_CAP] if full else None,
                    })

        policy = record.get("policy") or {}
        return {
            "run_id": record.get("run_id", run_id),
            "ticker": record.get("ticker"), "bucket": record.get("bucket"),
            "trade_date": record.get("trade_date"), "created_at": record.get("created_at"),
            "stage": record.get("stage"), "elapsed_seconds": record.get("elapsed_seconds"),
            "analysts": record.get("analysts") or [],
            "models": record.get("models") or {},
            "llm_usage": record.get("llm_usage") or {},
            "confidence": policy.get("confidence"),
            "policy_action": policy.get("final_action"),
            "hard_risk_verdict": (record.get("hard_risk") or {}).get("verdict"),
            "final_action": (record.get("final_recommendation") or {}).get("action"),
            "proposal_id": record.get("proposal_id"),
            "error": record.get("error"),
            "steps": steps,
        }

    def research_history(self, limit: int = 20) -> list[dict[str, Any]]:
        """Completed decision records, newest first."""
        profiles = self.profiles()
        runs = sorted((profiles.project_dir / "runs").glob("*/decision_record.json"),
                      key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
        out = []
        for path in runs:
            try:
                d = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            policy = d.get("policy") or {}
            out.append({
                "run_id": d.get("run_id"), "ticker": d.get("ticker"), "bucket": d.get("bucket"),
                "trade_date": d.get("trade_date"), "created_at": d.get("created_at"),
                "portfolio_manager_rating": (d.get("upstream_decision") or {}).get("portfolio_manager_signal"),
                "policy_action": policy.get("final_action"), "confidence": policy.get("confidence"),
                "hard_risk_verdict": (d.get("hard_risk") or {}).get("verdict"),
                "final_action": (d.get("final_recommendation") or {}).get("action"),
                "proposal_id": d.get("proposal_id"),
                "llm_calls": (d.get("llm_usage") or {}).get("calls"),
                "elapsed_seconds": d.get("elapsed_seconds"),
            })
        return out

    # ---------------------------------------------------------------- orders
    def orders(self, limit: int = 50) -> dict[str, Any]:
        """Fills recorded locally, plus live order state from the venue when connected."""
        profiles = self.profiles()
        fills_path = profiles.project_dir / str(
            (profiles.risk.get("broker") or {}).get("fills_file", "state/fills.jsonl"))
        fills = []
        if fills_path.exists():
            for line in fills_path.read_text(encoding="utf-8").splitlines()[-limit:]:
                try:
                    fills.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        live: list[dict[str, Any]] = []
        error = None
        if self._micro_enabled(profiles):
            try:
                broker = self._broker(profiles, MICRO_BUCKET)
                try:
                    live = [{"order_id": o.order_id, "symbol": o.symbol, "side": o.side,
                             "status": o.status, "quantity": o.quantity,
                             "filled_quantity": o.filled_quantity,
                             "filled_avg_price": o.filled_avg_price, "is_open": o.is_open}
                            for o in broker.get_open_orders()]
                finally:
                    _close(broker)
            except (ServiceError, BrokerError) as exc:
                error = str(exc)
        return {"fills": list(reversed(fills)), "open_orders": live, "error": error}

    # ---------------------------------------------------------------- controls
    def pause(self, reason: str = "paused through the application interface") -> dict[str, Any]:
        """Engage the kill switch. Research continues; no order can be submitted."""
        profiles = self.profiles()
        halt = profiles.project_dir / str(
            profiles.risk_cfg("execution_policy", "kill_switch", "halt_file", default="state/HALT"))
        halt.parent.mkdir(parents=True, exist_ok=True)
        halt.write_text(f"{utcnow().isoformat()} {reason}\n", encoding="utf-8")
        return {"paused": True, "reason": reason, "status": SystemStatus.PAUSED}

    def resume(self) -> dict[str, Any]:
        profiles = self.profiles()
        halt = profiles.project_dir / str(
            profiles.risk_cfg("execution_policy", "kill_switch", "halt_file", default="state/HALT"))
        halt.unlink(missing_ok=True)
        return {"paused": False, "status": SystemStatus.READY}


# ---------------------------------------------------------------------- helpers
def _close(broker: Any) -> None:
    if hasattr(broker, "close"):
        try:
            broker.close()
        except Exception as exc:  # noqa: BLE001
            logger.debug("error closing broker session: %s", exc)


def _scrubbed_env() -> dict[str, str]:
    from ..llm.claude_code import scrubbed_environment

    return scrubbed_environment()


def _fred_configured(profiles: Profiles) -> bool:
    """A real key, not an empty placeholder line copied from .env.example."""
    import os

    if os.environ.get("FRED_API_KEY", "").strip():
        return True
    try:
        for line in (profiles.project_dir / ".env").read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("FRED_API_KEY=") and stripped.split("=", 1)[1].strip():
                return True
    except OSError:
        return False
    return False


@lru_cache(maxsize=4)
def _session_has_data(session: str) -> bool:
    """Whether the data vendor has published a usable bar for this session.

    A vendor can lag hours behind the close, publishing a bar with volume but no closing price.
    Research and pricing must anchor to a session that is actually complete, so this probes a
    single liquid symbol rather than assuming the calendar and the vendor agree.
    """
    try:
        from tradingagents.dataflows.stockstats_utils import load_ohlcv

        df = load_ohlcv("SPY", session)
        return df is not None and not df.empty
    except Exception:  # noqa: BLE001 - any failure means "not usable"
        return False


def _last_session(max_lookback: int = 5) -> str:
    """The most recent trading session with usable data, which is what research anchors to."""
    import pandas as pd

    try:
        from ..broker.robinhood import _nyse_calendar

        cal = _nyse_calendar()
        now = pd.Timestamp.now(tz="America/New_York")
        session = cal.date_to_session(now.date(), direction="previous")
        if session.date() == now.date() and now.hour < 16:
            session = cal.date_to_session(session.date() - pd.Timedelta(days=1), direction="previous")
        for _ in range(max_lookback):
            candidate = str(session.date())
            if _session_has_data(candidate):
                return candidate
            session = cal.date_to_session(session.date() - pd.Timedelta(days=1), direction="previous")
        return str(session.date())
    except Exception:  # noqa: BLE001
        return (pd.Timestamp.now(tz="America/New_York") - pd.Timedelta(days=1)).date().isoformat()


def _find_record(profiles: Profiles, run_id: str | None) -> dict[str, Any] | None:
    if not run_id:
        return None
    path = profiles.project_dir / "runs" / run_id / "decision_record.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _hard_risk_view(hard_risk: dict[str, Any]) -> dict[str, Any]:
    return {
        "verdict": hard_risk.get("verdict"),
        "final_action": hard_risk.get("final_action"),
        "notional_usd": hard_risk.get("proposed_notional_usd"),
        "post_trade_weight_pct": hard_risk.get("post_trade_weight_pct"),
        "requires_human_approval": hard_risk.get("requires_human_approval", True),
        "rules": [{"id": r.get("rule_id"), "name": r.get("name"), "passed": r.get("passed"),
                   "detail": r.get("detail")} for r in hard_risk.get("rules", [])],
        "failed_rule_ids": hard_risk.get("failed_rule_ids", []),
        "advisories": hard_risk.get("advisories", []),
        "ladder": hard_risk.get("ladder"),
        "regime": hard_risk.get("regime"),
    }


def _approval_message(status: ProposalStatus, result: Any) -> str:
    return {
        ProposalStatus.EXECUTED: "the order was submitted and the portfolio was updated",
        ProposalStatus.STALE: "conditions moved on before execution; nothing was submitted. Request fresh research.",
        ProposalStatus.REANALYSIS_REQUIRED: "the world changed materially since the analysis; this idea needs "
                                            "fresh research rather than a re-priced old order",
        ProposalStatus.INVALIDATED: "the proposal is no longer valid; nothing was submitted",
        ProposalStatus.HARD_RISK_REJECTED: "the deterministic risk layer rejected this at execution time",
        ProposalStatus.BROKER_REVIEW_REJECTED: "the broker's own pre-trade review blocked it",
    }.get(status, result.error or "no order was submitted")


def _latest_run_error(profiles: Profiles) -> str | None:
    logs = sorted((profiles.project_dir / "runs").glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in logs[:3]:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            if "STOPPED" in line and "limit" in line.lower():
                return line.strip()[:300]
    return None
