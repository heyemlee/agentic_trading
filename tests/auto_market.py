"""A market the automatic-trading tests script for themselves: no vendor, no venue, no model.

Everything the plan, the trader and the simulated account read from outside this process goes
through a handful of names. ``scripted`` points each of them at a table of prices the test owns
and at the synthetic identities and regime the risk scenarios already use, so a test moves a
price by changing a number and every reader sees the move.
"""

import json
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from agentic_trading import cli, evidence, execution, portfolio, risk_scenarios
from agentic_trading.auto import backtest, paper, quotes, trader
from agentic_trading.auto import bars as bar_source
from agentic_trading.auto import costs as cost_model
from agentic_trading.broker import Clock
from agentic_trading.service import Service
from agentic_trading.service.shapes import MarketStatus


def today() -> str:
    """The market's own date, which is the session a fill is dated to."""
    return datetime.now(ZoneInfo("America/New_York")).date().isoformat()


def write_research(runs_dir, ticker="VTI", *, rating="Buy", confidence=88, reviewer="BUY",
                   missing=(), age_minutes=30, bucket="micro_live", serial=1) -> str:
    """A finished research run about the Agentic account, as the pipeline leaves one on disk.

    Half an hour old unless a test says otherwise: old enough that the simulated venue's prices
    are from after it (``trader.settle_seconds``), and nowhere near its two-day lifetime.
    """
    run_id = f"{today()}_{ticker}_{serial:08x}"
    run_dir = runs_dir / run_id
    (run_dir / "reports" / "1_analysts").mkdir(parents=True)
    for name in ("market", "news"):
        (run_dir / "reports" / "1_analysts" / f"{name}.md").write_text(
            f"{name} report for {ticker}: the trend is intact.", encoding="utf-8")
    record = {
        "run_id": run_id, "ticker": ticker, "bucket": bucket, "trade_date": today(),
        "instrument_type": "etf",
        "created_at": (datetime.now(UTC) - timedelta(minutes=age_minutes)).isoformat(),
        "upstream_decision": {"portfolio_manager_signal": rating},
        "policy": {"final_action": "BUY", "confidence": confidence, "reviewer_action": reviewer,
                   "proposal": {"data_quality": "good", "missing_information": list(missing)}},
        "llm_calls": [],
    }
    (run_dir / "decision_record.json").write_text(json.dumps(record), encoding="utf-8")
    return run_id


#: Daily closes the tests invent, by symbol: where it starts and how far it drifts a session.
#: Smooth on purpose -- a test about a rule should be able to say what the rule does on it.
DRIFT = {"VTI": (200.0, 0.0010), "VXUS": (60.0, -0.0004), "BND": (72.0, 0.0001),
         "VGIT": (58.0, 0.0), "SGOV": (100.0, 0.0002), "IAU": (60.0, 0.0006)}


def invented_bars(symbol: str, start: str, end: str) -> list[backtest.Bar]:
    """One bar for every weekday from ``start`` to ``end``, on the symbol's own straight line."""
    base, drift = DRIFT[symbol.upper()]
    origin = date(2020, 1, 1)
    out, day = [], date.fromisoformat(start)
    while day.isoformat() <= end:
        if day.weekday() < 5:
            close = round(base * (1.0 + drift * (day - origin).days / 1.4), 4)
            out.append(backtest.Bar(day.isoformat(), round(close * 0.999, 4), close))
        day += timedelta(days=1)
    return out


def scripted_history(monkeypatch, *, last: str | None = None, missing: tuple[str, ...] = ()) -> str:
    """Point every experiment's read of history at :func:`invented_bars`. Returns the last session.

    ``last`` is the newest session that has "finished"; ``missing`` names symbols whose history
    cannot be read at all.
    """
    last = last or today()

    def read(symbols, *, start, end, **_ignored):
        final = min(end, last)
        out = {}
        for symbol in dict.fromkeys(str(s).upper() for s in symbols):
            if symbol in missing or symbol not in DRIFT:
                raise bar_source.BarsUnavailable(f"{symbol}: no history in this test")
            out[symbol] = invented_bars(symbol, start, final)
            if not out[symbol]:
                raise bar_source.BarsUnavailable(f"{symbol}: no finished session between {start} and {final}")
        return out

    monkeypatch.setattr(bar_source, "read", read)
    monkeypatch.setattr(bar_source, "last_completed_session", lambda *_a, **_k: last)
    return last


def feed_of(prices: dict[str, float], *, age_seconds: float = 0.0):
    """A price feed for the simulated venue that reads the test's table, stamped as it is asked.

    ``age_seconds`` makes every price that old, for the tests about a feed that has fallen behind.
    """
    def read(symbol):
        now = datetime.now(UTC)
        if symbol.upper() not in prices:
            raise quotes.FeedError(f"the price feed has no bars for {symbol.upper()} in the current session")
        return quotes.Price(symbol.upper(), float(prices[symbol.upper()]),
                            as_of=now - timedelta(seconds=age_seconds), fetched_at=now)
    return read


def scripted(monkeypatch, prices: dict[str, float], *, market_open: bool = True,
             costs=cost_model.FREE) -> dict[str, float]:
    """Point every market read at ``prices`` (kept by reference, so the test can move them).

    The simulated venue is the real one (``auto.venue.PaperVenue``) fed from the same table. Its
    fills are free unless a test asks for the cost model: most of what is tested is what the
    pipeline does with an order, and the arithmetic of that is clearer at the quoted price.
    """

    def price(symbol, as_of):
        return prices[symbol.upper()], as_of

    real_snapshot = portfolio.build_snapshot
    real_from_broker = portfolio.snapshot_from_broker
    real_venue = paper.venue
    opened = {"market": market_open}

    def snapshot(state, profiles, trade_date, price_lookup=None, identity_lookup=None):
        # Only the vendor's defaults are replaced. A caller that brings its own prices -- the
        # rebuild from the venue at execution does -- is priced by what it brought.
        return real_snapshot(state, profiles, trade_date, price_lookup or price,
                             identity_lookup or risk_scenarios.identity_lookup)

    def venue(profiles, **overrides):
        # The simulated account's own venue, fed the test's prices rather than a vendor's.
        return real_venue(profiles, **{
            "feed": feed_of(prices), "costs": costs,
            "clock": lambda: Clock(is_open=opened["market"], timestamp=datetime.now(UTC)),
            **overrides})

    monkeypatch.setattr("agentic_trading.service.auto._last_session", today)
    monkeypatch.setattr(portfolio, "build_snapshot", snapshot)
    monkeypatch.setattr(cli, "build_snapshot", snapshot)
    monkeypatch.setattr(cli, "assess_regime", lambda *_a, **_k: risk_scenarios.regime("NORMAL"))
    monkeypatch.setattr(execution, "assess_regime", lambda *_a, **_k: risk_scenarios.regime("NORMAL"))
    monkeypatch.setattr(execution, "snapshot_from_broker", lambda *a, **k: real_from_broker(
        *a, **{**k, "identity_lookup": risk_scenarios.identity_lookup}))
    monkeypatch.setattr(evidence, "upstream_price_lookup", price)
    monkeypatch.setattr(trader, "upstream_identity_lookup", risk_scenarios.identity_lookup)
    monkeypatch.setattr(paper, "venue", venue)
    monkeypatch.setattr(Service, "market_status", lambda self: MarketStatus(is_open=market_open))
    return prices
