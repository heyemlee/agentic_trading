"""Robinhood Agentic Trading adapter, over Robinhood's official MCP server.

Architecture note, and the most important design decision in this file:

The Robinhood MCP is used here as a **broker transport for deterministic Python**, never as a
tool handed to a language model. Robinhood markets the MCP as something an AI agent connects
to and trades through directly; that shape would put a model inside the execution path and let
it place orders without passing the deterministic Hard Risk Layer. This system does the
opposite. The model researches, our code decides, and this adapter is the only thing that
talks to the venue. The research runtime is started with ``--strict-mcp-config`` and only our
own data tools, so no Claude session in this project can reach a trading tool.

Two further consequences of using the official interface rather than rebuilding it:

* Every account, quote, position, order and cancellation comes from Robinhood's own tools. No
  market data, order routing or portfolio logic is reimplemented here.
* Tool names are discovered from the server at connect time and matched against the candidate
  lists in config. Robinhood has renamed tools before; a missing capability raises rather than
  being guessed at, so the pipeline fails closed instead of inventing broker behaviour.

Authentication is OAuth 2.0 authorization code with PKCE and dynamic client registration, the
flow the MCP SDK implements. The first connection needs a browser sign-in and approval in the
Robinhood app; after that the refresh token keeps a headless process working.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import uuid
from collections.abc import Iterable
from concurrent.futures import Future
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from .base import (
    Account,
    BrokerError,
    BrokerOrder,
    BrokerPosition,
    BrokerRejection,
    BrokerUnavailable,
    Clock,
    DuplicateOrder,
    OrderRequest,
    Quote,
)

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _nyse_calendar():
    """Build the NYSE calendar once. Construction is expensive and the calendar is static."""
    import exchange_calendars as xcals

    return xcals.get_calendar("XNYS")

_STATUS_MAP = {
    "queued": "new", "new": "new", "unconfirmed": "new", "confirmed": "accepted",
    "pending": "new", "partially_filled": "partially_filled", "filled": "filled",
    "cancelled": "canceled", "canceled": "canceled", "rejected": "rejected",
    "failed": "rejected", "expired": "expired", "voided": "canceled",
}


class RobinhoodNotConnected(BrokerError):
    """No usable OAuth token. The investor must run the connect command once, in a browser."""


def _num(value: Any, default: float | None = None) -> float | None:
    if value is None or value == "":
        return default
    if isinstance(value, dict):  # some fields arrive as {"amount": "...", "currency": "USD"}
        value = value.get("amount", value.get("value"))
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _first(payload: dict[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in payload and payload[name] not in (None, ""):
            return payload[name]
    return default


def _unwrap(payload: Any) -> Any:
    """Strip Robinhood's response envelope.

    Every tool answers ``{"data": <payload>, "guide": "<presentation advice>"}``. The guide is
    text written by the venue about how to display the payload; it is treated as data, never as
    instructions to this program.
    """
    if isinstance(payload, dict) and "data" in payload and set(payload) <= {"data", "guide", "meta", "next"}:
        return payload["data"]
    return payload


def _rows(payload: Any, *keys: str) -> list[dict[str, Any]]:
    """Normalise the shapes an MCP tool may return into a list of dicts."""
    payload = _unwrap(payload)
    if payload is None:
        return []
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for key in (*keys, "results", "items", "accounts", "positions", "orders", "quotes"):
            inner = payload.get(key)
            if isinstance(inner, list):
                return [r for r in inner if isinstance(r, dict)]
        return [payload]
    return []


class _Portal:
    """A private event loop on a background thread, so a sync adapter can drive an async client."""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name="robinhood-mcp", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def call(self, coro, timeout: float):
        future: Future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    def close(self) -> None:
        self._loop.call_soon_threadsafe(self._loop.stop)


class FileTokenStorage:
    """OAuth tokens on disk, owner-readable only. Never logged, never printed."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)

    async def get_tokens(self):
        from mcp.shared.auth import OAuthToken

        raw = self._read().get("tokens")
        return OAuthToken.model_validate(raw) if raw else None

    async def set_tokens(self, tokens) -> None:
        data = self._read()
        data["tokens"] = tokens.model_dump(mode="json")
        data["updated_at"] = datetime.now(UTC).isoformat()
        self._write(data)

    async def get_client_info(self):
        from mcp.shared.auth import OAuthClientInformationFull

        raw = self._read().get("client")
        return OAuthClientInformationFull.model_validate(raw) if raw else None

    async def set_client_info(self, info) -> None:
        data = self._read()
        data["client"] = info.model_dump(mode="json")
        self._write(data)

    @property
    def connected(self) -> bool:
        return bool(self._read().get("tokens"))


