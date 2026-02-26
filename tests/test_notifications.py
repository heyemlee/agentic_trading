"""Notification collection, deduplication, retry and the boundary on what may be sent.

Offline: the only channel exercised is the local file. No message leaves the machine, and the
webhook path is tested by asserting it refuses to send rather than by contacting anything.
"""
from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime, timedelta

import pytest

from agentic_trading import notify, operations
from agentic_trading.execution_scenarios import _proposal
from agentic_trading.proposals import PROPOSED, ProposalStore
from agentic_trading.service import Service
from tests.conftest import ROOT, copy_example_ledger


@pytest.fixture
def service(tmp_path, monkeypatch):
    # The notifications read the market regime and the last session through the data layer;
    # which of them fire must not depend on today's market or on reaching it.
    from agentic_trading import risk
    from agentic_trading.risk_scenarios import regime
    from agentic_trading.service import base, books
    from tests.conftest import recent_session

    monkeypatch.setattr(risk, "assess_regime", lambda *_args, **_kwargs: regime("NORMAL"))
    # Each module that anchors to a session resolves its own name, so each one is stubbed: the
    # kept readings in base, the priced portfolio in books.
    for module in (base, books):
        monkeypatch.setattr(module, "_last_session", lambda *_args, **_kwargs: recent_session())
    cfg = tmp_path / "config"
    shutil.copytree(ROOT / "config", cfg)
    state = tmp_path / "state"
    state.mkdir()
    (tmp_path / "runs").mkdir()
    copy_example_ledger("portfolio.example.yaml", state / "portfolio.yaml")
    copy_example_ledger("micro_live_portfolio.example.yaml", state / "micro_live_portfolio.yaml")
    return Service(cfg)


def file_settings(service, **overrides):
    profiles = service.profiles()
    profiles.risk["notifications"].update({"channel": "file", **overrides})
    return notify.Settings.load(profiles)


def delivered(service):
    path = service.profiles().project_dir / "runs" / "notifications.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_nothing_is_sent_until_a_device_has_actually_subscribed(service):
    """The shipped channel is webpush, and it reaches nobody until a device opts in.

    Turning the channel on is not what authorises sending: a person tapping "allow" on the
    device is. Until then there is no subscription to send to and the pass is a no-op.
    """
    settings = service.notification_settings()
    assert settings.channel == "webpush" and settings.configured() is False
    assert "home-screen icon" in settings.why_not_configured()

    event = notify.Event(key="k", kind="proposal_waiting", title="t")
    with pytest.raises(notify.DeliveryError):
        notify.send(settings, event)

    result = notify.dispatch(settings._db, settings, [event])
    assert result["sent"] == [] and result["configured"] is False
    assert delivered(service) == []

    # and with the channel explicitly off, the same holds for a different reason
    off = file_settings(service, channel="none")
    assert off.configured() is False and "authorise" in off.why_not_configured()


def test_an_unconfigured_webhook_never_falls_back_to_sending_somewhere_else(service, monkeypatch):
    monkeypatch.delenv("CT_NOTIFY_WEBHOOK", raising=False)
    settings = file_settings(service, channel="webhook")
    assert settings.configured() is False
    with pytest.raises(notify.DeliveryError, match="CT_NOTIFY_WEBHOOK"):
        notify.send(settings, notify.Event(key="k", kind="proposal_waiting", title="t"))


def test_a_plaintext_endpoint_is_refused(service, monkeypatch):
    monkeypatch.setenv("CT_NOTIFY_WEBHOOK", "http://example.invalid/hook")
    settings = file_settings(service, channel="webhook")
    with pytest.raises(notify.DeliveryError, match="https"):
        notify.send(settings, notify.Event(key="k", kind="proposal_waiting", title="t"))


