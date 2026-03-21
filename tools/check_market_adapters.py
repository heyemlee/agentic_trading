"""Read the market-data vendor once through the adapters automatic trading uses.

The suite cannot find this class of mistake. It is offline by construction, so every bar an
adapter has parsed in a test is one a fixture wrote, in the shape its author believed the vendor
returns. The vendor's library has changed that shape before -- a column index that became two
levels deep -- and an adapter that has only met fixtures learns of it from the simulated
account, as a feed that cannot be read.

    uv run python tools/check_market_adapters.py

What it reads, for the funds a plan may buy: the newest session's one-minute bars (the simulated
venue's price, and the intraday checks) and two and a half years of daily bars (experiments and
the trend rule). It then runs the comparison over those bars and holds its 60/40 row against the
same figure worked out by hand.

What it does not do: start an account, read state, open a venue session or load a credential.
It moves into a temporary directory before the upstream data layer is imported, because that
import loads the first ``.env`` found walking up from the working directory -- which from this
checkout, or from any worktree beneath it, is the deployment's own. The vendor's cache is pointed
at the same temporary directory, so this reads the vendor and not what production last cached,
and leaves nothing behind in the cache production reads.

It says nothing about whether a strategy works. There is no strategy in it: funds are held.

Run while the market is open, it also reports how old the newest minute bar is. That is the one
thing about the feed that cannot be known while the market is shut, and it is what decides
whether the simulated account's orders are priced at all (``auto.paper.QUOTE_MAX_AGE_SECONDS``).

In a worktree, set ``PYTHONPATH`` to the worktree: the virtual environment's own copy of the
package is the checkout's, and the first line of output says which one was read.

Exits non-zero if anything it checked did not hold.
"""

from __future__ import annotations

import argparse
import itertools
import os
import shutil
import sys
import tempfile
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

#: Always read, whatever the plan's funds are: the benchmark's two, and the intraday checks' one.
ALWAYS = ("VTI", "BND", "SPY")
BENCHMARK = {"VTI": 60.0, "BND": 40.0}
WINDOW_DAYS = 730
#: The simple rule averages a hundred sessions; this many calendar days hold them.
WARM_UP_DAYS = 170
#: What a spread, slippage and fees on one purchase of each fund can cost, generously. A ledger
#: further than this from first-open-to-last-close is not paying costs, it is computing something
#: else.
BY_HAND_TOLERANCE_POINTS = 0.5


class Findings:
    def __init__(self) -> None:
        self.rows: list[tuple[str, bool, str]] = []

    def note(self, label: str, ok: bool, detail: str = "") -> None:
        self.rows.append((label, ok, detail))
        print(("  ok    " if ok else "  FAIL  ") + label + (f": {detail}" if detail else ""), flush=True)

    @property
    def failed(self) -> list[str]:
        return [label for label, ok, _ in self.rows if not ok]


