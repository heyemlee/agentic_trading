"""``ct-robinhood``: connect to, inspect, and preflight the Robinhood Agentic account.

``connect``    run the one-time OAuth flow in a browser and store the token locally
``status``     show the bound tools, your accounts, and which one is the Agentic account
``preflight``  run every safety gate that must pass before a real order

None of these place an order or move money.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import socket
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .micro_live import run_preflight
from .profile import ProfileError, load_profiles

CALLBACK_PORT = 8765


def _print(msg: str) -> None:
    print(msg, flush=True)


def _make_server(port: int, handler_cls: type[BaseHTTPRequestHandler]) -> ThreadingHTTPServer:
    """Create a local HTTP server that accepts both IPv4 and IPv6 localhost connections."""
    try:
        class IPv6Server(ThreadingHTTPServer):
            address_family = socket.AF_INET6

            def server_bind(self):
                try:
                    self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
                except (AttributeError, OSError):
                    pass
                super().server_bind()

        return IPv6Server(("::", port), handler_cls)
    except OSError:
        return ThreadingHTTPServer(("127.0.0.1", port), handler_cls)


class _CallbackServer:
    """Catches the single OAuth redirect on localhost, then stops."""

    def __init__(self, port: int = CALLBACK_PORT):
        self.code: str | None = None
        self.state: str | None = None
        self.iss: str | None = None
        self._event = threading.Event()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                params = parse_qs(urlparse(self.path).query)
                outer.code = (params.get("code") or [None])[0]
                outer.state = (params.get("state") or [None])[0]
                outer.iss = (params.get("iss") or [None])[0]
                body = (b"<h3>Robinhood connected.</h3><p>You can close this tab and return to the terminal.</p>"
                        if outer.code else b"<h3>No authorization code received.</h3>")
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(body)
                outer._event.set()

            def log_message(self, *args):  # silence the default stderr logging
                return

        self._server = _make_server(port, Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()

    def wait(self, timeout: float = 300.0) -> tuple[str, str | None, str | None]:
        if not self._event.wait(timeout):
            raise TimeoutError("timed out waiting for the Robinhood authorization redirect")
        if not self.code:
            raise RuntimeError("Robinhood did not return an authorization code")
        return self.code, self.state, self.iss


def _connect(profiles) -> int:
    from .broker.robinhood import RobinhoodBroker

    _print("Connecting to Robinhood Agentic Trading.")
    _print("A browser window will open. Sign in to Robinhood and approve access for this agent.")
    _print("Robinhood opens a dedicated Agentic account during this flow; fund it with at most "
           f"${float((profiles.risk.get('micro_live_execution') or {}).get('max_capital_usd', 100)):,.0f}.\n")

    with _CallbackServer() as callback:
        async def redirect_handler(url: str) -> None:
            _print(f"Opening: {url}")
            webbrowser.open(url)

        async def callback_handler():
            from mcp.client.auth import AuthorizationCodeResult

            code, state, iss = await asyncio.to_thread(callback.wait)
            return AuthorizationCodeResult(code=code, state=state, iss=iss)

        try:
            broker = RobinhoodBroker(profiles, redirect_handler=redirect_handler,
                                     callback_handler=callback_handler)
        except Exception as exc:  # noqa: BLE001
            _print(f"connection failed: {exc}")
            return 1
    _print("\nconnected. tools bound:")
    for capability, tool in sorted(broker.tools.items()):
        _print(f"  {capability:14s} -> {tool}")
    unbound = [t for t in broker.available_tools if t not in set(broker.tools.values())]
    if unbound:
        _print(f"  other tools offered by the server: {', '.join(unbound)}")
    broker.close()
    _print("\nnext: uv run ct-robinhood status")
    return 0


def _status(profiles) -> int:
    from .broker.robinhood import RobinhoodBroker

    try:
        broker = RobinhoodBroker(profiles)
    except Exception as exc:  # noqa: BLE001
        _print(f"not connected: {exc}")
        return 1
    try:
        _print(f"MCP: {broker.url}")
        _print(f"tools bound: {broker.tools}")
        _print(f"all tools offered: {', '.join(broker.available_tools)}")
        try:
            accounts = broker._accounts()
            _print(f"\naccounts visible (read access spans all of them): {len(accounts)}")
            for a in accounts:
                number = str(a.get("account_number") or a.get("rhs_account_number") or "")
                masked = f"••••{number[-4:]}" if len(number) >= 4 else "(unknown)"
                agentic = "AGENTIC" if a.get("agentic_allowed") else "        "
                nickname = f" nickname={a["nickname"]!r}" if a.get("nickname") else ""
                _print(f"  {masked}  {agentic}  brokerage={a.get("brokerage_account_type", "?")}  "
                       f"trading_type={a.get("type", "?")}{nickname}")
        except Exception as exc:  # noqa: BLE001
            _print(f"could not list accounts: {exc}")
        try:
            acct = broker.agentic_account(refresh=True)
            number = str(acct.get("account_number") or "")
            _print(f"\nAgentic account (the only one that may trade): ••••{number[-4:]}")
            account = broker.get_account()
            _print(f"  equity ${account.equity:,.2f} | cash ${account.cash:,.2f} | buying power ${account.buying_power:,.2f}")
            boundary = float((profiles.risk.get("micro_live_execution") or {}).get("max_capital_usd", 100))
            if account.equity > boundary:
                _print(f"  WARNING: equity is above the ${boundary:,.0f} boundary for this experiment")
        except Exception as exc:  # noqa: BLE001
            _print(f"\nAgentic account not identified: {exc}")
            return 1
    finally:
        broker.close()
    return 0


def _preflight(profiles) -> int:
    broker = None
    try:
        from .broker.robinhood import RobinhoodBroker

        broker = RobinhoodBroker(profiles)
    except Exception as exc:  # noqa: BLE001
        _print(f"(no broker session: {exc})\nrunning configuration-only checks\n")
    try:
        pf = run_preflight(profiles, broker)
        for c in pf.checks:
            _print(f"  {'PASS' if c.passed else 'FAIL'} {c.check_id:6s} {c.name}: {c.detail}")
        _print(f"\nready for a real order: {'YES' if pf.ready else 'NO'}")
        if not pf.ready:
            _print("blocking: " + ", ".join(f"{c.check_id}" for c in pf.failures))
    finally:
        if broker is not None:
            broker.close()
    return 0 if pf.ready else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ct-robinhood",
                                     description="Connect and inspect the Robinhood Agentic account. Places no orders.")
    parser.add_argument("command", choices=["connect", "status", "preflight"])
    parser.add_argument("--config-dir", default="config")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        profiles = load_profiles(args.config_dir)
    except ProfileError as exc:
        _print(f"profile error: {exc}")
        return 2
    return {"connect": _connect, "status": _status, "preflight": _preflight}[args.command](profiles)


if __name__ == "__main__":
    raise SystemExit(main())
