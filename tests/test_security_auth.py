"""Exercise the HTTP security boundary without a broker or model connection."""
import re
import time
from importlib import import_module
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from agentic_trading.api.app import app
from agentic_trading.api.auth import COOKIE, ConsoleAuth, configure_password


@pytest.mark.parametrize('path', ['/health', '/portfolio', '/risk', '/orders', '/proposals', '/research', '/openapi.json'])
def test_business_reads_require_login(console_auth, path):
    client = TestClient(app, base_url='https://testserver')
    assert client.get(path).status_code == 401


def test_missing_configuration_is_closed(monkeypatch, tmp_path):
    module = import_module('agentic_trading.api.app')
    monkeypatch.setattr(module, '_auth', ConsoleAuth(tmp_path / 'absent', ['https://testserver']))
    client = TestClient(app, base_url='https://testserver')
    assert client.get('/portfolio').status_code == 503
    assert client.get('/ui/').status_code == 200  # redirected to the login screen
    assert client.get('/auth/session').json()['configured'] is False


def test_external_origin_empty_approval_and_resume_never_reach_service(auth_client, monkeypatch):
    calls = []
    module = import_module('agentic_trading.api.app')
    monkeypatch.setattr(module, '_service', SimpleNamespace(
        approve=lambda *args, **kwargs: calls.append('approve'), resume=lambda: calls.append('resume')))
    for path in ['/proposals/example/approve', '/system/resume']:
        assert auth_client.post(path, headers={'Origin': 'https://attacker.example'}, json={}).status_code == 403
    assert auth_client.post('/proposals/example/approve').status_code == 403
    assert auth_client.post('/proposals/example/approve', json={}).status_code == 422
    assert calls == []


def test_csrf_host_and_fetch_metadata_checks(auth_client):
    for headers in [{'X-CSRF-Token': 'wrong'}, {'Sec-Fetch-Site': 'cross-site'}, {'Origin': 'null'}]:
        assert auth_client.post('/system/resume', headers=headers, json={}).status_code == 403
    assert auth_client.get('/health', headers={'Host': 'rebound.example'}).status_code == 400


def test_the_referrer_stays_inside_the_console_and_goes_no_further(auth_client):
    """The back link tells an in-app step back from a cold open by document.referrer, which
    no-referrer emptied on every navigation. same-origin keeps it and still sends nothing out."""
    assert auth_client.get('/ui/login.html').headers['referrer-policy'] == 'same-origin'


def test_a_non_ascii_csrf_token_is_refused_not_a_server_error(auth_client):
    response = auth_client.post('/system/resume', headers={'X-CSRF-Token': b'caf\xe9'}, json={})
    assert response.status_code == 403
    assert response.headers['x-frame-options'] == 'DENY'


def test_login_cookie_logout_and_headers(console_auth):
    client = TestClient(app, base_url='https://testserver', headers={
        'Origin': 'https://testserver', 'X-CT-Request': '1'})
    result = client.post('/auth/login', json={'username': 'test-owner', 'password': 'Test-Only-Console-Pw1'})
    cookie = result.headers['set-cookie'].lower()
    assert all(flag in cookie for flag in ['httponly', 'secure', 'samesite=strict'])
    assert 'max-age=2592000' in cookie
    assert 'password' not in result.text
    session = client.get('/auth/session').json()
    assert session['username'] == 'test-owner'
    assert client.post('/auth/logout', json={}, headers={'X-CSRF-Token': session['csrf_token']}).status_code == 200
    assert client.get('/portfolio').status_code == 401
    ui = client.get('/ui/login.html')
    assert "script-src 'self';" in ui.headers['content-security-policy']
    # Revalidated on every visit, never shown from a cache unasked: see _cache_policy.
    assert ui.headers['cache-control'] == 'no-cache'
    assert ui.headers['x-frame-options'] == 'DENY'


