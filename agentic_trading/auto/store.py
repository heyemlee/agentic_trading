"""What automatic trading remembers: the plan a person confirmed, the strategy versions, the pause.

Three documents in the state database, beside the ledger they describe. Each account's are its
own -- the simulated account has its own database (``auto.paper``) -- so nothing here needs to
say which account it is about, and a record of the simulated account can never be read as the
real one's.

**A strategy version is identified by everything that decides what it does.** Its id is a hash
of its mode, its template, its parameters, the models it reasons with and the code that carries
it out. Change any one and the id changes, and with it the record: a new version starts with no
simulated days and no verified exits of its own, whatever its predecessor had earned. Nothing
copies a validation forward, because a validation is a statement about one exact thing.

**Nothing here decides anything.** These are records and the small state machine around them:
which plan is in force, which version opens new positions, whether a person or a loss line has
paused it. What may be traded is still the risk profile's to say, read where it always is.
"""

from __future__ import annotations

import ast
import hashlib
import json
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from .. import UPSTREAM_TRADINGAGENTS_COMMIT, __version__

PLAN = "auto.plan"
VERSIONS = "auto.strategy_versions"
STATE = "auto.state"
DAYS = "auto.days"
EQUITY = "auto.equity"
LAST_PASS = "auto.last_pass"
REQUEST = "auto.start_request"

MAX_HISTORY = 50

#: What paused the plan. They are cleared by the same act but mean different things: only a
#: resume from the loss line moves where the line is measured from.
PERSON, LOSS_LINE = "person", "loss_line"

#: Where a version stands. A version is born unvalidated and nothing but its own record moves it.
UNVALIDATED, SIMULATING, VALIDATED, RETIRED, REJECTED = (
    "unvalidated", "simulating", "validated", "retired", "rejected")

#: The parts of a plan's strategy that are behaviour. A rationale is words about it, and two
#: strategies that differ only in how they were explained are the same strategy.
BEHAVIOUR = ("template", "max_positions", "position_pct", "min_confidence", "stop_loss_pct",
             "take_profit_pct", "max_holding_days", "trend_window", "universe")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _today() -> str:
    """The market's own date, which is the day a strategy fixed now was fixed on."""
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo("America/New_York")).date().isoformat()


#: The modules that decide what an order is: what research becomes, how it is sized, what may
#: be approved without a person, when a position is left. A change to any of them is a change
#: to the trading logic, and a strategy carried out by different logic is a different strategy.
#: Kept to those: what only words a result (``diagnosis``) or records it (this module) is not
#: here, or every release would restart every validation for nothing.
LOGIC = ("policy.py", "sizing.py", "risk/engine.py", "execution.py", "autonomy.py",
         "auto/trader.py", "auto/positions.py")
#: What the model is told about the person it works for. Part of "the model", as far as what it
#: concludes goes.
MANDATE = ("MANDATE.md", "mandate_context.md")


@lru_cache(maxsize=1)
def logic_digest() -> str:
    """A digest of the trading logic as it is running, by what it does rather than how it reads.

    Taken over each module's syntax tree, so a comment or a reflowed line does not make a new
    strategy of an unchanged one; anything that could change behaviour does. A module that
    cannot be read or parsed is part of the digest as exactly that, never skipped.
    """
    package = Path(__file__).resolve().parent.parent
    digest = hashlib.sha256()
    for name in LOGIC:
        digest.update(name.encode())
        try:
            tree = ast.parse((package / name).read_text(encoding="utf-8"))
            digest.update(ast.dump(tree, annotate_fields=False).encode())
        except (OSError, SyntaxError, ValueError) as exc:
            digest.update(f"unreadable:{type(exc).__name__}".encode())
    return digest.hexdigest()[:16]


def mandate_digest(profiles: Any) -> str:
    digest = hashlib.sha256()
    for name in MANDATE:
        digest.update(name.encode())
        try:
            digest.update((Path(profiles.config_dir) / name).read_bytes())
        except OSError:
            digest.update(b"absent")
    return digest.hexdigest()[:16]


