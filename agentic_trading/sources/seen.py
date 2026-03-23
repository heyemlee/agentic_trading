"""When this system first held an item, remembered between runs.

The time a source gives for something is the source's claim about itself. The time this system
first held it is a fact about this system, and the two answer different questions: one says
when a thing became public, the other says when anything here could first have acted on it. A
record that keeps only the first invites the mistake of dating knowledge by the event.

One small file per item, named by a hash of its id and created exclusively, so two research
runs that meet the same filing at the same moment agree on one time without a lock between
them. Only the id's hash and a timestamp are kept: no title, no text, no author. Entries are
forgotten after a period the caller chooses, which for posts written by people is short.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import now_iso


def _path(root: Path, source: str, item: str) -> Path:
    digest = hashlib.sha256(str(item).encode("utf-8")).hexdigest()
    return Path(root) / source / "seen" / digest[:2] / digest[2:]


def first_seen(root: Path, source: str, items: Iterable[str], *,
               now: datetime | None = None) -> dict[str, str]:
    """{item: when it was first seen}, recording ``now`` for any not seen before.

    Never raises. If the time cannot be kept, the answer is ``now`` and simply is not
    remembered: the next run will say the same thing about its own moment.
    """
    stamp = now_iso(now)
    out: dict[str, str] = {}
    for item in dict.fromkeys(str(i) for i in items):
        path = _path(root, source, item)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("x", encoding="utf-8") as stream:
                stream.write(stamp)
            out[item] = stamp
        except FileExistsError:
            try:
                out[item] = path.read_text(encoding="utf-8").strip() or stamp
            except OSError:
                out[item] = stamp
        except OSError:
            out[item] = stamp
    return out


def forget(root: Path, source: str, *, older_than_days: int, now: datetime | None = None) -> int:
    """Drop what was first seen longer ago than this. Returns how many were dropped."""
    base = Path(root) / source / "seen"
    cutoff = (now or datetime.now(UTC)) - timedelta(days=older_than_days)
    dropped = 0
    try:
        files = [f for bucket in base.iterdir() if bucket.is_dir() for f in bucket.iterdir()]
    except OSError:
        return 0
    for file in files:
        try:
            seen_at = datetime.fromisoformat(file.read_text(encoding="utf-8").strip())
            if seen_at.tzinfo is None:
                seen_at = seen_at.replace(tzinfo=UTC)
            if seen_at < cutoff:
                file.unlink()
                dropped += 1
        except (OSError, ValueError):
            continue
    return dropped
