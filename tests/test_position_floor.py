"""The floor under a position code may reduce by itself. Fully offline.

The rule is one sentence: deterministic code may never take a position below a declared share of
the most a person ever held in it. Everything here is about the two ways that sentence can be got
wrong — a baseline that drifts down, and a baseline derived from a record that does not cover the
position — and both are refusals rather than numbers.

The worked example in ``test_a_small_human_purchase_buys_back_exactly_itself`` is the frontend's,
kept verbatim because it is what made the second draft of the contract wrong.
"""

import json
import uuid

import pytest

from agentic_trading import autonomy
from agentic_trading.execution_scenarios import _profiles, _proposal
from agentic_trading.proposals import EXECUTED

MICRO = "micro_live"


@pytest.fixture
def armed(tmp_path):
    profiles = _profiles(tmp_path)
    profiles.risk["buckets"][MICRO]["autonomy"] = {
        "reduce": {"enabled": True, "min_confidence": 80, "max_positions_reduced_per_day": 1,
                   "min_remaining_pct_of_opened": 25}}
    return profiles


class Log:
    """Fills and the approval records they belong to, built in the order they happened."""

    def __init__(self):
        self.fills: list[dict] = []
        self.proposals: list = []
        self._minute = 0

    def _at(self) -> str:
        self._minute += 1
        return f"2026-09-10T14:{self._minute:02d}:00+00:00"

    def buy(self, quantity: float, *, via: str | None = "console", symbol: str = "VTI",
            bucket: str = MICRO) -> str:
        return self._fill("buy", quantity, via=via, symbol=symbol, bucket=bucket)

    def sell(self, quantity: float, *, via: str | None = "autonomy", symbol: str = "VTI",
             bucket: str = MICRO) -> str:
        return self._fill("sell", quantity, via=via, symbol=symbol, bucket=bucket)

    def _fill(self, side: str, quantity: float, *, via, symbol, bucket) -> str:
        p = _proposal(bucket=bucket, ticker=symbol, action="BUY" if side == "buy" else "REDUCE",
                      status=EXECUTED)
        p.approved_via = via
        self.proposals.append(p)
        self.fills.append({"at": self._at(), "proposal_id": p.proposal_id, "bucket": bucket,
                           "symbol": symbol, "side": side, "quantity": quantity, "price": 370.0,
                           "status": "FILLED", "fill_id": uuid.uuid4().hex})
        return p.proposal_id

    def floor(self, profiles, *, held: float | None, symbol: str = "VTI",
              bucket: str = MICRO) -> autonomy.PositionFloor:
        return autonomy.position_floor_for(profiles, fills=self.fills, proposals=self.proposals,
                                          bucket=bucket, symbol=symbol, ledger_quantity=held)


# ------------------------------------------------------------------ the baseline only rises

def test_a_small_human_purchase_buys_back_exactly_itself(armed):
    """The frontend's counter-example, which is why the baseline is a high water mark.

    A hundred shares a person bought, a floor of a quarter of them, code reduces to the floor.
    The person then buys one share. Under a "since the last purchase" baseline the floor would
    fall to 6.5 and code could sell another 19.5; under a high water mark the floor stays at 25
    and code gets back exactly the one share.
    """
    log = Log()
    log.buy(100.0)
    log.sell(75.0)
    at_the_floor = log.floor(armed, held=25.0)
    assert at_the_floor.high_water == 100.0
    assert at_the_floor.floor == 25.0
    assert at_the_floor.headroom == 0.0

    log.buy(1.0)
    after = log.floor(armed, held=26.0)
    assert after.high_water == 100.0, "a one-share purchase does not reset the baseline"
    assert after.floor == 25.0, "the floor is only ever lowered by changing the configuration"
    assert after.headroom == 1.0, "code regains the one share and no more"

    assert autonomy.permits(armed, bucket=MICRO, action="REDUCE", confidence=85,
                            held_quantity=26.0, requested_quantity=1.0, floor=after).permitted
    refused = autonomy.permits(armed, bucket=MICRO, action="REDUCE", confidence=85,
                               held_quantity=26.0, requested_quantity=1.5, floor=after)
    assert not refused.permitted
    assert "below its floor of 25" in refused.reasons[-1]


def test_selling_never_lowers_the_baseline(armed):
    log = Log()
    log.buy(0.4)
    log.sell(0.1)
    log.sell(0.1)
    floor = log.floor(armed, held=0.2)
    assert floor.high_water == 0.4 and floor.floor == pytest.approx(0.1)
    assert floor.headroom == pytest.approx(0.1)