def test_console_pages_revalidate_and_their_assets_are_stamped(console_auth):
    """A tap on the tab bar must not download the console again.

    Every response used to be no-store, so each screen change re-fetched a quarter of a
    megabyte of unchanged script. The pages still go back to the server every time, because a
    new release or a signed-out session has to be seen at once, but a page that has not changed
    is answered with 304 and no body, and the assets it names carry the release stamp, so the
    browser keeps them without asking until a release names different ones. What the API says
    about money and sessions stays no-store.
    """
    client = TestClient(app, base_url='https://testserver')
    page = client.get('/ui/login.html')
    assert page.status_code == 200
    assert page.headers['cache-control'] == 'no-cache'
    stamps = set(re.findall(r'(?:src|href)="(?:assets|icons)/[^"?]+\?v=([0-9a-f]+)"', page.text))
    assert len(stamps) == 1, 'every asset a page names carries the one release stamp'
    stamp = stamps.pop()
    assert 'src="assets/i18n.js?v=' + stamp + '"' in page.text
    assert 'href="assets/console.css?v=' + stamp + '"' in page.text
    assert not re.search(r'(?:src|href)="(?:assets|icons)/[^"?]+"', page.text), 'an asset was left unstamped'

    unchanged = client.get('/ui/login.html', headers={'If-None-Match': page.headers['etag']})
    assert unchanged.status_code == 304
    assert not unchanged.content
    head = client.head('/ui/login.html')
    assert head.status_code == 200 and not head.content
    assert head.headers['etag'] == page.headers['etag']

    stamped = client.get('/ui/assets/console.js?v=' + stamp)
    assert stamped.status_code == 200
    assert stamped.headers['cache-control'] == 'max-age=31536000, immutable'
    assert client.get('/ui/assets/console.js').headers['cache-control'] == 'no-cache'
    assert client.get('/ui/icons/icon-192.png?v=' + stamp).headers['cache-control'] == 'max-age=31536000, immutable'
    assert client.get('/ui/sw.js').headers['cache-control'] == 'no-store'
    assert client.get('/ui/manifest.webmanifest').headers['cache-control'] == 'no-store'
    assert client.get('/auth/session').headers['cache-control'] == 'no-store'
    # The directory index is a page like any other, and demo pages are public.
    index = client.get('/ui/?demo=1')
    assert index.status_code == 200 and index.headers['cache-control'] == 'no-cache'
    assert 'src="assets/console.js?v=' + stamp + '"' in index.text


def test_a_slow_read_is_named_in_the_log(auth_client, monkeypatch, caplog):
    """The console cannot show a screen before its reads answer, so a slow one must be findable."""
    import logging

    module = import_module('agentic_trading.api.app')
    monkeypatch.setattr(module, '_SLOW_MS', 20)
    monkeypatch.setattr(module, '_service', SimpleNamespace(
        operations=lambda: time.sleep(0.05) or {}, notification_status=dict))
    with caplog.at_level(logging.INFO, logger='agentic_trading.api.app'):
        assert auth_client.get('/system/operations').status_code == 200
        assert auth_client.get('/system/notifications').status_code == 200
    slow = [record.getMessage() for record in caplog.records if 'slow request' in record.getMessage()]
    assert len(slow) == 1 and 'GET /system/operations answered in' in slow[0]


def test_console_asset_stamp_follows_the_release_and_otherwise_the_files(tmp_path):
    """In a release the stamp is the release; in a checkout it moves when a file does."""
    from fastapi import FastAPI

    from agentic_trading.api.app import ConsoleFiles

    ui = tmp_path / 'ui'
    (ui / 'assets').mkdir(parents=True)
    (ui / 'icons').mkdir()
    (ui / 'assets' / 'console.js').write_text('1')
    (ui / 'page.html').write_text('<link rel="stylesheet" href="assets/console.css?v=old" />'
                                  '<script src="assets/console.js"></script><a href="index.html">x</a>')
    checkout = ConsoleFiles(ui)
    first = checkout.stamp()
    assert checkout.stamp() == first
    (ui / 'assets' / 'console.js').write_text('22')
    assert checkout.stamp() != first, 'an edited asset must reach the browser on the next load'

    (tmp_path / 'RELEASE.json').write_text('{"name": "20260916T070731Z-0c1edee"}')
    release = ConsoleFiles(ui)
    assert release.stamp() == release.stamp()
    assert release.stamp() not in (first, checkout.stamp())
    (ui / 'assets' / 'console.js').write_text('333')
    assert release.stamp() == release.stamp(), 'a release is immutable, so its stamp is too'

    probe = FastAPI()
    probe.mount('/ui', release)
    with TestClient(probe) as client:
        body = client.get('/ui/page.html').text
        assert 'href="assets/console.css?v=' + release.stamp() + '"' in body
        assert 'src="assets/console.js?v=' + release.stamp() + '"' in body
        assert 'v=old' not in body
        assert 'href="index.html"' in body, 'navigation is not an asset and is left alone'


