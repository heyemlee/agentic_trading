"""Values that must survive a reboot but must never enter the repository.

An API key, a backup destination, a webhook URL: none of them belong in a tracked file, and
this repository is public. They live in a ``.env`` beside the ledger, which is gitignored, mode
0600, and named in the release checks as a file that must never be tracked.

The alternative, ``launchctl setenv``, does not survive a restart. Nothing announces that it is
gone: the macro data quietly returns to "unavailable", the off-site backup quietly stops
copying, and the first sign is a confidence score that drifted down weeks ago.

Reading it here rather than relying on the upstream package's own dotenv call fixes two real
gaps. That call resolves the file by walking up from the working directory, so a command run
from anywhere else silently gets nothing; and it only happens at all if ``tradingagents`` is
imported, which the backup job, the notification CLI, the scheduler and the API never do. On
this deployment that meant ``CT_BACKUP_MIRROR`` in ``.env`` would never have reached the job
that writes the off-site copy.
"""

from __future__ import annotations

import logging
import os
import stat
from pathlib import Path

logger = logging.getLogger(__name__)

FILE_NAME = ".env"


def env_path(project_dir: str | Path | None = None) -> Path:
    """Anchored to the data directory, never to whatever directory the command was run from."""
    base = project_dir or os.environ.get("CT_DATA_DIR") or Path.cwd()
    return Path(base) / FILE_NAME


def load_env(project_dir: str | Path | None = None) -> list[str]:
    """Put the file's values into the environment. Returns the names set, never the values.

    A variable already present in the environment wins: an operator running a command with an
    explicit value in front of it is being deliberate, and a file should not silently overrule
    that. Empty assignments are skipped so the placeholder lines in ``.env.example`` do not
    register as configuration that exists.
    """
    path = env_path(project_dir)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []

    try:
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & (stat.S_IRGRP | stat.S_IROTH):
            logger.warning("%s is readable beyond its owner (mode %o); run chmod 600 on it", path, mode)
    except OSError:
        pass

    loaded = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, _, value = stripped.partition("=")
        name, value = name.strip(), value.strip().strip("'").strip('"')
        if not name or not value or name in os.environ:
            continue
        os.environ[name] = value
        loaded.append(name)
    return sorted(loaded)
