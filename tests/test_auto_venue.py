"""Simulated fills that are what they say: priced after the research, never on stale data, and paid for.

docs/todo.md, P2.3, for the live simulation (the backtest's half is tests/test_auto_backtest.py):

* a price carries the time of the bar it came from, not the time it was asked for;
* with no price from the current session, or one too old, nothing fills -- there is no falling
  back to an earlier close;
* an order is not priced until its research is older than any quote the venue may use, so the
  price is always from after the research;
* a fill pays the spread and slippage the cost model assumes and the fees the venue documents,
  and what each fill gave up is kept.
"""

from datetime import UTC, date, datetime

import pandas as pd
import pytest

from agentic_trading import autonomy, execution, risk_scenarios
from agentic_trading.auto import costs, paper, quotes, trader, venue
from agentic_trading.broker import BrokerRejection, BrokerUnavailable, Clock, OrderRequest
from agentic_trading.portfolio import load_portfolio
from agentic_trading.profile import load_profiles
from agentic_trading.proposals import EXECUTED, INVALIDATED, PROPOSED
from tests.auto_market import feed_of, scripted, write_research
from tests.conftest import recent_session
from tests.test_api import sandbox_service  # noqa: F401 - shared isolated fixture

MICRO = "micro_live"
NY = quotes.NEW_YORK


# ---------------------------------------------------------------- what a fill costs
def test_a_purchase_pays_more_than_the_quote_and_a_sale_receives_less():
    bought = costs.STANDARD.fill("buy", 100.0, 2.0, limit_class="broad_diversified_etf")
    sold = costs.STANDARD.fill("sell", 100.0, 2.0, limit_class="broad_diversified_etf")
    # One basis point of half-spread and two of slippage, each way.
    assert bought.price == pytest.approx(100.03, abs=1e-4) and sold.price == pytest.approx(99.97, abs=1e-4)
    assert bought.cost_bps == pytest.approx(3.0, abs=0.05) and sold.cost_bps == pytest.approx(3.0, abs=0.05)
    assert (bought.half_spread_bps, bought.slippage_bps) == (1.0, 2.0)
    # An instrument nobody classified takes the widest spread rather than none.
    unknown = costs.STANDARD.fill("buy", 100.0, 2.0, limit_class=None)
    assert unknown.half_spread_bps == costs.DEFAULT_HALF_SPREAD_BPS and unknown.price > bought.price
    assert costs.STANDARD.fill("buy", 100.0, 2.0, limit_class="individual_stock").price > bought.price


def test_the_fees_are_the_schedules_and_are_part_of_the_price():
    day = date(2026, 9, 30)
    small = costs.STANDARD.fill("sell", 100.0, 4.0, limit_class="broad_diversified_etf", on=day)
    # $400 and four shares: under both waivers, so only the audit-trail fee, which is nothing.
    assert set(small.fees) == {"cat_fee"} and small.fees_usd == pytest.approx(0.000012)
    large = costs.STANDARD.fill("sell", 100.0, 100.0, limit_class="broad_diversified_etf", on=day)
    traded = 100.0 * (1 - 3 / 10_000)
    assert large.fees["sec_fee"] == pytest.approx(100 * traded * 20.60 / 1_000_000, abs=1e-6)
    assert large.fees["finra_taf"] == pytest.approx(100 * 0.000195) and "commission" not in large.fees
    assert large.price == pytest.approx(traded - large.fees_usd / 100.0, abs=1e-6), "folded into the price"
    # The schedule pauses one of them for a quarter; inside the window it is not charged.
    paused = costs.STANDARD.fill("sell", 100.0, 100.0, limit_class="broad_diversified_etf",
                                 on=date(2026, 11, 3))
    assert "finra_taf" not in paused.fees and "sec_fee" in paused.fees
    # Nothing is charged on a purchase but the audit-trail fee; the cap holds on a huge sale.
    assert set(costs.STANDARD.fill("buy", 100.0, 100.0, limit_class=None, on=day).fees) == {"cat_fee"}
    huge = costs.STANDARD.fill("sell", 10.0, 1_000_000.0, limit_class=None, on=day)
    assert huge.fees["finra_taf"] == 9.79