@pytest.mark.parametrize('password', [
    'Aa1!aaa',                 # seven characters, even with every class present
    'aa1!aaaa',                # no upper case
    'AA1!AAAA',                # no lower case
    'Aa!aaaaa',                # no digit
    'Aa1aaaaa',                # no symbol
    'Aa1!' + 'a' * 1021,       # nothing rescues a password past the ceiling
])
def test_weak_passwords_are_refused(tmp_path, password):
    """Eight characters is the floor only because all four classes are mandatory."""
    with pytest.raises(ValueError):
        configure_password(tmp_path / 'auth.json', 'test-owner', password)
    assert not (tmp_path / 'auth.json').exists()


@pytest.mark.parametrize('password', ['Aa1!aaaa', 'Tr4de$!!', 'Test-Only-Console-Pw1'])
def test_shortest_acceptable_password_signs_in(tmp_path, password):
    path = tmp_path / 'auth.json'
    configure_password(path, 'test-owner', password)
    assert ConsoleAuth(path, ['https://testserver']).login('test-owner', password)[1] == 'ok'


@pytest.mark.parametrize('password', [
    'correct-horse-battery-staple',        # no upper case, no digit, no symbol
    'test-only-password-for-console',
])
def test_a_passphrase_is_accepted_on_its_length_alone(tmp_path, password):
    """The four classes are what a short password owes instead of length, not as well as it.

    Requiring them at every length rejected a thirty-character passphrase for having no digit —
    the one kind of password worth encouraging — and made every password in this suite shorter.
    """
    path = tmp_path / 'auth.json'
    configure_password(path, 'test-owner', password)
    assert ConsoleAuth(path, ['https://testserver']).login('test-owner', password)[1] == 'ok'


def test_one_character_short_of_a_passphrase_still_owes_its_classes(tmp_path):
    fifteen = 'a-short-passwrd'
    assert len(fifteen) == 15
    with pytest.raises(ValueError):
        configure_password(tmp_path / 'auth.json', 'test-owner', fifteen)
    configure_password(tmp_path / 'auth.json', 'test-owner', fifteen + 'x')


def test_password_rotation_revokes_sessions(auth_client, console_auth):
    configure_password(console_auth.path, 'test-owner', 'Replacement-Test-Pw2')
    assert auth_client.get('/portfolio').status_code == 401


def test_session_survives_auth_object_restart(auth_client, console_auth, monkeypatch):
    module = import_module('agentic_trading.api.app')
    restarted = ConsoleAuth(console_auth.path, ['https://testserver'])
    monkeypatch.setattr(module, '_auth', restarted)
    session = auth_client.get('/auth/session').json()
    assert session['authenticated'] is True
    assert session['username'] == 'test-owner'


def test_expiry_and_rate_limit(auth_client, console_auth):
    token = auth_client.cookies.get(COOKIE)
    with console_auth.db.transaction() as connection:
        connection.execute('UPDATE console_sessions SET expires_at=? WHERE token_hash=?',
                           (time.time() - 1, console_auth._key(token)))
    assert auth_client.get('/portfolio').status_code == 401
    for _ in range(5):
        assert console_auth.login('test-owner', 'wrong')[1] == 'invalid'
    assert console_auth.login('test-owner', 'Test-Only-Console-Pw1')[1] == 'rate_limited'


def test_identity_is_from_session_not_payload(auth_client, monkeypatch):
    calls = []
    module = import_module('agentic_trading.api.app')
    monkeypatch.setattr(module, '_service', SimpleNamespace(approve=lambda *args, **kwargs: calls.append(kwargs) or {}))
    body = {'confirm': True, 'version': 'a' * 64, 'idempotency_key': '77197e1a-bdeb-48f7-a807-f425365bc705',
            'ticker': 'VTI', 'bucket': 'micro_live', 'action': 'BUY', 'notional_usd': 35}
    assert auth_client.post('/proposals/id/approve', json={**body, 'identity': 'someone-else'}).status_code == 422
    assert auth_client.post('/proposals/id/approve', json={**body, 'confirm': 1}).status_code == 422
    assert auth_client.post('/proposals/id/approve', json={**body, 'confirm': False}).status_code == 400
    assert calls == []
    assert auth_client.post('/proposals/id/approve', json=body).status_code == 200
    assert calls[0]['identity'] == 'test-owner'