def test_a_waiting_proposal_is_announced_once_per_quiet_period(service):
    profiles = service.profiles()
    profiles.risk["broker"]["state_file"] = "state/proposals.json"
    ProposalStore(profiles.project_dir / "state" / "proposals.json").save(_proposal(status=PROPOSED))

    events = service.collect_notifications()
    waiting = [e for e in events if e.kind == "proposal_waiting"]
    assert len(waiting) == 1 and "approval" in waiting[0].title

    settings = file_settings(service, min_interval_seconds=900)
    db = service.state_db()
    assert notify.dispatch(db, settings, waiting)["sent"] == [waiting[0].key]
    assert notify.dispatch(db, settings, waiting)["sent"] == []      # still waiting, not resent
    assert len(delivered(service)) == 1

    later = datetime.now(UTC) + timedelta(seconds=901)
    assert notify.dispatch(db, settings, waiting, now=later)["sent"] == [waiting[0].key]
    assert len(delivered(service)) == 2


def test_a_failed_delivery_is_retried_with_backoff_and_then_throttled(service, monkeypatch):
    settings = file_settings(service, max_attempts=2, retry_backoff_seconds=60,
                             min_interval_seconds=900)
    db = service.state_db()
    event = notify.Event(key="k", kind="research_failed", title="failed")
    monkeypatch.setattr(notify, "send", lambda *a: (_ for _ in ()).throw(
        notify.DeliveryError("endpoint unreachable (URLError)")))

    now = datetime.now(UTC)
    assert notify.dispatch(db, settings, [event], now=now)["failed"][0]["key"] == "k"
    # the retry is not attempted before its backoff has elapsed
    assert notify.dispatch(db, settings, [event], now=now + timedelta(seconds=30))["held"] == ["k"]
    assert notify.dispatch(db, settings, [event], now=now + timedelta(seconds=90))["failed"]

    entry = notify.pending(db)["events"]["k"]
    assert entry["state"] == "THROTTLED" and entry["attempts"] == 2
    # a dead endpoint is not hammered: the budget buys a quiet period, not silence
    assert notify.dispatch(db, settings, [event], now=now + timedelta(minutes=5))["held"] == ["k"]
    assert notify.dispatch(db, settings, [event], now=now + timedelta(hours=5))["failed"]


def test_the_notification_pass_does_not_open_the_venue_every_tick(service, monkeypatch):
    """It runs every fifteen seconds. Reading recorded fills opened a Robinhood session for the
    live order list, and asking whether the login still works ran the full connect and
    preflight: two handshakes a tick."""
    from types import SimpleNamespace

    from agentic_trading.service import books

    assert service._micro_enabled(service.profiles())
    now = [1000.0]
    # The venue-probe cooldown is read where the probe is: service/books.py.
    monkeypatch.setattr(books, "time", SimpleNamespace(monotonic=lambda: now[0]))
    probes = []
    monkeypatch.setattr(service, "micro_live",
                        lambda: probes.append(now[0]) or SimpleNamespace(connect_error="token expired"))
    monkeypatch.setattr(service, "_broker",
                        lambda *_args: pytest.fail("the notification pass needs no venue session"))

    for _ in range(3):
        assert [e for e in service.collect_notifications() if e.kind == "credential_invalid"]
        now[0] += 15
    assert probes == [1000.0]

    now[0] = 1000.0 + service.BROKER_PROBE_SECONDS
    service.collect_notifications()
    assert len(probes) == 2


def test_the_market_regime_is_not_announced_as_a_drawdown_ladder(service, monkeypatch):
    from agentic_trading import risk
    from agentic_trading.risk_scenarios import regime

    monkeypatch.setattr(risk, "assess_regime", lambda *_args, **_kwargs: regime("ELEVATED_RISK"))
    assert service.risk_state()["regime"]["level"] == "ELEVATED_RISK"
    assert not [e for e in service.collect_notifications() if "regime" in e.key]


