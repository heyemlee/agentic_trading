"""What each venue requires of an order, kept apart from what this product chooses.

``sizing.min_order_usd`` is $25. That is a setting of this product: below it an order is not
thought worth its own research, approval and record. It was never the venue's requirement, and
nothing had ever checked what the venue's requirement is. The two answer different questions and
fail differently when confused. Treat the product's $25 as the venue's rule and a $100 account
looks unable to hold more than three positions "because of the broker"; treat a guess at the
venue's rule as fact and the setting is lowered to a figure the venue then refuses, one order at
a time, in production.

So the venue's requirements are written down here as what they are: facts about somebody else's
system, each with where it was read and when, and with what has *not* been established listed
beside what has. Three rules follow from keeping them apart:

* the venue's minimum is a floor under the product's setting, never a replacement for it
  (:func:`agentic_trading.sizing.minimum_order` takes the larger);
* the product's setting may be lowered only as far as a minimum that is on record here, and not
  at all for a venue whose minimum is not (:func:`lowest_product_minimum`);
* a requirement read from documentation is not one the venue has confirmed for this account.
  ``confirmed_for_account`` says which, and for a real venue it is false until the venue's own
  answer has been read through the owner's session, which nothing in this module does.

Nothing here reaches a venue. The adapters enforce what they must at the moment of an order
(``broker/robinhood.py``); this is the record a plan and a person read before one.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

SIMULATED, ROBINHOOD = "simulated", "robinhood"


@dataclass(frozen=True)
class Evidence:
    """One requirement, where it was read, and the words it was read in."""

    claim: str
    source: str
    retrieved_on: str
    quote: str = ""


@dataclass(frozen=True)
class Requirements:
    venue: str
    #: The smallest order the venue accepts, in dollars. None: not established.
    min_order_usd: float | None
    #: Whether a fraction of a share, or a dollar amount, can be ordered at all.
    fractional: bool | None
    #: The order types this system may use for a fractional or dollar-amount order, and for
    #: whole shares. Narrower than the venue's own list is allowed; wider is not.
    fractional_order_types: tuple[str, ...]
    whole_share_order_types: tuple[str, ...]
    #: Whether a fractional or dollar-amount order only executes in the regular session.
    fractional_regular_hours_only: bool | None
    #: Decimal places of a share quantity. None: not established from the venue's own words.
    quantity_decimals: int | None
    #: Read from the venue's published documentation, or the simulator's own code.
    documented: bool
    #: Confirmed by the venue for this account. Never true for a real venue from this module.
    confirmed_for_account: bool
    evidence: tuple[Evidence, ...] = ()
    #: What has not been established, in words a person can act on: (Chinese, English) each.
    open_questions: tuple[tuple[str, str], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _both(zh: str, en: str) -> tuple[str, str]:
    """One open question in the two languages the console speaks."""
    return zh, en


_ROBINHOOD_FRACTIONAL = "https://robinhood.com/us/en/support/articles/fractional-shares/"
_ROBINHOOD_MARKET = "https://robinhood.com/us/en/support/articles/market-order-update/"
_ROBINHOOD_LIMIT = "https://robinhood.com/us/en/support/articles/limit-order/"

_REQUIREMENTS: dict[str, Requirements] = {
    # The in-process paper venue. Its rules are its own code, so they are known exactly and are
    # confirmed by construction: it fills any positive amount, to any fraction, at any hour its
    # clock is set open.
    SIMULATED: Requirements(
        venue=SIMULATED, min_order_usd=0.01, fractional=True,
        fractional_order_types=("market", "limit"), whole_share_order_types=("market", "limit"),
        fractional_regular_hours_only=False, quantity_decimals=6,
        documented=True, confirmed_for_account=True,
        evidence=(Evidence("the simulator fills any positive dollar amount or share quantity",
                           "agentic_trading/broker/simulated.py", "2026-10-06"),),
    ),
    # Robinhood, as its public support pages describe retail accounts. The Agentic Trading
    # interface this system places orders through publishes no separate minimum; these are the
    # nearest statements the venue makes, and whether they hold unchanged for an Agentic account
    # is the first open question below.
    ROBINHOOD: Requirements(
        venue=ROBINHOOD, min_order_usd=1.00, fractional=True,
        fractional_order_types=("market",), whole_share_order_types=("market", "limit"),
        fractional_regular_hours_only=True, quantity_decimals=None,
        documented=True, confirmed_for_account=False,
        evidence=(
            Evidence("a fractional or dollar-amount order must be for at least $1",
                     _ROBINHOOD_FRACTIONAL, "2026-10-06",
                     "If you're buying or selling fractional shares, you must enter an amount "
                     "of at least $1."),
            Evidence("fractional orders are available for most stocks and ETFs above $1 a "
                     "share with a market capitalisation above $25 million, on NMS exchanges",
                     _ROBINHOOD_FRACTIONAL, "2026-10-06",
                     "Most stocks worth over $1 per share with a market capitalization over "
                     "$25,000,000 are eligible"),
            Evidence("a dollar-amount purchase is rounded to the cent",
                     _ROBINHOOD_FRACTIONAL, "2026-10-06",
                     "All purchases will be rounded to the nearest penny."),
            Evidence("a dollar-amount order is a market order during regular hours",
                     _ROBINHOOD_MARKET, "2026-10-06",
                     "Dollar-based buy orders [and] Dollar-based sell orders [default to] "
                     "Market [during] market hours (9:30 AM-4 PM ET)"),
            Evidence("a market order placed outside regular hours waits for the next open",
                     _ROBINHOOD_MARKET, "2026-10-06",
                     "the order will be queued for the start of regular market hours on the "
                     "next trading day"),
            Evidence("a dollar-amount SELL may cover at most 95% of the position, which is why "
                     "this system sells by share quantity and never by dollar amount",
                     _ROBINHOOD_MARKET, "2026-10-06",
                     "may only sell up to 95% of your current position"),
            Evidence("the adapter refuses a dollar amount on anything but a market order, and "
                     "sends a limit order only for whole shares",
                     "agentic_trading/broker/robinhood.py (_order_args)", "2026-10-06"),
        ),
        open_questions=(
            _both("$1 最低下单和可交易范围是否原样适用于通过 Agentic Trading 接口下的单。目前只读自零售帮助页面，"
                  "券商尚未对本账户确认。",
                  "Whether the $1 minimum and the eligibility rule apply unchanged to orders "
                  "placed through the Agentic Trading interface. They are read from the retail "
                  "support pages; the venue has not confirmed them for this account."),
            _both("券商接受的股数小数位数。适配器发送 6 位，本次读到的公开页面没有给出这个数字。",
                  "How many decimal places of a share the venue accepts. The adapter sends six, "
                  "and no published page read here states the figure."),
            _both("Agentic 接口是否接受碎股或按金额订单带限价。零售页面提到盘前盘后有按金额的限价单；"
                  "适配器不使用，所以目前不影响下单。",
                  "Whether the Agentic interface accepts a limit price on a fractional or "
                  "dollar-amount order. The retail pages mention dollar-based limit orders in "
                  "extended hours; the adapter does not use them, so this blocks nothing today."),
        ),
    ),
}


def for_venue(venue: str) -> Requirements | None:
    """What is on record for a venue, or None for one nothing has been written down about."""
    return _REQUIREMENTS.get(str(venue).lower())


def venue_of(profiles: Any, bucket: str) -> str:
    """The venue an order from this bucket would be sent to.

    The same binding :func:`agentic_trading.micro_live.bucket_is_executable` enforces at
    execution: the real-money bucket goes to Robinhood and nowhere else, and every other bucket
    to the paper venue.
    """
    from ..micro_live import BUCKET as MICRO_BUCKET

    return ROBINHOOD if bucket == MICRO_BUCKET else SIMULATED


def venue_minimum_order(profiles: Any, bucket: str) -> float | None:
    """The venue's own smallest order for this bucket, or None when it is not established."""
    known = for_venue(venue_of(profiles, bucket))
    return known.min_order_usd if known is not None else None


def lowest_product_minimum(profiles: Any, bucket: str) -> tuple[float | None, str]:
    """(how low ``sizing.min_order_usd`` may be set for this bucket, why).

    The venue's minimum where one is on record. None where it is not: a setting cannot be
    lowered toward a floor nobody has established, so it stays where a person last put it.
    """
    venue = venue_of(profiles, bucket)
    known = for_venue(venue)
    if known is None or known.min_order_usd is None:
        return None, (f"the minimum order {venue} accepts is not on record, so the product's own "
                      "minimum cannot be lowered")
    confirmed = ("confirmed for this account" if known.confirmed_for_account else
                 "read from the venue's documentation, not yet confirmed for this account")
    return float(known.min_order_usd), f"{venue} accepts orders from ${known.min_order_usd:,.2f} ({confirmed})"