def test_every_figure_says_whether_it_was_assumed_or_read_somewhere():
    said = costs.STANDARD.describe()
    assert said["half_spread_bps"]["basis"] == costs.ASSUMED and said["slippage_bps"]["basis"] == costs.ASSUMED
    assert "假设值" in said["note"]["zh"] and "未用真实成交校准" in said["note"]["zh"]
    for fee in said["fees"]:
        assert fee["basis"] == costs.DOCUMENTED and fee["quote"] and fee["retrieved"] == "2026-10-06"
        assert fee["source"].startswith("https://robinhood.com/")
    assert costs.FREE.fill("buy", 100.0, 1.0, limit_class=None).price == 100.0
    assert costs.FREE.fill("sell", 100.0, 1.0, limit_class=None).price == 100.0
    for side, quote, quantity in (("hold", 100.0, 1.0), ("buy", 0.0, 1.0), ("buy", 100.0, 0.0)):
        with pytest.raises(ValueError):
            costs.STANDARD.fill(side, quote, quantity, limit_class=None)


# ---------------------------------------------------------------- a price and its time
def _bars(*closes, start="2026-10-06 10:00", tz="America/New_York"):
    index = pd.date_range(start, periods=len(closes), freq="1min", tz=tz)
    return pd.DataFrame({"Close": list(closes)}, index=index)


NOW = datetime(2026, 10, 6, 14, 5, tzinfo=UTC)          # 10:05 in New York


def test_a_price_carries_the_time_of_its_bar_not_the_time_it_was_asked_for():
    price = quotes.last_minute("vti", now=NOW, download=lambda _s: _bars(100.0, 100.5, 101.25))
    assert (price.symbol, price.price) == ("VTI", 101.25)
    assert price.as_of == datetime(2026, 10, 6, 14, 2, tzinfo=UTC), "the bar's own start, in UTC"
    assert price.fetched_at == NOW and price.age_seconds(NOW) == 180.0
    # A bar still forming has no close yet; the last one that does is the price.
    assert quotes.last_minute("VTI", now=NOW, download=lambda _s: _bars(100.0, 100.5, float("nan"))).price == 100.5


@pytest.mark.parametrize("frame, says", [
    (pd.DataFrame(), "no bars"),
    (None, "no bars"),
    (pd.DataFrame({"Open": [1.0]}), "no bars"),
    (_bars(float("nan")), "no completed bar"),
    (_bars(100.0, start="2026-10-05 15:59"), "not the current session"),
    (_bars(100.0, start="2026-10-06 10:00", tz=None), "no market timestamp"),
    (_bars(-1.0), "unusable price"),
    (_bars(100.0, start="2026-10-06 10:20"), "in the future"),
])
def test_with_nothing_from_the_current_session_there_is_no_price(frame, says):
    with pytest.raises(quotes.FeedError, match=says):
        quotes.last_minute("VTI", now=NOW, download=lambda _s: frame)


def test_a_feed_that_cannot_be_read_is_no_price_and_is_asked_again():
    calls = []

    def read(symbol):
        calls.append(symbol)
        if len(calls) == 1:
            raise quotes.FeedError("the vendor did not answer")
        return quotes.Price(symbol, 100.0, as_of=NOW, fetched_at=NOW)

    feed = quotes.Feed(read, reuse_seconds=60)
    with pytest.raises(quotes.FeedError):
        feed("VTI")
    assert feed("vti").price == 100.0 and feed("VTI").price == 100.0
    assert calls == ["VTI", "VTI"], "a failure is not kept; a price is, briefly"

    def broken(_symbol):
        raise ConnectionError("no route")

    with pytest.raises(quotes.FeedError, match="could not be read"):
        quotes.last_minute("VTI", now=NOW, download=broken)