def minute_bars(symbols: list[str], found: Findings, now: datetime) -> None:
    from agentic_trading.auto import intraday, paper, quotes

    new_york = quotes.NEW_YORK
    for symbol in symbols:
        try:
            frame = quotes._vendor_minutes(symbol)
        except Exception as exc:  # noqa: BLE001 - whatever the vendor raised is the finding
            found.note(f"{symbol} minute bars could be read", False, f"{type(exc).__name__}: {exc}"[:200])
            continue
        if frame is None or getattr(frame, "empty", True):
            found.note(f"{symbol} minute bars could be read", False, "the vendor returned no rows")
            continue
        zone = getattr(frame.index, "tz", None)
        found.note(f"{symbol} minute bars have one Close column and a market time",
                   "Close" in frame.columns and zone is not None,
                   f"{len(frame)} rows, {frame.index[0]} .. {frame.index[-1]}")
        if "Close" not in frame.columns or zone is None:
            continue

        def held(_symbol: str, kept: Any = frame) -> Any:
            return kept

        newest = frame["Close"].dropna().index[-1].to_pydatetime()
        session = newest.astimezone(new_york).date()
        try:
            price = quotes.last_minute(symbol, now=now, download=held)
        except quotes.FeedError as exc:
            # The market is shut, or the feed is a session behind. Either way the adapter's
            # answer has to be this refusal, and never the earlier session's last price.
            found.note(f"{symbol} is given no price from an earlier session",
                       "not the current session" in str(exc), str(exc))
        else:
            age = price.age_seconds(now)
            found.note(f"{symbol} newest minute bar is young enough for the simulated account",
                       age <= paper.QUOTE_MAX_AGE_SECONDS,
                       f"{age:.0f}s old; the simulated account accepts {paper.QUOTE_MAX_AGE_SECONDS:.0f}s")

        # The same frame read half a minute after its own newest bar: a price, and its exact age.
        then = newest + timedelta(seconds=30)
        try:
            price = quotes.last_minute(symbol, now=then, download=held)
        except quotes.FeedError as exc:
            found.note(f"{symbol} is priced inside its own session", False, str(exc))
        else:
            found.note(f"{symbol} is priced inside its own session",
                       price.as_of == newest.astimezone(UTC) and abs(price.age_seconds(then) - 30.0) < 1e-6,
                       f"as of {price.as_of.isoformat()}")

        stamps = [stamp.to_pydatetime() for stamp in frame["Close"].dropna().index]
        ended = datetime.combine(session, time(16, 0), tzinfo=new_york)
        whole = intraday.data_integrity(stamps, now=min(ended, now))
        facts = whole.facts
        # Reported, not held to: a thin fund missing minutes is a fact about the fund, and the
        # intraday check exists to say so. What must hold is that it could be measured at all.
        found.note(f"{symbol} session completeness could be measured", whole.ok is not None,
                   f"{facts.get('bars')} of {facts.get('expected_minutes')} minutes "
                   f"({facts.get('complete_pct')}%), longest gap {facts.get('longest_gap_minutes')} min"
                   if facts else whole.en)


def daily_bars(symbols: list[str], found: Findings, now: datetime) -> tuple[dict[str, Any], str, str] | None:
    from agentic_trading.auto import bars

    final = bars.last_completed_session(now)
    start = (date.fromisoformat(final) - timedelta(days=WINDOW_DAYS)).isoformat()
    warm = (date.fromisoformat(start) - timedelta(days=WARM_UP_DAYS)).isoformat()
    print(f"  last completed session {final}; window {start} .. {final}, warm-up from {warm}")
    try:
        history = bars.read(symbols, start=warm, end=final, now=now)
        again = bars.read(symbols, start=warm, end=final, now=now)
    except bars.BarsUnavailable as exc:
        found.note("daily bars could be read", False, str(exc))
        return None
    found.note("a read from the cache is the data that was downloaded",
               bars.digest(history) == bars.digest(again), bars.digest(history))
    for symbol, rows in history.items():
        days = [bar.date for bar in rows]
        long_gaps = [(a, b) for a, b in itertools.pairwise(days)
                     if (date.fromisoformat(b) - date.fromisoformat(a)).days > 5]
        found.note(f"{symbol} sessions are in order, unique and on weekdays",
                   days == sorted(set(days)) and all(date.fromisoformat(d).weekday() < 5 for d in days),
                   f"{len(days)} sessions from {days[0]}")
        found.note(f"{symbol} has no gap longer than a holiday weekend", not long_gaps, str(long_gaps[:3]))
        # The vendor has published a session late before (2026-09-22), and everything dated by
        # the last bar then falls a day behind. Worth knowing on the day it happens.
        found.note(f"{symbol} reaches the last completed session", days[-1] == final,
                   f"last bar {days[-1]}, expected {final}")
    lengths = {symbol: len(rows) for symbol, rows in history.items()}
    found.note("every fund has the same sessions", len(set(lengths.values())) == 1, str(lengths))
    return history, start, final


