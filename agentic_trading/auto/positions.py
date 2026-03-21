"""Which shares code itself bought, under which strategy, and when that strategy says to leave.

A position code opens has to be one code can close, or it is a position nobody is watching:
that was the state of things before this module, where a holding opened without a person had
no baseline a reduction could be measured from and so could never be reduced at all. The
authority given here is deliberately the narrowest that fixes it.

**Code may sell what code bought, and nothing else.** The quantity is replayed from the fills,
oldest first, exactly as the floor under a person's purchases is (``autonomy.position_floor``),
and for the same reason: a derivation cannot be lost by a restart or counted twice by a second
process. A purchase a strategy version approved without a person adds to the managed quantity;
a purchase a person approved does not, even into the same holding. So a person who adds to a
position code opened keeps their shares under their own authority, and a holding nobody
attributed to a strategy -- anything from before this existed -- is not managed at all.

**An answer that cannot be established is absent, never zero and never "all of it".** A log too
long to replay, shares the fills cannot explain, a book that could not be read: each leaves the
managed quantity unknown, and unknown authorises nothing.

**The exit conditions are the opening version's, not today's.** A position is bound to the
strategy version that opened it and is held to that version's stop, its target and its longest
holding period until it is gone, whatever plan is in force by then.

Pure functions over records already read. Nothing here places, approves or writes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from .. import autonomy

STOP_LOSS, TAKE_PROFIT, MAX_HOLDING, TREND_BREAK = "stop_loss", "take_profit", "max_holding", "trend_break"
#: How close to a stop or a target, in percentage points, is at it.
AT_THE_LINE = 1e-9


def trend(closes: list[float] | None, window: int) -> dict[str, Any] | None:
    """Where a fund's last finished close stands against its own average over ``window`` sessions.

    None whenever that cannot be said: no rule to apply, or fewer closes than the average needs.
    A caller treats None as "not known", which neither confirms a trend nor breaks one.
    """
    if window <= 0 or not closes or len(closes) < window:
        return None
    average = sum(closes[-window:]) / window
    return {"close": float(closes[-1]), "average": average, "window": window,
            "above": closes[-1] > average, "below": closes[-1] < average}
#: Why a position is being closed, in a sentence each language the console speaks.
REASONS: dict[str, tuple[str, str]] = {
    STOP_LOSS: ("跌幅 {pnl_pct}% 触及止损线 -{limit_pct}%。",
                "It is down {pnl_pct}% against a stop of -{limit_pct}%."),
    TAKE_PROFIT: ("涨幅 {pnl_pct}% 达到止盈线 +{limit_pct}%。",
                  "It is up {pnl_pct}% against a target of +{limit_pct}%."),
    MAX_HOLDING: ("已持有 {held_days} 天，达到最长持有期 {limit_days} 天。",
                  "It has been held {held_days} days, the longest this strategy holds is {limit_days}."),
    TREND_BREAK: ("收盘价 {close} 跌破自己的 {window} 日均线 {average}。",
                  "Its close of {close} is below its own {window}-session average of {average}."),
}


@dataclass(frozen=True)
class Managed:
    """What code holds of one symbol under a strategy version, and how that was derived."""

    symbol: str
    bucket: str
    #: Shares code itself bought and has not sold. None whenever that could not be established.
    quantity: float | None
    #: The strategy version whose approval opened this life of the position.
    version_id: str | None = None
    opened_at: str | None = None
    #: Average cost of the managed shares, from the fills that bought them.
    cost_basis: float | None = None
    #: Shares in the same holding that are not code's: a person's, or unattributed.
    other_quantity: float = 0.0
    reason: str | None = None

    @property
    def known(self) -> bool:
        return self.quantity is not None

    def as_dict(self) -> dict[str, Any]:
        return {"symbol": self.symbol, "bucket": self.bucket, "managed_quantity": self.quantity,
                "strategy_version": self.version_id, "opened_at": self.opened_at,
                "cost_basis": self.cost_basis, "other_quantity": self.other_quantity,
                "reason": self.reason}


def _is_managed_order(proposal: Any) -> bool:
    """Whether a strategy version, and no person, stood behind this order."""
    return (proposal is not None and autonomy.was_autonomous(proposal)
            and bool(getattr(proposal, "strategy_version", None)))


@dataclass
class _Played:
    """Where one symbol's fills leave it, and every life of code's own that they closed."""

    own: float = 0.0
    other: float = 0.0
    cost: float = 0.0
    version: str | None = None
    opened_at: str | None = None
    error: str | None = None
    lives: list[dict[str, Any]] | None = None


def _replay(fills: list[dict[str, Any]], proposals: Any, bucket: str, symbol: str) -> _Played:
    """Play one symbol's fills forward, oldest first. The one arithmetic behind every answer here."""
    tolerance = autonomy.QUANTITY_TOLERANCE
    by_proposal = {p.proposal_id: p for p in proposals}
    rows = [row for row in sorted(fills, key=lambda r: str(r.get("at") or ""))
            if str(row.get("bucket")) == bucket and str(row.get("symbol") or "").upper() == symbol]
    state = _Played(lives=[])
    life: dict[str, Any] = {}
    for row in rows:
        side = str(row.get("side") or "").lower()
        quantity = float(row.get("quantity") or 0.0)
        price = float(row.get("price") or 0.0)
        at = str(row.get("at") or "") or None
        order = by_proposal.get(str(row.get("proposal_id")))
        automatic = _is_managed_order(order)
        if side == "buy":
            if automatic:
                if state.own <= tolerance:
                    state.version, state.opened_at, state.cost = order.strategy_version, at, 0.0
                    life = {"symbol": symbol, "bucket": bucket, "version": state.version,
                            "opened_at": at, "bought": 0.0, "spent_usd": 0.0, "sold": 0.0,
                            "proceeds_usd": 0.0, "orders": 0,
                            # The proposal that opened it, and how sure its research said it was.
                            "opened_by": getattr(order, "proposal_id", None),
                            "confidence": getattr(order, "confidence", None)}
                state.own += quantity
                state.cost += quantity * price
                life["bought"] += quantity
                life["spent_usd"] += quantity * price
                life["orders"] += 1
            else:
                state.other += quantity
        elif side == "sell":
            from_own = min(state.own, quantity) if automatic else max(0.0, quantity - state.other)
            from_own = min(state.own, from_own)
            had = state.own
            if state.own > tolerance:
                state.cost *= max(0.0, state.own - from_own) / state.own
            state.own -= from_own
            state.other = max(0.0, state.other - (quantity - from_own))
            if life and from_own > 0:
                life["sold"] += from_own
                life["proceeds_usd"] += from_own * price
                life["orders"] += 1
            if life and had > tolerance and state.own <= tolerance:
                spent, proceeds = life["spent_usd"], life["proceeds_usd"]
                state.lives.append({
                    **life, "closed_at": at, "closed_by": "code" if automatic else "person",
                    "bought": round(life["bought"], 8), "sold": round(life["sold"], 8),
                    "spent_usd": round(spent, 2), "proceeds_usd": round(proceeds, 2),
                    "realized_usd": round(proceeds - spent, 2),
                    "return_pct": round((proceeds / spent - 1.0) * 100.0, 4) if spent > 0 else None})
                life = {}
        else:
            state.error = (f"a fill records an unrecognised side {side!r}; the position cannot "
                           "be replayed")
            return state
        if state.own + state.other <= tolerance:
            state.own = state.other = state.cost = 0.0
            state.version = state.opened_at = None
        elif state.own <= tolerance:
            # Code's own shares are gone though the holding lives on: that life is over, and a
            # later automatic purchase begins a new one under whichever version makes it.
            state.own, state.cost, state.version, state.opened_at = 0.0, 0.0, None, None
    return state


