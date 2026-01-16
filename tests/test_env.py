"""Secrets that must outlive a reboot: where they are read from, and who reads them.

The failure this guards against is silent by construction. `launchctl setenv` does not survive
a restart, and nothing reports the loss: macro data returns to "unavailable" and the off-site
backup stops copying, both of which look like the system working as designed.
"""
from __future__ import annotations

import subprocess
import sys
import tomllib

import pytest

from agentic_trading.env import env_path, load_env
from tests.conftest import ROOT


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.delenv("CT_DOTENV_PROBE", raising=False)
    return tmp_path


def write(project, text):
    (project / ".env").write_text(text)
    return project


def test_values_reach_the_environment_by_name_not_by_working_directory(project, monkeypatch):
    write(project, "CT_DOTENV_PROBE=reached\n")
    monkeypatch.chdir(ROOT)                       # deliberately somewhere else
    assert load_env(project) == ["CT_DOTENV_PROBE"]
    import os
    assert os.environ["CT_DOTENV_PROBE"] == "reached"


def test_an_explicit_value_in_the_environment_is_never_overruled(project, monkeypatch):
    monkeypatch.setenv("CT_DOTENV_PROBE", "from the command")
    write(project, "CT_DOTENV_PROBE=from the file\n")
    assert load_env(project) == []
    import os
    assert os.environ["CT_DOTENV_PROBE"] == "from the command"


@pytest.mark.parametrize("line", ["CT_DOTENV_PROBE=", "CT_DOTENV_PROBE=   ", "# CT_DOTENV_PROBE=x",
                                  "", "no equals sign here"])
def test_a_placeholder_is_not_configuration(project, line):
    """.env.example ships empty assignments; they must not read as a key that exists."""
    write(project, line + "\n")
    assert load_env(project) == []


def test_quotes_and_comments_are_handled_the_way_a_person_would_write_them(project):
    write(project, '# a comment\n\nCT_DOTENV_PROBE="quoted value"\n')
    assert load_env(project) == ["CT_DOTENV_PROBE"]
    import os
    assert os.environ["CT_DOTENV_PROBE"] == "quoted value"


def test_a_missing_file_is_not_an_error(project):
    assert load_env(project) == [] and not env_path(project).exists()


def test_a_world_readable_file_is_reported(project, caplog):
    write(project, "CT_DOTENV_PROBE=reached\n").joinpath(".env").chmod(0o644)
    with caplog.at_level("WARNING"):
        load_env(project)
    assert any("readable beyond its owner" in r.getMessage() for r in caplog.records)


def test_every_console_script_loads_it():
    """The gap this closes: only the research entry point saw .env, because it was the only one
    that imported the upstream package. The backup job, which needs CT_BACKUP_MIRROR, did not."""
    scripts = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["scripts"]
    missing = []
    for name, target in sorted(scripts.items()):
        module, _, func = target.partition(":")
        source = (ROOT / (module.replace(".", "/") + ".py")).read_text()
        body = source.split(f"def {func}(", 1)[1][:400]
        if "load_env()" not in body:
            missing.append(f"{name} ({target})")
    assert missing == [], "these entry points would not see .env"


def test_the_backup_job_really_sees_the_file(tmp_path):
    """One end-to-end case behind the source scan: ct-ops is the entry point that was broken."""
    (tmp_path / ".env").write_text("CT_DOTENV_PROBE=reached\n")
    script = ("import os; from agentic_trading.env import load_env; load_env(); "
              "print(os.environ.get('CT_DOTENV_PROBE', '<absent>'))")
    out = subprocess.run(
        [sys.executable, "-c", script], check=False,
        cwd=tmp_path, capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(ROOT), "CT_DATA_DIR": str(tmp_path)})
    assert out.stdout.strip() == "reached", out.stderr