# ---------------------------------------------------------------- the venue itself
def _venue(tmp_path, prices, *, age=0.0, market_open=True, model=costs.STANDARD, cash=1000.0,
           positions=None, max_age=1200.0):
    return venue.PaperVenue(
        feed=feed_of(prices, age_seconds=age), costs=model,
        limit_class_of=lambda symbol: "broad_diversified_etf" if symbol.upper() == "VTI" else None,
        clock=lambda: Clock(is_open=market_open, timestamp=datetime.now(UTC)),
        max_quote_age_seconds=max_age, cash=cash, positions=positions or {},
        quantity_increment=paper.QUANTITY_INCREMENT,
        order_state_path=tmp_path / "paper_orders.json", fill_record_path=tmp_path / "fill_costs.json")


def _order(side="buy", *, notional=None, quantity=None, limit=None, coid="ct-1", symbol="VTI"):
    return OrderRequest(symbol=symbol, side=side, client_order_id=coid, notional=notional,
                        quantity=quantity, order_type="limit" if limit else "market", limit_price=limit,
                        time_in_force="day", extended_hours=False)


def test_the_venues_quote_is_the_feeds_with_its_own_time_and_a_spread(tmp_path):
    book = _venue(tmp_path, {"VTI": 200.0}, age=240.0)
    quote = book.get_quote("vti")
    assert quote.price == 200.0 and quote.source == quotes.SOURCE
    assert 235 <= (datetime.now(UTC) - quote.as_of).total_seconds() <= 245, "four minutes old, and says so"
    assert quote.bid == pytest.approx(199.98) and quote.ask == pytest.approx(200.02)
    assert quote.spread_pct == pytest.approx(0.02, abs=0.001)


def test_the_execution_paths_own_freshness_check_now_means_what_it_says(tmp_path, profiles):
    """The same check a real venue's quote is put to, with each account's own limit."""
    real_limit = profiles                      # two minutes
    simulated = load_profiles(profiles.config_dir)
    simulated.risk["data_freshness"]["fast"]["max_age_seconds"] = paper.QUOTE_MAX_AGE_SECONDS
    fresh, four_minutes, half_hour = (_venue(tmp_path, {"VTI": 200.0}, age=age) for age in (5, 240, 1800))
    assert execution._venue_price(fresh, "VTI", real_limit)[0] == 200.0
    price, why = execution._venue_price(four_minutes, "VTI", real_limit)
    assert price is None and "stale" in why
    assert execution._venue_price(four_minutes, "VTI", simulated)[0] == 200.0
    price, why = execution._venue_price(half_hour, "VTI", simulated)
    assert price is None and "stale" in why
    # No price at all is the same answer, never an older one.
    price, why = execution._venue_price(_venue(tmp_path, {}), "VTI", simulated)
    assert price is None and "could not price" in why


def test_a_fill_pays_to_trade_and_what_it_gave_up_is_kept(tmp_path):
    book = _venue(tmp_path, {"VTI": 200.0})
    bought = book.submit_order(_order("buy", notional=100.0))
    assert bought.status == "filled" and bought.filled_avg_price == pytest.approx(200.06, abs=1e-3)
    assert bought.filled_quantity == pytest.approx(100.0 / 200.06, abs=1e-6)
    assert bought.filled_quantity * bought.filled_avg_price <= 100.0, "a dollar amount buys no more than it pays for"
    assert book.get_account().cash == pytest.approx(1000.0 - bought.filled_quantity * bought.filled_avg_price)
    sold = book.submit_order(_order("sell", quantity=bought.filled_quantity, coid="ct-2"))
    assert sold.filled_avg_price == pytest.approx(199.94, abs=1e-3)
    # Round trip at an unchanged quote: the account is down exactly what trading cost.
    spent = 1000.0 - book.get_account().cash
    kept = venue.fill_costs(tmp_path / "fill_costs.json")
    assert set(kept) == {"ct-1", "ct-2"} and all(row["model"] == "standard" for row in kept.values())
    assert spent == pytest.approx(sum(row["cost_usd"] for row in kept.values()), abs=1e-4) and spent > 0
    assert kept["ct-1"]["quote"] == 200.0 and kept["ct-1"]["cost_bps"] == pytest.approx(3.0, abs=0.1)
    assert kept["ct-1"]["quote_source"] == quotes.SOURCE and kept["ct-1"]["quote_as_of"]
    assert venue.fill_costs(tmp_path / "nothing-here.json") == {}


