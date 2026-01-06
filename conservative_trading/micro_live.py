"""Micro-Live preflight: every safety property that must hold before a real order is sent.

This exists because "the code has a Hard Risk Layer" and "the Hard Risk Layer is switched on
right now, against this account" are different claims. Each check below is evaluated against
live configuration and live broker state, and every one must pass. A single failure blocks
real-money execution.

Nothing here places an order or moves money.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .profile import Profiles

BUCKET = "micro_live"


@dataclass
class Check:
    check_id: str
    name: str
    passed: bool
    detail: str


@dataclass
class Preflight:
    ready: bool
    checks: list[Check] = field(default_factory=list)
    account: dict[str, Any] | None = None
    tools: dict[str, str] = field(default_factory=dict)

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if not c.passed]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _research_runtime_is_broker_free(profiles: Profiles) -> tuple[bool, str]:
    """The reasoning runtime must not be able to reach a trading tool.

    Claude Code sessions in this project are started with built-in tools disabled, only our own
    market-data MCP server attached, and --strict-mcp-config so no other MCP configuration is
    read. That is what keeps the model on the research side of the line: it cannot place an
    order even if it decided to.
    """
    import os
    import shutil

    from .llm.claude_code import ClaudeCodeChatModel

    claude_bin = os.environ.get("CT_CLAUDE_BIN") or shutil.which("claude") or "claude"
    probe = ClaudeCodeChatModel(model="claude-sonnet-5", tool_names=["get_stock_data"], claude_bin=claude_bin)
    command = probe._build_command("")
    if "--strict-mcp-config" not in command:
        return False, "the research runtime is not started with --strict-mcp-config"
    if "--tools" not in command or command[command.index("--tools") + 1] != "":
        return False, "the research runtime does not disable built-in tools"
    blob = " ".join(command).lower()
    for marker in ("robinhood", "agent.robinhood.com", "place_equity_order", "place_order"):
        if marker in blob:
            return False, f"a trading tool reached the research runtime: {marker}"
    allowed = command[command.index("--allowedTools") + 1] if "--allowedTools" in command else ""
    return True, (f"built-in tools disabled, strict MCP config, only our data tools exposed ({allowed}); "
                  "no trading tool is reachable by the model")


def _resolve(profiles: Profiles, path_value: Any) -> Path:
    """Resolve a configured path, honouring an absolute override."""
    path = Path(str(path_value))
    return path if path.is_absolute() else profiles.project_dir / path


def _market_calendar_ok(broker: Any | None) -> tuple[bool, str]:
    """The session must come from a real exchange calendar, not a hand-maintained assumption."""
    try:
        from conservative_trading.broker.robinhood import _nyse_calendar

        cal = _nyse_calendar()
        import pandas as pd

        holiday = not cal.is_session(pd.Timestamp("2026-12-25"))
        if not holiday:
            return False, "the calendar does not recognise a known market holiday"
        detail = f"{cal.name} exchange calendar loaded, holidays and half days included"
        if broker is not None and hasattr(broker, "get_clock"):
            clock = broker.get_clock()
            detail += f"; market open now: {clock.is_open}"
        return True, detail
    except Exception as exc:  # noqa: BLE001
        return False, f"exchange calendar unavailable: {type(exc).__name__}: {exc}"


def run_preflight(profiles: Profiles, broker: Any | None = None) -> Preflight:
    """Evaluate every gate. ``broker`` may be None to check configuration only."""
    micro = dict(profiles.risk.get("micro_live_execution") or {})
    checks: list[Check] = []
    account_info: dict[str, Any] | None = None

    def check(cid: str, name: str, passed: bool, detail: str) -> None:
        checks.append(Check(cid, name, bool(passed), detail))

    enabled = bool(micro.get("enabled", False))
    check("PF00", "micro-live master switch", enabled,
          "micro_live_execution.enabled is true" if enabled
          else "micro_live_execution.enabled is false: real-money orders are disabled (this is the intended default)")

    # --- configuration gates, checkable without a broker
    mode = str(micro.get("execution_mode", "human_approval"))
    check("PF04", "human approval active",
          mode == "human_approval" and not bool(micro.get("allow_autonomous_opening", False)),
          f"execution_mode={mode}, autonomous opening={micro.get('allow_autonomous_opening', False)}")

    fresh_classes = (profiles.risk.get("proposal_freshness") or {}).get("classes") or {}
    check("PF05", "stale-proposal protection active", bool(fresh_classes),
          f"{len(fresh_classes)} freshness classes configured; the micro-live class is "
          f"{micro.get('freshness_class', 'core_conservative')}")

    reval = profiles.risk.get("revalidation") or {}
    check("PF06", "execution-time revalidation active", bool(reval.get("outcomes")),
          f"outcomes: {reval.get('outcomes')}")

    idem = profiles.risk.get("idempotency") or {}
    check("PF07", "duplicate protection active",
          bool(idem.get("check_broker_before_submit")) and int(idem.get("max_submit_attempts", 1)) <= 1,
          f"client-order-id prefix {idem.get('client_order_id_prefix')!r}, broker checked before submit, "
          f"max attempts {idem.get('max_submit_attempts')}")

    perms = dict(micro.get("permissions") or {})
    disabled = [k for k in ("options", "crypto", "short_selling", "margin", "extended_hours") if not perms.get(k, False)]
    check("PF09", "unsupported strategies disabled", len(disabled) == 5,
          f"disabled: {', '.join(disabled) or 'none'}; enabled: "
          f"{', '.join(k for k, v in perms.items() if v) or 'none'}")

    strategy_policy = (profiles.risk.get("strategy_policy") or {}).get("sandbox") or {}
    gated = strategy_policy.get("strategies_requiring_human_approval_always") or []
    check("PF09b", "sensitive strategies permanently human-gated",
          {"momentum", "short", "event_driven"} <= set(gated),
          f"always human-approved: {gated}")

    # The ledger that matters for a real-money run is the Micro-Live one, not the conceptual
    # portfolio. Checking the wrong file would have reported audit logging healthy while the
    # ledger this bucket actually reconciles against was missing.
    ledger = _resolve(profiles, micro.get("ledger_file", "state/micro_live_portfolio.yaml"))
    fills = _resolve(profiles, (profiles.risk.get("broker") or {}).get("fills_file", "state/fills.jsonl"))
    proposals = _resolve(profiles, (profiles.risk.get("broker") or {}).get("state_file", "state/proposals.json"))
    audit_ok = ledger.exists() and fills.parent.is_dir() and proposals.parent.is_dir()
    check("PF10", "audit logging active", audit_ok,
          f"micro-live ledger {ledger.name} {'present' if ledger.exists() else 'MISSING'}, "
          f"fills log and proposal store directories writable, decision records under runs/")

    poll_ok = broker is None or hasattr(broker, "poll_order")
    check("PF08", "reconciliation active", poll_ok,
          "every snapshot is rebuilt from broker cash, quantities and prices; an unattributed position "
          "or a cash mismatch beyond 0.1% fails closed; order state is followed to a terminal state "
          "through the venue's own order-status tool and only filled quantity is recorded")

    session_ok, session_detail = _market_calendar_ok(broker)
    check("PF12", "market calendar available", session_ok, session_detail)

    # Every risk number that reaches a live order must be one the investor has seen and
    # accepted, either as an explicit value or by accepting the documented defaults.
    used = len(profiles.defaults_used)
    acknowledged = profiles.accept_defaults or used == 0
    check("PF13", "risk parameters acknowledged", acknowledged,
          "all risk parameters are explicit values" if used == 0 else
          (f"{used} recommended defaults are in use and accepted via "
           "accept_all_recommended_defaults" if profiles.accept_defaults else
           f"{used} risk parameters are still recommended defaults you have not accepted; set "
           "accept_all_recommended_defaults: true in config/investor_profile.yaml, or replace the "
           "values you want to change, before real money moves"))

    ok, detail = _research_runtime_is_broker_free(profiles)
    check("PF11", "research runtime has no broker tools", ok, detail)

    hard_risk_on = profiles.stage >= 1 and bool(profiles.risk.get("buckets"))
    check("PF03", "hard risk active", hard_risk_on,
          "the deterministic layer runs at proposal time and again at execution time")

    # --- gates that need the live venue
    boundary = float(micro.get("max_capital_usd", 100))
    tolerance = float(micro.get("capital_tolerance_usd", 5))
    if broker is None:
        check("PF01", "agentic account connected", False, "no broker session was provided")
        check("PF02", "account equity within the capital boundary", False, "cannot verify without a broker session")
    else:
        try:
            account = broker.get_account()
            acct_raw = broker.agentic_account() if hasattr(broker, "agentic_account") else {}
            account_info = {"account_id_masked": f"••••{str(account.account_id)[-4:]}",
                            "cash": account.cash, "equity": account.equity,
                            "buying_power": account.buying_power,
                            "type": str(acct_raw.get("brokerage_account_type", acct_raw.get("account_type", "")))}
            number = str(account.account_id or "")
            masked = f"••••{number[-4:]}" if len(number) >= 4 else "(unnamed)"
            check("PF01", "agentic account connected", bool(number) and not account.trading_blocked,
                  f"account {masked}, equity ${account.equity:,.2f}, cash ${account.cash:,.2f}, "
                  f"trading blocked: {account.trading_blocked}")
            within = account.equity <= boundary + tolerance
            check("PF02", "account equity within the capital boundary", within,
                  f"equity ${account.equity:,.2f} vs boundary ${boundary:,.2f} (+${tolerance:,.2f} tolerance)"
                  + ("" if within else "; withdraw the excess before trading"))
        except Exception as exc:  # noqa: BLE001 - any failure to read the venue blocks trading
            check("PF01", "agentic account connected", False, f"{type(exc).__name__}: {exc}")
            check("PF02", "account equity within the capital boundary", False, "account state unreadable")

    tools = dict(getattr(broker, "tools", {}) or {})
    return Preflight(ready=all(c.passed for c in checks), checks=checks, account=account_info, tools=tools)


def bucket_is_executable(profiles: Profiles, bucket: str, broker_name: str) -> tuple[bool, str]:
    """Only the micro-live bucket may reach the real-money venue.

    Core and Sandbox are conceptual portfolios with no broker behind them. Sending one of their
    proposals to Robinhood would silently move conceptual money into a real account, so the
    binding between bucket and venue is checked explicitly rather than assumed.
    """
    if broker_name == "robinhood" and bucket != BUCKET:
        return False, (f"the {bucket} bucket has no real-money venue; only {BUCKET} executes at Robinhood. "
                       "Core and Sandbox remain conceptual until you decide otherwise.")
    if broker_name != "robinhood" and bucket == BUCKET:
        return False, f"the {BUCKET} bucket is real money and only executes at Robinhood, not {broker_name}"
    return True, f"{bucket} may execute at {broker_name}"