def arithmetic(history: dict[str, Any], start: str, end: str, universe: list[str], found: Findings) -> None:
    from agentic_trading.auto import backtest as bt
    from agentic_trading.auto import comparison

    held = tuple(universe[:3]) or tuple(BENCHMARK)
    rules = bt.Rules(universe=held, max_positions=len(held), position_pct=round(90.0 / len(held), 2))
    out = comparison.run(bars=history, start=start, end=end, mode="steady", rules=rules,
                         capital_usd=100.0, opening_cash_usd=100.0, deploy_cap_usd=100.0,
                         benchmark_weights=BENCHMARK)
    for name, row in out["rows"].items():
        found.note(f"the {name} row was computed", row.get("figures") is not None,
                   "" if row.get("figures") is not None else str(row.get("why_not")))
    ledger = (out["rows"][comparison.BENCHMARK].get("figures") or {}).get("net_return_pct")
    if ledger is None:
        return
    inside = {symbol: [bar for bar in history[symbol] if start <= bar.date <= end] for symbol in BENCHMARK}
    by_hand = (sum(share / 100.0 * inside[symbol][-1].close / inside[symbol][0].open
                   for symbol, share in BENCHMARK.items()) - 1.0) * 100.0
    found.note("the 60/40 row is first open to last close, less what trading costs",
               abs(ledger - by_hand) < BY_HAND_TOLERANCE_POINTS,
               f"the ledger is {ledger - by_hand:+.3f} points from the figure worked out by hand")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="check_market_adapters",
        description="Read the market-data vendor once through the adapters automatic trading uses.",
        epilog="Reads the vendor only: no state, no venue session, no credential. Says nothing "
               "about whether a strategy works. Exits non-zero if a check did not hold.")
    parser.add_argument("--config-dir", default="config",
                        help="the config directory whose exposure groups name the funds a plan may buy")
    args = parser.parse_args(argv)
    config_dir = Path(args.config_dir).resolve()

    # Everything below runs from a directory with no .env above it, and with the vendor's cache
    # inside it. Set before the first import that could reach the upstream package.
    holder = Path(tempfile.mkdtemp(prefix="ct-market-adapters-")).resolve()
    came_from = Path.cwd()
    os.chdir(holder)
    os.environ["TRADINGAGENTS_CACHE_DIR"] = str(holder / "vendor-cache")
    before = set(os.environ)
    try:
        import yfinance

        yfinance.set_tz_cache_location(str(holder / "yfinance-cache"))

        from agentic_trading.auto import bars, plan
        from agentic_trading.profile import load_profiles

        now = datetime.now(UTC)
        print(f"reading {Path(bars.__file__).resolve().parent}")
        print(f"at {now.isoformat(timespec='seconds')}, yfinance {yfinance.__version__}")
        profiles = load_profiles(config_dir)
        universe = plan.universe_of(profiles, plan.allowed_classes(profiles, "micro_live"))
        symbols = list(dict.fromkeys([*universe, *ALWAYS]))
        print(f"funds a plan may buy: {', '.join(universe) or '(none)'}; also read: "
              f"{', '.join(s for s in ALWAYS if s not in universe) or '(none)'}")

        found = Findings()
        print("\none-minute bars")
        minute_bars(symbols, found, now)
        print("\ndaily bars")
        read = daily_bars(symbols, found, now)
        if read is not None:
            print("\nthe comparison's arithmetic over those bars (funds held; no strategy is in this)")
            arithmetic(*read, universe, found)

        arrived = [name for name in set(os.environ) - before
                   if name.startswith("CT_") or name.endswith(("_KEY", "_TOKEN", "_SECRET", "_PASSPHRASE"))]
        print()
        found.note("no deployment environment was loaded along the way", not arrived,
                   f"{len(arrived)} variable(s) appeared")
        failed = found.failed
        print(f"\n{len(found.rows) - len(failed)} of {len(found.rows)} checks held")
        if failed:
            print("did not hold:\n  " + "\n  ".join(failed), file=sys.stderr)
        return 1 if failed else 0
    finally:
        os.chdir(came_from)
        shutil.rmtree(holder, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
