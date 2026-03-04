"""launchd job definitions for the macOS deployment.

The installer is a shell script, but what it writes into ``~/Library/LaunchAgents`` decides
whether the phone can still reach the console afterwards. That decision is made here instead,
in one testable place, because the failure it prevents is silent: a plain rerun of the
installer used to reset ``CT_ALLOWED_ORIGINS`` to loopback and the private HTTPS entry point
started answering ``untrusted host`` with nothing in the log to explain why.
"""

from __future__ import annotations

import argparse
import plistlib
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

API = "com.local.agentic-trading.api"
RESEARCH = "com.local.agentic-trading.research-weekly"
BACKUP = "com.local.agentic-trading.backup-daily"
MAINTENANCE = "com.local.agentic-trading.maintenance-daily"
LABELS = (API, RESEARCH, BACKUP, MAINTENANCE)

LOOPBACK = ("http://127.0.0.1:8787", "http://localhost:8787")

#: The symlink release.sh keeps beside its release directories, flipped to the active one on
#: every publish or rollback. Referencing it here rather than a release's own timestamped path
#: is what keeps an ordinary publish from touching the plist at all — see render()'s docstring.
CURRENT_LINK_NAME = "current"


def installed_origins(launch_agents: Path) -> list[str]:
    """Remote origins the currently installed API job already trusts."""
    try:
        with (Path(launch_agents) / f"{API}.plist").open("rb") as stream:
            payload = plistlib.load(stream)
    except (OSError, plistlib.InvalidFileException, ValueError):
        return []
    raw = (payload.get("EnvironmentVariables") or {}).get("CT_ALLOWED_ORIGINS", "")
    return [origin for origin in (x.strip().rstrip("/") for x in str(raw).split(","))
            if origin and urlsplit(origin).scheme == "https"]


def check_origin(origin: str) -> str:
    origin = origin.rstrip("/")
    parsed = urlsplit(origin)
    if parsed.scheme != "https" or not parsed.netloc or parsed.path or parsed.query or parsed.fragment:
        raise ValueError("remote origin must look like https://device.tailnet.ts.net")
    return origin


def allowed_origins(*, origin: str | None, keep: list[str]) -> list[str]:
    """Loopback, plus the remote origin being set, or the ones already installed.

    ``origin`` replaces; an empty ``origin`` with an empty ``keep`` is the deliberate
    loopback-only case, which is why "keep" is passed in rather than discovered here.
    """
    out = list(LOOPBACK)
    for remote in ([check_origin(origin)] if origin else keep):
        if remote not in out:
            out.append(remote)
    return out


def research_interval(data_dir: Path) -> dict[str, int]:
    """When the research job fires, read from the same config the console reports from.

    Two copies of a schedule drift, and the one a screen shows is the one a person plans around,
    so the plist is generated from the configuration rather than repeating it.

    The job must fire at least as often as the shortest period any account schedules: a daily
    holdings review that the operating system only invoked on Fridays would be considered once a
    week, which is a weekly review wearing a daily name. ``schedule.finest_period`` is that
    answer, and ``next_run_at`` reads the same one, so the plist and the screen agree.
    """
    default = {"Weekday": 5, "Hour": 13, "Minute": 30}
    try:
        from .profile import load_profiles
        from .schedule import finest_period, settings

        cfg = settings(load_profiles(Path(data_dir) / "config"))
        launchd = dict(cfg.get("launchd") or {})
        interval = {"Hour": int(launchd.get("hour", 13)), "Minute": int(launchd.get("minute", 30))}
        if finest_period(cfg) != "daily" and launchd.get("weekday") is not None:
            interval["Weekday"] = int(launchd["weekday"])
        return interval
    except Exception:  # noqa: BLE001 - an unreadable profile must not block reinstalling
        return default


