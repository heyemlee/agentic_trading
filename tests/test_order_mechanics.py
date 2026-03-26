"""What the agents are told about how an order is placed: true, and read from where it is enforced.

Research for a $100 account kept recording "one share costs more than the whole bucket" as a
reason not to act, when a purchase is sent as a dollar amount. The paragraph that says otherwise
is only worth having while every figure in it is the one the engine and the venue record use, so
these tests move the figures and expect the words to follow.
"""

from agentic_trading.broker import requirements
from agentic_trading.portfolio import render_order_mechanics


def test_the_real_account_buys_by_dollar_amount_so_a_share_price_is_no_limit(profiles):
    text = render_order_mechanics(profiles, "micro_live")
    assert text.startswith("ORDER MECHANICS")
    assert "sent to Robinhood as a dollar amount" in text
    assert "fraction of a share" in text
    assert "not a reason for or against an idea" in text
    assert "not yours to choose" in text


def test_the_limits_it_names_are_the_ones_in_the_risk_profile(profiles):
    limits = profiles.risk["buckets"]["micro_live"]["position_limits"]
    limits["max_single_position_pct_of_allocation"] = 33
    limits["max_pct_of_allocation_by_class"] = {"broad_diversified_etf": 31, "individual_stock": 7}
    text = render_order_mechanics(profiles, "micro_live")
    assert "at most 33% of the bucket in one position" in text
    assert "broad_diversified_etf 31%" in text and "individual_stock 7%" in text
    assert "40%" not in text


def test_the_smallest_order_is_the_larger_of_the_setting_and_the_venue(profiles):
    profiles.risk.setdefault("sizing", {})["min_order_usd"] = 30
    assert "smallest order code places is $30.00" in render_order_mechanics(profiles, "micro_live")
    profiles.risk["sizing"]["min_order_usd"] = 0.5
    venue = requirements.venue_minimum_order(profiles, "micro_live")
    assert f"smallest order code places is ${venue:,.2f}" in render_order_mechanics(profiles, "micro_live")


def test_no_fraction_is_promised_by_a_venue_with_nothing_on_record(profiles, monkeypatch):
    monkeypatch.setattr(requirements, "for_venue", lambda venue: None)
    text = render_order_mechanics(profiles, "micro_live")
    assert "dollar amount" not in text and "fraction" not in text
    assert "not yours to choose" in text


def test_the_paper_books_name_their_own_venue_and_limits(profiles):
    core = render_order_mechanics(profiles, "core")
    assert "sent to the paper venue as a dollar amount" in core
    classes = profiles.risk["buckets"]["core"]["position_limits_pct_of_core"]
    first = next(name for name, value in classes.items() if isinstance(value, dict | int | float))
    assert f"{first} " in core
    assert "reasoning" not in core
