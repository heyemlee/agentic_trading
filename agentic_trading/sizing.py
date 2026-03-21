"""How large an opening order may be, once every limit on it is taken together.

The Hard Risk Layer used to size an order from the strategy's hint and then put that one figure
to each limit in turn. Any limit it exceeded refused the order outright, and a figure below the
minimum order was dropped before the limits were even asked. Both are correct refusals of *that
figure*; neither says whether a smaller order would have been perfectly legal, and in a small
account the answer is usually yes. At $1,000 the "normal" size is $350 and the low tier's ticket
is $200, so every normal-sized purchase of a broad fund was refused by a limit it could simply
have been cut to.

So the order of operations is the thing this module fixes:

1. what the hint asks for;
2. the room every limit leaves, taken together -- the smallest of them is the most that may be
   ordered, and an order no larger than it satisfies all of them at once by construction;
3. the order cut down to that room, never raised to it;
4. only then, the minimum order. An order that ends below it is not placed, and the limit that
   left too little room is named.

Step 3 can only ever shrink. Nothing here can make an order larger than the hint asked for, and
nothing here reads or widens a limit: the limits arrive as numbers, computed by the engine from
the risk profile, and leave as they came. Whether to shrink at all is the caller's to say
(``shrink``), because an account that has not opted in keeps the refusals it has always had.

Pure arithmetic over figures already read: no venue, no ledger, no model.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import Any

#: Orders are placed in dollars and cents. Rounding is always down: a figure that grew by half a
#: cent on its way to the venue would be an order above a limit it was cut to.
CENT = 0.01

OK, SHRUNK = "ok", "shrunk"
AT_TARGET = "at_target"
TARGET_BELOW_MINIMUM = "target_below_minimum"
LIMIT_BELOW_MINIMUM = "limit_below_minimum"
LIMIT_EXHAUSTED = "limit_exhausted"
#: The codes under which nothing may be ordered.
INFEASIBLE = (AT_TARGET, TARGET_BELOW_MINIMUM, LIMIT_BELOW_MINIMUM, LIMIT_EXHAUSTED)


def fits_to_limits(profiles: Any, bucket: str) -> bool:
    """Whether this bucket's opening orders are cut to their limits or refused by them.

    Off unless ``sizing.fit_to_limits`` -- or ``sizing.<bucket>.fit_to_limits``, which wins --
    is literally ``true``. Absent means off, so a deployment that has not chosen it behaves
    exactly as it did before the choice existed.
    """
    settings = profiles.risk.get("sizing") or {}
    own = (settings.get(bucket) or {}).get("fit_to_limits") if isinstance(
        settings.get(bucket), dict) else None
    return (own if own is not None else settings.get("fit_to_limits", False)) is True


def minimum_order(profiles: Any, bucket: str) -> tuple[float, str]:
    """(the smallest opening order, whose rule that is) for a bucket.

    ``sizing.min_order_usd`` is a setting of this product: below it an order is not worth its
    own record. It is not what the venue requires, and was never checked against that. The
    venue's own requirement, where it is on record (agentic_trading.broker.requirements), is a
    floor under the setting: the larger of the two applies, and the answer says which it was.
    One reader, so sizing at proposal time and revalidation at approval hold the same line.
    """
    from .broker.requirements import venue_minimum_order

    settings = profiles.risk.get("sizing") or {}
    product = float(settings.get("min_order_usd", 25))
    venue = venue_minimum_order(profiles, bucket)
    if venue is not None and venue > product:
        return float(venue), "broker"
    return product, "product"


def floor_cents(amount: float) -> float:
    """A dollar amount rounded down to the cent, exactly."""
    if not math.isfinite(amount) or amount <= 0:
        return 0.0
    return float(Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_DOWN))


@dataclass(frozen=True)
class Limit:
    """The most one limit lets an opening order be, in dollars.

    ``code`` names the limit for a person (agentic_trading.diagnosis.LIMIT_NAMES) and ``rule_id``
    is the Hard Risk rule that enforces it. ``max_usd`` may be negative, when the account is
    already past the limit; that is no room, not a debt.
    """

    code: str
    rule_id: str
    max_usd: float

    @property
    def room(self) -> float:
        """The room this limit leaves. Unbounded stays unbounded; a figure that is not a number
        leaves none, because a limit that could not be computed is not a limit that was met."""
        return 0.0 if math.isnan(self.max_usd) else max(0.0, self.max_usd)


@dataclass(frozen=True)
class Sizing:
    """What may be ordered, and how that was arrived at."""

    requested_usd: float
    #: The room all the limits leave together; None when no limit applies at all.
    legal_max_usd: float | None
    #: What may be ordered. Zero whenever ``feasible`` is false.
    order_usd: float
    minimum_usd: float
    #: Whose minimum that is: the product's own setting, or the venue's requirement.
    minimum_source: str
    feasible: bool
    code: str
    #: The limits that set ``legal_max_usd``, by code, the tightest first.
    binding: tuple[str, ...] = ()
    shrink: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "feasible": self.feasible, "requested": self.requested_usd,
                "legal_max": self.legal_max_usd, "order": self.order_usd,
                "minimum": self.minimum_usd, "minimum_source": self.minimum_source,
                "binding": list(self.binding), "shrink": self.shrink}


def fit(requested_usd: float, limits: list[Limit], *, minimum_usd: float,
        minimum_source: str = "product", shrink: bool = True) -> Sizing:
    """Cut an order to the room its limits leave, then hold it to the minimum order.

    With ``shrink`` false the order is left at what was asked for and the limits are reported
    but not applied: the caller judges each one pass or fail, as it always has. The minimum is
    applied either way, and so is the difference between "already at its target" and "too small
    to place", which used to be reported as the same thing.
    """
    requested = floor_cents(requested_usd)
    minimum = max(0.0, float(minimum_usd))
    ordered = sorted(limits, key=lambda limit: limit.room)
    legal_max = (floor_cents(ordered[0].room)
                 if ordered and math.isfinite(ordered[0].room) else None)
    # Every limit within a cent of the tightest is as much the reason as the tightest is.
    binding = tuple(limit.code for limit in ordered
                    if legal_max is not None and limit.room <= legal_max + CENT)

    def answer(code: str, order: float, feasible: bool) -> Sizing:
        return Sizing(requested_usd=requested, legal_max_usd=legal_max,
                      order_usd=order if feasible else 0.0, minimum_usd=minimum,
                      minimum_source=minimum_source, feasible=feasible, code=code,
                      binding=binding if code in (SHRUNK, LIMIT_BELOW_MINIMUM, LIMIT_EXHAUSTED) else (),
                      shrink=shrink)

    if requested < CENT:
        return answer(AT_TARGET, 0.0, False)
    if requested + 1e-9 < minimum:
        # Before any limit is asked: the hint itself is for less than may be ordered. No limit
        # caused it, so none is named; what conflicts is the size setting and the minimum.
        return answer(TARGET_BELOW_MINIMUM, 0.0, False)
    if not shrink or legal_max is None:
        return answer(OK, requested, True)
    order = min(requested, legal_max)
    if order + 1e-9 < minimum:
        return answer(LIMIT_EXHAUSTED if legal_max < CENT else LIMIT_BELOW_MINIMUM, 0.0, False)
    return answer(SHRUNK if order < requested - CENT / 2 else OK, order, True)
