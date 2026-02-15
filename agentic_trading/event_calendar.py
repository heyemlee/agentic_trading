"""Event blackouts: the days a position should not be opened into.

Two different rules with two different reasons, and the difference matters.

**Earnings** is about jump risk in a single name. A stock can gap 15% on a print, which is a
loss no position sizing survives gracefully, and it happens on a date known in advance. This
applies to every buy of an individual stock, whoever approved it, because the risk is in the
instrument and not in who pressed the button. A ticker whose next earnings date cannot be
determined fails closed: an unknown print date is indistinguishable from an imminent one.

**Macro releases** (an FOMC decision, a CPI print, a payrolls report) are different. The risk
is not that the number is bad, it is that price discovery around it is fast and a decision made
minutes before it is a decision made without the information. A person choosing to buy on FOMC
day has looked at the calendar and accepted that; deterministic code acting unattended has not.
So this one gates autonomous actions only, which is what ``macro_blackout_autonomous_only`` has
declared all along.

Neither ever blocks a sell. A blackout exists to stop new risk being taken in an information
vacuum, not to stop existing risk being reduced -- and the days this fires on are exactly the
days someone might most need to get out.

The calendar is a hand-maintained file because there is no keyless feed for it. That makes
running out of dates a real failure mode, so the file declares how far it is good for: past
that it fails closed for autonomous buys, and it asks to be renewed well before then.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from .profile import Profiles

#: Warn this many days before the calendar's own end, so renewing it is never a surprise stop.
RENEW_WARNING_DAYS = 45


@dataclass
class MacroCalendar:
    path: Path
    exists: bool = False
    maintained_through: date | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None

    def dates(self) -> set[date]:
        return {e["date"] for e in self.events}

    def stale_in_days(self, today: date) -> int | None:
        if self.maintained_through is None:
            return None
        return (self.maintained_through - today).days

    def needs_renewal(self, today: date) -> bool:
        remaining = self.stale_in_days(today)
        return remaining is not None and remaining <= RENEW_WARNING_DAYS

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path), "exists": self.exists, "error": self.error,
            "maintained_through": self.maintained_through.isoformat() if self.maintained_through else None,
            "events": len(self.events),
        }


def _parse_date(value: Any) -> date | None:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    try:
        return datetime.fromisoformat(str(value)).date()
    except (TypeError, ValueError):
        return None


def load_macro_calendar(profiles: Profiles) -> MacroCalendar:
    """Read the calendar, reporting a problem rather than raising.

    A malformed calendar has to behave like a missing one: the caller decides what a missing
    calendar means, and that decision is ``fail_closed_if_missing``.
    """
    rules = (profiles.risk.get("event_rules") or {}).get("macro_blackout_autonomous_only") or {}
    name = str(rules.get("calendar_file", "config/event_calendar.yaml"))
    path = Path(name)
    if not path.is_absolute():
        path = profiles.project_dir / path
    cal = MacroCalendar(path=path)
    if not path.exists():
        return cal
    cal.exists = True
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        cal.error = f"calendar could not be read: {exc}"
        return cal
    if not isinstance(raw, dict):
        cal.error = "calendar is not a mapping"
        return cal
    cal.maintained_through = _parse_date(raw.get("maintained_through"))
    if cal.maintained_through is None:
        cal.error = "calendar has no readable maintained_through date"
        return cal
    for entry in raw.get("events") or []:
        if not isinstance(entry, dict):
            cal.error = f"calendar event is not a mapping: {entry!r}"
            return cal
        when = _parse_date(entry.get("date"))
        if when is None:
            cal.error = f"calendar event has no readable date: {entry!r}"
            return cal
        cal.events.append({"date": when, "kind": str(entry.get("kind") or "unspecified"),
                           "note": entry.get("note")})
    return cal


def macro_blackout(profiles: Profiles, trade_date: str, *, autonomous: bool,
                   calendar: MacroCalendar | None = None) -> tuple[bool, str]:
    """(blocked, reason) for opening a position on ``trade_date`` under a macro release.

    Only autonomous actions are gated. A human approval on a release day is a choice made with
    the calendar in view; unattended code has no such view.
    """
    rules = (profiles.risk.get("event_rules") or {}).get("macro_blackout_autonomous_only") or {}
    if not autonomous:
        return False, "macro blackout applies to autonomous actions only; this one is human-approved"
    cal = calendar if calendar is not None else load_macro_calendar(profiles)
    fail_closed = bool(rules.get("fail_closed_if_missing", True))
    if not cal.exists or cal.error:
        detail = cal.error or f"no macro calendar at {cal.path.name}"
        if fail_closed:
            return True, f"{detail}; fail_closed_if_missing refuses autonomous opens without it"
        return False, f"{detail}; fail_closed_if_missing is not set, so this is not a blackout"
    today = _parse_date(trade_date)
    if today is None:
        return True, f"trade date {trade_date!r} is unreadable"
    if cal.maintained_through < today:
        return True, (f"the macro calendar was only maintained through "
                      f"{cal.maintained_through.isoformat()}; renew it before autonomous opens resume")
    # The release day and the day before it. A decision taken the afternoon before a print is
    # taken without the print, which is the same exposure as taking it an hour after.
    window = {today, today + timedelta(days=1)}
    hit = sorted(e for e in cal.events if e["date"] in window)
    if hit:
        first = hit[0]
        when = "today" if first["date"] == today else "tomorrow"
        return True, (f"{first['kind']} is {when} ({first['date'].isoformat()}): no autonomous "
                      f"opening into a macro release")
    return False, (f"no macro release today or tomorrow (calendar good through "
                   f"{cal.maintained_through.isoformat()})")


def earnings_blackout(profiles: Profiles, ticker: str, trade_date: str, *, action: str,
                      next_earnings: date | None, known: bool,
                      trading_days_between=None) -> tuple[bool, str]:
    """(blocked, reason) for opening or adding to an individual stock near its earnings print.

    ``known`` is separate from ``next_earnings`` being None on purpose: a lookup that failed and
    a company with no scheduled date are different facts, and only the first fails closed.
    """
    rules = (profiles.risk.get("event_rules") or {}).get("earnings") or {}
    before = profiles.effective(rules.get("stock_no_new_position_trading_days_before", 5),
                               "risk_profile.event_rules.earnings.stock_no_new_position_trading_days_before")
    after = profiles.effective(rules.get("stock_no_add_trading_days_after", 2),
                               "risk_profile.event_rules.earnings.stock_no_add_trading_days_after")
    if not known:
        return True, (f"the next earnings date for {ticker} could not be determined; an unknown "
                      "print date is indistinguishable from an imminent one")
    if next_earnings is None:
        return False, f"{ticker} has no scheduled earnings date"
    today = _parse_date(trade_date)
    if today is None:
        return True, f"trade date {trade_date!r} is unreadable"
    if trading_days_between is None:
        from .portfolio import trading_days_between
    if next_earnings >= today:
        days = trading_days_between(today.isoformat(), next_earnings.isoformat())
        limit = int(before)
        if days <= limit:
            return True, (f"{ticker} reports on {next_earnings.isoformat()}, {days} trading day(s) "
                          f"away (no new position within {limit})")
        return False, f"{ticker} reports on {next_earnings.isoformat()}, {days} trading day(s) away"
    days = trading_days_between(next_earnings.isoformat(), today.isoformat())
    limit = int(after)
    if action == "ADD" and days <= limit:
        return True, (f"{ticker} reported on {next_earnings.isoformat()}, {days} trading day(s) ago "
                      f"(no adding within {limit})")
    return False, f"{ticker} last reported on {next_earnings.isoformat()}, {days} trading day(s) ago"


def yfinance_next_earnings(symbol: str) -> tuple[date | None, bool]:
    """(next earnings date, whether the lookup succeeded). Never raises."""
    try:
        import yfinance as yf

        frame = yf.Ticker(symbol).get_earnings_dates(limit=8)
        if frame is None or frame.empty:
            return None, True
        # The market's clock, never the laptop's: a print is scheduled against US trading days.
        today = datetime.now(ZoneInfo("America/New_York")).date()
        upcoming = [d.date() if hasattr(d, "date") else d for d in frame.index]
        future = sorted(d for d in upcoming if d >= today)
        past = sorted(d for d in upcoming if d < today)
        return (future[0] if future else (past[-1] if past else None)), True
    except Exception:  # noqa: BLE001 - an unavailable lookup is a refusal, not a crash
        return None, False
