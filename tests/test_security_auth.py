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
