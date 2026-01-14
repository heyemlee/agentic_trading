"""Web push to the console's own home-screen app, with nobody in between able to read it.

Every other way of reaching a phone from here routes the message through somebody else's
service: a relay account for WeChat, a bot for WeCom, a vendor for a generic webhook. Each of
them reads what passes through. This one does not. The browser generates a key pair when it
subscribes, the payload is encrypted to that public key, and Apple's push service carries a
sealed envelope it cannot open.

The private half of the VAPID identity never leaves this machine. A subscription endpoint is a
capability — anyone holding it can push to that device — so subscriptions live in the state
database at 0600 alongside everything else that must not leak.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SUBSCRIPTIONS = "push_subscriptions"
KEY_FILE = "vapid_key.json"
#: Apple requires a contact for the pushing party. It is sent to the push service, not to the
#: device, and identifies the sender if a subscription misbehaves.
DEFAULT_SUBJECT = "mailto:owner@localhost"
MAX_SUBSCRIPTIONS = 16


class PushError(RuntimeError):
    """The message could not be encrypted, signed or delivered."""


def _b64(raw: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def ensure_keys(state_dir: Path) -> dict[str, str]:
    """The VAPID identity for this deployment, created once and then reused.

    Rotating it silently would orphan every device already subscribed, so the pair is generated
    only when absent and the file is the single place it lives.
    """
    from cryptography.hazmat.primitives.asymmetric import ec

    path = Path(state_dir) / KEY_FILE
    if path.exists():
        try:
            stored = json.loads(path.read_text())
            if stored.get("private") and stored.get("public"):
                return stored
        except (OSError, ValueError) as exc:
            raise PushError(f"the VAPID key file at {path} is unreadable: {exc}") from exc

    private = ec.generate_private_key(ec.SECP256R1())
    numbers = private.private_numbers()
    public = private.public_key().public_numbers()
    keys = {
        "private": _b64(numbers.private_value.to_bytes(32, "big")),
        "public": _b64(b"\x04" + public.x.to_bytes(32, "big") + public.y.to_bytes(32, "big")),
        "created_at": datetime.now(UTC).isoformat(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(keys, indent=2))
    path.chmod(0o600)
    return keys


def public_key(state_dir: Path) -> str:
    return ensure_keys(state_dir)["public"]


def _endpoint_id(subscription: dict[str, Any]) -> str:
    """A stable, short identity for one device's subscription.

    The endpoint itself is a capability URL, so it is hashed for use as a key rather than
    echoed back through the interface.
    """
    import hashlib

    return hashlib.sha256(str(subscription.get("endpoint", "")).encode()).hexdigest()[:16]


def valid(subscription: Any) -> bool:
    keys = (subscription or {}).get("keys") if isinstance(subscription, dict) else None
    return bool(
        isinstance(subscription, dict)
        and str(subscription.get("endpoint", "")).startswith("https://")
        and isinstance(keys, dict) and keys.get("p256dh") and keys.get("auth"))


def subscribe(db, subscription: dict[str, Any], *, label: str = "") -> dict[str, Any]:
    """Record a device's subscription. Replacing the same endpoint rather than duplicating it."""
    if not valid(subscription):
        raise PushError("a push subscription needs an https endpoint and p256dh/auth keys")
    record = db.read_meta(SUBSCRIPTIONS, {}) or {}
    devices = dict(record.get("devices") or {})
    key = _endpoint_id(subscription)
    devices[key] = {
        "subscription": subscription,
        "label": label or "this device",
        "created_at": devices.get(key, {}).get("created_at") or datetime.now(UTC).isoformat(),
        "last_sent_at": devices.get(key, {}).get("last_sent_at"),
        "failures": 0,
    }
    if len(devices) > MAX_SUBSCRIPTIONS:
        oldest = sorted(devices, key=lambda k: devices[k]["created_at"])[:-MAX_SUBSCRIPTIONS]
        for stale in oldest:
            devices.pop(stale, None)
    record["devices"] = devices
    db.write_meta(SUBSCRIPTIONS, record)
    return {"device_id": key, "devices": len(devices)}


def unsubscribe(db, device_id: str) -> bool:
    record = db.read_meta(SUBSCRIPTIONS, {}) or {}
    devices = dict(record.get("devices") or {})
    if device_id not in devices:
        return False
    devices.pop(device_id)
    record["devices"] = devices
    db.write_meta(SUBSCRIPTIONS, record)
    return True


def devices(db) -> list[dict[str, Any]]:
    """What is subscribed, without handing back any endpoint."""
    record = db.read_meta(SUBSCRIPTIONS, {}) or {}
    return [{"device_id": key, "label": item.get("label") or "this device",
             "created_at": item.get("created_at"), "last_sent_at": item.get("last_sent_at"),
             "failures": int(item.get("failures", 0))}
            for key, item in sorted((record.get("devices") or {}).items(),
                                    key=lambda pair: pair[1].get("created_at") or "")]


def deliver(db, state_dir: Path, payload: dict[str, Any], *,
            subject: str = DEFAULT_SUBJECT) -> dict[str, Any]:
    """Send one payload to every subscribed device.

    A device that the push service reports as gone is removed: a subscription is dropped when
    the app is deleted or the browser data cleared, and retrying it forever would turn a device
    that no longer exists into a permanent failure in the delivery record.
    """
    from pywebpush import WebPushException, webpush

    keys = ensure_keys(state_dir)
    record = db.read_meta(SUBSCRIPTIONS, {}) or {}
    stored = dict(record.get("devices") or {})
    if not stored:
        raise PushError("no device has subscribed to push notifications yet")

    sent, failed, removed = [], [], []
    for key, item in list(stored.items()):
        try:
            webpush(subscription_info=item["subscription"], data=json.dumps(payload),
                    vapid_private_key=keys["private"],
                    vapid_claims={"sub": subject}, timeout=10)
        except WebPushException as exc:
            status = getattr(exc.response, "status_code", None)
            if status in (404, 410):
                stored.pop(key, None)
                removed.append(key)
            else:
                item["failures"] = int(item.get("failures", 0)) + 1
                failed.append({"device_id": key, "status": status})
        except Exception as exc:  # noqa: BLE001 - one unreachable device must not lose the rest
            item["failures"] = int(item.get("failures", 0)) + 1
            failed.append({"device_id": key, "error": type(exc).__name__})
        else:
            item["failures"] = 0
            item["last_sent_at"] = datetime.now(UTC).isoformat()
            sent.append(key)

    record["devices"] = stored
    db.write_meta(SUBSCRIPTIONS, record)
    if not sent and failed:
        raise PushError("no device accepted the notification: "
                        + ", ".join(str(item.get("status") or item.get("error"))
                                    for item in failed))
    return {"sent": sent, "failed": failed, "removed": removed}
