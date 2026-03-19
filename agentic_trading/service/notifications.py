"""Which events are worth telling somebody about, and the devices they are told through."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .. import autonomy
from ..micro_live import BUCKET as MICRO_BUCKET
from ..profile import Profiles
from ..proposals import PROPOSED, utcnow
from .errors import ServiceError
from .status import ProposalStatus

#: The drawdown ladders `risk_state` reports, as opposed to the other entries beside them.
LADDER_NAMES = ("total", "core", "sandbox", "micro_live")

#: Named for the package rather than this module: the split is internal, so a log
#: record must still read as it did, and getLogger returns the one same object.
logger = logging.getLogger("agentic_trading.service")

#: How each to-do kind ranks: severity, priority (1 is taken first), and whether only a person
#: can resolve it. The order is the plan's: an order whose state is unknown, a book that does
#: not agree with the venue and a connection that no longer works come before everything;
#: then a halt that needs a hand; then a proposal that needs a decision; then a failed
#: background task; and last the things that are only information.
RANKING: dict[str, tuple[str, int, bool]] = {
    "order_unknown": ("critical", 1, True),
    "reconciliation_error": ("critical", 1, True),
    "ledger_unconfirmed": ("critical", 1, True),
    "credential_invalid": ("critical", 1, True),
    "risk_halt": ("critical", 2, True),
    "autonomy_withdrawn": ("high", 2, True),
    "proposal_waiting": ("high", 3, True),
    "exit_incomplete": ("high", 3, True),
    "proposal_stale": ("normal", 3, True),
    "research_failed": ("normal", 4, True),
    "research_limit_exhausted": ("normal", 4, True),
    "backup_overdue": ("normal", 4, True),
    "config_change_failed": ("high", 4, True),
    "personal_task_failed": ("normal", 4, True),
    "config_change_pending": ("info", 5, False),
    "personal_task_done": ("info", 5, False),
}

#: What a screen may offer against each kind. A closed set: the console localises these.
ACTIONS: dict[str, list[str]] = {
    "order_unknown": ["open"],
    "reconciliation_error": ["open"],
    "ledger_unconfirmed": ["confirm_ledger", "record_cashflow", "open"],
    "credential_invalid": ["open"],
    "risk_halt": ["open"],
    "autonomy_withdrawn": ["reset_autonomy", "open"],
    "proposal_waiting": ["approve", "reject", "reanalyze"],
    "exit_incomplete": ["continue_exit", "open"],
    "proposal_stale": ["reanalyze", "reject"],
    "research_failed": ["retry", "open"],
    "research_limit_exhausted": ["retry", "cancel", "open"],
    "backup_overdue": ["open"],
    "config_change_failed": ["open"],
    "config_change_pending": ["withdraw_change"],
    "personal_task_done": ["open"],
    "personal_task_failed": ["retry", "open"],
}

_SEVERITY_RANK = {"critical": 0, "high": 1, "normal": 2, "info": 3}

#: How long a finished task the person has not opened stays on Home, and how long a failure
#: does. After that they are history, which is Activity's to show, not a to-do.
DONE_HOURS = 24.0
FAILED_HOURS = 72.0

#: How long an exit that stopped part way keeps asking. A sell nobody finished is not a
#: permanent to-do: after this the remainder is an ordinary position, and continuing it under
#: research this old would be refused anyway.
EXIT_INCOMPLETE_HOURS = 72.0

#: The personal tasks a person asked for by name. A chat or a refresh that failed is announced
#: like any failure, but it is not something Home keeps asking about.
PERSONAL_REQUESTED = ("analysis",)


@dataclass
class Condition:
    """One unresolved state, as the attention list shows it and as a notification announces it.

    Read from state on every pass, never emitted at the site that caused it, so a restart
    re-notices what is still true and a problem that resolved is simply absent. ``id`` is
    stable for as long as the condition stands and doubles as the notification's dedup key,
    which is why the split between the two readers changed no key.
    """

    id: str
    kind: str
    title: str
    detail: str = ""
    scope: str = "system"                     # agentic | personal | demo | system
    account_scope_kind: str = "single"        # single | all_personal | unknown
    account_id: str | None = None
    account_ids: list[str] = field(default_factory=list)
    account_label: str | None = None
    subject: str | None = None
    since: str | None = None
    link: dict[str, Any] = field(default_factory=lambda: {"page": "index", "params": {}})
    #: Whether the attention list shows it. An announcement -- an order decided without
    #: asking, a change that landed -- is told once and is never a to-do.
    attention: bool = True
    #: The notification this condition raises, if any. Keys are the condition id.
    notify_kind: str | None = None
    notify_link: str = ""
    urgent: bool = False
    severity: str = "info"
    priority: int = 5
    requires_person: bool = False
    actions: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        ranked = RANKING.get(self.kind)
        if ranked is None:
            self.attention = False
        else:
            self.severity, self.priority, self.requires_person = ranked
            self.actions = list(ACTIONS.get(self.kind, []))

    def event(self):
        from ..notify import Event

        return Event(key=self.id, kind=str(self.notify_kind), title=self.title,
                     detail=self.detail, link=self.notify_link, urgent=self.urgent)

    def item(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in (
            "id", "kind", "severity", "priority", "requires_person", "scope", "account_scope_kind",
            "account_id", "account_ids", "account_label", "subject", "title", "detail", "since",
            "link", "actions")}


def _bucket_scope(bucket: str | None) -> tuple[str, str | None]:
    if bucket == MICRO_BUCKET:
        return "agentic", MICRO_BUCKET
    if bucket in ("core", "sandbox", "conceptual"):
        return "demo", "conceptual"
    return "system", None


def _label(scope: str, account: str | None) -> str | None:
    return {"agentic": "Agentic", "demo": "Demo"}.get(scope)


def _link(page: str, **params: Any) -> dict[str, Any]:
    """A place in the console as {page, params}; the console builds the href and checks it."""
    return {"page": page, "params": dict(params)}


def _parse(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)


class NotificationsMixin:
    """Collecting notifications, dispatching them, and the push devices they reach."""

    # ---------------------------------------------------------------- conditions
    def collect_conditions(self, now: datetime | None = None) -> tuple[list[Condition], list[str]]:
        """Every unresolved state a person might need to see, read from state on each pass.

        Pulled rather than emitted at each site. A push would lose whatever happened while the
        process was down, and this system's whole point is that it runs unattended; reading the
        world each pass means a restart re-notices a waiting proposal instead of forgetting it.

        One pass feeds two readers. The attention list (``attention``) shows the conditions that
        still need a person and drops each one the moment its state resolves. The notification
        pass (``collect_notifications``) announces the same conditions, plus a few that are told
        once and are never a to-do. A source that cannot be read is reported by name rather
        than silently contributing nothing: "nothing to do" and "could not look" must never
        read the same.
        """
        from .. import config_changes as cc
        from ..advisor import SNAPSHOT
        from ..ledger_confirm import REPORT

        profiles = self.profiles()
        now = now or utcnow()
        out: list[Condition] = []
        unreadable: list[str] = []

        def read(source: str, collector) -> None:
            try:
                collector()
            except Exception as exc:  # one unreadable source must not hide the rest
                logger.exception("could not read %s for the attention list", source)
                unreadable.append(f"{source}: {type(exc).__name__}: {exc}"[:200])

        stored: dict[str, Any] = {}

        def proposals() -> None:
            stored.update({p.proposal_id: p for p in self._store(profiles).all()})
            for proposal in self.proposals():
                scope, account = _bucket_scope(proposal.bucket)
                original = stored.get(proposal.proposal_id)
                base = {"scope": scope, "account_id": account, "account_label": _label(scope, account),
                        "subject": proposal.proposal_id, "since": proposal.created_at}
                if proposal.status == ProposalStatus.WAITING_FOR_APPROVAL:
                    detail = (f"${proposal.notional_usd:,.2f} in {proposal.bucket}"
                              + (" (real money)" if proposal.is_real_money else ""))
                    if original is not None and original.authority_changed_during_research:
                        detail += "; the authority changed while it was being researched"
                    out.append(Condition(
                        id=f"proposal_waiting:{proposal.proposal_id}:{proposal.version}",
                        kind="proposal_waiting",
                        title=f"{proposal.action} {proposal.ticker} needs your approval",
                        detail=detail, link=_link("approvals", proposal=proposal.proposal_id),
                        notify_kind="proposal_waiting", notify_link="/ui/approvals.html",
                        urgent=proposal.is_real_money, **base))
                if proposal.reconciliation_error:
                    out.append(Condition(
                        id=f"reconciliation_error:{proposal.proposal_id}", kind="reconciliation_error",
                        title=f"{proposal.ticker} order could not be reconciled",
                        detail=proposal.reconciliation_error[:300],
                        link=_link("activity", subject=proposal.proposal_id),
                        notify_kind="reconciliation_error", notify_link="/ui/activity.html",
                        urgent=True, **base))
                elif proposal.status == ProposalStatus.UNKNOWN:
                    out.append(Condition(
                        id=f"order_unknown:{proposal.proposal_id}", kind="order_unknown",
                        title=f"{proposal.ticker} order state is unknown",
                        detail=(f"venue status {proposal.order_status or 'not yet read'}; reconciliation "
                                "continues, do not resubmit"),
                        link=_link("activity", subject=proposal.proposal_id), **base))
            # An expired proposal is no longer open, so the queue above never lists it. It still
            # waits on a person to say what becomes of the idea, and a screen that hid it would
            # be showing "nothing to do" over an unanswered question.
            for proposal in self.proposals(include_closed=True):
                original = stored.get(proposal.proposal_id)
                if proposal.status != ProposalStatus.STALE or original is None or original.status != PROPOSED:
                    continue
                scope, account = _bucket_scope(proposal.bucket)
                out.append(Condition(
                    id=f"proposal_stale:{proposal.proposal_id}:{proposal.version}", kind="proposal_stale",
                    title=f"{proposal.action} {proposal.ticker} expired before a decision",
                    detail=f"{proposal.freshness.detail}; approval would be refused, request fresh research",
                    scope=scope, account_id=account, account_label=_label(scope, account),
                    subject=proposal.proposal_id, since=proposal.created_at,
                    link=_link("approvals", proposal=proposal.proposal_id)))

        def unfinished_exits() -> None:
            """Exits that stopped with shares still on the book and allowance still unspent.

            One item per chain, and the condition is ``exit.continuable.eligible`` itself rather
            than a second reading of the same rules. The endpoint, the button on the approvals
            page and this to-do therefore cannot disagree: when a continuation exists, or the
            allowance is spent, or the venue has not finished, all three go quiet together.
            """
            from .. import exits

            store = self._store(profiles)
            everything = store.all()
            seen: set[str] = set()
            for proposal in everything:
                if proposal.action not in exits.REDUCING or proposal.continuation_of:
                    continue
                if proposal.proposal_id in seen:
                    continue
                members = exits.members_of(proposal, everything)
                root = members[0]
                seen.update(m.proposal_id for m in members)
                if any(exits.unfinished(m) for m in members):
                    continue                      # still somebody's move, not an unfinished exit
                last = max([m.reconciled_at or m.executed_at or m.created_at for m in members])
                since = _parse(last)
                if since is None or (now - since).total_seconds() / 3600 > EXIT_INCOMPLETE_HOURS:
                    continue
                facts = self._exit_facts(profiles, store, root)
                chain = exits.chain_of(root, facts["everything"])
                onward = exits.continuable(
                    root, facts["everything"], sources=facts["sources"], chain=chain,
                    account_has_active_order=facts["account_has_active_order"],
                    research_expired=facts["research_expired"])
                if not onward["eligible"]:
                    continue
                scope, account = _bucket_scope(root.bucket)
                out.append(Condition(
                    id=f"exit_incomplete:{root.proposal_id}:{chain['filled']:g}",
                    kind="exit_incomplete",
                    title=f"{root.ticker} was only partly sold",
                    detail=(f"{chain['filled']:g} of the {chain['allowance']:g} authorised sold; "
                            f"{onward['quantity']:g} may still be taken up under the same "
                            "authorisation"),
                    scope=scope, account_id=account, account_label=_label(scope, account),
                    subject=root.proposal_id, since=last,
                    link=_link("approvals", proposal=root.proposal_id),
                    notify_kind="exit_incomplete", notify_link="/ui/approvals.html",
                    urgent=root.bucket == MICRO_BUCKET))

        def research() -> None:
            jobs = self.research_jobs(limit=50)
            snapshot = self.state_db(profiles).read_meta(SNAPSHOT, {}) or {}
            labels = {a.get("id"): a.get("label") for a in snapshot.get("accounts") or []}
            exhausted_at = self._research_manager(profiles).max_limit_recoveries
            for job in jobs:
                task = job.get("task") or {}
                task_kind = task.get("kind") if task else "full_research"
                personal = bool(task)
                if personal:
                    account = task.get("account_id")
                    base = {"scope": "personal", "account_id": account,
                            "account_scope_kind": "single" if account else "all_personal",
                            "account_label": labels.get(account) or ("all personal accounts" if account is None
                                                                     else account)}
                else:
                    scope, account = _bucket_scope(job["bucket"])
                    base = {"scope": scope, "account_id": account, "account_label": _label(scope, account)}
                base.update(subject=job["run_key"], since=job.get("finished_at") or job.get("created_at"))
                finished = _parse(job.get("finished_at"))
                age_hours = (now - finished).total_seconds() / 3600 if finished else None
                if job["state"] in ("FAILED", "TIMED_OUT"):
                    superseded = any(
                        other is not job and other["ticker"] == job["ticker"]
                        and other.get("bucket") == job.get("bucket")
                        and (other.get("task") or {}).get("kind") == task.get("kind")
                        and (other.get("task") or {}).get("account_id") == task.get("account_id")
                        and other["state"] in ("QUEUED", "RUNNING", "COMPLETE")
                        and other["created_at"] > job["created_at"] for other in jobs)
                    fresh = age_hours is not None and age_hours <= FAILED_HOURS and not superseded
                    if personal:
                        listed = fresh and task_kind in PERSONAL_REQUESTED and not job.get("opened_at")
                        kind = "personal_task_failed"
                    else:
                        listed, kind = fresh, "research_failed"
                    out.append(Condition(
                        id=f"research_failed:{job['run_key']}", kind=kind,
                        title=f"research on {job['ticker']} did not finish",
                        detail=str(job.get("error") or job["state"])[:300],
                        link=_link("research", run=job["run_key"]), attention=listed,
                        notify_kind="research_failed", notify_link="/ui/lab.html", **base))
                elif job["state"] == "CLAUDE_LIMIT_REACHED":
                    recoveries = int(job.get("limit_recoveries", 0))
                    out.append(Condition(
                        id=f"research_limit:{job['run_key']}:{recoveries}",
                        kind="research_limit_exhausted",
                        title="the Claude subscription limit stopped research",
                        detail=str(job.get("error") or "")[:300],
                        link=_link("research", run=job["run_key"]),
                        attention=recoveries >= exhausted_at,
                        notify_kind="research_limit", notify_link="/ui/lab.html", **base))
                elif (job["state"] == "COMPLETE" and personal and task_kind in PERSONAL_REQUESTED
                      and not job.get("opened_at") and age_hours is not None and age_hours <= DONE_HOURS):
                    out.append(Condition(
                        id=f"personal_task_done:{job['run_key']}", kind="personal_task_done",
                        title=f"review of {job['ticker']} finished",
                        detail=f"finished {job.get('finished_at')}; open it to see the result",
                        link=_link("research", run=job["run_key"]), **base))

        def venue() -> None:
            # A fill is not announced from here. Robinhood tells the account holder about every
            # trade in the Agentic account itself, so a message from this side would repeat it.
            # What only this side can know -- an order it cannot reconcile, a decision nobody was
            # asked about -- is announced elsewhere in this pass.
            if not self._micro_enabled(profiles):
                return
            connect_error = self._broker_connect_error()
            if connect_error:
                out.append(Condition(
                    id="credential_invalid:robinhood", kind="credential_invalid",
                    title="the broker connection needs attention",
                    # The error text can name an endpoint but never a token.
                    detail=connect_error[:200], scope="agentic", account_id=MICRO_BUCKET,
                    account_label=_label("agentic", MICRO_BUCKET), link=_link("system"),
                    notify_kind="credential_invalid", notify_link="/ui/system.html", urgent=True))

        def ladders() -> None:
            for name, ladder in self.risk_state().items():
                # risk_state also carries the market regime, which has a "level" too. Read as
                # a ladder it produced "the regime drawdown ladder is at ELEVATED_RISK" and
                # repeated it every quiet period for as long as the market stayed there.
                if name not in LADDER_NAMES or not isinstance(ladder, dict) or "level" not in ladder:
                    continue
                if ladder["level"] == "NORMAL":
                    continue
                halted = bool(ladder.get("manual_reset_required", False))
                scope, account = _bucket_scope(name if name != "total" else "core")
                out.append(Condition(
                    id=f"risk_level_changed:{name}:{ladder['level']}",
                    kind="risk_halt" if halted else "risk_level_changed",
                    title=f"the {name} drawdown ladder is at {ladder['level']}",
                    detail=f"drawdown {ladder.get('drawdown_pct', 0):.2f}% from the high-water mark",
                    scope=scope, account_id=account, account_label=_label(scope, account),
                    subject=name, link=_link("risk", scope=scope), attention=halted,
                    notify_kind="risk_level_changed", notify_link="/ui/risk.html", urgent=halted))

        def ledgers() -> None:
            # The last ledger check, whoever ran it. A refusal is the venue and a book
            # disagreeing, which is what reconciliation_error already means; like any standing
            # condition it is repeated each quiet period until a check agrees.
            report = self.state_db(profiles).read_meta(REPORT, {}) or {}
            refused = [(row["ledger"], row["error"]) for row in report.get("ledgers") or []
                       if isinstance(row, dict) and row.get("error")]
            if report.get("error"):
                refused.append(("ledgers", report["error"]))
            for ledger, error in refused:
                scope, account = (_bucket_scope(ledger) if ledger in (MICRO_BUCKET, "conceptual", "core")
                                  else ("system", None))
                out.append(Condition(
                    id=f"ledger_unconfirmed:{ledger}:{report.get('date', '')}", kind="ledger_unconfirmed",
                    title=f"the {ledger} ledger could not be confirmed", detail=str(error)[:300],
                    scope=scope, account_id=account, account_label=_label(scope, account),
                    subject=ledger, since=report.get("at"),
                    link=_link("portfolio", scope=scope if scope != "system" else "agentic"),
                    notify_kind="reconciliation_error", notify_link="/ui/portfolio.html#ledger-notice"))

        def operations() -> None:
            for activity in self.operations()["activities"]:
                # `state`, not the `overdue` boolean. An activity that has not come due yet is
                # nobody's to-do, and one that ran and refused already raises a critical item of
                # its own from its report: two items for one fact is how a person learns to
                # ignore both.
                if activity["state"] == "overdue" and activity["id"] in ("backup", "maintenance",
                                                                         "ledger_confirm"):
                    out.append(Condition(
                        id=f"backup_overdue:{activity['id']}:{now.date()}", kind="backup_overdue",
                        title=f"{activity['id'].replace('_', ' ')} has not run when expected",
                        detail=f"last success {activity['last_success_at'] or 'never'}",
                        subject=activity["id"], since=activity["last_success_at"], link=_link("system"),
                        notify_kind="backup_overdue", notify_link="/ui/system.html",
                        urgent=activity["id"] == "backup"))

        def withdrawn() -> None:
            # Autonomy is the case where nobody was asked, so being told is the only visibility
            # there is. Read from state like everything else here, so a restart re-notices
            # rather than forgetting what happened while the process was down.
            state = autonomy.load_downgrade(profiles)
            if state.tripped:
                out.append(Condition(
                    id=f"autonomy_withdrawn:{state.at}", kind="autonomy_withdrawn",
                    title="autonomous reductions have been withdrawn",
                    detail=f"{state.reason}; they stay off until a manual reset",
                    scope="agentic", account_id=MICRO_BUCKET, account_label=_label("agentic", MICRO_BUCKET),
                    since=state.at, link=_link("system"),
                    notify_kind="autonomy_withdrawn", notify_link="/ui/system.html", urgent=True))

        def changes() -> None:
            # A change made from one device is announced to the others while it can still be
            # withdrawn, and again once it is in force. Derived from the record, like the rest.
            for change in cc.records(self.state_db(profiles)):
                if change["status"] == cc.PENDING:
                    out.append(Condition(
                        id=f"config_change_pending:{change['id']}", kind="config_change_pending",
                        title=f"a loosening change to {change['file']} applies at "
                              f"{str(change.get('apply_after') or '')[:16].replace('T', ' ')} UTC",
                        detail="; ".join(change.get("reasons") or change.get("paths") or [])[:300]
                               + f" — requested by {change['requested_by']}; withdraw it from Settings",
                        subject=change["id"], since=change.get("requested_at"),
                        link=_link("settings", change=change["id"]),
                        notify_kind="config_change_pending", notify_link="/ui/settings.html", urgent=True))
                elif change["status"] in (cc.APPLIED, cc.FAILED) and change["classification"] == cc.COOLING:
                    applied = _parse(change.get("applied_at"))
                    recent = applied is not None and (now - applied).total_seconds() / 3600 <= FAILED_HOURS
                    out.append(Condition(
                        id=f"config_change_applied:{change['id']}:{change['status']}",
                        kind="config_change_failed" if change["status"] == cc.FAILED else "config_change_applied",
                        title=f"a change to {change['file']} "
                              + ("is now in force" if change["status"] == cc.APPLIED else "could not be applied"),
                        detail=(change.get("error") or "; ".join(change.get("paths") or []))[:300],
                        subject=change["id"], since=change.get("applied_at"),
                        link=_link("settings", change=change["id"]),
                        attention=change["status"] == cc.FAILED and recent,
                        notify_kind="config_change_applied", notify_link="/ui/settings.html"))
                elif (change["status"] == cc.APPLIED and change["classification"] == cc.CONFIRMED
                      and (change.get("switch") or {}).get("on") is True):
                    # An autonomy switch turned on applies at once, so there is no cooling period
                    # in which the other devices would hear of it: they hear now, urgently. Told
                    # for a day; after that it is Settings' recent changes, not news.
                    applied = _parse(change.get("applied_at"))
                    if applied is None or (now - applied).total_seconds() / 3600 > DONE_HOURS:
                        continue
                    what = "selling" if change["switch"].get("block") == "reduce" else "buying"
                    out.append(Condition(
                        id=f"autonomy_switched_on:{change['id']}", kind="autonomy_switched_on",
                        title=f"autonomous {what} was switched on",
                        detail=f"by {change['requested_by']}, in force at once; switch it off on "
                               "the Agentic account in Assets",
                        scope="agentic", account_id=MICRO_BUCKET, account_label=_label("agentic", MICRO_BUCKET),
                        subject=change["id"], since=change.get("applied_at"),
                        link=_link("portfolio", scope="agentic"), attention=False,
                        notify_kind="config_change_applied",
                        notify_link="/ui/portfolio.html?scope=agentic", urgent=True))

        def acted() -> None:
            for proposal in stored.values():
                if not autonomy.was_autonomous(proposal):
                    continue
                out.append(Condition(
                    # Keyed on the status too, so the outcome arrives as well as the decision.
                    id=f"autonomy_acted:{proposal.proposal_id}:{proposal.status}", kind="autonomy_acted",
                    title=f"{proposal.action} {proposal.ticker} was decided without asking you",
                    detail=(f"{proposal.bucket}, status {proposal.status}"
                            + (f", filled {proposal.filled_quantity:g}" if proposal.filled_quantity else "")),
                    scope="agentic", account_id=MICRO_BUCKET, account_label=_label("agentic", MICRO_BUCKET),
                    subject=proposal.proposal_id, link=_link("activity", subject=proposal.proposal_id),
                    attention=False, notify_kind="autonomy_acted", notify_link="/ui/activity.html",
                    urgent=proposal.bucket == MICRO_BUCKET))

        for source, collector in (("proposals", proposals), ("exits", unfinished_exits),
                                  ("research", research), ("venue", venue),
                                  ("ladders", ladders), ("ledger_report", ledgers),
                                  ("operations", operations), ("autonomy", withdrawn),
                                  ("config_changes", changes), ("autonomy_acted", acted)):
            read(source, collector)
        return out, unreadable

    def collect_notifications(self) -> list[Any]:
        """Everything a person would want to be told, read from current state."""
        conditions, _unreadable = self.collect_conditions()
        return [c.event() for c in conditions if c.notify_kind]

    def attention(self, now: datetime | None = None) -> dict[str, Any]:
        """What still needs a person, in the order it needs them, from unresolved state only.

        Not from the delivery record (whether something was sent is a different question) and
        not from the journal (history is Activity's). An item leaves the list when the state
        behind it resolves, and a source that could not be read makes the whole answer
        ``readable: false`` so a screen shows "cannot confirm" rather than "nothing to do".
        """
        from .. import schedule as scheduling

        profiles = self.profiles()
        now = now or utcnow()
        conditions, unreadable = self.collect_conditions(now)
        items = sorted((c.item() for c in conditions if c.attention),
                       key=lambda i: (i["priority"], _SEVERITY_RANK[i["severity"]], i["since"] or ""))
        by_severity = {level: 0 for level in _SEVERITY_RANK}
        for item in items:
            by_severity[item["severity"]] += 1
        context: dict[str, Any] = {"market_open": None, "paused": None, "autonomy_armed": None,
                                   "autonomous_queue": None, "next_scheduled_at": None}
        try:
            context["market_open"] = self.market_status().is_open
            context["paused"] = bool(autonomy.kill_switch_state(profiles)["engaged"])
            context["autonomy_armed"] = autonomy.is_armed(profiles, MICRO_BUCKET)
            context["autonomous_queue"] = sum(
                1 for p in self._store(profiles).all()
                if p.status == PROPOSED and p.bucket == MICRO_BUCKET and not p.requires_human_approval)
            context["next_scheduled_at"] = scheduling.next_run_at(scheduling.settings(profiles), now)
        except Exception as exc:  # noqa: BLE001 - context is decoration; the items are the answer
            unreadable.append(f"context: {type(exc).__name__}: {exc}"[:200])
        return {
            "checked_at": now.isoformat(),
            "readable": not unreadable,
            "unreadable": unreadable,
            "top": items[0] if items else None,
            "items": items,
            "summary": {"total": len(items),
                        "requires_person": sum(1 for i in items if i["requires_person"]),
                        "by_severity": by_severity},
            "context": context,
        }

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
