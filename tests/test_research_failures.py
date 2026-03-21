"""Research that did not finish: told apart, recovered where it can be, and run from the release.

One defect, read off the runs of 2026-10-02: the worker's traceback named the checkout's own
source under a service started from a release, because ``python -m`` puts the working directory
-- the data directory, which is the checkout -- first on the import path.

Fully offline: no worker process is started except the one that proves which package a launch
imports.
"""

import os
import subprocess
import sys
from pathlib import Path

from agentic_trading import research_jobs
from agentic_trading.research_jobs import ResearchJobManager, worker_command


def _manager(tmp_path, **settings):
    config = tmp_path / "config"
    config.mkdir(exist_ok=True)
    return ResearchJobManager(
        state_source=tmp_path / "state" / "proposals.json", project_dir=tmp_path,
        config_dir=config, settings={"max_concurrent": 2, "timeout_seconds": 120,
                                     "max_attempts": 2, **settings})


# ---------------------------------------------------------------- which code a worker runs
def test_a_worker_is_started_with_the_working_directory_off_the_import_path(tmp_path, monkeypatch):
    command = worker_command(tmp_path / "config", "2026-10-02_AMZN_55ed6631")
    assert command[:4] == [sys.executable, "-P", "-m", "agentic_trading.research_worker"]
    assert command[-2:] == ["--run-key", "2026-10-02_AMZN_55ed6631"]

    manager = _manager(tmp_path)
    manager.create(run_key="job", ticker="VTI", bucket="core", trade_date="2026-10-02",
                   instrument_type="etf", trigger="manual")
    launched = []

    class Process:
        pid = 4321

    monkeypatch.setattr(research_jobs.subprocess, "Popen",
                        lambda cmd, **kwargs: launched.append((cmd, kwargs)) or Process())
    monkeypatch.setattr(research_jobs, "_scrubbed_environment", dict)
    assert manager.dispatch() == ["job"]
    cmd, kwargs = launched[0]
    assert cmd == worker_command(manager.config_dir, "job")
    # The working directory is still the data directory: relative state paths depend on it.
    assert Path(kwargs["cwd"]) == manager.project_dir
    # And a worker the supervisor asks about is still recognised as its own.
    monkeypatch.setattr(research_jobs, "_worker_command", lambda _pid: " ".join(cmd))
    monkeypatch.setattr(research_jobs.os, "kill", lambda _pid, _sig: None)
    assert research_jobs._worker_state(4321, "job") == research_jobs.WORKER_OURS


def test_the_flag_is_what_keeps_a_package_in_the_data_directory_from_being_imported(tmp_path):
    """The mechanism itself, with a real interpreter, in a directory shaped like the checkout.

    The data directory of a release install is the developer's checkout, which holds a package
    of the same name as the installed one. Started there with ``-m`` or ``-c``, the interpreter
    imports that one. ``-P`` is the difference, and this is what it was on 2026-10-02.
    """
    decoy = tmp_path / "agentic_trading"
    decoy.mkdir()
    (decoy / "__init__.py").write_text("raise SystemExit(97)\n", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONSAFEPATH", "PYTHONPATH")}

    def start(*flags):
        return subprocess.run([sys.executable, *flags, "-c", "import agentic_trading"],
                              cwd=tmp_path, env=env, capture_output=True, text=True,
                              timeout=60, check=False).returncode

    assert start() == 97, "without the flag the working directory's package wins"
    assert start("-P") == 0, "with it, the installed package is the one imported"
    assert "-P" in worker_command(tmp_path, "any")