class RobinhoodBroker:
    """Implements the project's BrokerAdapter over Robinhood's official MCP tools."""

    name = "robinhood"

    def __init__(self, profiles, *, redirect_handler=None, callback_handler=None, connect: bool = True):
        cfg = dict(profiles.risk.get("robinhood") or {})
        micro = dict(profiles.risk.get("micro_live_execution") or {})
        self.profiles = profiles
        self.url = str(cfg.get("mcp_url", "https://agent.robinhood.com/mcp/trading"))
        self.call_timeout = float(cfg.get("call_timeout_seconds", 45))
        self.connect_timeout = float(cfg.get("connect_timeout_seconds", 60))
        self.tool_candidates: dict[str, list[str]] = dict(cfg.get("tool_candidates") or {})
        self.required: list[str] = list(cfg.get("required_capabilities") or [])
        self.agentic_cfg = dict(cfg.get("agentic_account") or {})
        self.max_capital = float(micro.get("max_capital_usd", 100))
        self.storage = FileTokenStorage(profiles.project_dir / str(cfg.get("token_file", "state/robinhood_token.json")))
        # Real money: this adapter is never a paper venue, and says so.
        self.is_paper = False
        self.tools: dict[str, str] = {}
        self.available_tools: list[str] = []
        self._agentic_account: dict[str, Any] | None = None
        self._portal: _Portal | None = None
        self._session = None
        self._exit_stack = None
        self._redirect_handler = redirect_handler
        self._callback_handler = callback_handler
        if connect:
            self.connect()

    # ------------------------------------------------------------------ session
    def connect(self) -> None:
        if not self.storage.connected and self._callback_handler is None:
            raise RobinhoodNotConnected(
                "no Robinhood OAuth token. Run `uv run ct-robinhood connect` once: it opens a browser, "
                "you sign in to Robinhood and approve access, and the token is stored locally."
            )
        self._portal = _Portal()
        timeout = 300.0 if self._callback_handler is not None else self.connect_timeout
        try:
            self._portal.call(self._aconnect(), timeout=timeout)
        except Exception as exc:
            self.close()
            raise BrokerUnavailable(f"could not connect to the Robinhood MCP: {type(exc).__name__}: {exc}") from exc
        self._resolve_tools()

    async def _aconnect(self) -> None:
        from contextlib import AsyncExitStack

        import httpx2
        from mcp.client.auth import OAuthClientProvider
        from mcp.client.session import ClientSession
        from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
        from mcp.shared.auth import OAuthClientMetadata

        metadata = OAuthClientMetadata(
            client_name="conservative-trading (personal)",
            redirect_uris=["http://localhost:8765/callback"],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="none",
        )
        auth = OAuthClientProvider(
            server_url=self.url, client_metadata=metadata, storage=self.storage,
            redirect_handler=self._redirect_handler, callback_handler=self._callback_handler,
        )
        self._exit_stack = AsyncExitStack()
        http_client = await self._exit_stack.enter_async_context(
            create_mcp_http_client(
                auth=auth,
                timeout=httpx2.Timeout(self.call_timeout, read=300.0),
            )
        )
        read, write = await self._exit_stack.enter_async_context(
            streamable_http_client(self.url, http_client=http_client)
        )
        self._session = await self._exit_stack.enter_async_context(ClientSession(read, write))
        await self._session.initialize()

    def _resolve_tools(self) -> None:
        """Discover the server's tools and bind our capabilities to real names, or fail."""
        listing = self._portal.call(self._session.list_tools(), timeout=self.call_timeout)
        self.available_tools = sorted(t.name for t in listing.tools)
        for capability, candidates in self.tool_candidates.items():
            match = next((c for c in candidates if c in self.available_tools), None)
            if match:
                self.tools[capability] = match
        missing = [c for c in self.required if c not in self.tools]
        if missing:
            raise BrokerUnavailable(
                f"the Robinhood MCP does not expose a tool for {missing}. Available tools: "
                f"{self.available_tools}. Update robinhood.tool_candidates in config/risk_profile.yaml "
                "rather than assuming the capability exists."
            )
        logger.info("Robinhood MCP tools bound: %s", self.tools)

    def close(self) -> None:
        if self._portal is not None:
            try:
                if self._exit_stack is not None:
                    self._portal.call(self._exit_stack.aclose(), timeout=10)
            except Exception as exc:  # noqa: BLE001
                logger.debug("error closing the Robinhood session: %s", exc)
            self._portal.close()
            self._portal = None

    # ------------------------------------------------------------------ calls
    def _call(self, capability: str, arguments: dict[str, Any] | None = None) -> Any:
        tool = self.tools.get(capability)
        if tool is None:
            raise BrokerUnavailable(f"no Robinhood tool bound for {capability!r}")
        if self._session is None or self._portal is None:
            raise BrokerUnavailable("the Robinhood session is not open")
        try:
            result = self._portal.call(self._session.call_tool(tool, arguments or {}), timeout=self.call_timeout)
        except TimeoutError as exc:
            raise BrokerUnavailable(
                f"the Robinhood call {tool} timed out. If this was an order, it MAY have been placed: "
                "look it up by client order id before any retry."
            ) from exc
        except Exception as exc:
            raise BrokerUnavailable(f"the Robinhood call {tool} failed: {type(exc).__name__}: {exc}") from exc
        if getattr(result, "isError", False):
            raise BrokerRejection(f"Robinhood tool {tool} returned an error: {_content_text(result)}")
        payload = getattr(result, "structuredContent", None)
        if payload is None:
            text = _content_text(result)
            try:
                payload = json.loads(text)
            except (json.JSONDecodeError, TypeError):
                payload = {"text": text}
        return payload

    # ------------------------------------------------------------------ accounts
    def _accounts(self) -> list[dict[str, Any]]:
        rows = _rows(self._call("accounts"), "accounts")
        if not rows:
            raise BrokerUnavailable("Robinhood returned no accounts")
        return rows

    @staticmethod
    def _account_number(account: dict[str, Any]) -> str:
        return str(_first(account, "account_number", "rhs_account_number", "number", "id", default=""))

    def agentic_account(self, refresh: bool = False) -> dict[str, Any]:
        """The dedicated Agentic account. Ambiguity is a hard failure, never a guess."""
        if self._agentic_account is not None and not refresh:
            return self._agentic_account
        rows = self._accounts()
        pinned = self.agentic_cfg.get("account_number")
        if pinned:
            match = [a for a in rows if str(_first(a, "account_number", "number", "id", default="")) == str(pinned)]
            if not match:
                raise BrokerUnavailable(f"the pinned Agentic account {pinned} was not found among your accounts")
            self._agentic_account = match[0]
            return self._agentic_account
        # Robinhood marks exactly one account as agentic_allowed. That explicit flag is the
        # authority; nickname and account type are not (both accounts read "individual").
        matches = [a for a in rows if a.get("agentic_allowed") is True]
        if len(matches) != 1:
            raise BrokerUnavailable(
                f"expected exactly one account with agentic_allowed=true, found {len(matches)} out of "
                f"{len(rows)} accounts. Run `ct-robinhood status`, then pin the account number in "
                "robinhood.agentic_account.account_number. Refusing to guess which account trades."
            )
        self._agentic_account = matches[0]
        return self._agentic_account

    def get_account(self) -> Account:
        acct = self.agentic_account(refresh=True)
        number = self._account_number(acct)
        if not number:
            raise BrokerUnavailable("the Agentic account has no account number")
        portfolio = _unwrap(self._call("portfolio", {"account_number": number}))
        if not isinstance(portfolio, dict):
            raise BrokerUnavailable(f"unexpected portfolio payload for the Agentic account: {type(portfolio).__name__}")
        equity = _num(_first(portfolio, "total_value", "equity_value", "portfolio_value"))
        cash = _num(_first(portfolio, "cash", "portfolio_cash"))
        buying_power = _num(_first(portfolio, "buying_power"), cash)
        if equity is None or cash is None:
            raise BrokerUnavailable(
                f"could not read cash and equity for the Agentic account (portfolio keys: "
                f"{sorted(portfolio)}). Refusing to trade against an unknown balance."
            )
        return Account(
            account_id=number, cash=cash, buying_power=buying_power or cash,
            equity=equity,
            trading_blocked=bool(acct.get("deactivated") or acct.get("permanently_deactivated")
                                 or str(acct.get("state", "active")).lower() != "active"),
            shorting_enabled=False, multiplier="1", options_level=0, fractional=True,
            as_of=datetime.now(UTC), raw={"account_number": number},
        )

    def get_positions(self) -> list[BrokerPosition]:
        number = self._account_number(self.agentic_account())
        rows = _rows(self._call("positions", {"account_number": number}), "positions")
        out: list[BrokerPosition] = []
        for row in rows:
            # Read access spans every account; only the Agentic account's positions are tradeable.
            row_account = str(_first(row, "account_number", "account", default=number))
            if number and row_account and row_account != number:
                continue
            symbol = str(_first(row, "symbol", "ticker", "instrument_symbol", default="")).upper()
            qty = _num(_first(row, "quantity", "shares", "qty"))
            if not symbol or qty is None:
                continue
            if qty <= 0:
                continue
            avg = _num(_first(row, "average_buy_price", "average_price", "avg_entry_price", "cost_basis_per_share"), 0.0) or 0.0
            mv = _num(_first(row, "market_value", "equity", "value"), qty * avg) or qty * avg
            out.append(BrokerPosition(symbol, qty, avg, mv, _num(_first(row, "unrealized_pl", "total_return_today"), 0.0) or 0.0))
        return out

    def get_quote(self, symbol: str) -> Quote:
        def market_time(value):
            if not isinstance(value, str):
                return None
            try:
                stamp = datetime.fromisoformat(value)
                return stamp if stamp.tzinfo is not None else None
            except ValueError:
                return None

        rows = _rows(self._call("quotes", {"symbols": [symbol.upper()]}), "quotes")
        for row in rows:
            if str(_first(row, "symbol", "ticker", default=symbol)).upper() != symbol.upper():
                continue
            bid, ask = _num(_first(row, "bid_price", "bid")), _num(_first(row, "ask_price", "ask"))
            last = _num(_first(row, "last_trade_price", "last_price", "price", "mark_price"))
            received = datetime.now(UTC)
            traded = market_time(_first(row, "last_trade_at", "last_trade_time", "last_trade_timestamp"))
            quoted = market_time(_first(row, "quote_timestamp", "updated_at", "timestamp"))
            if bid and ask and quoted:
                return Quote(symbol.upper(), (bid + ask) / 2, quoted, "last_quote_mid",
                             bid=bid, ask=ask, received_at=received)
            if last:
                return Quote(symbol.upper(), last, traded, "last_trade", bid=bid, ask=ask, received_at=received)
            if bid and ask and bid > 0 and ask > 0:
                return Quote(symbol.upper(), (bid + ask) / 2, quoted, "last_quote_mid",
                             bid=bid, ask=ask, received_at=received)
        raise BrokerUnavailable(f"Robinhood returned no usable quote for {symbol}")

    def get_open_orders(self) -> list[BrokerOrder]:
        return [o for o in self._orders() if o.is_open]

    def get_order_by_client_id(self, client_order_id: str) -> BrokerOrder | None:
        ref_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"conservative-trading:{client_order_id}"))
        return self._find_by_ref(ref_id, client_order_id)

    def _orders(self, **filters: Any) -> list[BrokerOrder]:
        number = self._account_number(self.agentic_account())
        rows = _rows(self._call("orders", {"account_number": number, **filters}), "orders")
        return [self._order(r) for r in rows if isinstance(r, dict) and _first(r, "id", "order_id")]

    def get_clock(self) -> Clock:
        """Market session from the NYSE exchange calendar.

        Robinhood's MCP exposes no market-clock tool, so the session comes from
        ``exchange_calendars`` (XNYS), which carries real holidays, half days and session
        times. Nothing here is hand-maintained. If the calendar itself cannot be loaded the
        session is reported closed, because an unknown session must never permit an order.
        """
        import pandas as pd

        now = pd.Timestamp.now(tz="UTC")
        try:
            cal = _nyse_calendar()
            is_open = bool(cal.is_open_on_minute(now, ignore_breaks=True))
            next_open = cal.next_open(now).to_pydatetime() if not is_open else None
            next_close = cal.next_close(now).to_pydatetime() if is_open else None
        except Exception as exc:  # noqa: BLE001 - fail closed: no calendar means no trading
            logger.warning("exchange calendar unavailable, reporting the market closed: %s", exc)
            return Clock(is_open=False, timestamp=now.to_pydatetime())
        return Clock(is_open=is_open, timestamp=now.to_pydatetime(),
                     next_open=next_open, next_close=next_close)

    # ------------------------------------------------------------------ orders
    def review_order(self, request: OrderRequest) -> dict[str, Any]:
        """Robinhood's own pre-trade review: a simulated order returning a quote and alerts.

        Used as an extra layer on top of our Hard Risk Layer, never as a replacement. The venue
        documents it as the default step before placing an order, and it is where buying-power,
        pattern-day-trader and instrument-halt warnings surface.
        """
        if "review_order" not in self.tools:
            return {"available": False, "alerts": [], "detail": "the venue exposes no review tool in this session"}
        payload = _unwrap(self._call("review_order", self._order_args(request)))
        alerts = []
        if isinstance(payload, dict):
            for key in ("alerts", "warnings", "messages", "errors"):
                value = payload.get(key)
                if isinstance(value, list):
                    alerts.extend(str(a if not isinstance(a, dict) else a.get("message", a)) for a in value)
        return {"available": True, "alerts": alerts, "payload": payload}

    def _order_args(self, request: OrderRequest) -> dict[str, Any]:
        """Map our order onto the venue's actual parameters.

        Two constraints come from Robinhood and shape everything: a fractional quantity or a
        dollar amount is accepted only on a market order in regular hours, and time in force is
        'gfd' or 'gtc' rather than 'day'. A $100 account cannot buy a whole share of a broad
        ETF, so fractional is mandatory and therefore so is the market order type.
        """
        number = self._account_number(self.agentic_account())
        args: dict[str, Any] = {
            "account_number": number,
            "symbol": request.symbol.upper(),
            "side": request.side,
            "type": request.order_type,
            "time_in_force": "gtc" if request.time_in_force == "gtc" else "gfd",
            "market_hours": "extended_hours" if request.extended_hours else "regular_hours",
        }
        if request.notional is not None:
            if request.order_type != "market":
                raise BrokerRejection(
                    "Robinhood accepts a dollar amount only on a market order; a limit order needs a "
                    "whole-share quantity"
                )
            args["dollar_amount"] = f"{request.notional:.2f}"
        elif request.quantity is not None:
            args["quantity"] = f"{request.quantity:.6f}".rstrip("0").rstrip(".")
        else:
            raise BrokerRejection("an order needs either a dollar amount or a quantity")
        if request.limit_price is not None and request.order_type in ("limit", "stop_limit"):
            args["limit_price"] = f"{request.limit_price:.2f}"
        return args

    def check_tradable(self, symbol: str) -> tuple[bool, str]:
        """Ask the venue whether the symbol is tradable in this account, including fractional."""
        if "tradability" not in self.tools:
            return True, "no tradability tool in this session"
        number = self._account_number(self.agentic_account())
        rows = _rows(self._call("tradability", {"account_number": number, "symbols": [symbol.upper()]}), "tradability")
        for row in rows:
            if str(_first(row, "symbol", default="")).upper() != symbol.upper():
                continue
            tradable = row.get("tradable")
            fractional = row.get("fractional")
            detail = f"tradable={tradable}, fractional={fractional}"
            if tradable is False:
                return False, f"{symbol} is not tradable in this account ({detail})"
            return True, detail
        return True, f"the venue returned no tradability row for {symbol}"

    def submit_order(self, request: OrderRequest) -> BrokerOrder:
        micro = dict(self.profiles.risk.get("micro_live_execution") or {})
        if not bool(micro.get("enabled", False)):
            raise BrokerError(
                "micro_live_execution.enabled is false: real-money orders are disabled. This is the "
                "master switch and it is not flipped by any other setting."
            )
        acct = self.agentic_account(refresh=True)
        if acct.get("agentic_allowed") is not True:
            raise BrokerError("the resolved account is not agentic_allowed; the venue would reject the order")
        account = self.get_account()
        ceiling = float(micro.get("refuse_if_account_equity_above_usd", self.max_capital * 1.5))
        if account.equity > ceiling:
            raise BrokerError(
                f"the Agentic account holds ${account.equity:,.2f}, above the ${ceiling:,.2f} ceiling for this "
                f"experiment (capital boundary ${self.max_capital:,.2f}). Withdraw the excess before trading."
            )
        if account.trading_blocked:
            raise BrokerError("the Agentic account is not active for trading")

        tradable, tradable_detail = self.check_tradable(request.symbol)
        if not tradable:
            raise BrokerRejection(tradable_detail)

        # Idempotency: ref_id is the venue's key, a UUID derived deterministically from our
        # client order id, so a retry carries the same value and the venue rejects the duplicate.
        ref_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"conservative-trading:{request.client_order_id}"))
        existing = self._find_by_ref(ref_id, request.client_order_id)
        if existing is not None:
            raise DuplicateOrder(f"an order for {request.client_order_id} already exists at Robinhood", existing)

        args = self._order_args(request)
        args["ref_id"] = ref_id
        try:
            payload = self._call("place_order", args)
        except BrokerUnavailable:
            raise
        except BrokerRejection as exc:
            text = str(exc).lower()
            if "duplicate" in text or "ref_id" in text:
                raise DuplicateOrder(str(exc)) from exc
            raise
        rows = _rows(payload, "orders")
        if not rows:
            raise BrokerUnavailable(f"Robinhood accepted the order call but returned no order object: {payload!r}")
        return self._order(rows[0], fallback_client_id=request.client_order_id, ref_id=ref_id)

    def _find_by_ref(self, ref_id: str, client_order_id: str) -> BrokerOrder | None:
        """Look for an order this system already placed, by the venue's idempotency key."""
        for order in self._orders(placed_agent="agentic"):
            if order.client_order_id in (ref_id, client_order_id):
                return order
        return None

    def poll_order(self, order_id: str, timeout_seconds: float = 30.0,
                   interval_seconds: float = 2.0) -> BrokerOrder:
        """Follow an order to a terminal state using the venue's own order-status tool.

        Robinhood answers with the authoritative state: queued, confirmed, partially_filled,
        filled, cancelled or rejected. This waits briefly for a terminal state and otherwise
        returns the latest known one; it never guesses, and a still-pending order is reported
        as pending rather than assumed filled.
        """
        import time as _time

        deadline = _time.monotonic() + timeout_seconds
        latest: BrokerOrder | None = None
        while True:
            rows = _rows(self._call("orders", {
                "account_number": self._account_number(self.agentic_account()), "order_id": order_id,
            }), "orders")
            if rows:
                latest = self._order(rows[0])
                if latest.is_terminal:
                    return latest
            if _time.monotonic() >= deadline:
                break
            _time.sleep(interval_seconds)
        if latest is None:
            raise BrokerUnavailable(f"the venue returned no order for id {order_id}")
        return latest

    def cancel_order(self, order_id: str) -> None:
        if "cancel_order" not in self.tools:
            raise BrokerUnavailable("the Robinhood MCP exposes no cancel tool in this session")
        self._call("cancel_order", {"order_id": order_id})

    @staticmethod
    def _order(row: dict[str, Any], fallback_client_id: str = "", ref_id: str = "") -> BrokerOrder:
        status = str(_first(row, "state", "status", default="")).lower()
        qty = _num(_first(row, "quantity", "shares", "qty"))
        filled = _num(_first(row, "cumulative_quantity", "filled_quantity", "filled_qty"), 0.0) or 0.0
        return BrokerOrder(
            order_id=str(_first(row, "id", "order_id", default="")),
            client_order_id=str(_first(row, "ref_id", "client_order_id", "client_id",
                                       default=ref_id or fallback_client_id)),
            symbol=str(_first(row, "symbol", "ticker", default="")).upper(),
            side=str(_first(row, "side", "direction", default="")).lower(),
            quantity=qty, notional=_num(_first(row, "amount", "dollar_amount")),
            order_type=str(_first(row, "type", "order_type", default="limit")).lower(),
            status=_STATUS_MAP.get(status, status or "new"), filled_quantity=filled,
            filled_avg_price=_num(_first(row, "average_price", "filled_avg_price", "price")),
            limit_price=_num(_first(row, "limit_price", "price")),
            submitted_at=None, reason=str(_first(row, "reject_reason", "cancel_reason", default="")) or None,
        )


def _content_text(result: Any) -> str:
    parts: Iterable[Any] = getattr(result, "content", []) or []
    return "\n".join(str(getattr(p, "text", "")) for p in parts if getattr(p, "text", None))
