"""Exercise the HTTP security boundary without a broker or model connection."""
import time
from importlib import import_module
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from conservative_trading.api.app import app
from conservative_trading.api.auth import COOKIE, ConsoleAuth, configure_password


@pytest.mark.parametrize('path', ['/health', '/portfolio', '/risk', '/orders', '/proposals', '/research', '/openapi.json'])
def test_business_reads_require_login(console_auth, path):
    client = TestClient(app, base_url='https://testserver')
    assert client.get(path).status_code == 401


def test_missing_configuration_is_closed(monkeypatch, tmp_path):
    module = import_module('conservative_trading.api.app')
    monkeypatch.setattr(module, '_auth', ConsoleAuth(tmp_path / 'absent', ['https://testserver']))
    client = TestClient(app, base_url='https://testserver')
    assert client.get('/portfolio').status_code == 503
    assert client.get('/ui/').status_code == 200  # redirected to the login screen
    assert client.get('/auth/session').json()['configured'] is False


def test_external_origin_empty_approval_and_resume_never_reach_service(auth_client, monkeypatch):
    calls = []
    module = import_module('conservative_trading.api.app')
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


def test_login_cookie_logout_and_headers(console_auth):
    client = TestClient(app, base_url='https://testserver', headers={
        'Origin': 'https://testserver', 'X-CT-Request': '1'})
    result = client.post('/auth/login', json={'username': 'test-owner', 'password': 'test-only-password-for-console'})
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
    assert ui.headers['cache-control'] == 'no-store'
    assert ui.headers['x-frame-options'] == 'DENY'


def test_password_rotation_revokes_sessions(auth_client, console_auth):
    configure_password(console_auth.path, 'test-owner', 'replacement-test-password')
    assert auth_client.get('/portfolio').status_code == 401


def test_session_survives_auth_object_restart(auth_client, console_auth, monkeypatch):
    module = import_module('conservative_trading.api.app')
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
    assert console_auth.login('test-owner', 'test-only-password-for-console')[1] == 'rate_limited'


def test_identity_is_from_session_not_payload(auth_client, monkeypatch):
    calls = []
    module = import_module('conservative_trading.api.app')
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


def sign_in(console_auth, agent, password='test-only-password-for-console'):
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
    from conservative_trading.api import auth as auth_module

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
    configure_password(console_auth.path, 'test-owner', 'a-replacement-password-here')
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
    assert console_auth.sessions() == []           # nothing to revoke until it is seen again

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

    from conservative_trading.api.auth import ConsoleAuth, configure_password
    from conservative_trading.state_db import StateDB

    path = tmp_path / 'auth.json'
    configure_password(path, 'test-owner', 'test-only-password-for-console')
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
    assert auth.sessions() == []          # not yet revocable, and not an error either
    with sqlite3.connect(database) as connection:
        columns = {row[1] for row in connection.execute('PRAGMA table_info(console_sessions)')}
    assert {'session_id', 'label'} <= columns
    assert connection.execute('SELECT count(*) FROM console_sessions').fetchone()[0] == 1


def test_an_unreadable_session_table_is_an_error_not_an_empty_device_list(console_auth, monkeypatch):
    import sqlite3

    sign_in(console_auth, LAPTOP)
    with console_auth.db.transaction() as connection:
        connection.execute('ALTER TABLE console_sessions DROP COLUMN label')
    monkeypatch.setattr(console_auth.db, 'ensure_schema', lambda: False)

    with pytest.raises(sqlite3.OperationalError):
        console_auth.sessions()