def test_the_baseline_is_the_largest_holding_a_person_ever_reached(armed):
    """Two purchases with a sale between them: the mark is the peak, not the last one."""
    log = Log()
    log.buy(1.0)
    log.buy(1.0)          # two shares held, the peak
    log.sell(0.5)
    log.buy(0.1)          # a person buys again, but 1.6 is below the peak
    floor = log.floor(armed, held=1.6)
    assert floor.high_water == 2.0
    assert floor.floor == pytest.approx(0.5)


def test_code_cannot_raise_the_baseline_by_buying(armed):
    """Opening is separately switched and can be autonomous, so this is a real case."""
    log = Log()
    log.buy(1.0, via="console")
    log.buy(3.0, via=autonomy.AUTONOMOUS_VIA)
    floor = log.floor(armed, held=4.0)
    assert floor.high_water == 1.0, "only what a person bought bounds what code may sell"
    assert floor.floor == pytest.approx(0.25)
    assert floor.headroom == pytest.approx(3.75)


def test_the_source_of_the_baseline_is_named(armed):
    log = Log()
    log.buy(1.0, via="console")
    log.buy(1.0, via="cli")
    floor = log.floor(armed, held=2.0)
    assert floor.high_water_via == "cli", "the approval that pushed the mark to where it is"
    assert floor.high_water_at == floor.cycle_started_at or floor.high_water_at is not None


# ------------------------------------------------------------------ the cycle

def test_a_closed_position_starts_a_new_cycle(armed):
    """The fills' own netted zero ends a life. What was bought before it bounds nothing after."""
    log = Log()
    log.buy(100.0)
    log.sell(100.0)
    assert log.floor(armed, held=0.0).high_water is None
    log.buy(4.0)
    floor = log.floor(armed, held=4.0)
    assert floor.high_water == 4.0, "the new life is bounded by its own purchase"
    assert floor.floor == pytest.approx(1.0)
    assert floor.cycle_started_at is not None


def test_a_position_reduced_to_almost_nothing_keeps_its_baseline(armed):
    """Almost closed is not closed: a dust holding still belongs to the life that built it."""
    log = Log()
    log.buy(100.0)
    log.sell(99.9)
    floor = log.floor(armed, held=0.1)
    assert floor.high_water == 100.0 and floor.floor == 25.0
    assert floor.headroom == 0.0


# ------------------------------------------------------------------ refusals

def test_a_position_nobody_bought_here_has_no_floor(armed):
    """A transferred-in holding: the book has shares the log cannot explain."""
    log = Log()
    floor = log.floor(armed, held=0.2)
    assert floor.high_water is None
    assert "shares the log cannot explain" in (floor.reason or "")
    decision = autonomy.permits(armed, bucket=MICRO, action="REDUCE", confidence=85,
                                held_quantity=0.2, requested_quantity=0.1, floor=floor)
    assert not decision.permitted and "no floor could be derived" in decision.reasons[-1]


def test_an_old_record_without_an_explicit_approver_has_no_floor(armed):
    """``approved_via`` is written by the caller; a record from before it is not evidence."""
    log = Log()
    log.buy(1.0, via=None)
    floor = log.floor(armed, held=1.0)
    assert floor.high_water is None
    assert "approved by a person" in (floor.reason or "")


def test_a_log_too_long_to_replay_refuses_rather_than_guessing(armed, monkeypatch):
    monkeypatch.setattr(autonomy, "FILL_REPLAY_LIMIT", 3)
    log = Log()
    log.buy(1.0)
    log.sell(0.1)
    log.sell(0.1)
    floor = log.floor(armed, held=0.8)
    assert floor.high_water is None
    assert "cannot be replayed from the position's first purchase" in (floor.reason or "")


def test_an_unreadable_book_refuses(armed):
    log = Log()
    log.buy(1.0)
    floor = log.floor(armed, held=None)
    assert floor.floor is None and floor.headroom is None
    assert "could not be read" in (floor.reason or "")
    assert not autonomy.permits(armed, bucket=MICRO, action="REDUCE", confidence=85,
                                held_quantity=None, requested_quantity=0.1,
                                floor=floor).permitted


def test_a_book_that_disagrees_with_the_log_refuses(armed):
    log = Log()
    log.buy(1.0)
    floor = log.floor(armed, held=1.5)
    assert floor.high_water is None
    assert "the fills account for 1 shares and the book holds 1.5" in (floor.reason or "")


