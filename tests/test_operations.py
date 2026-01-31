"""Unattended-operation checks: heartbeats, log rotation, retention and the deployment jobs."""
from __future__ import annotations

import json
import os
import shutil
import time
from datetime import UTC, datetime, timedelta

import pytest

from agentic_trading import launchd, operations
from agentic_trading.service import Service
from tests.conftest import ROOT


@pytest.fixture
def service(tmp_path):
    cfg = tmp_path / "config"
    shutil.copytree(ROOT / "config", cfg)
    (tmp_path / "state").mkdir()
    (tmp_path / "runs").mkdir()
    return Service(cfg)


def test_an_activity_that_never_ran_is_overdue_not_healthy(service):
    """An install whose backup job was never loaded must not look identical to a working one."""
    report = service.operations()
    by_id = {row["id"]: row for row in report["activities"]}
    assert set(by_id) == set(operations.ACTIVITIES)
    assert all(row["overdue"] for row in report["activities"])
    assert all(row["last_success_at"] is None for row in report["activities"])

    service.record_activity("backup", "verified", min_interval_seconds=0)
    fresh = {row["id"]: row for row in service.operations()["activities"]}["backup"]
    assert fresh["overdue"] is False and fresh["detail"] == "verified"


def test_a_stale_heartbeat_is_reported_late_with_its_age(service):
    db = service.state_db()
    stale = (datetime.now(UTC) - timedelta(hours=50)).isoformat()
    db.write_meta(operations.HEARTBEATS, {"backup": {"at": stale, "detail": "old"}})

    row = {r["id"]: r for r in service.operations()["activities"]}["backup"]
    assert row["overdue"] is True
    assert row["age_minutes"] > 2 * row["expected_every_minutes"]


def test_heartbeats_are_throttled_so_a_15_second_loop_is_not_15_second_disk_traffic(service):
    service.record_activity("reconciliation", "first", min_interval_seconds=0)
    first = {r["id"]: r for r in service.operations()["activities"]}["reconciliation"]
    service.record_activity("reconciliation", "second", min_interval_seconds=3600)
    again = {r["id"]: r for r in service.operations()["activities"]}["reconciliation"]
    assert again["last_success_at"] == first["last_success_at"] and again["detail"] == "first"


def test_a_heartbeat_read_and_write_share_one_transaction(service):
    """Separate processes write this one document, so the read-modify-write must be serialised.

    Every activity lives in a single JSON document that each writer rewrites whole. While the
    read sat outside the transaction, the API's own heartbeat could overwrite one another
    process had just recorded. That is why `schedule`, written weekly by ct-trigger, was
    missing from a live document with 17,866 revisions.
    """
    db = service.state_db()
    inside = {}
    unlocked = db.read_meta

    def watched(name, default=None):
        inside[name] = db.current() is not None
        return unlocked(name, default)

    db.read_meta = watched
    operations.record(db, "schedule", "weekly", min_interval_seconds=0)
    assert inside[operations.HEARTBEATS] is True


def test_recording_one_activity_keeps_every_other_activity(service):
    """A whole-document rewrite must merge, never replace: four writers share this record."""
    for activity in ("backup", "maintenance", "schedule"):
        service.record_activity(activity, f"{activity} ran", min_interval_seconds=0)
    service.record_activity("reconciliation", "0 order(s) checked", min_interval_seconds=0)

    rows = {r["id"]: r for r in service.operations()["activities"]}
    assert [rows[a]["detail"] for a in ("backup", "maintenance", "schedule")] == [
        "backup ran", "maintenance ran", "schedule ran"]


def test_ct_ops_status_exits_non_zero_while_an_activity_is_late(service, capsys):
    """selfcheck.sh decides pass or fail from this exit code, so staleness must reach it.

    On 2026-09-15 the API spent twenty minutes unable to open its own state database while
    selfcheck reported "all checks passed": it printed the heartbeat ages and then ignored
    them, because status returned zero however old they were.
    """
    assert operations.main(["status", "--config-dir", str(service.config_dir)]) == 1
    report = json.loads(capsys.readouterr().out)
    assert set(report["late"]) == set(operations.ACTIVITIES)

    for activity in operations.ACTIVITIES:
        service.record_activity(activity, "just ran", min_interval_seconds=0)

    assert operations.main(["status", "--config-dir", str(service.config_dir)]) == 0
    assert json.loads(capsys.readouterr().out)["late"] == []