def test_a_push_the_service_refused_is_recorded_and_the_pass_goes_on(service, monkeypatch):
    """With a device subscribed, a refusal from the push service raised PushError, which dispatch
    did not catch: the pass stopped at that event with nothing recorded and nothing after it
    tried, and the 15-second loop logged a traceback each time."""
    from agentic_trading import webpush

    settings = service.notification_settings()
    monkeypatch.setattr(webpush, "devices", lambda _db: [{"device_id": "phone"}])
    refused = []

    def deliver(_db, _state_dir, payload, **_kwargs):
        refused.append(payload["title"])
        raise webpush.PushError("no device accepted the notification: 403: BadJwtToken")

    monkeypatch.setattr(webpush, "deliver", deliver)
    events = [notify.Event(key="a", kind="proposal_waiting", title="first"),
              notify.Event(key="b", kind="proposal_waiting", title="second")]

    result = notify.dispatch(settings._db, settings, events)
    assert [f["key"] for f in result["failed"]] == ["a", "b"] and len(refused) == 2
    assert "BadJwtToken" in result["failed"][0]["error"]
    entry = notify.pending(settings._db)["events"]["a"]
    assert entry["state"] == "PENDING" and entry["attempts"] == 1


def test_a_condition_outlives_the_outage_that_stopped_it_being_delivered(service, monkeypatch):
    """Exhausting the retry budget against a broken channel must not retire the condition.

    On 2026-09-15 nine conditions -- a disconnected broker, four failed research runs, two
    regime changes, two stalled background jobs -- reached the end of their budget while the
    push channel had no subscribed device. Repairing the channel would have left every one of
    them silent for good, because the event keys are stable and the state was terminal.
    """
    settings = file_settings(service, max_attempts=2, retry_backoff_seconds=60,
                             min_interval_seconds=900)
    db = service.state_db()
    event = notify.Event(key="credential_invalid:robinhood", kind="credential_invalid",
                         title="the broker connection needs attention")
    broken = lambda *a: (_ for _ in ()).throw(notify.DeliveryError("no device has subscribed"))
    monkeypatch.setattr(notify, "send", broken)

    now = datetime.now(UTC)
    notify.dispatch(db, settings, [event], now=now)
    notify.dispatch(db, settings, [event], now=now + timedelta(seconds=90))
    assert notify.pending(db)["events"][event.key]["state"] == "THROTTLED"

    monkeypatch.setattr(notify, "send", lambda *a: None)          # the channel is repaired
    result = notify.dispatch(db, settings, [event], now=now + timedelta(hours=1))
    assert result["sent"] == [event.key]
    assert notify.pending(db)["events"][event.key]["state"] == "SENT"


def test_an_entry_left_terminal_by_the_old_code_is_not_silent_forever(service, monkeypatch):
    """Records already written as GAVE_UP must come back without anyone editing state by hand."""
    settings = file_settings(service, max_attempts=2, min_interval_seconds=900)
    db = service.state_db()
    event = notify.Event(key="credential_invalid:robinhood", kind="credential_invalid",
                         title="the broker connection needs attention")
    stale = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    db.write_meta(notify.RECORD, {"events": {event.key: {
        "kind": event.kind, "title": event.title, "state": "GAVE_UP", "attempts": 5,
        "first_seen_at": stale, "last_seen_at": stale, "next_attempt_at": stale,
        "error": "channel is webpush but no device has subscribed"}}})

    monkeypatch.setattr(notify, "send", lambda *a: None)
    assert notify.dispatch(db, settings, [event])["sent"] == [event.key]


def test_a_message_carries_no_credential_or_account_number(service):
    """A notification travels through infrastructure this system does not control."""
    profiles = service.profiles()
    profiles.risk["broker"]["state_file"] = "state/proposals.json"
    ProposalStore(profiles.project_dir / "state" / "proposals.json").save(_proposal(status=PROPOSED))

    blob = json.dumps([event.payload() for event in service.collect_notifications()]).lower()
    for secret in ("access_token", "refresh_token", "bearer", "password", "csrf",
                   "account_number", "client_secret", "api_key"):
        assert secret not in blob


def test_only_configured_event_kinds_are_delivered(service):
    settings = file_settings(service, events=["research_failed"])
    db = service.state_db()
    result = notify.dispatch(db, settings, [
        notify.Event(key="a", kind="proposal_waiting", title="ignored"),
        notify.Event(key="b", kind="research_failed", title="sent"),
    ])
    assert result["sent"] == ["b"]
    assert [row["key"] for row in delivered(service)] == ["b"]