def test_an_undeclared_floor_refuses_rather_than_defaulting(armed):
    del armed.risk["buckets"][MICRO]["autonomy"]["reduce"]["min_remaining_pct_of_opened"]
    log = Log()
    log.buy(1.0)
    floor = log.floor(armed, held=1.0)
    assert floor.high_water == 1.0, "the baseline is still known"
    assert floor.floor is None and "no min_remaining_pct_of_opened" in (floor.reason or "")
    decision = autonomy.permits(armed, bucket=MICRO, action="REDUCE", confidence=85,
                                held_quantity=1.0, requested_quantity=0.1, floor=floor)
    assert not decision.permitted
    assert "declares no min_remaining_pct_of_opened" in decision.reasons[-1]


def test_a_fill_with_an_unrecognised_side_refuses(armed):
    log = Log()
    log.buy(1.0)
    log.fills[-1]["side"] = "transfer"
    floor = log.floor(armed, held=1.0)
    assert floor.high_water is None and "unrecognised side" in (floor.reason or "")


def test_the_gate_refuses_when_no_floor_was_computed_at_all(armed):
    """A caller that forgets the floor gets a refusal, not a permission."""
    decision = autonomy.permits(armed, bucket=MICRO, action="REDUCE", confidence=85,
                                held_quantity=2.0, requested_quantity=0.1, floor=None)
    assert not decision.permitted and "no floor could be derived" in decision.reasons[-1]


# ------------------------------------------------------------------ isolation

def test_another_symbol_and_another_bucket_are_a_different_position(armed):
    log = Log()
    log.buy(100.0, symbol="VOO")
    log.buy(50.0, bucket="core")
    log.buy(4.0)
    floor = log.floor(armed, held=4.0)
    assert floor.high_water == 4.0, "only this bucket's fills for this symbol"
    assert log.floor(armed, held=100.0, symbol="VOO").high_water == 100.0


# ------------------------------------------------------------------ what the screen reads

def test_the_block_carries_the_contract_s_fields(armed):
    log = Log()
    log.buy(0.4)
    log.sell(0.2)
    block = autonomy.reduce_block(armed, MICRO, log.floor(armed, held=0.2))
    assert set(block) == {"permitted", "min_remaining_pct_of_opened",
                          "position_high_water_quantity", "high_water_set_at",
                          "high_water_source", "cycle_started_at", "current_quantity",
                          "floor_quantity", "headroom_quantity", "blocked_by"}
    assert block["permitted"] is True
    assert block["min_remaining_pct_of_opened"] == 25
    assert block["position_high_water_quantity"] == 0.4
    assert block["floor_quantity"] == pytest.approx(0.1)
    assert block["current_quantity"] == 0.2
    assert block["headroom_quantity"] == pytest.approx(0.1)
    assert block["high_water_source"] == "console"
    assert block["blocked_by"] == []


def test_the_block_says_why_when_the_position_is_at_its_floor(armed):
    log = Log()
    log.buy(0.4)
    log.sell(0.3)
    block = autonomy.reduce_block(armed, MICRO, log.floor(armed, held=0.1))
    assert block["permitted"] is False
    assert [b["code"] for b in block["blocked_by"]] == ["below_floor"]
    assert "belongs to a person" in block["blocked_by"][0]["detail"]


def test_the_block_says_why_when_there_is_no_baseline(armed):
    block = autonomy.reduce_block(armed, MICRO, Log().floor(armed, held=0.2))
    assert block["permitted"] is False
    assert [b["code"] for b in block["blocked_by"]] == ["no_opened_baseline"]
    assert block["position_high_water_quantity"] is None


def test_a_fill_log_read_from_the_legacy_journal_still_replays(armed, tmp_path):
    """The reader falls back to the JSONL log for a database that was never migrated."""
    from agentic_trading.service.books import BooksMixin

    log = Log()
    log.buy(1.0)
    path = tmp_path / "fills.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in log.fills) + "\n", encoding="utf-8")
    armed.risk["broker"] = {**(armed.risk.get("broker") or {}), "fills_file": str(path)}
    rows = [f.model_dump() for f in BooksMixin._recorded_fills(BooksMixin(), armed, 5000)]
    floor = autonomy.position_floor_for(armed, fills=rows, proposals=log.proposals, bucket=MICRO,
                                       symbol="VTI", ledger_quantity=1.0)
    assert floor.high_water == 1.0 and floor.floor == pytest.approx(0.25)