def closed_trades(*, fills: list[dict[str, Any]], proposals: Any, bucket: str) -> list[dict[str, Any]]:
    """Every position code opened and has since left, with what it cost and what it brought back.

    One row per life of a position: from the first purchase a strategy version made to the sale
    that took code's last share. Each says who closed it, because a position a person sold out
    of is not evidence that code's own exit works. Losing trades are rows like any other --
    nothing here selects.
    """
    if len(fills) >= autonomy.FILL_REPLAY_LIMIT:
        return []
    symbols = sorted({str(row.get("symbol") or "").upper() for row in fills
                      if str(row.get("bucket")) == bucket and row.get("symbol")})
    out: list[dict[str, Any]] = []
    for symbol in symbols:
        out.extend(_replay(fills, proposals, bucket, symbol).lives or [])
    return sorted(out, key=lambda row: str(row.get("closed_at") or ""))


def managed(*, fills: list[dict[str, Any]], proposals: Any, bucket: str, symbol: str,
            ledger_quantity: float | None) -> Managed:
    """The shares of one holding that code may close, replayed from the fills that built it.

    A sale code made comes out of code's own shares. A sale a person made comes out of the
    person's first and only then out of code's, so a person selling down a mixed holding never
    leaves code believing it still owns shares that are gone.
    """
    symbol = str(symbol).upper()
    tolerance = autonomy.QUANTITY_TOLERANCE
    if len(fills) >= autonomy.FILL_REPLAY_LIMIT:
        return Managed(symbol, bucket, None, reason=(
            f"more than {autonomy.FILL_REPLAY_LIMIT} fills on record; the log cannot be replayed "
            "from the position's first purchase"))
    played = _replay(fills, proposals, bucket, symbol)
    if played.error:
        return Managed(symbol, bucket, None, reason=played.error)
    own, other, cost = played.own, played.other, played.cost
    version, opened_at = played.version, played.opened_at
    if ledger_quantity is None:
        return Managed(symbol, bucket, None, version, opened_at, other_quantity=other, reason=(
            "the book could not be read, so the replay cannot be checked against it"))
    if abs((own + other) - float(ledger_quantity)) > tolerance:
        return Managed(symbol, bucket, None, version, opened_at, other_quantity=other, reason=(
            f"the fills account for {own + other:g} shares and the book holds "
            f"{float(ledger_quantity):g}; there are shares the log cannot explain, so it cannot "
            "say which of them code bought"))
    return Managed(symbol, bucket, round(own, 8), version, opened_at,
                   cost_basis=round(cost / own, 6) if own > tolerance else None,
                   other_quantity=round(other, 8))


