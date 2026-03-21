"""What an intraday mode would have to be able to rely on, checked rather than assumed.

The intraday mode is not built (docs/todo.md, P2.5), and what is here does not build it. It is
the other half of saying so honestly: the specific things that would have to hold before
trading inside a session could be trusted, each as a check that can be run today and answers
with what it found. "Not open" for this mode is then a list of measured reasons, not a label.

Four checks, and they are independent of the daily modes' because they fail differently:

**The session's data is all there.** A rule that acts on minute bars is deciding on whichever
minutes arrived. So: how many of the minutes since the open have a bar, and how long is the
longest stretch with none.

**The newest price is young enough to act on.** By the real account's own rule for a quote
(``data_freshness.fast.max_age_seconds``), not a looser one made up for the occasion. A feed
that lags by a quarter of an hour is fine for valuing a position held for weeks and useless
for one held for minutes.

**The venue is known to allow it.** Trading in and out within a day runs into rules the daily
modes never meet -- how soon the proceeds of a sale can be spent again, how same-day round
trips are counted, which order types exist for a fraction of a share. What is on record about
the venue (``broker.requirements``) either covers those or it does not.

**The account's own pace has room for it.** In and out is two orders a day.

The design this would serve is fixed even though it is not built: a model sets the day's plan
once, before the open, and local rules carry it out. Nothing here, and nothing in a pass of any
mode, asks a model anything per trade.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from typing import Any

from ..broker import requirements
from . import quotes

#: The share of the minutes since the open that must have a bar, and the longest run without
#: one, for a session's data to count as whole.
COMPLETENESS_PCT = 98.0
LONGEST_GAP_MINUTES = 3
OPENS_AT = time(9, 30)
#: In and out is two orders; five sessions is ten.
ORDERS_A_WEEK_NEEDED = 10

DATA, TIMELINESS, VENUE, PACE = "data_integrity", "quote_timeliness", "venue_capability", "pace"


@dataclass(frozen=True)
class Check:
    """One thing an intraday mode needs, and what was found. ``ok`` None: it could not be run."""

    key: str
    ok: bool | None
    zh: str
    en: str
    facts: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"key": self.key, "ok": self.ok, "text": {"zh": self.zh, "en": self.en},
                "facts": self.facts}


def data_integrity(stamps: list[datetime] | None, *, now: datetime) -> Check:
    """Whether the current session's minute bars are all there."""
    local = now.astimezone(quotes.NEW_YORK)
    opened = datetime.combine(local.date(), OPENS_AT, tzinfo=quotes.NEW_YORK)
    if stamps is None:
        return Check(DATA, None, "读不到今天的分钟行情，无法检查完整性。",
                     "Today's minute bars could not be read, so their completeness is unknown.")
    todays = sorted({s.astimezone(quotes.NEW_YORK).replace(second=0, microsecond=0) for s in stamps
                     if s.astimezone(quotes.NEW_YORK).date() == local.date()})
    expected = int((local - opened).total_seconds() // 60)
    if expected <= 0 or not todays:
        return Check(DATA, None, "今天还没有开盘后的分钟行情可以检查。",
                     "There are no minute bars since today's open to check yet.",
                     {"expected_minutes": max(expected, 0), "bars": len(todays)})
    have = sum(1 for s in todays if opened <= s < opened + timedelta(minutes=expected))
    gaps = [(later - earlier).total_seconds() / 60.0 - 1 for earlier, later in zip(
        [opened - timedelta(minutes=1), *todays], [*todays, local.replace(second=0, microsecond=0)],
        strict=False)]
    longest = max(0.0, max(gaps))
    share = round(min(have / expected, 1.0) * 100.0, 2)
    ok = share >= COMPLETENESS_PCT and longest <= LONGEST_GAP_MINUTES
    facts = {"expected_minutes": expected, "bars": have, "complete_pct": share,
             "longest_gap_minutes": round(longest, 1), "needed_pct": COMPLETENESS_PCT,
             "longest_gap_allowed": LONGEST_GAP_MINUTES}
    return Check(DATA, ok,
                 f"开盘以来 {expected} 分钟里有 {have} 根分钟线（{share}%），最长缺口 {longest:.0f} 分钟；"
                 f"要求不低于 {COMPLETENESS_PCT:g}% 且缺口不超过 {LONGEST_GAP_MINUTES} 分钟。",
                 f"{have} minute bar(s) for the {expected} minute(s) since the open ({share}%), the "
                 f"longest gap {longest:.0f} min; at least {COMPLETENESS_PCT:g}% and no gap over "
                 f"{LONGEST_GAP_MINUTES} min are needed.", facts)


def timeliness(newest: datetime | None, *, now: datetime, max_age_seconds: float) -> Check:
    """Whether the newest price is as young as the real account requires a quote to be."""
    if newest is None:
        return Check(TIMELINESS, None, "读不到最新一根分钟行情，无法检查时效。",
                     "The newest minute bar could not be read, so its age is unknown.")
    age = (now - newest).total_seconds()
    return Check(TIMELINESS, age <= max_age_seconds,
                 f"最新行情距现在 {age:.0f} 秒；实盘对报价的要求是不超过 {max_age_seconds:.0f} 秒。",
                 f"The newest price is {age:.0f}s old; the real account requires a quote no older "
                 f"than {max_age_seconds:.0f}s.", {"age_seconds": round(age, 1),
                                                    "max_age_seconds": float(max_age_seconds)})


def venue_capability(profiles: Any, account: str = "micro_live") -> Check:
    """Whether what is on record about the venue covers what trading within a day needs."""
    venue = requirements.venue_of(profiles, account)
    known = requirements.for_venue(venue)
    missing_zh, missing_en = [], []
    if known is None or not known.confirmed_for_account:
        missing_zh.append("券商的下单要求只读自公开文档，尚未对本账户确认")
        missing_en.append("the venue's order requirements are read from its documentation and "
                          "not confirmed for this account")
    # None of these is on record anywhere in this system. They are what a daily mode never has
    # to know and an intraday one cannot run without.
    missing_zh += ["卖出后资金何时可以再次买入（结算规则）没有记录",
                   "同一天买入又卖出的次数限制（日内交易规则）没有记录"]
    missing_en += ["when the proceeds of a sale may be spent again (settlement) is not on record",
                   "how same-day round trips are counted and limited is not on record"]
    if known is not None and set(known.fractional_order_types) <= {"market"}:
        missing_zh.append("碎股只能下市价单，不能挂止损或限价单，盘中退出只能由本地规则发市价单")
        missing_en.append("a fraction of a share can only be a market order: no resting stop or "
                          "limit, so an exit inside the session would have to be a market order "
                          "sent by local rules")
    return Check(VENUE, False,
                 "券商能力尚未核实：" + "；".join(missing_zh) + "。",
                 "The venue's capability is not established: " + "; ".join(missing_en) + ".",
                 {"venue": venue, "confirmed_for_account": bool(known and known.confirmed_for_account),
                  "fractional_order_types": list(known.fractional_order_types) if known else None,
                  "open": len(missing_zh)})


def pace(profiles: Any, account: str = "micro_live") -> Check:
    """Whether the account's own limits on activity leave room for in-and-out every session."""
    limits = ((profiles.risk.get("buckets") or {}).get(account) or {}).get("activity_limits") or {}
    per_week = profiles.effective(limits.get("max_orders_per_week"), "activity.max_orders_per_week")
    round_trips = limits.get("no_same_day_round_trips")
    facts = {"max_orders_per_week": None if per_week is None else int(per_week),
             "needed": ORDERS_A_WEEK_NEEDED, "no_same_day_round_trips": round_trips}
    short = per_week is not None and int(per_week) < ORDERS_A_WEEK_NEEDED
    if not short and round_trips is not True:
        return Check(PACE, True, "账户的下单频率限制容得下每天一进一出。",
                     "The account's limits on activity leave room for in and out each session.", facts)
    zh = (f"账户每周最多下 {int(per_week)} 笔单，每天一进一出需要约 {ORDERS_A_WEEK_NEEDED} 笔。" if short else "")
    en = (f"The account allows {int(per_week)} orders a week; in and out each session is about "
          f"{ORDERS_A_WEEK_NEEDED}. " if short else "")
    if round_trips is True:
        zh += "账户还禁止同一天买入又卖出同一只标的。"
        en += "The account also forbids buying and selling the same symbol on one day."
    return Check(PACE, False, zh, en.strip(), facts)


def _vendor_stamps(symbol: str) -> list[datetime]:
    frame = quotes._vendor_minutes(symbol)
    closes = frame["Close"].dropna()
    return [stamp.to_pydatetime() for stamp in closes.index]


def run(profiles: Any, *, symbol: str = "SPY", account: str = "micro_live",
        minutes: Callable[[str], list[datetime]] | None = None,
        now: datetime | None = None) -> dict[str, Any]:
    """Every check, with whether together they would let an intraday mode be relied on.

    ``ready`` is true only if every check passed. One that could not be run is not a pass.
    """
    now = now or datetime.now(UTC)
    try:
        stamps: list[datetime] | None = (minutes or _vendor_stamps)(symbol)
    except Exception:  # noqa: BLE001 - a feed that cannot be read is a check that could not run
        stamps = None
    todays = [s for s in stamps or []
              if s.astimezone(quotes.NEW_YORK).date() == now.astimezone(quotes.NEW_YORK).date()]
    fast = (profiles.risk.get("data_freshness") or {}).get("fast") or {}
    found = [data_integrity(stamps, now=now),
             timeliness(max(todays) if todays else None, now=now,
                        max_age_seconds=float(fast.get("max_age_seconds", 120))),
             venue_capability(profiles, account), pace(profiles, account)]
    return {"checked_at": now.isoformat(), "symbol": symbol.upper(),
            "ready": all(check.ok is True for check in found),
            "checks": [check.as_dict() for check in found],
            "built": False,
            "note": {"zh": "短线模式尚未实现。这些检查说明的是：即使实现了，还有哪些条件不满足。",
                     "en": "The intraday mode is not built. These checks say what would still be "
                           "missing if it were."}}