def test_an_overdue_backup_is_something_a_person_is_told_about(service):
    events = service.collect_notifications()
    kinds = {event.kind for event in events}
    assert "backup_overdue" in kinds       # nothing has ever run in a fresh state directory
    # The daily ledger check is one of them: a job that never ran, or one whose check has
    # been refusing, leaves the ledger a week from failing closed.
    late = {event.key.split(":")[1] for event in events if event.kind == "backup_overdue"}
    assert {"backup", "maintenance", "ledger_confirm"} <= late


def test_the_heartbeat_is_silent_until_an_external_watcher_is_configured(service, monkeypatch):
    monkeypatch.delenv("CT_HEARTBEAT_URL", raising=False)
    result = service.send_heartbeat()
    assert result["sent"] is False and "CT_HEARTBEAT_URL" in result["reason"]


def test_a_degraded_service_withholds_the_ping_it_would_otherwise_send(service, monkeypatch):
    """The watcher reads silence as an outage, so a degraded deployment must stop pinging.

    On 2026-09-15 this process stayed up and answered on loopback for twenty minutes while it
    could not open its own state database. A contentless ping sent regardless would have
    reported that as healthy, which is the one case an external watcher exists to catch.
    """
    monkeypatch.setenv("CT_HEARTBEAT_URL", "https://watcher.test/ping")
    monkeypatch.setattr(notify, "_post", lambda *a, **k: pytest.fail("a degraded service pinged"))

    for activity in ("reconciliation", "research_dispatch"):
        service.record_activity(activity, "running", min_interval_seconds=0)
    stale = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    db = service.state_db()
    stored = db.read_meta(operations.HEARTBEATS, {})
    stored["reconciliation"] = {"at": stale, "detail": "stopped"}
    db.write_meta(operations.HEARTBEATS, stored)

    result = service.send_heartbeat()
    assert result["sent"] is False and result["withheld_by"] == ["reconciliation"]


def test_a_working_service_pings_the_watcher(service, monkeypatch):
    """The other half of the same switch: silence must mean something, so noise must too."""
    monkeypatch.setenv("CT_HEARTBEAT_URL", "https://watcher.test/ping")
    posted = []
    monkeypatch.setattr(notify, "_post", lambda url, body, token: posted.append(url) or {})

    for activity in ("reconciliation", "research_dispatch"):
        service.record_activity(activity, "running", min_interval_seconds=0)

    assert service.send_heartbeat()["sent"] is True
    assert posted == ["https://watcher.test/ping"]


def test_state_that_cannot_be_read_is_the_loudest_blocker(service, monkeypatch):
    """The failure that leaves the process answering while it can do nothing must stop the ping."""
    monkeypatch.setenv("CT_HEARTBEAT_URL", "https://watcher.test/ping")
    monkeypatch.setattr(notify, "_post", lambda *a, **k: pytest.fail("unreadable state pinged"))
    monkeypatch.setattr(Service, "operations",
                        lambda self: (_ for _ in ()).throw(RuntimeError("unable to open database")))

    result = service.send_heartbeat()
    assert result["sent"] is False
    assert "unable to open database" in result["withheld_by"][0]


def test_a_quiet_pass_writes_nothing_to_disk(service):
    """This shares the fifteen-second loop, and a quiet system is the normal case."""
    settings = file_settings(service)
    db = service.state_db()
    event = notify.Event(key="k", kind="research_failed", title="failed")

    assert notify.dispatch(db, settings, [event])["recorded"] is True
    before = db.read_meta(notify.RECORD, {})
    assert notify.dispatch(db, settings, [event])["recorded"] is False
    assert db.read_meta(notify.RECORD, {}) == before
    assert notify.dispatch(db, settings, [])["recorded"] is False


def wecom_settings(service, monkeypatch, **overrides):
    monkeypatch.setenv('CT_WECOM_WEBHOOK', 'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=k')
    return file_settings(service, channel='wecom', **overrides)