def code_identity(profiles: Any) -> dict[str, Any]:
    """The models and the code a strategy runs on. Part of what a version *is*.

    The package's version number is here for the record and decides nothing by itself: it has
    been 0.1.0 through every change so far. What moves a version is ``logic``.
    """
    return {"package_version": __version__,
            "upstream_commit": UPSTREAM_TRADINGAGENTS_COMMIT,
            "deep_model": profiles.tradingagents.deep_think_llm,
            "quick_model": profiles.tradingagents.quick_think_llm,
            "logic": logic_digest(),
            "mandate": mandate_digest(profiles)}


def version_id(mode: str, strategy: dict[str, Any], code: dict[str, Any]) -> str:
    behaviour = {key: strategy.get(key) for key in BEHAVIOUR}
    blob = json.dumps({"mode": mode, "strategy": behaviour, "code": code}, sort_keys=True,
                      separators=(",", ":"), default=str)
    return "sv-" + hashlib.sha256(blob.encode()).hexdigest()[:12]


# ---------------------------------------------------------------- strategy versions
def versions(db) -> list[dict[str, Any]]:
    return list((db.read_meta(VERSIONS, {}) or {}).get("versions") or [])


def version(db, wanted: str | None) -> dict[str, Any] | None:
    return next((item for item in versions(db) if item.get("version_id") == wanted), None)


def record_version(db, profiles: Any, *, mode: str, strategy: dict[str, Any], plan_id: str,
                   data_as_of: str | None, created_by: str, parent: str | None = None,
                   reason: str = "", conn=None) -> dict[str, Any]:
    """The version for this exact strategy on this exact code: the one on record, or a new one.

    Asked twice for the same thing it answers with the same record, so a plan confirmed again
    unchanged does not restart its own validation. Asked for anything different it writes a new
    record whose validation is empty: not the parent's, which is kept on the parent.
    """
    code = code_identity(profiles)
    wanted = version_id(mode, strategy, code)
    with db.transaction() as tx:
        record = db.read_meta(VERSIONS, {}) or {}
        items = list(record.get("versions") or [])
        found = next((item for item in items if item.get("version_id") == wanted), None)
        if found is not None:
            return found
        fresh = {
            "version_id": wanted, "mode": mode,
            "strategy": {key: strategy.get(key) for key in BEHAVIOUR},
            "rationale": strategy.get("rationale"),
            "suggested_by": strategy.get("suggested_by") or created_by,
            "code": code, "plan_id": plan_id, "parent": parent,
            "created_at": _now(), "created_by": created_by, "reason": reason[:500],
            #: What the strategy was designed on, so a later test can prove it used later data.
            "data_as_of": data_as_of,
            # Earned by this version alone. A parent's days are the parent's.
            "validation": {"status": UNVALIDATED, "simulated_days": 0, "exits_verified": False,
                           "inherited": False, "updated_at": None},
            "evidence": [],
        }
        items.append(fresh)
        record["versions"] = items
        db.write_meta(VERSIONS, record, conn or tx)
        return fresh


def update_validation(db, wanted: str, **changes: Any) -> dict[str, Any] | None:
    """Move a version's own validation record. Returns it, or None for a version not on record."""
    with db.transaction() as conn:
        record = db.read_meta(VERSIONS, {}) or {}
        items = list(record.get("versions") or [])
        for item in items:
            if item.get("version_id") == wanted:
                item["validation"] = {**(item.get("validation") or {}), **changes,
                                      "inherited": False, "updated_at": _now()}
                record["versions"] = items
                db.write_meta(VERSIONS, record, conn)
                return item
    return None