def render(*, data_dir: Path, release_dir: Path | None = None, home: Path,
           origins: list[str]) -> dict[str, dict[str, Any]]:
    """Jobs for one deployment.

    ``data_dir`` owns config, state and runs; ``release_dir`` owns the code and its virtual
    environment. They are the same directory in a single-checkout install and different ones
    once :file:`deploy/macos/release.sh` is used, which is what lets the service keep running a
    fixed version while the checkout it was cut from carries on being edited.

    When a release layout is in use, the jobs reference the ``current`` symlink beside the
    release directories rather than the specific release passed in — so two calls for
    different releases (same data directory and origins) render byte-identical output. That
    property is not cosmetic: a plist that changes on every publish is, to macOS, a LaunchAgent
    being re-registered on every publish. Do that enough times in a short window and
    ``backgroundtaskmanagementd`` throttles it — ``launchctl bootstrap`` keeps reporting
    success while the job silently never starts, logging only "Exceeded max notifications for
    <script>" to the unified log. Publishing new code should mean flipping the symlink and
    restarting the running process (``launchctl kickstart``, which touches no registration),
    not re-registering the LaunchAgent — this is what makes that possible.
    """
    data_dir = Path(data_dir).resolve()
    resolved_release = Path(release_dir).resolve() if release_dir else data_dir
    release_dir = (resolved_release.parent / CURRENT_LINK_NAME
                  if resolved_release != data_dir else resolved_release)
    home = Path(home)
    logs = data_dir / "runs/service"
    environment = {
        "HOME": str(home),
        "PATH": ":".join((str(home / ".local/bin"), "/opt/homebrew/bin", "/usr/local/bin",
                          "/usr/bin", "/bin")),
        "PYTHONUNBUFFERED": "1",
        "CT_AUTH_FILE": str(data_dir / "state/console_auth.json"),
        "CT_CLAUDE_BIN": str(home / ".local/bin/claude"),
        "CT_CONFIG_DIR": str(data_dir / "config"),
        "CT_DATA_DIR": str(data_dir),
        "CT_RELEASE_DIR": str(release_dir),
        "CT_ALLOWED_ORIGINS": ",".join(origins),
    }
    common = {
        "WorkingDirectory": str(data_dir),
        "EnvironmentVariables": environment,
        "ProcessType": "Background",
        "Umask": 0o077,
    }
    return {
        API: {
            **common,
            "ProgramArguments": [str(release_dir / "deploy/macos/run_api.sh")],
            "RunAtLoad": True,
            "KeepAlive": True,
            "ThrottleInterval": 10,
            "ExitTimeOut": 30,
            # launchd hands a job 256 descriptors by default. This process runs an HTTP server
            # alongside four background loops (notifications, reconciliation, heartbeat, research
            # jobs), each holding sockets and log handles, so the default is tight enough that a
            # leak reaches it quickly -- 2026-09-14 spent one on every OAuth retry. That leak is
            # fixed; this is the second line, so the next one has room to be noticed rather than
            # exhausting the process. Soft, so it can still be raised without reinstalling.
            "SoftResourceLimits": {"NumberOfFiles": 4096},
            "StandardOutPath": str(logs / "api.stdout.log"),
            "StandardErrorPath": str(logs / "api.stderr.log"),
        },
        RESEARCH: {
            **common,
            "ProgramArguments": [str(release_dir / "deploy/macos/run_scheduled_research.sh")],
            # In the Mac's local time, from risk_profile.research_schedule.launchd. The trigger
            # itself re-checks the exchange calendar, the period already executed and the daily
            # budget, so an extra firing records why it declined rather than repeating work.
            "StartCalendarInterval": research_interval(data_dir),
            "StandardOutPath": str(logs / "research.stdout.log"),
            "StandardErrorPath": str(logs / "research.stderr.log"),
        },
        BACKUP: {
            **common,
            "ProgramArguments": [str(release_dir / "deploy/macos/backup_state.sh")],
            "StartCalendarInterval": {"Hour": 3, "Minute": 15},
            "LowPriorityIO": True,
            "StandardOutPath": str(logs / "backup.stdout.log"),
            "StandardErrorPath": str(logs / "backup.stderr.log"),
        },
        MAINTENANCE: {
            **common,
            "ProgramArguments": [str(release_dir / "deploy/macos/maintenance.sh")],
            "StartCalendarInterval": {"Hour": 3, "Minute": 45},
            "LowPriorityIO": True,
            "StandardOutPath": str(logs / "maintenance.stdout.log"),
            "StandardErrorPath": str(logs / "maintenance.stderr.log"),
        },
    }


