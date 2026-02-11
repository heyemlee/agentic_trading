"""Proposal lifecycle: the record that survives between analysis, approval and execution.

A proposal is a dated opinion, not a standing instruction. It carries the state of the world
that produced it (price, portfolio, ladder, regime) so execution can ask the only question
that matters: is that world still close enough to today's world to act on the idea.

Freshness is per strategy class, because a core allocation thesis and an event-driven thesis
decay at completely different speeds. Thresholds live in config/risk_profile.yaml.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from . import events
from .profile import Profiles
from .state_db import StateConflict, StateDB
from .storage import file_lock

# Lifecycle of the proposal itself
PROPOSED = "PROPOSED"
APPROVED = "APPROVED"
EXECUTED = "EXECUTED"
REJECTED = "REJECTED"
EXPIRED = "EXPIRED"
INVALIDATED = "INVALIDATED"
NEEDS_REANALYSIS = "NEEDS_REANALYSIS"
NO_ACTION = "NO_ACTION"
SUBMITTING = "SUBMITTING"
SUBMITTED = "SUBMITTED"
PARTIALLY_FILLED = "PARTIALLY_FILLED"
CANCELED = "CANCELED"
UNKNOWN = "UNKNOWN"
PENDING_EXECUTION = (SUBMITTING, SUBMITTED, PARTIALLY_FILLED, UNKNOWN)

# Freshness of the world behind it
VALID = "VALID"
STALE = "STALE"

# Strategy tag -> freshness class. Anything unmapped falls back to the profile's default class.
STRATEGY_FRESHNESS_CLASS = {
    "long_only_diversified": "core_conservative",
    "buy_and_hold": "core_conservative",
    "rebalance": "core_conservative",
    "de_risk": "core_conservative",
    "momentum": "momentum",
    "momentum_trading": "momentum",
    "trend_following": "momentum",
    "short": "short",
    "event_driven": "event_driven",
}


def utcnow() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


@dataclass
class Proposal:
    """One actionable idea, with the world it was born into."""

    proposal_id: str
    run_id: str | None
    ticker: str
    bucket: str
    action: str                     # BUY | ADD | REDUCE | SELL
    strategy: str
    instrument_type: str
    limit_class: str
    size_hint: str
    confidence: int | None
    notional_usd: float
    target_weight_pct: float
    trade_date: str
    created_at: str                 # when the analysis produced it
    status: str = PROPOSED
    # world snapshot at creation
    reference_price: float | None = None
    reference_price_date: str | None = None
    ladder_level_at_proposal: str | None = None
    regime_at_proposal: str | None = None
    portfolio_equity_at_proposal: float | None = None
    bucket_cash_at_proposal: float | None = None
    positions_fingerprint: str | None = None
    data_quality: str | None = None
    missing_information: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)
    # approval and execution
    requires_human_approval: bool = True
    approved_at: str | None = None
    approved_by: str | None = None
    approval_note: str | None = None
    executed_at: str | None = None
    client_order_id: str | None = None
    broker_order_id: str | None = None
    executed_notional_usd: float | None = None
    submit_attempts: int = 0
    sell_quantity: float | None = None
    order_status: str | None = None
    order_request: dict[str, Any] | None = None
    filled_quantity: float = 0.0
    filled_avg_price: float | None = None
    reconciled_at: str | None = None
    reconciliation_error: str | None = None
    execution_ledger_path: str | None = None
    approval_request_id: str | None = None
    approved_version: str | None = None
    approval_response: dict[str, Any] | None = None
    history: list[dict[str, Any]] = field(default_factory=list)
    _revision: int = field(default=0, repr=False, compare=False)

    # -- freshness -----------------------------------------------------------
    def freshness_class(self, profiles: Profiles) -> str:
        cfg = profiles.risk.get("proposal_freshness") or {}
        default = str(cfg.get("default_class", "core_conservative"))
        name = STRATEGY_FRESHNESS_CLASS.get((self.strategy or "").lower(), default)
        return name if name in (cfg.get("classes") or {}) else default

    def freshness_rules(self, profiles: Profiles) -> dict[str, Any]:
        cfg = profiles.risk.get("proposal_freshness") or {}
        return dict((cfg.get("classes") or {}).get(self.freshness_class(profiles)) or {})

    def age_minutes(self, now: datetime | None = None) -> float:
        created = _parse(self.created_at) or utcnow()
        return max(0.0, ((now or utcnow()) - created).total_seconds() / 60.0)

    def freshness(self, profiles: Profiles, now: datetime | None = None) -> tuple[str, str]:
        """(VALID | STALE | EXPIRED, human-readable reason)."""
        if self.status in (INVALIDATED, REJECTED, EXECUTED, NO_ACTION):
            return self.status, f"proposal status is {self.status}"
        rules = self.freshness_rules(profiles)
        age = self.age_minutes(now)
        ttl = float(rules.get("ttl_minutes", 2880))
        stale_after = float(rules.get("stale_after_minutes", ttl / 2))
        cls = self.freshness_class(profiles)
        if age > ttl:
            return EXPIRED, f"{cls}: {age:.0f} min old, past its {ttl:.0f} min lifetime"
        if age > stale_after:
            return STALE, f"{cls}: {age:.0f} min old, past the {stale_after:.0f} min mark; revalidation is mandatory"
        return VALID, f"{cls}: {age:.0f} min old, inside the {stale_after:.0f} min window"

    def approval_expired(self, profiles: Profiles, now: datetime | None = None) -> tuple[bool, str]:
        approved = _parse(self.approved_at)
        if approved is None:
            return False, "not approved yet"
        ttl = float((profiles.risk.get("approval") or {}).get("approval_ttl_minutes", 60))
        age = ((now or utcnow()) - approved).total_seconds() / 60.0
        return age > ttl, f"approval is {age:.0f} min old (limit {ttl:.0f} min)"

    def note(self, event: str, detail: str, **extra: Any) -> None:
        self.history.append({"at": _iso(utcnow()), "event": event, "detail": detail, **extra})

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("_revision", None)
        return data

    def version(self) -> str:
        """Hash every part of the idea and its lifecycle; polls do not change it."""
        data = self.as_dict()
        data.pop("approval_response", None)
        return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":"),
                                         allow_nan=False).encode()).hexdigest()


def new_proposal(*, run_id: str | None, ticker: str, bucket: str, action: str, strategy: str,
                 instrument_type: str, limit_class: str, size_hint: str, confidence: int | None,
                 notional_usd: float, target_weight_pct: float, trade_date: str,
                 requires_human_approval: bool = True, **world: Any) -> Proposal:
    p = Proposal(
        proposal_id=f"{trade_date}-{ticker.upper().replace('/', '-')}-{uuid.uuid4().hex[:8]}",
        run_id=run_id, ticker=ticker.upper(), bucket=bucket, action=action, strategy=strategy,
        instrument_type=instrument_type, limit_class=limit_class, size_hint=size_hint,
        confidence=confidence, notional_usd=notional_usd, target_weight_pct=target_weight_pct,
        trade_date=trade_date, created_at=_iso(utcnow()), requires_human_approval=requires_human_approval,
        **{k: v for k, v in world.items() if k in Proposal.__dataclass_fields__},
    )
    p.note("created", f"{action} {p.ticker} in {bucket} for ${notional_usd:,.2f} ({strategy})")
    return p


class ProposalStore:
    """SQLite proposal store with optimistic revisions and a process transition lock."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.db = StateDB(self.path)

    def locked(self):
        return file_lock(self.path)

    def _load_raw(self) -> dict[str, Any]:
        conn = self.db.current() or self.db._read()
        owned = self.db.current() is None
        if conn is not None:
            try:
                # A read-only connection cannot adopt pre-move absolute keys, so the legacy
                # key is tried only when the current one holds nothing.
                rows = conn.execute("SELECT payload,revision FROM proposals WHERE namespace=? ORDER BY updated_at",
                                    (self.db.source_key(self.path),)).fetchall() or conn.execute(
                    "SELECT payload,revision FROM proposals WHERE namespace=? ORDER BY updated_at",
                    (str(self.path.resolve()),)).fetchall()
                if rows or self.db.migrated(self.path):
                    values = []
                    for row in rows:
                        value = json.loads(row[0])
                        value["_revision"] = row[1]
                        values.append(value)
                    return {"proposals": values}
            except (sqlite3.DatabaseError, json.JSONDecodeError) as exc:
                # A damaged canonical database must never be hidden by stale legacy JSON.
                # The only safe fallback is a database that has never had our schema.
                if not isinstance(exc, sqlite3.OperationalError) or "no such table" not in str(exc):
                    raise StateConflict(f"proposal database is unavailable or corrupt: {exc}") from exc
            finally:
                if owned:
                    conn.close()
        if not self.path.exists():
            return {"proposals": []}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StateConflict(f"legacy proposal state is unavailable or corrupt: {exc}") from exc

    def all(self) -> list[Proposal]:
        return [Proposal(**{k: v for k, v in d.items() if k in Proposal.__dataclass_fields__})
                for d in self._load_raw().get("proposals", [])]

    def get(self, proposal_id: str) -> Proposal | None:
        matches = [p for p in self.all() if p.proposal_id == proposal_id or p.proposal_id.endswith(proposal_id)]
        if len(matches) > 1:
            raise ValueError(f"{proposal_id!r} matches {len(matches)} proposals; use the full id")
        return matches[0] if matches else None

    def save(self, proposal: Proposal) -> Proposal:
        with self.db.transaction() as conn:
            self._import_legacy(conn)
            namespace = self.db.source_key(self.path)
            row = conn.execute("SELECT revision FROM proposals WHERE namespace=? AND proposal_id=?",
                               (namespace, proposal.proposal_id)).fetchone()
            if row and row[0] != proposal._revision:
                raise StateConflict("proposal changed concurrently; reload it before writing")
            revision = (row[0] if row else 0) + 1
            payload = proposal.as_dict(); payload.pop("_revision", None)
            now = _iso(utcnow())
            try:
                conn.execute("INSERT INTO proposals VALUES(?,?,?,?,?,?) ON CONFLICT(namespace,proposal_id) DO UPDATE SET "
                             "payload=excluded.payload,revision=excluded.revision,approval_request_id=excluded.approval_request_id,updated_at=excluded.updated_at",
                             (namespace, proposal.proposal_id, json.dumps(payload, allow_nan=False), revision,
                              proposal.approval_request_id, now))
            except sqlite3.IntegrityError as exc:
                raise StateConflict("approval request id already exists") from exc
            proposal._revision = revision
            if proposal.approval_request_id:
                approval = (proposal.approval_request_id, proposal.proposal_id, proposal.approved_by or "unknown",
                            proposal.approved_version or "legacy", json.dumps({
                                "ticker": proposal.ticker, "bucket": proposal.bucket, "action": proposal.action,
                                "notional_usd": proposal.notional_usd, "sell_quantity": proposal.sell_quantity,
                            }, allow_nan=False), proposal.approved_at or now)
                existing_approval = conn.execute("SELECT * FROM approvals WHERE request_id=?", (approval[0],)).fetchone()
                if existing_approval and tuple(existing_approval) != approval:
                    raise StateConflict("an immutable approval record cannot be changed")
                conn.execute("INSERT OR IGNORE INTO approvals VALUES(?,?,?,?,?,?)", approval)
            # The journal is appended inside this transaction, so it can never claim a
            # transition the proposal record does not also show.
            events.from_history(conn, proposal)
            if proposal.client_order_id:
                account = "micro_live" if proposal.bucket == "micro_live" else "conceptual"
                conn.execute("INSERT INTO orders VALUES(?,?,?,?,?,?) ON CONFLICT(client_order_id) DO UPDATE SET "
                             "status=excluded.status,payload=excluded.payload,updated_at=excluded.updated_at",
                             (proposal.client_order_id, proposal.proposal_id, account,
                              proposal.order_status or proposal.status, json.dumps(payload, allow_nan=False), now))
                if proposal.status not in PENDING_EXECUTION:
                    conn.execute("UPDATE reservations SET state='RELEASED',updated_at=? WHERE client_order_id=?",
                                 (now, proposal.client_order_id))
        return proposal

    def migrate(self) -> None:
        """Import the legacy JSON once without changing any proposal revision."""
        with self.db.transaction() as conn:
            self._import_legacy(conn)

    def _import_legacy(self, conn):
        if conn.execute("SELECT 1 FROM migrations WHERE source IN (?,?)",
                        self.db.source_keys(self.path)).fetchone():
            return
        raw = self.path.read_bytes() if self.path.exists() else b""
        data = json.loads(raw) if raw else {"proposals": []}
        namespace = self.db.source_key(self.path)
        for item in data.get("proposals", []):
            pid = item.get("proposal_id")
            if pid:
                conn.execute("INSERT OR IGNORE INTO proposals VALUES(?,?,?,1,?,?)", (
                    namespace, pid, json.dumps(item, allow_nan=False), item.get("approval_request_id"),
                    data.get("updated_at") or _iso(utcnow())))
                if item.get("approval_request_id"):
                    conn.execute("INSERT OR IGNORE INTO approvals VALUES(?,?,?,?,?,?)", (
                        item["approval_request_id"], pid, item.get("approved_by") or "legacy",
                        item.get("approved_version") or "legacy", json.dumps({
                            "ticker": item.get("ticker"), "bucket": item.get("bucket"), "action": item.get("action"),
                            "notional_usd": item.get("notional_usd"), "sell_quantity": item.get("sell_quantity"),
                        }), item.get("approved_at") or _iso(utcnow())))
                if item.get("client_order_id"):
                    account = "micro_live" if item.get("bucket") == "micro_live" else "conceptual"
                    conn.execute("INSERT OR IGNORE INTO orders VALUES(?,?,?,?,?,?)", (
                        item["client_order_id"], pid, account, item.get("order_status") or item.get("status", "UNKNOWN"),
                        json.dumps(item, allow_nan=False), item.get("reconciled_at") or _iso(utcnow())))
        self.db.mark_migrated(conn, self.path, "proposals", raw)

    def open_proposals(self, profiles: Profiles) -> list[Proposal]:
        out = []
        for p in self.all():
            if p.status in (PROPOSED, APPROVED):
                state, _ = p.freshness(profiles)
                if state != EXPIRED:
                    out.append(p)
        return out


