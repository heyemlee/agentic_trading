"""What every domain shares: the profiles, the stores, the venue session and the kept readings.

The session anchor lives here too -- which trading session research and pricing hold to while
the data vendor lags the close -- because four domains read it.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from ..broker import make_broker
from ..broker.read_session import ReadSession, SessionUnavailable
from ..micro_live import BUCKET as MICRO_BUCKET
from ..micro_live import VenueReading, read_venue
from ..profile import ProfileError, Profiles, load_profiles
from ..proposals import ProposalStore, utcnow
from ..research_jobs import ResearchJobManager
from ..state_db import StateDB
from .errors import ServiceError
from .kept import Kept
from .shapes import Portfolio

_T = TypeVar("_T")


#: Named for the package rather than this module: the split is internal, so a log
#: record must still read as it did, and getLogger returns the one same object.
logger = logging.getLogger("agentic_trading.service")


class ServiceBase:
    """The state a :class:`~agentic_trading.service.Service` keeps, and the readers of it."""

    def __init__(self, config_dir: str | Path = "config"):
        self.config_dir = Path(config_dir)
        self._venue_reads = ReadSession()
        # Daily bars: renewed every quarter hour, and a failed assessment every two minutes.
        self._regime_reading = Kept(
            lambda: self._read_regime(), key=lambda: self._regime_inputs(),
            refresh_after=lambda reading: 900.0 if reading[0].available else 120.0,
            serve_for=lambda reading: 1800.0 if reading[0].available else 300.0)
        # The account moves with a trade or a transfer; a minute old while someone is looking.
        # Served for ninety seconds at most, so a poll a minute later is inside the console's
        # three-minute threshold for it (SOURCES in ui/assets/api.js).
        self._venue_reading = Kept(
            lambda: self._read_venue_account(), key=lambda: self._venue_inputs(),
            refresh_after=60.0, serve_for=90.0, watch_for=300.0)
        # Recomputed at once when a ledger is written; otherwise prices move once a session.
        self._portfolio_reading = Kept(
            lambda: self.portfolio(), key=lambda: self._portfolio_inputs(),
            refresh_after=240.0, serve_for=480.0, watch_for=600.0)

    # ---------------------------------------------------------------- profiles
    def profiles(self) -> Profiles:
        try:
            return load_profiles(self.config_dir)
        except ProfileError as exc:
            raise ServiceError(f"configuration error: {exc}") from exc

    def _store(self, profiles: Profiles) -> ProposalStore:
        path = str((profiles.risk.get("broker") or {}).get("state_file", "state/proposals.json"))
        return ProposalStore(profiles.project_dir / path)

    def state_db(self, profiles: Profiles | None = None) -> StateDB:
        """The one transactional database behind this deployment's state directory."""
        return self._store(profiles or self.profiles()).db

    def _research_manager(self, profiles: Profiles) -> ResearchJobManager:
        store = self._store(profiles)
        return ResearchJobManager(
            state_source=store.path, project_dir=profiles.project_dir,
            config_dir=profiles.config_dir, settings=profiles.risk.get("research_jobs") or {},
        )

    def _micro_enabled(self, profiles: Profiles) -> bool:
        return bool((profiles.risk.get("micro_live_execution") or {}).get("enabled", False))

    def _require_micro_enabled(self, profiles: Profiles) -> None:
        if not self._micro_enabled(profiles):
            raise ServiceError("real-money execution is disabled (micro_live_execution.enabled is false)")

    def _broker(self, profiles: Profiles, bucket: str):
        """The venue that owns a bucket. Real money only ever comes from the Micro-Live path."""
        from ..auto import paper

        if paper.is_paper(profiles):
            # The simulated account: one venue, and it is not a real one whatever the bucket.
            return paper.venue(profiles)
        if bucket == MICRO_BUCKET:
            from ..broker.robinhood import RobinhoodBroker

            self._require_micro_enabled(profiles)
            return RobinhoodBroker(profiles)
        return make_broker(profiles)

    def _read_venue(self, profiles: Profiles, read: Callable[[Any], _T]) -> _T:
        """Run a read against the real-money venue on the session kept for reads.

        For reads that record nothing when the venue fails them, and no others. A read that
        fails on a kept session is run again on a fresh one, so whatever it recorded the first
        time would be recorded twice, and a failure it recorded would describe a session the
        venue had ended rather than the venue. Reconciliation marks the order it could not read,
        which is why it opens its own session. Raises SessionUnavailable when no session can be
        opened.
        """
        from ..broker import robinhood

        return self._venue_reads.read(robinhood.session_key(profiles),
                                      lambda: robinhood.RobinhoodBroker(profiles), read)

    # ---------------------------------------------------------------- kept readings
    def refresh_kept_readings(self) -> dict[str, bool]:
        """Renew whichever kept readings are due; the regime first, since the portfolio uses it.

        Called by the API's refresh loop, off the request path. A reading that fails is logged
        and the others still run; it keeps its last result, which requests judge by its age.
        """
        refreshed: dict[str, bool] = {}
        for name, reading in (("regime", self._regime_reading), ("venue", self._venue_reading),
                              ("portfolio", self._portfolio_reading)):
            try:
                refreshed[name] = reading.refresh()
            except Exception:
                logger.exception("renewing the kept %s reading failed", name)
                refreshed[name] = False
        return refreshed

    def kept_portfolio(self) -> Portfolio:
        """The portfolio as the console reads it, kept between polls (see kept.py).

        Computed again as soon as a ledger is written, the configuration changes or the session
        rolls over, and otherwise renewed every few minutes while it is being read. Use
        :meth:`portfolio` where the books must be read and priced now.
        """
        return self._portfolio_reading.get()

    def _regime_inputs(self) -> object:
        profiles = self.profiles()
        return _last_session(), json.dumps(profiles.risk.get("market_regime") or {},
                                           sort_keys=True, default=str)

    def _read_regime(self) -> tuple[Any, str]:
        from ..risk import assess_regime
        from ..risk.regime import RegimeResult

        profiles = self.profiles()
        checked_at = utcnow().isoformat()
        try:
            return assess_regime(profiles, _last_session()), checked_at
        except Exception as exc:  # noqa: BLE001 - unknown, never guessed
            return RegimeResult("UNKNOWN", False, str(exc)), checked_at

    def _venue_inputs(self) -> object:
        from ..broker import robinhood

        return robinhood.session_key(self.profiles())

    def _read_venue_account(self) -> VenueReading:
        from ..auto import paper

        profiles = self.profiles()
        checked_at = utcnow().isoformat()
        if paper.is_paper(profiles):
            # The simulated account's venue is in this process; there is no session to keep.
            return read_venue(paper.venue(profiles), checked_at)
        try:
            return self._read_venue(profiles, lambda broker: read_venue(broker, checked_at))
        except SessionUnavailable as exc:  # not connected is a normal state, not a crash
            return VenueReading(checked_at=checked_at, connected=False, connect_error=str(exc))

    def _portfolio_inputs(self) -> object:
        """Everything the priced portfolio is computed from that can change within a session.

        A ledger lives in the state database, where every write moves its revision; one not
        yet migrated is still a file. The drawdown ladders are not here: computing the portfolio
        writes them, and they follow from the ledgers and prices that are.
        """
        profiles = self.profiles()
        ps = profiles.risk.get("portfolio_state") or {}
        micro = profiles.risk.get("micro_live_execution") or {}
        ledgers = []
        for value in (ps.get("file", "state/portfolio.yaml"),
                      micro.get("ledger_file", "state/micro_live_portfolio.yaml")):
            path = profiles.project_dir / str(value)
            ledgers.append((str(value), StateDB(path).document_revision(path), _file_stamp(path)))
        return _last_session(), _directory_stamp(profiles.config_dir), tuple(ledgers)

    def release_idle_venue_session(self) -> bool:
        """Close the kept venue session if nobody has read through it lately."""
        return self._venue_reads.release_idle()

    def close(self) -> None:
        """Close the kept venue session. The API does this as it stops."""
        self._venue_reads.close()


