"""What a simulated fill costs, with each figure labelled for what it is: assumed, or documented.

A simulation that fills at the price on the screen flatters every strategy by the same amount,
and the amount is not small for something that trades often. So a simulated order pays to
trade, and this module is the one place that says how much.

Two kinds of figure live here and they are never mixed up:

* **Assumed.** The half-spread and the slippage are assumptions. Nothing in this system has
  measured what this account's orders actually pay, and until real fills exist to measure them
  against, a number here is a judgement written down so that it can be argued with. They are
  set on the cautious side of what a liquid US ETF trades at.
* **Documented.** The venue's commission and the regulatory fees it passes on are read from its
  published schedule, with the page, the wording and the day it was read. They were read
  through a summarising fetch, so the last digit of a rate wants checking against the page
  before anything is made to depend on it; at this account's size every one of them rounds to
  nothing, which is why that has not been a reason to hold the work.

The same model prices the live simulation's fills (``auto.paper``) and the backtest's
(``auto.backtest``), so the two are at least wrong in the same direction by the same amount.
Nothing here places an order or reads a market.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

ASSUMED, DOCUMENTED = "assumed", "documented"
BUY, SELL = "buy", "sell"

#: Half the bid-ask spread, in basis points of the price, by the class an instrument is limited
#: under. ASSUMED. The funds this system may buy trade a cent wide on a price of tens or
#: hundreds of dollars, which is under one basis point; these are a little wider than that on
#: purpose, and an unclassified instrument takes the widest.
HALF_SPREAD_BPS: dict[str, float] = {
    "broad_diversified_etf": 1.0,
    "bond_or_cash_like_etf": 1.0,
    "sector_factor_thematic_etf": 2.5,
    "individual_stock": 3.0,
}
DEFAULT_HALF_SPREAD_BPS = 5.0
#: What an order gives up beyond the half-spread by being a market order of an odd size, in
#: basis points. ASSUMED, and deliberately not zero.
SLIPPAGE_BPS = 2.0

SOURCE = "https://robinhood.com/us/en/support/articles/trading-fees-on-robinhood/"
RETRIEVED = "2026-10-06"


@dataclass(frozen=True)
class Fee:
    """One charge the venue's schedule names, as the schedule words it."""

    key: str
    applies_to: tuple[str, ...]                 # the sides it is charged on
    per_million_usd: float = 0.0                # of principal
    per_share_usd: float = 0.0
    cap_usd: float | None = None
    #: Not passed on at or below this notional, or this many shares.
    waived_to_notional_usd: float | None = None
    waived_to_shares: float | None = None
    #: A window in which the schedule says the fee is not charged at all.
    paused: tuple[str, str] | None = None
    quote: str = ""
    basis: str = DOCUMENTED
    source: str = SOURCE
    retrieved: str = RETRIEVED

    def charge(self, side: str, quantity: float, notional: float, on: date | None = None) -> float:
        if side not in self.applies_to:
            return 0.0
        if self.paused and on is not None and self.paused[0] <= on.isoformat() <= self.paused[1]:
            return 0.0
        if self.waived_to_notional_usd is not None and notional <= self.waived_to_notional_usd + 1e-9:
            return 0.0
        if self.waived_to_shares is not None and quantity <= self.waived_to_shares + 1e-9:
            return 0.0
        amount = notional * self.per_million_usd / 1_000_000.0 + quantity * self.per_share_usd
        return min(amount, self.cap_usd) if self.cap_usd is not None else amount


#: The venue's schedule for a US-listed stock or ETF. DOCUMENTED, as read on ``RETRIEVED``.
FEES: tuple[Fee, ...] = (
    Fee("commission", (BUY, SELL),
        quote="Investing with a Robinhood investing account is commission free for stocks, ETFs, "
              "and their options."),
    Fee("sec_fee", (SELL,), per_million_usd=20.60, waived_to_notional_usd=500.0,
        quote="$20.60 per $1 million of principal ... only for sell orders ... Robinhood doesn't "
              "pass this fee on to you for equity sales with a notional value of $500 or less "
              "(as of April 4, 2026)"),
    Fee("finra_taf", (SELL,), per_share_usd=0.000195, cap_usd=9.79, waived_to_shares=50.0,
        paused=("2026-10-01", "2026-12-31"),
        quote="$0.000195 per share ... no greater than $9.79 per trade ... except for sales of 50 "
              "shares or less (as of January 1, 2026); temporarily paused from October 1, 2026, "
              "through December 31, 2026"),
    Fee("cat_fee", (BUY, SELL), per_share_usd=0.000003,
        quote="$0.000003 per share of listed equities"),
)


