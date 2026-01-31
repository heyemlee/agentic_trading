"""``ct-notify``: inspect the notification channel, and send deliberately.

Configuring a channel is what authorises this system to send messages, so the commands here are
explicit about it: ``status`` never sends, ``test`` sends exactly one message and says where it
went, and ``dispatch`` performs the same pass the API does in the background.
"""

from __future__ import annotations

import argparse
import json
import sys

from .env import load_env
from .notify import DeliveryError, Event, send
from .profile import load_profiles
from .service import Service, ServiceError


def main(argv: list[str] | None = None) -> int:
    load_env()
    parser = argparse.ArgumentParser(
        prog="ct-notify",
        description="Inspect the notification channel and send a deliberate test message.",
        epilog="Nothing is sent while notifications.channel is 'none'. Set a channel and its "
               "credentials in the service environment to authorise sending.")
    parser.add_argument("action", choices=("status", "test", "dispatch", "heartbeat", "pending"))
    parser.add_argument("--config-dir", default="config")
    args = parser.parse_args(argv)

    service = Service(args.config_dir)
    try:
        profiles = load_profiles(args.config_dir)
    except Exception as exc:  # noqa: BLE001 - a configuration error is the answer here
        print(json.dumps({"error": str(exc)}, indent=2))
        return 2
    settings = service.notification_settings(profiles)

    if args.action == "status":
        print(json.dumps(service.notification_status(), indent=2))
        return 0
    if args.action == "pending":
        print(json.dumps([{k: v for k, v in event.__dict__.items()}
                          for event in service.collect_notifications()], indent=2))
        return 0
    if args.action == "dispatch":
        print(json.dumps(service.dispatch_notifications(), indent=2))
        return 0
    if args.action == "heartbeat":
        # Through the service, so that running this by hand answers the same question the
        # scheduled loop answers. A command that pinged unconditionally would report a healthy
        # watcher for a deployment the loop had already, correctly, gone quiet about.
        try:
            print(json.dumps(service.send_heartbeat(), indent=2))
        except DeliveryError as exc:
            print(json.dumps({"sent": False, "error": str(exc)}, indent=2))
            return 1
        return 0

    if not settings.configured():
        print(json.dumps({"sent": False, "reason": settings.why_not_configured()}, indent=2))
        return 1
    destination = ("the local file " + settings.file_path if settings.channel == "file"
                   else f"the endpoint in ${settings.webhook_url_env}")
    try:
        send(settings, Event(key="test", kind="proposal_waiting",
                             title="test message from the trading console",
                             detail="If you can read this, notifications reach you.",
                             link="/ui/index.html"))
    except (DeliveryError, ServiceError) as exc:
        print(json.dumps({"sent": False, "channel": settings.channel, "error": str(exc)}, indent=2))
        return 1
    print(json.dumps({"sent": True, "channel": settings.channel, "to": destination}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
