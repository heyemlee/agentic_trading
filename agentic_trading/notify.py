"""Notifications for the events a person would want to know about without opening the console.

Two rules shape this module.

The first is that it sends nothing until a channel is configured. The default channel is
``none``: events are still collected and recorded, so the behaviour can be inspected and the
delivery record reviewed, but nothing leaves the machine. Choosing a channel and supplying its
credentials is how a person authorises sending, and that decision is not made in code.

The second is that a notification carries no credential and no position detail beyond what the
console already shows. A message travels through somebody else's infrastructure, so it says
what happened and where to look, never the account, the token or the quote.

Delivery is deduplicated by event key and retried with backoff, and every attempt is recorded
durably. A condition that persists — an unapproved proposal, a broken credential — notifies once
per quiet period rather than on every poll.
"""

from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

RECORD = "notifications"
MAX_RECORDS = 200
TIMEOUT_SECONDS = 10

#: Every event this system can raise, and how urgent it is for a person. The set is closed:
#: an unknown key is a bug, not a new notification.
EVENTS = {
    "proposal_waiting": "a proposal is waiting for your approval",
    "research_failed": "a research run failed",
    "research_limit": "the Claude subscription limit stopped research",
    "fill_recorded": "an order filled",
    "reconciliation_error": "an order could not be reconciled with the broker",
    "credential_invalid": "a stored credential no longer works",
    "risk_level_changed": "the drawdown ladder changed level",
    "backup_overdue": "a background activity has not run when expected",
    # Autonomy is the case where nobody was asked. Being told is then the only visibility there
    # is, which is why these two are not optional in the way the others are.
    "autonomy_acted": "an order was placed without asking you",
    "autonomy_withdrawn": "autonomous action was withdrawn and needs a manual reset",
}


@dataclass
class Event:
    """One thing worth telling a person about, identified so it is sent once."""

    key: str
    kind: str
    title: str
    detail: str = ""
    link: str = ""
    urgent: bool = False

    def payload(self) -> dict[str, Any]:
        return {"key": self.key, "kind": self.kind, "title": self.title,
                "detail": self.detail, "link": self.link, "urgent": self.urgent,
                "at": datetime.now(UTC).isoformat()}


@dataclass
class Settings:
    channel: str = "none"
    file_path: str = "runs/notifications.jsonl"
    webhook_url_env: str = "CT_NOTIFY_WEBHOOK"
    webhook_token_env: str = "CT_NOTIFY_TOKEN"
    wecom_url_env: str = "CT_WECOM_WEBHOOK"
    wechat_url_env: str = "CT_WECHAT_WEBHOOK"
    console_url: str = ""
    state_dir: Path = field(default_factory=Path)
    #: Set by the service so a webpush channel can see whether a device is subscribed.
    _db: Any = None
    events: tuple[str, ...] = tuple(EVENTS)
    min_interval_seconds: int = 900
    max_attempts: int = 5
    retry_backoff_seconds: int = 60
    heartbeat_url_env: str = "CT_HEARTBEAT_URL"
    heartbeat_every_minutes: int = 15
    #: Activities that run inside the service process. While one of them is late the
    #: deployment is degraded even though the process answers, so the ping is withheld.
    heartbeat_requires: tuple[str, ...] = ("reconciliation", "research_dispatch")
    project_dir: Path = field(default_factory=Path)

    @classmethod
    def load(cls, profiles) -> Settings:
        cfg = dict(profiles.risk.get("notifications") or {})
        heartbeat = dict(cfg.get("heartbeat") or {})
        events = tuple(cfg.get("events") or EVENTS)
        return cls(
            channel=str(cfg.get("channel", "none")),
            file_path=str(cfg.get("file_path", "runs/notifications.jsonl")),
            webhook_url_env=str(cfg.get("webhook_url_env", "CT_NOTIFY_WEBHOOK")),
            webhook_token_env=str(cfg.get("webhook_token_env", "CT_NOTIFY_TOKEN")),
            wecom_url_env=str(cfg.get("wecom_url_env", "CT_WECOM_WEBHOOK")),
            wechat_url_env=str(cfg.get("wechat_url_env", "CT_WECHAT_WEBHOOK")),
            console_url=str(cfg.get("console_url", "")).rstrip("/"),
            state_dir=Path(profiles.project_dir) / "state",
            events=tuple(name for name in events if name in EVENTS),
            min_interval_seconds=int(cfg.get("min_interval_seconds", 900)),
            max_attempts=int(cfg.get("max_attempts", 5)),
            retry_backoff_seconds=int(cfg.get("retry_backoff_seconds", 60)),
            heartbeat_url_env=str(heartbeat.get("url_env", "CT_HEARTBEAT_URL")),
            heartbeat_every_minutes=int(heartbeat.get("every_minutes", 15)),
            heartbeat_requires=tuple(heartbeat.get("requires_healthy")
                                     or ("reconciliation", "research_dispatch")),
            project_dir=Path(profiles.project_dir),
        )

    def configured(self) -> bool:
        if self.channel == "file":
            return True
        if self.channel == "webhook":
            return bool(os.environ.get(self.webhook_url_env, "").strip())
        if self.channel == "wecom":
            return bool(os.environ.get(self.wecom_url_env, "").strip())
        if self.channel == "wechat":
            return bool(os.environ.get(self.wechat_url_env, "").strip())
        if self.channel == "webpush":
            # Configured means a device has actually subscribed. Keys alone reach nobody.
            from . import webpush

            return bool(webpush.devices(self._db) if self._db is not None else False)
        return False

    def why_not_configured(self) -> str:
        if self.channel == "none":
            return ("no channel is configured, so nothing is sent. Set notifications.channel and "
                    "supply its credentials to authorise sending.")
        if self.channel == "webhook":
            return (f"channel is webhook but {self.webhook_url_env} is not set in the service "
                    "environment, so nothing is sent.")
        if self.channel == "wecom":
            return (f"channel is wecom but {self.wecom_url_env} is not set in the service "
                    "environment, so nothing is sent.")
        if self.channel == "wechat":
            return (f"channel is wechat but {self.wechat_url_env} is not set in the service "
                    "environment, so nothing is sent.")
        if self.channel == "webpush":
            return ("channel is webpush but no device has subscribed. Open the console from its "
                    "home-screen icon and allow notifications; a Safari tab cannot receive them.")
        return f"unknown notification channel {self.channel!r}"