def test_with_no_price_or_a_stale_one_nothing_fills(tmp_path):
    missing = _venue(tmp_path, {})
    with pytest.raises(BrokerUnavailable, match="no bars"):
        missing.submit_order(_order("buy", notional=100.0))
    # Too old for this account: refused by the venue itself, whatever the caller checked.
    stale = _venue(tmp_path, {"VTI": 200.0}, age=1500.0)
    with pytest.raises(BrokerUnavailable, match="does not fill on stale data"):
        stale.submit_order(_order("buy", notional=100.0, coid="ct-2"))
    for book in (missing, stale):
        assert book._orders == {} and book.get_account().cash == 1000.0
    assert venue.fill_costs(tmp_path / "fill_costs.json") == {}
    # A holding it cannot price cannot be valued either: unavailable, not worth nothing.
    holding = _venue(tmp_path, {}, positions={"VTI": (1.0, 190.0)})
    with pytest.raises(BrokerUnavailable):
        holding.get_positions()


def test_a_limit_the_real_cost_does_not_reach_rests_and_is_gone_when_the_session_ends(tmp_path):
    book = _venue(tmp_path, {"VTI": 200.0})
    # A limit at the quote: the purchase would really cost more than that, so it does not fill.
    resting = book.submit_order(_order("buy", quantity=1.0, limit=200.0))
    assert resting.status == "new" and resting.filled_quantity == 0.0
    assert book.get_account().cash == 1000.0 and [o.client_order_id for o in book.get_open_orders()] == ["ct-1"]
    # A marketable one fills, at what it costs and not at its limit.
    filled = book.submit_order(_order("buy", quantity=1.0, limit=201.0, coid="ct-2"))
    assert filled.status == "filled" and filled.filled_avg_price == pytest.approx(200.06, abs=1e-3)
    # The session ends: what was still working is cancelled, as a day order is.
    closed = _venue(tmp_path, {"VTI": 200.0}, market_open=False)
    assert closed.get_open_orders() == []
    assert closed.get_order_by_client_id("ct-1").status == "canceled"
    assert closed.get_order_by_client_id("ct-2").status == "filled"
    with pytest.raises(BrokerRejection, match="market is closed"):
        closed.submit_order(_order("buy", notional=50.0, coid="ct-3"))


def test_its_clock_is_the_exchange_calendar_unless_a_test_says_otherwise(tmp_path, monkeypatch):
    from agentic_trading.broker import robinhood

    stamp = datetime(2026, 10, 10, 15, 0, tzinfo=UTC)                 # a Saturday
    monkeypatch.setattr(robinhood, "market_clock", lambda: Clock(is_open=False, timestamp=stamp))
    book = venue.PaperVenue(feed=feed_of({"VTI": 200.0}), cash=100.0,
                            order_state_path=tmp_path / "orders.json")
    assert book.get_clock() == Clock(is_open=False, timestamp=stamp)
    with pytest.raises(BrokerRejection, match="market is closed"):
        book.submit_order(_order("buy", notional=50.0))


# ---------------------------------------------------------------- through the account
@pytest.fixture
def charged(sandbox_service, monkeypatch):  # noqa: F811
    """A started simulation whose venue charges the standard cost model."""
    prices = scripted(monkeypatch, dict(risk_scenarios.PRICES), costs=costs.STANDARD)
    preview = sandbox_service.auto_plan()
    sandbox_service.auto_start(plan_id=preview["plan_id"], identity="owner")
    return sandbox_service, prices, sandbox_service.profiles().project_dir / "runs"


