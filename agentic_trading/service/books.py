"""The books: the priced portfolio, the drawdown ladders, the real-money gates and the orders.

Reading and reporting only. What may happen to a book is decided in approvals.py.
"""

from __future__ import annotations

import json
import time
from typing import Any, ClassVar

from ..micro_live import BUCKET as MICRO_BUCKET
from ..micro_live import preflight_from
from ..portfolio import PortfolioError
from ..profile import ProfileError, Profiles
from ..proposals import utcnow
from ..state_db import StateDB
from .base import _last_session
from .errors import ServiceError
from .shapes import (
    Fill,
    MicroLive,
    MicroLiveAccount,
    Orders,
    Portfolio,
    PortfolioAccount,
    PortfolioBucket,
    PortfolioCombined,
    PortfolioPosition,
    PreflightCheck,
    TrackedOrder,
)
from .status import micro_live_status, proposal_status


class BooksMixin:
    """The portfolio, the risk ladders, the micro-live status, the orders and the cash flows."""

    #: Which ledger owns which buckets. Two separate books: the conceptual core/sandbox
    #: portfolio, and the real-money Micro-Live account at the venue. They never share a cash
    #: reconciliation, a high-water mark or a total.
    ACCOUNTS: ClassVar = (
        ("conceptual", "core", ("core", "sandbox"), False, None, "total"),
        (MICRO_BUCKET, MICRO_BUCKET, (MICRO_BUCKET,), True, "robinhood", MICRO_BUCKET),
    )

    # ---------------------------------------------------------------- portfolio
    def portfolio(self) -> Portfolio:
        """Every bucket a screen can show, and the account each one belongs to.

        Equity, funding, high-water mark and drawdown are reported per account. A figure that
        added the paper portfolio to the real-money account would answer no question anyone
        has, and comparing such a sum against one account's high-water mark misstates both the
        assets and the drawdown, which is what this shape exists to prevent.
        """
        from ..cli import portfolio_context
        from ..portfolio import cash_flow_summary

        profiles = self.profiles()
        trade_date = _last_session()
        checked_at = utcnow().isoformat()
        # Only the snapshot's text uses the regime, and this discards the text.
        regime, _ = self._regime_reading.get()
        buckets_out: dict[str, PortfolioBucket] = {}
        accounts_out: dict[str, PortfolioAccount] = {}
        errors: dict[str, str] = {}
        for account, sample, buckets, real, venue, ladder_key in self.ACCOUNTS:
            try:
                snap, ladders, _regime, _text = portfolio_context(profiles, trade_date, sample,
                                                                  regime=regime)
            except (PortfolioError, ServiceError, ProfileError) as exc:
                # One unreadable book must not take the other down with it: the reason is
                # reported beside whatever was readable, and `combined.complete` says so.
                errors[account] = str(exc)
                continue
            views = {name: view for name, view in snap.buckets.items() if name in buckets}
            if not views:
                continue
            for name, view in views.items():
                buckets_out[name] = PortfolioBucket(
                    account=account,
                    allocation_usd=view.allocation_usd,
                    cash_usd=round(view.cash_usd, 2),
                    positions_value_usd=round(view.market_value, 2),
                    equity_usd=round(view.equity, 2),
                    position_count=view.n_positions,
                    is_real_money=real,
                    venue=venue,
                    positions=[PortfolioPosition(
                        symbol=p.symbol, quantity=p.quantity, price=p.price,
                        market_value_usd=round(p.market_value, 2),
                        cost_basis_usd=round(p.cost_basis, 2),
                        unrealized_pct=round(p.unrealized_pct, 2),
                        weight_pct_of_allocation=round(p.weight_pct_of_bucket, 2),
                        classification=p.cls.limit_class,
                        exposure_group=p.cls.exposure_group,
                    ) for p in view.positions],
                    sleeves_pct={k: round(v, 2) for k, v in view.sleeves_pct.items()},
                )
            ladder = getattr(ladders, ladder_key, None)
            equity = round(sum(v.equity for v in views.values()), 2)
            # Funding is the configured allocation the bucket opened with, plus every deposit
            # and withdrawal recorded since. Additive on purpose: treating recorded flows as
            # the whole history would turn a bucket's original capital into fabricated profit
            # the moment a single later deposit was written down.
            recorded = cash_flow_summary(snap.state)
            opening = round(sum(profiles.capital_allocation(name) for name in views), 2)
            flows = round(sum(recorded.get(name, 0.0) for name in views), 2)
            deposits = round(opening + flows, 2)
            accounts_out[account] = PortfolioAccount(
                buckets=sorted(views),
                is_real_money=real,
                venue=venue,
                source="robinhood account, reconciled" if real else "local demo ledger",
                as_of=snap.as_of,
                priced_for=snap.trade_date,
                equity_usd=equity,
                cash_usd=round(sum(v.cash_usd for v in views.values()), 2),
                positions_value_usd=round(sum(v.market_value for v in views.values()), 2),
                net_deposits_usd=deposits,
                opening_allocation_usd=opening,
                recorded_flows_usd=flows,
                net_deposits_basis="configured allocation plus recorded deposits and withdrawals",
                investment_pnl_usd=round(equity - deposits, 2),
                return_pct=round((equity - deposits) / deposits * 100, 2) if deposits else None,
                high_water_mark_usd=round(ladder.high_water_mark_usd, 2) if ladder else None,
                drawdown_pct=round(ladder.drawdown_pct, 2) if ladder else None,
                ladder_level=ladder.level if ladder else None,
            )
        return Portfolio(
            as_of=trade_date,
            buckets=buckets_out,
            accounts=accounts_out,
            errors=errors,
            combined=PortfolioCombined(
                equity_usd=round(sum(a.equity_usd for a in accounts_out.values()), 2),
                accounts=sorted(accounts_out),
                complete=not errors,
                # True whenever the sum spans both books, which is precisely when it must not be
                # compared against any single account's high-water mark.
                mixes_real_and_conceptual=len({a.is_real_money for a in accounts_out.values()}) > 1,
            ),
            checked_at=checked_at,
        )

    def ledger_check(self) -> dict[str, Any]:
        """The last ledger check's report, whoever ran it: the daily job, the CLI or the console.

        Read from the state database, never by opening the venue: the Portfolio screen shows
        it on every visit, and a refusal's figures are what its two recording buttons offer.
        """
        from ..ledger_confirm import REPORT

        return self.state_db().read_meta(REPORT, {}) or {}

    def confirm_portfolio(self, *, ledger: str = "all", dry_run: bool = False) -> dict[str, Any]:
        """Renew the ledgers' as_of, but only where a check agrees with them.

        The same call `ct-state confirm` makes. A console that could stamp a date the CLI
        would have refused would be a second opinion about what confirming means, and the
        weaker of the two would be the one reachable from a phone.
        """
        from ..ledger_confirm import confirm_ledgers

        profiles = self.profiles()
        return confirm_ledgers(profiles, self._store(profiles), ledger=ledger, dry_run=dry_run)

    def risk_state(self) -> dict[str, Any]:
        """Drawdown ladders and the market regime, in display form."""
        from ..risk import load_ladder_states

        try:
            profiles = self.profiles()
        except ServiceError:
            return {}
        ps = profiles.risk.get("portfolio_state") or {}
        micro = profiles.risk.get("micro_live_execution") or {}
        out: dict[str, Any] = {}
        for key, path in (("conceptual", ps.get("risk_state_file", "state/risk_state.json")),
                          ("micro_live", micro.get("risk_state_file", "state/micro_live_risk_state.json"))):
            ladders = load_ladder_states(profiles.project_dir / str(path), profiles)
            data = ladders.as_dict()
            if key == "conceptual":
                out.update({k: v for k, v in data.items() if k in ("total", "core", "sandbox")})
            elif data.get("micro_live"):
                out["micro_live"] = data["micro_live"]
        # Kept between polls: the notification pass reads this every fifteen seconds and three
        # screens poll it, and assessing the regime asks the data vendor (see kept.py).
        regime, checked_at = self._regime_reading.get()
        out["regime"] = {**regime.as_dict(), "checked_at": checked_at}
        return out

    # ---------------------------------------------------------------- micro-live
    def micro_live(self) -> MicroLive:
        """Real-money workflow status, the preflight gates, and the capital boundary.

        The venue's answer is a kept reading, at most a couple of minutes old while the status
        is being read; the configuration gates and the open proposal are read on every call.
        Execution never relies on this: it runs preflight again in the session it orders in.
        """
        profiles = self.profiles()
        micro = profiles.risk.get("micro_live_execution") or {}
        enabled = self._micro_enabled(profiles)
        reading = self._venue_reading.get()
        preflight = preflight_from(profiles, reading)
        open_proposal = next((p for p in self._store(profiles).open_proposals(profiles)
                              if p.bucket == MICRO_BUCKET), None)
        status = micro_live_status(enabled=enabled, connected=reading.connected,
                                   preflight_ready=preflight.ready, open_proposal=open_proposal,
                                   profiles=profiles)
        return MicroLive(
            status=status,
            enabled=enabled,
            connected=reading.connected,
            connect_error=reading.connect_error,
            capital_boundary_usd=float(micro.get("max_capital_usd", 100)),
            account=MicroLiveAccount(**preflight.account) if preflight.account else None,
            preflight_ready=preflight.ready,
            checks=[PreflightCheck(id=c.check_id, name=c.name, passed=c.passed, detail=c.detail)
                    for c in preflight.checks],
            blocking=[c.check_id for c in preflight.failures],
            permissions=dict(micro.get("permissions") or {}),
            execution_mode=str(micro.get("execution_mode", "human_approval")),
            checked_at=reading.checked_at,
        )

    # ---------------------------------------------------------------- orders
    #: How often the notification pass opens a venue session only to learn whether it still can.
    #: A standing condition is re-sent every notifications.min_interval_seconds (900) at most.
    BROKER_PROBE_SECONDS: ClassVar[float] = 300.0

    def _broker_connect_error(self) -> str | None:
        probe = getattr(self, "_broker_probe", None)
        now = time.monotonic()
        if probe is None or now - probe[0] >= self.BROKER_PROBE_SECONDS:
            probe = (now, self.micro_live().connect_error)
            self._broker_probe = probe
        return probe[1]

    def _recorded_fills(self, profiles: Profiles, limit: int) -> list[Fill]:
        """Fills this system recorded, newest first, without asking the venue anything."""
        fills_path = profiles.project_dir / str(
            (profiles.risk.get("broker") or {}).get("fills_file", "state/fills.jsonl"))
        rows = StateDB(fills_path).fills(limit)
        if not rows and fills_path.exists() and not StateDB(fills_path).migrated(fills_path):
            for line in fills_path.read_text(encoding="utf-8").splitlines()[-limit:]:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return [Fill(**row) for row in reversed(rows)]

    def orders(self, limit: int = 50) -> Orders:
        """The fills this system recorded and the orders it is tracking, from its own record.

        The venue's open-order list used to be read here too, on every poll of the Activity
        screen: a Robinhood handshake every ten seconds to show a list the Robinhood app
        already shows. What the app cannot show is what this side knows about an order -- the
        proposal it came from, whether it reconciled, and where the two disagree -- and that
        is what is returned. Execution still reads the venue's open orders where they decide
        something, at revalidation.
        """
        profiles = self.profiles()
        fills = self._recorded_fills(profiles, limit)
        tracked = [TrackedOrder(proposal_id=p.proposal_id, order_id=p.broker_order_id,
                                client_order_id=p.client_order_id, symbol=p.ticker,
                                status=proposal_status(p, profiles), venue_status=p.order_status,
                                bucket=p.bucket, side="buy" if p.action in ("BUY", "ADD") else "sell",
                                filled_quantity=p.filled_quantity,
                                filled_avg_price=p.filled_avg_price, reconciled_at=p.reconciled_at,
                                reconciliation_error=p.reconciliation_error)
                   for p in self._store(profiles).all() if p.client_order_id]
        return Orders(fills=fills, tracked_orders=tracked)

    # ---------------------------------------------------------------- cash flows
    def record_cashflow(self, *, bucket: str, amount_usd: float, kind: str = "deposit",
                        date: str | None = None, reason: str = "",
                        identity: str | None = None) -> dict[str, Any]:
        """Fund or defund a bucket, the same call as ``ct-state cashflow``."""
        from ..portfolio import cash_flow_summary, record_cash_flow
        from ..state_cli import _sources

        profiles = self.profiles()
        _proposal, _fills, ledgers, _ladders = _sources(profiles)
        if bucket in ("core", "sandbox"):
            ledger = ledgers[0]
        elif len(ledgers) > 1:
            ledger = ledgers[1]
        else:
            raise ServiceError(f"no ledger holds bucket {bucket!r}")
        try:
            state = record_cash_flow(ledger, bucket=bucket, amount_usd=amount_usd, kind=kind,
                                     date=date, note=reason)
        except (ValueError, KeyError, PortfolioError) as exc:
            raise ServiceError(str(exc)) from exc
        self._journal(profiles, kind="cashflow_recorded", subject=bucket,
                      actor=identity or "console",
                      detail={"bucket": bucket, "kind": kind, "amount_usd": amount_usd,
                              "date": date})
        return {"ledger": str(ledger.name), "bucket": bucket,
                "cash_usd": state.buckets[bucket].cash_usd,
                "net_deposits": cash_flow_summary(state)}
