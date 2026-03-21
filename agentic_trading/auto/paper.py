"""The simulated account: the real account's rules and code path, with no way to the real venue.

A rehearsal is only worth something if it rehearses the real thing. So the simulated account is
not a second set of rules that resemble the real ones: it is the same bucket, the same risk
profile, the same Hard Risk engine, the same approval and execution code, pointed at a different
directory. What differs is exactly two things, and both are stated rather than implied.

**Its state is its own.** Everything it records -- the ledger, proposals, orders, fills, the
drawdown ladder, research artefacts -- lives under ``<state>/paper/``, in its own database
(``<state>/paper/state/trading_state.sqlite3``). Nothing it writes is in the files the real
account reads, so a simulated fill cannot appear in the real ledger and a simulated record
cannot be counted as a real one.

**Its venue cannot be the real one.** The directory carries a generated copy of the
configuration with one extra section, ``paper_trading``. Wherever a venue is chosen, that
section sends the order to the simulator and refuses anything else, and the one place a real
venue session is created (``RobinhoodBroker.__init__``) refuses to be created under it. The
second lock is the one that matters: it does not depend on every caller choosing correctly.

The generated configuration is derived, never edited: it is rebuilt from ``config/`` and the
plan in force whenever either changes, and it applies the plan's own changes -- which is the
point. The simulated account runs under the settings the plan *proposes*, so what is being
rehearsed is the plan, before any of it is confirmed for the real account.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import Any

from ..profile import Profiles, load_profiles
from ..storage import atomic_write

#: The top-level section that marks a configuration as the simulated account's. It exists only
#: in a generated copy; the console cannot add a top-level section to the real configuration.
MARKER = "paper_trading"
DIRECTORY = "paper"
ACCOUNT = "micro_live"
VENUE = "simulated"
#: The simulated venue fills in the increment the real one trades in (``exits.MIN_INCREMENT``).
#: Without it a dollar amount would buy a quantity no real order could, and selling that
#: position in the venue's increment would always leave a sliver nothing could ever sell.
QUANTITY_INCREMENT = 0.000001
#: The oldest quote the simulated venue may fill on. See ``OVERRIDES`` for why it is not the
#: real account's two minutes.
QUOTE_MAX_AGE_SECONDS = 1200

#: Copied as they are. The mandate is a prompt and the calendar is a list of dates; neither
#: differs between a rehearsal and the thing rehearsed.
COPIED = ("investor_profile.yaml", "event_calendar.yaml", "MANDATE.md", "mandate_context.md")

#: What the simulated account's configuration sets regardless of the plan.
OVERRIDES: dict[str, Any] = {
    # The bucket's own master switch has to be on for the pipeline to run at all. In this
    # configuration it switches nothing real: the venue below cannot be Robinhood.
    "micro_live_execution.enabled": True,
    # A simulated fill is not something to send to a phone.
    "notifications.channel": "none",
    # It schedules no research of its own; it reads what the real account's research concluded.
    "research_schedule.enabled": False,
    # How old a quote from its venue may be. The real venue quotes the market and is held to two
    # minutes. The simulated one prices from a public feed of one-minute bars that can lag the
    # market by a quarter of an hour on some listings; held to two minutes it would never fill,
    # and what it would be rehearsing is the feed. So it is allowed twenty, every price carries
    # its own time, and nothing is acted on until its research is older than that (auto.trader).
    "data_freshness.fast.max_age_seconds": QUOTE_MAX_AGE_SECONDS,
}


class PaperError(RuntimeError):
    """The simulated account could not be prepared, or was asked to do something real."""


def is_paper(profiles: Any) -> bool:
    """Whether these profiles are the simulated account's. True only for a generated copy."""
    section = getattr(profiles, "risk", {}).get(MARKER) if profiles is not None else None
    return isinstance(section, dict) and section.get("enabled") is True


def refuse_real_venue(profiles: Any, what: str) -> None:
    """Raise if these are the simulated account's profiles. Called where a real session opens."""
    if is_paper(profiles):
        from ..broker.base import BrokerError

        raise BrokerError(f"this is the simulated account: {what} is never done from it, and no "
                          "session with a real venue can be opened under its configuration")


