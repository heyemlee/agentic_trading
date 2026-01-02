from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


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
def real_state_guard():
    """Offline tests must never write into the real state/ directory.

    The audit trail behind real money has to be trustworthy, so a test that appends a fake fill
    or proposal to it is a defect, not an inconvenience.
    """
    watched = {p: (p.read_bytes() if p.exists() else None)
               for p in (ROOT / "state").glob("*") if p.is_file()}
    yield
    for path, before in watched.items():
        after = path.read_bytes() if path.exists() else None
        assert after == before, f"an offline test modified the real state file {path.name}"
    for path in (ROOT / "state").glob("*"):
        if path.is_file() and path not in watched:
            raise AssertionError(f"an offline test created the real state file {path.name}")
