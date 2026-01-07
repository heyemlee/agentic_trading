"""Inspect, migrate, back up and restore the transactional application state."""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from .operations import record as record_activity
from .portfolio import cash_flow_summary, migrate_portfolio, record_cash_flow
from .profile import load_profiles
from .proposals import ProposalStore
from .state_db import StateDB

#: Proposal states that still own money at the venue. A restored database must be compared
#: against the broker before anything new is executed, and these are the rows to compare.
UNSETTLED = ("APPROVED", "EXECUTING", "SUBMITTED", "PARTIALLY_FILLED", "UNKNOWN")


def _sources(profiles):
    broker = profiles.risk.get("broker") or {}
    portfolio = profiles.risk.get("portfolio_state") or {}
    micro = profiles.risk.get("micro_live_execution") or {}
    proposal = profiles.project_dir / str(broker.get("state_file", "state/proposals.json"))
    fills = profiles.project_dir / str(broker.get("fills_file", "state/fills.jsonl"))
    ledgers = [profiles.project_dir / str(portfolio.get("file", "state/portfolio.yaml"))]
    ladders = [profiles.project_dir / str(portfolio.get("risk_state_file", "state/risk_state.json"))]
    micro_ledger = profiles.project_dir / str(micro.get("ledger_file", "state/micro_live_portfolio.yaml"))
    micro_ladder = profiles.project_dir / str(micro.get(
        "risk_state_file", "state/micro_live_risk_state.json"))
    if micro_ledger.exists() or StateDB(micro_ledger).read_document(micro_ledger) is not None:
        ledgers.append(micro_ledger)
    if micro_ladder.exists() or StateDB(micro_ladder).read_document(micro_ladder) is not None:
        ladders.append(micro_ladder)
    return proposal, fills, ledgers, ladders


def unsettled_orders(db: StateDB) -> list[dict[str, str]]:
    """Proposals whose real fate is known only to the broker.

    Publishing a release or restoring a backup while one of these is open risks a second
    submission or a lost fill, so both paths ask this question before they act.
    """
    conn = db.current() or db._read()
    if conn is None:
        return []
    owned = db.current() is None
    try:
        rows = conn.execute("SELECT payload FROM proposals").fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        if owned:
            conn.close()
    out = []
    for row in rows:
        item = json.loads(row[0])
        status = str(item.get("order_status") or item.get("status") or "")
        if item.get("client_order_id") and status in UNSETTLED:
            out.append({"proposal_id": item.get("proposal_id"), "ticker": item.get("ticker"),
                        "bucket": item.get("bucket"), "order_status": status,
                        "client_order_id": item.get("client_order_id")})
    return out


def _integrity(path: Path) -> str:
    """The integrity verdict, as a string. A file too damaged to open is a verdict too."""
    try:
        with sqlite3.connect(path) as connection:
            return connection.execute("PRAGMA integrity_check").fetchone()[0]
    except sqlite3.DatabaseError as exc:
        return f"unreadable: {exc}"


def _encrypt(source: Path, destination: Path, key_file: Path) -> None:
    """AES-256 with a passphrase file, using the openssl already on the machine.

    A second copy exists to survive losing this Mac, so it travels to hardware whose physical
    security is not this machine's; it is encrypted for the same reason.
    """
    if shutil.which("openssl") is None:
        raise RuntimeError("openssl is required to write an encrypted mirror copy")
    if not key_file.exists() or key_file.stat().st_size < 16:
        raise RuntimeError(f"passphrase file {key_file} is missing or too short (16+ bytes)")
    if key_file.stat().st_mode & 0o077:
        raise RuntimeError(f"passphrase file {key_file} must not be group or world readable")
    destination.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "600000", "-salt",
         "-in", str(source), "-out", str(destination), "-pass", f"file:{key_file}"],
        capture_output=True, text=True, check=False)
    if result.returncode != 0:
        destination.unlink(missing_ok=True)
        raise RuntimeError(f"openssl failed: {result.stderr.strip()}")
    destination.chmod(0o600)


def _decrypt(source: Path, destination: Path, key_file: Path) -> None:
    if shutil.which("openssl") is None:
        raise RuntimeError("openssl is required to read an encrypted backup")
    result = subprocess.run(
        ["openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-iter", "600000",
         "-in", str(source), "-out", str(destination), "-pass", f"file:{key_file}"],
        capture_output=True, text=True, check=False)
    if result.returncode != 0:
        destination.unlink(missing_ok=True)
        raise RuntimeError(f"openssl failed: {result.stderr.strip()}")


def _prune(directory: Path, pattern: str, retain_days: int) -> list[str]:
    cutoff = datetime.now(UTC).timestamp() - retain_days * 86400
    removed = []
    for path in sorted(directory.glob(pattern)):
        if path.is_file() and path.stat().st_mtime < cutoff:
            path.unlink()
            removed.append(path.name)
    return removed


