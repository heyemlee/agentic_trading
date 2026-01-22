"""How an approved size becomes an order at the venue. Fully offline.

The constraint everything here follows from is Robinhood's: a fractional quantity or a dollar
amount is accepted only on a market order, so a limit price costs whole shares. Whether a limit
order is reachable at all is therefore a fact about the share price and the order cap, not a
preference — at $1,000 with a $200 cap it reaches a $72 bond ETF and cannot reach a $377
total-market one, and it widens on its own as the account grows.
"""

import pytest

from agentic_trading.execution import Revalidation, _build_order_request
from agentic_trading.execution_scenarios import _profiles, _proposal


def shape(profiles, *, bucket="micro_live", action="BUY", notional=200.0, limit_price=71.83,
          quantity=None, ticker="BND", instrument_type="etf",
          limit_class="broad_diversified_etf"):
    proposal = _proposal(ticker=ticker, bucket=bucket, action=action, notional=notional,
                         instrument_type=instrument_type, limit_class=limit_class,
                         price=limit_price)
    rv = Revalidation(outcome="VALID", executable_notional_usd=notional,
                      executable_quantity=quantity, limit_price=limit_price,
                      current_price=limit_price)
    return _build_order_request(profiles, proposal, rv, "ct-test-1")


def test_a_buy_takes_a_limit_price_when_whole_shares_fit(tmp_path):
    # $200 of a $71.83 ETF is 2 whole shares at $143.66 — a 28% trim, inside the materiality
    # ratio, so the better fill is worth having.
    request = shape(_profiles(tmp_path))
    assert request.order_type == "limit"
    assert request.quantity == 2.0
    assert request.notional is None, "a limit order must not also carry a dollar amount"
    assert request.limit_price == 71.83


def test_a_buy_stays_a_dollar_amount_when_one_share_costs_more_than_the_order(tmp_path):
    # A $377 total-market ETF under a $200 cap: no whole share fits, so the only way to express
    # the approved size is the dollar amount, which the venue takes only on a market order.
    request = shape(_profiles(tmp_path), ticker="VTI", limit_price=376.88, notional=200.0)
    assert request.order_type == "market"
    assert request.notional == 200.0 and request.quantity is None
    assert request.limit_price is None


@pytest.mark.parametrize("price", [1.0, 9.99, 50.0, 71.83, 99.99, 100.42, 101.0, 150.0, 199.99])
def test_rounding_to_whole_shares_can_never_cut_an_order_below_half(tmp_path, price):
    """Why no materiality guard sits on the rounding.

    floor(n/p)*p > n - p, which is at least n/2 whenever p <= n/2; and when p > n/2 exactly one
    share is bought for p, itself more than n/2. So the rounding can never breach
    revalidation.min_reduction_ratio at its configured 0.5, and a guard for it would be a check
    that never fires — worse than none, because it would read as protection.
    """
    notional = 200.0
    request = shape(_profiles(tmp_path), ticker="BND", limit_price=price, notional=notional)
    assert request.order_type == "limit", f"a whole share fits at {price}"
    executed = request.quantity * price
    assert executed > notional / 2, f"{executed} is below half of {notional}"
    assert executed <= notional + 1e-9, "rounding down must never increase the order"


@pytest.mark.parametrize("action", ["SELL", "REDUCE"])
def test_an_exit_stays_a_market_order(tmp_path, action):
    # An exit that does not fill is not risk reduced, and a fractional holding cannot be sold
    # any other way.
    request = shape(_profiles(tmp_path), action=action, quantity=1.5, notional=0.0)
    assert request.order_type == "market"
    assert request.quantity == 1.5 and request.notional is None
    assert request.limit_price is None


def test_the_conceptual_buckets_are_untouched(tmp_path):
    request = shape(_profiles(tmp_path), bucket="core", ticker="VTI", limit_price=378.01,
                    notional=875.0)
    assert request.order_type == "limit"
    assert request.notional == 875.0 and request.quantity is None
    assert request.limit_price == 378.01


def test_the_venue_refuses_a_dollar_amount_with_a_limit_price(tmp_path):
    """The pairing this whole design exists to avoid, asserted at the adapter rather than
    trusted: it is what makes a limit order cost whole shares."""
    from unittest import mock

    from agentic_trading.broker.base import OrderRequest
    from agentic_trading.broker.robinhood import BrokerRejection, RobinhoodBroker

    request = OrderRequest(symbol="VTI", side="buy", client_order_id="ct-x", notional=200.0,
                           quantity=None, order_type="limit", limit_price=378.0,
                           time_in_force="gfd", extended_hours=False)
    broker = RobinhoodBroker.__new__(RobinhoodBroker)
    with mock.patch.object(RobinhoodBroker, "agentic_account", return_value={}), \
            mock.patch.object(RobinhoodBroker, "_account_number", return_value="X"), \
            pytest.raises(BrokerRejection, match="whole-share quantity"):
        RobinhoodBroker._order_args(broker, request)