class DeliveryError(RuntimeError):
    """The channel refused or could not be reached; the event stays pending."""


def _https(url: str) -> str:
    if not url.startswith("https://"):
        raise DeliveryError("a notification endpoint must be https")
    return url


def _post(url: str, body: dict[str, Any], token: str | None) -> dict[str, Any]:
    """POST JSON and return the parsed reply.

    The reply matters: WeCom answers a rejected message with HTTP 200 and an error code in the
    body, so a delivery that never reaches anyone would otherwise be recorded as sent.
    """
    request = urllib.request.Request(      # https-only: _https rejects anything else
        _https(url), data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "agentic-trading/1"})
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS,
                                    context=ssl.create_default_context()) as response:
            if response.status >= 400:
                raise DeliveryError(f"endpoint returned {response.status}")
            raw = response.read(8192)
    except urllib.error.HTTPError as exc:
        raise DeliveryError(f"endpoint returned {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        # Deliberately not including the URL: it may embed a routing secret.
        raise DeliveryError(f"endpoint unreachable ({type(exc).__name__})") from exc
    try:
        parsed = json.loads(raw or b"{}")
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def wecom_message(settings: Settings, event: Event) -> dict[str, Any]:
    """One event as a WeCom group-bot message.

    Written for a person glancing at a phone: what happened, one line of detail, and a link
    that opens the right screen. The link is only included when a console URL is configured,
    because a relative path is not tappable and a wrong absolute one is worse than none.
    """
    lines = [f"**{event.title}**"]
    if event.detail:
        lines.append(event.detail)
    if settings.console_url and event.link:
        lines.append(f"[打开控制台]({settings.console_url}{event.link})")
    return {"msgtype": "markdown", "markdown": {"content": "\n".join(lines)}}


#: Server酱 truncates a longer title itself, but silently. Doing it here keeps what a person
#: sees on the lock screen under our control rather than the relay's.
WECHAT_TITLE_CHARS = 32


def wechat_message(settings: Settings, event: Event) -> dict[str, Any]:
    """One event as a Server酱 message, which forwards into personal WeChat.

    Personal WeChat has no interface a program may send to; every route into it goes through
    somebody's verified service account. That relay therefore reads whatever is sent, which is
    why these messages carry what happened and where to look, and never a credential, an
    account number or a quote.
    """
    title = event.title[:WECHAT_TITLE_CHARS]
    lines = []
    if len(event.title) > WECHAT_TITLE_CHARS:
        lines.append(event.title)          # the full title survives in the body
    if event.detail:
        lines.append(event.detail)
    if settings.console_url and event.link:
        lines.append(f"[打开控制台]({settings.console_url}{event.link})")
    return {"title": title, "desp": "\n\n".join(lines)}


def send(settings: Settings, event: Event) -> None:
    """Deliver one event through the configured channel, or raise DeliveryError."""
    if not settings.configured():
        raise DeliveryError(settings.why_not_configured())
    if settings.channel == "file":
        path = settings.project_dir / settings.file_path
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event.payload()) + "\n")
        path.chmod(0o600)
        return
    if settings.channel == "wecom":
        reply = _post(os.environ[settings.wecom_url_env].strip(),
                      wecom_message(settings, event), None)
        # WeCom rejects a bad key, a revoked bot or a rate-limited burst with HTTP 200 and an
        # error code in the body. Treating that as delivered would record a message nobody got.
        if int(reply.get("errcode", 0) or 0) != 0:
            raise DeliveryError(
                f"WeCom refused the message (errcode {reply.get('errcode')}: "
                f"{reply.get('errmsg', 'no reason given')})")
        return
    if settings.channel == "webpush":
        from . import webpush

        webpush.deliver(settings._db, settings.state_dir, event.payload())
        return
    if settings.channel == "wechat":
        reply = _post(os.environ[settings.wechat_url_env].strip(),
                      wechat_message(settings, event), None)
        # Server酱 answers a spent quota or a revoked key with HTTP 200 and a non-zero code.
        if int(reply.get("code", 0) or 0) != 0:
            raise DeliveryError(
                f"the WeChat relay refused the message (code {reply.get('code')}: "
                f"{reply.get('message') or 'no reason given'})")
        return
    _post(os.environ[settings.webhook_url_env].strip(), event.payload(),
          os.environ.get(settings.webhook_token_env, "").strip() or None)


