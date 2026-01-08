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
from pathlib import Path
from urllib.parse import urlsplit

from ..state_db import StateDB
from ..storage import atomic_write

COOKIE = "ct_session"
SESSION_SECONDS = 30 * 24 * 3600
MAX_SESSIONS = 32


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

    def login(self, username: str, password: str) -> tuple[str | None, str]:
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
                    "INSERT INTO console_sessions VALUES(?,?,?,?,?,?,?)",
                    (self._key(token), credentials["username"], secrets.token_urlsafe(32),
                     credentials["generation"], now, now, now + SESSION_SECONDS),
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
            return Session(row["username"], row["csrf"], row["generation"],
                           row["created_at"], row["expires_at"])

    def logout(self, token: str | None) -> None:
        with self._lock:
            if token:
                with self.db.transaction() as connection:
                    connection.execute("DELETE FROM console_sessions WHERE token_hash=?",
                                       (self._key(token),))


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