# ---------------------------------------------------------------------- helpers
def _file_stamp(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size


def _directory_stamp(directory: Path) -> tuple[tuple[str, int, int], ...]:
    """Every file under a directory, by name, modification time and size: any edit moves it."""
    stamps = []
    for path in sorted(Path(directory).rglob("*")):
        stamp = _file_stamp(path) if path.is_file() else None
        if stamp is not None:
            stamps.append((str(path.relative_to(directory)), *stamp))
    return tuple(stamps)


#: How long a "no usable bar yet" answer stands before the vendor is asked again. A published
#: bar does not go away, so a "yes" stands for the life of the process.
SESSION_PROBE_RETRY_SECONDS = 600.0


_SESSION_PROBES_KEPT = 16


_session_probes: dict[str, tuple[bool, float]] = {}


def _session_has_data(session: str) -> bool:
    """Whether the data vendor has published a usable bar for this session.

    A vendor can lag hours behind the close, publishing a bar with volume but no closing price.
    Research and pricing must anchor to a session that is actually complete, so this probes a
    single liquid symbol rather than assuming the calendar and the vendor agree.

    This was an ``lru_cache``, which kept a "no" for the life of the API process. Probed once in
    the vendor's lag window, a session stayed "no data" until a restart: every research run and
    every price the next day anchored to the session before it, and each later day added another
    stale key, up to four sessions behind. A "no" is therefore only remembered for a few minutes,
    which still keeps a console polling every few seconds from asking the vendor each time.
    """
    now = time.monotonic()
    known = _session_probes.get(session)
    if known is not None and (known[0] or now - known[1] < SESSION_PROBE_RETRY_SECONDS):
        return known[0]
    try:
        from tradingagents.dataflows.stockstats_utils import load_ohlcv

        df = load_ohlcv("SPY", session)
        usable = df is not None and not df.empty
    except Exception:  # noqa: BLE001 - any failure means "not usable"
        usable = False
    _session_probes[session] = (usable, now)
    while len(_session_probes) > _SESSION_PROBES_KEPT:
        _session_probes.pop(next(iter(_session_probes)))
    return usable


def _last_session(max_lookback: int = 5) -> str:
    """The most recent trading session with usable data, which is what research anchors to."""
    import pandas as pd

    try:
        from ..broker.robinhood import _nyse_calendar

        cal = _nyse_calendar()
        now = pd.Timestamp.now(tz="America/New_York")
        session = cal.date_to_session(now.date(), direction="previous")
        if session.date() == now.date() and now.hour < 16:
            session = cal.date_to_session(session.date() - pd.Timedelta(days=1), direction="previous")
        for _ in range(max_lookback):
            candidate = str(session.date())
            if _session_has_data(candidate):
                return candidate
            session = cal.date_to_session(session.date() - pd.Timedelta(days=1), direction="previous")
        return str(session.date())
    except Exception:  # noqa: BLE001
        return (pd.Timestamp.now(tz="America/New_York") - pd.Timedelta(days=1)).date().isoformat()
