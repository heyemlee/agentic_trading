"""Unattended-operation checks: heartbeats, log rotation, retention and the deployment jobs."""
from __future__ import annotations

import os
import shutil
import time
from datetime import UTC, datetime, timedelta

import pytest

from conservative_trading import launchd, operations
from conservative_trading.api.service import Service
from tests.conftest import ROOT


@pytest.fixture
def service(tmp_path):
    cfg = tmp_path / "config"
    shutil.copytree(ROOT / "config", cfg)
    shutil.copy(ROOT / "MANDATE.md", tmp_path / "MANDATE.md")
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
    api = jobs[launchd.API]
    assert api["WorkingDirectory"] == str(data.resolve())
    assert api["ProgramArguments"] == [str((release / "deploy/macos/run_api.sh").resolve())]
    assert api["EnvironmentVariables"]["CT_DATA_DIR"] == str(data.resolve())
    assert api["EnvironmentVariables"]["CT_RELEASE_DIR"] == str(release.resolve())
    assert api["EnvironmentVariables"]["CT_CONFIG_DIR"] == str((data / "config").resolve())
    assert set(jobs) == set(launchd.LABELS)


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
