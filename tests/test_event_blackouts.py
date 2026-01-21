"""Event blackouts: the days a position may not be opened into. Fully offline.

The property worth stating first, because it is the one that could hurt: a blackout must never
stand between a holding and its exit. The days these rules fire on are exactly the days someone
might most need to reduce.
"""

from datetime import date

import pytest

from agentic_trading.event_calendar import (
    RENEW_WARNING_DAYS,
    earnings_blackout,
    load_macro_calendar,
    macro_blackout,
)
from agentic_trading.portfolio import build_snapshot, classify_instrument
from agentic_trading.risk import HardRiskEngine, Proposal
from agentic_trading.risk.ladder import LadderState, LadderStates, evaluate_ladders
from agentic_trading.risk_scenarios import (
    TRADE_DATE,
    earnings_lookup,
    identity_lookup,
    make_state,
    price_lookup,
    regime,
)

TODAY = "2026-09-02"          # == TRADE_DATE


def calendar_at(profiles, tmp_path, body: str):
    path = tmp_path / "event_calendar.yaml"
    path.write_text(body, encoding="utf-8")
    profiles.risk["event_rules"]["macro_blackout_autonomous_only"]["calendar_file"] = str(path)
    return path


def good_calendar(*dates: str, through: str = "2027-12-31") -> str:
    events = "\n".join(f"  - {{date: {d}, kind: fomc_decision}}" for d in dates) or "  []"
    return f"schema_version: 1\nmaintained_through: {through}\nevents:\n{events}\n"


# -- macro releases ----------------------------------------------------------------

def test_a_human_approved_buy_is_never_blocked_by_a_macro_release(profiles, tmp_path):
    calendar_at(profiles, tmp_path, good_calendar(TODAY))
    blocked, why = macro_blackout(profiles, TODAY, autonomous=False)
    assert not blocked and "human-approved" in why


def test_an_autonomous_buy_is_blocked_on_the_release_day_and_the_day_before(profiles, tmp_path):
    calendar_at(profiles, tmp_path, good_calendar("2026-09-03"))
    today = macro_blackout(profiles, "2026-09-03", autonomous=True)
    assert today[0] and "is today" in today[1]
    eve = macro_blackout(profiles, "2026-09-02", autonomous=True)
    assert eve[0] and "is tomorrow" in eve[1]
    clear = macro_blackout(profiles, "2026-09-01", autonomous=True)
    assert not clear[0], clear[1]


def test_a_missing_calendar_refuses_autonomous_opens(profiles, tmp_path):
    profiles.risk["event_rules"]["macro_blackout_autonomous_only"]["calendar_file"] = str(
        tmp_path / "absent.yaml")
    blocked, why = macro_blackout(profiles, TODAY, autonomous=True)
    assert blocked and "fail_closed_if_missing" in why


def test_a_missing_calendar_is_not_a_blackout_when_fail_closed_is_off(profiles, tmp_path):
    rules = profiles.risk["event_rules"]["macro_blackout_autonomous_only"]
    rules["calendar_file"] = str(tmp_path / "absent.yaml")
    rules["fail_closed_if_missing"] = False
    blocked, why = macro_blackout(profiles, TODAY, autonomous=True)
    assert not blocked and "not set" in why


def test_a_calendar_that_ran_out_of_dates_refuses_autonomous_opens(profiles, tmp_path):
    # The failure mode of a hand-maintained file: it silently stops describing the future.
    calendar_at(profiles, tmp_path, good_calendar("2026-01-14", through="2026-08-31"))
    blocked, why = macro_blackout(profiles, TODAY, autonomous=True)
    assert blocked and "renew it" in why


@pytest.mark.parametrize("body,expected", [
    ("just a string", "not a mapping"),
    ("schema_version: 1\nevents: []\n", "no readable maintained_through"),
    ("maintained_through: 2027-12-31\nevents:\n  - {kind: cpi}\n", "no readable date"),
    ("maintained_through: 2027-12-31\nevents:\n  - not-a-mapping\n", "not a mapping"),
])
def test_a_malformed_calendar_behaves_like_a_missing_one(profiles, tmp_path, body, expected):
    calendar_at(profiles, tmp_path, body)
    cal = load_macro_calendar(profiles)
    assert cal.error and expected in cal.error, cal.error
    blocked, why = macro_blackout(profiles, TODAY, autonomous=True)
    assert blocked, why


def test_the_calendar_asks_to_be_renewed_before_it_expires(profiles, tmp_path):
    calendar_at(profiles, tmp_path, good_calendar(through="2026-09-20"))
    cal = load_macro_calendar(profiles)
    today = date.fromisoformat(TODAY)
    assert cal.stale_in_days(today) == 18
    assert cal.needs_renewal(today), f"18 days left is inside the {RENEW_WARNING_DAYS}-day warning"
    assert not cal.needs_renewal(date(2026, 1, 1))