def test_a_wecom_message_says_what_happened_and_links_to_the_right_screen(service, monkeypatch):
    monkeypatch.setenv('CT_CONSOLE_URL', 'https://mac.tail1234.ts.net')
    settings = wecom_settings(service, monkeypatch)
    body = notify.wecom_message(settings, notify.Event(
        key='k', kind='proposal_waiting', title='BUY VTI needs your approval',
        detail='$35.00 in micro_live (real money)', link='/ui/approvals.html'))

    assert body['msgtype'] == 'markdown'
    content = body['markdown']['content']
    assert 'BUY VTI needs your approval' in content
    assert '$35.00' in content
    assert 'https://mac.tail1234.ts.net/ui/approvals.html' in content


def test_a_link_is_omitted_rather_than_sent_as_an_unusable_relative_path(service, monkeypatch):
    settings = wecom_settings(service, monkeypatch)          # no console_url configured
    content = notify.wecom_message(settings, notify.Event(
        key='k', kind='proposal_waiting', title='t', link='/ui/approvals.html'))['markdown']['content']
    assert '/ui/approvals.html' not in content and 't' in content


def test_wecom_refusing_a_message_is_a_failure_even_though_it_answers_200(service, monkeypatch):
    """WeCom reports a bad key or a revoked bot inside a 200 response.

    Reading only the status code would record a message nobody received as delivered, which is
    the worst possible failure for a notification channel: silent and confident.
    """
    settings = wecom_settings(service, monkeypatch)
    monkeypatch.setattr(notify, '_post',
                        lambda *a, **k: {'errcode': 93000, 'errmsg': 'invalid webhook url'})
    with pytest.raises(notify.DeliveryError, match='93000'):
        notify.send(settings, notify.Event(key='k', kind='research_failed', title='t'))

    monkeypatch.setattr(notify, '_post', lambda *a, **k: {'errcode': 0, 'errmsg': 'ok'})
    notify.send(settings, notify.Event(key='k', kind='research_failed', title='t'))


def test_an_unconfigured_wecom_channel_sends_nothing(service, monkeypatch):
    monkeypatch.delenv('CT_WECOM_WEBHOOK', raising=False)
    settings = file_settings(service, channel='wecom')
    assert settings.configured() is False
    with pytest.raises(notify.DeliveryError, match='CT_WECOM_WEBHOOK'):
        notify.send(settings, notify.Event(key='k', kind='proposal_waiting', title='t'))


def test_the_console_address_is_read_from_the_environment_never_from_config(service, monkeypatch):
    """The address names this machine on the tailnet, and config/ is published with the code."""
    assert "console_url" not in service.profiles().risk["notifications"]
    monkeypatch.delenv('CT_CONSOLE_URL', raising=False)
    assert file_settings(service).console_url == ''
    monkeypatch.setenv('CT_CONSOLE_URL', 'http://mac.tail1234.ts.net/')
    assert file_settings(service).console_url == ''          # not https: no link rather than a bad one
    monkeypatch.setenv('CT_CONSOLE_URL', 'https://mac.tail1234.ts.net/')
    assert file_settings(service).console_url == 'https://mac.tail1234.ts.net'


def wechat_settings(service, monkeypatch, **overrides):
    monkeypatch.setenv('CT_WECHAT_WEBHOOK', 'https://sctapi.ftqq.com/SCTxxxx.send')
    return file_settings(service, channel='wechat', **overrides)


def test_a_wechat_message_fits_the_relay_and_keeps_the_full_title_in_the_body(service, monkeypatch):
    monkeypatch.setenv('CT_CONSOLE_URL', 'https://mac.tail1234.ts.net')
    settings = wechat_settings(service, monkeypatch)
    long_title = 'BUY VTI 需要你审批，金额 35 美元，实盘账户，请尽快处理这条提案'
    body = notify.wechat_message(settings, notify.Event(
        key='k', kind='proposal_waiting', title=long_title,
        detail='$35.00 in micro_live', link='/ui/approvals.html'))

    assert len(body['title']) <= notify.WECHAT_TITLE_CHARS
    assert long_title in body['desp']          # nothing is lost, only moved
    assert '$35.00 in micro_live' in body['desp']
    assert 'https://mac.tail1234.ts.net/ui/approvals.html' in body['desp']


