"""Small-file persistence with process/thread exclusion and durable atomic replacement."""

from __future__ import annotations

import fcntl
import os
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

_registry_guard = threading.Lock()
_locks: dict[str, threading.RLock] = {}
_local = threading.local()


@contextmanager
def file_lock(path: str | Path):
    key = str(Path(path).resolve())
    with _registry_guard:
        lock = _locks.setdefault(key, threading.RLock())
    with lock:
        held = getattr(_local, "held", set())
        if key in held:
            yield
            return
        lock_path = Path(key + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            _local.held = held | {key}
            yield
        finally:
            _local.held = held
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def atomic_write(path: str | Path, content: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
