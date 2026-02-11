"""Backup, encrypted second copy, restore, and surviving a move to another directory.

The failure these guard against is not losing the file. It is restoring one and quietly getting
a portfolio that no longer matches its own ledger, or a session that was signed out months ago,
or an order the broker settled while the snapshot was cold.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import time
from pathlib import Path

import pytest

from agentic_trading import state_cli
from agentic_trading.execution_scenarios import _proposal
from agentic_trading.portfolio import load_portfolio, migrate_portfolio
from agentic_trading.proposals import (
    APPROVED,
    CANCELED,
    EXECUTED,
    PARTIALLY_FILLED,
    PROPOSED,
    REJECTED,
    SUBMITTED,
    SUBMITTING,
    UNKNOWN,
    ProposalStore,
)
from agentic_trading.state_db import StateDB
from tests.conftest import ROOT, copy_example_ledger


@pytest.fixture
def project(tmp_path):
    """A complete state directory: config, a ledger, a proposal and a ladder document."""
    cfg = tmp_path / "config"
    shutil.copytree(ROOT / "config", cfg)
    state = tmp_path / "state"
    state.mkdir()
    (tmp_path / "runs").mkdir()
    copy_example_ledger("portfolio.example.yaml", state / "portfolio.yaml")
    migrate_portfolio(state / "portfolio.yaml")
    ProposalStore(state / "proposals.json").save(_proposal(status=PROPOSED))
    StateDB(state / "proposals.json").write_document(state / "risk_state.json",
                                                     {"total": {"high_water_mark_usd": 3000.0}})
    return tmp_path


def key_file(tmp_path) -> Path:
    path = tmp_path / "backup.key"
    path.write_text("a-passphrase-long-enough-to-matter")
    path.chmod(0o600)
    return path


def run(argv) -> int:
    return state_cli.main(argv)


def test_state_keys_survive_a_move_to_another_directory(project, tmp_path):
    """A restored database must still know which ledger its documents belong to.

    Absolute keys made a backup useless anywhere but the directory it was taken in: the ledger
    would read as missing, and the loader would silently fall back to a stale YAML file.
    """
    source_db = StateDB(project / "state" / "proposals.json")
    assert source_db.source_key(project / "state" / "portfolio.yaml") == "portfolio.yaml"

    elsewhere = tmp_path / "restored-elsewhere"
    elsewhere.mkdir()
    shutil.copyfile(source_db.path, elsewhere / "trading_state.sqlite3")

    moved = StateDB(elsewhere / "proposals.json")
    assert moved.read_document(elsewhere / "portfolio.yaml") is not None
    assert load_portfolio(elsewhere / "portfolio.yaml").buckets["core"].cash_usd > 0
    assert len(ProposalStore(elsewhere / "proposals.json").all()) == 1


def test_a_database_written_with_absolute_keys_is_adopted_on_first_open(tmp_path):
    """Databases in the field were written before keys were relative."""
    state = tmp_path / "state"
    state.mkdir()
    db = StateDB(state / "proposals.json")
    legacy = str((state / "portfolio.yaml").resolve())
    with db.transaction() as conn:
        conn.execute("INSERT INTO documents VALUES(?,?,1,'2026-01-01')",
                     (legacy, json.dumps({"as_of": "2026-01-01", "buckets": {}})))
        conn.execute("INSERT INTO migrations VALUES(?,?,?,'2026-01-01')",
                     (legacy, "portfolio", None))

    import agentic_trading.state_db as module
    module._relinked.clear()               # a fresh process opening the moved database

    reopened = StateDB(state / "proposals.json")
    assert reopened.read_document(state / "portfolio.yaml") is not None
    with reopened.transaction() as conn:
        keys = [row[0] for row in conn.execute("SELECT namespace FROM documents")]
    assert keys == ["portfolio.yaml"]      # rewritten, not merely readable


def test_backup_verifies_the_copy_and_prunes_by_age(project):
    backups = project / "state" / "backups"
    backups.mkdir()
    stale = backups / "trading_state_20200101T000000Z.sqlite3"
    stale.write_bytes(b"not a database")
    old = time.time() - 90 * 86400
    import os
    os.utime(stale, (old, old))

    assert run(["backup", "--config-dir", str(project / "config"), "--verify",
                "--retain-days", "30"]) == 0
    copies = sorted(backups.glob("trading_state_*.sqlite3"))
    assert len(copies) == 1 and copies[0] != stale        # the aged copy was pruned
    with sqlite3.connect(copies[0]) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_an_off_machine_copy_is_encrypted_and_needs_the_passphrase(project, tmp_path):
    """A copy that leaves this Mac protects against losing the Mac, so it travels encrypted."""
    if shutil.which("openssl") is None:
        pytest.skip("openssl is required for the encrypted mirror")
    mirror = tmp_path / "mirror"
    mirror.mkdir()                 # the destination must already exist; see the unmounted case
    assert run(["backup", "--config-dir", str(project / "config"), "--verify",
                "--mirror-dir", str(mirror), "--key-file", str(key_file(tmp_path))]) == 0

    copies = list(mirror.glob("trading_state_*.sqlite3.enc"))
    assert len(copies) == 1
    assert not copies[0].read_bytes().startswith(b"SQLite format 3")
    assert copies[0].stat().st_mode & 0o077 == 0


def test_a_mirror_without_a_passphrase_is_refused(project, tmp_path):
    with pytest.raises(SystemExit):
        run(["backup", "--config-dir", str(project / "config"),
             "--mirror-dir", str(tmp_path / "mirror")])


def test_restoring_revokes_sessions_and_names_the_orders_to_check(project, tmp_path, capsys):
    """A snapshot's sessions and open orders are both stale claims about the present."""
    db = StateDB(project / "state" / "proposals.json")
    with db.transaction() as conn:
        # named columns, so adding one later cannot silently break this
        conn.execute("INSERT INTO console_sessions(token_hash,username,csrf,generation,"
                     "created_at,last_seen_at,expires_at) VALUES('hash','owner','csrf','gen',1,1,9e9)")
    # the shape execution leaves behind: lifecycle SUBMITTED, venue status normalised, polled once
    open_order = _proposal(status=SUBMITTED)
    open_order.client_order_id = "ct-open-1"
    open_order.order_status = "accepted"
    open_order.reconciled_at = "2026-09-15T14:31:00+00:00"
    ProposalStore(project / "state" / "proposals.json").save(open_order)

    backup = tmp_path / "snapshot.sqlite3"
    assert run(["backup", "--config-dir", str(project / "config"), "--verify",
                "--output", str(backup)]) == 0

    target = tmp_path / "recovered"
    capsys.readouterr()
    assert run(["restore", "--source", str(backup), "--into", str(target)]) == 0
    report = json.loads(capsys.readouterr().out)

    assert report["integrity"] == "ok"
    assert report["console_sessions_revoked"] == 1
    assert [row["client_order_id"] for row in report["orders_to_verify_at_the_broker"]] == ["ct-open-1"]
    assert any("sign in again" in step for step in report["next_steps"])

    restored = StateDB(target / "proposals.json")
    assert restored.status()["counts"]["console_sessions"] == 0
    # the ledger is still attached to its proposal after the move
    assert load_portfolio(target / "portfolio.yaml").buckets["core"].cash_usd > 0
    assert len(ProposalStore(target / "proposals.json").all()) == 2