def test_the_wechat_relay_refusing_a_message_is_a_failure_despite_the_200(service, monkeypatch):
    """A spent free quota comes back as HTTP 200 with a non-zero code."""
    settings = wechat_settings(service, monkeypatch)
    monkeypatch.setattr(notify, '_post',
                        lambda *a, **k: {'code': 40001, 'message': 'quota exceeded'})
    with pytest.raises(notify.DeliveryError, match='40001'):
        notify.send(settings, notify.Event(key='k', kind='proposal_waiting', title='t'))

    monkeypatch.setattr(notify, '_post', lambda *a, **k: {'code': 0, 'message': ''})
    notify.send(settings, notify.Event(key='k', kind='proposal_waiting', title='t'))


def test_a_wechat_message_carries_nothing_the_relay_should_not_read(service, monkeypatch):
    """The relay is a third party that reads everything passing through it."""
    profiles = service.profiles()
    profiles.risk['broker']['state_file'] = 'state/proposals.json'
    ProposalStore(profiles.project_dir / 'state' / 'proposals.json').save(_proposal(status=PROPOSED))
    monkeypatch.setenv('CT_CONSOLE_URL', 'https://mac.tail1234.ts.net')
    settings = wechat_settings(service, monkeypatch)

    blob = json.dumps([notify.wechat_message(settings, event)
                       for event in service.collect_notifications()]).lower()
    for secret in ('access_token', 'refresh_token', 'bearer', 'password', 'csrf',
                   'account_number', 'client_secret', 'api_key', 'sctxxxx'):
        assert secret not in blob


def test_an_unconfigured_wechat_channel_sends_nothing(service, monkeypatch):
    monkeypatch.delenv('CT_WECHAT_WEBHOOK', raising=False)
    settings = file_settings(service, channel='wechat')
    assert settings.configured() is False
    with pytest.raises(notify.DeliveryError, match='CT_WECHAT_WEBHOOK'):
        notify.send(settings, notify.Event(key='k', kind='proposal_waiting', title='t'))


# -- autonomy: the case where nobody was asked ---------------------------------------

def test_every_declared_event_kind_exists_in_code_and_the_reverse(service):
    """The set is closed on purpose, so config and code must not drift apart."""
    declared = set(service.profiles().risk["notifications"]["events"])
    assert declared <= set(notify.EVENTS), f"config names unknown kinds: {declared - set(notify.EVENTS)}"
    assert {"autonomy_acted", "autonomy_withdrawn"} <= declared, \
        "autonomy is the case where nobody was asked; being told is the only visibility"


def test_a_withdrawn_autonomy_is_announced_urgently(service):
    from agentic_trading import autonomy

    profiles = service.profiles()
    assert not [e for e in service.collect_notifications() if e.kind == "autonomy_withdrawn"]
    autonomy.trip(profiles, "the venue returned an order we cannot account for")
    event = next(e for e in service.collect_notifications() if e.kind == "autonomy_withdrawn")
    assert event.urgent, "orders stopping without anyone knowing is the failure this prevents"
    assert "cannot account for" in event.detail and "manual reset" in event.detail


def test_an_order_placed_without_asking_is_announced(service):
    profiles = service.profiles()
    store = ProposalStore(profiles.project_dir / "state" / "proposals.json")
    proposal = _proposal(bucket="micro_live", action="REDUCE", ticker="VTI", notional=0.0)
    proposal.status = PROPOSED
    proposal.approved_by = "autonomy:reduce"
    store.save(proposal)
    event = next(e for e in service.collect_notifications() if e.kind == "autonomy_acted")
    assert "without asking you" in event.title
    assert event.urgent, "real money decided without a person is always urgent"


