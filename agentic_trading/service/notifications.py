"""Which events are worth telling somebody about, and the devices they are told through."""

from __future__ import annotations

import os
from typing import Any

from .. import autonomy
from ..micro_live import BUCKET as MICRO_BUCKET
from ..profile import Profiles
from ..proposals import utcnow
from .errors import ServiceError
from .status import ProposalStatus

#: The drawdown ladders `risk_state` reports, as opposed to the other entries beside them.
LADDER_NAMES = ("total", "core", "sandbox", "micro_live")


class NotificationsMixin:
    """Collecting notifications, dispatching them, and the push devices they reach."""

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