def _backup(profiles, args, parser) -> int:
    proposal, *_ = _sources(profiles)
    db = ProposalStore(proposal).db
    if not db.path.exists():
        parser.error("state database does not exist; run ct-state migrate first")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    destination = Path(args.output) if args.output else (
        profiles.project_dir / "state" / "backups" / f"trading_state_{stamp}.sqlite3")
    db.backup(destination)
    report = {"backup": str(destination.resolve()), "integrity": None,
              "mirror": None, "pruned": [], "mirror_pruned": []}
    if args.verify:
        report["integrity"] = _integrity(destination)
        if report["integrity"] != "ok":
            destination.unlink(missing_ok=True)
            raise SystemExit(f"backup integrity check failed: {report['integrity']}")
    if args.mirror_dir:
        if not args.key_file:
            parser.error("--mirror-dir also needs --key-file: an off-machine copy is encrypted")
        mirror = Path(args.mirror_dir) / f"{destination.name}.enc"
        _encrypt(destination, mirror, Path(args.key_file))
        report["mirror"] = str(mirror.resolve())
        if args.retain_days:
            report["mirror_pruned"] = _prune(Path(args.mirror_dir),
                                             "trading_state_*.sqlite3.enc", args.retain_days)
    if args.retain_days:
        report["pruned"] = _prune(destination.parent, "trading_state_*.sqlite3", args.retain_days)
    record_activity(db, "backup", f"{destination.name}"
                    + (f", mirrored to {Path(report['mirror']).parent}" if report["mirror"] else "")
                    + f", integrity {report['integrity'] or 'unchecked'}", min_interval_seconds=0)
    print(json.dumps(report, indent=2))
    return 0


def _restore(args, parser) -> int:
    """Put a verified backup into a state directory and say what must be checked next.

    Restoring is never only a file copy. The snapshot carries console sessions that were valid
    when it was taken, and orders whose real fate is known only to the broker, so this revokes
    the former and lists the latter instead of letting a stale database look authoritative.
    """
    source = Path(args.source)
    into = Path(args.into)
    if not source.exists():
        parser.error(f"no backup at {source}")
    target = into / "trading_state.sqlite3"
    if target.exists() and not args.force:
        parser.error(f"{target} already exists; move it aside or pass --force")
    into.mkdir(parents=True, exist_ok=True)

    staged = into / f".restore-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.sqlite3"
    try:
        if source.suffix == ".enc":
            if not args.key_file:
                parser.error("an encrypted backup needs --key-file")
            _decrypt(source, staged, Path(args.key_file))
        else:
            shutil.copyfile(source, staged)
        integrity = _integrity(staged)
        if integrity != "ok":
            raise SystemExit(f"refusing to restore a damaged backup: {integrity}")
        staged.chmod(0o600)
        staged.replace(target)
    except BaseException:
        staged.unlink(missing_ok=True)
        raise

    db = StateDB(target.with_name("proposals.json"))
    unsettled, sessions_revoked = [], 0
    with db.transaction() as conn:   # opening the database relinks pre-move absolute keys
        sessions_revoked = conn.execute("SELECT count(*) FROM console_sessions").fetchone()[0]
        conn.execute("DELETE FROM console_sessions")
        unsettled = unsettled_orders(db)
        conn.execute("UPDATE reservations SET state='RELEASED',updated_at=? WHERE state='ACTIVE'",
                     (datetime.now(UTC).isoformat(),))
    print(json.dumps({
        "restored_to": str(target.resolve()),
        "from": str(source.resolve()),
        "integrity": "ok",
        "console_sessions_revoked": sessions_revoked,
        "orders_to_verify_at_the_broker": unsettled,
        "counts": db.status()["counts"],
        "next_steps": [
            "sign in again on every device: the restored sessions were revoked",
            "compare each listed order against the broker before enabling execution",
            "run ct-state status and confirm the ledger and ladder documents are readable",
            "record the data loss window: everything after this backup's timestamp is gone",
        ],
    }, indent=2))
    return 0


def _sessions(profiles) -> int:
    """Persistent console sessions, so a phone login can be confirmed without a device UI."""
    proposal, *_ = _sources(profiles)
    db = ProposalStore(proposal).db
    conn = db._read()
    if conn is None:
        print(json.dumps({"sessions": []}, indent=2))
        return 0
    try:
        rows = conn.execute("SELECT username,created_at,last_seen_at,expires_at "
                            "FROM console_sessions ORDER BY created_at").fetchall()
    except sqlite3.OperationalError:
        rows = []
    finally:
        conn.close()
    now = datetime.now(UTC).timestamp()
    print(json.dumps({"now": datetime.now(UTC).isoformat(), "sessions": [{
        "username": row["username"],
        "created_at": datetime.fromtimestamp(row["created_at"], UTC).isoformat(),
        "last_seen_at": datetime.fromtimestamp(row["last_seen_at"], UTC).isoformat(),
        "expires_at": datetime.fromtimestamp(row["expires_at"], UTC).isoformat(),
        "days_left": round((row["expires_at"] - now) / 86400, 2),
    } for row in rows]}, indent=2))
    return 0