def book(*, fills: list[dict[str, Any]], proposals: Any, ledger: Any, bucket: str) -> list[Managed]:
    """Every holding in the bucket that code has shares in, or that it cannot account for."""
    out = []
    for position in ledger.positions:
        if position.bucket != bucket:
            continue
        view = managed(fills=fills, proposals=proposals, bucket=bucket, symbol=position.symbol,
                       ledger_quantity=float(position.quantity))
        if not view.known or (view.quantity or 0.0) > autonomy.QUANTITY_TOLERANCE:
            out.append(view)
    return out


@dataclass(frozen=True)
class ExitCall:
    """Whether a managed position should be closed now, and on which of its own conditions."""

    due: bool
    reason: str | None = None
    facts: dict[str, Any] | None = None
    #: The last day this position may be held, from the version that opened it.
    exit_by: str | None = None
    #: Why nothing could be decided, when nothing could.
    undecided: str | None = None

    def text(self) -> dict[str, str] | None:
        if not self.due or self.reason not in REASONS:
            return None
        zh, en = REASONS[self.reason]
        return {"zh": zh.format(**(self.facts or {})), "en": en.format(**(self.facts or {}))}

    def as_dict(self) -> dict[str, Any]:
        return {"due": self.due, "reason": self.reason, "facts": self.facts,
                "exit_by": self.exit_by, "undecided": self.undecided, "text": self.text()}


def _day(stamp: str | None) -> date | None:
    try:
        return datetime.fromisoformat(str(stamp)).date() if stamp else None
    except ValueError:
        return None


def exit_call(position: Managed, version: dict[str, Any] | None, *, price: float | None,
              today: date, closes: list[float] | None = None) -> ExitCall:
    """Whether this position has met one of its opening version's exit conditions.

    Nothing is inferred. Without the version there are no conditions to meet; without a cost or
    a price there is no loss or gain to measure. Each of those is "undecided", which closes
    nothing: a position is only ever sold on a condition that was actually read as met.

    ``closes`` is the fund's finished daily closes, oldest first, for a version with a trend
    rule. Absent or too short, the trend is not known and is not a reason to sell.
    """
    if not position.known or (position.quantity or 0.0) <= autonomy.QUANTITY_TOLERANCE:
        return ExitCall(False, undecided=position.reason or "code holds none of this position")
    if version is None:
        return ExitCall(False, undecided=(
            f"the strategy version {position.version_id or '(none)'} that opened this position is "
            "not on record, so it has no exit conditions to apply"))
    rules = version.get("strategy") or {}
    opened = _day(position.opened_at)
    longest = int(rules.get("max_holding_days") or 0)
    exit_by = (opened + timedelta(days=longest)).isoformat() if opened and longest > 0 else None
    if opened and longest > 0 and (today - opened).days >= longest:
        return ExitCall(True, MAX_HOLDING, {"held_days": (today - opened).days, "limit_days": longest},
                        exit_by)
    measurable = not (price is None or not position.cost_basis or position.cost_basis <= 0 or price <= 0)
    if measurable:
        pnl = (price / position.cost_basis - 1.0) * 100.0
        stop = float(rules.get("stop_loss_pct") or 0.0)
        target = float(rules.get("take_profit_pct") or 0.0)
        shown = f"{abs(pnl):.1f}"
        # At the line counts as reaching it. The slack is for the arithmetic, not for the market:
        # 90 against a cost of 100 computes to -9.999999999999998, and a stop of 10 must still fire.
        if stop > 0 and pnl <= -stop + AT_THE_LINE:
            return ExitCall(True, STOP_LOSS, {"pnl_pct": shown, "limit_pct": f"{stop:g}"}, exit_by)
        if target > 0 and pnl >= target - AT_THE_LINE:
            return ExitCall(True, TAKE_PROFIT, {"pnl_pct": shown, "limit_pct": f"{target:g}"}, exit_by)
    # The version's trend rule, on finished closes. It needs no cost, so it is read even when
    # a gain or a loss could not be.
    seen = trend(closes, int(rules.get("trend_window") or 0))
    if seen is not None and seen["below"]:
        return ExitCall(True, TREND_BREAK, {"close": f"{seen['close']:.2f}",
                                            "average": f"{seen['average']:.2f}",
                                            "window": seen["window"]}, exit_by)
    if not measurable:
        return ExitCall(False, exit_by=exit_by, undecided=(
            "no current price or no cost on record, so a loss or a gain cannot be measured"))
    return ExitCall(False, exit_by=exit_by)