def test_ct_ops_status_fails_loudly_when_the_activities_cannot_be_read(service, capsys, monkeypatch):
    """State that cannot be read is a failed check, never a quiet pass.

    This is the case the outage actually produced: the database was unreachable, so the very
    reading that would have revealed it was the thing that broke.
    """
    def unreadable(self):
        raise RuntimeError("state database is unavailable")

    monkeypatch.setattr(Service, "operations", unreadable)

    assert operations.main(["status", "--config-dir", str(service.config_dir)]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["late"] and "state database is unavailable" in report["activities"]


def test_log_rotation_leaves_the_path_launchd_writes_to(tmp_path):
    logs = tmp_path / "runs" / "service"
    logs.mkdir(parents=True)
    (logs / "api.stdout.log").write_bytes(b"x" * 4096)
    (logs / "api.stderr.log").write_bytes(b"y" * 10)

    assert operations.rotate_logs(logs, max_bytes=1024, keep=3) == ["api.stdout.log"]
    assert (logs / "api.stdout.log").stat().st_size == 0          # launchd keeps appending here
    assert (logs / "api.stdout.log.1").stat().st_size == 4096
    assert (logs / "api.stderr.log").stat().st_size == 10          # under the size, left alone


def test_rotation_keeps_only_the_configured_number_of_generations(tmp_path):
    logs = tmp_path / "runs" / "service"
    logs.mkdir(parents=True)
    for round_number in range(5):
        (logs / "api.stdout.log").write_bytes(bytes([round_number]) * 4096)
        operations.rotate_logs(logs, max_bytes=1024, keep=3)
    generations = sorted(p.name for p in logs.glob("api.stdout.log.*"))
    assert generations == ["api.stdout.log.1", "api.stdout.log.2", "api.stdout.log.3"]


def test_pruning_drops_report_bodies_and_keeps_the_audit_record(tmp_path):
    runs = tmp_path / "runs"
    old, recent = runs / "2020-01-01_VTI_a", runs / "2026-09-01_VTI_b"
    for run in (old, recent):
        (run / "reports").mkdir(parents=True)
        (run / "reports" / "market.md").write_text("a report")
        (run / "decision_record.json").write_text("{}")
    stamp = time.time() - 400 * 86400
    os.utime(old, (stamp, stamp))

    assert operations.prune_runs(runs, retention_days=120) == [old.name]
    assert not (old / "reports").exists()
    assert (old / "decision_record.json").exists()      # the audit trail survives retention
    assert (recent / "reports" / "market.md").exists()


def test_reinstalling_without_an_origin_keeps_the_private_https_entry_point(tmp_path):
    """A plain rerun after a code change must not lock the phone out."""
    agents = tmp_path / "LaunchAgents"
    remote = "https://mac.tail1234.ts.net"
    launchd.write(agents, launchd.render(
        data_dir=tmp_path, home=tmp_path,
        origins=launchd.allowed_origins(origin=remote, keep=[])))
    assert launchd.installed_origins(agents) == [remote]

    launchd.main(["--data-dir", str(tmp_path), "--launch-agents", str(agents),
                  "--home", str(tmp_path)])
    assert launchd.installed_origins(agents) == [remote]

    launchd.main(["--data-dir", str(tmp_path), "--launch-agents", str(agents),
                  "--home", str(tmp_path), "--clear-origins"])
    assert launchd.installed_origins(agents) == []


def test_a_release_layout_runs_pinned_code_against_the_service_data(tmp_path):
    """The service keeps running one version while the checkout it came from is edited."""
    data, release = tmp_path / "data", tmp_path / "releases" / "2026-09-04"
    jobs = launchd.render(data_dir=data, release_dir=release, home=tmp_path,
                          origins=list(launchd.LOOPBACK))
    current = release.parent / "current"
    api = jobs[launchd.API]
    assert api["WorkingDirectory"] == str(data.resolve())
    assert api["ProgramArguments"] == [str(current / "deploy/macos/run_api.sh")]
    assert api["EnvironmentVariables"]["CT_DATA_DIR"] == str(data.resolve())
    assert api["EnvironmentVariables"]["CT_RELEASE_DIR"] == str(current)
    assert api["EnvironmentVariables"]["CT_CONFIG_DIR"] == str((data / "config").resolve())
    assert set(jobs) == set(launchd.LABELS)


def test_two_releases_render_byte_identical_jobs_through_the_current_symlink(tmp_path):
    """A routine publish must not look like re-registering the LaunchAgent to macOS.

    release.sh publishes a new, differently-named release directory every time. Pointing the
    job at that specific path meant every publish produced a different plist, which macOS's
    background-task manager treats as the LaunchAgent being re-registered. Enough of those in
    a short span trips its notification throttle: `launchctl bootstrap` keeps reporting success
    while the job silently never starts (`log show` records "Exceeded max notifications for
    run_api.sh"). This happened for real on 2026-09-05, on all four jobs at once, after two
    publishes inside an hour. Referencing the stable `current` symlink instead means an ordinary
    publish (same data directory, same origins) changes nothing about the plist at all — only
    the symlink's target moves, and a restart (`launchctl kickstart`) is what makes the new code
    active without touching launchd's registration.
    """
    data = tmp_path / "data"
    releases = tmp_path / "releases"
    jobs_a = launchd.render(data_dir=data, release_dir=releases / "20260101T000000Z-aaa1111",
                            home=tmp_path, origins=list(launchd.LOOPBACK))
    jobs_b = launchd.render(data_dir=data, release_dir=releases / "20260102T000000Z-bbb2222",
                            home=tmp_path, origins=list(launchd.LOOPBACK))
    assert jobs_a == jobs_b
    for jobs in (jobs_a, jobs_b):
        for label in launchd.LABELS:
            assert str(releases / "current") in jobs[label]["ProgramArguments"][0]

    # a single-checkout install (no release layout) keeps referencing its own directory
    single = launchd.render(data_dir=data, home=tmp_path, origins=list(launchd.LOOPBACK))
    assert single[launchd.API]["ProgramArguments"] == [str(data / "deploy/macos/run_api.sh")]


def test_an_invalid_remote_origin_is_refused_rather_than_installed():
    for bad in ("http://mac.example.net", "https://mac.example.net/console", "not-a-url"):
        with pytest.raises(ValueError, match="https://"):
            launchd.allowed_origins(origin=bad, keep=[])


def test_the_deployment_scripts_do_not_hard_code_a_checkout(tmp_path):
    """Every script resolves its release and data directories the same way."""
    scripts = sorted((ROOT / "deploy" / "macos").glob("*.sh"))
    assert {p.name for p in scripts} >= {"common.sh", "run_api.sh", "backup_state.sh",
                                         "maintenance.sh", "install.sh", "selfcheck.sh"}
    for script in scripts:
        text = script.read_text()
        assert str(ROOT) not in text, f"{script.name} hard-codes this checkout"
        # install.sh, uninstall.sh and release.sh manage the layout rather than run inside it.
        if script.name not in ("common.sh", "install.sh", "uninstall.sh", "release.sh"):
            assert 'source "$(dirname "$0")/common.sh"' in text, script.name


def command_lines(text: str) -> list[str]:
    """Shell commands, with line continuations joined, so a flag split across lines is seen."""
    joined = text.replace("\\\n", " ")
    return [line.strip() for line in joined.splitlines()
            # A comment or a message that merely names a command is not an invocation.
            if line.strip() and not line.strip().startswith(("#", "print", "echo"))]


#: Which argument parser serves each command the deployment scripts invoke.
PARSERS = {
    "agentic_trading.launchd": "agentic_trading.launchd",
    "ct-ops": "agentic_trading.operations",
    "agentic_trading.operations": "agentic_trading.operations",
    "ct-state": "agentic_trading.state_cli",
    "ct-trigger": "agentic_trading.triggers",
    "ct-notify": "agentic_trading.notify_cli",
    "ct-api": "agentic_trading.api.app",
}


def parser_options(module_name: str) -> set[str]:
    """The long options a command's parser accepts, without running the command."""
    import argparse
    import importlib
    from unittest.mock import patch

    module = importlib.import_module(module_name)
    captured: set[str] = set()
    real_add = argparse.ArgumentParser.add_argument

    def record(self, *args, **kwargs):
        captured.update(arg for arg in args if str(arg).startswith("--"))
        return real_add(self, *args, **kwargs)

    class Stop(Exception):
        pass

    with patch.object(argparse.ArgumentParser, "add_argument", record), \
            patch.object(argparse.ArgumentParser, "parse_args",
                         side_effect=Stop), patch.object(argparse.ArgumentParser, "error"):
        for call in (lambda: module.main([]), module.main):   # some mains take no argv
            try:
                call()
            except TypeError:
                continue
            except (Stop, SystemExit, Exception):    # noqa: BLE001 - only the flags matter
                break
            break
    return captured


def test_every_flag_the_deployment_scripts_pass_is_actually_accepted():
    """The installer once passed --project-dir to a parser that had renamed it --data-dir.

    Nothing failed until it was run on a real machine, because no test connected what the
    scripts type to what the commands accept. This is that connection.
    """
    checked = 0
    for script in sorted((ROOT / "deploy").rglob("*.sh")):
        for line in command_lines(script.read_text()):
            for token, module_name in PARSERS.items():
                if token not in line:
                    continue
                accepted = parser_options(module_name)
                assert accepted, f"could not read the parser for {token}"
                used = {word.split("=")[0] for word in line.split()
                        if word.startswith("--") and word != "--"}   # -- ends options
                unknown = used - accepted
                assert not unknown, (
                    f"{script.name} passes {sorted(unknown)} to {token}, "
                    f"which accepts {sorted(accepted)}")
                checked += 1
    assert checked >= 8, f"only {checked} invocations were checked; the scan missed some"


def test_a_script_run_by_hand_checks_the_deployment_not_the_checkout(tmp_path):
    """After a release install, "is the service healthy" is about the release.

    launchd exports the layout when it starts a job. Run from a terminal there is no such
    environment, and falling back to the script's own checkout made selfcheck.sh report on
    whatever the developer was editing while claiming to describe the running service.
    """
    import subprocess

    if not (shutil.which("zsh") and shutil.which("plutil")):
        pytest.skip("zsh and plutil are required for the deployment scripts")

    home = tmp_path / "home"
    data, release = tmp_path / "service-data", tmp_path / "releases" / "r1"
    current = release.parent / "current"
    (data / "config").mkdir(parents=True)
    (release / ".venv" / "bin").mkdir(parents=True)
    (release / ".venv" / "bin" / "python").write_text("#!/bin/sh\n")
    (release / ".venv" / "bin" / "python").chmod(0o755)
    current.symlink_to(release)             # release.sh always flips this before common.sh runs
    launchd.write(home / "Library" / "LaunchAgents",
                  launchd.render(data_dir=data, release_dir=release, home=home,
                                 origins=list(launchd.LOOPBACK)))

    # A script living in the checkout, invoked with no CT_* environment at all.
    checkout = tmp_path / "checkout" / "deploy" / "macos"
    checkout.mkdir(parents=True)
    shutil.copy(ROOT / "deploy" / "macos" / "common.sh", checkout / "common.sh")
    probe = checkout / "probe.sh"
    probe.write_text('#!/bin/zsh\nset -euo pipefail\nsource "$(dirname "$0")/common.sh"\n'
                     'print -r -- "$release_dir"\nprint -r -- "$data_dir"\n')
    probe.chmod(0o755)

    environment = {"HOME": str(home), "PATH": os.environ["PATH"]}
    result = subprocess.run(["zsh", str(probe)], capture_output=True, text=True,
                            env=environment, check=True, timeout=30)
    # The plist carries the stable symlink, not the specific release, so that a routine publish
    # does not change it (see test_two_releases_render_byte_identical_jobs_through_the_current_symlink).
    assert result.stdout.split() == [str(current), str(data.resolve())]

    # With no installed job the old behaviour stands: the script's own checkout.
    shutil.rmtree(home / "Library" / "LaunchAgents")
    (tmp_path / "checkout" / ".venv" / "bin").mkdir(parents=True)
    (tmp_path / "checkout" / ".venv" / "bin" / "python").write_text("#!/bin/sh\n")
    (tmp_path / "checkout" / ".venv" / "bin" / "python").chmod(0o755)
    (tmp_path / "checkout" / "config").mkdir()
    fallback = subprocess.run(["zsh", str(probe)], capture_output=True, text=True,
                              env=environment, check=True, timeout=30)
    assert fallback.stdout.split()[0] == str((tmp_path / "checkout").resolve())
