from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def console_auth(monkeypatch, tmp_path):
    from importlib import import_module

    from conservative_trading.api.auth import ConsoleAuth, configure_password

    path = tmp_path / "auth.json"
    configure_password(path, "test-owner", "test-only-password-for-console")
    auth = ConsoleAuth(path, ["https://testserver"])
    monkeypatch.setattr(import_module("conservative_trading.api.app"), "_auth", auth)
    return auth


@pytest.fixture
def auth_client(console_auth):
    from fastapi.testclient import TestClient

    from conservative_trading.api.app import app

    client = TestClient(app, base_url="https://testserver")
    client.headers.update({"Origin": "https://testserver", "X-CT-Request": "1"})
    response = client.post("/auth/login", json={
        "username": "test-owner", "password": "test-only-password-for-console"})
    assert response.status_code == 200
    client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
    yield client
    client.close()


@pytest.fixture
def fake_claude(monkeypatch, tmp_path):
    script = ROOT / "tests" / "fake_claude.py"
    script.chmod(0o755)
    monkeypatch.setenv("CT_CLAUDE_BIN", str(script))
    monkeypatch.delenv("CLAUDECODE", raising=False)
    return script


@pytest.fixture
def profiles():
    from conservative_trading.profile import load_profiles

    return load_profiles(ROOT / "config")


@pytest.fixture
def config_dir():
    return ROOT / "config"


@pytest.fixture
def real_audit_guard():
    """Offline tests must never land a fake CLI call in the real runs/llm_calls.jsonl.

    A live run may legitimately append real calls while the suite runs, so the check is
    on the nature of new lines, not their count: a fake call returns in well under a
    second, a real subscription call never does.
    """
    import json

    path = ROOT / "runs" / "llm_calls.jsonl"

    def lines():
        return path.read_text().splitlines() if path.exists() else []

    before = len(lines())
    yield
    new_lines = lines()[before:]
    fake = [ln for ln in new_lines if json.loads(ln).get("wall_seconds", 99) < 1.0]
    assert not fake, f"an offline test appended {len(fake)} fake call(s) to the real audit trail"


@pytest.fixture(autouse=True)
def real_state_guard(monkeypatch):
    """Offline tests must never touch the real state/ directory.

    The audit trail behind real money has to be trustworthy, so a test that appends a fake fill
    or proposal to it is a defect, not an inconvenience. Two guards, because they fail for
    different reasons:

    * the live SQLite family is blocked at the point of connection, so the test that reached for
      it fails there instead of silently succeeding. Comparing bytes afterwards cannot work: a
      deployed API checkpoints its own WAL while the suite runs, which is not a test writing.
    * every other file in state/ is compared before and after, which also catches a new file.
    """
    from conservative_trading import state_db

    live_directory = (ROOT / "state").resolve()

    def refuse(database: state_db.StateDB):
        raise AssertionError(
            f"an offline test opened the real state database at {database.path}; "
            "point the fixture at tmp_path instead")

    for name in ("_connect", "_read"):
        original = getattr(state_db.StateDB, name)
        monkeypatch.setattr(state_db.StateDB, name, (
            lambda self, _original=original: refuse(self)
            if self.path.parent == live_directory else _original(self)))

    def guarded(path: Path) -> bool:
        return path.is_file() and not path.name.startswith("trading_state.sqlite3")

    watched = {p: p.read_bytes() for p in (ROOT / "state").glob("*") if guarded(p)}
    yield
    for path, before in watched.items():
        after = path.read_bytes() if path.exists() else None
        assert after == before, f"an offline test modified the real state file {path.name}"
    for path in (ROOT / "state").glob("*"):
        if guarded(path) and path not in watched:
            raise AssertionError(f"an offline test created the real state file {path.name}")