IPHONE = ('Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 '
          '(KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1')
LAPTOP = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
          '(KHTML, like Gecko) Chrome/130.0 Safari/537.36')


def sign_in(console_auth, agent, password='Test-Only-Console-Pw1'):
    """A second browser signing in, with its own cookie jar."""
    client = TestClient(app, base_url='https://testserver', headers={
        'Origin': 'https://testserver', 'X-CT-Request': '1', 'User-Agent': agent})
    result = client.post('/auth/login', json={'username': 'test-owner', 'password': password})
    assert result.status_code == 200
    client.headers['X-CSRF-Token'] = result.json()['csrf_token']
    return client


def test_each_device_is_listed_separately_and_the_current_one_is_marked(console_auth):
    phone, laptop = sign_in(console_auth, IPHONE), sign_in(console_auth, LAPTOP)

    listed = laptop.get('/auth/sessions').json()
    assert listed['max_age_days'] == 30
    by_label = {item['label']: item for item in listed['sessions']}
    assert set(by_label) == {'iPhone · Safari', 'Mac · Chrome'}
    assert by_label['Mac · Chrome']['current'] is True
    assert by_label['iPhone · Safari']['current'] is False
    # the phone sees the same two devices, with the roles reversed
    assert {i['label']: i['current'] for i in phone.get('/auth/sessions').json()['sessions']} == {
        'iPhone · Safari': True, 'Mac · Chrome': False}


def test_no_token_or_hash_is_exposed_by_the_device_list(console_auth):
    client = sign_in(console_auth, LAPTOP)
    token = client.cookies.get(COOKIE)
    blob = client.get('/auth/sessions').text
    assert token not in blob
    assert console_auth._key(token) not in blob
    for field in ('token', 'hash', 'csrf', 'password'):
        assert field not in blob.lower()


def test_a_lost_phone_is_signed_out_without_touching_the_other_devices(console_auth):
    phone, laptop = sign_in(console_auth, IPHONE), sign_in(console_auth, LAPTOP)
    target = next(i for i in laptop.get('/auth/sessions').json()['sessions']
                  if i['label'] == 'iPhone · Safari')

    assert laptop.post(f'/auth/sessions/{target["session_id"]}/revoke', json={}).status_code == 200
    assert phone.get('/portfolio').status_code == 401          # the phone is out
    assert laptop.get('/auth/sessions').status_code == 200     # this device is untouched
    assert [i['label'] for i in laptop.get('/auth/sessions').json()['sessions']] == ['Mac · Chrome']
    # revoking it again is not a second success
    assert laptop.post(f'/auth/sessions/{target["session_id"]}/revoke', json={}).status_code == 404


def test_signing_out_other_devices_keeps_this_one(console_auth):
    phone, laptop = sign_in(console_auth, IPHONE), sign_in(console_auth, LAPTOP)
    assert laptop.post('/auth/sessions/revoke-others', json={}).json()['revoked'] == 1
    assert phone.get('/portfolio').status_code == 401
    assert laptop.get('/auth/session').json()['authenticated'] is True


def test_revoking_the_device_you_are_holding_signs_it_out_here_too(console_auth):
    laptop = sign_in(console_auth, LAPTOP)
    mine = next(i for i in laptop.get('/auth/sessions').json()['sessions'] if i['current'])
    response = laptop.post(f'/auth/sessions/{mine["session_id"]}/revoke', json={})
    assert response.status_code == 200
    assert 'ct_session=""' in response.headers['set-cookie'] or \
           'ct_session=;' in response.headers['set-cookie']


def test_use_records_activity_but_never_buys_more_time(console_auth, monkeypatch):
    """A console left polling in a browser tab must not keep a session alive forever."""
    from agentic_trading.api import auth as auth_module

    client = sign_in(console_auth, LAPTOP)
    first = client.get('/auth/sessions').json()['sessions'][0]

    # polling inside the quiet window writes nothing
    client.get('/auth/session')
    unchanged = client.get('/auth/sessions').json()['sessions'][0]
    assert unchanged['last_seen_at'] == first['last_seen_at']

    monkeypatch.setattr(auth_module, 'TOUCH_SECONDS', 0)
    client.get('/auth/session')
    later = client.get('/auth/sessions').json()['sessions'][0]
    assert later['last_seen_at'] > first['last_seen_at']
    assert later['expires_at'] == first['expires_at']          # activity is not an extension
    assert later['created_at'] == first['created_at']


