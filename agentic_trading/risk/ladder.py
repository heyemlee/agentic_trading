"""Progressive drawdown ladder with persistence, hysteresis and sticky halt levels.

Three ladders run side by side: the total portfolio, the core bucket alone and
the sandbox alone. Each keeps its own high-water mark. Escalation is immediate;
de-escalation needs the drawdown to recover by ``hysteresis_pct_points`` past a
level's trigger, and HARD_HALT / EMERGENCY only clear after a manual reset.
Nothing here sells anything: the ladder produces a level and a list of
required or advisory actions that the Hard Risk Layer and a human act on.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..profile import Profiles
from ..state_db import StateDB

ORDER = ["NORMAL", "WARNING", "RISK_REDUCTION", "HARD_HALT", "EMERGENCY"]
SANDBOX_ORDER = ["NORMAL", "WARNING", "HALT"]


@dataclass
class LadderState:
    high_water_mark_usd: float
    level: str = "NORMAL"
    drawdown_pct: float = 0.0
    manual_reset_required: bool = False
    updated: str | None = None
    history: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class LadderStates:
    total: LadderState
    core: LadderState
    sandbox: LadderState
    micro_live: LadderState | None = None

    def as_dict(self) -> dict[str, Any]:
        pairs = [("total", self.total), ("core", self.core), ("sandbox", self.sandbox)]
        if self.micro_live is not None:
            pairs.append(("micro_live", self.micro_live))
        return {k: asdict(v) for k, v in pairs}

    def for_bucket(self, bucket: str) -> LadderState:
        """The ladder that governs a bucket. Micro-Live is a separate portfolio with its own
        high-water mark, so it is never judged against the conceptual portfolio's drawdown."""
        if bucket == "micro_live" and self.micro_live is not None:
            return self.micro_live
        return {"core": self.core, "sandbox": self.sandbox}.get(bucket, self.sandbox)

    def governing(self, bucket: str) -> LadderState:
        """The ladder a proposal is recorded against when it is made and compared with when it
        is approved. Core is held to the total portfolio's; every other bucket to its own.

        One definition for both ends. Proposals used to record the total ladder whatever their
        bucket while revalidation compared the bucket's own, so a Micro-Live proposal carried
        the conceptual book's level into a comparison with the real account's.
        """
        return self.total if bucket == "core" else self.for_bucket(bucket)


def escalated(bucket: str, before: str | None, after: str) -> bool:
    """Whether the governing ladder is at a worse level now than it was.

    Sandbox and Micro-Live step through their own shorter ladder, so their levels are ranked on
    it. Ranked on the total ladder's, HALT was an unknown word and a climb to it went unnoticed.
    """
    order = ORDER if bucket == "core" else SANDBOX_ORDER
    return before in order and after in order and order.index(after) > order.index(before)


def load_ladder_states(path: str | Path, profiles: Profiles) -> LadderStates:
    path = Path(path)
    total0 = float(profiles.risk_cfg("drawdown_ladder", "measurement", "high_water_mark_start_usd",
                                     default=(profiles.investor.get("capital") or {}).get("total_usd", 0)))
    core0 = profiles.capital_allocation("core")
    sandbox0 = profiles.capital_allocation("sandbox")
    micro0 = profiles.capital_allocation("micro_live")
    stored = StateDB(path).read_document(path)
    if stored is not None:
        raw = stored
    elif path.exists():
        raw = json.loads(path.read_text(encoding="utf-8"))
    else:
        return LadderStates(LadderState(total0), LadderState(core0), LadderState(sandbox0),
                            LadderState(micro0) if micro0 else None)

    def mk(key: str, default_hwm: float) -> LadderState:
        d = dict(raw.get(key) or {})
        d.setdefault("high_water_mark_usd", default_hwm)
        return LadderState(**{k: d[k] for k in LadderState.__dataclass_fields__ if k in d})

    micro = mk("micro_live", micro0) if ("micro_live" in raw or micro0) else None
    return LadderStates(mk("total", total0), mk("core", core0), mk("sandbox", sandbox0), micro)


def save_ladder_states(path: str | Path, states: LadderStates) -> None:
    path = Path(path)
    db = StateDB(path)
    with db.transaction() as conn:
        raw = path.read_bytes() if path.exists() else b""
        db.write_document(path, states.as_dict(), conn)
        if not db.migrated(path):
            db.mark_migrated(conn, path, "drawdown_ladder", raw)


