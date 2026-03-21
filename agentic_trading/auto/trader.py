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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .. import autonomy, diagnosis, exits
from ..evidence import collect_evidence
from ..micro_live import BUCKET as ACCOUNT
from ..policy import decide
from ..portfolio import classify_instrument, upstream_identity_lookup
from ..proposals import (
    APPROVED,
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
from . import modes, positions, store

logger = logging.getLogger("agentic_trading.service")

SIGNALS = "auto.signals"
MAX_SIGNALS = 500
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


def plan_loss(book: Book) -> dict[str, Any]:
    """What the plan's own trading has made or lost: closed trades plus open positions marked.

    Only what code traded under a strategy version counts. The line this is held to is the
    person's loss line for the plan, and a loss on a holding they bought themselves is not the
    plan's to answer for.
    """
    own_orders = {p.client_order_id for p in book.proposals
                  if p.client_order_id and p.strategy_version and autonomy.was_autonomous(p)}
    realized = sum(float(trade.realized_usd) for trade in book.ledger.realized
                   if trade.bucket == ACCOUNT and trade.client_order_id in own_orders)
    bucket = book.snap.buckets[ACCOUNT]
    prices = {view.symbol: view.price for view in bucket.positions}
    unrealized, unpriced = 0.0, []
    for held in book.managed:
        if not held.known or not held.cost_basis:
            unpriced.append(held.symbol)
            continue
        price = prices.get(held.symbol)
        if price is None:
            unpriced.append(held.symbol)
            continue
        unrealized += (price - held.cost_basis) * float(held.quantity or 0.0)
    line = -abs(float(book.plan["envelope"]["loss_pause_usd"]))
    total = round(realized + unrealized, 2)
    # Measured from where the line last began: the plan's confirmation, or the last time a
    # person looked at a loss-line pause and resumed (``store.resume``).
    baseline = float(store.state(book.db).get("loss_baseline_usd") or 0.0)
    since = round(total - baseline, 2)
    return {"realized_usd": round(realized, 2), "unrealized_usd": round(unrealized, 2),
            "total_usd": total, "baseline_usd": baseline, "since_usd": since, "line_usd": line,
            # A position that cannot be valued leaves the result unknown, and an unknown result
            # is not one that is inside the line.
            "known": not unpriced, "reached": since <= line, "unpriced": unpriced}


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
    prices = {view.symbol: view.price for view in book.snap.buckets[ACCOUNT].positions}
    for held in book.managed:
        version = store.version(book.db, held.version_id)
        # The trend rule of the version that opened it, if it has one, read on finished closes.
        window = int(((version or {}).get("strategy") or {}).get("trend_window") or 0)
        call = positions.exit_call(held, version, price=prices.get(held.symbol), today=today,
                                   closes=book.closes(held.symbol, window) if window > 0 else None)
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

    seen = seen_signals(db)
    hold = opening_hold(profiles, paused, result.loss)
    settle = settle_seconds(profiles)
    source = Path(runs_dir) if runs_dir is not None else profiles.project_dir / "runs"
    for record, run_dir in reversed(records_in(source)):          # oldest first
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
                raise TraderError(
                    "the plan was paused at its loss line and what it has lost cannot be worked "
                    f"out right now ({', '.join(loss['unpriced'])} cannot be valued), so there is "
                    "nowhere to measure the line from; it stays paused")
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