def test_a_session_that_reaches_thirty_days_is_gone_however_active_it_was(console_auth):
    client = sign_in(console_auth, LAPTOP)
    token = client.cookies.get(COOKIE)
    with console_auth.db.transaction() as connection:
        connection.execute('UPDATE console_sessions SET expires_at=?,last_seen_at=? '
                           'WHERE token_hash=?',
                           (time.time() - 1, time.time(), console_auth._key(token)))
    assert client.get('/portfolio').status_code == 401
    assert console_auth.sessions() == []          # and it is not left in the device list


def test_password_rotation_signs_every_device_out(console_auth):
    phone, laptop = sign_in(console_auth, IPHONE), sign_in(console_auth, LAPTOP)
    configure_password(console_auth.path, 'test-owner', 'A-Replacement-Pw3-Here')
    for client in (phone, laptop):
        assert client.get('/portfolio').status_code == 401
    assert console_auth.sessions() == []


def test_a_session_predating_device_management_still_works_and_becomes_revocable(console_auth):
    """A deployed console must keep its logins across the upgrade, not sign everyone out."""
    client = sign_in(console_auth, LAPTOP)
    token = client.cookies.get(COOKIE)
    with console_auth.db.transaction() as connection:
        connection.execute('UPDATE console_sessions SET session_id=NULL,label=NULL '
                           'WHERE token_hash=?', (console_auth._key(token),))
    before = console_auth.sessions()
    assert len(before) == 1 and before[0]['revocable'] is False   # listed, not yet revocable

    assert client.get('/auth/session').json()['authenticated'] is True   # still signed in
    listed = console_auth.sessions(token)
    assert len(listed) == 1 and listed[0]['session_id'] and listed[0]['current'] is True
    assert console_auth.revoke(listed[0]['session_id']) is True


def test_an_empty_session_id_revokes_nothing(console_auth):
    sign_in(console_auth, LAPTOP)
    assert console_auth.revoke('') is False
    assert len(console_auth.sessions()) == 1


def test_upgrading_a_deployed_database_keeps_its_sessions(tmp_path):
    """The columns behind device management were added after the table shipped.

    A database written by an earlier release has neither, and a read-only query for them fails.
    Reporting that as an empty list would tell the owner they had been signed out everywhere.
    """
    import sqlite3

    from agentic_trading.api.auth import ConsoleAuth, configure_password
    from agentic_trading.state_db import StateDB

    path = tmp_path / 'auth.json'
    configure_password(path, 'test-owner', 'Test-Only-Console-Pw1')
    database = StateDB(path).path
    with sqlite3.connect(database) as connection:      # recreate the pre-upgrade shape
        connection.execute('DROP TABLE console_sessions')
        connection.execute(
            'CREATE TABLE console_sessions(token_hash TEXT PRIMARY KEY, username TEXT NOT NULL,'
            ' csrf TEXT NOT NULL, generation TEXT NOT NULL, created_at REAL NOT NULL,'
            ' last_seen_at REAL NOT NULL, expires_at REAL NOT NULL)')
        connection.execute('INSERT INTO console_sessions VALUES(?,?,?,?,?,?,?)',
                           ('hash', 'test-owner', 'csrf', 'gen', 1.0, 1.0, time.time() + 9999))

    auth = ConsoleAuth(path, ['https://testserver'])
    listed = auth.sessions()
    # Listed, so the device count is truthful, but not yet revocable individually.
    assert len(listed) == 1
    assert listed[0]['revocable'] is False and listed[0]['session_id'] == ''
    assert listed[0]['label'] == 'unknown device'

    # and reading it changed nothing: the running API may still be the older release
    with sqlite3.connect(database) as connection:
        columns = {row[1] for row in connection.execute('PRAGMA table_info(console_sessions)')}
        assert {'session_id', 'label'}.isdisjoint(columns)
        assert connection.execute('SELECT count(*) FROM console_sessions').fetchone()[0] == 1


def test_an_unreadable_session_table_is_an_error_not_an_empty_device_list(console_auth):
    """An empty device list reads as "you are signed out everywhere". Never say that by mistake."""
    import sqlite3

    sign_in(console_auth, LAPTOP)
    with console_auth.db.transaction() as connection:
        connection.execute('DROP TABLE console_sessions')

    with pytest.raises(sqlite3.OperationalError):
        console_auth.sessions()


