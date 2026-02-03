"""Read every document this deployment has actually stored through the API's response models.

The models in ``agentic_trading/service/shapes.py`` are enforcement, not documentation: a field
they declare that the stored data does not match is a 500 on the screen that reads it. The test
suite cannot find that class of mistake, because it writes its own fixtures — and a fixture is
built from the shape the constructor produces, while the thing on disk is the shape every later
pass left behind. The first run of this tool found exactly that: every stored holding carries
``quote_at`` and ``weight_pct``, added by the pricing pass rather than by the construction, and
``/advisor`` would have answered 500 against this deployment's own saved data.

    uv run python tools/validate_stored_shapes.py

Read-only, and offline. Nothing here opens the live database: state and runs are snapshotted to
a temporary directory first — the SQLite copy through a ``mode=ro`` connection, so the running
API's own file is never written, not even its write-ahead log. Nothing here reaches the venue
either: the two service methods that would open a broker session are deliberately not called,
and the rows they project from are read from the snapshot instead.

Exits non-zero if any stored document fails to validate, so it can be a push or release gate.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import ValidationError

#: Copied into the snapshot: the ledgers and the drawdown marks, by extension, plus the database
#: that holds the proposals, the fills and the assistant's saved documents.
STATE_SUFFIXES = (".yaml", ".json")

#: Never copied, and named rather than filtered by pattern. These are the files CLAUDE.md lists
#: as holding credentials; none of them describes a response shape, so a tool that reads stored
#: documents has no reason to hold a copy of any of them, however short-lived.
STATE_SECRETS = ("console_auth.json", "robinhood_token.json", "vapid_key.json")


def snapshot(project_dir: Path, into: Path) -> Path:
    """A copy of config, state and runs, without the credentials and without the 126MB of runs.

    Only the documents a response model is built from are copied. The rest of a run's artefacts
    — the reports, the transcripts — are never projected into a response and would make this
    tool slow enough that nobody would put it in front of a push.
    """
    root = into / "deployment"
    shutil.copytree(project_dir / "config", root / "config")
    (root / "state").mkdir(parents=True)
    (root / "runs").mkdir(parents=True)

    for path in sorted((project_dir / "state").iterdir()):
        if not path.is_file() or path.name in STATE_SECRETS:
            continue
        if path.suffix in STATE_SUFFIXES:
            shutil.copy2(path, root / "state" / path.name)

    for database in sorted((project_dir / "state").glob("*.sqlite3")):
        # A read-only source connection, so the API that holds this file open keeps every byte
        # of it: no journal, no -wal, no migration on open. The backup is a consistent read.
        source = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        destination = sqlite3.connect(root / "state" / database.name)
        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()

    for record in sorted((project_dir / "runs").glob("*/decision_record.json")):
        run = root / "runs" / record.parent.name
        run.mkdir(parents=True, exist_ok=True)
        shutil.copy2(record, run / record.name)

    audit = project_dir / "runs" / "llm_calls.jsonl"
    if audit.exists():
        shutil.copy2(audit, root / "runs" / audit.name)
    return root


def checks(service: Any) -> list[tuple[str, Callable[[], str]]]:
    """Every projection that reads a stored document, and nothing that reaches the venue.

    ``orders`` and ``micro_live`` are absent on purpose: both construct a Robinhood session, and
    this tool must be safe to run against an armed deployment. The rows ``orders`` projects are
    read here directly instead, which is the half of it that stored data can contradict.
    """
    from agentic_trading.service import _hard_risk_view
    from agentic_trading.service.shapes import Fill
    from agentic_trading.state_db import StateDB

    profiles = service.profiles()

    def fills() -> str:
        configured = str((profiles.risk.get("broker") or {}).get("fills_file", "state/fills.jsonl"))
        path = Path(configured)
        if not path.is_absolute():
            path = profiles.project_dir / path
        rows = StateDB(path).fills(100_000)
        for row in rows:
            Fill(**row)
        return f"{len(rows)} recorded fills"

    def proposals() -> str:
        listed = service.proposals(include_closed=True)
        for row in listed:
            # The detail is where the research and Hard Risk blocks are projected out of a run
            # record, so reading every one of them is what exercises those two models.
            service.proposal(row.proposal_id)
        return f"{len(listed)} proposals, each also read as detail"

    def records() -> str:
        found = sorted((profiles.project_dir / "runs").glob("*/decision_record.json"))
        for path in found:
            _hard_risk_view(json.loads(path.read_text(encoding="utf-8")).get("hard_risk") or {})
        return f"{len(found)} decision records through HardRiskView"

    def advisor() -> str:
        state = service.advisor_state()
        for conversation in state.conversations:
            service.advisor_conversation(conversation)
        held = sum(len(account.positions) for account in state.snapshot.accounts)
        return (f"snapshot of {len(state.snapshot.accounts)} accounts holding {held}, "
                f"{len(state.jobs)} jobs, {len(state.decisions)} decisions, "
                f"{len(state.conversations)} conversations")

    def portfolio() -> str:
        book = service.portfolio()
        return (f"{len(book.buckets)} buckets across {len(book.accounts)} accounts"
                + (f", {len(book.errors)} unreadable" if book.errors else ""))

    def health() -> str:
        state = service.health()
        return f"status {state.status.value}"

    return [("fills", fills), ("proposals", proposals), ("decision records", records),
            ("advisor", advisor), ("portfolio", portfolio), ("health", health)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="validate_stored_shapes",
        description="Read this deployment's stored documents through the API's response models.",
        epilog="Read-only: the live database is snapshotted through a read-only connection and "
               "no venue session is opened. Exits non-zero if a stored document does not match "
               "the shape the API promises for it.")
    parser.add_argument("--config-dir", default="config",
                        help="the live config directory; its parent is the data directory")
    parser.add_argument("--keep", action="store_true",
                        help="leave the snapshot in place, to inspect a document that failed")
    args = parser.parse_args(argv)

    config_dir = Path(args.config_dir).resolve()
    project_dir = config_dir.parent
    for required in ("state", "runs"):
        if not (project_dir / required).is_dir():
            print(f"{project_dir / required} is not a directory: this tool reads a deployment's "
                  f"own stored documents, so it has nothing to check without one.", file=sys.stderr)
            return 2

    # resolve(): the snapshot's own project_dir is compared against this, and on macOS the
    # temporary directory is reached through a symlink that would make the two differ.
    holder = Path(tempfile.mkdtemp(prefix="ct-stored-shapes-")).resolve()
    try:
        root = snapshot(project_dir, holder)
        from agentic_trading.service import Service

        service = Service(root / "config")
        passed: list[tuple[str, str]] = []
        failed: list[tuple[str, str, str]] = []
        for label, check in checks(service):
            try:
                passed.append((label, check()))
            except ValidationError as exc:
                failed.append((label, "does not match the declared shape", str(exc)))
            except Exception as exc:  # noqa: BLE001 - any failure to read is worth reporting
                failed.append((label, f"could not be read ({type(exc).__name__})", str(exc)))

        for label, detail in passed:
            print(f"  ok    {label:18} {detail}")
        for label, summary, detail in failed:
            print(f"  FAIL  {label:18} {summary}\n{detail}\n", file=sys.stderr)

        if failed:
            print(f"\n{len(failed)} stored document(s) contradict the shape the API promises. "
                  f"Each one is a 500 on the screen that reads it.", file=sys.stderr)
            if args.keep:
                print(f"snapshot kept at {root}", file=sys.stderr)
            return 1
        print("\nevery stored document matches the shape the API promises for it")
        return 0
    finally:
        if not args.keep:
            shutil.rmtree(holder, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