def add_evidence(db, wanted: str, entry: dict[str, Any]) -> dict[str, Any] | None:
    """Attach one piece of evidence -- a backtest, a simulated record -- to the version it is of."""
    with db.transaction() as conn:
        record = db.read_meta(VERSIONS, {}) or {}
        items = list(record.get("versions") or [])
        for item in items:
            if item.get("version_id") == wanted:
                # One entry per experiment: the same card recorded again replaces itself, so
                # running an experiment twice does not make it look like two.
                kept = [row for row in item.get("evidence") or []
                        if not entry.get("card_id") or row.get("card_id") != entry["card_id"]]
                item["evidence"] = [*kept, {**entry, "recorded_at": _now()}]
                record["versions"] = items
                db.write_meta(VERSIONS, record, conn)
                return item
    return None


# ---------------------------------------------------------------- the plan in force
def plan_record(db) -> dict[str, Any]:
    return dict(db.read_meta(PLAN, {}) or {})


def active_plan(db) -> dict[str, Any] | None:
    """The plan a person confirmed and has not withdrawn, or None."""
    current = plan_record(db).get("active")
    return dict(current) if isinstance(current, dict) and current.get("status") == "active" else None


def confirm_plan(db, profiles: Any, document: dict[str, Any], *, identity: str,
                 scope: str, result_usd: float | None = None) -> dict[str, Any]:
    """Put a plan in force, and name the strategy version that will open positions under it.

    The version that was opening positions before is not discarded: it goes to ``managing``,
    where it keeps looking after what it bought until those positions are gone (docs/todo.md:
    a switch of mode leaves old positions with the strategy that opened them).

    ``result_usd`` is what automatic trading in this account has made or lost so far. Given, it
    becomes where this plan's loss line is measured from: the line a person sets with a plan is
    about what happens under that plan, not a debt carried in from the one before it.
    """
    with db.transaction() as conn:
        if result_usd is not None:
            current = state(db)
            current["loss_baseline_usd"] = round(float(result_usd), 2)
            db.write_meta(STATE, current, conn)
        record = db.read_meta(PLAN, {}) or {}
        previous = record.get("active") if isinstance(record.get("active"), dict) else None
        parent = (previous or {}).get("opening_version")
        strategy = document["strategy"]
        made = record_version(
            db, profiles, mode=document["envelope"]["mode"], strategy=strategy,
            plan_id=document["plan_id"], data_as_of=(document.get("account") or {}).get("as_of"),
            created_by=strategy.get("suggested_by") or "template", parent=parent,
            reason="plan confirmed", conn=conn)
        # Whatever was opening or managing before may still own a position: the plan before
        # this one, and, after a withdrawal, the versions that plan left behind.
        managing = [*((previous or {}).get("managing") or []), parent,
                    *(record.get("orphaned_versions") or [])]
        active = {
            "plan_id": document["plan_id"], "scope": scope, "status": "active",
            "envelope": document["envelope"], "strategy": strategy,
            "changes": document.get("changes") or [],
            # What the plan could actually put to work when it was confirmed, every limit
            # counted. What a comparison holds the alternatives to, so they get no more room.
            "deployable_usd": (document.get("under_plan") or {}).get("deployable_usd"),
            "opening_version": made["version_id"],
            # Versions that open nothing more but still own positions they opened.
            "managing": [item for item in dict.fromkeys(managing)
                         if item and item != made["version_id"]],
            "confirmed_at": _now(), "confirmed_by": identity,
        }
        history = list(record.get("history") or [])
        if previous is not None:
            history.append({**previous, "status": "superseded", "superseded_at": active["confirmed_at"]})
        db.write_meta(PLAN, {"active": active, "history": history[-MAX_HISTORY:]}, conn)
        return active


