"""Push to the console's own app: what it keeps private, and what it refuses to cache.

No network. Delivery itself is exercised against a stubbed sender, because what can be checked
here is the handling around it — which endpoints are stored, what is handed back, and which
failures remove a device rather than retrying it forever.
"""
from __future__ import annotations

import json
import re
import shutil

import pytest

from agentic_trading import webpush
from agentic_trading.service import Service
from tests.conftest import ROOT, copy_example_ledger

ENDPOINT = "https://web.push.apple.com/QABC123-secret-capability-url"
SUBSCRIPTION = {"endpoint": ENDPOINT,
                "keys": {"p256dh": "BNc" + "x" * 84, "auth": "y" * 22}}


@pytest.fixture
def service(tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    shutil.copytree(ROOT / "config", cfg)
    (tmp_path / "state").mkdir()
    (tmp_path / "runs").mkdir()
    copy_example_ledger("portfolio.example.yaml", tmp_path / "state" / "portfolio.yaml")
    # A deployment names itself to the push service; the tests need one that passes the same
    # check a real send makes, and it must not be the placeholder Apple refuses.
    monkeypatch.setenv(webpush.SUBJECT_ENV, "https://console.example.com")
    return Service(cfg)


def test_the_signing_key_stays_on_this_machine_and_is_never_regenerated(service, tmp_path):
    """Rotating the identity would orphan every device already subscribed."""
    state = tmp_path / "state"
    first = webpush.ensure_keys(state)
    assert webpush.ensure_keys(state)["public"] == first["public"]
    assert (state / webpush.KEY_FILE).stat().st_mode & 0o077 == 0

    # only the public half is ever handed out
    published = service.push_key()
    assert published["public_key"] == first["public"]
    assert first["private"] not in json.dumps(published)


def test_a_subscription_endpoint_is_never_handed_back_out(service):
    """The endpoint is a capability: whoever holds it can push to that device."""
    service.push_subscribe(SUBSCRIPTION, "iPhone")
    listed = json.dumps(service.push_devices())

    assert ENDPOINT not in listed
    assert "secret-capability-url" not in listed
    assert SUBSCRIPTION["keys"]["auth"] not in listed
    assert "iPhone" in listed          # the label is all a person needs to tell devices apart


def test_the_same_device_resubscribing_replaces_rather_than_duplicates(service):
    service.push_subscribe(SUBSCRIPTION, "iPhone")
    service.push_subscribe(SUBSCRIPTION, "iPhone renamed")
    devices = service.push_devices()["devices"]

    assert len(devices) == 1 and devices[0]["label"] == "iPhone renamed"


def test_a_malformed_subscription_is_refused(service):
    from agentic_trading.service import ServiceError

    for bad in ({"endpoint": "http://insecure.example/x", "keys": SUBSCRIPTION["keys"]},
                {"endpoint": ENDPOINT}, {"endpoint": ENDPOINT, "keys": {"p256dh": "x"}}, {}):
        assert webpush.valid(bad) is False
        with pytest.raises(ServiceError):
            service.push_subscribe(bad)


def test_a_device_the_push_service_says_is_gone_is_dropped_not_retried(service, monkeypatch):
    """A subscription dies when the app is deleted or browser data is cleared."""
    service.push_subscribe(SUBSCRIPTION, "iPhone")

    class Gone(Exception):
        response = type("R", (), {"status_code": 410})()

    def fail(*_args, **_kwargs):
        raise Gone()

    monkeypatch.setitem(__import__("sys").modules, "pywebpush",
                        type("M", (), {"webpush": staticmethod(fail), "WebPushException": Gone}))

    result = webpush.deliver(service.state_db(), service.profiles().project_dir / "state",
                             {"title": "t"})
    assert result["removed"] and not result["sent"]
    assert service.push_devices()["devices"] == []


def test_a_transient_failure_keeps_the_device_and_counts_against_it(service, monkeypatch):
    service.push_subscribe(SUBSCRIPTION, "iPhone")

    class Down(Exception):
        response = type("R", (), {"status_code": 503})()

    def fail(*_args, **_kwargs):
        raise Down()

    monkeypatch.setitem(__import__("sys").modules, "pywebpush",
                        type("M", (), {"webpush": staticmethod(fail), "WebPushException": Down}))

    with pytest.raises(webpush.PushError):
        webpush.deliver(service.state_db(), service.profiles().project_dir / "state",
                        {"title": "t"})
    devices = service.push_devices()["devices"]
    assert len(devices) == 1 and devices[0]["failures"] == 1


def test_a_rejection_carries_what_the_push_service_said_not_just_its_number(service, monkeypatch):
    """Apple answers a rejected VAPID identity with 403 and a named reason in the body.

    Recording the status alone turned "the sender identity is wrong" into a bare number, and a
    person reading "403" has no way to tell that from a quota or a routing problem. The body
    describes this sender; it carries nothing about the device or the payload.
    """
    service.push_subscribe(SUBSCRIPTION, "iPhone")

    class Refused(Exception):
        response = type("R", (), {"status_code": 403, "text": "BadJwtToken: bad subject"})()

    def fail(*_args, **_kwargs):
        raise Refused()

    monkeypatch.setitem(__import__("sys").modules, "pywebpush",
                        type("M", (), {"webpush": staticmethod(fail), "WebPushException": Refused}))

    with pytest.raises(webpush.PushError, match="BadJwtToken"):
        webpush.deliver(service.state_db(), service.profiles().project_dir / "state",
                        {"title": "t"})
    assert service.push_devices()["devices"][0]["failures"] == 1


def test_pushing_with_nobody_subscribed_says_so_rather_than_reporting_success(service):
    with pytest.raises(webpush.PushError, match="no device"):
        webpush.deliver(service.state_db(), service.profiles().project_dir / "state",
                        {"title": "t"})


def test_the_service_worker_never_caches_and_never_replays_a_request():
    """The red line for this feature.

    A cached page would show an old price, an old drawdown level and an old proposal as if they
    were current. Worse, an approval queued offline and replayed later would authorise an idea
    against a world that has moved on, which is exactly what the freshness clock, the
    revalidation and the second Hard Risk pass exist to prevent.
    """
    source = (ROOT / "ui" / "sw.js").read_text()

    for forbidden in ("caches", "CacheStorage", "'fetch'", '"fetch"',
                      "background-sync", "'sync'", "BackgroundSync"):
        assert forbidden not in source, f"the service worker must not use {forbidden}"
    assert "addEventListener('fetch'" not in source
    # the only listeners it may register
    listeners = set(re.findall(r"addEventListener\('([a-z]+)'", source))
    assert listeners == {"push", "notificationclick", "pushsubscriptionchange"}, listeners


def test_the_console_is_declared_as_an_app_on_every_page():
    """iOS only delivers push to a page opened from its home-screen icon."""
    manifest = json.loads((ROOT / "ui" / "manifest.webmanifest").read_text())
    assert manifest["display"] == "standalone"
    for icon in manifest["icons"]:
        assert (ROOT / "ui" / icon["src"]).exists(), icon["src"]

    for page in sorted((ROOT / "ui").glob("*.html")):
        text = page.read_text()
        assert 'rel="manifest"' in text, page.name
        assert 'rel="apple-touch-icon"' in text, page.name


def test_the_app_shell_is_reachable_before_signing_in(console_auth):
    """Add to Home Screen happens from the login screen, before any session exists."""
    from fastapi.testclient import TestClient

    from agentic_trading.api.app import app

    client = TestClient(app, base_url="https://testserver")
    for path in ("/ui/manifest.webmanifest", "/ui/sw.js", "/ui/icons/icon-180.png",
                 "/ui/login.html"):
        assert client.get(path).status_code == 200, path

    # and nothing behind it opened up
    for guarded in ("/notifications/key", "/notifications/devices", "/portfolio"):
        assert client.get(guarded).status_code == 401, guarded


def test_subscribing_needs_a_session_and_a_csrf_token(auth_client, console_auth):
    """A subscription endpoint is a capability; writing one is an authenticated action."""
    from fastapi.testclient import TestClient

    from agentic_trading.api.app import app

    anonymous = TestClient(app, base_url="https://testserver", headers={
        "Origin": "https://testserver", "X-CT-Request": "1"})
    assert anonymous.post("/notifications/subscribe",
                          json={"subscription": SUBSCRIPTION}).status_code in (401, 403)

    assert auth_client.post("/notifications/subscribe", json={"subscription": SUBSCRIPTION},
                            headers={"X-CSRF-Token": "wrong"}).status_code == 403


@pytest.mark.parametrize("value", ["", "mailto:owner@localhost", "mailto:owner", "http://x.test",
                                   "https://localhost", "https://console.localhost", "owner@x.test"])
def test_a_contact_the_push_service_would_refuse_is_refused_here_first(value, monkeypatch):
    """Apple answers every bad VAPID contact with one opaque 403, so the check belongs here.

    The shipped placeholder was mailto:owner@localhost. It looked configured, it passed every
    offline test, and Apple rejected all eight sends with 403 BadJwtToken -- a reply that reads
    exactly like a broken key, and sends nobody looking at the subject line.
    """
    monkeypatch.setenv(webpush.SUBJECT_ENV, value)
    with pytest.raises(webpush.PushError, match=webpush.SUBJECT_ENV):
        webpush.resolve_subject()


@pytest.mark.parametrize("value", ["https://console.example.com", "mailto:owner@example.com",
                                   "https://trade.example.ts.net"])
def test_a_real_contact_is_accepted(value, monkeypatch):
    monkeypatch.setenv(webpush.SUBJECT_ENV, value)
    assert webpush.resolve_subject() == value


def test_the_signed_token_expires_well_inside_the_push_service_ceiling():
    """The library's default sits exactly on Apple's 24-hour limit, leaving nothing for skew."""
    assert 0 < webpush.TOKEN_LIFETIME_SECONDS <= 12 * 3600
