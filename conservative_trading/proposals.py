"""Proposal lifecycle: the record that survives between analysis, approval and execution.

A proposal is a dated opinion, not a standing instruction. It carries the state of the world
that produced it (price, portfolio, ladder, regime) so execution can ask the only question
that matters: is that world still close enough to today's world to act on the idea.

Freshness is per strategy class, because a core allocation thesis and an event-driven thesis
decay at completely different speeds. Thresholds live in config/risk_profile.yaml.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .profile import Profiles

# Lifecycle of the proposal itself
PROPOSED = "PROPOSED"
APPROVED = "APPROVED"
EXECUTED = "EXECUTED"
REJECTED = "REJECTED"
EXPIRED = "EXPIRED"
INVALIDATED = "INVALIDATED"
NEEDS_REANALYSIS = "NEEDS_REANALYSIS"
NO_ACTION = "NO_ACTION"

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
    history: list[dict[str, Any]] = field(default_factory=list)

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
        return asdict(self)


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
    """Small JSON-file store. A database would be premature for a handful of proposals a week."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def _load_raw(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"proposals": []}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def all(self) -> list[Proposal]:
        return [Proposal(**{k: v for k, v in d.items() if k in Proposal.__dataclass_fields__})
                for d in self._load_raw().get("proposals", [])]

    def get(self, proposal_id: str) -> Proposal | None:
        matches = [p for p in self.all() if p.proposal_id == proposal_id or p.proposal_id.endswith(proposal_id)]
        if len(matches) > 1:
            raise ValueError(f"{proposal_id!r} matches {len(matches)} proposals; use the full id")
        return matches[0] if matches else None

    def save(self, proposal: Proposal) -> Proposal:
        data = self._load_raw()
        items = [d for d in data.get("proposals", []) if d.get("proposal_id") != proposal.proposal_id]
        items.append(proposal.as_dict())
        data["proposals"] = items[-200:]      # keep the file small; records live in runs/ anyway
        data["updated_at"] = _iso(utcnow())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
        tmp.replace(self.path)
        return proposal

    def open_proposals(self, profiles: Profiles) -> list[Proposal]:
        out = []
        for p in self.all():
            if p.status in (PROPOSED, APPROVED):
                state, _ = p.freshness(profiles)
                if state != EXPIRED:
                    out.append(p)
        return out


def positions_fingerprint(symbols_and_qty: dict[str, float]) -> str:
    """Stable fingerprint of the position book, to detect that it changed under a proposal."""
    parts = [f"{s.upper()}:{round(q, 6)}" for s, q in sorted(symbols_and_qty.items())]
    return "|".join(parts) if parts else "(empty)"


def client_order_id(profiles: Profiles, proposal: Proposal, attempt: int) -> str:
    prefix = str((profiles.risk.get("idempotency") or {}).get("client_order_id_prefix", "ct"))
    return f"{prefix}-{proposal.proposal_id}-{attempt}"[:128]


def next_business_datetime(dt: datetime, minutes: int) -> datetime:
    return dt + timedelta(minutes=minutes)