# Each row is a state execution.py really writes: (lifecycle status, venue order status as
# broker/robinhood.py normalises it, polled at least once, reconciliation error).
OWNED_BY_THE_BROKER = [
    pytest.param(SUBMITTING, "submitting", False, None, id="intent persisted, venue not answered"),
    pytest.param(SUBMITTED, "new", True, None, id="queued at the venue"),
    pytest.param(SUBMITTED, "accepted", True, None, id="confirmed at the venue"),
    pytest.param(PARTIALLY_FILLED, "partially_filled", True, None, id="partly filled"),
    pytest.param(UNKNOWN, None, False, "submission result unknown: TimeoutError; lookup required",
                 id="submission outcome unknown"),
    pytest.param(UNKNOWN, "accepted", True, "order synchronization failed: timeout",
                 id="poll failed after acceptance"),
    pytest.param(APPROVED, None, False, None, id="pre-upgrade approved order never polled"),
]
SETTLED = [
    pytest.param(EXECUTED, "filled", True, None, id="filled"),
    pytest.param(CANCELED, "canceled", True, None, id="canceled"),
    pytest.param(REJECTED, "rejected", True, None, id="rejected by the venue"),
]


def _stored_order(project, status, order_status, polled, error):
    order = _proposal(status=status)
    order.client_order_id = "ct-order-1"
    order.order_status = order_status
    order.reconciled_at = "2026-09-15T14:31:00+00:00" if polled else None
    order.reconciliation_error = error
    ProposalStore(project / "state" / "proposals.json").save(order)
    return StateDB(project / "state" / "proposals.json")


@pytest.mark.parametrize(("status", "order_status", "polled", "error"), OWNED_BY_THE_BROKER)
def test_the_release_gate_sees_every_order_the_broker_still_owns(project, status, order_status,
                                                                 polled, error, capsys):
    """The gate in front of a release, a restore and a ledger confirmation.

    It once compared the venue's lowercase order status against an uppercase list and let five
    of these six states through as settled, because the one test it had used a status string
    production never writes.
    """
    db = _stored_order(project, status, order_status, polled, error)
    assert [row["client_order_id"] for row in state_cli.unsettled_orders(db)] == ["ct-order-1"]

    capsys.readouterr()
    assert run(["status", "--config-dir", str(project / "config"), "--fail-on-unsettled"]) == 3


@pytest.mark.parametrize(("status", "order_status", "polled", "error"), SETTLED)
def test_the_release_gate_lets_a_settled_order_through(project, status, order_status, polled,
                                                       error, capsys):
    db = _stored_order(project, status, order_status, polled, error)
    assert state_cli.unsettled_orders(db) == []

    capsys.readouterr()
    assert run(["status", "--config-dir", str(project / "config"), "--fail-on-unsettled"]) == 0


