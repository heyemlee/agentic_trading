"""Transactional SQLite state shared by proposals, ledgers, orders, fills and jobs."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

_local = threading.local()
_relinked: set[str] = set()
_schema_ready: set[str] = set()
_relink_guard = threading.Lock()
SCHEMA = """
CREATE TABLE IF NOT EXISTS migrations(source TEXT PRIMARY KEY, kind TEXT NOT NULL, sha256 TEXT, imported_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS documents(namespace TEXT PRIMARY KEY, payload TEXT NOT NULL, revision INTEGER NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS proposals(namespace TEXT NOT NULL, proposal_id TEXT NOT NULL, payload TEXT NOT NULL, revision INTEGER NOT NULL,
 approval_request_id TEXT, updated_at TEXT NOT NULL, PRIMARY KEY(namespace,proposal_id), UNIQUE(namespace,approval_request_id));
CREATE TABLE IF NOT EXISTS orders(client_order_id TEXT PRIMARY KEY, proposal_id TEXT NOT NULL, account_key TEXT NOT NULL,
 status TEXT NOT NULL, payload TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS approvals(request_id TEXT PRIMARY KEY, proposal_id TEXT NOT NULL, identity TEXT NOT NULL,
 version TEXT NOT NULL, terms TEXT NOT NULL, approved_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS fills(fill_id TEXT PRIMARY KEY, proposal_id TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reservations(client_order_id TEXT PRIMARY KEY, proposal_id TEXT UNIQUE NOT NULL, account_key TEXT NOT NULL,
 cash_usd REAL NOT NULL DEFAULT 0, quantity REAL NOT NULL DEFAULT 0, state TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_order_per_account ON reservations(account_key) WHERE state='ACTIVE';
CREATE TABLE IF NOT EXISTS research_jobs(run_key TEXT PRIMARY KEY, dedupe_key TEXT NOT NULL, payload TEXT NOT NULL,
 state TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_research_key ON research_jobs(dedupe_key) WHERE state IN ('QUEUED','RUNNING');
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, kind TEXT NOT NULL,
 subject TEXT, actor TEXT, version TEXT, outcome TEXT, detail TEXT);
CREATE INDEX IF NOT EXISTS events_subject ON events(subject);
CREATE INDEX IF NOT EXISTS events_kind ON events(kind);
CREATE TABLE IF NOT EXISTS console_sessions(token_hash TEXT PRIMARY KEY, username TEXT NOT NULL, csrf TEXT NOT NULL,
 generation TEXT NOT NULL, created_at REAL NOT NULL, last_seen_at REAL NOT NULL, expires_at REAL NOT NULL);
"""


class StateConflict(RuntimeError):
    pass


def database_path(source: str | Path) -> Path:
    return Path(source).resolve().parent / "trading_state.sqlite3"


class StateDB:
    def __init__(self, source: str | Path):
        self.path = database_path(source)

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(SCHEMA)
        self._add_columns(conn)
        self._relink(conn)
        self.path.chmod(0o600)
        return conn

    def _read(self):
        if not self.path.exists():
            return None
        # A read-only connection cannot create a table or add a column, so a database written
        # by an earlier release would fail any query touching something added since. Bring it
        # up to date here, once per process, rather than at each call site: fixing this one
        # reader at a time is how `ct-state status` came to crash on a missing events table
        # after the session list had already been patched for the same reason.
        self._ensure_schema_once()
        conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema_once(self) -> None:
        key = str(self.path)
        if key in _schema_ready:
            return
        if self.ensure_schema():
            _schema_ready.add(key)

    @contextmanager
    def transaction(self, immediate=True):
        connections = getattr(_local, "connections", {})
        key = str(self.path)
        if key in connections:
            yield connections[key]
            return
        conn = self._connect()
        _local.connections = {**connections, key: conn}
        try:
            conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
            _local.connections = connections

    def current(self):
        return getattr(_local, "connections", {}).get(str(self.path))

    #: Columns added after a table shipped. CREATE TABLE IF NOT EXISTS cannot add them to a
    #: database that already exists, and a deployed console must keep its sessions across the
    #: upgrade rather than signing every device out.
    ADDED_COLUMNS: ClassVar = (
        ("console_sessions", "session_id", "TEXT"),
        ("console_sessions", "label", "TEXT"),
    )

    def _add_columns(self, conn) -> None:
        for table, column, kind in self.ADDED_COLUMNS:
            existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            if existing and column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")

    def ensure_schema(self) -> bool:
        """Bring the database up to date before a read-only caller queries it.

        A read-only connection cannot add a column, so a reader that asks for one added after
        the database was created gets an error. Swallowing that error would report an empty
        result, which for a device list reads as "you are signed out everywhere" — the most
        alarming possible way to say "this process has not written yet".
        """
        try:
            with self.transaction():
                return True
        except sqlite3.DatabaseError:
            return False

    #: Every table keyed by a state file. A key is rewritten in all of them at once.
    KEYED: ClassVar = (("migrations", "source"), ("documents", "namespace"), ("proposals", "namespace"))

    def source_key(self, source):
        """Key for a state file, relative to the directory the database itself lives in.

        Absolute keys made the database unusable after a move: restoring a backup into another
        directory, or onto another machine, left every ledger and proposal unreachable, and the
        loaders would then silently fall back to whatever stale YAML or JSON sat beside them.
        By construction a source always sits next to its database, so the key is its file name.
        """
        resolved = Path(source).resolve()
        try:
            return resolved.relative_to(self.path.parent).as_posix()
        except ValueError:
            return str(resolved)

    def source_keys(self, source):
        """The current key first, then the pre-move absolute key, for read paths."""
        return (self.source_key(source), str(Path(source).resolve()))

    def _relink(self, conn) -> None:
        """Adopt rows written before keys were relative, once per database per process.

        An absolute key names a file that lived beside the database, so its relative form is
        the file name. A row is only moved when the relative key is free, which keeps a
        restored database from overwriting state that has been written since.
        """
        key = str(self.path)
        with _relink_guard:
            if key in _relinked:
                return
            _relinked.add(key)
        try:
            for table, column in self.KEYED:
                legacy = [row[0] for row in conn.execute(
                    f"SELECT DISTINCT {column} FROM {table} WHERE {column} LIKE '/%'")]
                for old in legacy:
                    new = Path(old).name
                    taken = conn.execute(
                        f"SELECT 1 FROM {table} WHERE {column}=?", (new,)).fetchone()
                    conn.execute(
                        f"DELETE FROM {table} WHERE {column}=?" if taken
                        else f"UPDATE {table} SET {column}=? WHERE {column}=?",
                        (old,) if taken else (new, old))
        except sqlite3.DatabaseError:
            with _relink_guard:
                _relinked.discard(key)
            raise

    def migrated(self, source):
        current = self.current()
        conn = current or self._read()
        if conn is None:
            return False
        try:
            return conn.execute("SELECT 1 FROM migrations WHERE source IN (?,?)",
                                self.source_keys(source)).fetchone() is not None
        except sqlite3.OperationalError:
            return False
        finally:
            if current is None:
                conn.close()

    def mark_migrated(self, conn, source, kind, raw=b""):
        conn.execute("INSERT OR IGNORE INTO migrations VALUES(?,?,?,?)", (
            self.source_key(source), kind, hashlib.sha256(raw).hexdigest() if raw else None,
            datetime.now(UTC).isoformat()))

    def read_document(self, namespace):
        conn = self.current() or self._read()
        if conn is None:
            return None
        owned = self.current() is None
        try:
            row = conn.execute(
                "SELECT payload FROM documents WHERE namespace=? ORDER BY namespace LIMIT 1",
                (self.source_key(namespace),)).fetchone() or conn.execute(
                "SELECT payload FROM documents WHERE namespace=?",
                (str(Path(namespace).resolve()),)).fetchone()
            return json.loads(row[0]) if row else None
        finally:
            if owned:
                conn.close()

    def write_document(self, namespace, payload, conn=None):
        conn = conn or self.current()
        if conn is None:
            with self.transaction() as tx:
                return self.write_document(namespace, payload, tx)
        now = datetime.now(UTC).isoformat()
        conn.execute("INSERT INTO documents VALUES(?,?,1,?) ON CONFLICT(namespace) DO UPDATE SET "
                     "payload=excluded.payload,revision=documents.revision+1,updated_at=excluded.updated_at",
                     (self.source_key(namespace), json.dumps(payload, allow_nan=False), now))

    #: Documents that belong to the deployment rather than to a state file live under this
    #: prefix, so they can never collide with a ledger's own key.
    META = "meta:"

    def read_meta(self, name, default=None):
        """A deployment-owned document, such as the schedule's execution record."""
        conn = self.current() or self._read()
        if conn is None:
            return default
        owned = self.current() is None
        try:
            row = conn.execute("SELECT payload FROM documents WHERE namespace=?",
                               (self.META + name,)).fetchone()
            return json.loads(row[0]) if row else default
        except sqlite3.OperationalError:
            return default
        finally:
            if owned:
                conn.close()

    def write_meta(self, name, payload, conn=None):
        conn = conn or self.current()
        if conn is None:
            with self.transaction() as tx:
                return self.write_meta(name, payload, tx)
        conn.execute("INSERT INTO documents VALUES(?,?,1,?) ON CONFLICT(namespace) DO UPDATE SET "
                     "payload=excluded.payload,revision=documents.revision+1,updated_at=excluded.updated_at",
                     (self.META + name, json.dumps(payload, allow_nan=False),
                      datetime.now(UTC).isoformat()))
        return None

    def append_fill(self, fill, source=None, conn=None):
        conn = conn or self.current()
        if conn is None:
            with self.transaction() as tx:
                return self.append_fill(fill, source, tx)
        if source:
            self.import_fills(conn, source)
        cur = conn.execute("INSERT OR IGNORE INTO fills VALUES(?,?,?,?)", (
            fill["fill_id"], fill["proposal_id"], json.dumps(fill, allow_nan=False), fill["at"]))
        if cur.rowcount == 1:
            from . import events

            events.record(conn, kind="order_filled", subject=fill.get("proposal_id"),
                          at=fill.get("at"), outcome="FILLED",
                          detail={k: fill.get(k) for k in
                                  ("symbol", "side", "quantity", "price", "notional", "bucket")})
        return cur.rowcount == 1

    def import_fills(self, conn, source):
        if conn.execute("SELECT 1 FROM migrations WHERE source IN (?,?)",
                        self.source_keys(source)).fetchone():
            return
        path = Path(source)
        raw = path.read_bytes() if path.exists() else b""
        for number, line in enumerate(raw.decode(errors="replace").splitlines(), 1):
            if not line.strip():
                continue
            try:
                old = json.loads(line)
                if not isinstance(old, dict):
                    raise TypeError("fill must be a JSON object")
                fid = old.get("fill_id") or "legacy:" + hashlib.sha256(line.encode()).hexdigest()
                old["fill_id"] = fid
                old.setdefault("proposal_id", "legacy")
                old.setdefault("at", datetime.now(UTC).isoformat())
                conn.execute("INSERT OR IGNORE INTO fills VALUES(?,?,?,?)",
                             (fid, old["proposal_id"], json.dumps(old, allow_nan=False), old["at"]))
            except (ValueError, TypeError, KeyError) as exc:
                raise StateConflict(f"invalid legacy fill at {path}:{number}: {exc}") from exc
        self.mark_migrated(conn, source, "fills", raw)

    def fills(self, limit=50):
        current = self.current()
        conn = current or self._read()
        if conn is None:
            return []
        try:
            return [json.loads(r[0]) for r in conn.execute(
                "SELECT payload FROM fills ORDER BY created_at DESC LIMIT ?", (limit,))]
        finally:
            if current is None:
                conn.close()

    def reservations(self, *, active_only=False):
        current = self.current()
        conn = current or self._read()
        if conn is None:
            return []
        try:
            sql = "SELECT * FROM reservations"
            if active_only:
                sql += " WHERE state='ACTIVE'"
            return [dict(row) for row in conn.execute(sql + " ORDER BY updated_at")]
        finally:
            if current is None:
                conn.close()

    def research_jobs(self, *, states=(), limit=100):
        current = self.current()
        conn = current or self._read()
        if conn is None:
            return []
        try:
            params: list[object] = []
            sql = "SELECT payload FROM research_jobs"
            if states:
                sql += " WHERE state IN (" + ",".join("?" for _ in states) + ")"
                params.extend(states)
            sql += " ORDER BY created_at DESC LIMIT ?"
            params.append(limit)
            return [json.loads(row[0]) for row in conn.execute(sql, params)]
        finally:
            if current is None:
                conn.close()

    def get_research_job(self, run_key):
        current = self.current()
        conn = current or self._read()
        if conn is None:
            return None
        try:
            row = conn.execute("SELECT payload FROM research_jobs WHERE run_key=?", (run_key,)).fetchone()
            return json.loads(row[0]) if row else None
        finally:
            if current is None:
                conn.close()

    def console_session(self, token_hash):
        current = self.current()
        conn = current or self._read()
        if conn is None:
            return None
        try:
            row = conn.execute(
                "SELECT session_id,label,username,csrf,generation,created_at,last_seen_at,expires_at "
                "FROM console_sessions WHERE token_hash=?", (token_hash,)).fetchone()
            return dict(row) if row else None
        except sqlite3.OperationalError:
            return None  # an older database has no session table until its next write connection
        finally:
            if current is None:
                conn.close()

    def backup(self, destination):
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self.transaction(immediate=False) as source:
            target = sqlite3.connect(destination)
            try:
                source.backup(target)
            finally:
                target.close()
        destination.chmod(0o600)

    def status(self):
        conn = self._read()
        if conn is None:
            return {"path": str(self.path), "exists": False, "integrity": None, "counts": {}}
        try:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            tables = ("documents", "proposals", "orders", "approvals", "fills", "reservations",
                      "research_jobs", "console_sessions", "events", "migrations")
            counts = {}
            for name in tables:
                try:
                    counts[name] = conn.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
                except sqlite3.OperationalError as exc:
                    # Status is what a person runs when something is already wrong. It reports
                    # the gap rather than raising and telling them nothing else about the state.
                    counts[name] = f"unavailable: {exc}"
            return {"path": str(self.path), "exists": True, "integrity": integrity, "counts": counts}
        finally:
            conn.close()