def needs_reconciliation(proposal: Proposal) -> bool:
    """Whether this proposal's real fate is known only to the venue.

    Every question of the form "does the broker still own an order?" asks this, and only this:
    execution before it places another order, the release gate, a restore and a ledger
    confirmation. It is phrased in the lifecycle status this system writes, never in the
    venue's own order status, whose vocabulary belongs to the broker and changes with it.
    """
    return bool(proposal.client_order_id and (
        proposal.status in PENDING_EXECUTION or proposal.reconciliation_error
        or (not proposal.reconciled_at and proposal.status in (APPROVED, EXECUTED))))


def positions_fingerprint(symbols_and_qty: dict[str, float]) -> str:
    """Stable fingerprint of the position book, to detect that it changed under a proposal."""
    parts = [f"{s.upper()}:{round(float(q), 6)}" for s, q in sorted(symbols_and_qty.items())]
    return "|".join(parts) if parts else "(empty)"


def client_order_id(profiles: Profiles, proposal: Proposal, attempt: int) -> str:
    prefix = str((profiles.risk.get("idempotency") or {}).get("client_order_id_prefix", "ct"))
    return f"{prefix}-{proposal.proposal_id}-{attempt}"[:128]


def next_business_datetime(dt: datetime, minutes: int) -> datetime:
    return dt + timedelta(minutes=minutes)