def _thresholds(profiles: Profiles, section: str) -> list[tuple[str, float]]:
    levels = profiles.risk_cfg("drawdown_ladder", section, "levels", default=[]) or []
    out: list[tuple[str, float]] = []
    for i, lvl in enumerate(levels):
        trig = profiles.effective(lvl.get("trigger_pct"), f"risk_profile.drawdown_ladder.{section}.levels[{i}].trigger_pct")
        out.append((str(lvl["name"]), float(trig)))
    return out


def _step(state: LadderState, equity: float, thresholds: list[tuple[str, float]], order: list[str],
          hysteresis: float, manual_levels: set[str], as_of: str) -> LadderState:
    state.high_water_mark_usd = max(state.high_water_mark_usd, equity)
    hwm = state.high_water_mark_usd
    dd = (equity / hwm - 1.0) * 100.0 if hwm > 0 else 0.0
    state.drawdown_pct = dd
    trig = dict(thresholds)
    # deepest level whose trigger has been crossed
    reached = "NORMAL"
    for name in order:
        t = trig.get(name)
        if t is not None and t < 0 and dd <= t + 1e-9:  # tolerance: 2790/3000 is -6.999...%
            reached = name
    current = state.level if state.level in order else "NORMAL"
    new = current
    if order.index(reached) > order.index(current):
        new = reached
    elif order.index(reached) < order.index(current):
        if current in manual_levels or state.manual_reset_required:
            new = current  # sticky until a human resets
        else:
            t_cur = trig.get(current, 0.0)
            if dd > t_cur + hysteresis + 1e-9:
                # step down one level at a time; never skip past a still-triggered level
                new = order[max(order.index(current) - 1, order.index(reached))]
    if new != state.level:
        state.history.append({"date": as_of, "from": state.level, "to": new, "drawdown_pct": round(dd, 3)})
        state.level = new
        if new in manual_levels:
            state.manual_reset_required = True
    state.updated = as_of
    return state


def evaluate_ladders(states: LadderStates, profiles: Profiles, as_of: str,
                     total_equity: float | None, core_equity: float | None,
                     sandbox_equity: float | None,
                     micro_live_equity: float | None = None) -> LadderStates:
    """Step each ladder against its own portfolio's equity.

    None means the snapshot did not contain that portfolio, so its ladder is left exactly
    as it was. A Micro-Live run is handed a snapshot with one bucket in it; standing a zero
    in for the conceptual buckets it was never shown reads as a total loss, and the ladder
    latches a halt that needs a human to clear. That happened: a $100 account judged against
    the paper book's $3,000 high-water mark recorded -96.7% and went to EMERGENCY, with
    Sandbox at -100% and HALT, on portfolios that had never held a position. The guard the
    Micro-Live ladder already had is simply the same one, applied to all four.
    """
    hyst = float(profiles.risk_cfg("drawdown_ladder", "de_escalation", "hysteresis_pct_points", default=2))
    manual = set(profiles.risk_cfg("drawdown_ladder", "de_escalation", "manual_reset_levels", default=[]) or [])
    total_thr = _thresholds(profiles, "total_portfolio")
    if total_equity is not None:
        _step(states.total, total_equity, total_thr, ORDER, hyst, manual, as_of)
    if core_equity is not None and profiles.risk_cfg("drawdown_ladder", "core", "mirror_total_portfolio_levels", default=True):
        _step(states.core, core_equity, total_thr, ORDER, hyst, manual, as_of)
    if sandbox_equity is not None:
        sandbox_thr = _thresholds(profiles, "sandbox")
        _step(states.sandbox, sandbox_equity, sandbox_thr, SANDBOX_ORDER, hyst, {"HALT"}, as_of)
    if states.micro_live is not None and micro_live_equity is not None:
        _step(states.micro_live, micro_live_equity, _thresholds(profiles, "micro_live"),
              SANDBOX_ORDER, hyst, {"HALT"}, as_of)
    return states


def effective_core_level(states: LadderStates) -> str:
    """Core opens are governed by the worse of the total and core ladders."""
    return max(states.total.level, states.core.level, key=ORDER.index)