def test_a_migrated_database_still_accepts_a_login_from_the_release_before_it(console_auth):
    """The schema must never get ahead of the process using it.

    This is the failure it prevents, exactly as it happened: a CLI run from a development
    checkout added the two device-management columns to the live database while the older
    released API was still serving it. That API inserted sessions with positional values, so
    the next login failed with "table console_sessions has 9 columns but 7 values were
    supplied" — the console could still be browsed on an existing cookie, but nobody could sign
    in again. Named columns on the write side make the older shape keep working.
    """
    import sqlite3

    sign_in(console_auth, LAPTOP)             # migrates the table to the current shape
    with console_auth.db.transaction() as connection:
        columns = [row[1] for row in connection.execute('PRAGMA table_info(console_sessions)')]
        assert {'session_id', 'label'} <= set(columns)

        # what the previous release's login did, verbatim: positional values, seven of them
        with pytest.raises(sqlite3.OperationalError, match='columns'):
            connection.execute('INSERT INTO console_sessions VALUES(?,?,?,?,?,?,?)',
                               ('older', 'test-owner', 'csrf', 'gen', 1.0, 1.0, 9e9))

    # and what this release does instead, which survives any further column being added
    assert console_auth.login('test-owner', 'Test-Only-Console-Pw1')[1] == 'ok'
    assert len(console_auth.sessions()) == 2


def test_every_write_to_a_migrated_table_names_its_columns():
    """A positional INSERT is what breaks the moment a column is added. Ban it at the source."""
    import re

    from tests.conftest import ROOT

    migrated = {table for table, _column, _kind in
                __import__('agentic_trading.state_db', fromlist=['StateDB']).StateDB.ADDED_COLUMNS}
    offenders = []
    for path in sorted((ROOT / 'agentic_trading').rglob('*.py')):
        for table in migrated:
            if re.search(rf'INSERT\s+INTO\s+{table}\s+VALUES', path.read_text()):
                offenders.append(f'{path.name}: positional INSERT into {table}')
    assert not offenders, offenders


def test_primary_layouts_arrive_before_navigation_without_executable_scripts(console_auth):
    """The first tap must not wait for another HTML request, even on a cold connection."""
    client = TestClient(app, base_url='https://testserver')
    names = {'index', 'portfolio', 'advisor', 'more', 'risk', 'activity', 'evaluation',
             'profile', 'notifications', 'system', 'approvals', 'research', 'lab'}
    for name in names:
        page = client.get(f'/ui/{name}.html?demo=1')
        templates = re.findall(r'<template data-ct-page="([^"]+)"[^>]*>(.*?)</template>', page.text, re.DOTALL)
        assert {key for key, _ in templates} == names - {name}
        for _, markup in templates:
            assert 'id="content"' in markup
            assert '<script' not in markup, 'prefetch must not mount pages or trigger automatic writes'
            assert 'id="startup"' not in markup, 'route changes must not bring the startup curtain back'
        assert page.text.index('<template ') < page.text.index('<script src="assets/navigation.js')
        assert 'href="assets/advisor.css?v=' in page.text
        assert page.text.count('id="startup"') == 1
        assert 'data-ct-starting' in page.text
        assert page.text.index('assets/startup.js') < page.text.index('assets/i18n.js')
        assert 'href="assets/startup.css?v=' in page.text
    assert 'data-ct-page=' not in client.get('/ui/login.html').text


def test_editing_an_embedded_layout_invalidates_the_host_page_etag(tmp_path):
    from fastapi import FastAPI

    from agentic_trading.api.app import ConsoleFiles

    ui = tmp_path / 'ui'
    ui.mkdir()
    for name in ('index', 'portfolio', 'advisor', 'more'):
        (ui / f'{name}.html').write_text('<html><head><title>Page</title></head><body>'
                                       '<main id="content">Original</main>'
                                       '<script src="assets/i18n.js"></script></body></html>')
    probe = FastAPI()
    probe.mount('/ui', ConsoleFiles(ui))
    with TestClient(probe) as client:
        original = client.get('/ui/index.html')
        target = ui / 'portfolio.html'
        target.write_text(target.read_text().replace('Original', 'Updated holdings layout'))
        changed = client.get('/ui/index.html', headers={'If-None-Match': original.headers['etag']})
        assert changed.status_code == 200
        assert changed.headers['etag'] != original.headers['etag']
        assert 'Updated holdings layout' in changed.text