def adjust_strategy(db, profiles: Any, strategy: dict[str, Any], *, identity: str,
                    reason: str, data_as_of: str | None = None) -> dict[str, Any] | None:
    """Change the strategy the plan in force opens positions with, inside the same envelope.

    The envelope, the plan's id and the settings it was confirmed with do not move: those are
    the person's, and this is not their act. What moves is which strategy version opens from
    here on. It is a new version with nothing on its record, and the one it replaces goes on
    managing what it bought. Returns the plan as it now stands, or None when none is in force.
    """
    with db.transaction() as conn:
        record = db.read_meta(PLAN, {}) or {}
        current = record.get("active")
        if not isinstance(current, dict) or current.get("status") != "active":
            return None
        parent = current.get("opening_version")
        made = record_version(
            db, profiles, mode=current["envelope"]["mode"], strategy=strategy,
            # Fixed today, by whoever or whatever changed it, with everything up to today there
            # to be seen. Only what comes after can test it; left empty, nothing ever could.
            # A caller that knows the day it was really fixed -- a candidate fixed months ago in
            # the simulated account -- says so.
            plan_id=current["plan_id"], data_as_of=data_as_of or _today(),
            created_by=strategy.get("suggested_by") or identity, parent=parent,
            reason=reason, conn=conn)
        if made["version_id"] == parent:
            return dict(current)                 # the same strategy: nothing to adjust
        managing = [*(current.get("managing") or []), parent]
        updated = {**current, "strategy": strategy, "opening_version": made["version_id"],
                   "managing": [item for item in dict.fromkeys(managing)
                                if item and item != made["version_id"]],
                   "adjusted_at": _now(), "adjusted_by": identity}
        history = [*(record.get("history") or []),
                   {**current, "status": "adjusted", "superseded_at": updated["adjusted_at"]}]
        db.write_meta(PLAN, {**record, "active": updated, "history": history[-MAX_HISTORY:]}, conn)
        return updated


def withdraw_plan(db, *, identity: str, reason: str) -> dict[str, Any] | None:
    """Take the plan out of force. Positions it opened keep the version that opened them."""
    with db.transaction() as conn:
        record = db.read_meta(PLAN, {}) or {}
        current = record.get("active")
        if not isinstance(current, dict) or current.get("status") != "active":
            return None
        ended = {**current, "status": "withdrawn", "withdrawn_at": _now(),
                 "withdrawn_by": identity, "withdrawn_reason": reason[:500]}
        history = [*(record.get("history") or []), ended][-MAX_HISTORY:]
        db.write_meta(PLAN, {"active": None, "history": history,
                             # Still looked after, by whichever version opened each one.
                             "orphaned_versions": [current.get("opening_version"),
                                                   *(current.get("managing") or [])]}, conn)
        return ended


def known_versions(db) -> set[str]:
    """Every version that may still own a position: the opener, the managers, the withdrawn."""
    record = plan_record(db)
    current = record.get("active") if isinstance(record.get("active"), dict) else {}
    out = {current.get("opening_version"), *(current.get("managing") or []),
           *(record.get("orphaned_versions") or [])}
    return {item for item in out if item}


def running_version_id(profiles: Any, plan: dict[str, Any]) -> str:
    """The id this plan's strategy has on the code, the models and the mandate as they are now."""
    return version_id(plan["envelope"]["mode"], plan["strategy"], code_identity(profiles))


def moved_on(profiles: Any, plan: dict[str, Any] | None) -> bool:
    """Whether the plan's opening version is no longer what would be running if it opened now.

    True after a release that changed the trading logic, a change of model, or an edit to the
    mandate. The strategy's numbers are the same and the thing carrying them out is not, which
    by this module's own rule is a different version with nothing on its record.
    """
    return plan is not None and running_version_id(profiles, plan) != plan.get("opening_version")


def rebind(db, profiles: Any, *, identity: str = "auto:code_changed") -> dict[str, Any] | None:
    """Move the plan in force to the version its strategy has on the code now running.

    The simulated account's answer to :func:`moved_on`: it goes on simulating, under a new
    version that starts with an empty record, while the version it replaces keeps looking after
    what it bought. Never the real account's answer -- there an unvalidated version may not open
    anything, so the plan is taken out of force instead (``auto.runner``).
    """
    plan = active_plan(db)
    if plan is None or not moved_on(profiles, plan):
        return plan
    return adjust_strategy(
        db, profiles, plan["strategy"], identity=identity,
        reason="the trading logic, the models or the mandate this strategy runs on changed; "
               "the same parameters on different code are a different version")