def root_of(live: Profiles) -> Path:
    """Where the simulated account lives: beside the real state, never among it."""
    store = Path(str((live.risk.get("broker") or {}).get("state_file", "state/proposals.json")))
    state_dir = (store if store.is_absolute() else live.project_dir / store).parent
    return state_dir / DIRECTORY


def _revision(live_config: Path, updates: dict[str, Any]) -> str:
    digest = hashlib.sha256()
    for name in ("risk_profile.yaml", *COPIED):
        path = live_config / name
        digest.update(name.encode())
        digest.update(path.read_bytes() if path.exists() else b"")
    digest.update(repr(sorted((str(k), repr(v)) for k, v in updates.items())).encode())
    # What this module itself contributes: a release that changes what the simulated account
    # sets has to rebuild a configuration that was generated before it.
    digest.update(repr(sorted((str(k), repr(v)) for k, v in OVERRIDES.items())).encode())
    return digest.hexdigest()[:16]


def build_risk_profile(live_text: str, updates: dict[str, Any], *, creates: frozenset[str],
                       revision: str, plan_id: str | None) -> str:
    """The real risk profile with the plan's changes and the simulated account's own applied."""
    from .. import config_changes as cc

    merged = {**updates, **OVERRIDES}
    text = cc.set_values(live_text, merged, create=creates & set(merged))
    marker = (
        f"\n# GENERATED by agentic_trading.auto.paper from config/risk_profile.yaml and the plan\n"
        f"# in force. Do not edit: it is rewritten whenever either changes. This section is what\n"
        f"# makes the configuration the simulated account's; under it no order can reach a real\n"
        f"# venue (see agentic_trading/auto/paper.py).\n"
        f"{MARKER}:\n  enabled: true\n  source_revision: {revision}\n"
        f"  plan_id: {plan_id or 'none'}\n  venue: {VENUE}\n"
        f"  # What its fills are priced from and what they are charged: auto/quotes.py, auto/costs.py.\n"
        f"  quote_feed: vendor_minute_bar\n  cost_model: standard\n")
    return text.rstrip("\n") + "\n" + marker


def ensure(live: Profiles, *, changes: list[dict[str, Any]] | None = None,
           creates: frozenset[str] = frozenset(), plan_id: str | None = None) -> Path:
    """Make the simulated account's directory and configuration current; return its config dir.

    Idempotent, and cheap when nothing moved: the generated risk profile records the revision
    of what it was built from, and is only rewritten when that differs.
    """
    if is_paper(live):
        raise PaperError("the simulated account cannot be nested inside itself")
    root = root_of(live)
    config = root / "config"
    updates = {str(c["path"]): c["to"] for c in changes or []
               if c.get("file", "risk_profile.yaml") == "risk_profile.yaml"}
    revision = _revision(live.config_dir, {**updates, "plan": plan_id})
    target = config / "risk_profile.yaml"
    if target.exists() and f"source_revision: {revision}\n" in target.read_text(encoding="utf-8"):
        return config
    for directory in (config, root / "state", root / "runs"):
        directory.mkdir(parents=True, exist_ok=True)
    for name in COPIED:
        source = live.config_dir / name
        if source.exists():
            shutil.copyfile(source, config / name)
    live_text = (live.config_dir / "risk_profile.yaml").read_text(encoding="utf-8")
    text = build_risk_profile(live_text, updates, creates=creates, revision=revision, plan_id=plan_id)
    atomic_write(target, text)
    built = load_profiles(config)
    if not is_paper(built):                      # the marker is the whole of the second lock
        target.unlink()
        raise PaperError("the generated configuration does not carry the simulated-account marker")
    return config


def profiles_of(live: Profiles) -> Profiles | None:
    """The simulated account's profiles, or None if it has never been prepared."""
    config = root_of(live) / "config"
    if not (config / "risk_profile.yaml").exists():
        return None
    built = load_profiles(config)
    if not is_paper(built):
        raise PaperError(f"{config} does not carry the simulated-account marker; refusing to use it")
    return built


