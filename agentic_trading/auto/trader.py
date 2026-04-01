"""Carrying a confirmed plan out: research in, proposals out, and the exits a position is owed.

This is the whole of what "automatic" means here, and it is less than the word suggests. The
trader reads what research concluded, sizes a proposal by the plan, and writes it down. It reads
what the plan's own positions are worth, and where one has met its exit condition it writes a
proposal for that down too. Then it stops. Every proposal it writes is an ordinary one: it is
approved by ``Service.autonomous_reductions`` under ``autonomy.permits`` or it waits for a
person, it is revalidated against the venue, the Hard Risk Layer runs again, capital is
reserved, the order is sent and reconciled -- all by the code that does those things for a
person's order, none of it here. This module imports nothing that can place one.

It works on whichever account it is handed: the real one, or the simulated one, which is the
same code under a different directory (``auto.paper``). The only difference between them is
where research is read from -- the simulated account reads the real account's, so a rehearsal
costs no second model run.

Four things stop it adding risk, and each leaves exits and reconciliation running:

* no plan is in force, or a person paused it;
* the plan's loss line was reached -- which pauses it until a person looks;
* an order in the account has no known outcome -- reconciled first, never retried blind;
* autonomy was withdrawn after an outcome nobody could account for (``autonomy.trip``).

A signal is decided once. Whatever came of it -- a proposal, or which kind of no -- is recorded
against the research run it came from, so the same run is never acted on twice and "why did
nothing happen" has an answer for every run, not only for the ones that traded.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

from .. import autonomy, diagnosis, exits, sizing
from ..event_calendar import macro_blackout
from ..evidence import collect_evidence
from ..micro_live import BUCKET as ACCOUNT
from ..policy import decide
from ..portfolio import classify_instrument, trading_days_between, upstream_identity_lookup
from ..proposals import (
    APPROVED,
    CANCELED,
    EXECUTED,
    EXPIRED,
    INVALIDATED,
    PROPOSED,
    UNKNOWN,
    needs_reconciliation,
    new_proposal,
    positions_fingerprint,
)
from ..risk import HardRiskEngine
from ..risk import Proposal as RiskProposal
from . import bars as bar_source
from . import modes, paper, positions, store

logger = logging.getLogger("agentic_trading.service")

SIGNALS = "auto.signals"
MAX_SIGNALS = 500
#: Since which session each position of the plan's has had an exit nobody could decide.
UNDECIDED = "auto.exits_undecided"
#: Where a baseline plan stood, fund by fund, as the last pass read it (contract 3.1).
DEPLOYMENT = "auto.deployment"
#: The report each analyst leaves, by the key the evidence check knows it under.
REPORTS = {"market_report": "market", "news_report": "news",
           "fundamentals_report": "fundamentals", "sentiment_report": "social"}
ANALYST_REPORT = {"market": "market_report", "news": "news_report",
                  "fundamentals": "fundamentals_report", "social": "sentiment_report"}


@dataclass
class Tick:
    """What one pass did, in the terms a person would ask about it."""

    ran: bool = True
    skipped: str | None = None
    paused: dict[str, Any] | None = None
    signals: list[dict[str, Any]] = field(default_factory=list)
    exits: list[dict[str, Any]] = field(default_factory=list)
    approvals: dict[str, Any] | None = None
    loss: dict[str, Any] | None = None
    #: What the account was worth as this pass read it, with funding kept apart from result.
    account: dict[str, Any] | None = None
    #: What the pass could and could not read, for whatever keeps a record of passes.
    health: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"ran": self.ran, "skipped": self.skipped, "paused": self.paused,
                "signals": self.signals, "exits": self.exits, "approvals": self.approvals,
                "loss": self.loss, "account": self.account, "health": self.health}


# ---------------------------------------------------------------- research, as a signal
def records_in(runs_dir: Path, *, limit: int = 200) -> list[tuple[dict[str, Any], Path]]:
    """Finished research about the Agentic account, newest first, each with its run directory."""
    found = []
    try:
        paths = sorted(Path(runs_dir).glob("*/decision_record.json"),
                       key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
    except OSError:
        return []
    for path in paths:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        # Only research about this account, by the full pipeline. A read-only review of the
        # owner's own holdings is advice about another account and is never a signal here.
        if (record.get("bucket") != ACCOUNT or record.get("advisory") or record.get("task_kind")
                or not record.get("run_id") or not record.get("ticker")):
            continue
        found.append((record, path.parent))
    return found


def _reports(run_dir: Path) -> dict[str, str]:
    out = {}
    for key, stem in REPORTS.items():
        try:
            out[key] = (run_dir / "reports" / "1_analysts" / f"{stem}.md").read_text(encoding="utf-8")
        except OSError:
            out[key] = ""
    return out


#: Reasons that are about the moment and not about the research. A run held back only by these
#: is looked at again on the next pass, for as long as the research is still fresh; any other
#: reason is the run's answer and is not revisited.
#:
#: The account's pace is here on purpose. A week's research arrives together and the account
#: opens one position a day: if "today's opening is used" were an answer to the research, the
#: second conclusion of the week would be thrown away for having arrived on the same day as the
#: first. Nothing is loosened by waiting -- the same rule is put to the same question tomorrow,
#: and the research's own lifetime is what ends the wait.
NOT_NOW = frozenset({
    # held by this module
    "plan_paused", "autonomy_withdrawn", "plan_result_unknown", "unresolved_order",
    "order_in_progress",
    # the account's pace: used today or this week, and not tomorrow or next
    "new_positions_per_day", "new_positions_per_week", "orders_per_week", "same_day_round_trip",
    "daily_loss_gate",
    # a pause that lifts without anyone deciding anything about this research
    "kill_switch", "real_money_off", "drawdown_ladder", "market_regime", "macro_blackout",
    "earnings_blackout",
    # the data of the moment
    "prices_stale", "regime_unknown", "snapshot_warning", "price_not_after_research",
    # the strategy's own timing rule: not above its average today, which is not never
    "trend_not_confirmed", "trend_unknown",
})

#: A purchase whose approval reached execution and was turned back for a reason about the
#: moment -- no quote young enough, the venue not answering -- is written again, this many
#: times at most and no sooner than this long after it was turned back. The research is still
#: what it was; what failed was the minute it was tried in.
MAX_RETRIES = 3
RETRY_AFTER_MINUTES = 15.0


def settle_seconds(profiles: Any) -> float:
    """How old research must be before an order is priced for it.

    As old as the oldest quote the account will accept from its venue. Then whatever price an
    order is checked and filled against was observed after the research concluded, which is the
    only order of events in which the research could have led to the trade. For a real venue
    that is two minutes and changes nothing anyone would notice. For the simulated one, whose
    feed can lag, it is what keeps a fill from being priced by a market the research had
    already seen.
    """
    fast = (profiles.risk.get("data_freshness") or {}).get("fast") or {}
    return float(fast.get("max_age_seconds", 120))


def _age_seconds(stamp: Any, now: datetime | None = None) -> float | None:
    try:
        made = datetime.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        return None
    if made.tzinfo is None:
        made = made.replace(tzinfo=UTC)
    return ((now or datetime.now(UTC)) - made).total_seconds()


def retry_due(book: Book, entry: dict[str, Any], now: datetime | None = None) -> bool:
    """Whether a run already answered with a proposal should be looked at once more.

    Only when that proposal was a purchase this module wrote, never reached the venue, and was
    turned back at execution for a reason that says nothing about the idea
    (``execution.revalidate``'s ``retryable``). Bounded, and spaced, so a feed that stays down
    costs a few proposals over an hour rather than one a minute for two days.
    """
    wanted = entry.get("proposal_id")
    if not wanted or int(entry.get("retries") or 0) >= MAX_RETRIES:
        return False
    proposal = next((p for p in book.proposals if p.proposal_id == wanted), None)
    if (proposal is None or proposal.client_order_id or not proposal.strategy_version
            or proposal.status not in (INVALIDATED, EXPIRED)
            or proposal.action not in autonomy.OPENING_ACTIONS):
        return False
    turned_back = next((item for item in reversed(proposal.history)
                        if item.get("event") == "revalidation_failed"), None)
    if turned_back is None or turned_back.get("retryable") is not True:
        return False
    age = _age_seconds(turned_back.get("at"), now)
    return age is not None and age >= RETRY_AFTER_MINUTES * 60.0


def is_final(outcome: dict[str, Any]) -> bool:
    """Whether what came of a research run is its answer, rather than a wait."""
    found = outcome.get("diagnosis") or {}
    codes = [str(reason.get("code")) for reason in found.get("reasons") or []] or [str(found.get("code"))]
    return outcome.get("proposal_id") is not None or not all(code in NOT_NOW for code in codes)


def opening_hold(profiles: Any, paused: dict[str, Any] | None,
                 loss: dict[str, Any]) -> diagnosis.Reason | None:
    """Why nothing new is opened at the moment, or None when nothing stands in the way.

    None of these is a verdict on the research, and none of them touches an exit.
    """
    if paused is not None:
        return diagnosis.Reason("plan_paused", {"why": paused.get("reason") or ""})
    if autonomy.load_downgrade(profiles).tripped:
        # Nothing would carry a purchase out, and one is not left waiting for whenever a person
        # next looks: it is decided again once they have.
        return diagnosis.Reason("autonomy_withdrawn")
    if not loss.get("known", True):
        return diagnosis.Reason("plan_result_unknown")
    return None


def seen_signals(db) -> dict[str, Any]:
    """What came of each research run, by run id: the last answer, and whether it is final."""
    return dict((db.read_meta(SIGNALS, {}) or {}).get("seen") or {})


def _remember(db, run_id: str, entry: dict[str, Any], *, final: bool) -> None:
    """Keep the answer against the run. One that is not final is kept too, so "why did nothing
    happen" can be answered while it waits, and is rewritten only when the reason changes."""
    with db.transaction() as conn:
        record = db.read_meta(SIGNALS, {}) or {}
        seen = dict(record.get("seen") or {})
        before = seen.get(run_id) or {}
        if (before.get("final") is final and before.get("proposal_id") == entry.get("proposal_id")
                and (before.get("diagnosis") or {}).get("code")
                == (entry.get("diagnosis") or {}).get("code")):
            return
        seen[run_id] = {**entry, "final": final, "at": datetime.now(UTC).isoformat()}
        keep = sorted(seen, key=lambda key: str(seen[key].get("at") or ""))[-MAX_SIGNALS:]
        record["seen"] = {key: seen[key] for key in keep}
        db.write_meta(SIGNALS, record, conn)


def _why(*reasons: diagnosis.Reason) -> dict[str, Any]:
    return diagnosis.outcome_of(*reasons).as_dict()


# ---------------------------------------------------------------- the account, as it stands
@dataclass
class Book:
    """Everything one pass reads about the account, read once."""

    profiles: Any
    db: Any
    store: Any
    #: The plan in force, or None once it has been withdrawn and only its positions remain.
    plan: dict[str, Any] | None
    snap: Any
    ladders: Any
    regime: Any
    ledger: Any
    proposals: list[Any]
    fills: list[dict[str, Any]]
    managed: list[positions.Managed]
    #: Finished daily closes by symbol, read at most once a pass and only for a strategy that
    #: has a trend rule. None for a symbol whose history could not be read.
    history: dict[str, list[float] | None] = field(default_factory=dict)

    def closes(self, symbol: str, window: int) -> list[float] | None:
        """The fund's finished closes, oldest first, far enough back for an average over ``window``.

        Finished sessions only: the day in progress has no close yet, and a rule that read the
        last trade as one would be deciding on a number that is still moving.
        """
        symbol = symbol.upper()
        if symbol not in self.history:
            from datetime import date, timedelta

            try:
                start = (date.fromisoformat(self.snap.trade_date)
                         - timedelta(days=int(window * 1.6) + 15)).isoformat()
                rows = bar_source.read([symbol], start=start, end=self.snap.trade_date)[symbol]
                self.history[symbol] = [bar.close for bar in rows]
            except bar_source.BarsUnavailable as exc:
                logger.warning("no daily history for %s, so its trend is not known: %s", symbol, exc)
                self.history[symbol] = None
        return self.history[symbol]


def read_book(service: Any, profiles: Any, plan: dict[str, Any] | None, trade_date: str) -> Book:
    """The account priced and its records read, so every decision in a pass sees one world."""
    from ..cli import portfolio_context
    from ..portfolio import load_portfolio
    from ..schedule import ledger_for

    snap, ladders, regime, _text = portfolio_context(profiles, trade_date, ACCOUNT)
    proposal_store = service._store(profiles)
    everything = proposal_store.all()
    fills = service._fills_for_replay(profiles)
    ledger = load_portfolio(ledger_for(profiles, ACCOUNT))
    return Book(profiles=profiles, db=proposal_store.db, store=proposal_store, plan=plan, snap=snap,
                ladders=ladders, regime=regime, ledger=ledger, proposals=everything, fills=fills,
                managed=positions.book(fills=fills, proposals=everything, ledger=ledger,
                                       bucket=ACCOUNT))


def unresolved(book: Book) -> list[str]:
    """Orders whose outcome only the venue knows. While there is one, nothing new is sent."""
    return [p.proposal_id for p in book.proposals
            if p.bucket == ACCOUNT and needs_reconciliation(p)]


def account_figures(book: Book) -> dict[str, Any]:
    """What the account is worth, and how much of that is money that was put in.

    Deposits and withdrawals are funding and are kept out of the result: an account that was
    given another thousand dollars has not made a thousand dollars. ``result_usd`` is everything
    else -- what trading, income and fees have made of what was put in.
    """
    from ..portfolio import cash_flow_summary

    bucket = book.snap.buckets[ACCOUNT]
    funded = float(cash_flow_summary(book.ledger).get(ACCOUNT, 0.0))
    equity = round(float(bucket.equity), 2)
    # A book whose opening balance was never written down as a deposit has no funding on
    # record. Its equity less nothing is not what it made: the result is not known from here,
    # and is said to be unknown rather than reported as the whole account. (What an account has
    # made since its record began is still computed, from its marks: ``Service._auto_record``.)
    known = funded > 0
    return {"as_of": book.snap.trade_date, "equity_usd": equity,
            "cash_usd": round(float(bucket.cash_usd), 2),
            "invested_usd": round(float(bucket.market_value), 2),
            "positions": int(bucket.n_positions), "funded_usd": round(funded, 2),
            "result_usd": round(equity - funded, 2) if known else None,
            "result_pct": round((equity / funded - 1.0) * 100.0, 4) if known else None}


def health(book: Book) -> dict[str, Any]:
    """What this pass read, and what it could not: the regime, and any order nobody can place."""
    unknown = [p.proposal_id for p in book.proposals if p.bucket == ACCOUNT
               and (p.status == UNKNOWN or p.reconciliation_error)]
    priced = {view.symbol: view.price_date for view in book.snap.buckets[ACCOUNT].positions}
    return {"regime": book.regime.level, "regime_available": bool(book.regime.available),
            "unknown_orders": unknown, "price_dates": priced,
            "autonomy_withdrawn": bool(autonomy.load_downgrade(book.profiles).tripped)}


def pending(book: Book, proposal: Any) -> bool:
    """Whether this proposal could still become an order: working at the venue, or approvable.

    A proposal nobody approved goes on reading PROPOSED after its lifetime has run out -- the
    status is only rewritten when something tries to execute it -- and one past its lifetime
    can never be approved. Counting it would hold its symbol, and its place in the plan, for
    as long as the record exists.
    """
    if not exits.unfinished(proposal):
        return False
    if proposal.status in (PROPOSED, APPROVED) and not proposal.client_order_id:
        return proposal.freshness(book.profiles)[0] != EXPIRED
    return True


def in_progress(book: Book, symbol: str, *, leaving: bool = False) -> bool:
    """Whether something for this symbol is already waiting, approved or working.

    For a purchase, anything at all: two purchases of one fund is what this prevents. For an
    exit (``leaving``), only what could actually collide with it: an order already approved or
    at the venue, or an exit this module wrote itself. A sale waiting for a person's decision
    is not an order yet, and a position that has met its stop does not wait behind it -- what
    either may sell is bounded by what is held when it is sent, so two cannot sell a share twice.
    """
    for p in book.proposals:
        if p.bucket != ACCOUNT or p.ticker != symbol.upper() or not pending(book, p):
            continue
        if not leaving or p.strategy_version or p.status != PROPOSED:
            return True
    return False


def withdraw_moved_exits(book: Book, symbol: str) -> list[str]:
    """Withdraw the exits this module wrote for a symbol under an authority that has since moved.

    The autonomous pass acts only on a proposal born under the authority of the moment and
    leaves any other for a person. For research that is the point: the thinking is as old as it
    is. An exit condition is different in kind -- it is read off the book this minute and can be
    read again -- so when one is due, an earlier exit that can no longer be carried out without
    a person is withdrawn rather than left holding the position's place, and the condition is
    written again under the authority that now stands. Only one nobody has touched is withdrawn:
    still PROPOSED, never approved, no order, and not one the rules held for a person anyway.
    """
    current = autonomy.note_version(book.profiles, book.db)
    withdrawn = []
    for index, old in enumerate(book.proposals):
        if not (old.bucket == ACCOUNT and old.ticker == symbol.upper() and old.status == PROPOSED
                and old.strategy_version and old.action in autonomy.EXIT_ACTIONS
                and not old.requires_human_approval and not old.client_order_id
                and old.authority_version != current
                and any(entry.get("event") == "auto_exit" for entry in old.history)):
            continue
        with book.store.locked():
            now = book.store.get(old.proposal_id)
            if (now is None or now.status != PROPOSED or now.client_order_id
                    or now.approval_request_id):
                continue                        # a person, or the pass, reached it first
            now.status = INVALIDATED
            now.note("auto_exit_withdrawn",
                     "the authority this exit was written under has moved; the condition is "
                     "read again under the one that now stands",
                     written_under=old.authority_version, now=current)
            book.store.save(now)
            book.proposals[index] = now
        withdrawn.append(old.proposal_id)
    return withdrawn


@lru_cache(maxsize=1)
def _exchange_calendar() -> Any:
    """The NYSE's sessions, built once; None when the calendar cannot be built."""
    try:
        import exchange_calendars

        return exchange_calendars.get_calendar("XNYS")
    except Exception as exc:  # noqa: BLE001 - weekdays stand in, which only ever errs toward "old"
        logger.warning("exchange calendar unavailable, so price ages are counted in weekdays: %s", exc)
        return None


def price_stale(price_date: str | None, trade_date: str) -> bool:
    """Whether a price is too old to decide anything on in the session ``trade_date``.

    Older than the session before it: a whole session went by that the price knows nothing of,
    as when a vendor misses a day. The book still accepts a price three weekdays old
    (``portfolio_state.max_price_age_trading_days``), which is right for valuing an account and
    wrong for a stop: a stop read on Tuesday's price on Thursday answers a question about
    Tuesday (docs/AUTO_BASELINE_CONTRACT_2026-10-15.md, 2.4). Sessions are the exchange's, so a
    weekend or a holiday is not a missing day; without the calendar they are weekdays, which
    errs only toward calling a price old. A price whose day is not known is old.
    """
    try:
        first = date.fromisoformat(str(price_date or "")[:10]) + timedelta(days=1)
        last = date.fromisoformat(str(trade_date)[:10]) - timedelta(days=1)
    except ValueError:
        return True
    if first > last:
        return False
    calendar = _exchange_calendar()
    if calendar is not None:
        try:
            return len(calendar.sessions_in_range(first.isoformat(), last.isoformat())) > 0
        except Exception as exc:  # noqa: BLE001 - outside the calendar's years: weekdays instead
            logger.debug("%s to %s is outside the exchange calendar: %s", first, last, exc)
    return trading_days_between(str(price_date)[:10], str(trade_date)[:10]) > 1


def plan_loss(book: Book) -> dict[str, Any]:
    """What the plan's positions have made or lost: what their sales realized, and what is open, marked.

    The plan's positions are what a plan bought -- every purchase written under a strategy
    version, whoever approved it (``positions``) -- and everything their sales realized counts,
    whoever approved the sale. Until 2026-10-15 only orders code itself approved counted, so a
    stop a person approved took its loss off the line, and the pause it should have brought
    could never come (review of 2026-10-14, item 2; docs/AUTO_BASELINE_CONTRACT_2026-10-15.md,
    2.2). A holding a person bought for themselves is still not the plan's to answer for.

    Realized is at the plan's own average cost, from the replay of the fills, and is split by
    who approved each sale (``realized_by``). Open positions are marked at the book's prices; one
    whose price is too old for this session (``price_stale``) is not marked at all. Either way
    round, a figure that cannot be established leaves the result unknown, and an unknown result
    is not one inside the line.
    """
    split = positions.realized_by_version(fills=book.fills, proposals=book.proposals, bucket=ACCOUNT)
    realized_by = None
    if split is not None:
        realized_by = {"code": 0.0, "person": 0.0}
        for amounts in split.values():
            for closer, amount in amounts.items():
                realized_by[closer] += amount
    bucket = book.snap.buckets[ACCOUNT]
    views = {view.symbol: view for view in bucket.positions}
    unrealized, unpriced, stale = 0.0, [], []
    for held in book.managed:
        view = views.get(held.symbol)
        if not held.known or not held.cost_basis or view is None:
            unpriced.append(held.symbol)
            continue
        if price_stale(view.price_date, book.snap.trade_date):
            stale.append(held.symbol)
            continue
        unrealized += (view.price - held.cost_basis) * float(held.quantity or 0.0)
    line = -abs(float(book.plan["envelope"]["loss_pause_usd"]))
    # Measured from where the line last began: the plan's confirmation, or the last time a
    # person looked at a loss-line pause and resumed (``store.resume``).
    baseline = float(store.state(book.db).get("loss_baseline_usd") or 0.0)
    out = {"realized_usd": None, "realized_by": None, "unrealized_usd": round(unrealized, 2),
           "total_usd": None, "baseline_usd": baseline, "since_usd": None, "line_usd": line,
           "known": False, "reached": False, "unpriced": unpriced, "stale": stale}
    if realized_by is None:
        # The fills cannot be replayed, so what the plan's sales realized is not known at all.
        return out
    realized = sum(realized_by.values())
    total = round(realized + unrealized, 2)
    since = round(total - baseline, 2)
    out.update(realized_usd=round(realized, 2),
               realized_by={closer: round(amount, 2) for closer, amount in realized_by.items()},
               total_usd=total, since_usd=since, known=not unpriced and not stale,
               reached=since <= line)
    return out


# ---------------------------------------------------------------- one research run, decided once
def consider(service: Any, book: Book, record: dict[str, Any], run_dir: Path, *,
             blocked: list[str], hold: diagnosis.Reason | None = None) -> dict[str, Any]:
    """Decide one research run under the plan. Returns what came of it; writes at most a proposal.

    ``hold`` is why nothing new is being opened at the moment, when something is stopping it
    (:func:`opening_hold`). It stops an opening and nothing else: research that says to sell a
    position code holds is acted on whatever is holding purchases back.
    """
    profiles, plan = book.profiles, book.plan
    strategy = plan["strategy"]
    ticker = str(record["ticker"]).upper()
    policy = record.get("policy") or {}
    answer: dict[str, Any] = {"run_id": record["run_id"], "ticker": ticker, "proposal_id": None}

    def no(*reasons: diagnosis.Reason) -> dict[str, Any]:
        return {**answer, "diagnosis": _why(*reasons)}

    if ticker not in [str(s).upper() for s in strategy.get("universe") or []]:
        return no(diagnosis.Reason("not_in_plan"))
    if policy.get("error"):
        return no(diagnosis.Reason("policy_review_failed"))
    has_position = book.snap.has_position(ticker, ACCOUNT)
    instrument = str(record.get("instrument_type") or "etf")
    rating = str((record.get("upstream_decision") or {}).get("portfolio_manager_signal") or "")
    confidence = policy.get("confidence")
    action, size_hint, _reasons, codes = decide(
        rating, confidence=confidence, llm_action=policy.get("reviewer_action"), bucket=ACCOUNT,
        instrument_type=instrument, profiles=profiles, has_position=has_position)
    if action == "HOLD":
        return no(diagnosis.Reason("hold"))
    if action == "NO_TRADE":
        return no(*[diagnosis.Reason(str(item["code"]), {k: v for k, v in item.items() if k != "code"})
                    for item in codes] or [diagnosis.Reason("no_trade")])
    if blocked:
        return no(diagnosis.Reason("unresolved_order"))
    if in_progress(book, ticker, leaving=action in autonomy.EXIT_ACTIONS):
        return no(diagnosis.Reason("order_in_progress"))

    if action in ("REDUCE", "SELL"):
        held = next((view for view in book.managed if view.symbol == ticker), None)
        if held is None or not held.known or (held.quantity or 0.0) <= autonomy.QUANTITY_TOLERANCE:
            return no(diagnosis.Reason("not_managed"))
        quantity = float(held.quantity) * (1.0 if action == "SELL" else 0.5)
        made = _write_exit(book, held, action=action, quantity=quantity, run_id=record["run_id"],
                           why={"reason": "research", "rating": rating},
                           created_at=record.get("created_at"))
        return {**answer, **made}

    # --- an opening
    if is_baseline(strategy):
        # Under the baseline template research is a veto and never a reason to buy: what is
        # bought is the owner's weights, in tranches (``tranche_pass``; contract 2.1).
        return no(diagnosis.Reason("baseline_research_not_a_buy"))
    if hold is not None:
        return no(hold)
    if has_position:
        return no(diagnosis.Reason("already_held"))
    floor = int(strategy.get("min_confidence") or 0)
    if confidence is None or int(confidence) < floor:
        return no(diagnosis.Reason("confidence_below_floor",
                                   {"confidence": confidence, "floor": floor}))
    holding = sum(1 for view in book.managed
                  if view.known and (view.quantity or 0.0) > autonomy.QUANTITY_TOLERANCE)
    waiting = sum(1 for p in book.proposals if p.bucket == ACCOUNT and p.strategy_version
                  and p.action in autonomy.OPENING_ACTIONS and pending(book, p))
    most = int(strategy.get("max_positions") or 0)
    if holding + waiting >= most:
        return no(diagnosis.Reason("plan_positions_full", {"max": most}))
    # A strategy with a trend rule buys only what is above its own average. The rule times a
    # purchase research has already called for; it is never a reason to buy by itself, and
    # where the average cannot be computed the answer is "not known", which is not a yes.
    window = int(strategy.get("trend_window") or 0)
    if window > 0:
        seen = positions.trend(book.closes(ticker, window), window)
        if seen is None:
            return no(diagnosis.Reason("trend_unknown", {"window": window}))
        if not seen["above"]:
            return no(diagnosis.Reason("trend_not_confirmed", {
                "window": window, "close": f"{seen['close']:.2f}", "average": f"{seen['average']:.2f}"}))

    cls = classify_instrument(ticker, profiles, upstream_identity_lookup)
    proposal_doc = policy.get("proposal") or {}
    required = [ANALYST_REPORT[name] for name in profiles.analysts_for(cls.instrument_type)
                if name in ANALYST_REPORT]
    evidence = collect_evidence(ticker, str(record["trade_date"]), reports=_reports(run_dir),
                                required_reports=required, bucket=ACCOUNT,
                                known_buckets=tuple(book.snap.buckets),
                                llm_calls=record.get("llm_calls") or [])
    bucket = book.snap.buckets[ACCOUNT]
    equity = bucket.allocation_usd or bucket.equity
    envelope = plan["envelope"]
    weight = float(envelope["capital_usd"]) * float(strategy["position_pct"]) / 100.0 / equity * 100.0
    mode = modes.TABLE[envelope["mode"]]
    asked = RiskProposal(
        ticker=ticker, bucket=ACCOUNT, action=action, size_hint=size_hint, confidence=confidence,
        trade_date=str(record["trade_date"]), cls=cls,
        data_quality=proposal_doc.get("data_quality"),
        missing_information=list(proposal_doc.get("missing_information") or []),
        source="auto:signal", strategy=mode.strategy_tag, evidence=evidence,
        # Sized as what it is: an order code would place. The allowance and the macro blackout
        # both read this, and both must bind when the size is chosen, not only when it is sent.
        autonomous=True, weight_override_pct=weight)
    result = HardRiskEngine(profiles).evaluate(asked, book.snap, book.ladders, book.regime)
    hard = result.as_dict()
    if result.verdict != "APPROVE":
        return no(*diagnosis.classify_record(
            {"policy": {"final_action": action}, "hard_risk": hard,
             "final_recommendation": {"action": "NO_TRADE"}}).reasons)

    version = plan["opening_version"]
    proposal = new_proposal(
        run_id=record["run_id"], ticker=ticker, bucket=ACCOUNT, action=action,
        strategy=mode.strategy_tag, instrument_type=cls.instrument_type,
        limit_class=cls.limit_class, size_hint=size_hint, confidence=confidence,
        notional_usd=result.proposed_notional_usd,
        target_weight_pct=result.target_weight_pct_of_allocation,
        trade_date=str(record["trade_date"]),
        requires_human_approval=result.requires_human_approval,
        reference_price=evidence.get("price"), reference_price_date=evidence.get("price_date"),
        ladder_level_at_proposal=book.ladders.governing(ACCOUNT).level,
        regime_at_proposal=book.regime.level,
        portfolio_equity_at_proposal=book.snap.total_equity,
        bucket_cash_at_proposal=bucket.cash_usd,
        positions_fingerprint=positions_fingerprint(
            {p.symbol: p.quantity for p in book.snap.state.positions}),
        data_quality=proposal_doc.get("data_quality"), evidence=evidence,
        missing_information=list(proposal_doc.get("missing_information") or []),
        authority_version=autonomy.note_version(profiles, book.db),
        weight_override_pct=weight, strategy_version=version)
    # The research clock, not the moment this record was made: a proposal is as old as the
    # thinking behind it, and writing it down must not make stale research look fresh.
    if record.get("created_at"):
        proposal.created_at = str(record["created_at"])
    proposal.note("auto_signal", f"research run {record['run_id']} under plan {plan['plan_id']}, "
                                 f"strategy version {version}",
                  plan_id=plan["plan_id"], strategy_version=version, sizing=hard.get("sizing"))
    book.store.save(proposal)
    book.proposals.append(proposal)
    return {**answer, "proposal_id": proposal.proposal_id,
            "diagnosis": _why(diagnosis.Reason(
                "proposal_waiting" if proposal.requires_human_approval else "proposal_automatic",
                {"action": action}))}


def _write_exit(book: Book, held: positions.Managed, *, action: str, quantity: float,
                run_id: str | None, why: dict[str, Any],
                created_at: str | None = None) -> dict[str, Any]:
    """Write the proposal to sell some or all of what code holds of one position."""
    profiles = book.profiles
    cls = classify_instrument(held.symbol, profiles, upstream_identity_lookup)
    trade_date = book.snap.trade_date
    asked = RiskProposal(
        ticker=held.symbol, bucket=ACCOUNT, action=action, size_hint="none", confidence=None,
        trade_date=trade_date, cls=cls, source=autonomy.MANAGED_EXIT_SOURCE, strategy="de_risk",
        sell_quantity=exits.floor_to_increment(quantity))
    result = HardRiskEngine(profiles).evaluate(asked, book.snap, book.ladders, book.regime)
    if result.verdict != "APPROVE" or not result.proposed_quantity:
        return {"proposal_id": None, "diagnosis": _why(*diagnosis.classify_record(
            {"policy": {"final_action": action}, "hard_risk": result.as_dict(),
             "final_recommendation": {"action": "NO_TRADE"}}).reasons)}
    proposal = new_proposal(
        run_id=run_id, ticker=held.symbol, bucket=ACCOUNT, action=action, strategy="de_risk",
        instrument_type=cls.instrument_type, limit_class=cls.limit_class, size_hint="none",
        confidence=None, notional_usd=result.proposed_notional_usd,
        sell_quantity=result.proposed_quantity,
        target_weight_pct=result.target_weight_pct_of_allocation, trade_date=trade_date,
        requires_human_approval=result.requires_human_approval,
        # No analysis price and no book fingerprint. Both exist to stop an old idea being acted
        # on in a changed world; an exit decided this minute has no old world, and a price that
        # has fallen since is the reason for a stop, not a reason to hold the order back.
        reference_price=None, positions_fingerprint=None,
        ladder_level_at_proposal=book.ladders.governing(ACCOUNT).level,
        regime_at_proposal=book.regime.level,
        portfolio_equity_at_proposal=book.snap.total_equity,
        bucket_cash_at_proposal=book.snap.buckets[ACCOUNT].cash_usd,
        authority_version=autonomy.note_version(profiles, book.db),
        strategy_version=held.version_id)
    if created_at:
        proposal.created_at = str(created_at)
    proposal.note("auto_exit", f"exit of {result.proposed_quantity:g} {held.symbol} held under "
                               f"{held.version_id}: {why.get('reason')}",
                  strategy_version=held.version_id, managed_quantity=held.quantity, **why)
    book.store.save(proposal)
    book.proposals.append(proposal)
    return {"proposal_id": proposal.proposal_id,
            "diagnosis": _why(diagnosis.Reason(
                "proposal_waiting" if proposal.requires_human_approval else "proposal_automatic",
                {"action": action}))}


# ---------------------------------------------------------------- the exits a position is owed
def exit_pass(book: Book, *, today, blocked: list[str]) -> list[dict[str, Any]]:
    """For each position code holds, whether its own strategy says to leave, and the proposal if so."""
    out = []
    views = {view.symbol: view for view in book.snap.buckets[ACCOUNT].positions}
    for held in book.managed:
        version = store.version(book.db, held.version_id)
        # The trend rule of the version that opened it, if it has one, read on finished closes.
        window = int(((version or {}).get("strategy") or {}).get("trend_window") or 0)
        view = views.get(held.symbol)
        priced_on = view.price_date if view is not None else None
        call = positions.exit_call(
            held, version, price=view.price if view is not None else None, today=today,
            closes=book.closes(held.symbol, window) if window > 0 else None, price_date=priced_on,
            stale=view is not None and price_stale(priced_on, book.snap.trade_date))
        row = {"symbol": held.symbol, **held.as_dict(), "exit": call.as_dict(), "proposal_id": None}
        if call.due:
            if blocked:
                row["diagnosis"] = _why(diagnosis.Reason("unresolved_order"))
                out.append(row)
                continue
            row["withdrawn"] = withdraw_moved_exits(book, held.symbol)
            if in_progress(book, held.symbol, leaving=True):
                row["diagnosis"] = _why(diagnosis.Reason("order_in_progress"))
            else:
                row.update(_write_exit(book, held, action="SELL", quantity=float(held.quantity),
                                       run_id=None, why={"reason": call.reason, "facts": call.facts}))
        out.append(row)
    return out


def remember_undecided(book: Book, rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Keep, by symbol, since which session a position's exit could not be decided.

    A position whose exit was decided again, or that is no longer held, is dropped, so what is
    kept is always an unbroken run of sessions. Written only when that changes: a pass a minute
    costs one read while nothing moves. This is what Home reads (``auto_exit_undecided``): a stop
    that cannot be read for more than a session is a person's to look at, not something to wait
    out (docs/AUTO_BASELINE_CONTRACT_2026-10-15.md, 2.4 and 3.4).
    """
    session = book.snap.trade_date
    with book.db.transaction() as conn:
        before = dict(book.db.read_meta(UNDECIDED, {}) or {})
        now: dict[str, dict[str, Any]] = {}
        for row in rows:
            call = row.get("exit") or {}
            if call.get("due") or not call.get("undecided"):
                continue
            was = before.get(row["symbol"]) or {}
            now[row["symbol"]] = {
                "since": was.get("since") or session, "last": session,
                "code": call["undecided"] if call["undecided"] == positions.PRICE_STALE else "undecided",
                "detail": call["undecided"], "price_date": call.get("price_date"),
                "text": call.get("text"), "strategy_version": row.get("strategy_version")}
        if now != before:
            book.db.write_meta(UNDECIDED, now, conn)
    return now


def undecided_exits(db) -> dict[str, dict[str, Any]]:
    """The positions whose exit could not be decided on the last pass, by symbol."""
    return dict(db.read_meta(UNDECIDED, {}) or {})


# ---------------------------------------------------------------- the baseline template's tranches
#: What vetoes a tranche (D2): research saying hold less of it. A rating, or the reviewer's action.
NEGATIVE_RATINGS = ("underweight", "sell")
NEGATIVE_REVIEWS = ("SELL", "REDUCE")
#: The regimes a tranche may be bought in (D3). A tense one, a high-risk one and one nobody could
#: read all skip it -- the configuration says HIGH_RISK, and the owner added ELEVATED.
OPEN_REGIMES = ("LOW_RISK", "NORMAL")
#: How often one tranche is written in one session at most, and how far apart: a tranche turned
#: back at the venue is tried again, but not every minute for the rest of the day.
TRANCHE_TRIES_PER_SESSION = 4
#: The sessions a position of the simulated account is held before its drill (D6).
DRILL_AFTER_SESSIONS = 5


def is_baseline(strategy: dict[str, Any] | None) -> bool:
    """Whether a strategy is the baseline template's: bought by weights, research a veto."""
    return str((strategy or {}).get("template") or "") == autonomy.BASELINE_TEMPLATE


def is_tranche(proposal: Any) -> bool:
    """Whether a proposal is one this module wrote as a baseline tranche."""
    return (str((getattr(proposal, "evidence", None) or {}).get("purpose") or "") == autonomy.TRANCHE_PURPOSE
            and any(isinstance(entry, dict) and entry.get("event") == "auto_tranche"
                    for entry in getattr(proposal, "history", None) or ()))


def sessions_after(day: str, count: int) -> str:
    """The session ``count`` sessions after the one ``day`` falls in, or the next if it is none.

    By the exchange's calendar, so a holiday is not a session; by weekdays without it.
    """
    import pandas as pd

    calendar = _exchange_calendar()
    if calendar is not None:
        try:
            session = calendar.date_to_session(str(day)[:10], direction="next")
            return calendar.session_offset(session, int(count)).date().isoformat()
        except Exception as exc:  # noqa: BLE001 - outside the calendar's years: weekdays instead
            logger.debug("%s is outside the exchange calendar: %s", day, exc)
    return (pd.Timestamp(str(day)[:10]) + pd.offsets.BDay(int(count))).date().isoformat()


def research_by_fund(records: list[tuple[dict[str, Any], Path]], *, window_days: int,
                     now: datetime | None = None) -> dict[str, dict[str, Any]]:
    """The newest finished research about each fund within the window, by symbol.

    A record with no rating says nothing either way and is passed over. A fund with none in the
    window is absent, which the tranche reads as "not researched": nobody looked, so nothing
    is bought (D2).
    """
    out: dict[str, dict[str, Any]] = {}
    newest: dict[str, float] = {}
    for record, _run_dir in records:
        symbol = str(record.get("ticker") or "").upper()
        age = _age_seconds(record.get("created_at"), now)
        if not symbol or age is None or age > window_days * 86400.0:
            continue
        # By the research's own clock: files written in the same second come back in no order.
        if symbol in newest and age >= newest[symbol]:
            continue
        rating = str((record.get("upstream_decision") or {}).get("portfolio_manager_signal") or "")
        if not rating:
            continue
        review = str((record.get("policy") or {}).get("reviewer_action") or "") or None
        newest[symbol] = age
        out[symbol] = {"run_id": record.get("run_id"), "rating": rating, "reviewer_action": review,
                       "at": record.get("created_at"),
                       "negative": rating.strip().lower() in NEGATIVE_RATINGS
                       or (review or "").upper() in NEGATIVE_REVIEWS}
    return out


def _veto(seen: dict[str, Any]) -> tuple[dict[str, Any], diagnosis.Reason]:
    rating = str(seen.get("rating") or "")
    said = rating if rating.strip().lower() in NEGATIVE_RATINGS else str(seen.get("reviewer_action") or rating)
    reason = diagnosis.Reason("tranche_vetoed", {"day": str(seen.get("at") or "")[5:10], "said": said})
    return ({"code": "research_negative", "run_id": seen.get("run_id"), "rating": rating,
             "reviewer_action": seen.get("reviewer_action"), "at": seen.get("at"),
             "text": reason.text()}, reason)


def _filled(proposal: Any) -> bool:
    return proposal.status == EXECUTED or (proposal.status == CANCELED
                                           and float(proposal.filled_quantity or 0.0) > 0)


def _day_of(stamp: Any) -> str | None:
    try:
        made = datetime.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        return None
    if made.tzinfo is None:
        made = made.replace(tzinfo=UTC)
    from zoneinfo import ZoneInfo

    return made.astimezone(ZoneInfo("America/New_York")).date().isoformat()


def _ended(book: Book, symbol: str) -> dict[str, Any] | None:
    """The last time a plan's position in this fund came to an end: when, and by what sale."""
    lives = [row for row in positions.closed_trades(fills=book.fills, proposals=book.proposals,
                                                    bucket=ACCOUNT) if row["symbol"] == symbol]
    if not lives:
        return None
    last = lives[-1]
    closing = next((p for p in book.proposals if p.proposal_id == last.get("closing_proposal")), None)
    drill = closing is not None and any(
        isinstance(entry, dict) and entry.get("event") == "auto_exit" and entry.get("reason") == positions.DRILL
        for entry in closing.history)
    stop = closing is not None and any(
        isinstance(entry, dict) and entry.get("event") == "auto_exit" and entry.get("reason") == positions.STOP_LOSS
        for entry in closing.history)
    return {"at": last.get("closed_at"), "day": _day_of(last.get("closed_at")), "drill": drill,
            "stop": stop, "closed_by": last.get("closed_by")}


def tranche_facts(book: Book, plan_id: str, symbol: str) -> dict[str, Any]:
    """Where one fund's tranches stand, read from the records: nothing about them is kept apart.

    How many have filled since the plan's position in it last began, which is waiting, and how
    its last position ended. A restart, a second process, or a tranche that expired unfilled can
    neither add one nor lose one (contract 3.6).
    """
    mine = [p for p in book.proposals if p.bucket == ACCOUNT and p.ticker == symbol
            and is_tranche(p) and autonomy.plan_of(p) == plan_id]
    ended = _ended(book, symbol)
    since = ended["at"] if ended else None

    def after_end(proposal: Any) -> bool:
        if since is None:
            return True
        at = _age_seconds(proposal.executed_at or proposal.created_at)
        end = _age_seconds(since)
        return at is not None and end is not None and at < end

    filled = [p for p in mine if _filled(p) and after_end(p)]
    waiting = next((p for p in mine if pending(book, p)), None)
    held = next((view for view in book.managed if view.symbol == symbol), None)
    price = next((view.price for view in book.snap.buckets[ACCOUNT].positions if view.symbol == symbol), None)
    held_usd = None
    if held is None:
        held_usd = 0.0
    elif held.known and price is not None:
        held_usd = round(float(held.quantity or 0.0) * float(price), 2)
    today = book.snap.trade_date
    tries = [p for p in mine if p.trade_date == today and not _filled(p) and not pending(book, p)]
    return {"done": len(filled), "waiting": waiting, "held_usd": held_usd, "ended": ended,
            "last_fill_day": max((_day_of(p.executed_at or p.created_at) or "" for p in filled), default=None)
            or None,
            "tried_today": len(tries), "last_try_at": max((p.created_at for p in tries), default=None)}


def tranche_rows(deployment: dict[str, Any], *, facts: dict[str, dict[str, Any]],
                 research: dict[str, dict[str, Any]], gate: dict[str, Any] | None,
                 trade_date: str) -> list[dict[str, Any]]:
    """Each fund of a baseline plan as the console shows it: its target, its progress, its status.

    ``facts`` is ``tranche_facts`` by symbol (empty for a plan not yet started). ``gate`` is why
    the market says not now -- the regime, the ladder or a release -- or None. The status set is
    closed (contract 3.1): due, waiting, vetoed, not_researched, regime_gate, cooling_down,
    below_minimum, position_limit, complete.
    """
    rules = deployment.get("rules") or {}
    rows = []
    for target in deployment.get("targets") or []:
        symbol = str(target["symbol"]).upper()
        of = max(1, int(target["tranches"]))
        target_usd = float(target["target_usd"])
        tranche_usd = sizing.floor_cents(target_usd / of)
        found = facts.get(symbol) or {"done": 0, "waiting": None, "held_usd": 0.0, "ended": None,
                                      "last_fill_day": None, "tried_today": 0}
        done = min(of, int(found["done"]))
        row: dict[str, Any] = {"symbol": symbol, "baseline_pct": target.get("baseline_pct"),
                               "target_usd": target_usd, "held_usd": found.get("held_usd"),
                               "tranches": {"planned": of, "done": done}, "next": None,
                               "status": "due", "veto": None}
        nxt = {"index": done + 1, "of": of, "usd": tranche_usd}
        ended = found.get("ended")
        cooling_until = (sessions_after(ended["day"], int(rules.get("after_stop_cooldown_sessions") or 0))
                         if ended and ended.get("day") and not ended.get("drill") and done == 0 else None)
        spaced_from = found.get("last_fill_day") or (ended["day"] if ended and ended.get("drill") and done == 0
                                                      else None)
        spacing_until = (sessions_after(spaced_from, int(rules.get("spacing_sessions") or 0))
                         if spaced_from else None)
        seen = research.get(symbol)
        # What stands in the way comes before a tranche already written: one that waits while its
        # fund is now cooling, gated, vetoed or unresearched no longer stands (``tranche_pass``
        # withdraws it). The spacing it was written under it had already met.
        if done >= of:
            status, due, reason = "complete", None, diagnosis.Reason("tranche_complete", {"of": of})
        elif cooling_until and trade_date < cooling_until:
            status, due = "cooling_down", cooling_until
            reason = diagnosis.Reason("tranche_cooling_down", {
                "sessions": rules.get("after_stop_cooldown_sessions"), "due": cooling_until[5:]})
        elif gate is not None:
            status, due, reason = "regime_gate", None, diagnosis.Reason("tranche_regime_gate", gate)
        elif seen is not None and seen.get("negative"):
            veto, reason = _veto(seen)
            status, due = "vetoed", None
            row["veto"] = veto
        elif seen is None:
            status, due = "not_researched", None
            reason = diagnosis.Reason("tranche_not_researched", {"days": rules.get("veto_window_days")})
        elif found.get("waiting") is not None:
            status, due = "due", trade_date
            nxt["proposal_id"] = found["waiting"].proposal_id
            reason = diagnosis.Reason("tranche_written", {"index": done + 1, "of": of,
                                                          "usd": f"{tranche_usd:,.2f}"})
        elif spacing_until and trade_date < spacing_until:
            status, due = "waiting", spacing_until
            reason = diagnosis.Reason("tranche_not_due", {"due": spacing_until[5:],
                                                          "spacing": rules.get("spacing_sessions")})
        else:
            status, due = "due", trade_date
            reason = diagnosis.Reason("tranche_due", {"index": done + 1, "of": of,
                                                      "usd": f"{tranche_usd:,.2f}"})
        row.update(status=status, next=None if status == "complete" else {**nxt, "due": due},
                   why=reason.text(), diagnosis=_why(reason))
        rows.append(row)
    for left in deployment.get("left_out") or []:
        code = str(left.get("code") or "tranche_below_minimum")
        reason = diagnosis.Reason(code, {
            "usd": f"{float(left['share_usd']):,.2f}" if left.get("share_usd") is not None else None,
            "minimum": f"{float(deployment.get('minimum_order_usd') or 0):,.2f}",
            "max": deployment.get("max_positions")})
        rows.append({"symbol": left["symbol"], "baseline_pct": left.get("baseline_pct"),
                     "target_usd": 0.0, "held_usd": (facts.get(left["symbol"]) or {}).get("held_usd", 0.0),
                     "tranches": {"planned": 0, "done": 0}, "next": None, "veto": None,
                     "status": "position_limit" if code == "tranche_position_limit" else "below_minimum",
                     "why": reason.text(), "diagnosis": _why(reason)})
    return rows


def market_gate(book: Book) -> dict[str, Any] | None:
    """Why the market says no tranche today, or None: the regime, the ladder, a release (D3)."""
    if book.regime.level not in OPEN_REGIMES:
        return {"gate": "regime", "level": book.regime.level}
    ladder = book.ladders.governing(ACCOUNT).level
    if ladder != "NORMAL":
        return {"gate": "ladder", "level": ladder}
    blocked, detail = macro_blackout(book.profiles, book.snap.trade_date, autonomous=True)
    if blocked:
        return {"gate": "macro", "detail": str(detail)[:200]}
    return None


def _block(deployment: dict[str, Any], rows: list[dict[str, Any]], *, regime: dict[str, Any],
           as_of: str | None, plan_id: str | None = None, version: str | None = None) -> dict[str, Any]:
    rules = deployment.get("rules") or {}
    return {"template": autonomy.BASELINE_TEMPLATE, "plan_id": plan_id, "version": version,
            "rules": {"tranches": rules.get("tranches"), "spacing_sessions": rules.get("spacing_sessions"),
                      "veto_window_days": rules.get("veto_window_days"),
                      "minimum_order_usd": deployment.get("minimum_order_usd"),
                      "regime_skips": rules.get("regime_skips"),
                      "after_stop_cooldown_sessions": rules.get("after_stop_cooldown_sessions")},
            "targets": rows, "cash_left_usd": deployment.get("cash_left_usd"),
            "regime": regime, "as_of": as_of, "error": None}


def deployment_view(book: Book, version: dict[str, Any] | None,
                    research: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    """A baseline plan's ``deployment`` block (contract 3.1), or None for any other plan."""
    strategy = (version or {}).get("strategy") or {}
    if book.plan is None or not is_baseline(strategy):
        return None
    deployment = strategy.get("deployment") or {}
    plan_id = book.plan["plan_id"]
    facts = {str(row["symbol"]).upper(): tranche_facts(book, plan_id, str(row["symbol"]).upper())
             for row in deployment.get("targets") or []}
    gate = market_gate(book)
    return _block(deployment, tranche_rows(deployment, facts=facts, research=research, gate=gate,
                                           trade_date=book.snap.trade_date),
                  regime={"level": book.regime.level, "ladder": book.ladders.governing(ACCOUNT).level,
                          "open": gate is None},
                  as_of=book.snap.trade_date, plan_id=plan_id, version=(version or {}).get("version_id"))


def preview_view(deployment: dict[str, Any], *, research: dict[str, dict[str, Any]],
                 trade_date: str) -> dict[str, Any]:
    """The block for a plan nothing has been bought under yet: a preview, or a plan not yet run.

    What research has said is read; the market is not -- a preview prices the account and
    nothing else, so ``regime`` is unknown rather than guessed, and is the first pass's to say.
    """
    return _block(deployment, tranche_rows(deployment, facts={}, research=research, gate=None,
                                           trade_date=trade_date),
                  regime={"level": None, "ladder": None, "open": None}, as_of=None)


def _write_tranche(book: Book, version_id: str, row: dict[str, Any]) -> dict[str, Any]:
    """Write one tranche's proposal, sized by the plan and held to every limit by the engine."""
    profiles, plan = book.profiles, book.plan
    symbol = row["symbol"]
    index, of, usd = int(row["next"]["index"]), int(row["next"]["of"]), float(row["next"]["usd"])
    trade_date = book.snap.trade_date
    cls = classify_instrument(symbol, profiles, upstream_identity_lookup)
    evidence = collect_evidence(symbol, trade_date, bucket=ACCOUNT, known_buckets=tuple(book.snap.buckets))
    evidence.update(purpose=autonomy.TRANCHE_PURPOSE,
                    tranche={"plan_id": plan["plan_id"], "index": index, "of": of,
                             "target_usd": row["target_usd"]})
    bucket = book.snap.buckets[ACCOUNT]
    equity = bucket.allocation_usd or bucket.equity
    # The engine sizes a position to a target weight, less what is held: the tranche on top of
    # whatever the holding is now, a person's shares included, so the caps see all of it.
    weight = (bucket.market_value_of(symbol) + usd) / equity * 100.0 if equity else 0.0
    action = "ADD" if book.snap.has_position(symbol, ACCOUNT) else "BUY"
    mode = modes.TABLE[plan["envelope"]["mode"]]
    asked = RiskProposal(
        ticker=symbol, bucket=ACCOUNT, action=action, size_hint="normal", confidence=None,
        trade_date=trade_date, cls=cls, data_quality=None, missing_information=[],
        source="auto:tranche", strategy=mode.strategy_tag, evidence=evidence, autonomous=True,
        weight_override_pct=weight)
    result = HardRiskEngine(profiles).evaluate(asked, book.snap, book.ladders, book.regime)
    hard = result.as_dict()
    if result.verdict != "APPROVE":
        return {"proposal_id": None, "diagnosis": _why(*diagnosis.classify_record(
            {"policy": {"final_action": action}, "hard_risk": hard,
             "final_recommendation": {"action": "NO_TRADE"}}).reasons)}
    proposal = new_proposal(
        run_id=None, ticker=symbol, bucket=ACCOUNT, action=action, strategy=mode.strategy_tag,
        instrument_type=cls.instrument_type, limit_class=cls.limit_class, size_hint="normal",
        confidence=None, notional_usd=result.proposed_notional_usd,
        target_weight_pct=result.target_weight_pct_of_allocation, trade_date=trade_date,
        requires_human_approval=result.requires_human_approval,
        reference_price=evidence.get("price"), reference_price_date=evidence.get("price_date"),
        ladder_level_at_proposal=book.ladders.governing(ACCOUNT).level,
        regime_at_proposal=book.regime.level,
        portfolio_equity_at_proposal=book.snap.total_equity,
        bucket_cash_at_proposal=bucket.cash_usd,
        positions_fingerprint=positions_fingerprint(
            {p.symbol: p.quantity for p in book.snap.state.positions}),
        data_quality=None, evidence=evidence, missing_information=[],
        authority_version=autonomy.note_version(profiles, book.db),
        weight_override_pct=weight, strategy_version=version_id)
    proposal.note("auto_tranche", f"tranche {index} of {of} of {symbol} under plan {plan['plan_id']}, "
                                  f"toward ${float(row['target_usd']):,.2f}",
                  plan_id=plan["plan_id"], strategy_version=version_id, index=index, of=of,
                  target_usd=row["target_usd"], sizing=hard.get("sizing"))
    book.store.save(proposal)
    book.proposals.append(proposal)
    return {"proposal_id": proposal.proposal_id, "diagnosis": _why(diagnosis.Reason(
        "tranche_written", {"index": index, "of": of, "usd": f"{proposal.notional_usd:,.2f}"}))}


def _withdraw_tranche(book: Book, proposal: Any, why: str) -> bool:
    """Withdraw a tranche nobody has acted on, when what it was written under no longer holds."""
    with book.store.locked():
        now = book.store.get(proposal.proposal_id)
        if (now is None or now.status != PROPOSED or now.client_order_id or now.approval_request_id):
            return False                          # a person, or the pass, reached it first
        now.status = INVALIDATED
        now.note("auto_tranche_withdrawn", why)
        book.store.save(now)
        book.proposals[:] = [now if p.proposal_id == now.proposal_id else p for p in book.proposals]
    return True


#: The statuses under which a tranche already written no longer stands.
WITHDRAWN_UNDER = ("vetoed", "regime_gate", "not_researched", "cooling_down")


def tranche_pass(book: Book, version: dict[str, Any] | None, research: dict[str, dict[str, Any]], *,
                 blocked: list[str], hold: diagnosis.Reason | None) -> tuple[list[dict[str, Any]],
                                                                             dict[str, Any] | None]:
    """A baseline plan's tranches for this pass: the rows run records show, and the view.

    One tranche at most is written per fund per pass, only where the fund is due and nothing for
    it is waiting, and the engine decides its size. One that waits while its fund became vetoed,
    gated or cooling is withdrawn rather than left to be bought at the open. What stops openings
    -- a pause, an unknown result, an unresolved order -- stops these too.
    """
    view = deployment_view(book, version, research)
    if view is None:
        return [], None
    plan_id, version_id = book.plan["plan_id"], (version or {}).get("version_id")
    out = []
    for row in view["targets"]:
        if row["status"] in ("below_minimum", "position_limit", "complete"):
            continue
        signal: dict[str, Any] = {"kind": "tranche", "run_id": None, "ticker": row["symbol"],
                                  "proposal_id": None, "status": row["status"],
                                  "tranche": None if row["next"] is None else {
                                      "index": row["next"]["index"], "of": row["next"]["of"]}}
        facts = tranche_facts(book, plan_id, row["symbol"])
        waiting = facts.get("waiting")
        if waiting is not None and row["status"] == "due":
            signal.update(proposal_id=waiting.proposal_id, diagnosis=_why(diagnosis.Reason(
                "tranche_written", {"index": row["next"]["index"], "of": row["next"]["of"],
                                    "usd": f"{waiting.notional_usd:,.2f}"})))
        elif row["status"] != "due":
            if row["status"] in WITHDRAWN_UNDER:
                for proposal in list(book.proposals):
                    if (proposal.ticker == row["symbol"] and is_tranche(proposal)
                            and autonomy.plan_of(proposal) == plan_id and pending(book, proposal)
                            and proposal.status == PROPOSED):
                        _withdraw_tranche(book, proposal, f"no longer due: {row['status']}")
            signal["diagnosis"] = row["diagnosis"]
        elif blocked:
            signal["diagnosis"] = _why(diagnosis.Reason("unresolved_order"))
        elif hold is not None:
            signal["diagnosis"] = _why(hold)
        elif in_progress(book, row["symbol"]):
            signal["diagnosis"] = _why(diagnosis.Reason("order_in_progress"))
        elif facts["tried_today"] >= TRANCHE_TRIES_PER_SESSION or (
                facts.get("last_try_at") and (_age_seconds(facts["last_try_at"]) or 0.0) < RETRY_AFTER_MINUTES * 60.0):
            signal["diagnosis"] = _why(diagnosis.Reason("tranche_retry_later", {"tries": facts["tried_today"]}))
        else:
            signal.update(_write_tranche(book, version_id, row))
        _remember(book.db, f"tranche:{plan_id}:{row['symbol']}",
                  {"kind": "tranche", "ticker": row["symbol"], "proposal_id": signal.get("proposal_id"),
                   "diagnosis": signal.get("diagnosis")}, final=False)
        out.append(signal)
    return out, view


def remember_deployment(db, view: dict[str, Any] | None) -> None:
    """Keep the last pass's view for the console, which must not price an account to show it."""
    if view is None:
        if db.read_meta(DEPLOYMENT, None) is not None:
            db.write_meta(DEPLOYMENT, None)
        return
    before = db.read_meta(DEPLOYMENT, None) or {}
    stamped = {**view, "at": datetime.now(UTC).isoformat()}
    if {k: v for k, v in before.items() if k != "at"} != view:
        db.write_meta(DEPLOYMENT, stamped)


def last_deployment(db) -> dict[str, Any] | None:
    found = db.read_meta(DEPLOYMENT, None)
    return dict(found) if isinstance(found, dict) else None


def drill_pass(book: Book, *, blocked: list[str]) -> list[dict[str, Any]]:
    """The simulated account's one sale of a baseline position, so its exit is seen to work (D6).

    Only in the simulated account, only under a baseline plan, only once a version has no sale
    of its own on record, and only once a position has been held ``DRILL_AFTER_SESSIONS``. The
    sale is an ordinary exit of code's shares, approved by code like any other; after it the
    fund is bought back by the tranche rules. The real account never drills.
    """
    if book.plan is None or not paper.is_paper(book.profiles) or not is_baseline(book.plan.get("strategy")):
        return []
    version_id = book.plan["opening_version"]
    lives = positions.closed_trades(fills=book.fills, proposals=book.proposals, bucket=ACCOUNT)
    if any(row["version"] == version_id and row["closed_by"] == "code" for row in lives) or blocked:
        return []
    out = []
    for held in book.managed:
        if (held.version_id != version_id or not held.known
                or (held.quantity or 0.0) <= autonomy.QUANTITY_TOLERANCE or not held.opened_at):
            continue
        opened = _day_of(held.opened_at)
        if not opened or book.snap.trade_date < sessions_after(opened, DRILL_AFTER_SESSIONS):
            continue
        if in_progress(book, held.symbol, leaving=True):
            continue
        call = positions.ExitCall(True, positions.DRILL, {"sessions": DRILL_AFTER_SESSIONS})
        row = {"symbol": held.symbol, **held.as_dict(), "exit": call.as_dict(), "proposal_id": None}
        row.update(_write_exit(book, held, action="SELL", quantity=float(held.quantity), run_id=None,
                               why={"reason": positions.DRILL, "facts": call.facts}))
        out.append(row)
        break                                   # one drill is all the record needs
    return out


# ---------------------------------------------------------------- one pass
def tick(service: Any, *, runs_dir: Path | None = None, trade_date: str, today=None,
         approve: bool = True) -> Tick:
    """One pass over the account ``service`` is for: exits first, then new research, then approval.

    Exits first, because a position that should be left is owed that before anything is added.
    ``runs_dir`` is where research is read from; absent, the account's own. ``approve`` hands
    the proposals that need no person to ``Service.autonomous_reductions``, which is the only
    thing in this pass that can lead to an order, and is not in this module.
    """
    profiles = service.profiles()
    db = service.state_db(profiles)
    plan = store.active_plan(db)
    if plan is None and not store.versions(db):
        # Nothing was ever confirmed here, so there is nothing of code's to look after either.
        # Decided on two small reads: this is the answer on every pass of an account that has
        # never had a plan, and it must not cost a priced book each time.
        return Tick(ran=False, skipped="no plan is in force")
    today = today or datetime.now(UTC).date()
    book = read_book(service, profiles, plan, trade_date)
    blocked = unresolved(book)
    result = Tick(account=account_figures(book), health=health(book))
    if plan is None:
        # Withdrawn. Nothing is opened and no research is read, but what the plan bought is
        # still code's to close on the terms it was bought under: a position does not stop
        # needing its stop because the plan that opened it was ended.
        result.skipped = "no plan is in force; the positions it opened are still looked after"
        result.exits = exit_pass(book, today=today, blocked=blocked)
        remember_undecided(book, result.exits)
        if approve and result.exits:
            result.approvals = service.autonomous_reductions()
        return result

    result.loss = plan_loss(book)
    state = store.state(db)
    if result.loss["reached"] and not state["paused"]:
        state = store.pause(db, kind=store.LOSS_LINE, by="auto:loss_line", reason=(
            f"the plan is down ${abs(result.loss['since_usd']):,.2f}, at its loss line of "
            f"${abs(result.loss['line_usd']):,.2f}"))
    paused = state if state["paused"] else None
    result.paused = paused and {"reason": paused["reason"], "kind": paused.get("kind"),
                                "at": paused["at"]}

    result.exits = exit_pass(book, today=today, blocked=blocked)
    remember_undecided(book, result.exits)

    seen = seen_signals(db)
    hold = opening_hold(profiles, paused, result.loss)
    settle = settle_seconds(profiles)
    source = Path(runs_dir) if runs_dir is not None else profiles.project_dir / "runs"
    records = records_in(source)
    for record, run_dir in reversed(records):                     # oldest first
        before = seen.get(record["run_id"]) or {}
        retries = int(before.get("retries") or 0)
        if before and before.get("final", True):
            if not retry_due(book, before):
                continue
            retries += 1
        answer = {"run_id": record["run_id"], "ticker": str(record["ticker"]).upper(),
                  "proposal_id": None}
        age = _age_seconds(record.get("created_at"))
        if _expired(profiles, record, modes.TABLE[plan["envelope"]["mode"]].strategy_tag):
            outcome = {**answer, "diagnosis": _why(diagnosis.Reason("signal_expired"))}
        elif age is not None and age < settle:
            outcome = {**answer, "diagnosis": _why(diagnosis.Reason(
                "price_not_after_research", {"minutes": max(1, round((settle - age) / 60.0))}))}
        else:
            outcome = consider(service, book, record, run_dir, blocked=blocked, hold=hold)
        # A reason about the moment is not the run's answer: it is looked at again next pass
        # rather than written off, for as long as the research is still fresh.
        _remember(db, record["run_id"],
                  {**{k: outcome[k] for k in ("ticker", "proposal_id", "diagnosis")}, "retries": retries},
                  final=is_final(outcome))
        result.signals.append({**outcome, "retry": retries} if retries else outcome)

    # A baseline plan's own purchases, after research has had its say: a veto read this pass
    # applies to this pass's tranches (contract 2.1).
    version = store.version(db, plan["opening_version"])
    window = int((((version or {}).get("strategy") or {}).get("deployment") or {}).get("rules", {})
                 .get("veto_window_days") or 14)
    tranches, view = tranche_pass(book, version, research_by_fund(records, window_days=window),
                                  blocked=blocked, hold=hold)
    result.signals.extend(tranches)
    remember_deployment(db, view)
    result.exits.extend(drill_pass(book, blocked=blocked))

    if approve:
        result.approvals = service.autonomous_reductions()
    return result


def recorded_conclusions(profiles: Any, runs_dir: Path, *, limit: int = 2000) -> list[dict[str, Any]]:
    """What research recorded live concluded, as dated conclusions a replay can act on.

    Each run is put to the same deterministic mapping a live pass puts it to (``policy.decide``)
    and nothing else is decided here: whether a purchase fits the strategy, or whether anything
    is held to sell, is the replay's question. A run that concluded nothing actionable is
    returned too, with ``action`` None -- the count of those is part of what a record says.
    """
    out = []
    for record, _run_dir in records_in(runs_dir, limit=limit):
        policy = record.get("policy") or {}
        if policy.get("error"):
            continue
        rating = str((record.get("upstream_decision") or {}).get("portfolio_manager_signal") or "")
        common = {"confidence": policy.get("confidence"), "llm_action": policy.get("reviewer_action"),
                  "bucket": ACCOUNT, "instrument_type": str(record.get("instrument_type") or "etf"),
                  "profiles": profiles}
        unheld = decide(rating, has_position=False, **common)[0]
        held = decide(rating, has_position=True, **common)[0]
        action = (unheld if unheld in autonomy.OPENING_ACTIONS
                  else held if held in autonomy.EXIT_ACTIONS else None)
        out.append({"run_id": record["run_id"], "at": record.get("created_at"),
                    "symbol": str(record["ticker"]).upper(), "action": action,
                    "confidence": policy.get("confidence")})
    return sorted(out, key=lambda row: str(row["at"] or ""))


class TraderError(RuntimeError):
    """Something a person asked of the plan could not be done, and why."""


def result_now(service: Any, *, trade_date: str) -> dict[str, Any] | None:
    """What automatic trading in this account has made or lost as of now; None with no plan."""
    profiles = service.profiles()
    plan = store.active_plan(service.state_db(profiles))
    if plan is None:
        return None
    return plan_loss(read_book(service, profiles, plan, trade_date))


def resume(service: Any, *, note: str, by: str, trade_date: str) -> dict[str, Any]:
    """A person allows new positions again.

    Out of a loss-line pause, the line starts being measured from the result as it stands now
    (``store.resume``), so that result has to be one that can be read. If a position cannot be
    valued it cannot, and the pause stays: resuming on a number nobody could establish would
    move the line to somewhere nobody chose.
    """
    profiles = service.profiles()
    db = service.state_db(profiles)
    current = store.state(db)
    total = None
    if current.get("kind") == store.LOSS_LINE:
        loss = result_now(service, trade_date=trade_date)
        if loss is not None:
            if not loss["known"]:
                missing = ([f"{', '.join(loss['unpriced'])} cannot be valued"] if loss["unpriced"] else []) + (
                    [f"{', '.join(loss['stale'])} is priced too long ago"] if loss["stale"] else []) + (
                    ["the record of fills cannot be replayed"] if loss["realized_usd"] is None else [])
                raise TraderError(
                    "the plan was paused at its loss line and what it has lost cannot be worked "
                    f"out right now ({'; '.join(missing)}), so there is nowhere to measure the "
                    "line from; it stays paused")
            total = loss["total_usd"]
    return store.resume(db, note=note, by=by, result_usd=total)


def _expired(profiles: Any, record: dict[str, Any], strategy_tag: str = "long_only_diversified") -> bool:
    """Whether the research is past the lifetime a proposal made from it would have.

    The lifetime is the freshness class of the strategy tag the plan's proposals carry, read
    where a proposal's own freshness is read, so a signal is never kept alive longer here than
    the proposal written from it could be approved.
    """
    from ..proposals import freshness_class_of

    try:
        made = datetime.fromisoformat(str(record.get("created_at")))
    except (TypeError, ValueError):
        return True
    if made.tzinfo is None:
        made = made.replace(tzinfo=UTC)
    classes = (profiles.risk.get("proposal_freshness") or {}).get("classes") or {}
    ttl = float((classes.get(freshness_class_of(profiles, strategy_tag)) or {})
                .get("ttl_minutes", 2880))
    return (datetime.now(UTC) - made).total_seconds() / 60.0 > ttl