def option(db, name: str, default: Any = None) -> Any:
    """One choice a person made about the plan in force that is not part of its strategy."""
    return ((active_plan(db) or {}).get("options") or {}).get(name, default)


def set_option(db, name: str, value: Any, *, by: str) -> dict[str, Any] | None:
    """Record such a choice on the plan in force. Returns the plan, or None when none is."""
    with db.transaction() as conn:
        record = db.read_meta(PLAN, {}) or {}
        current = record.get("active")
        if not isinstance(current, dict) or current.get("status") != "active":
            return None
        options = {**(current.get("options") or {}), name: value}
        updated = {**current, "options": options,
                   "options_changed": {"at": _now(), "by": by, "name": name}}
        db.write_meta(PLAN, {**record, "active": updated}, conn)
        return updated


def opened_modes(db) -> tuple[str, ...]:
    """The modes a person has put in force in this account at some time, oldest first.

    Read from the plan record: the plan in force and the ones before it. For the real account
    this is what "that mode has been opened" means -- a person started it there, behind every
    condition that stood in front of doing so.
    """
    record = plan_record(db)
    rows = [*(record.get("history") or []), record.get("active")]
    seen = [str((row.get("envelope") or {}).get("mode")) for row in rows
            if isinstance(row, dict) and (row.get("envelope") or {}).get("mode")]
    return tuple(dict.fromkeys(seen))


# ---------------------------------------------------------------- a start that is waiting
#
# A plan a person confirmed for the real account whose settings are not all in force yet: the
# ones that loosen a limit wait out the console's cooling period like any other. The request is
# the record that they confirmed it, what exactly, and until when it stands. It puts nothing in
# force by itself -- ``Service._auto_live_activate`` does that, once, when nothing is waiting any
# more and every condition holds at that moment.

#: How long a confirmed start stands while its settings wait. A day of cooling and two more for
#: a weekend; after that the person is asked again rather than surprised.
REQUEST_HOURS = 72.0
MAX_REQUESTS = 20


def start_request(db) -> dict[str, Any] | None:
    """The start that is waiting, or None."""
    found = (db.read_meta(REQUEST, {}) or {}).get("pending")
    return dict(found) if isinstance(found, dict) else None


def request_history(db) -> list[dict[str, Any]]:
    return list((db.read_meta(REQUEST, {}) or {}).get("history") or [])


def write_request(db, request: dict[str, Any]) -> dict[str, Any]:
    """Record a confirmed start as waiting. One at a time: a newer one replaces the older."""
    with db.transaction() as conn:
        record = db.read_meta(REQUEST, {}) or {}
        history = list(record.get("history") or [])
        if isinstance(record.get("pending"), dict):
            history.append({**record["pending"], "status": "replaced", "ended_at": _now()})
        db.write_meta(REQUEST, {"pending": request, "history": history[-MAX_REQUESTS:]}, conn)
    return request


