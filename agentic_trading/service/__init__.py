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
import time
import uuid
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from datetime import date as calendar_date
from pathlib import Path
from typing import Any, ClassVar, TypeVar

from .. import agent_roster, autonomy
from ..broker import make_broker
from ..broker.read_session import ReadSession, SessionUnavailable
from ..env import load_env
from ..execution import ExecutionResult, needs_reconciliation, reconcile_proposal, revalidate
from ..execution import execute as execute_proposal
from ..micro_live import BUCKET as MICRO_BUCKET
from ..micro_live import VenueReading, preflight_from, read_venue
from ..portfolio import PortfolioError, load_portfolio
from ..profile import ProfileError, Profiles, load_profiles
from ..proposals import (
    APPROVED,
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
from .kept import Kept
from .shapes import (
    AdvisorConversation,
    AdvisorDecision,
    AdvisorJob,
    AdvisorMessage,
    AdvisorSnapshot,
    AdvisorState,
    ClaudeStatus,
    Fill,
    HardRiskRule,
    HardRiskView,
    Health,
    MarketStatus,
    MicroLive,
    MicroLiveAccount,
    Orders,
    PersonalNotes,
    Portfolio,
    PortfolioAccount,
    PortfolioBucket,
    PortfolioCombined,
    PortfolioPosition,
    PreflightCheck,
    ProposalDetail,
    ProposalFreshness,
    ProposalResearch,
    ProposalSummary,
    ProposalWorld,
    TrackedOrder,
)
from .status import (
    FRESHNESS_LABELS,
    ProposalStatus,
    SystemStatus,
    micro_live_status,
    proposal_status,
)

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

#: How much of runs/llm_calls.jsonl /health reads: several days of calls at the daily budget.
AUDIT_TAIL_BYTES = 2_000_000

#: The drawdown ladders `risk_state` reports, as opposed to the other entries beside them.
LADDER_NAMES = ("total", "core", "sandbox", "micro_live")

BUCKETS = ("core", "sandbox", MICRO_BUCKET)


class ServiceError(RuntimeError):
    """A product-level failure with a message meant for a person."""


class ServiceConflict(ServiceError):
    """A stale version or conflicting retry; fetch current state before acting."""


class Service:
    """One instance per process. Cheap to construct.

    Between calls it keeps at most one venue session, and only for reads that record nothing
    when the venue fails them (see :meth:`_read_venue`). An order, and the reconciliation of
    one, opens a session of its own every time.

    It also keeps the results of the three reads a console polls that are costly to repeat --
    the market regime, the venue's account, the priced portfolio -- each with the inputs it was
    computed from (see kept.py). The readings compute through methods looked up when they run.
    """

    def __init__(self, config_dir: str | Path = "config"):
        self.config_dir = Path(config_dir)
        self._venue_reads = ReadSession()
        # Daily bars: renewed every quarter hour, and a failed assessment every two minutes.
        self._regime_reading = Kept(
            lambda: self._read_regime(), key=lambda: self._regime_inputs(),
            refresh_after=lambda reading: 900.0 if reading[0].available else 120.0,
            serve_for=lambda reading: 1800.0 if reading[0].available else 300.0)
        # The account moves with a trade or a transfer; a minute old while someone is looking.
        self._venue_reading = Kept(
            lambda: self._read_venue_account(), key=lambda: self._venue_inputs(),
            refresh_after=60.0, serve_for=120.0, watch_for=300.0)
        # Recomputed at once when a ledger is written; otherwise prices move once a session.
        self._portfolio_reading = Kept(
            lambda: self.portfolio(), key=lambda: self._portfolio_inputs(),
            refresh_after=240.0, serve_for=480.0, watch_for=600.0)

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

    def _require_micro_enabled(self, profiles: Profiles) -> None:
        if not self._micro_enabled(profiles):
            raise ServiceError("real-money execution is disabled (micro_live_execution.enabled is false)")

    def _broker(self, profiles: Profiles, bucket: str):
        """The venue that owns a bucket. Real money only ever comes from the Micro-Live path."""
        if bucket == MICRO_BUCKET:
            from ..broker.robinhood import RobinhoodBroker

            self._require_micro_enabled(profiles)
            return RobinhoodBroker(profiles)
        return make_broker(profiles)

    def _read_venue(self, profiles: Profiles, read: Callable[[Any], _T]) -> _T:
        """Run a read against the real-money venue on the session kept for reads.

        For reads that record nothing when the venue fails them, and no others. A read that
        fails on a kept session is run again on a fresh one, so whatever it recorded the first
        time would be recorded twice, and a failure it recorded would describe a session the
        venue had ended rather than the venue. Reconciliation marks the order it could not read,
        which is why it opens its own session. Raises SessionUnavailable when no session can be
        opened.
        """
        from ..broker import robinhood

        return self._venue_reads.read(robinhood.session_key(profiles),
                                      lambda: robinhood.RobinhoodBroker(profiles), read)

    # ---------------------------------------------------------------- kept readings
    def refresh_kept_readings(self) -> dict[str, bool]:
        """Renew whichever kept readings are due; the regime first, since the portfolio uses it.

        Called by the API's refresh loop, off the request path. A reading that fails is logged
        and the others still run; it keeps its last result, which requests judge by its age.
        """
        refreshed: dict[str, bool] = {}
        for name, reading in (("regime", self._regime_reading), ("venue", self._venue_reading),
                              ("portfolio", self._portfolio_reading)):
            try:
                refreshed[name] = reading.refresh()
            except Exception:
                logger.exception("renewing the kept %s reading failed", name)
                refreshed[name] = False
        return refreshed

    def kept_portfolio(self) -> Portfolio:
        """The portfolio as the console reads it, kept between polls (see kept.py).

        Computed again as soon as a ledger is written, the configuration changes or the session
        rolls over, and otherwise renewed every few minutes while it is being read. Use
        :meth:`portfolio` where the books must be read and priced now.
        """
        return self._portfolio_reading.get()

    def _regime_inputs(self) -> object:
        profiles = self.profiles()
        return _last_session(), json.dumps(profiles.risk.get("market_regime") or {},
                                           sort_keys=True, default=str)

    def _read_regime(self) -> tuple[Any, str]:
        from ..risk import assess_regime
        from ..risk.regime import RegimeResult

        profiles = self.profiles()
        checked_at = utcnow().isoformat()
        try:
            return assess_regime(profiles, _last_session()), checked_at
        except Exception as exc:  # noqa: BLE001 - unknown, never guessed
            return RegimeResult("UNKNOWN", False, str(exc)), checked_at

    def _venue_inputs(self) -> object:
        from ..broker import robinhood

        return robinhood.session_key(self.profiles())

    def _read_venue_account(self) -> VenueReading:
        profiles = self.profiles()
        checked_at = utcnow().isoformat()
        try:
            return self._read_venue(profiles, lambda broker: read_venue(broker, checked_at))
        except SessionUnavailable as exc:  # not connected is a normal state, not a crash
            return VenueReading(checked_at=checked_at, connected=False, connect_error=str(exc))

    def _portfolio_inputs(self) -> object:
        """Everything the priced portfolio is computed from that can change within a session.

        A ledger lives in the state database, where every write moves its revision; one not
        yet migrated is still a file. The drawdown ladders are not here: computing the portfolio
        writes them, and they follow from the ledgers and prices that are.
        """
        profiles = self.profiles()
        ps = profiles.risk.get("portfolio_state") or {}
        micro = profiles.risk.get("micro_live_execution") or {}
        ledgers = []
        for value in (ps.get("file", "state/portfolio.yaml"),
                      micro.get("ledger_file", "state/micro_live_portfolio.yaml")):
            path = profiles.project_dir / str(value)
            ledgers.append((str(value), StateDB(path).document_revision(path), _file_stamp(path)))
        return _last_session(), _directory_stamp(profiles.config_dir), tuple(ledgers)

    def release_idle_venue_session(self) -> bool:
        """Close the kept venue session if nobody has read through it lately."""
        return self._venue_reads.release_idle()

    def close(self) -> None:
        """Close the kept venue session. The API does this as it stops."""
        self._venue_reads.close()

    # ---------------------------------------------------------------- health
    def health(self) -> Health:
        """One call a dashboard can poll: can the system act, and what is blocking it."""
        warnings: list[str] = []
        try:
            profiles = self.profiles()
        except ServiceError as exc:
            # Nothing below this line can be answered without a config, so the shape is the
            # status, the reason and the time — and every other field is absent, not zeroed.
            return Health(status=SystemStatus.CONFIG_ERROR, detail=str(exc),
                          checked_at=utcnow().isoformat())

        halt_file = profiles.project_dir / str(
            profiles.risk_cfg("execution_policy", "kill_switch", "halt_file", default="state/HALT"))
        paused = halt_file.exists() or bool(
            profiles.risk_cfg("execution_policy", "kill_switch", "trading_halted", default=False))

        risk = self.risk_state()
        halted = risk.get("total", {}).get("manual_reset_required", False)

        claude = self.claude_status()
        # A halt waits for a person; a subscription limit clears on its own at a stated time.
        # Ranked the other way, a drawdown halt was reported as "limit reached" for as long as
        # both held. Either one blocks scheduled research, and `claude` still carries the limit.
        if halted:
            status = SystemStatus.HALTED
        elif claude.state == "LIMIT_REACHED":
            status = SystemStatus.CLAUDE_LIMIT_REACHED
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
        return Health(
            status=status,
            detail={
                SystemStatus.READY: "able to research and to accept approvals",
                SystemStatus.PAUSED: "kill switch engaged: no orders will be submitted",
                SystemStatus.HALTED: "a drawdown ladder halt is active and needs a manual reset",
                SystemStatus.CLAUDE_LIMIT_REACHED: claude.detail,
                SystemStatus.CONFIG_ERROR: "configuration error",
            }[status],
            paused=paused,
            stage=profiles.stage,
            market=market,
            claude=claude,
            micro_live_enabled=self._micro_enabled(profiles),
            warnings=warnings,
            checked_at=utcnow().isoformat(),
        )

    def claude_status(self) -> ClaudeStatus:
        """Subscription state, read from the most recent runs. Never exposes credentials."""
        profiles = self.profiles()
        audit = profiles.project_dir / "runs" / "llm_calls.jsonl"
        last_call, calls_today = None, 0
        today = datetime.now(UTC).date()
        # The audit is only ever appended to and nothing rotates it, and /health reads this on
        # every poll: read its end, not all of it. Timestamps carry the Mac's local offset, so a
        # call is counted by its UTC date; comparing the local string against the UTC date
        # dropped every call made in the hours the two dates differ. One unreadable line used to
        # end the count at that line.
        for line in _file_tail(audit, AUDIT_TAIL_BYTES):
            try:
                ts = str(json.loads(line).get("ts") or "")
                when = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S%z")
            except (ValueError, TypeError, AttributeError):
                continue
            last_call = ts
            if when.astimezone(UTC).date() == today:
                calls_today += 1
        state, detail = "OK", "subscription-backed Claude Code runtime"
        jobs = self._research_manager(profiles).store.list(limit=1)
        if jobs and jobs[0].get("state") == "CLAUDE_LIMIT_REACHED":
            state = "LIMIT_REACHED"
            detail = jobs[0].get("error") or "Claude subscription limit reached"
        return ClaudeStatus(state=state, detail=detail,
                            billing="claude_subscription_no_api_charges",
                            calls_today=calls_today, last_call_at=last_call)

    def market_status(self) -> MarketStatus:
        try:
            import pandas as pd

            from ..broker.robinhood import _nyse_calendar

            cal = _nyse_calendar()
            now = pd.Timestamp.now(tz="UTC")
            is_open = bool(cal.is_open_on_minute(now, ignore_breaks=True))
            return MarketStatus(is_open=is_open, calendar=cal.name,
                                next_open=str(cal.next_open(now)) if not is_open else None,
                                next_close=str(cal.next_close(now)) if is_open else None)
        except Exception as exc:  # noqa: BLE001 - an unknown session is reported closed
            return MarketStatus(is_open=False, error=f"{type(exc).__name__}: {exc}")

    # ---------------------------------------------------------------- portfolio
    #: Which ledger owns which buckets. Two separate books: the conceptual core/sandbox
    #: portfolio, and the real-money Micro-Live account at the venue. They never share a cash
    #: reconciliation, a high-water mark or a total.
    ACCOUNTS: ClassVar = (
        ("conceptual", "core", ("core", "sandbox"), False, None, "total"),
        (MICRO_BUCKET, MICRO_BUCKET, (MICRO_BUCKET,), True, "robinhood", MICRO_BUCKET),
    )

    def portfolio(self) -> Portfolio:
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
        checked_at = utcnow().isoformat()
        # Only the snapshot's text uses the regime, and this discards the text.
        regime, _ = self._regime_reading.get()
        buckets_out: dict[str, PortfolioBucket] = {}
        accounts_out: dict[str, PortfolioAccount] = {}
        errors: dict[str, str] = {}
        for account, sample, buckets, real, venue, ladder_key in self.ACCOUNTS:
            try:
                snap, ladders, _regime, _text = portfolio_context(profiles, trade_date, sample,
                                                                  regime=regime)
            except (PortfolioError, ServiceError, ProfileError) as exc:
                # One unreadable book must not take the other down with it: the reason is
                # reported beside whatever was readable, and `combined.complete` says so.
                errors[account] = str(exc)
                continue
            views = {name: view for name, view in snap.buckets.items() if name in buckets}
            if not views:
                continue
            for name, view in views.items():
                buckets_out[name] = PortfolioBucket(
                    account=account,
                    allocation_usd=view.allocation_usd,
                    cash_usd=round(view.cash_usd, 2),
                    positions_value_usd=round(view.market_value, 2),
                    equity_usd=round(view.equity, 2),
                    position_count=view.n_positions,
                    is_real_money=real,
                    venue=venue,
                    positions=[PortfolioPosition(
                        symbol=p.symbol, quantity=p.quantity, price=p.price,
                        market_value_usd=round(p.market_value, 2),
                        cost_basis_usd=round(p.cost_basis, 2),
                        unrealized_pct=round(p.unrealized_pct, 2),
                        weight_pct_of_allocation=round(p.weight_pct_of_bucket, 2),
                        classification=p.cls.limit_class,
                        exposure_group=p.cls.exposure_group,
                    ) for p in view.positions],
                    sleeves_pct={k: round(v, 2) for k, v in view.sleeves_pct.items()},
                )
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
            accounts_out[account] = PortfolioAccount(
                buckets=sorted(views),
                is_real_money=real,
                venue=venue,
                source="robinhood account, reconciled" if real else "local conceptual ledger",
                as_of=snap.as_of,
                priced_for=snap.trade_date,
                equity_usd=equity,
                cash_usd=round(sum(v.cash_usd for v in views.values()), 2),
                positions_value_usd=round(sum(v.market_value for v in views.values()), 2),
                net_deposits_usd=deposits,
                opening_allocation_usd=opening,
                recorded_flows_usd=flows,
                net_deposits_basis="configured allocation plus recorded deposits and withdrawals",
                investment_pnl_usd=round(equity - deposits, 2),
                return_pct=round((equity - deposits) / deposits * 100, 2) if deposits else None,
                high_water_mark_usd=round(ladder.high_water_mark_usd, 2) if ladder else None,
                drawdown_pct=round(ladder.drawdown_pct, 2) if ladder else None,
                ladder_level=ladder.level if ladder else None,
            )
        return Portfolio(
            as_of=trade_date,
            buckets=buckets_out,
            accounts=accounts_out,
            errors=errors,
            combined=PortfolioCombined(
                equity_usd=round(sum(a.equity_usd for a in accounts_out.values()), 2),
                accounts=sorted(accounts_out),
                complete=not errors,
                # True whenever the sum spans both books, which is precisely when it must not be
                # compared against any single account's high-water mark.
                mixes_real_and_conceptual=len({a.is_real_money for a in accounts_out.values()}) > 1,
            ),
            checked_at=checked_at,
        )

    def ledger_check(self) -> dict[str, Any]:
        """The last ledger check's report, whoever ran it: the daily job, the CLI or the console.

        Read from the state database, never by opening the venue: the Portfolio screen shows
        it on every visit, and a refusal's figures are what its two recording buttons offer.
        """
        from ..ledger_confirm import REPORT

        return self.state_db().read_meta(REPORT, {}) or {}

    def confirm_portfolio(self, *, ledger: str = "all", dry_run: bool = False) -> dict[str, Any]:
        """Renew the ledgers' as_of, but only where a check agrees with them.

        The same call `ct-state confirm` makes. A console that could stamp a date the CLI
        would have refused would be a second opinion about what confirming means, and the
        weaker of the two would be the one reachable from a phone.
        """
        from ..ledger_confirm import confirm_ledgers

        profiles = self.profiles()
        return confirm_ledgers(profiles, self._store(profiles), ledger=ledger, dry_run=dry_run)

    def risk_state(self) -> dict[str, Any]:
        """Drawdown ladders and the market regime, in display form."""
        from ..risk import load_ladder_states

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
        # Kept between polls: the notification pass reads this every fifteen seconds and three
        # screens poll it, and assessing the regime asks the data vendor (see kept.py).
        regime, checked_at = self._regime_reading.get()
        out["regime"] = {**regime.as_dict(), "checked_at": checked_at}
        return out

    # ---------------------------------------------------------------- micro-live
    def micro_live(self) -> MicroLive:
        """Real-money workflow status, the preflight gates, and the capital boundary.

        The venue's answer is a kept reading, at most a couple of minutes old while the status
        is being read; the configuration gates and the open proposal are read on every call.
        Execution never relies on this: it runs preflight again in the session it orders in.
        """
        profiles = self.profiles()
        micro = profiles.risk.get("micro_live_execution") or {}
        enabled = self._micro_enabled(profiles)
        reading = self._venue_reading.get()
        preflight = preflight_from(profiles, reading)
        open_proposal = next((p for p in self._store(profiles).open_proposals(profiles)
                              if p.bucket == MICRO_BUCKET), None)
        status = micro_live_status(enabled=enabled, connected=reading.connected,
                                   preflight_ready=preflight.ready, open_proposal=open_proposal,
                                   profiles=profiles)
        return MicroLive(
            status=status,
            enabled=enabled,
            connected=reading.connected,
            connect_error=reading.connect_error,
            capital_boundary_usd=float(micro.get("max_capital_usd", 100)),
            account=MicroLiveAccount(**preflight.account) if preflight.account else None,
            preflight_ready=preflight.ready,
            checks=[PreflightCheck(id=c.check_id, name=c.name, passed=c.passed, detail=c.detail)
                    for c in preflight.checks],
            blocking=[c.check_id for c in preflight.failures],
            permissions=dict(micro.get("permissions") or {}),
            execution_mode=str(micro.get("execution_mode", "human_approval")),
            checked_at=reading.checked_at,
        )

    # ---------------------------------------------------------------- proposals
    def proposals(self, include_closed: bool = False) -> list[ProposalSummary]:
        profiles = self.profiles()
        store = self._store(profiles)
        items = store.all() if include_closed else store.open_proposals(profiles)
        return [self._proposal_summary(p, profiles) for p in sorted(items, key=lambda x: x.created_at, reverse=True)]

    def _proposal_summary(self, p: Proposal, profiles: Profiles) -> ProposalSummary:
        freshness, why = p.freshness(profiles)
        rules = p.freshness_rules(profiles)
        return ProposalSummary(
            proposal_id=p.proposal_id,
            version=p.version(),
            sell_quantity=p.sell_quantity,
            order_status=p.order_status,
            reconciliation_error=p.reconciliation_error,
            status=proposal_status(p, profiles),
            ticker=p.ticker,
            bucket=p.bucket,
            is_real_money=p.bucket == MICRO_BUCKET,
            action=p.action,
            strategy=p.strategy,
            notional_usd=p.notional_usd,
            target_weight_pct=p.target_weight_pct,
            confidence=p.confidence,
            requires_human_approval=p.requires_human_approval,
            created_at=p.created_at,
            approved_at=p.approved_at,
            freshness=ProposalFreshness(
                state=freshness, label=FRESHNESS_LABELS.get(freshness, freshness),
                detail=why, freshness_class=p.freshness_class(profiles),
                ttl_minutes=rules.get("ttl_minutes"),
                age_minutes=round(p.age_minutes(), 1),
                max_price_drift_pct=rules.get("max_price_drift_pct")),
            reference_price=p.reference_price,
            run_id=p.run_id,
        )

    def proposal(self, proposal_id: str) -> ProposalDetail:
        """Full detail: the reasoning, the risk rules, and the world the proposal was born into."""
        profiles = self.profiles()
        p = self._store(profiles).get(proposal_id)
        if p is None:
            raise ServiceError(f"no proposal {proposal_id!r}")
        research, hard_risk = None, None
        record = _find_record(profiles, p.run_id)
        if record:
            policy = record.get("policy") or {}
            proposal_doc = policy.get("proposal") or {}
            research = ProposalResearch(
                portfolio_manager_rating=(record.get("upstream_decision") or {}).get("portfolio_manager_signal"),
                policy_action=policy.get("final_action"),
                policy_reasons=policy.get("reasons"),
                confidence=policy.get("confidence"),
                data_quality=proposal_doc.get("data_quality"),
                market_risk_level=proposal_doc.get("market_risk_level"),
                investment_thesis=proposal_doc.get("investment_thesis"),
                bull_case=proposal_doc.get("bull_case"),
                bear_case=proposal_doc.get("bear_case"),
                primary_risks=proposal_doc.get("primary_risks"),
                what_could_make_this_wrong=proposal_doc.get("what_could_make_this_thesis_wrong"),
                why_doing_nothing_may_be_better=proposal_doc.get("alternative_why_doing_nothing_may_be_preferable"),
                missing_information=proposal_doc.get("missing_information"),
                entry_logic=proposal_doc.get("entry_logic"),
                exit_logic=proposal_doc.get("exit_logic"),
                expected_holding_period=proposal_doc.get("expected_holding_period"),
            )
            hard_risk = _hard_risk_view(record.get("hard_risk") or {})
        return ProposalDetail(
            **self._proposal_summary(p, profiles).model_dump(),
            history=p.history,
            world_at_proposal=ProposalWorld(
                ladder_level=p.ladder_level_at_proposal, regime=p.regime_at_proposal,
                portfolio_equity_usd=p.portfolio_equity_at_proposal,
                bucket_cash_usd=p.bucket_cash_at_proposal, data_quality=p.data_quality,
                evidence=p.evidence, missing_information=p.missing_information,
            ),
            research=research,
            hard_risk=hard_risk,
        )

    # ---------------------------------------------------------------- actions
    def revalidate_proposal(self, proposal_id: str) -> dict[str, Any]:
        """Show what would happen on approval, changing nothing."""
        profiles = self.profiles()
        p = self._store(profiles).get(proposal_id)
        if p is None:
            raise ServiceError(f"no proposal {proposal_id!r}")
        if p.status == PROPOSED:
            p = replace(p, status=APPROVED, approved_at=utcnow().isoformat())
        if p.bucket == MICRO_BUCKET:
            # A preview persists no risk state and places nothing, so it shares the kept session.
            # The approval itself revalidates again, on a session opened for it.
            self._require_micro_enabled(profiles)
            rv = self._read_venue(profiles, lambda broker: revalidate(profiles, p, broker,
                                                                      persist_risk=False))
        else:
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
        checks = {"initial": initial, "identity": identity, "version": version,
                  "request_id": request_id, "terms": terms}
        # Every approval/execution for an account shares this lock. The proposal lock is only
        # held for short state transitions, and never across a broker network request.
        with file_lock(store.db.path.with_name(f"{account_key}.execution")):
            with store.locked():
                p = self._approvable(store, proposal_id, **checks)
                if p.approval_request_id == request_id and p.approval_response is not None:
                    return p.approval_response
            # The venue is reached before the approval is recorded. Opening it can fail -- an
            # expired token, the venue down, real money switched off -- and an approval saved
            # ahead of that was left APPROVED with no order and no response: a retry under a
            # fresh request id then met "status changed" for good, and the autonomous loop,
            # which only considers PROPOSED, never looked at it again.
            broker = self._broker(profiles, p.bucket)
            try:
                with store.locked():
                    # Checked again: the proposal lock was not held while the venue was opened,
                    # so a rejection or a competing approval may have landed in between.
                    p = self._approvable(store, proposal_id, **checks)
                    replay = p.approval_request_id == request_id
                    if replay and p.approval_response is not None:
                        return p.approval_response
                    if not replay:
                        p.status = APPROVED
                        p.approved_at = utcnow().isoformat()
                        p.approved_by = identity
                        p.approval_note = note
                        p.approval_request_id = request_id
                        p.approved_version = version
                        # A proposal handed back by an earlier approval still carries that
                        # approval's answer, and the answer is what gets returned below.
                        p.approval_response = None
                        p.note("approved", note or "explicit approval through the console",
                               identity=identity, version=version, request_id=request_id, terms=terms)
                        store.save(p)
                try:
                    result = execute_proposal(profiles, p, broker, store)
                except Exception as exc:  # noqa: BLE001 - an approval must always end in a recorded answer
                    result = self._execution_failed(store, proposal_id, exc)
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

    @staticmethod
    def _approvable(store: ProposalStore, proposal_id: str, *, initial: Proposal,
                    identity: str | None, version: str | None, request_id: str | None,
                    terms: dict[str, Any] | None) -> Proposal:
        """The proposal, if this request may approve it or is a replay of the approval that did.

        Call with the proposal lock held. Raises rather than returning a reason, so no caller
        can proceed past a refusal by forgetting to read it.
        """
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
        if p.approval_request_id == request_id:
            if p.approved_by != identity or p.approved_version != version:
                raise ServiceConflict("request id does not match the original approval")
        elif p.status != PROPOSED or p.version() != version:
            raise ServiceConflict("proposal version or status changed; review it again")
        return p

    @staticmethod
    def _execution_failed(store: ProposalStore, proposal_id: str, exc: Exception) -> ExecutionResult:
        """Turn an exception out of execution into an outcome the approval can record.

        Execution handles every failure after the order intent is persisted itself, so an
        exception reaching here normally means the venue was never asked for an order. That is
        read back from the stored proposal rather than assumed: an order id on record means the
        venue may hold an order, and the proposal is left for reconciliation instead.
        """
        logger.exception("execution of %s raised", proposal_id)
        detail = f"{type(exc).__name__}: {exc}"[:300]
        with store.locked():
            current = store.get(proposal_id)
            if current is not None and current.client_order_id:
                current.reconciliation_error = (current.reconciliation_error
                                                or f"execution was interrupted: {detail}; lookup required")
                current.note("execution_failed", detail)
                store.save(current)
                return ExecutionResult(False, "RECONCILIATION_REQUIRED", proposal_id, {},
                                       error=current.reconciliation_error)
            error = f"execution failed before any order was sent: {detail}"
            if current is not None:
                current.note("execution_failed", error)
                store.save(current)
        return ExecutionResult(False, "EXECUTION_FAILED", proposal_id, {}, error=error)

    def autonomous_reductions(self) -> dict[str, Any]:
        """Approve and execute the reductions the deterministic gate permits.

        Deliberately built on ``approve`` rather than beside it. An autonomous approval is a
        real approval: it takes the same account lock, writes the same immutable approval
        record under its own identity, and goes down the same execution path. A parallel route
        would have been a second place for the rules to be slightly different.

        The market calendar is consulted before anything else, and without the venue. Outside
        session hours every attempt would reach the venue only to come back STALE, and this runs
        on a fifteen-second loop.
        """
        profiles = self.profiles()
        report: dict[str, Any] = {"considered": 0, "approved": [], "declined": [], "skipped": None}
        if not autonomy.is_armed(profiles, MICRO_BUCKET):
            state = autonomy.load_downgrade(profiles)
            report["skipped"] = ("autonomy was withdrawn and needs a manual reset: "
                                 f"{state.reason}") if state.tripped else "no autonomy is switched on"
            return report
        if not self.market_status().is_open:
            report["skipped"] = "the market is closed"
            return report

        store = self._store(profiles)
        everything = store.all()
        actionable = (autonomy.AUTONOMOUS_ACTION, *autonomy.OPENING_ACTIONS)
        candidates = [p for p in everything
                      if p.status == PROPOSED and p.bucket == MICRO_BUCKET
                      and p.action in actionable and not p.requires_human_approval]
        report["considered"] = len(candidates)
        if not candidates:
            return report

        ledger = load_portfolio(profiles.project_dir / str(
            (profiles.risk.get("micro_live_execution") or {}).get(
                "ledger_file", "state/micro_live_portfolio.yaml")))
        for proposal in candidates:
            held = ledger.position(proposal.ticker, MICRO_BUCKET)
            decision = autonomy.permits(
                profiles, bucket=MICRO_BUCKET, action=proposal.action,
                confidence=proposal.confidence,
                limit_class=proposal.limit_class,
                held_quantity=held.quantity if held else None,
                requested_quantity=proposal.sell_quantity,
                reduced_today=autonomy.reduced_today(everything, MICRO_BUCKET),
                opened_today=autonomy.opened_today(everything, MICRO_BUCKET))
            if not decision.permitted:
                # Not a refusal to trade: the proposal stays in the queue for a person.
                report["declined"].append({"proposal_id": proposal.proposal_id,
                                           "reasons": decision.reasons})
                continue
            try:
                response = self.approve(
                    proposal.proposal_id, note="; ".join(decision.reasons),
                    identity=f"autonomy:{autonomy.AUTONOMOUS_ACTION.lower()}",
                    version=proposal.version(), request_id=uuid.uuid4().hex,
                    terms={"ticker": proposal.ticker, "bucket": proposal.bucket,
                           "action": proposal.action, "notional_usd": proposal.notional_usd,
                           "sell_quantity": proposal.sell_quantity})
            except ServiceConflict as exc:
                # A person reached the same proposal first, or it moved underneath us. The
                # approval protocol resolved the race; there is nothing to repair.
                report["declined"].append({"proposal_id": proposal.proposal_id,
                                           "reasons": [f"superseded: {exc}"]})
                continue
            outcome = str(response.get("outcome") or "")
            withdrawn = autonomy.trip_for_outcome(profiles, MICRO_BUCKET, outcome,
                                                  str(response.get("error") or ""))
            report["approved"].append({"proposal_id": proposal.proposal_id, "outcome": outcome,
                                       "submitted": response.get("submitted"),
                                       "autonomy_withdrawn": withdrawn})
            if withdrawn:
                break
            everything = store.all()
        return report

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

    # ---------------------------------------------------------------- research
    def advisor_decisions(self, limit: int = 40) -> list[AdvisorDecision]:
        """The same decision records as the research page, including the actual policy details."""
        profiles = self.profiles()
        records = sorted((profiles.project_dir / "runs").glob("*/decision_record.json"),
                         key=lambda p: p.stat().st_mtime, reverse=True)
        job_states = {j["run_key"]: j["state"]
                      for j in self._research_manager(profiles).store.list(limit=10000)}
        out: list[AdvisorDecision] = []
        for path in records:
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not record.get("policy"):
                continue
            if record.get("advisory") and job_states.get(record.get("run_id")) != "COMPLETE":
                continue
            out.append(AdvisorDecision(
                run_id=record.get("run_id"), ticker=record.get("ticker"),
                bucket=record.get("bucket"), account_id=record.get("account_id"),
                created_at=record.get("created_at"), trade_date=record.get("trade_date"),
                action=(record.get("final_recommendation") or {}).get("action"),
                policy=record.get("policy"), advisory=bool(record.get("advisory")),
                proposal_id=record.get("proposal_id")))
            if len(out) >= limit:
                break
        return out

    def _advisor_result(self, run_key: str) -> dict[str, Any] | None:
        if not re.fullmatch(r"[A-Za-z0-9._-]+", run_key):
            raise ServiceError("invalid run identifier")
        root = (self.profiles().project_dir / "runs").resolve()
        path = (root / run_key / "decision_record.json").resolve()
        if root not in path.parents:
            raise ServiceError("result is outside the run directory")
        try:
            return json.loads(path.read_text(encoding="utf-8")).get("assistant_result")
        except (OSError, ValueError):
            return None

    def advisor_conversation(self, conversation_id: str = "default") -> AdvisorConversation:
        jobs = self._research_manager(self.profiles()).store.list(limit=1000)
        messages: list[AdvisorMessage] = []
        for job in sorted(jobs, key=lambda j: j["created_at"]):
            task = job.get("task") or {}
            if task.get("conversation_id") != conversation_id or not task.get("message"):
                continue
            messages.append(AdvisorMessage(role="user", content=task["message"],
                                           run_key=job["run_key"], state=job["state"]))
            result = self._advisor_result(job["run_key"]) if job["state"] == "COMPLETE" else None
            if result:
                messages.append(AdvisorMessage(
                    role="assistant", content=result["answer"],
                    research_candidates=result.get("research_candidates", []),
                    run_key=job["run_key"], state=job["state"]))
        return AdvisorConversation(conversation_id=conversation_id, messages=messages[-80:])

    def advisor_state(self) -> AdvisorState:
        from ..advisor import NOTES, SNAPSHOT, age

        db = self.state_db()
        snapshot = db.read_meta(SNAPSHOT, {}) or {}
        jobs = self._research_manager(self.profiles()).store.list(limit=500)
        helper_jobs = [j for j in jobs if j.get("task")]
        conversations = list(dict.fromkeys(j["task"]["conversation_id"] for j in helper_jobs
                                          if j["task"].get("conversation_id")))
        return AdvisorState(
            snapshot=AdvisorSnapshot(**snapshot),
            stale=age(snapshot) > 300,
            notes=PersonalNotes(**(db.read_meta(NOTES, {"text": "", "revision": 0}) or {})),
            jobs=[AdvisorJob(**{k: j.get(k)
                                for k in ("run_key", "ticker", "state", "error", "created_at", "task")})
                  for j in helper_jobs[:30]],
            conversations=conversations[:50],
            decisions=self.advisor_decisions(),
            coverage="All Robinhood brokerage accounts, stocks/ETFs only; analysis is read-only.")

    def advisor_notes(self, text: str, revision: int) -> PersonalNotes:
        from ..advisor import NOTES

        if len(text) > 4000:
            raise ServiceError("personal notes must be at most 4000 characters")
        db = self.state_db()
        with db.transaction() as conn:
            old = db.read_meta(NOTES, {}) or {}
            if old.get("revision", 0) != revision:
                raise ServiceConflict("Personal notes changed on another device; reload before saving.")
            value = PersonalNotes(text=text.strip(), revision=revision + 1,
                                  updated_at=utcnow().isoformat())
            db.write_meta(NOTES, value.model_dump(), conn)
        return value

    def start_advisor(self, *, kind: str, request_id: str, message: str = "",
                      conversation_id: str = "default", ticker: str = "",
                      account_id: str | None = None, language: str = "zh") -> dict[str, Any]:
        from ..advisor import SYMBOL
        from ..schedule import analyses_today, settings

        if kind not in ("refresh", "chat", "shortlist", "analysis"):
            raise ServiceError("unknown assistant task")
        try:
            request_id = str(uuid.UUID(request_id))
        except ValueError as exc:
            raise ServiceError("request_id must be a UUID") from exc
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", conversation_id):
            raise ServiceError("invalid conversation identifier")
        if language not in ("zh", "en") or len(message) > 4000:
            raise ServiceError("invalid language or message length")
        if kind in ("chat", "shortlist") and not message.strip():
            raise ServiceError("a message is required")
        ticker = ticker.strip().upper() if kind == "analysis" else "ASSISTANT"
        if not SYMBOL.fullmatch(ticker):
            raise ServiceError("enter a stock or ETF symbol")
        if account_id is not None and not re.fullmatch(r"[0-9a-f]{20}", account_id):
            raise ServiceError("invalid observed account identifier")
        profiles = self.profiles()
        manager = self._research_manager(profiles)
        task = {"kind": kind, "language": language, "account_id": account_id}
        if kind in ("chat", "shortlist"):
            task.update(conversation_id=conversation_id, message=message.strip())
        run_key = "advisor_" + request_id.replace("-", "")
        with manager.store.db.transaction():
            existing = manager.store.get(run_key)
            if existing:
                if existing.get("task") != task or existing["ticker"] != ticker:
                    raise ServiceConflict("request_id was already used for a different request")
                return {**existing, "deduplicated": True}
            jobs = manager.store.list(limit=10000)
            if any(j["state"] in ("QUEUED", "RUNNING") and j.get("task", {}).get("conversation_id") == conversation_id
                   for j in jobs) and kind in ("chat", "shortlist"):
                raise ServiceConflict("The assistant is still answering this conversation.")
            cfg = settings(profiles)
            if kind == "analysis":
                if analyses_today(jobs, cfg) >= int(cfg["max_analyses_per_day"]):
                    raise ServiceError("Today's full-research budget is used; try again next trading day.")
            elif kind != "refresh":
                assistant_jobs = [j for j in jobs if j.get("task", {}).get("kind") in ("chat", "shortlist")]
                # These short calls have their own cap while sharing the existing concurrency pool.
                stamp = utcnow().date().isoformat()
                if sum(j["created_at"].startswith(stamp) for j in assistant_jobs) >= 30:
                    raise ServiceError("Today's 30 assistant replies are used.")
            job, created = manager.create(run_key=run_key, ticker=ticker, bucket="advisory",
                                          trade_date=_last_session(), instrument_type="auto",
                                          trigger="manual", task=task)
        manager.dispatch()
        return {**(manager.store.get(job["run_key"]) or job), "deduplicated": not created}

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
        # The worker names the run directory after the run key, so one lookup reaches the
        # events this run has appended. The containment check is what stops a crafted key
        # from reading a jsonl file elsewhere on disk.
        run_dir = (profiles.project_dir / "runs" / run_key).resolve()
        progress = self._research_progress(run_dir) if runs_root in run_dir.parents else None
        return {**job, "log_tail": text[-4000:], "progress": progress,
                **({"assistant_result": self._advisor_result(run_key)} if job.get("task") else {})}

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
            if proposal.status == ProposalStatus.WAITING_FOR_APPROVAL:
                events.append(Event(
                    key=f"proposal_waiting:{proposal.proposal_id}:{proposal.version}",
                    kind="proposal_waiting",
                    title=f"{proposal.action} {proposal.ticker} needs your approval",
                    detail=(f"${proposal.notional_usd:,.2f} in {proposal.bucket}"
                            + (" (real money)" if proposal.is_real_money else "")),
                    link="/ui/approvals.html", urgent=proposal.is_real_money))
            if proposal.reconciliation_error:
                events.append(Event(
                    key=f"reconciliation_error:{proposal.proposal_id}",
                    kind="reconciliation_error",
                    title=f"{proposal.ticker} order could not be reconciled",
                    detail=proposal.reconciliation_error[:300],
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

        # A fill is not announced from here. Robinhood tells the account holder about every
        # trade in the Agentic account itself, so a message from this side would repeat it. What
        # only this side can know -- an order it cannot reconcile, a decision nobody was asked
        # about -- is announced below.

        if self._micro_enabled(profiles):
            connect_error = self._broker_connect_error()
            if connect_error:
                events.append(Event(
                    key="credential_invalid:robinhood", kind="credential_invalid",
                    title="the broker connection needs attention",
                    # The error text can name an endpoint but never a token.
                    detail=connect_error[:200], link="/ui/system.html", urgent=True))

        for name, ladder in self.risk_state().items():
            # risk_state also carries the market regime, which has a "level" too. Read as a
            # ladder it produced "the regime drawdown ladder is at ELEVATED_RISK, drawdown 0.00%"
            # and repeated it every quiet period for as long as the market stayed there.
            if name not in LADDER_NAMES or not isinstance(ladder, dict) or "level" not in ladder:
                continue
            if ladder["level"] != "NORMAL":
                events.append(Event(
                    key=f"risk_level_changed:{name}:{ladder['level']}", kind="risk_level_changed",
                    title=f"the {name} drawdown ladder is at {ladder['level']}",
                    detail=f"drawdown {ladder.get('drawdown_pct', 0):.2f}% from the high-water mark",
                    link="/ui/risk.html", urgent=ladder.get("manual_reset_required", False)))

        # The last ledger check, whoever ran it. A refusal is the venue and a book disagreeing,
        # which is what reconciliation_error already means; like any standing condition it is
        # repeated each quiet period until a check agrees, and the console button is one way
        # to make that happen today rather than at the next morning's job.
        from ..ledger_confirm import REPORT

        report = self.state_db(profiles).read_meta(REPORT, {}) or {}
        refused = [(row["ledger"], row["error"]) for row in report.get("ledgers") or []
                   if isinstance(row, dict) and row.get("error")]
        if report.get("error"):
            refused.append(("ledgers", report["error"]))
        for ledger, error in refused:
            events.append(Event(
                key=f"ledger_unconfirmed:{ledger}:{report.get('date', '')}",
                kind="reconciliation_error",
                title=f"the {ledger} ledger could not be confirmed",
                detail=str(error)[:300], link="/ui/portfolio.html#ledger-notice"))

        for activity in self.operations()["activities"]:
            if activity["overdue"] and activity["id"] in ("backup", "maintenance", "ledger_confirm"):
                events.append(Event(
                    key=f"backup_overdue:{activity['id']}:{utcnow().date()}",
                    kind="backup_overdue",
                    title=f"{activity['id'].replace('_', ' ')} has not run when expected",
                    detail=f"last success {activity['last_success_at'] or 'never'}",
                    link="/ui/system.html", urgent=activity["id"] == "backup"))

        # Autonomy is the case where nobody was asked, so being told is the only visibility
        # there is. Read from state like everything else here, so a restart re-notices rather
        # than forgetting what happened while the process was down.
        withdrawn = autonomy.load_downgrade(profiles)
        if withdrawn.tripped:
            events.append(Event(
                key=f"autonomy_withdrawn:{withdrawn.at}",
                kind="autonomy_withdrawn",
                title="autonomous reductions have been withdrawn",
                detail=f"{withdrawn.reason}; they stay off until a manual reset",
                link="/ui/system.html", urgent=True))

        # A change made from one device is announced to the others while it can still be
        # withdrawn, and again once it is in force. Derived from the record, like the rest.
        from .. import config_changes as cc

        for change in cc.records(self.state_db(profiles)):
            if change["status"] == cc.PENDING:
                events.append(Event(
                    key=f"config_change_pending:{change['id']}", kind="config_change_pending",
                    title=f"a loosening change to {change['file']} applies at "
                          f"{str(change.get('apply_after') or '')[:16].replace('T', ' ')} UTC",
                    detail="; ".join(change.get("reasons") or change.get("paths") or [])[:300]
                           + f" — requested by {change['requested_by']}; withdraw it from Settings",
                    link="/ui/settings.html", urgent=True))
            elif change["status"] in (cc.APPLIED, cc.FAILED) and change["classification"] == cc.COOLING:
                events.append(Event(
                    key=f"config_change_applied:{change['id']}:{change['status']}",
                    kind="config_change_applied",
                    title=f"a change to {change['file']} "
                          + ("is now in force" if change["status"] == cc.APPLIED else "could not be applied"),
                    detail=(change.get("error") or "; ".join(change.get("paths") or []))[:300],
                    link="/ui/settings.html"))
        for proposal in self._store(profiles).all():
            if not str(proposal.approved_by or "").startswith("autonomy:"):
                continue
            events.append(Event(
                # Keyed on the status too, so the outcome arrives as well as the decision.
                key=f"autonomy_acted:{proposal.proposal_id}:{proposal.status}",
                kind="autonomy_acted",
                title=f"{proposal.action} {proposal.ticker} was decided without asking you",
                detail=(f"{proposal.bucket}, status {proposal.status}"
                        + (f", filled {proposal.filled_quantity:g}" if proposal.filled_quantity
                           else "")),
                link="/ui/activity.html", urgent=proposal.bucket == MICRO_BUCKET))
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

    def heartbeat_withheld_by(self, settings) -> list[str]:
        """Which in-process activities have stopped, so that silence can carry the news.

        The watcher infers an outage from a missing ping, and only the activities that run
        inside this process can be judged here: a weekly schedule or a daily backup is late for
        reasons that deserve their own event, not a paging alarm. State that cannot be read at
        all is the loudest blocker there is, because that is the failure which leaves the
        process answering while it can do nothing.
        """
        required = set(settings.heartbeat_requires)
        if not required:
            return []
        try:
            activities = self.operations()["activities"]
        except Exception as exc:  # noqa: BLE001 - unreadable state must stop the ping, not raise
            return [f"state unreadable: {type(exc).__name__}: {exc}"[:160]]
        return [row["id"] for row in activities if row["id"] in required and row["overdue"]]

    def send_heartbeat(self) -> dict[str, Any]:
        """Ping an external watcher, if one is configured and the work is still happening."""
        from ..notify import DeliveryError, Settings, heartbeat

        try:
            settings = Settings.load(self.profiles())
            result = heartbeat(settings, withhold=self.heartbeat_withheld_by(settings))
            return {**result, "every_minutes": settings.heartbeat_every_minutes}
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
        from ..notify import Settings

        profiles = self.profiles()
        try:
            return webpush.deliver(self.state_db(profiles), profiles.project_dir / "state", {
                "title": "Agentic Trading",
                "detail": "Notifications reach this device.",
                "link": "index.html", "key": "test", "urgent": False},
                subject_env=Settings.load(profiles).push_subject_env)
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
            # The study reads rows, not the response shape: dumping here keeps the evaluation
            # module free of a model that exists for the console.
            proposals=[p.model_dump() for p in self.proposals(include_closed=True)],
            fills=[f.model_dump() for f in self.orders(limit=200).fills])

    def evaluation_snapshot(self, on: str | None = None) -> dict[str, Any]:
        """Mark the study to market for one session. Called daily by the maintenance job."""
        from .. import evaluation

        profiles = self.profiles()
        portfolio = self.portfolio()
        account = portfolio.accounts.get("conceptual")
        if account is None:
            raise ServiceError("the conceptual account could not be read")
        return evaluation.snapshot(
            self.state_db(profiles), profiles, on=on or portfolio.as_of,
            equity_usd=account.equity_usd, net_deposits_usd=account.net_deposits_usd)

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
            profiles, health_status=health.status, paused=bool(health.paused),
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
                        # The report stem and the console's role key differ for the portfolio
                        # manager (decision.md), so carry both rather than make every reader
                        # know that exception.
                        "role": agent_roster.BY_REPORT.get(path.stem),
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
            "instrument_type": record.get("instrument_type"),
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

    #: Progress lines returned with a run. A long analysis writes a few dozen; the cap is
    #: there so a pathological run cannot make the polling response unbounded.
    _PROGRESS_EVENT_CAP: ClassVar = 200

    def _research_progress(self, run_dir: Path) -> dict[str, Any] | None:
        """What a run in flight has done so far, from the events its model calls append.

        The report tree is written in one go when the whole graph finishes, so it says
        nothing while a run is working. ``progress.jsonl`` is appended per model call and is
        the only signal that can tell one desk from another before the run ends.

        Only two desk states are reported here, and both are facts the file states outright:
        a call is open (``working``), or the last one returned (``done``). Whether a desk is
        idle, skipped for this instrument, or stalled is a judgement about the clock and the
        roster, and belongs to the caller that has both.
        """
        try:
            lines = (run_dir / "progress.jsonl").read_text(encoding="utf-8").splitlines()
        except (FileNotFoundError, NotADirectoryError):
            return None
        except OSError:
            return None

        events: list[dict[str, Any]] = []
        for line in lines:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue  # the worker may be mid-write on the final line
            if isinstance(event, dict) and event.get("kind"):
                events.append(event)

        desks: dict[str, dict[str, Any]] = {}
        started: dict[str, Any] = {}
        finished: dict[str, Any] | None = None
        for event in events:
            kind = event.get("kind")
            if kind == "run_started":
                started = event
                continue
            if kind == "run_finished":
                finished = event
                continue
            role = event.get("role")
            if not role or kind not in ("call_started", "call_finished"):
                continue
            desk = desks.setdefault(role, {"calls": 0, "wall_seconds": 0.0, "tools": []})
            desk["last_at"] = event.get("t")
            if kind == "call_started":
                desk["state"] = "working"
                desk["calls"] += 1
            else:
                desk["state"] = "done"
                desk["wall_seconds"] = round(
                    desk["wall_seconds"] + float(event.get("wall_seconds") or 0.0), 1)
                desk["tools"] = sorted({*desk["tools"], *(event.get("tools") or [])})
        return {
            "ticker": started.get("ticker"),
            "bucket": started.get("bucket"),
            "instrument_type": started.get("instrument_type"),
            "analysts": started.get("analysts") or [],
            "started_at": started.get("t"),
            "desks": desks,
            "result": finished,
            "last_event_at": events[-1].get("t") if events else None,
            "events": events[-self._PROGRESS_EVENT_CAP:],
        }

    def research_roster(self) -> dict[str, Any]:
        """The research team, and which analysts each instrument type actually starts.

        The console draws one desk per role. Without this it would keep a second copy of the
        team and of the analyst selection in ``config/``, and disagree with the engine the
        first time either changed — showing a desk that no longer runs, or missing one that
        does.
        """
        profiles = self.profiles()
        return {
            "groups": [{"key": key, "directory": directory}
                       for key, directory in agent_roster.GROUPS],
            "roles": [dict(role) for role in agent_roster.ROLES],
            "analysts": {kind: list(profiles.analysts_for(kind))
                         for kind in ("etf", "stock", "crypto")},
            # How long one model call may run before the adapter gives up. A desk whose call
            # has been open longer than this is genuinely stuck, so the console can say so
            # from configuration rather than from a guessed timeout of its own.
            "call_timeout_seconds": int(profiles.llm.timeout_seconds),
        }

    def research_history(self, limit: int = 20) -> list[dict[str, Any]]:
        """Completed decision records, newest first."""
        profiles = self.profiles()
        runs = sorted((profiles.project_dir / "runs").glob("*/decision_record.json"),
                      key=lambda p: p.stat().st_mtime, reverse=True)
        out = []
        for path in runs:
            try:
                d = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if d.get("task_kind") in ("refresh", "chat", "shortlist"):
                continue
            policy = d.get("policy") or {}
            out.append({
                "run_id": d.get("run_id"), "ticker": d.get("ticker"), "bucket": d.get("bucket"),
                "trade_date": d.get("trade_date"), "created_at": d.get("created_at"),
                # Which desks this run seated. The console shows the team per run rather than
                # inferring an instrument type back out of the analyst list.
                "instrument_type": d.get("instrument_type"),
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
            if len(out) >= limit:
                break
        return out

    # ---------------------------------------------------------------- orders
    #: How often the notification pass opens a venue session only to learn whether it still can.
    #: A standing condition is re-sent every notifications.min_interval_seconds (900) at most.
    BROKER_PROBE_SECONDS: ClassVar[float] = 300.0

    def _broker_connect_error(self) -> str | None:
        probe = getattr(self, "_broker_probe", None)
        now = time.monotonic()
        if probe is None or now - probe[0] >= self.BROKER_PROBE_SECONDS:
            probe = (now, self.micro_live().connect_error)
            self._broker_probe = probe
        return probe[1]

    def _recorded_fills(self, profiles: Profiles, limit: int) -> list[Fill]:
        """Fills this system recorded, newest first, without asking the venue anything."""
        fills_path = profiles.project_dir / str(
            (profiles.risk.get("broker") or {}).get("fills_file", "state/fills.jsonl"))
        rows = StateDB(fills_path).fills(limit)
        if not rows and fills_path.exists() and not StateDB(fills_path).migrated(fills_path):
            for line in fills_path.read_text(encoding="utf-8").splitlines()[-limit:]:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return [Fill(**row) for row in reversed(rows)]

    def orders(self, limit: int = 50) -> Orders:
        """The fills this system recorded and the orders it is tracking, from its own record.

        The venue's open-order list used to be read here too, on every poll of the Activity
        screen: a Robinhood handshake every ten seconds to show a list the Robinhood app
        already shows. What the app cannot show is what this side knows about an order -- the
        proposal it came from, whether it reconciled, and where the two disagree -- and that
        is what is returned. Execution still reads the venue's open orders where they decide
        something, at revalidation.
        """
        profiles = self.profiles()
        fills = self._recorded_fills(profiles, limit)
        tracked = [TrackedOrder(proposal_id=p.proposal_id, order_id=p.broker_order_id,
                                client_order_id=p.client_order_id, symbol=p.ticker,
                                status=proposal_status(p, profiles), venue_status=p.order_status,
                                bucket=p.bucket, side="buy" if p.action in ("BUY", "ADD") else "sell",
                                filled_quantity=p.filled_quantity,
                                filled_avg_price=p.filled_avg_price, reconciled_at=p.reconciled_at,
                                reconciliation_error=p.reconciliation_error)
                   for p in self._store(profiles).all() if p.client_order_id]
        return Orders(fills=fills, tracked_orders=tracked)

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

    # ---------------------------------------------------------------- configuration
    #: Values the console's forms edit, read here so a form and the file cannot disagree.
    FORM_PATHS: ClassVar = {
        "risk_profile.yaml": (
            "research_schedule.enabled", "research_schedule.period",
            "research_schedule.launchd.weekday", "research_schedule.launchd.hour",
            "research_schedule.launchd.minute", "research_schedule.on_non_session_day",
            "research_schedule.tickers", "research_schedule.max_tickers_per_run",
            "research_schedule.max_analyses_per_day", "research_schedule.run_while_paused"),
        "investor_profile.yaml": ("universe.core.individual_stocks.watchlist",),
        "event_calendar.yaml": ("maintained_through", "events"),
    }

    def _config_text(self, profiles: Profiles, file: str) -> str:
        from .. import config_changes as cc

        if file not in cc.FILES:
            raise ServiceError(f"{file} is not a file the console may edit")
        return (profiles.config_dir / file).read_text(encoding="utf-8")

    def config_overview(self) -> dict[str, Any]:
        """The editable files, their sections, the forms' current values and the change trail."""
        from .. import config_changes as cc

        profiles = self.profiles()
        files = []
        values: dict[str, Any] = {}
        for name in cc.FILES:
            text = self._config_text(profiles, name)
            doc = yaml_load(text)
            files.append({
                "file": name, "revision": cc.revision(text),
                "sections": [{"key": s.key, "lines": s.lines,
                              "guarded": cc._guarded(name, s.key)} for s in cc.sections(text)],
            })
            for path in self.FORM_PATHS[name]:
                values[path] = _jsonable(cc.get_value(doc, path))
        records = cc.records(self.state_db(profiles))
        downgrade = autonomy.load_downgrade(profiles)
        return {
            "files": files,
            "values": values,
            "cooling_hours": cc.COOLING_HOURS,
            "pending": [cc.public(r) for r in records if r["status"] == cc.PENDING],
            "recent": [cc.public(r) for r in reversed(records) if r["status"] != cc.PENDING][:20],
            "autonomy": {"armed": autonomy.is_armed(profiles, MICRO_BUCKET),
                         "withdrawn": downgrade.tripped, "reason": downgrade.reason,
                         "at": downgrade.at},
            "launchd_managed": bool(os.environ.get("CT_RELEASE_DIR")),
        }

    def config_section(self, file: str, key: str) -> dict[str, Any]:
        from .. import config_changes as cc

        profiles = self.profiles()
        text = self._config_text(profiles, file)
        try:
            item = cc.section(text, key)
        except cc.ConfigChangeError as exc:
            raise ServiceError(str(exc)) from exc
        return {"file": file, "key": key, "text": item.text, "revision": cc.revision(text),
                "guarded": cc._guarded(file, key)}

    def _prepare_change(self, profiles: Profiles, spec, revision: str) -> tuple[str, str, dict[str, Any]]:
        """The current text, the rebuilt text and the verdict; every refusal is a ServiceError."""
        from .. import config_changes as cc

        current = self._config_text(profiles, spec.file)
        if revision and revision != cc.revision(current):
            raise ServiceConflict(f"{spec.file} changed since it was loaded; reload and try again")
        try:
            new_text = cc.build(current, spec)
            cc.validate(profiles.config_dir, spec.file, new_text)
            verdict = cc.classify(spec.file, current, new_text)
        except cc.ConfigChangeError as exc:
            raise ServiceError(str(exc)) from exc
        return current, new_text, verdict

    def preview_config_change(self, *, file: str, revision: str = "", key: str | None = None,
                              text: str | None = None, set: dict[str, Any] | None = None) -> dict[str, Any]:
        from .. import config_changes as cc

        profiles = self.profiles()
        spec = cc.ChangeSpec(file=file, key=key, text=text, set=dict(set or {}))
        current, new_text, verdict = self._prepare_change(profiles, spec, revision)
        return {"file": file, "diff": cc.unified_diff(current, new_text, file),
                "changed": current != new_text, "revision": cc.revision(current), **verdict}

    def submit_config_change(self, *, file: str, revision: str = "", key: str | None = None,
                             text: str | None = None, set: dict[str, Any] | None = None,
                             reason: str = "", identity: str | None = None,
                             confirmed: bool = False) -> dict[str, Any]:
        """Apply a change now, or park it for the cooling period when it loosens anything.

        ``confirmed`` says the caller re-verified the console password; a loosening change
        without it is refused before anything is written or recorded.
        """
        from .. import config_changes as cc

        profiles = self.profiles()
        spec = cc.ChangeSpec(file=file, key=key, text=text, set=dict(set or {}))
        current, new_text, verdict = self._prepare_change(profiles, spec, revision)
        if current == new_text:
            raise ServiceError("nothing changed")
        if verdict["classification"] == cc.COOLING and not confirmed:
            raise ServiceError("this change loosens a limit; confirm it with the console password")
        db = self.state_db(profiles)
        record = cc.new_record(spec, base=cc.base_of(current, spec), verdict=verdict,
                               identity=identity or "console", reason=reason[:500])
        if record["status"] == cc.APPLIED:
            record["outcome"] = self._write_config(profiles, spec.file, new_text, record)
        with db.transaction() as conn:
            items = cc.records(db)
            items.append(record)
            cc.save(db, items, conn)
        self._journal(profiles,
                      kind="config_changed" if record["status"] == cc.APPLIED else "config_change_pending",
                      subject=record["id"], actor=identity or "console",
                      outcome=record["status"],
                      detail={"file": spec.file, "paths": record["paths"][:20],
                              "classification": record["classification"]})
        return cc.public(record)

    def _write_config(self, profiles: Profiles, file: str, new_text: str,
                      record: dict[str, Any]) -> dict[str, Any]:
        """Write the file, commit it locally, and re-register launchd if the schedule moved."""
        from .. import config_changes as cc
        from ..storage import atomic_write

        atomic_write(profiles.config_dir / file, new_text)
        paths = ", ".join(record["paths"][:6]) + (", …" if len(record["paths"]) > 6 else "")
        message = (f"config: {file} {paths} (console, {record['requested_by']})"
                   + (f"\n\n{record['reason']}" if record.get("reason") else ""))
        outcome = {"git": cc.commit(profiles.project_dir, file, message,
                                    author=f"{record['requested_by']} via console <console@agentic-trading.local>")}
        if any(p.startswith("research_schedule") for p in record["paths"]):
            outcome["launchd"] = self._reregister_research(profiles)
        return outcome

    def _reregister_research(self, profiles: Profiles) -> str:
        from .. import launchd

        try:
            return launchd.reregister_research(data_dir=profiles.project_dir)
        except Exception as exc:  # the file is written; this is reporting
            logger.exception("re-registering the research job failed")
            return f"launchd re-registration failed: {exc}"

    def cancel_config_change(self, change_id: str, identity: str | None = None) -> dict[str, Any]:
        from .. import config_changes as cc

        profiles = self.profiles()
        db = self.state_db(profiles)
        with db.transaction() as conn:
            items = cc.records(db)
            match = next((r for r in items if r["id"] == change_id), None)
            if match is None:
                raise ServiceError("no such change")
            if match["status"] != cc.PENDING:
                raise ServiceConflict(f"that change is already {match['status']}")
            match["status"] = cc.CANCELLED
            match["applied_at"] = utcnow().isoformat()
            match["outcome"] = {"cancelled_by": identity or "console"}
            cc.save(db, items, conn)
        self._journal(profiles, kind="config_change_cancelled", subject=change_id,
                      actor=identity or "console", detail={"file": match["file"]})
        return cc.public(match)

    def apply_config_changes(self) -> list[dict[str, Any]]:
        """Apply every parked change whose cooling period has ended. Runs on the API's loop."""
        from .. import config_changes as cc

        profiles = self.profiles()
        db = self.state_db(profiles)
        if not cc.due(cc.records(db), utcnow()):
            return []
        applied = []
        with db.transaction() as conn:
            items = cc.records(db)
            for record in cc.due(items, utcnow()):
                spec = cc.spec_of(record)
                current = self._config_text(profiles, spec.file)
                blocked = cc.moved(current, spec, record.get("base"))
                if blocked is None:
                    try:
                        new_text = cc.build(current, spec)
                        cc.validate(profiles.config_dir, spec.file, new_text)
                    except cc.ConfigChangeError as exc:
                        blocked = str(exc)
                record["applied_at"] = utcnow().isoformat()
                if blocked:
                    record["status"], record["error"] = cc.FAILED, blocked
                else:
                    record["status"] = cc.APPLIED
                    record["outcome"] = self._write_config(profiles, spec.file, new_text, record)
                applied.append(record)
            cc.save(db, items, conn)
        for record in applied:
            self._journal(profiles, kind="config_changed", subject=record["id"],
                          actor=record["requested_by"], outcome=record["status"],
                          detail={"file": record["file"], "paths": record["paths"][:20],
                                  "classification": record["classification"],
                                  "error": record.get("error")})
        return [cc.public(r) for r in applied]

    def record_cashflow(self, *, bucket: str, amount_usd: float, kind: str = "deposit",
                        date: str | None = None, reason: str = "",
                        identity: str | None = None) -> dict[str, Any]:
        """Fund or defund a bucket, the same call as ``ct-state cashflow``."""
        from ..portfolio import cash_flow_summary, record_cash_flow
        from ..state_cli import _sources

        profiles = self.profiles()
        _proposal, _fills, ledgers, _ladders = _sources(profiles)
        if bucket in ("core", "sandbox"):
            ledger = ledgers[0]
        elif len(ledgers) > 1:
            ledger = ledgers[1]
        else:
            raise ServiceError(f"no ledger holds bucket {bucket!r}")
        try:
            state = record_cash_flow(ledger, bucket=bucket, amount_usd=amount_usd, kind=kind,
                                     date=date, note=reason)
        except (ValueError, KeyError, PortfolioError) as exc:
            raise ServiceError(str(exc)) from exc
        self._journal(profiles, kind="cashflow_recorded", subject=bucket,
                      actor=identity or "console",
                      detail={"bucket": bucket, "kind": kind, "amount_usd": amount_usd,
                              "date": date})
        return {"ledger": str(ledger.name), "bucket": bucket,
                "cash_usd": state.buckets[bucket].cash_usd,
                "net_deposits": cash_flow_summary(state)}

    def record_password_change(self, identity: str | None = None) -> dict[str, Any]:
        self._journal(self.profiles(), kind="password_changed", actor=identity or "console")
        return {"rotated": True}

    def autonomy_stop(self, reason: str, identity: str | None = None) -> dict[str, Any]:
        profiles = self.profiles()
        if not reason.strip():
            raise ServiceError("say why; the reason is the useful half of the record")
        state = autonomy.trip(profiles, f"stopped by hand: {reason.strip()}")
        self._journal(profiles, kind="autonomy_stopped", actor=identity or "console")
        return {"withdrawn": state.tripped, "reason": state.reason, "at": state.at}

    def autonomy_resume(self, reason: str, identity: str | None = None) -> dict[str, Any]:
        profiles = self.profiles()
        if not reason.strip():
            raise ServiceError("say what was looked at before restoring autonomy")
        state = autonomy.reset(profiles, reason.strip())
        self._journal(profiles, kind="autonomy_resumed", actor=identity or "console")
        return {"withdrawn": state.tripped, "reason": state.reason, "at": state.at}


def yaml_load(text: str) -> dict[str, Any]:
    import yaml

    data = yaml.safe_load(text)
    return data if isinstance(data, dict) else {}


def _jsonable(value: Any) -> Any:
    if isinstance(value, calendar_date | datetime):
        return value.isoformat()
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    return value


# ---------------------------------------------------------------------- helpers
def _file_stamp(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size


def _directory_stamp(directory: Path) -> tuple[tuple[str, int, int], ...]:
    """Every file under a directory, by name, modification time and size: any edit moves it."""
    stamps = []
    for path in sorted(Path(directory).rglob("*")):
        stamp = _file_stamp(path) if path.is_file() else None
        if stamp is not None:
            stamps.append((str(path.relative_to(directory)), *stamp))
    return tuple(stamps)


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
    environment at every entry point (:mod:`agentic_trading.env`), so there is one answer
    and it is the one the fetcher sees.
    """
    import os

    if os.environ.get("FRED_API_KEY", "").strip():
        return True
    return bool(load_env(profiles.project_dir) and os.environ.get("FRED_API_KEY", "").strip())


def _file_tail(path: Path, max_bytes: int) -> list[str]:
    """The complete lines in the last ``max_bytes`` of a file; nothing if it cannot be read."""
    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(max(0, size - max_bytes))
            data = stream.read()
    except OSError:
        return []
    lines = data.decode("utf-8", errors="replace").splitlines()
    return lines[1:] if size > max_bytes else lines        # the first may start mid-line


#: How long a "no usable bar yet" answer stands before the vendor is asked again. A published
#: bar does not go away, so a "yes" stands for the life of the process.
SESSION_PROBE_RETRY_SECONDS = 600.0
_SESSION_PROBES_KEPT = 16
_session_probes: dict[str, tuple[bool, float]] = {}


def _session_has_data(session: str) -> bool:
    """Whether the data vendor has published a usable bar for this session.

    A vendor can lag hours behind the close, publishing a bar with volume but no closing price.
    Research and pricing must anchor to a session that is actually complete, so this probes a
    single liquid symbol rather than assuming the calendar and the vendor agree.

    This was an ``lru_cache``, which kept a "no" for the life of the API process. Probed once in
    the vendor's lag window, a session stayed "no data" until a restart: every research run and
    every price the next day anchored to the session before it, and each later day added another
    stale key, up to four sessions behind. A "no" is therefore only remembered for a few minutes,
    which still keeps a console polling every few seconds from asking the vendor each time.
    """
    now = time.monotonic()
    known = _session_probes.get(session)
    if known is not None and (known[0] or now - known[1] < SESSION_PROBE_RETRY_SECONDS):
        return known[0]
    try:
        from tradingagents.dataflows.stockstats_utils import load_ohlcv

        df = load_ohlcv("SPY", session)
        usable = df is not None and not df.empty
    except Exception:  # noqa: BLE001 - any failure means "not usable"
        usable = False
    _session_probes[session] = (usable, now)
    while len(_session_probes) > _SESSION_PROBES_KEPT:
        _session_probes.pop(next(iter(_session_probes)))
    return usable


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


def _hard_risk_view(hard_risk: dict[str, Any]) -> HardRiskView:
    return HardRiskView(
        verdict=hard_risk.get("verdict"),
        final_action=hard_risk.get("final_action"),
        notional_usd=hard_risk.get("proposed_notional_usd"),
        post_trade_weight_pct=hard_risk.get("post_trade_weight_pct"),
        requires_human_approval=hard_risk.get("requires_human_approval", True),
        rules=[HardRiskRule(id=r.get("rule_id"), name=r.get("name"), passed=r.get("passed"),
                            detail=r.get("detail")) for r in hard_risk.get("rules", [])],
        failed_rule_ids=hard_risk.get("failed_rule_ids", []),
        advisories=hard_risk.get("advisories", []),
        ladder=hard_risk.get("ladder"),
        regime=hard_risk.get("regime"),
    )


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