def test_restoring_never_silently_replaces_a_live_database(project, tmp_path):
    backup = tmp_path / "snapshot.sqlite3"
    run(["backup", "--config-dir", str(project / "config"), "--output", str(backup)])
    target = tmp_path / "occupied"
    target.mkdir()
    (target / "trading_state.sqlite3").write_bytes(b"existing")

    with pytest.raises(SystemExit):
        run(["restore", "--source", str(backup), "--into", str(target)])
    assert (target / "trading_state.sqlite3").read_bytes() == b"existing"


def test_a_damaged_backup_is_refused_rather_than_restored(tmp_path):
    broken = tmp_path / "broken.sqlite3"
    broken.write_bytes(b"SQLite format 3\x00" + b"\x00" * 200)
    with pytest.raises(SystemExit):
        run(["restore", "--source", str(broken), "--into", str(tmp_path / "out")])
    assert not (tmp_path / "out" / "trading_state.sqlite3").exists()


def test_an_encrypted_backup_round_trips_through_restore(project, tmp_path, capsys):
    if shutil.which("openssl") is None:
        pytest.skip("openssl is required for the encrypted mirror")
    mirror, key = tmp_path / "mirror", key_file(tmp_path)
    mirror.mkdir()
    run(["backup", "--config-dir", str(project / "config"), "--verify",
         "--mirror-dir", str(mirror), "--key-file", str(key)])
    encrypted = next(iter(mirror.glob("*.enc")))

    capsys.readouterr()
    assert run(["restore", "--source", str(encrypted), "--into", str(tmp_path / "from-mirror"),
                "--key-file", str(key)]) == 0
    assert json.loads(capsys.readouterr().out)["integrity"] == "ok"
    assert load_portfolio(tmp_path / "from-mirror" / "portfolio.yaml").buckets["core"].cash_usd > 0


def test_reading_an_older_database_reports_gaps_and_changes_nothing(tmp_path):
    """A read must never migrate the database it is reading.

    Doing so broke the running deployment: a CLI run from a development checkout added two
    session columns to the live database while an older API served it, and that API — inserting
    a session with positional values — then failed every new login against a table with more
    columns than it knew. The database must never get ahead of the process using it, so readers
    report what is missing and leave the schema alone.
    """
    state = tmp_path / "state"
    state.mkdir()
    database = state / "trading_state.sqlite3"
    with sqlite3.connect(database) as connection:      # a database with none of the later tables
        connection.execute("CREATE TABLE documents(namespace TEXT PRIMARY KEY, payload TEXT "
                           "NOT NULL, revision INTEGER NOT NULL, updated_at TEXT NOT NULL)")
        connection.execute("INSERT INTO documents VALUES('portfolio.yaml','{}',1,'2026-01-01')")

    import agentic_trading.state_db as module
    module._relinked.clear()

    db = StateDB(state / "proposals.json")
    status = db.status()
    assert status["integrity"] == "ok"
    assert status["counts"]["documents"] == 1        # the pre-existing row is untouched
    # the tables this release added are reported as absent, not conjured into existence
    assert "unavailable" in str(status["counts"]["events"])

    # every reader degrades instead of raising
    assert db.read_document(state / "portfolio.yaml") == {}
    assert db.fills() == []
    assert db.research_jobs() == []
    assert db.console_session("absent") is None
    assert state_cli.unsettled_orders(db) == []

    with sqlite3.connect(database) as connection:      # and the schema is still the old one
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables == {"documents"}

    # a write connection is what migrates it, which is the code that needs the new tables
    with db.transaction():
        pass
    assert isinstance(db.status()["counts"]["events"], int)


def test_status_reports_a_table_it_cannot_count_rather_than_raising(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    db = StateDB(state / "proposals.json")
    with db.transaction() as conn:
        conn.execute("DROP TABLE events")

    counts = db.status()["counts"]
    assert isinstance(counts["events"], str) and "unavailable" in counts["events"]
    assert counts["proposals"] == 0        # everything else still reports


def test_an_unmounted_mirror_destination_is_refused_not_created(project, tmp_path):
    """A copy on the internal disk is not an off-machine copy, however much it looks like one.

    Pointing the mirror at /Volumes/Something and running with the disk unplugged used to
    create that path on the boot volume, so the backup meant to survive losing this Mac landed
    on the Mac. It has to fail loudly instead.
    """
    if shutil.which("openssl") is None:
        pytest.skip("openssl is required for the encrypted mirror")
    absent = tmp_path / "Volumes" / "BackupDisk"      # a mount point that is not mounted

    with pytest.raises(SystemExit) as raised:
        run(["backup", "--config-dir", str(project / "config"), "--verify",
             "--mirror-dir", str(absent), "--key-file", str(key_file(tmp_path))])
    assert "does not exist" in str(raised.value)
    assert not absent.exists()                        # and nothing was created in its place

    # the local copy still succeeded, so the failure costs the mirror and nothing else
    assert list((project / "state" / "backups").glob("trading_state_*.sqlite3"))
