"""Approving a proposal, executing it, and the controls that stop or resume that happening.

This is the only module that sends an order to a venue: it is the one that imports
``execution.execute``. The test beside it (tests/test_money_paths.py) holds that to one module,
so an audit of what can move money is an audit of this file.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import replace
from typing import Any

from .. import autonomy, exits
from ..execution import ExecutionResult, needs_reconciliation, reconcile_proposal, revalidate
from ..execution import execute as execute_proposal
from ..micro_live import BUCKET as MICRO_BUCKET
from ..portfolio import load_portfolio
from ..profile import Profiles
from ..proposals import (
    APPROVED,
    NEEDS_REANALYSIS,
    PROPOSED,
    REJECTED,
    Proposal,
    ProposalStore,
    utcnow,
)
from ..storage import file_lock
from .errors import ServiceConflict, ServiceError
from .shapes import (
    ExitContinuation,
    HardRiskRule,
    HardRiskView,
    ProposalDetail,
    ProposalFreshness,
    ProposalResearch,
    ProposalSummary,
    ProposalWorld,
)
from .status import FRESHNESS_LABELS, ProposalStatus, SystemStatus, proposal_status

#: How each way of being blocked is answered. A conflict says "this may become possible
#: without anybody changing their mind" -- the venue is still working, the reading is old --
#: and a plain refusal says "not under this authorisation". The console shows the text, so
#: the wording is the contract's and not this module's invention.
_ORDER_WORKING_SAYS = ("an order from this exit is still working at the venue; reconciliation "
                       "decides what remains")
_UNRESOLVED_SAYS = "the previous attempt is unresolved; reconciliation continues"
_SPENT_SAYS = ("the original authorisation is fully filled; a position acquired since is a new "
               "idea and needs its own proposal")
_EXPIRED_SAYS = "the research behind this exit has expired; request fresh research"
CONTINUE_REFUSALS: dict[str, tuple[type[ServiceError], str | None]] = {
    exits.ORDER_WORKING: (ServiceConflict, _ORDER_WORKING_SAYS),
    exits.RECONCILIATION_UNRESOLVED: (ServiceConflict, _UNRESOLVED_SAYS),
    exits.ALLOWANCE_SPENT: (ServiceError, _SPENT_SAYS),
    exits.RESEARCH_EXPIRED: (ServiceError, _EXPIRED_SAYS),
    exits.POSITION_GONE: (ServiceError, "the position is gone; nothing to continue"),
    # No wording of its own: the block already says which order, which account, which reading.
    exits.ACTIVE_ORDER: (ServiceConflict, None),
    exits.QUANTITY_UNKNOWN: (ServiceConflict, None),
    exits.CONTINUATION_WAITING: (ServiceConflict, None),
    exits.BELOW_PRECISION: (ServiceError, None),
    exits.NOT_ATTEMPTED: (ServiceError, None),
}


#: Named for the package rather than this module: the split is internal, so a log
#: record must still read as it did, and getLogger returns the one same object.
logger = logging.getLogger("agentic_trading.service")


class ApprovalsMixin:
    """Everything that can place, reduce or reconcile a real order."""

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
            approved_via=p.approved_via,
            freshness=ProposalFreshness(
                state=freshness, label=FRESHNESS_LABELS.get(freshness, freshness),
                detail=why, freshness_class=p.freshness_class(profiles),
                ttl_minutes=rules.get("ttl_minutes"),
                age_minutes=round(p.age_minutes(), 1),
                max_price_drift_pct=rules.get("max_price_drift_pct")),
            reference_price=p.reference_price,
            run_id=p.run_id,
            authority_version=p.authority_version,
        )

    def proposal(self, proposal_id: str) -> ProposalDetail:
        """Full detail: the reasoning, the risk rules, and the world the proposal was born into."""
        profiles = self.profiles()
        store = self._store(profiles)
        p = store.get(proposal_id)
        if p is None:
            raise ServiceError(f"no proposal {proposal_id!r}")
        current_authority = autonomy.note_version(profiles, store.db)
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
            authority_changed=autonomy.authority_change(
                autonomy.version_history(store.db), p.authority_version, current_authority),
            authority_changed_during_research=p.authority_changed_during_research,
            exit=self._exit_view(profiles, store, p),
        )

    def _fills_for_replay(self, profiles: Profiles) -> list[dict[str, Any]]:
        """The recorded fills, as far back as a position floor may be replayed.

        The same reader the Activity screen uses, so the legacy log a database from before the
        fills table still holds is read here too. ``Fill`` allows unknown keys, so an older
        row keeps whatever it carried.
        """
        return [f.model_dump() for f in self._recorded_fills(profiles, autonomy.FILL_REPLAY_LIMIT)]

    def _autonomous_block(self, profiles: Profiles, store: ProposalStore, p: Proposal,
                          ledger_quantity: float | None,
                          fills: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
        """The floor under this one position, or None for an action that is never autonomous."""
        if p.action != autonomy.AUTONOMOUS_ACTION:
            return None
        floor = autonomy.position_floor_for(
            profiles, fills=fills if fills is not None else self._fills_for_replay(profiles),
            proposals=store.all(), bucket=p.bucket, symbol=p.ticker,
            ledger_quantity=ledger_quantity)
        return autonomy.reduce_block(profiles, p.bucket, floor)

    def _exit_facts(self, profiles: Profiles, store: ProposalStore,
                    p: Proposal) -> dict[str, Any]:
        """The readings every answer about this exit is derived from, taken once.

        Read-only, and off readings that already exist: the ledger, and the venue reading the
        console keeps between polls. A screen asking how much can be sold must not cost a
        Robinhood session, and execution re-reads the venue for itself in any case.

        The screen, the continuation endpoint and the to-do list all take this. The contract asks
        for one derivation on purpose -- three that agree today would not stay agreed.
        """
        from .. import exits
        from ..portfolio import PortfolioError, load_portfolio
        from ..schedule import ledger_for

        ledger_quantity = None
        try:
            ledger = load_portfolio(ledger_for(profiles, p.bucket))
            held = ledger.position(p.ticker, p.bucket)
            ledger_quantity = float(held.quantity) if held else 0.0
        except (PortfolioError, OSError, ValueError):
            ledger_quantity = None            # unreadable is not empty
        reading = None
        if p.bucket == MICRO_BUCKET and self._micro_enabled(profiles):
            reading, _stale = self._venue_reading.peek()
        active = [row for row in store.db.reservations(active_only=True)
                  if str(row["account_key"]) == ("micro_live" if p.bucket == MICRO_BUCKET
                                                 else "conceptual")]
        mine = {row["proposal_id"] for row in active}
        committed = sum(float(row["quantity"] or 0.0) for row in active
                        if (store.get(row["proposal_id"]) or p).ticker == p.ticker)
        everything = store.all()
        # The research clock of the chain, not of this member: a continuation inherits the root's
        # ``created_at``, and ``research_expired`` reads the age rather than the lifecycle status,
        # so a root marked EXECUTED after a partial fill -- the very case a continuation is for --
        # does not report itself as fresh forever.
        root_id = exits.chain_of(p, everything)["root_proposal_id"]
        anchor = next((m for m in everything if m.proposal_id == root_id), p)
        return {"sources": exits.from_reading(ledger_quantity=ledger_quantity, reading=reading,
                                             symbol=p.ticker, committed_quantity=committed),
                "ledger_quantity": ledger_quantity,
                "everything": everything,
                "account_has_active_order": bool(mine - {p.proposal_id}),
                "research_expired": anchor.research_expired(profiles)}

    def _exit_view(self, profiles: Profiles, store: ProposalStore,
                   p: Proposal) -> dict[str, Any] | None:
        """What a sell would do to this position, or None for anything that is not a sell."""
        from .. import exits

        if p.action not in exits.REDUCING:
            return None
        facts = self._exit_facts(profiles, store, p)
        return exits.view(p, facts["everything"], sources=facts["sources"],
                          account_has_active_order=facts["account_has_active_order"],
                          research_expired=facts["research_expired"],
                          autonomous=self._autonomous_block(profiles, store, p,
                                                            facts["ledger_quantity"]))

    # ------------------------------------------------------------ continuing an exit
    def continue_exit(self, proposal_id: str, *, request_id: str, identity: str | None = None,
                      authority_version: str | None = None,
                      acknowledge_authority_change: bool = False) -> dict[str, Any]:
        """Take up the unsold part of an exit as a new proposal, for a person to approve.

        Nothing is executed here and nothing is approved: an exit that stopped half way is a
        second decision, and this endpoint only prepares the thing that decision is made about.
        Three guards stop one chain growing two executable branches, and all three are re-read
        inside the store lock, so two tabs, a restart and a repeated tap cannot beat them:
        one unfinished continuation per chain, one continuation per request id, and a refusal
        while any member of the chain could still be acted on at the venue.

        The continuation inherits the root's research clock rather than starting its own. That is
        the whole reason the contract insists on it: creating a record must not wash stale
        research into something that looks fresh, and a root past its lifetime is refused here
        rather than quietly given a new one.
        """
        profiles = self.profiles()
        store = self._store(profiles)
        asked = store.get(proposal_id)
        if asked is None or asked.proposal_id != proposal_id:
            raise ServiceError(f"no proposal {proposal_id!r}")
        if asked.action not in exits.REDUCING:
            raise ServiceError(f"{asked.action} has no exit chain; only SELL and REDUCE do")
        # By the same rule as an approval, and before anything is written: a screen that showed an
        # older authority reloads, and one that has loosened needs the person to say they looked.
        current_authority, authority_changed = self._authority_check(
            profiles, store, asked, authority_version, acknowledge_authority_change)
        with store.locked():
            return self._continue_locked(profiles, store, proposal_id, request_id=request_id,
                                         identity=identity, authority=current_authority,
                                         authority_changed=authority_changed)

    def _continue_locked(self, profiles: Profiles, store: ProposalStore, proposal_id: str, *,
                         request_id: str, identity: str | None, authority: str,
                         authority_changed: dict[str, Any] | None) -> dict[str, Any]:
        """The whole decision and the write, with the store lock held for both."""
        p = store.get(proposal_id)
        if p is None:
            raise ServiceError(f"no proposal {proposal_id!r}")
        facts = self._exit_facts(profiles, store, p)
        everything = facts["everything"]
        members = exits.members_of(p, everything)
        root = members[0]

        # One request id makes one continuation, whatever happened to it since; the same id
        # pointed at another chain is a different intention wearing the same label.
        replayed = next((m for m in everything if m.continuation_request_id == request_id), None)
        if replayed is not None:
            if replayed.proposal_id not in {m.proposal_id for m in members}:
                raise ServiceConflict(
                    f"request id {request_id} already created the continuation "
                    f"{replayed.proposal_id} of another exit; use a new id for this one")
            return self._continuation_answer(profiles, replayed, root, deduplicated=True)
        waiting = exits.open_continuation(p, everything)
        if waiting is not None:
            return self._continuation_answer(profiles, waiting, root, deduplicated=True)

        chain = exits.chain_of(p, everything)
        onward = exits.continuable(p, everything, sources=facts["sources"], chain=chain,
                                   account_has_active_order=facts["account_has_active_order"],
                                   research_expired=facts["research_expired"])
        if not onward["eligible"]:
            first = onward["blocked_by"][0]
            error, wording = CONTINUE_REFUSALS.get(first["code"], (ServiceError, None))
            raise error(wording or first["detail"])

        quantity = float(onward["quantity"])
        # A proportional share of the original authorisation's value. The figure is not what the
        # order is sized by -- the quantity is -- but it is what every screen and every notional
        # limit reads, so it has to describe this proposal rather than the whole exit.
        allowance = float(root.sell_quantity or 0.0)
        notional = (float(root.notional_usd) * quantity / allowance) if allowance > 0 else \
            float(root.notional_usd)
        continuation = replace(
            root,
            proposal_id=f"{root.trade_date}-{root.ticker}-{uuid.uuid4().hex[:8]}",
            status=PROPOSED,
            sell_quantity=quantity,
            notional_usd=notional,
            requires_human_approval=True,
            continuation_of=root.proposal_id,
            continuation_request_id=request_id,
            continued_at=utcnow().isoformat(),
            # Inherited on purpose: created_at is the research clock, not the moment this record
            # was made. What this record's own age is, continued_at says.
            created_at=root.created_at,
            # Nothing of the earlier attempt carries over. An inherited order id or fill count
            # would make the new proposal look like it had already been to the venue, and the
            # chain's arithmetic counts every member's fills.
            approved_at=None, approved_by=None, approved_via=None, approval_note=None,
            approval_request_id=None, approved_version=None, approval_response=None,
            executed_at=None, client_order_id=None, broker_order_id=None,
            executed_notional_usd=None, submit_attempts=0, order_status=None, order_request=None,
            filled_quantity=0.0, filled_avg_price=None, reconciled_at=None,
            reconciliation_error=None, execution_ledger_path=None,
            history=[],
            _revision=0,
        )
        continuation.note(
            "exit_continued",
            f"continues {root.proposal_id}: {quantity:g} of the {allowance:g} authorised remains "
            f"unsold, {chain['filled']:g} filled",
            root_proposal_id=root.proposal_id, request_id=request_id, identity=identity,
            purpose="exit_review", research_anchor_at=root.created_at,
            authority_version=authority, authority_changed=authority_changed,
            remaining_allowance=chain["remaining_allowance"])
        store.save(continuation)
        return self._continuation_answer(profiles, continuation, root, deduplicated=False)

    def _continuation_answer(self, profiles: Profiles, continuation: Proposal, root: Proposal, *,
                             deduplicated: bool) -> dict[str, Any]:
        """The one answer shape, whether a continuation was just made or already existed."""
        return ExitContinuation(
            deduplicated=deduplicated,
            continuation_of=continuation.continuation_of or root.proposal_id,
            root_proposal_id=root.proposal_id,
            authorised_quantity=float(continuation.sell_quantity or 0.0),
            research_anchor_at=continuation.created_at,
            continued_at=continuation.continued_at,
            proposal=self._proposal_summary(continuation, profiles),
        ).model_dump()

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
                terms: dict[str, Any] | None = None, authority_version: str | None = None,
                acknowledge_authority_change: bool = False,
                approved_via: str = "console") -> dict[str, Any]:
        """Approve a specific idea once, under the verified caller's identity.

        ``approved_via`` says which kind of caller this is -- the console, the CLI, or the
        autonomous pass -- and is recorded rather than inferred from ``identity``, whose text is
        a session's username. A limit that decided anything by reading a username for a prefix
        would be deciding on something a person can choose.
        """
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
            # The authority of the moment, against what the screen showed and what the proposal
            # was made under. Refused here, before the venue is opened: there is nothing to
            # record about an approval that never became one.
            current_authority, authority_changed = self._authority_check(
                profiles, store, p, authority_version, acknowledge_authority_change)
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
                        p.approved_via = approved_via
                        p.approval_note = note
                        p.approval_request_id = request_id
                        p.approved_version = version
                        # A proposal handed back by an earlier approval still carries that
                        # approval's answer, and the answer is what gets returned below.
                        p.approval_response = None
                        p.note("approved", note or "explicit approval through the console",
                               identity=identity, via=approved_via, version=version,
                               request_id=request_id, terms=terms,
                               authority_version=current_authority,
                               authority_changed=authority_changed)
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
                    "authority_changed": authority_changed,
                }
                p.approval_response = response
                store.save(p)
                return response

    @staticmethod
    def _authority_check(profiles: Profiles, store: ProposalStore, p: Proposal,
                         offered: str | None, acknowledged: bool) -> tuple[str, dict[str, Any] | None]:
        """The authority version now, and how it moved since the proposal was made.

        Two refusals, both before the venue is opened. A screen that showed an older authority
        is sent back to reload, exactly as one that showed an older proposal is. A proposal made
        under an authority that has since loosened -- or moved in a way the record cannot
        classify, which includes a proposal that predates the record -- needs the person to say
        they looked. A tightened authority needs nothing more: execution reads the current
        limits, so the tighter ones are the ones that apply.
        """
        if acknowledged and offered is None:
            # An acknowledgement is of a particular change. Without the version it was made
            # against, a change that landed while the person was reading would be covered by it.
            raise ServiceError("acknowledging an authority change requires the authority_version "
                               "it was acknowledged against")
        current = autonomy.note_version(profiles, store.db)
        if offered is not None and offered != current:
            raise ServiceConflict("authority changed since the screen loaded; reload and review")
        change = autonomy.authority_change(autonomy.version_history(store.db),
                                           p.authority_version, current)
        if (change is not None and change["classification"] in autonomy.NEEDS_ACKNOWLEDGEMENT
                and not acknowledged):
            raise ServiceConflict(f"authority {change['classification']} since this proposal was "
                                  "made; review the change and acknowledge it")
        return current, change

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

        # The authority of this moment. A proposal born under any other one -- tightened since,
        # loosened since, or never recorded -- waits for a person; code does not weigh the
        # difference, it only acts where nothing has moved.
        current_authority = autonomy.note_version(profiles, store.db)
        ledger = load_portfolio(profiles.project_dir / str(
            (profiles.risk.get("micro_live_execution") or {}).get(
                "ledger_file", "state/micro_live_portfolio.yaml")))
        # Read once for the pass, not once per candidate: the replay is the same log every time,
        # and this runs on a fifteen-second loop.
        fills = self._fills_for_replay(profiles) if any(
            p.action == autonomy.AUTONOMOUS_ACTION for p in candidates) else []
        for proposal in candidates:
            if proposal.authority_version != current_authority:
                born_under = proposal.authority_version or "no recorded version"
                reason = (f"authority changed since research (proposal under {born_under}, "
                          f"now {current_authority})")
                report["declined"].append({"proposal_id": proposal.proposal_id, "reasons": [reason]})
                continue
            held = ledger.position(proposal.ticker, MICRO_BUCKET)
            held_quantity = float(held.quantity) if held else None
            floor = (autonomy.position_floor_for(
                profiles, fills=fills, proposals=everything, bucket=MICRO_BUCKET,
                symbol=proposal.ticker, ledger_quantity=held_quantity)
                if proposal.action == autonomy.AUTONOMOUS_ACTION else None)
            decision = autonomy.permits(
                profiles, bucket=MICRO_BUCKET, action=proposal.action,
                confidence=proposal.confidence,
                limit_class=proposal.limit_class,
                held_quantity=held_quantity,
                requested_quantity=proposal.sell_quantity,
                reduced_today=autonomy.reduced_today(everything, MICRO_BUCKET),
                opened_today=autonomy.opened_today(everything, MICRO_BUCKET),
                floor=floor)
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
                           "sell_quantity": proposal.sell_quantity},
                    authority_version=current_authority, approved_via=autonomy.AUTONOMOUS_VIA)
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
        halt = autonomy.halt_file(profiles)
        halt.parent.mkdir(parents=True, exist_ok=True)
        halt.write_text(f"{utcnow().isoformat()} {reason}\n", encoding="utf-8")
        # The halt file is deleted on resume, so without this the pause leaves no trace at all.
        self._journal(profiles, kind="system_paused", actor=identity or "console",
                      detail={"reason": reason[:200]})
        return {"paused": True, "reason": reason, "status": SystemStatus.PAUSED}

    def resume(self, identity: str | None = None) -> dict[str, Any]:
        profiles = self.profiles()
        halt = autonomy.halt_file(profiles)
        halt.unlink(missing_ok=True)
        self._journal(profiles, kind="system_resumed", actor=identity or "console")
        return {"paused": False, "status": SystemStatus.READY}

    # ---------------------------------------------------------------- autonomy
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


# ---------------------------------------------------------------------- helpers
def _close(broker: Any) -> None:
    if hasattr(broker, "close"):
        try:
            broker.close()
        except Exception as exc:  # noqa: BLE001
            logger.debug("error closing broker session: %s", exc)


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