def reregister_research(*, data_dir: Path, launch_agents: Path | None = None,
                        release_dir: Path | None = None, home: Path | None = None,
                        run=None) -> str:
    """Re-register the research job after its schedule changed in config. Returns a summary.

    The same sequence install.sh follows, for the same reasons: the plist is rewritten only
    when its bytes change (re-registering an unchanged LaunchAgent trains macOS to throttle
    it), ``bootout`` is given time to finish before ``bootstrap`` is retried, and the result
    is proven with ``launchctl print``. Only the research job is touched; the API's own
    registration and process are never restarted from here.

    Outside a managed deployment -- no ``CT_RELEASE_DIR``, no installed API plist -- nothing
    is done and the summary says so, which is what a checkout run by hand should see.
    """
    import hashlib
    import os
    import subprocess
    import time

    run = run or (lambda *args: subprocess.run(args, check=False, capture_output=True, text=True, timeout=60))
    launch_agents = Path(launch_agents or Path.home() / "Library/LaunchAgents")
    release_dir = release_dir or (Path(os.environ["CT_RELEASE_DIR"]) if os.environ.get("CT_RELEASE_DIR") else None)
    api_plist = launch_agents / f"{API}.plist"
    if release_dir is None or not api_plist.exists():
        return "launchd not managed here; the schedule applies to the next installed release"
    jobs = render(data_dir=data_dir, release_dir=release_dir, home=home or Path.home(),
                  origins=allowed_origins(origin=None, keep=installed_origins(launch_agents)))
    target = launch_agents / f"{RESEARCH}.plist"
    before = hashlib.sha256(target.read_bytes()).hexdigest() if target.exists() else None
    write(launch_agents, {RESEARCH: jobs[RESEARCH]})
    if before == hashlib.sha256(target.read_bytes()).hexdigest():
        return "launchd job unchanged"
    domain = f"gui/{os.getuid()}"
    run("launchctl", "bootout", f"{domain}/{RESEARCH}")
    for _ in range(60):
        if run("launchctl", "print", f"{domain}/{RESEARCH}").returncode != 0:
            break
        time.sleep(0.5)
    error = ""
    for _ in range(60):
        result = run("launchctl", "bootstrap", domain, str(target))
        if result.returncode == 0:
            break
        error = (result.stderr or result.stdout or "").strip()
        time.sleep(0.5)
    if run("launchctl", "print", f"{domain}/{RESEARCH}").returncode != 0:
        return f"launchd job did not register: {error or 'no error reported'}; run deploy/macos/install.sh"
    run("launchctl", "enable", f"{domain}/{RESEARCH}")
    return "launchd job re-registered"


def write(destination: Path, jobs: dict[str, dict[str, Any]]) -> list[Path]:
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    written = []
    for label, payload in jobs.items():
        target = destination / f"{label}.plist"
        with target.open("wb") as stream:
            plistlib.dump({"Label": label, **payload}, stream, sort_keys=False)
        target.chmod(0o600)
        written.append(target)
    return written


def main(argv: list[str] | None = None) -> int:
    """``python -m agentic_trading.launchd``: render the deployment's launchd jobs."""
    parser = argparse.ArgumentParser(
        prog="ct-launchd", description="Write the macOS launchd jobs for this deployment.")
    parser.add_argument("--data-dir", required=True, type=Path,
                        help="owns config/, state/ and runs/")
    parser.add_argument("--release-dir", type=Path,
                        help="owns the code and .venv; defaults to --data-dir")
    parser.add_argument("--launch-agents", required=True, type=Path)
    parser.add_argument("--home", type=Path, default=Path.home())
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--https-origin", default="", help="set the remote origin")
    group.add_argument("--clear-origins", action="store_true",
                       help="deliberately drop every remote origin and serve loopback only")
    args = parser.parse_args(argv)

    keep = [] if (args.https_origin or args.clear_origins) else installed_origins(args.launch_agents)
    try:
        origins = allowed_origins(origin=args.https_origin or None, keep=keep)
    except ValueError as exc:
        parser.error(str(exc))
    write(args.launch_agents, render(data_dir=args.data_dir, release_dir=args.release_dir,
                                     home=args.home, origins=origins))
    print("allowed origins: " + ", ".join(origins))
    return 0


if __name__ == "__main__":
    sys.exit(main())