def ledger_path(paper: Profiles) -> Path:
    micro = paper.risk.get("micro_live_execution") or {}
    return paper.project_dir / str(micro.get("ledger_file", "state/micro_live_portfolio.yaml"))


def open_account(paper: Profiles, *, cash_usd: float, as_of: str) -> dict[str, Any]:
    """Start the simulated account's ledger with a deposit, once. A second call changes nothing.

    The opening balance is recorded as a deposit, so it is funding and never a gain: the
    account's return is measured against what was put in, exactly as the real account's is.
    """
    from ..portfolio import PortfolioError, PortfolioState, load_portfolio, record_cash_flow
    from ..state_db import StateDB

    if not is_paper(paper):
        raise PaperError("only the simulated account's ledger is opened from here")
    path = ledger_path(paper)
    try:
        existing = load_portfolio(path)
        return {"opened": False, "cash_usd": existing.buckets[ACCOUNT].cash_usd}
    except PortfolioError:
        pass
    if not isinstance(cash_usd, (int, float)) or isinstance(cash_usd, bool) or cash_usd <= 0:
        raise PaperError("the simulated account needs a positive opening balance")
    empty = PortfolioState(as_of=as_of, buckets={ACCOUNT: {"cash_usd": 0.0}}, positions=[],
                           recent_opens=[], notes="simulated account, opened empty")
    db = StateDB(path)
    with db.transaction() as conn:
        db.write_document(path, empty.model_dump(mode="json"), conn)
        db.mark_migrated(conn, path, "portfolio", b"")
    record_cash_flow(path, bucket=ACCOUNT, amount_usd=round(float(cash_usd), 2), kind="deposit",
                     date=as_of, note="opening balance of the simulated account")
    return {"opened": True, "cash_usd": round(float(cash_usd), 2)}


def fill_record_path(paper: Profiles) -> Path:
    """Where the simulated venue keeps what each of its fills gave up to the quote."""
    return paper.project_dir / "state" / "paper_fill_costs.json"


def venue(paper: Profiles, **overrides: Any):
    """The simulated account's venue (``auto.venue.PaperVenue``), holding what its ledger holds.

    Seeded from the ledger on every construction, so it starts each time from the book as it
    stands; its own orders persist beside the state, so one it accepted is still there to be
    reconciled after a restart. Its prices are the current session's, with their own time, and
    its fills pay the cost model's spread, slippage and fees -- see ``auto.venue``.
    """
    from ..portfolio import PortfolioError, classify_instrument, load_portfolio
    from .venue import PaperVenue

    if not is_paper(paper):
        raise PaperError("the simulated venue is built only for the simulated account")

    def limit_class_of(symbol: str) -> str | None:
        # By the configured universe alone: what the venue charges to trade a fund must not
        # depend on a vendor answering a question about it.
        try:
            return classify_instrument(symbol, paper, lambda _symbol: {"quote_type": "ETF"}).limit_class
        except Exception:  # noqa: BLE001 - unclassified takes the widest spread, not none
            return None

    fast = (paper.risk.get("data_freshness") or {}).get("fast") or {}
    kwargs: dict[str, Any] = {
        "quantity_increment": QUANTITY_INCREMENT, "limit_class_of": limit_class_of,
        "max_quote_age_seconds": float(fast.get("max_age_seconds", 120)),
        "order_state_path": paper.project_dir / "state" / "paper_orders.json",
        "fill_record_path": fill_record_path(paper)}
    try:
        ledger = load_portfolio(ledger_path(paper))
        kwargs["cash"] = sum(b.cash_usd for b in ledger.buckets.values())
        kwargs["positions"] = {p.symbol: (p.quantity, p.cost_basis_per_share) for p in ledger.positions}
    except PortfolioError:
        kwargs["cash"] = 0.0
    kwargs.update(overrides)
    return PaperVenue(**kwargs)
