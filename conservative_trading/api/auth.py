"""Single-owner console authentication. No credential means no business API access.

The password hash stays on disk. Opaque sessions survive an API restart for 30 days, while
SQLite stores only the session-token hash. Password rotation revokes every session. The browser
receives an HttpOnly cookie and a per-session CSRF token. Gateway identity headers are never
trusted implicitly.
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from ..state_db import StateDB
from ..storage import atomic_write

COOKIE = "ct_session"
SESSION_SECONDS = 30 * 24 * 3600
MAX_SESSIONS = 32
#: How stale "last seen" may become before it is written again. The console polls several
#: endpoints every ten to sixty seconds, so recording every request would mean a durable write
#: per poll to store a fact nobody reads that precisely.
TOUCH_SECONDS = 60


def device_label(user_agent: str | None) -> str:
    """A readable name for the device that signed in.

    Only ever displayed. The browser supplies this string, so it is treated as decoration: no
    decision anywhere depends on it, and it is truncated rather than parsed strictly.
    """
    agent = (user_agent or "").strip()
    if not agent:
        return "unknown device"
    platform = next((name for token, name in (
        ("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"),
        ("Macintosh", "Mac"), ("Windows", "Windows"), ("Linux", "Linux"),
    ) if token in agent), "unknown device")
    # Order matters: Chrome and Edge both claim Safari, and Edge also claims Chrome.
    browser = next((name for token, name in (
        ("Edg/", "Edge"), ("CriOS", "Chrome"), ("Chrome/", "Chrome"),
        ("FxiOS", "Firefox"), ("Firefox/", "Firefox"), ("Safari/", "Safari"),
    ) if token in agent), None)
    return f"{platform} · {browser}" if browser else platform


def password_hash(password: str, salt: str) -> str:
    return hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2**14,
                          r=8, p=1, dklen=32).hex()


def configure_password(path: Path, username: str, password: str) -> None:
    if len(password) < 14 or len(password) > 1024:
        raise ValueError("use a password of 14–1024 characters")
    if not username.strip() or len(username) > 100:
        raise ValueError("provide an owner username of 1–100 characters")
    salt = secrets.token_hex(16)
    atomic_write(path, json.dumps({"username": username, "salt": salt,
                                  "hash": password_hash(password, salt),
                                  "generation": secrets.token_hex(16)}))
    with StateDB(path).transaction() as connection:
        connection.execute("DELETE FROM console_sessions")


@dataclass
class Session:
    username: str
    csrf: str
    generation: str
    created: float
    expires: float
    session_id: str = ""
    label: str = ""


class ConsoleAuth:
    def __init__(self, path: str | Path | None = None, allowed_origins: list[str] | None = None):
        self.path = Path(path or os.environ.get("CT_AUTH_FILE", "state/console_auth.json"))
        self.origins = set(allowed_origins if allowed_origins is not None else os.environ.get(
            "CT_ALLOWED_ORIGINS", "http://127.0.0.1:8787,http://localhost:8787").split(","))
        self.origins = {x.strip().rstrip("/") for x in self.origins if x.strip()}
        for origin in self.origins:
            url = urlsplit(origin)
            local = url.hostname in ("localhost", "127.0.0.1", "::1")
            if (url.scheme not in ("https", "http") or not url.netloc or url.path
                    or url.query or url.fragment or url.username or url.password
                    or url.scheme == "http" and not local):
                raise ValueError("allowed origins must be HTTPS origins (HTTP is allowed only on loopback)")
        self.db = StateDB(self.path)
        self._failures: list[float] = []
        self._lock = threading.RLock()

    def credentials(self) -> dict | None:
        try:
            if self.path.stat().st_mode & 0o077:
                return None
            value = json.loads(self.path.read_text())
            if all(isinstance(value.get(k), str) and value[k] for k in ("username", "salt", "hash", "generation")):
                return value
        except (OSError, ValueError):
            pass
        return None

    def allowed_host(self, host: str) -> bool:
        return host.lower() in {urlsplit(origin).netloc.lower() for origin in self.origins}

    def allowed_origin(self, origin: str | None) -> bool:
        return origin is not None and origin in self.origins

    def login(self, username: str, password: str, user_agent: str | None = None) -> tuple[str | None, str]:
        with self._lock:
            monotonic_now = time.monotonic()
            self._failures = [stamp for stamp in self._failures if monotonic_now - stamp < 300]
            if len(self._failures) >= 5:
                return None, "rate_limited"
            credentials = self.credentials()
            if credentials is None:
                return None, "unconfigured"
            try:
                valid_password = hmac.compare_digest(password_hash(password, credentials["salt"]), credentials["hash"])
            except (ValueError, TypeError):
                return None, "unconfigured"
            if not valid_password or not hmac.compare_digest(username.encode(), credentials["username"].encode()):
                self._failures.append(monotonic_now)
                return None, "invalid"
            now = time.time()
            token = secrets.token_urlsafe(32)
            with self.db.transaction() as connection:
                connection.execute(
                    "DELETE FROM console_sessions WHERE expires_at<=? OR generation<>?",
                    (now, credentials["generation"]),
                )
                connection.execute(
                    "DELETE FROM console_sessions WHERE token_hash IN "
                    "(SELECT token_hash FROM console_sessions ORDER BY created_at ASC "
                    "LIMIT MAX(0, (SELECT count(*) FROM console_sessions) - ?))",
                    (MAX_SESSIONS - 1,),
                )
                connection.execute(
                    "INSERT INTO console_sessions(token_hash,username,csrf,generation,"
                    "created_at,last_seen_at,expires_at,session_id,label) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (self._key(token), credentials["username"], secrets.token_urlsafe(32),
                     credentials["generation"], now, now, now + SESSION_SECONDS,
                     secrets.token_hex(8), device_label(user_agent)),
                )
            return token, "ok"

    @staticmethod
    def _key(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def session(self, token: str | None) -> Session | None:
        with self._lock:
            if not token:
                return None
            key = self._key(token)
            credentials = self.credentials()
            row = self.db.console_session(key)
            now = time.time()
            valid = (row is not None and credentials is not None and row["expires_at"] > now
                     and hmac.compare_digest(row["generation"], credentials["generation"]))
            if not valid:
                if row is not None:
                    with self.db.transaction() as connection:
                        connection.execute("DELETE FROM console_sessions WHERE token_hash=?", (key,))
                return None
            session_id = row["session_id"] or secrets.token_hex(8)
            # Activity is recorded; it never buys time. Extending the expiry on use would let a
            # console left polling in a browser tab keep a session alive forever, which is the
            # opposite of what a 30-day limit is for.
            stale = now - float(row["last_seen_at"] or 0) >= TOUCH_SECONDS
            if stale or not row["session_id"]:
                with self.db.transaction() as connection:
                    connection.execute(
                        "UPDATE console_sessions SET last_seen_at=?,session_id=? WHERE token_hash=?",
                        (now, session_id, key))
            return Session(row["username"], row["csrf"], row["generation"],
                           row["created_at"], row["expires_at"], session_id,
                           row["label"] or "unknown device")

    def logout(self, token: str | None) -> None:
        with self._lock:
            if token:
                with self.db.transaction() as connection:
                    connection.execute("DELETE FROM console_sessions WHERE token_hash=?",
                                       (self._key(token),))

    def sessions(self, current_token: str | None = None) -> list[dict]:
        """Every device signed in, newest first, without exposing any token.

        A session is identified to the interface by an opaque id. The token hash stays in the
        database precisely so that reading this list can never yield a way to impersonate one.
        """
        current = self._key(current_token) if current_token else None
        connection = self.db._read()
        if connection is None:
            return []
        try:
            # Selected wholesale rather than by name: a database written before device
            # management has neither column, and naming them would raise. A failure that does
            # get through is deliberately not caught — reporting "no devices" because the query
            # failed would tell the owner they had been signed out everywhere, which is the most
            # alarming possible way to describe a broken read.
            rows = connection.execute(
                "SELECT * FROM console_sessions ORDER BY created_at DESC").fetchall()
        finally:
            connection.close()

        def stamp(value: float) -> str:
            return datetime.fromtimestamp(float(value), UTC).isoformat()

        # sqlite3.Row has no membership test, so the shape is read once from the result.
        columns = set(rows[0].keys()) if rows else set()

        def optional(row, column: str) -> str:
            return (row[column] if column in columns else "") or ""

        now = time.time()
        # A session with no id yet predates this release and cannot be revoked individually
        # until it is next used. It is still listed: hiding it would under-report how many
        # devices are signed in, which is the one thing this list exists to answer.
        return [{
            "session_id": optional(row, "session_id"),
            "label": optional(row, "label") or "unknown device",
            "created_at": stamp(row["created_at"]),
            "last_seen_at": stamp(row["last_seen_at"]),
            "expires_at": stamp(row["expires_at"]),
            "days_left": round((float(row["expires_at"]) - now) / 86400, 2),
            "revocable": bool(optional(row, "session_id")),
            "current": current is not None and hmac.compare_digest(row["token_hash"], current),
        } for row in rows]

    def revoke(self, session_id: str, *, current_token: str | None = None) -> bool:
        """Sign one device out. Refuses an empty id, which would otherwise match a backfill."""
        if not session_id:
            return False
        with self._lock, self.db.transaction() as connection:
            changed = connection.execute(
                "DELETE FROM console_sessions WHERE session_id=?", (session_id,))
            return changed.rowcount > 0

    def revoke_others(self, current_token: str | None) -> int:
        """Sign out every device except this one, without needing the password."""
        with self._lock, self.db.transaction() as connection:
            if current_token:
                changed = connection.execute(
                    "DELETE FROM console_sessions WHERE token_hash<>?", (self._key(current_token),))
            else:
                changed = connection.execute("DELETE FROM console_sessions")
            return changed.rowcount


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="ct-auth", description="Set/rotate the local console owner's password.")
    parser.add_argument("--file", type=Path, default=Path(os.environ.get("CT_AUTH_FILE", "state/console_auth.json")))
    parser.add_argument("--username", default="owner")
    args = parser.parse_args(argv)
    first = getpass.getpass("New console password (14+ characters): ")
    if first != getpass.getpass("Repeat password: "):
        parser.error("passwords do not match")
    try:
        configure_password(args.file, args.username, first)
    except ValueError as exc:
        parser.error(str(exc))
    print(f"Console owner configured at {args.file}. Existing sessions are revoked on their next request.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