def test_the_simulated_account_is_built_with_the_honest_venue_and_says_what_it_assumes(charged):
    service, _prices, _runs = charged
    sim = service._auto_paper()
    profiles = sim.profiles()
    book = sim._broker(profiles, MICRO)
    assert isinstance(book, venue.PaperVenue) and book._costs is costs.STANDARD
    assert book._max_quote_age == paper.QUOTE_MAX_AGE_SECONDS == 1200
    assert book._class_of("VTI") == "broad_diversified_etf" and book._class_of("NOSUCH") is not None
    # The one rule that differs from the real account's is written down, with the reason beside it.
    assert profiles.risk["data_freshness"]["fast"]["max_age_seconds"] == 1200
    assert service.profiles().risk["data_freshness"]["fast"]["max_age_seconds"] == 120
    marker = profiles.risk[paper.MARKER]
    assert (marker["quote_feed"], marker["cost_model"]) == ("vendor_minute_bar", "standard")
    assert trader.settle_seconds(profiles) == 1200 and trader.settle_seconds(service.profiles()) == 120


def test_a_round_trip_in_the_account_is_charged_both_ways(charged):
    service, prices, runs = charged
    write_research(runs, "VTI", confidence=88)
    service.auto_tick()
    sim = service._auto_paper()
    profiles = sim.profiles()
    held = load_portfolio(paper.ledger_path(profiles)).position("VTI", MICRO)
    quote = risk_scenarios.PRICES["VTI"]
    assert held.cost_basis_per_share == pytest.approx(quote * 1.0003, rel=1e-6), "bought above the quote"
    prices["VTI"] = round(quote * 0.88, 4)
    service.auto_tick()
    trade = service.auto_simulation()["trades"][0]
    # The stop's own loss, and on top of it what the two fills cost.
    costless = held.quantity * (prices["VTI"] - quote)
    assert trade["realized_usd"] < round(costless, 2) < 0
    kept = venue.fill_costs(paper.fill_record_path(profiles))
    assert len(kept) == 2 and {row["side"] for row in kept.values()} == {"buy", "sell"}
    paid = sum(row["cost_usd"] for row in kept.values())
    assert trade["realized_usd"] == pytest.approx(costless - paid, abs=0.02) and paid > 0


def test_research_is_not_priced_by_a_market_it_had_already_seen(charged):
    service, _prices, runs = charged
    run_id = write_research(runs, "VTI", confidence=88, age_minutes=5)       # younger than a quote may be
    waiting = service.auto_tick()["signals"][0]
    assert waiting["diagnosis"]["code"] == "price_not_after_research"
    assert "等行情更新到研究之后" in waiting["diagnosis"]["summary"]["zh"]
    assert waiting["diagnosis"]["reasons"][0]["text"]["en"].endswith("(about 15 min).")
    sim = service._auto_paper()
    assert sim._store(sim.profiles()).all() == []
    assert trader.seen_signals(sim.state_db())[run_id]["final"] is False, "it waits; it is not refused"
    # On the real account's two-minute rule the same research would already be old enough.
    assert trader.settle_seconds(service.profiles()) < 5 * 60 < trader.settle_seconds(sim.profiles())
    # Half an hour old, every quote the venue may use is from after it.
    write_research(runs, "VXUS", confidence=88, age_minutes=30, serial=2)
    outcome = {row["ticker"]: row["diagnosis"]["code"] for row in service.auto_tick()["signals"]}
    assert outcome == {"VTI": "price_not_after_research", "VXUS": "proposal_automatic"}


