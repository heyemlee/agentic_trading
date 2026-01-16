"""Product operations, in plain Python.

Everything a frontend can do lives here. There is no HTTP in this module, so the CLI, a
scheduler and the API server all run the same code. Each function returns a plain dict shaped
for a screen, never an internal object, and every write path calls the same engine functions
the CLI uses so policy, Hard Risk, approval and revalidation cannot be bypassed.
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from datetime import date as calendar_date
from functools import lru_cache
from pathlib import Path
from typing import Any, ClassVar

from ..broker import make_broker
from ..broker.base import BrokerError
from ..env import load_env
from ..execution import execute as execute_proposal
from ..execution import needs_reconciliation, reconcile_proposal, revalidate
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
from ..research_jobs import ResearchJobManager
from ..state_db import StateDB
from ..storage import file_lock
from .status import (
    FRESHNESS_LABELS,
    ProposalStatus,
    SystemStatus,
    micro_live_status,
    proposal_status,
)

logger = logging.getLogger(__name__)

BUCKETS = ("core", "sandbox", MICRO_BUCKET)


class ServiceError(RuntimeError):
    """A product-level failure with a message meant for a person."""


class ServiceConflict(ServiceError):
    """A stale version or conflicting retry; fetch current state before acting."""


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

    def state_db(self, profiles: Profiles | None = None) -> StateDB:
        """The one transactional database behind this deployment's state directory."""
        return self._store(profiles or self.profiles()).db

    def _research_manager(self, profiles: Profiles) -> ResearchJobManager:
        store = self._store(profiles)
        return ResearchJobManager(
            state_source=store.path, project_dir=profiles.project_dir,
            config_dir=profiles.config_dir, settings=profiles.risk.get("research_jobs") or {},
        )

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
        jobs = self._research_manager(profiles).store.list(limit=1)
        if jobs and jobs[0].get("state") == "CLAUDE_LIMIT_REACHED":
            state = "LIMIT_REACHED"
            detail = jobs[0].get("error") or "Claude subscription limit reached"
        latest = _latest_run_error(profiles)
        if state == "OK" and latest and "limit" in latest.lower():
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
    #: Which ledger owns which buckets. Two separate books: the conceptual core/sandbox
    #: portfolio, and the real-money Micro-Live account at the venue. They never share a cash
    #: reconciliation, a high-water mark or a total.
    ACCOUNTS: ClassVar = (
        ("conceptual", "core", ("core", "sandbox"), False, None, "total"),
        (MICRO_BUCKET, MICRO_BUCKET, (MICRO_BUCKET,), True, "robinhood", MICRO_BUCKET),
    )

    def portfolio(self) -> dict[str, Any]:
        """Every bucket a screen can show, and the account each one belongs to.

        Equity, funding, high-water mark and drawdown are reported per account. A figure that
        added the paper portfolio to the real-money account would answer no question anyone
        has, and comparing such a sum against one account's high-water mark misstates both the
        assets and the drawdown, which is what this shape exists to prevent.
        """
        from ..cli import portfolio_context
        from ..portfolio import cash_flow_summary

        profiles = self.profiles()
        trade_date = _last_session()
        out: dict[str, Any] = {"as_of": trade_date, "buckets": {}, "accounts": {}, "errors": {}}
        for account, sample, buckets, real, venue, ladder_key in self.ACCOUNTS:
            try:
                snap, ladders, _regime, _text = portfolio_context(profiles, trade_date, sample)
            except (PortfolioError, ServiceError, ProfileError) as exc:
                out["errors"][account] = str(exc)
                continue
            views = {name: view for name, view in snap.buckets.items() if name in buckets}
            if not views:
                continue
            for name, view in views.items():
                out["buckets"][name] = {
                    "account": account,
                    "allocation_usd": view.allocation_usd,
                    "cash_usd": round(view.cash_usd, 2),
                    "positions_value_usd": round(view.market_value, 2),
                    "equity_usd": round(view.equity, 2),
                    "position_count": view.n_positions,
                    "is_real_money": real,
                    "venue": venue,
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
            ladder = getattr(ladders, ladder_key, None)
            equity = round(sum(v.equity for v in views.values()), 2)
            # Funding is the configured allocation the bucket opened with, plus every deposit
            # and withdrawal recorded since. Additive on purpose: treating recorded flows as
            # the whole history would turn a bucket's original capital into fabricated profit
            # the moment a single later deposit was written down.
            recorded = cash_flow_summary(snap.state)
            opening = round(sum(profiles.capital_allocation(name) for name in views), 2)
            flows = round(sum(recorded.get(name, 0.0) for name in views), 2)
            deposits = round(opening + flows, 2)
            out["accounts"][account] = {
                "buckets": sorted(views),
                "is_real_money": real,
                "venue": venue,
                "source": "robinhood account, reconciled" if real else "local conceptual ledger",
                "as_of": snap.as_of,
                "priced_for": snap.trade_date,
                "equity_usd": equity,
                "cash_usd": round(sum(v.cash_usd for v in views.values()), 2),
                "positions_value_usd": round(sum(v.market_value for v in views.values()), 2),
                "net_deposits_usd": deposits,
                "opening_allocation_usd": opening,
                "recorded_flows_usd": flows,
                "net_deposits_basis": "configured allocation plus recorded deposits and withdrawals",
                "investment_pnl_usd": round(equity - deposits, 2),
                "return_pct": round((equity - deposits) / deposits * 100, 2) if deposits else None,
                "high_water_mark_usd": round(ladder.high_water_mark_usd, 2) if ladder else None,
                "drawdown_pct": round(ladder.drawdown_pct, 2) if ladder else None,
                "ladder_level": ladder.level if ladder else None,
            }
        accounts = out["accounts"]
        out["combined"] = {
            "equity_usd": round(sum(a["equity_usd"] for a in accounts.values()), 2),
            "accounts": sorted(accounts),
            "complete": not out["errors"],
            # True whenever the sum spans both books, which is precisely when it must not be
            # compared against any single account's high-water mark.
            "mixes_real_and_conceptual": len({a["is_real_money"] for a in accounts.values()}) > 1,
        }
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
            "version": p.version(),
            "sell_quantity": p.sell_quantity,
            "order_status": p.order_status,
            "reconciliation_error": p.reconciliation_error,
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
            "evidence": p.evidence, "missing_information": p.missing_information,
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
        if p.status == PROPOSED:
            p = replace(p, status=APPROVED, approved_at=utcnow().isoformat())
        broker = self._broker(profiles, p.bucket)
        try:
            rv = revalidate(profiles, p, broker, persist_risk=False)
        finally:
            _close(broker)
        return {"proposal_id": p.proposal_id, "outcome": rv.outcome, "reasons": rv.reasons,
                "changes": rv.changes, "current_price": rv.current_price,
                "price_drift_pct": rv.price_drift_pct,
                "executable_notional_usd": rv.executable_notional_usd,
                "would_execute": rv.may_execute}

    def approve(self, proposal_id: str, note: str | None = None, *, identity: str | None = None,
                version: str | None = None, request_id: str | None = None,
                terms: dict[str, Any] | None = None) -> dict[str, Any]:
        """Approve a specific idea once, under the verified caller's identity."""
        profiles = self.profiles()
        store = self._store(profiles)
        initial = store.get(proposal_id)
        if initial is None or initial.proposal_id != proposal_id:
            raise ServiceError(f"no proposal {proposal_id!r}")
        account_key = "micro_live" if initial.bucket == MICRO_BUCKET else "conceptual"
        # Every approval/execution for an account shares this lock. The proposal lock is only
        # held for short state transitions, and never across a broker network request.
        with file_lock(store.db.path.with_name(f"{account_key}.execution")):
            with store.locked():
                p = store.get(proposal_id)
                if p is None or p.proposal_id != proposal_id:
                    raise ServiceError(f"no proposal {proposal_id!r}")
                if p.bucket != initial.bucket:
                    raise ServiceConflict("proposal account changed; review it again")
                if not identity or not version or not request_id or terms is None:
                    raise ServiceError("verified identity, explicit terms, version and request id are required")
                expected = {"ticker": p.ticker, "bucket": p.bucket, "action": p.action,
                            "notional_usd": p.notional_usd, "sell_quantity": p.sell_quantity}
                if terms != expected:
                    raise ServiceConflict("approval terms changed; review the current proposal")
                if any(other.approval_request_id == request_id and other.proposal_id != p.proposal_id
                       for other in store.all()):
                    raise ServiceConflict("request id already belongs to another proposal")
                replay = p.approval_request_id == request_id
                if replay:
                    if p.approved_by != identity or p.approved_version != version:
                        raise ServiceConflict("request id does not match the original approval")
                    if p.approval_response is not None:
                        return p.approval_response
                elif p.status != PROPOSED or p.version() != version:
                    raise ServiceConflict("proposal version or status changed; review it again")
                if not replay:
                    p.status = APPROVED
                    p.approved_at = utcnow().isoformat()
                    p.approved_by = identity
                    p.approval_note = note
                    p.approval_request_id = request_id
                    p.approved_version = version
                    p.note("approved", note or "explicit approval through the console",
                           identity=identity, version=version, request_id=request_id, terms=terms)
                    store.save(p)
            broker = self._broker(profiles, p.bucket)
            try:
                result = execute_proposal(profiles, p, broker, store)
            finally:
                _close(broker)
            with store.locked():
                p = store.get(proposal_id)
                if p.approval_response is not None:
                    return p.approval_response
                status = proposal_status(p, profiles)
                response = {
                    "proposal_id": p.proposal_id, "status": status, "outcome": result.outcome,
                    "submitted": result.submitted, "duplicate_prevented": result.duplicate_prevented,
                    "order": result.order, "error": result.error,
                    "reasons": (result.revalidation or {}).get("reasons", []),
                    "message": _approval_message(status, result),
                }
                p.approval_response = response
                store.save(p)
                return response

    def reconcile_orders(self) -> dict[str, Any]:
        """Recover submitted orders independently of human approvals or the live switch.

        Only account/order READ methods are called. This must continue after a pause.
        No broker connection is opened if there is nothing to recover.
        """
        profiles = self.profiles()
        store = self._store(profiles)
        results = []
        for proposal in store.all():
            if not needs_reconciliation(proposal):
                continue
            broker = None
            try:
                if proposal.bucket == MICRO_BUCKET:
                    from ..broker.robinhood import RobinhoodBroker
                    broker = RobinhoodBroker(profiles)
                else:
                    broker = self._broker(profiles, proposal.bucket)
                result = reconcile_proposal(profiles, proposal, broker, store)
                results.append(result.as_dict())
            except Exception as exc:  # noqa: BLE001 - one unavailable venue must not stop recovery
                with store.locked():
                    current = store.get(proposal.proposal_id)
                    if current is not None and needs_reconciliation(current):
                        current.reconciliation_error = f"order synchronization unavailable: {exc}"
                        store.save(current)
                results.append({"proposal_id": proposal.proposal_id, "error": str(exc)})
            finally:
                if broker is not None:
                    _close(broker)
        self.record_activity("reconciliation", f"{len(results)} order(s) checked",
                             profiles=profiles)
        return {"orders": results}

    def reject(self, proposal_id: str, reason: str | None = None) -> dict[str, Any]:
        profiles = self.profiles()
        store = self._store(profiles)
        with store.locked():
            p = store.get(proposal_id)
            if p is None:
                raise ServiceError(f"no proposal {proposal_id!r}")
            if p.client_order_id or p.status not in (PROPOSED, APPROVED):
                raise ServiceConflict("an executing or closed proposal cannot be changed")
            p.status = REJECTED
            p.note("rejected_by_investor", reason or "rejected through the application interface")
            store.save(p)
        return {"proposal_id": p.proposal_id, "status": ProposalStatus.REJECTED, "message": "proposal rejected"}

    def request_reanalysis(self, proposal_id: str) -> dict[str, Any]:
        """Mark a proposal as needing fresh research, then start that research."""
        profiles = self.profiles()
        store = self._store(profiles)
        with store.locked():
            p = store.get(proposal_id)
            if p is None:
                raise ServiceError(f"no proposal {proposal_id!r}")
            if p.client_order_id or p.status not in (PROPOSED, APPROVED):
                raise ServiceConflict("an executing or closed proposal cannot be changed")
            p.status = NEEDS_REANALYSIS
            p.note("reanalysis_requested", "the investor asked for fresh research")
            store.save(p)
        run = self.start_research(p.ticker, bucket=p.bucket, trigger="reanalysis")
        return {"proposal_id": p.proposal_id, "status": ProposalStatus.REANALYSIS_REQUIRED, "run": run}

    def invalidate(self, proposal_id: str, reason: str) -> dict[str, Any]:
        profiles = self.profiles()
        store = self._store(profiles)
        with store.locked():
            p = store.get(proposal_id)
            if p is None:
                raise ServiceError(f"no proposal {proposal_id!r}")
            if p.client_order_id or p.status not in (PROPOSED, APPROVED):
                raise ServiceConflict("an executing or closed proposal cannot be changed")
            p.status = INVALIDATED
            p.note("invalidated", reason)
            store.save(p)
        return {"proposal_id": p.proposal_id, "status": ProposalStatus.INVALIDATED}

    # ---------------------------------------------------------------- research
    def start_research(self, ticker: str, *, bucket: str = "core", instrument_type: str = "auto",
                       trade_date: str | None = None, trigger: str = "manual") -> dict[str, Any]:
        """Persist and enqueue a research run, deduplicating an identical active run."""
        if bucket not in BUCKETS:
            raise ServiceError(f"unknown bucket {bucket!r}")
        ticker = ticker.upper().strip()
        if (not re.fullmatch(r"[A-Z0-9.^/-]{1,24}", ticker)
                or not any(character.isalnum() for character in ticker)):
            raise ServiceError("ticker contains unsupported characters")
        if instrument_type not in ("auto", "etf", "stock", "crypto"):
            raise ServiceError(f"unknown instrument type {instrument_type!r}")
        profiles = self.profiles()
        date = trade_date or _last_session()
        try:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
                raise ValueError
            calendar_date.fromisoformat(date)
        except ValueError as exc:
            raise ServiceError("trade_date must use ISO yyyy-mm-dd format") from exc
        safe_ticker = re.sub(r"[^A-Z0-9._-]", "-", ticker)
        run_key = f"{date}_{safe_ticker}_{uuid.uuid4().hex[:8]}"
        manager = self._research_manager(profiles)
        job, created = manager.create(
            run_key=run_key, ticker=ticker, bucket=bucket, trade_date=date,
            instrument_type=instrument_type, trigger=trigger,
        )
        manager.dispatch()
        current = manager.store.get(job["run_key"]) or job
        return {**current, "deduplicated": not created}

    def research_run(self, run_key: str) -> dict[str, Any]:
        profiles = self.profiles()
        if not re.fullmatch(r"[A-Za-z0-9._-]+", run_key):
            raise ServiceError(f"invalid research run {run_key!r}")
        job = self._research_manager(profiles).store.get(run_key)
        if job is None:
            raise ServiceError(f"no research run {run_key!r}")
        text = ""
        log_path = Path(job["log"]).resolve()
        runs_root = (profiles.project_dir / "runs").resolve()
        if runs_root not in log_path.parents:
            raise ServiceError("research log path is outside the project run directory")
        try:
            text = log_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            pass
        except OSError as exc:
            text = f"log unavailable: {exc}"
        return {**job, "log_tail": text[-4000:]}

    def research_jobs(self, limit: int = 50) -> list[dict[str, Any]]:
        profiles = self.profiles()
        return self._research_manager(profiles).store.list(limit=limit)

    def recover_research_jobs(self) -> dict[str, Any]:
        profiles = self.profiles()
        result = self._research_manager(profiles).tick()
        queued = len(self._research_manager(profiles).store.list(states=("QUEUED", "RUNNING")))
        self.record_activity("research_dispatch", f"{queued} job(s) active", profiles=profiles)
        return result

    def cancel_research(self, run_key: str, reason: str = "cancelled from the console") -> dict[str, Any]:
        """Stop a queued or running research job on request.

        A run that is going nowhere otherwise holds a concurrency slot and a share of the daily
        budget until its timeout, which on a long analysis is most of an afternoon.
        """
        if not re.fullmatch(r"[A-Za-z0-9._-]+", run_key):
            raise ServiceError(f"invalid research run {run_key!r}")
        manager = self._research_manager(self.profiles())
        job = manager.store.get(run_key)
        if job is None:
            raise ServiceError(f"no research run {run_key!r}")
        if job["state"] not in ("QUEUED", "RUNNING"):
            raise ServiceConflict(f"research run {run_key} already finished as {job['state']}")
        cancelled = manager.cancel(run_key, reason)
        if cancelled is None:
            raise ServiceConflict("research run changed state while it was being cancelled")
        return cancelled

    # ---------------------------------------------------------------- operations
    def record_activity(self, activity: str, detail: str = "", *,
                        profiles: Profiles | None = None,
                        min_interval_seconds: float = 60.0) -> None:
        """Note that a background activity just succeeded."""
        from .. import operations as ops

        try:
            ops.record(self.state_db(profiles or self.profiles()), activity, detail,
                       min_interval_seconds=min_interval_seconds)
        except Exception:
            logger.debug("could not record the %s heartbeat", activity, exc_info=True)

    def operations(self) -> dict[str, Any]:
        """Whether the unattended parts of the system are still running, and what they left."""
        from .. import operations as ops

        profiles = self.profiles()
        cfg = profiles.risk.get("operations") or {}
        expected = dict(cfg.get("expected_every_minutes") or {})
        record = ops.heartbeats(self.state_db(profiles))
        now = utcnow()

        activities = []
        for name in ops.ACTIVITIES:
            entry = record.get(name) or {}
            at = entry.get("at")
            age = None
            if at:
                try:
                    age = round((now - datetime.fromisoformat(at)).total_seconds() / 60, 1)
                except ValueError:
                    at = None
            window = float(expected.get(name, 1440))
            activities.append({
                "id": name, "last_success_at": at, "age_minutes": age,
                "expected_every_minutes": window,
                # Never having run is overdue too: an install whose backup job was never loaded
                # looks identical to a healthy one until the day it is needed.
                "overdue": age is None or age > window * 2,
                "detail": entry.get("detail") or "",
            })

        return {
            "checked_at": now.isoformat(),
            "activities": activities,
            "disk": ops.disk(profiles.project_dir, float(cfg.get("low_disk_free_gb", 10))),
            "retention": {"runs_days": int(cfg.get("runs_retention_days", 120)),
                          "logs_mb": int(cfg.get("log_max_mb", 32)),
                          "backups_days": int(cfg.get("backup_retention_days", 30))},
        }

    # ---------------------------------------------------------------- notifications
    def collect_notifications(self) -> list[Any]:
        """Everything a person would want to be told, read from current state.

        Pulled rather than emitted at each site. A push would lose whatever happened while the
        process was down, and this system's whole point is that it runs unattended; reading the
        world each pass means a restart re-notices a waiting proposal instead of forgetting it.
        """
        from ..notify import Event

        profiles = self.profiles()
        events: list[Event] = []

        for proposal in self.proposals():
            if proposal["status"] == ProposalStatus.WAITING_FOR_APPROVAL:
                events.append(Event(
                    key=f"proposal_waiting:{proposal['proposal_id']}:{proposal['version']}",
                    kind="proposal_waiting",
                    title=f"{proposal['action']} {proposal['ticker']} needs your approval",
                    detail=(f"${proposal['notional_usd']:,.2f} in {proposal['bucket']}"
                            + (" (real money)" if proposal["is_real_money"] else "")),
                    link="/ui/approvals.html", urgent=proposal["is_real_money"]))
            if proposal.get("reconciliation_error"):
                events.append(Event(
                    key=f"reconciliation_error:{proposal['proposal_id']}",
                    kind="reconciliation_error",
                    title=f"{proposal['ticker']} order could not be reconciled",
                    detail=str(proposal["reconciliation_error"])[:300],
                    link="/ui/activity.html", urgent=True))

        for job in self.research_jobs(limit=50):
            if job["state"] in ("FAILED", "TIMED_OUT"):
                events.append(Event(
                    key=f"research_failed:{job['run_key']}", kind="research_failed",
                    title=f"research on {job['ticker']} did not finish",
                    detail=str(job.get("error") or job["state"])[:300], link="/ui/research.html"))
            elif job["state"] == "CLAUDE_LIMIT_REACHED":
                events.append(Event(
                    key=f"research_limit:{job['run_key']}:{job.get('limit_recoveries', 0)}",
                    kind="research_limit",
                    title="the Claude subscription limit stopped research",
                    detail=str(job.get("error") or "")[:300], link="/ui/research.html"))

        for fill in self.orders(limit=10)["fills"]:
            fill_id = fill.get("fill_id") or f"{fill.get('proposal_id')}:{fill.get('at')}"
            events.append(Event(
                key=f"fill_recorded:{fill_id}", kind="fill_recorded",
                title=f"{fill.get('symbol', 'an order')} filled",
                detail=f"{fill.get('quantity')} @ {fill.get('price')}", link="/ui/activity.html"))

        if self._micro_enabled(profiles):
            micro = self.micro_live()
            if micro.get("connect_error"):
                events.append(Event(
                    key="credential_invalid:robinhood", kind="credential_invalid",
                    title="the broker connection needs attention",
                    # The error text can name an endpoint but never a token.
                    detail=str(micro["connect_error"])[:200], link="/ui/system.html", urgent=True))

        for name, ladder in self.risk_state().items():
            if not isinstance(ladder, dict) or "level" not in ladder:
                continue
            if ladder["level"] != "NORMAL":
                events.append(Event(
                    key=f"risk_level_changed:{name}:{ladder['level']}", kind="risk_level_changed",
                    title=f"the {name} drawdown ladder is at {ladder['level']}",
                    detail=f"drawdown {ladder.get('drawdown_pct', 0):.2f}% from the high-water mark",
                    link="/ui/risk.html", urgent=ladder.get("manual_reset_required", False)))

        for activity in self.operations()["activities"]:
            if activity["overdue"] and activity["id"] in ("backup", "maintenance"):
                events.append(Event(
                    key=f"backup_overdue:{activity['id']}:{utcnow().date()}",
                    kind="backup_overdue",
                    title=f"{activity['id'].replace('_', ' ')} has not run when expected",
                    detail=f"last success {activity['last_success_at'] or 'never'}",
                    link="/ui/system.html", urgent=activity["id"] == "backup"))
        return events

    def notification_settings(self, profiles: Profiles | None = None):
        """Notification settings with the state database bound.

        The webpush channel answers "is this configured" by looking for a subscribed device, so
        every caller has to hand it the same database or they disagree about whether anything
        would be sent.
        """
        from ..notify import Settings

        profiles = profiles or self.profiles()
        settings = Settings.load(profiles)
        settings._db = self.state_db(profiles)
        return settings

    def dispatch_notifications(self) -> dict[str, Any]:
        """Collect and deliver. A no-op that records nothing sent while no channel is set."""
        from ..notify import dispatch

        profiles = self.profiles()
        settings = self.notification_settings(profiles)
        return dispatch(settings._db, settings, self.collect_notifications())

    def send_heartbeat(self) -> dict[str, Any]:
        """Ping an external watcher, if one is configured. Never raises into the caller."""
        from ..notify import DeliveryError, Settings, heartbeat

        try:
            settings = Settings.load(self.profiles())
            return {**heartbeat(settings), "every_minutes": settings.heartbeat_every_minutes}
        except (DeliveryError, ServiceError) as exc:
            return {"sent": False, "reason": str(exc), "every_minutes": 15}

    # ---------------------------------------------------------------- push devices
    def push_key(self) -> dict[str, Any]:
        """The public half of this deployment's VAPID identity, for the browser to subscribe."""
        from .. import webpush

        profiles = self.profiles()
        return {"public_key": webpush.public_key(profiles.project_dir / "state")}

    def push_subscribe(self, subscription: dict[str, Any], label: str = "") -> dict[str, Any]:
        from .. import webpush

        profiles = self.profiles()
        try:
            return webpush.subscribe(self.state_db(profiles), subscription, label=label)
        except webpush.PushError as exc:
            raise ServiceError(str(exc)) from exc

    def push_unsubscribe(self, device_id: str) -> dict[str, Any]:
        from .. import webpush

        removed = webpush.unsubscribe(self.state_db(self.profiles()), device_id)
        if not removed:
            raise ServiceError(f"no subscribed device {device_id!r}")
        return {"unsubscribed": True, "device_id": device_id}

    def push_devices(self) -> dict[str, Any]:
        from .. import webpush

        return {"devices": webpush.devices(self.state_db(self.profiles()))}

    def push_test(self) -> dict[str, Any]:
        """Send one notification on request, so a person can prove the path before relying on it."""
        from .. import webpush

        profiles = self.profiles()
        try:
            return webpush.deliver(self.state_db(profiles), profiles.project_dir / "state", {
                "title": "Conservative Trading",
                "detail": "Notifications reach this device.",
                "link": "index.html", "key": "test", "urgent": False})
        except webpush.PushError as exc:
            raise ServiceError(str(exc)) from exc

    def notification_status(self) -> dict[str, Any]:
        from ..notify import EVENTS, pending

        profiles = self.profiles()
        settings = self.notification_settings(profiles)
        return {
            "channel": settings.channel,
            "configured": settings.configured(),
            "detail": "sending is enabled" if settings.configured() else settings.why_not_configured(),
            "events_enabled": list(settings.events),
            "events_known": EVENTS,
            "min_interval_seconds": settings.min_interval_seconds,
            "heartbeat_configured": bool(os.environ.get(settings.heartbeat_url_env, "").strip()),
            **pending(self.state_db(profiles)),
        }

    # ---------------------------------------------------------------- activity
    def activity(self, *, kinds: tuple[str, ...] = (), subject: str | None = None,
                 cursor: int | None = None, limit: int = 50) -> dict[str, Any]:
        """One page of the audit journal, newest first.

        The console used to build a timeline from the research list plus the history of
        whichever proposal happened to be open, which could show one proposal's story and never
        the system's. This is the whole record, in one order, and it survives a refresh, a
        different device and a proposal leaving the queue.
        """
        from .. import events

        profiles = self.profiles()
        db = self.state_db(profiles)
        # Proposals that closed before the journal existed still carry their own history.
        events.backfill(db, self._store(profiles).all())
        return events.query(db, kinds=kinds, subject=subject, cursor=cursor, limit=limit)

    # ---------------------------------------------------------------- evaluation
    def evaluation_report(self) -> dict[str, Any]:
        """The forward record, assembled from the sources that already hold it.

        Nothing is duplicated into a separate ledger of its own: the decisions, jobs, proposals
        and fills are read where they live, so the report cannot drift away from what happened.
        """
        from .. import evaluation

        profiles = self.profiles()
        return evaluation.report(
            self.state_db(profiles), profiles,
            research=self.research_history(limit=500),
            jobs=self.research_jobs(limit=500),
            proposals=self.proposals(include_closed=True),
            fills=self.orders(limit=200)["fills"])

    def evaluation_snapshot(self, on: str | None = None) -> dict[str, Any]:
        """Mark the study to market for one session. Called daily by the maintenance job."""
        from .. import evaluation

        profiles = self.profiles()
        portfolio = self.portfolio()
        account = (portfolio.get("accounts") or {}).get("conceptual")
        if account is None:
            raise ServiceError("the conceptual account could not be read")
        return evaluation.snapshot(
            self.state_db(profiles), profiles, on=on or portfolio["as_of"],
            equity_usd=account["equity_usd"], net_deposits_usd=account["net_deposits_usd"])

    # ---------------------------------------------------------------- schedule
    def schedule_status(self) -> dict[str, Any]:
        """What the schedule will do next, what it last did, and why it last did nothing."""
        from .. import schedule as scheduling

        profiles = self.profiles()
        cfg = scheduling.settings(profiles)
        db = self.state_db(profiles)
        record = db.read_meta(scheduling.EXECUTIONS, {}) or {}
        jobs = self.research_jobs(limit=200)
        health = self.health()
        decision = scheduling.decide(
            profiles, health_status=health["status"], paused=bool(health.get("paused")),
            executed=record, jobs=jobs)
        periods = record.get("periods") or {}
        last_key = max(periods) if periods else None
        used = scheduling.analyses_today(jobs, cfg)
        return {
            "enabled": bool(cfg.get("enabled", True)),
            "period": cfg.get("period"),
            "calendar": cfg.get("calendar"),
            "market_timezone": cfg.get("market_timezone"),
            "on_non_session_day": cfg.get("on_non_session_day"),
            "run_while_paused": bool(cfg.get("run_while_paused", True)),
            "tickers": scheduling.default_tickers(profiles, cfg),
            "next_run_at": decision.next_run_at,
            "current_period": decision.period_key,
            "would_run_now": decision.run,
            "reason": decision.reason,
            "analyses_today": used,
            "daily_budget": int(cfg.get("max_analyses_per_day", 6)),
            "last_period": last_key,
            "last_run": periods.get(last_key) if last_key else None,
            "last_skip": record.get("last_skip"),
        }

    # ---------------------------------------------------------------- trace

    #: Report-tree stage and file names, mapped to something a person reads.
    _TRACE_GROUPS: ClassVar = {
        "1_analysts": "Analysts",
        "2_research": "Research debate",
        "3_trading": "Trader",
        "4_risk": "Risk debate",
        "5_portfolio": "Portfolio manager",
    }
    _TRACE_STEPS: ClassVar = {
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
                # Evidence quality travels with the decision: a run that concluded without the
                # macro context is not the same kind of result as one that had it.
                "data_quality": (policy.get("proposal") or {}).get("data_quality"),
                "missing_information": len((policy.get("proposal") or {}).get("missing_information") or []),
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
        fills = StateDB(fills_path).fills(limit)
        if not fills and fills_path.exists() and not StateDB(fills_path).migrated(fills_path):
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
        tracked = [{"proposal_id": p.proposal_id, "order_id": p.broker_order_id,
                    "client_order_id": p.client_order_id, "symbol": p.ticker,
                    "status": proposal_status(p, profiles), "venue_status": p.order_status,
                    "bucket": p.bucket, "side": "buy" if p.action in ("BUY", "ADD") else "sell",
                    "filled_quantity": p.filled_quantity,
                    "filled_avg_price": p.filled_avg_price, "reconciled_at": p.reconciled_at,
                    "reconciliation_error": p.reconciliation_error}
                   for p in self._store(profiles).all() if p.client_order_id]
        return {"fills": list(reversed(fills)), "open_orders": live, "tracked_orders": tracked, "error": error}

    # ---------------------------------------------------------------- controls
    def _journal(self, profiles: Profiles, **fields: Any) -> None:
        """Append to the audit journal. A failure here must not block the action it describes."""
        from .. import events

        try:
            events.emit(self.state_db(profiles), **fields)
        except Exception:
            logger.exception("could not journal %s", fields.get("kind"))

    def pause(self, reason: str = "paused through the application interface",
              identity: str | None = None) -> dict[str, Any]:
        """Engage the kill switch. Research continues; no order can be submitted."""
        profiles = self.profiles()
        halt = profiles.project_dir / str(
            profiles.risk_cfg("execution_policy", "kill_switch", "halt_file", default="state/HALT"))
        halt.parent.mkdir(parents=True, exist_ok=True)
        halt.write_text(f"{utcnow().isoformat()} {reason}\n", encoding="utf-8")
        # The halt file is deleted on resume, so without this the pause leaves no trace at all.
        self._journal(profiles, kind="system_paused", actor=identity or "console",
                      detail={"reason": reason[:200]})
        return {"paused": True, "reason": reason, "status": SystemStatus.PAUSED}

    def resume(self, identity: str | None = None) -> dict[str, Any]:
        profiles = self.profiles()
        halt = profiles.project_dir / str(
            profiles.risk_cfg("execution_policy", "kill_switch", "halt_file", default="state/HALT"))
        halt.unlink(missing_ok=True)
        self._journal(profiles, kind="system_resumed", actor=identity or "console")
        return {"paused": False, "status": SystemStatus.READY}


# ---------------------------------------------------------------------- helpers
def _close(broker: Any) -> None:
    if hasattr(broker, "close"):
        try:
            broker.close()
        except Exception as exc:  # noqa: BLE001
            logger.debug("error closing broker session: %s", exc)


def _fred_configured(profiles: Profiles) -> bool:
    """A real key, not an empty placeholder line copied from .env.example.

    This used to read the environment and then fall back to parsing .env itself, which meant the
    health page could answer from the file while the process doing the fetching had nothing —
    the warning would go quiet without the data arriving. The file is now loaded into the
    environment at every entry point (:mod:`conservative_trading.env`), so there is one answer
    and it is the one the fetcher sees.
    """
    import os

    if os.environ.get("FRED_API_KEY", "").strip():
        return True
    return bool(load_env(profiles.project_dir) and os.environ.get("FRED_API_KEY", "").strip())


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
        ProposalStatus.EXECUTED: "the order is filled and the ledger is synchronized",
        ProposalStatus.SUBMITTED: "the order is submitted; execution is not yet complete",
        ProposalStatus.PARTIALLY_FILLED: "the order is partially filled; synchronization continues",
        ProposalStatus.UNKNOWN: "the order state or ledger synchronization is unresolved; do not resubmit",
        ProposalStatus.CANCELED: "the order was canceled or expired; any partial fills remain recorded",
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
