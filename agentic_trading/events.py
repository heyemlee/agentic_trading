"""An append-only journal of what happened, in one order, across every proposal and run.

The console previously assembled a timeline by flattening the research list and the history of
whichever single proposal happened to be open. That could show one proposal's story, never the
system's, and it lost everything the moment a different proposal took the top of the queue.

A journal is written instead. Each entry records when, who, to what, at which version and with
what result, and is appended inside the same transaction as the state change it describes, so
the record cannot claim something the database does not also show.

Two things are deliberately not stored. Nothing is written that would let an entry substitute
for the record it describes: the journal says an order was submitted, and the order row remains
the authority on that order. And a payload is assembled from named fields rather than by
serialising an object, so a credential or a free-text note cannot arrive here by accident.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

#: The closed set of things worth remembering. An unknown kind is a defect, not a new event,
#: because the interface filters on exactly these.
KINDS = {
    "proposal_created": "a proposal was created",
    "proposal_approved": "the investor approved a proposal",
    "proposal_rejected": "the investor rejected a proposal",
    "proposal_invalidated": "a proposal was invalidated before execution",
    "proposal_reanalysis": "fresh research was requested for a proposal",
    "proposal_executed": "an approved proposal reached the venue",
    "order_filled": "a fill was recorded against the ledger",
    "research_completed": "a research run finished",
    "research_failed": "a research run did not finish",
    "system_paused": "the kill switch was engaged",
    "system_resumed": "the kill switch was released",
}

#: Proposal history verbs, as written by Proposal.note(), mapped onto journal kinds.
FROM_HISTORY = {
    "created": "proposal_created",
    "approved": "proposal_approved",
    "rejected_by_investor": "proposal_rejected",
    "invalidated": "proposal_invalidated",
    "reanalysis_requested": "proposal_reanalysis",
    "executed": "proposal_executed",
    "submitted": "proposal_executed",
}

#: Field names that must never reach the journal, whatever a caller passes.
FORBIDDEN = ("token", "secret", "password", "csrf", "cookie", "api_key", "access", "refresh",
             "authorization", "note", "account_number")

BACKFILLED = "events_backfilled"
MAX_LIMIT = 200


def redact(detail: dict[str, Any] | None) -> dict[str, Any]:
    """Drop anything that looks like a credential or free text a person typed.

    A safety net, not the design: callers assemble payloads from named fields. The approval
    note is excluded on purpose — it is the investor's own words, kept on the proposal where it
    belongs rather than copied into a stream built for scanning.
    """
    out: dict[str, Any] = {}
    for key, value in (detail or {}).items():
        lowered = str(key).lower()
        if any(banned in lowered for banned in FORBIDDEN):
            continue
        out[key] = value
    return out


def record(conn, *, kind: str, subject: str | None = None, actor: str = "system",
           version: str | None = None, outcome: str | None = None,
           detail: dict[str, Any] | None = None, at: str | None = None) -> None:
    """Append one entry. Takes a live connection so it joins the caller's transaction."""
    if kind not in KINDS:
        raise ValueError(f"unknown event kind {kind!r}")
    conn.execute(
        "INSERT INTO events(at,kind,subject,actor,version,outcome,detail) VALUES(?,?,?,?,?,?,?)",
        (at or datetime.now(UTC).isoformat(), kind, subject, actor, version, outcome,
         json.dumps(redact(detail), allow_nan=False, default=str)))


def emit(db, **fields: Any) -> None:
    """Append outside an existing transaction, for callers that have no connection to hand."""
    with db.transaction() as conn:
        record(conn, **fields)


def from_history(conn, proposal, *, only_after: str | None = None) -> int:
    """Append journal entries for a proposal's history that are not in the journal yet.

    The proposal's own history is append-only and timestamped to the microsecond, so "newer
    than the newest entry already recorded for this proposal" is both idempotent and enough to
    catch up a proposal that predates the journal.
    """
    if only_after is None:
        row = conn.execute("SELECT max(at) FROM events WHERE subject=?",
                           (proposal.proposal_id,)).fetchone()
        only_after = row[0] if row else None
    written = 0
    for entry in proposal.history:
        at = str(entry.get("at") or "")
        kind = FROM_HISTORY.get(str(entry.get("event")))
        if not at or not kind or (only_after and at <= only_after):
            continue
        record(conn, kind=kind, subject=proposal.proposal_id, at=at,
               actor=str(entry.get("identity") or "system"),
               version=entry.get("version") or None,
               outcome=proposal.status,
               detail={"ticker": proposal.ticker, "bucket": proposal.bucket,
                       "action": proposal.action, "notional_usd": proposal.notional_usd,
                       "event": entry.get("event")})
        written += 1
    return written


def backfill(db, proposals) -> int:
    """Catch the journal up with proposals that closed before it existed. Runs once."""
    if db.read_meta(BACKFILLED, None):
        return 0
    written = 0
    with db.transaction() as conn:
        for proposal in proposals:
            written += from_history(conn, proposal)
        db.write_meta(BACKFILLED, {"at": datetime.now(UTC).isoformat(), "entries": written}, conn)
    return written


def query(db, *, kinds: tuple[str, ...] = (), subject: str | None = None,
          cursor: int | None = None, limit: int = 50) -> dict[str, Any]:
    """One page of the journal, newest first, with a stable cursor.

    The cursor is the row id rather than a timestamp: several entries can share an instant, and
    paging by time would drop or repeat them at the page boundary.
    """
    limit = max(1, min(int(limit), MAX_LIMIT))
    conn = db.current() or db._read()
    if conn is None:
        return {"events": [], "next_cursor": None, "kinds": KINDS}
    owned = db.current() is None
    clauses, params = [], []
    if kinds:
        clauses.append("kind IN (" + ",".join("?" for _ in kinds) + ")")
        params.extend(kinds)
    if subject:
        clauses.append("subject=?")
        params.append(subject)
    if cursor:
        clauses.append("id<?")
        params.append(int(cursor))
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    try:
        rows = conn.execute(
            f"SELECT id,at,kind,subject,actor,version,outcome,detail FROM events{where} "
            "ORDER BY id DESC LIMIT ?", (*params, limit + 1)).fetchall()
    finally:
        if owned:
            conn.close()
    page = [dict(row) for row in rows[:limit]]
    for item in page:
        item["detail"] = json.loads(item["detail"]) if item["detail"] else {}
        item["description"] = KINDS.get(item["kind"], item["kind"])
    return {"events": page,
            "next_cursor": page[-1]["id"] if len(rows) > limit and page else None,
            "kinds": KINDS}