def test_the_shipped_calendar_is_empty_and_therefore_fails_closed(profiles):
    """Dates were deliberately not guessed. An invented blackout calendar is worse than none:
    it would block the wrong days while looking maintained, leaving the real ones open."""
    cal = load_macro_calendar(profiles)
    assert cal.exists and not cal.error
    assert cal.events == []
    assert macro_blackout(profiles, "2026-09-09", autonomous=True)[0]


# -- earnings ----------------------------------------------------------------------

def _earnings(profiles, when, *, action="BUY", known=True, today=TODAY):
    return earnings_blackout(profiles, "AAPL", today, action=action,
                             next_earnings=date.fromisoformat(when) if when else None, known=known)


def test_a_new_position_is_refused_inside_the_pre_earnings_window(profiles):
    blocked, why = _earnings(profiles, "2026-09-04")     # 2 trading days out, limit is 5
    assert blocked and "no new position within 5" in why


def test_a_new_position_is_allowed_well_before_earnings(profiles):
    blocked, why = _earnings(profiles, "2026-10-29")
    assert not blocked, why


def test_adding_is_refused_just_after_a_print_but_a_new_buy_is_not(profiles):
    added = _earnings(profiles, "2026-09-01", action="ADD")   # reported 1 trading day ago
    assert added[0] and "no adding within 2" in added[1]
    bought = _earnings(profiles, "2026-09-01", action="BUY")
    assert not bought[0], bought[1]


def test_an_undeterminable_earnings_date_fails_closed(profiles):
    blocked, why = _earnings(profiles, None, known=False)
    assert blocked and "indistinguishable from an imminent one" in why


def test_a_company_with_no_scheduled_date_is_not_blocked(profiles):
    blocked, why = _earnings(profiles, None, known=True)
    assert not blocked and "no scheduled earnings date" in why


# -- the property that matters -----------------------------------------------------

def _engine_result(profiles, tmp_path, action, *, autonomous=False, held=()):
    calendar_at(profiles, tmp_path, good_calendar(TODAY))   # a release today: maximum blackout
    state = make_state(core_cash=2000.0, positions=[dict(p) for p in held])
    snap = build_snapshot(state, profiles, TRADE_DATE, price_lookup, identity_lookup)
    ladders = LadderStates(LadderState(3000.0), LadderState(2500.0), LadderState(500.0))
    evaluate_ladders(ladders, profiles, TRADE_DATE, snap.total_equity,
                     snap.buckets["core"].equity, snap.buckets["sandbox"].equity)
    prop = Proposal(ticker="AAPL", bucket="core", action=action, size_hint="normal",
                    confidence=85, trade_date=TRADE_DATE,
                    cls=classify_instrument("AAPL", profiles, identity_lookup),
                    data_quality="good", autonomous=autonomous,
                    evidence={"symbol": "AAPL", "source": "test", "price": 232.75,
                              "price_date": TRADE_DATE, "observed_at": f"{TRADE_DATE}T12:00:00+00:00"})
    from agentic_trading.risk_scenarios import GOOD_STOCK_INFO

    return HardRiskEngine(profiles, halt_file="/nonexistent/HALT",
                          stock_info_lookup=lambda _: GOOD_STOCK_INFO,
                          earnings_lookup=earnings_lookup).evaluate(prop, snap, ladders, regime())


@pytest.mark.parametrize("action", ["SELL", "REDUCE"])
def test_a_blackout_never_stands_between_a_holding_and_its_exit(profiles, tmp_path, action):
    held = [{"symbol": "AAPL", "bucket": "core", "quantity": 2.0, "cost_basis_per_share": 200.0}]
    result = _engine_result(profiles, tmp_path, action, autonomous=True, held=held)
    ids = {r.rule_id for r in result.rules}
    assert "HR18" not in ids and "HR19" not in ids, "a reduction must not be gated by an event"
    assert result.verdict == "APPROVE", [r.rule_id for r in result.failed_rules]


def test_an_autonomous_open_is_gated_by_the_calendar_but_a_human_one_is_not(profiles, tmp_path):
    auto = _engine_result(profiles, tmp_path, "BUY", autonomous=True)
    assert "HR19" in {r.rule_id for r in auto.failed_rules}
    human = _engine_result(profiles, tmp_path, "BUY", autonomous=False)
    assert "HR19" not in {r.rule_id for r in human.failed_rules}
    assert next(r for r in human.rules if r.rule_id == "HR19").passed