def end_request(db, *, status: str, by: str, reason: str = "",
                extra: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Close the waiting start as activated, withdrawn or lapsed. Returns it, or None if none was."""
    with db.transaction() as conn:
        record = db.read_meta(REQUEST, {}) or {}
        pending = record.get("pending")
        if not isinstance(pending, dict):
            return None
        ended = {**pending, **(extra or {}), "status": status, "ended_at": _now(), "ended_by": by,
                 "ended_reason": reason[:500]}
        history = [*(record.get("history") or []), ended][-MAX_REQUESTS:]
        db.write_meta(REQUEST, {"pending": None, "history": history}, conn)
        return ended


def note_request(db, **facts: Any) -> None:
    """Keep what the last look at the waiting start found, so a screen can say why it waits."""
    with db.transaction() as conn:
        record = db.read_meta(REQUEST, {}) or {}
        pending = record.get("pending")
        if not isinstance(pending, dict):
            return
        if all(pending.get(key) == value for key, value in facts.items() if key != "checked_at"):
            return
        db.write_meta(REQUEST, {**record, "pending": {**pending, **facts}}, conn)


# ---------------------------------------------------------------- the record of having run
#
# A session counts toward a version's record only if the account was actually being run through
# it: passes while the market was open, far enough apart to have watched some of it, and nothing
# wrong on any of them. It is a record of observation and says nothing about whether the
# strategy made money -- a day on which nothing was bought counts exactly as much as one on
# which something was, so there is never a reason to trade in order to be counted.

#: How far apart, within one session, the first and last open-market passes must be.
MIN_OBSERVED_MINUTES = 60
#: How often a session's record is rewritten while nothing about it changes.
REMARK_SECONDS = 300
MAX_SESSIONS = 800


def _minutes_between(first: str | None, last: str | None) -> float:
    try:
        return (datetime.fromisoformat(str(last)) - datetime.fromisoformat(str(first))).total_seconds() / 60.0
    except (TypeError, ValueError):
        return 0.0


def session_valid(row: dict[str, Any]) -> bool:
    """Whether one session's record counts. Derived every time it is asked, never stored."""
    return (not row.get("problems") and int(row.get("open_passes") or 0) >= 2
            and _minutes_between(row.get("first_open"), row.get("last_open")) >= MIN_OBSERVED_MINUTES)


def mark_pass(db, wanted: str, session: str, *, at: datetime, market_open: bool,
              problems: list[str]) -> dict[str, Any]:
    """Note that a pass ran for this version in this session, and anything that was wrong on it.

    A problem seen on any pass stays on the session: a day is not made good again by the passes
    after the one that went wrong.
    """
    stamp = at.astimezone(UTC).isoformat()
    with db.transaction() as conn:
        record = db.read_meta(DAYS, {}) or {}
        by_version = dict(record.get("versions") or {})
        sessions = dict(by_version.get(wanted) or {})
        before = dict(sessions.get(session) or {})
        row = {"first": before.get("first") or stamp, "last": stamp,
               "passes": int(before.get("passes") or 0) + 1,
               "open_passes": int(before.get("open_passes") or 0) + (1 if market_open else 0),
               "first_open": before.get("first_open") or (stamp if market_open else None),
               "last_open": stamp if market_open else before.get("last_open"),
               "problems": sorted({*(before.get("problems") or []), *problems})}
        unchanged = (before and row["problems"] == (before.get("problems") or [])
                     and session_valid(row) == session_valid(before)
                     and _minutes_between(before.get("written"), stamp) * 60.0 < REMARK_SECONDS)
        if unchanged:
            return before
        row["written"] = stamp
        sessions[session] = row
        by_version[wanted] = {key: sessions[key] for key in sorted(sessions)[-MAX_SESSIONS:]}
        record["versions"] = by_version
        db.write_meta(DAYS, record, conn)
        return row


def sessions_of(db, wanted: str | None) -> dict[str, dict[str, Any]]:
    return dict(((db.read_meta(DAYS, {}) or {}).get("versions") or {}).get(wanted) or {})


def simulated_days(db, wanted: str | None) -> int:
    """Sessions on this version's own record that count. Counted, every time, from the record."""
    return sum(1 for row in sessions_of(db, wanted).values() if session_valid(row))


def note_pass(db, *, at: datetime, error: str | None, market_open: bool) -> None:
    """When the account was last run, and whether that pass worked. Overwritten every pass.

    The session record above is rewritten every few minutes at most; this is the one line that
    says "it ran a minute ago", which is what tells a plan that is quiet from one that stopped.
    """
    db.write_meta(LAST_PASS, {"at": at.astimezone(UTC).isoformat(), "error": error,
                              "market_open": bool(market_open)})


def last_pass(db) -> dict[str, Any] | None:
    found = db.read_meta(LAST_PASS, None)
    return dict(found) if isinstance(found, dict) and found.get("at") else None


def mark_equity(db, session: str, *, at: datetime, figures: dict[str, Any]) -> None:
    """Keep what the account was worth in this session: the last figure of the day stands."""
    stamp = at.astimezone(UTC).isoformat()
    with db.transaction() as conn:
        record = db.read_meta(EQUITY, {}) or {}
        marks = dict(record.get("marks") or {})
        before = marks.get(session) or {}
        if before and _minutes_between(before.get("at"), stamp) * 60.0 < REMARK_SECONDS \
                and before.get("equity_usd") == figures.get("equity_usd"):
            return
        marks[session] = {**figures, "at": stamp}
        record["marks"] = {key: marks[key] for key in sorted(marks)[-MAX_SESSIONS:]}
        # What the account was worth at the first pass ever made over it, written once. A
        # session keeps only its last figure, so without this the day a record began would have
        # no "before" to be measured from.
        record.setdefault("opened", {**figures, "at": stamp, "session": session})
        db.write_meta(EQUITY, record, conn)


def equity_marks(db) -> dict[str, dict[str, Any]]:
    return dict((db.read_meta(EQUITY, {}) or {}).get("marks") or {})


def equity_opened(db) -> dict[str, Any] | None:
    """What the account was worth when its record began, or None before the first pass."""
    found = (db.read_meta(EQUITY, {}) or {}).get("opened")
    return dict(found) if isinstance(found, dict) else None


# ---------------------------------------------------------------- paused, and why
def state(db) -> dict[str, Any]:
    stored = dict(db.read_meta(STATE, {}) or {})
    stored.setdefault("paused", False)
    stored.setdefault("reason", None)
    stored.setdefault("at", None)
    stored.setdefault("by", None)
    stored.setdefault("kind", None)
    stored.setdefault("history", [])
    #: What automatic trading had made or lost when the loss line last began to be measured.
    stored.setdefault("loss_baseline_usd", 0.0)
    return stored


def pause(db, *, reason: str, by: str, kind: str = "person") -> dict[str, Any]:
    """Stop opening positions. The first reason is the one kept; exits and reconciliation go on.

    ``kind`` says what paused it -- a person, the loss line, an order nobody can account for --
    because they are cleared by different things and a screen has to say which this is.
    """
    with db.transaction() as conn:
        current = state(db)
        entry = {"at": _now(), "event": "paused", "reason": reason[:500], "by": by, "kind": kind}
        history = [*current["history"], entry][-MAX_HISTORY:]
        if not current["paused"]:
            current.update(paused=True, reason=reason[:500], at=entry["at"], by=by, kind=kind)
        current["history"] = history
        db.write_meta(STATE, current, conn)
        return current


def resume(db, *, note: str, by: str, result_usd: float | None = None) -> dict[str, Any]:
    """Allow opening again. Only ever a person's act; the history keeps what was cleared.

    Resuming from the loss line moves where the line is measured from to ``result_usd``, what
    automatic trading has made or lost as of now. It has to: the loss that reached the line is
    still there, and a line still measured from the old place would pause the plan again on the
    next pass, which makes resuming impossible. So a person who resumes is allowing a further
    loss of the same size from here, and whatever shows them the button has to say so. Resuming
    from any other pause moves nothing -- pausing and resuming by hand must not widen the line.
    """
    with db.transaction() as conn:
        current = state(db)
        entry = {"at": _now(), "event": "resumed", "note": note[:500], "by": by,
                 "cleared": current.get("reason"), "kind": current.get("kind")}
        if current.get("kind") == LOSS_LINE and result_usd is not None:
            entry["loss_baseline_usd"] = {"from": current.get("loss_baseline_usd", 0.0),
                                          "to": round(float(result_usd), 2)}
            current["loss_baseline_usd"] = round(float(result_usd), 2)
        current.update(paused=False, reason=None, at=None, by=None, kind=None)
        current["history"] = [*current["history"], entry][-MAX_HISTORY:]
        db.write_meta(STATE, current, conn)
        return current