def test_a_persons_own_approval_is_not_announced_as_autonomous(service):
    profiles = service.profiles()
    store = ProposalStore(profiles.project_dir / "state" / "proposals.json")
    proposal = _proposal(bucket="micro_live", action="REDUCE", ticker="VTI", notional=0.0)
    proposal.status = PROPOSED
    proposal.approved_by = "owner"
    store.save(proposal)
    assert not [e for e in service.collect_notifications() if e.kind == "autonomy_acted"]


def test_the_outcome_arrives_as_well_as_the_decision(service):
    profiles = service.profiles()
    store = ProposalStore(profiles.project_dir / "state" / "proposals.json")
    proposal = _proposal(bucket="micro_live", action="REDUCE", ticker="VTI", notional=0.0)
    proposal.status = PROPOSED
    proposal.approved_by = "autonomy:reduce"
    store.save(proposal)
    first = next(e for e in service.collect_notifications() if e.kind == "autonomy_acted")
    proposal = store.get(proposal.proposal_id)
    proposal.status = "EXECUTED"
    store.save(proposal)
    second = next(e for e in service.collect_notifications() if e.kind == "autonomy_acted")
    assert first.key != second.key, "a status change must not be deduplicated away"


@pytest.mark.parametrize("channel,expected", [
    ("file", "the local file"), ("webhook", "CT_NOTIFY_WEBHOOK"),
    ("wecom", "CT_WECOM_WEBHOOK"), ("wechat", "CT_WECHAT_WEBHOOK"),
    ("webpush", "subscribed device"),
])
def test_a_test_message_names_the_channel_it_actually_used(service, monkeypatch, capsys,
                                                           channel, expected):
    """Reporting the webhook variable for every channel described a send that never happened.

    A web push that reached a phone was announced as having gone to an endpoint in a variable
    nobody had set, which is the kind of true-sounding line that sends someone debugging the
    wrong thing.
    """
    from agentic_trading import notify_cli

    profiles = service.profiles()
    profiles.risk["notifications"]["channel"] = channel
    monkeypatch.setattr(notify_cli, "load_profiles", lambda *_a, **_k: profiles)
    monkeypatch.setattr(notify.Settings, "configured", lambda self: True)
    monkeypatch.setattr(notify, "send", lambda *_a, **_k: None)
    monkeypatch.setattr("agentic_trading.notify_cli.send", lambda *_a, **_k: None)

    assert notify_cli.main(["test", "--config-dir", str(service.config_dir)]) == 0
    reported = json.loads(capsys.readouterr().out)
    assert reported["sent"] is True and reported["channel"] == channel
    assert expected in reported["to"]


def test_a_ledger_the_check_refused_is_reported_as_the_venue_and_the_book_disagreeing(service):
    """The daily job's refusal would otherwise sit in a report nobody opens until the ledger
    expired a week later and the research stopped."""
    from agentic_trading.ledger_confirm import REPORT

    db = service.state_db()
    db.write_meta(REPORT, {
        "date": "2026-09-17", "confirmed": False, "at": "2026-09-17T10:45:00+00:00",
        "ledgers": [{"ledger": "conceptual", "confirmed": True},
                    {"ledger": "micro_live", "confirmed": False,
                     "error": "PortfolioError: cash mismatch: the venue holds $250.00 more"}]})
    events = [e for e in service.collect_notifications() if e.key.startswith("ledger_unconfirmed")]
    assert [e.key for e in events] == ["ledger_unconfirmed:micro_live:2026-09-17"]
    assert events[0].kind == "reconciliation_error" and "$250.00" in events[0].detail
    assert events[0].link.endswith("#ledger-notice")

    # A check that later agreed -- the job's, the CLI's or the console button's -- clears it.
    db.write_meta(REPORT, {"date": "2026-09-17", "confirmed": True, "at": "2026-09-17T10:50:00+00:00",
                           "ledgers": [{"ledger": "micro_live", "confirmed": True}]})
    assert not [e for e in service.collect_notifications() if e.key.startswith("ledger_unconfirmed")]