def test_a_purchase_turned_back_by_a_stale_feed_is_tried_again_and_not_for_ever(charged, monkeypatch):
    service, prices, runs = charged
    sim = service._auto_paper()
    profiles = sim.profiles()
    store = sim._store(profiles)
    real_venue = paper.venue
    behind = {"age": 2000.0}                                   # the feed has fallen half an hour behind

    def lagging(where, **overrides):
        return real_venue(where, **{**overrides, "feed": feed_of(prices, age_seconds=behind["age"])})

    monkeypatch.setattr(paper, "venue", lagging)
    run_id = write_research(runs, "VTI", confidence=88)
    first = service.auto_tick()
    assert first["approvals"]["approved"][0]["outcome"] == "INVALIDATED"
    assert first["approvals"]["approved"][0]["autonomy_withdrawn"] is False, "a stale quote is an ordinary not-now"
    turned_back = store.all()[0]
    assert turned_back.status == INVALIDATED and turned_back.client_order_id is None
    note = turned_back.history[-1]
    assert note["event"] == "revalidation_failed" and note["retryable"] is True and "stale" in note["detail"]
    assert load_portfolio(paper.ledger_path(profiles)).positions == [], "nothing filled on the stale price"

    # Straight away it is not tried again: a feed that is down is not hammered.
    assert service.auto_tick()["signals"] == [] and len(store.all()) == 1
    # A quarter of an hour on, with the feed caught up, the same research is given another go.
    monkeypatch.setattr(trader, "RETRY_AFTER_MINUTES", 0.0)
    behind["age"] = 0.0
    again = service.auto_tick()
    assert again["signals"][0]["retry"] == 1 and again["signals"][0]["diagnosis"]["code"] == "proposal_automatic"
    retried = next(p for p in store.all() if p.proposal_id != turned_back.proposal_id)
    kept = trader.seen_signals(sim.state_db())[run_id]
    assert kept["retries"] == 1 and kept["proposal_id"] == retried.proposal_id
    # Today's one automatic opening was spent on the attempt that was turned back, so this one
    # waits for the day to pass rather than being let through on a second count.
    assert retried.status == PROPOSED
    assert "already opened autonomously today" in again["approvals"]["declined"][0]["reasons"][0]
    # The next day -- the approval record cannot be backdated, so the day is moved instead.
    monkeypatch.setattr(autonomy, "opened_today", lambda *_a, **_k: 0)
    assert service.auto_tick()["approvals"]["approved"][0]["outcome"] == "SUBMITTED"
    assert store.get(retried.proposal_id).status == EXECUTED


def test_a_feed_that_stays_down_costs_a_few_proposals_not_one_a_minute(charged, monkeypatch):
    service, prices, runs = charged
    sim = service._auto_paper()
    store = sim._store(sim.profiles())
    real_venue = paper.venue
    monkeypatch.setattr(paper, "venue", lambda where, **overrides: real_venue(
        where, **{**overrides, "feed": feed_of(prices, age_seconds=2000.0)}))
    # Neither the wait between attempts nor the day's one opening is what is being counted here.
    monkeypatch.setattr(trader, "RETRY_AFTER_MINUTES", 0.0)
    monkeypatch.setattr(autonomy, "opened_today", lambda *_a, **_k: 0)
    run_id = write_research(runs, "VTI", confidence=88)
    for _ in range(trader.MAX_RETRIES + 5):
        service.auto_tick()
    attempts = store.all()
    assert len(attempts) == 1 + trader.MAX_RETRIES and {p.status for p in attempts} == {INVALIDATED}
    assert all(p.client_order_id is None for p in attempts), "none of them reached the venue"
    assert trader.seen_signals(sim.state_db())[run_id]["retries"] == trader.MAX_RETRIES
    assert autonomy.load_downgrade(sim.profiles()).tripped is False, "and none of it withdrew autonomy"
    assert load_portfolio(paper.ledger_path(sim.profiles())).positions == []
    assert recent_session()                       # the ledger fixture's date; nothing here needs a vendor


def test_a_purchase_refused_for_what_it_is_is_never_tried_again(charged, monkeypatch):
    """Only "not at that moment" is retried. A verdict about the idea is a verdict."""
    service, _prices, runs = charged
    sim = service._auto_paper()
    store = sim._store(sim.profiles())
    monkeypatch.setattr(trader, "RETRY_AFTER_MINUTES", 0.0)
    write_research(runs, "VTI", confidence=88)
    service.auto_tick()
    bought = store.all()[0]
    assert bought.status == EXECUTED
    assert service.auto_tick()["signals"] == [], "an executed proposal is an answer"
    # A proposal invalidated with no word that it was about the moment is left as it is.
    bought.status, bought.client_order_id = INVALIDATED, None
    bought.history.append({"at": "2026-10-06T14:00:00+00:00", "event": "revalidation_failed",
                           "detail": "INVALIDATED: the size no longer fits", "retryable": False})
    store.save(bought)
    assert service.auto_tick()["signals"] == [] and len(store.all()) == 1