def heartbeat(settings: Settings, *, withhold: list[str] | None = None) -> dict[str, Any]:
    """Tell an external watcher this deployment is still doing its work.

    Nothing running on this Mac can report that this Mac is gone. A watcher elsewhere that
    expects a regular ping is the only way an outage reaches a phone, so the payload is
    deliberately contentless: its arrival is the whole message.

    Which is why the ping is withheld rather than annotated when the deployment is degraded.
    A contentless ping sent unconditionally only ever covered lost power: on 2026-09-15 this
    process stayed up and answered on loopback for twenty minutes while it could not open its
    own state database, and an unconditional heartbeat would have called that healthy
    throughout. Staying silent puts the degraded case under the alarm that already exists.
    """
    url = os.environ.get(settings.heartbeat_url_env, "").strip()
    if not url:
        return {"sent": False, "reason": f"{settings.heartbeat_url_env} is not set"}
    if withhold:
        return {"sent": False, "reason": "withheld: " + "; ".join(withhold),
                "withheld_by": list(withhold)}
    _post(url, {"at": datetime.now(UTC).isoformat(), "source": "agentic-trading"}, None)
    return {"sent": True, "at": datetime.now(UTC).isoformat()}


def dispatch(db, settings: Settings, events: list[Event], *,
             now: datetime | None = None) -> dict[str, Any]:
    """Send what is due, honouring the quiet period, the retry budget and the event filter."""
    now = now or datetime.now(UTC)
    record = db.read_meta(RECORD, {}) or {}
    entries = dict(record.get("events") or {})
    sent, held, failed = [], [], []
    # This runs on the same fifteen-second loop as reconciliation, and a quiet system is the
    # normal case, so a pass that neither sent nor learned anything must not write to disk.
    changed = False

    for event in events:
        if event.kind not in settings.events:
            continue
        entry = dict(entries.get(event.key) or {})
        if not entry:
            changed = True
        entry.setdefault("first_seen_at", now.isoformat())
        entry.update({"kind": event.kind, "title": event.title, "detail": event.detail,
                      "last_seen_at": now.isoformat()})
        if not _due(entry, settings, now):
            held.append(event.key)
            entries[event.key] = entry
            continue
        try:
            send(settings, event)
        except DeliveryError as exc:
            entry["attempts"] = int(entry.get("attempts", 0)) + 1
            entry["state"] = "PENDING" if entry["attempts"] < settings.max_attempts else "GAVE_UP"
            entry["error"] = str(exc)
            entry["next_attempt_at"] = (
                now + timedelta(seconds=settings.retry_backoff_seconds * entry["attempts"])
            ).isoformat()
            failed.append({"key": event.key, "error": str(exc)})
            changed = True
        else:
            entry.update({"state": "SENT", "sent_at": now.isoformat(), "error": None,
                          "attempts": int(entry.get("attempts", 0)) + 1,
                          "next_attempt_at": None})
            sent.append(event.key)
            changed = True
        entries[event.key] = entry

    if changed:
        record["events"] = _trim(entries)
        record["last_dispatch_at"] = now.isoformat()
        db.write_meta(RECORD, record)
    return {"sent": sent, "held": held, "failed": failed, "recorded": changed,
            "channel": settings.channel, "configured": settings.configured()}


def _due(entry: dict[str, Any], settings: Settings, now: datetime) -> bool:
    state = entry.get("state")
    if state == "GAVE_UP":
        return False
    if state == "SENT":
        sent_at = _parse(entry.get("sent_at"))
        # A condition that persists is worth repeating, but not on every poll.
        return sent_at is None or (now - sent_at).total_seconds() >= settings.min_interval_seconds
    scheduled = _parse(entry.get("next_attempt_at"))
    return scheduled is None or now >= scheduled


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)


def _trim(entries: dict[str, Any]) -> dict[str, Any]:
    if len(entries) <= MAX_RECORDS:
        return entries
    ordered = sorted(entries.items(), key=lambda item: item[1].get("last_seen_at") or "")
    return dict(ordered[-MAX_RECORDS:])


def pending(db) -> dict[str, Any]:
    record = db.read_meta(RECORD, {}) or {}
    return {"last_dispatch_at": record.get("last_dispatch_at"),
            "events": record.get("events") or {}}