@dataclass(frozen=True)
class Fill:
    """What one simulated fill was priced at, and each part of the difference from the quote."""

    price: float                    # what the ledger records: the quote, less everything below
    quote: float
    half_spread_bps: float
    slippage_bps: float
    fees_usd: float
    fees: dict[str, float] = field(default_factory=dict)

    @property
    def cost_bps(self) -> float:
        return round(abs(self.price / self.quote - 1.0) * 10_000.0, 4) if self.quote else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "cost_bps": self.cost_bps}


@dataclass(frozen=True)
class Costs:
    """A cost model. ``STANDARD`` is the one a simulation uses; ``FREE`` exists to be compared with."""

    half_spread_bps: dict[str, float] = field(default_factory=lambda: dict(HALF_SPREAD_BPS))
    default_half_spread_bps: float = DEFAULT_HALF_SPREAD_BPS
    slippage_bps: float = SLIPPAGE_BPS
    fees: tuple[Fee, ...] = FEES
    name: str = "standard"

    def half_spread(self, limit_class: str | None) -> float:
        return float(self.half_spread_bps.get(str(limit_class), self.default_half_spread_bps))

    def fill(self, side: str, quote: float, quantity: float, *, limit_class: str | None,
             on: date | None = None) -> Fill:
        """The price a simulated order of this size fills at against this quote.

        Always the worse side of the quote: a purchase pays more than it, a sale receives less.
        Fees are folded into the price rather than booked beside it, so that the one number the
        ledger keeps for a fill is what the fill really cost or brought in.
        """
        if side not in (BUY, SELL):
            raise ValueError(f"a fill is a buy or a sell, not {side!r}")
        if not (quote > 0 and quantity > 0):
            raise ValueError("a fill needs a positive quote and a positive quantity")
        spread = self.half_spread(limit_class)
        away = (spread + self.slippage_bps) / 10_000.0
        traded = quote * (1.0 + away) if side == BUY else quote * (1.0 - away)
        charged = {fee.key: round(fee.charge(side, quantity, quantity * traded, on), 6)
                   for fee in self.fees}
        charged = {key: value for key, value in charged.items() if value > 0}
        total = sum(charged.values())
        per_share = total / quantity
        price = traded + per_share if side == BUY else traded - per_share
        return Fill(price=round(price, 6), quote=quote, half_spread_bps=spread,
                    slippage_bps=self.slippage_bps, fees_usd=round(total, 6), fees=charged)

    def describe(self) -> dict[str, Any]:
        """The model in the terms a record of an experiment has to state it in."""
        return {
            "name": self.name,
            "half_spread_bps": {"by_class": dict(self.half_spread_bps),
                                "otherwise": self.default_half_spread_bps, "basis": ASSUMED},
            "slippage_bps": {"value": self.slippage_bps, "basis": ASSUMED},
            "fees": [{"key": fee.key, "applies_to": list(fee.applies_to), "basis": fee.basis,
                      "per_million_usd": fee.per_million_usd, "per_share_usd": fee.per_share_usd,
                      "cap_usd": fee.cap_usd, "waived_to_notional_usd": fee.waived_to_notional_usd,
                      "waived_to_shares": fee.waived_to_shares, "paused": fee.paused,
                      "quote": fee.quote, "source": fee.source, "retrieved": fee.retrieved}
                     for fee in self.fees],
            "note": {"zh": "价差和滑点是假设值，未用真实成交校准；佣金和监管费读自券商公开的收费说明。",
                     "en": "The spread and the slippage are assumptions, not yet calibrated "
                           "against real fills; the commission and the regulatory fees are read "
                           "from the venue's published schedule."},
        }


STANDARD = Costs()
#: No spread, no slippage, no fees. Never what a simulation runs under: it is here so a result
#: can be shown beside the same result with nothing charged, which is how much of it was costs.
FREE = Costs(half_spread_bps={}, default_half_spread_bps=0.0, slippage_bps=0.0, fees=(), name="free")