def _cashflow(profiles, args, parser) -> int:
    """Record a deposit or withdrawal so return is never confused with funding."""
    _proposal, _fills, ledgers, _ladders = _sources(profiles)
    # ledgers[0] is the conceptual core/sandbox book; a Micro-Live ledger, when it exists, is
    # a separate account and is appended after it.
    ledger = ledgers[0] if args.bucket in ("core", "sandbox") else (
        ledgers[1] if len(ledgers) > 1 else None)
    if ledger is None:
        parser.error(f"no ledger holds bucket {args.bucket!r}")
    try:
        state = record_cash_flow(ledger, bucket=args.bucket, amount_usd=args.amount,
                                 kind=args.kind, date=args.date, note=args.note)
    except (ValueError, KeyError) as exc:
        parser.error(str(exc))
    print(json.dumps({"ledger": str(ledger), "bucket": args.bucket,
                      "cash_usd": state.buckets[args.bucket].cash_usd,
                      "net_deposits": cash_flow_summary(state)}, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ct-state")
    parser.add_argument("action", choices=("migrate", "status", "backup", "restore",
                                           "sessions", "cashflow"))
    parser.add_argument("--config-dir", default="config")
    parser.add_argument("--output", help="backup destination; defaults to state/backups/<timestamp>.sqlite3")
    parser.add_argument("--verify", action="store_true", help="backup: integrity-check the copy")
    parser.add_argument("--retain-days", type=int, default=0,
                        help="backup: delete copies older than this many days")
    parser.add_argument("--mirror-dir", help="backup: also write an encrypted copy here")
    parser.add_argument("--key-file", help="passphrase file for the encrypted copy")
    parser.add_argument("--source", help="restore: the backup file to restore from")
    parser.add_argument("--into", help="restore: the state directory to restore into")
    parser.add_argument("--force", action="store_true", help="restore: replace an existing database")
    parser.add_argument("--fail-on-unsettled", action="store_true",
                        help="status: exit 3 when an order is still open at the broker")
    parser.add_argument("--bucket", help="cashflow: core, sandbox or micro_live")
    parser.add_argument("--amount", type=float, help="cashflow: positive USD amount")
    parser.add_argument("--kind", choices=("deposit", "withdrawal"), default="deposit")
    parser.add_argument("--date", help="cashflow: yyyy-mm-dd, defaults to today")
    parser.add_argument("--note", default="", help="cashflow: free text")
    args = parser.parse_args(argv)

    if args.action == "restore":
        if not args.source or not args.into:
            parser.error("restore needs --source and --into")
        return _restore(args, parser)

    profiles = load_profiles(args.config_dir)
    proposal, fills, ledgers, ladders = _sources(profiles)
    store = ProposalStore(proposal)
    database_paths = {StateDB(path).path for path in (proposal, fills, *ledgers, *ladders)}
    if len(database_paths) != 1:
        parser.error("proposal, fill and portfolio files must share one directory for atomic transactions")

    if args.action == "migrate":
        store.migrate()
        for ledger in ledgers:
            if ledger.exists() or StateDB(ledger).read_document(ledger) is not None:
                migrate_portfolio(ledger)
        db = store.db
        with db.transaction() as conn:
            db.import_fills(conn, fills)
            for ladder in ladders:
                if ladder.exists() and db.read_document(ladder) is None:
                    raw = ladder.read_bytes()
                    db.write_document(ladder, json.loads(raw), conn)
                    db.mark_migrated(conn, ladder, "drawdown_ladder", raw)
        print(json.dumps(db.status(), indent=2))
        return 0
    if args.action == "status":
        status = store.db.status()
        status["unsettled_orders"] = unsettled_orders(store.db)
        print(json.dumps(status, indent=2))
        # A release or a restore must not proceed over an order the broker still owns.
        return 3 if (args.fail_on_unsettled and status["unsettled_orders"]) else 0
    if args.action == "sessions":
        return _sessions(profiles)
    if args.action == "cashflow":
        if not args.bucket or args.amount is None:
            parser.error("cashflow needs --bucket and --amount")
        return _cashflow(profiles, args, parser)
    return _backup(profiles, args, parser)


if __name__ == "__main__":
    sys.exit(main())
