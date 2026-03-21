"""Personal assistant over existing research jobs, TradingAgents and the official venue.

Account observations never enter a trading ledger. All helper jobs share the existing durable
worker, cancellation and restart recovery. A deep review reuses the research graph and policy;
only the normal bucket research path can create an approvable order proposal.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlparse

from pydantic import BaseModel, Field

from . import diagnosis
from .broker.base import BrokerError, BrokerUnavailable
from .broker.robinhood import RobinhoodBroker, _first, _quote_rows, _rows, _unwrap
from .llm.claude_code import ClaudeCodeChatModel, ClaudeCodeError
from .llm.untrusted import UNTRUSTED_CONTENT_NOTICE
from .records import write_record

SNAPSHOT = "advisor.snapshot"
NOTES = "advisor.notes"
SYMBOL = re.compile(r"[A-Z][A-Z0-9.\-]{0,19}")
READ_TOOLS = {
    "accounts": ["get_accounts"], "portfolio": ["get_portfolio"],
    "positions": ["get_equity_positions"], "quotes": ["get_equity_quotes"],
    "watchlists": ["get_watchlists"], "watchlist_items": ["get_watchlist_items"],
    "scans": ["get_scans"], "scan_results": ["run_scan"],
}


def now() -> str:
    return datetime.now(UTC).isoformat()


def number(value: Any) -> float | None:
    if isinstance(value, dict):
        value = _first(value, "amount", "value", "buying_power")
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (ValueError, TypeError):
        return None


def age(snapshot: dict) -> float:
    try:
        return (datetime.now(UTC) - datetime.fromisoformat(snapshot["fetched_at"])).total_seconds()
    except (KeyError, ValueError, TypeError):
        return float("inf")


class ObservationBroker(RobinhoodBroker):
    """Use the existing OAuth and transport but expose exclusively observed read capabilities."""

    def __init__(self, profiles):
        super().__init__(profiles, connect=False)
        self.tool_candidates = READ_TOOLS
        self.required = ["accounts", "portfolio", "positions", "quotes"]
        self.connect()

    def _call(self, capability, arguments=None):
        if capability not in READ_TOOLS:
            raise BrokerUnavailable("the observation connection only supports reading")
        return super()._call(capability, arguments)

    def position_rows(self, account_number):
        rows, cursors = [], set()
        args = {"account_number": account_number}
        for _ in range(20):
            raw = self._call("positions", args)
            data = _unwrap(raw)
            rows.extend(_rows(data, "positions"))
            next_page = ((data.get("next") if isinstance(data, dict) else None)
                         or (raw.get("next") if isinstance(raw, dict) else None))
            if not next_page:
                return rows
            cursor = parse_qs(urlparse(str(next_page)).query).get("cursor", [None])[0]
            if not cursor or cursor in cursors:
                raise BrokerUnavailable("unreadable or repeated positions page cursor")
            cursors.add(cursor)
            args = {**args, "cursor": cursor}
        raise BrokerUnavailable("positions exceeded the 20-page observation limit")


def fetch_snapshot(profiles, broker_factory=ObservationBroker) -> dict:
    """Explicitly scoped to all brokerage accounts, with no guessed/missing amounts as zero."""
    result = {"fetched_at": now(), "source": "Robinhood official MCP", "accounts": [],
              "candidates": [], "errors": [], "coverage": "stocks_and_etfs",
              "coverage_note": "Equity positions only; options, crypto and other assets are not "
                               "analysed. The Agentic Account is not observed here: it is the account "
                               "this system manages, shown from its reconciled ledger."}
    candidates: dict[str, dict] = {}

    def add(symbol, source):
        symbol = str(symbol or "").upper()
        if SYMBOL.fullmatch(symbol):
            item = candidates.setdefault(symbol, {"symbol": symbol, "sources": []})
            if source not in item["sources"]:
                item["sources"].append(source)

    # Existing ETF universe remains useful when the user has no saved scans or watchlists.
    for group, symbols in profiles.exposure_groups().items():
        for symbol in symbols:
            add(symbol, "profile:" + group)
    broker = broker_factory(profiles)
    try:
        for account in broker._accounts():
            if account.get("agentic_allowed") is True:
                # The Agentic account is the Micro-Live bucket this system manages. Its holdings
                # are shown from the reconciled ledger and researched by the executable path, so
                # observing it here too would show one account twice and review it twice.
                continue
            account_number = broker._account_number(account)
            if not account_number:
                result["errors"].append("An account had no identifier and could not be read.")
                continue
            account_id = hashlib.sha256(account_number.encode()).hexdigest()[:20]
            view = {"id": account_id, "label": "••••" + account_number[-4:],
                    "positions": [], "complete": False, "equity_usd": None, "cash_usd": None}
            result["accounts"].append(view)
            try:
                totals = _unwrap(broker._call("portfolio", {"account_number": account_number}))
                view["equity_usd"] = number(_first(totals, "total_value", "portfolio_value"))
                view["cash_usd"] = number(_first(totals, "cash", "portfolio_cash"))
                for row in broker.position_rows(account_number):
                    row_account = str(row.get("account_number") or account_number)
                    if row_account != account_number:
                        raise BrokerUnavailable("position response belonged to a different account")
                    symbol = str(_first(row, "symbol", "ticker", default="")).upper()
                    qty = number(_first(row, "quantity", "shares"))
                    if not SYMBOL.fullmatch(symbol) or qty is None:
                        raise BrokerUnavailable("an equity position had missing symbol or quantity")
                    if qty < 0:
                        raise BrokerUnavailable("short positions are outside the advisory scope")
                    if qty == 0:
                        continue
                    cost = number(_first(row, "average_buy_price", "average_price", "avg_entry_price"))
                    view["positions"].append({"symbol": symbol, "quantity": qty,
                                              "average_cost": cost, "price": None,
                                              "market_value_usd": None, "unrealized_pct": None})
                view["complete"] = view["equity_usd"] is not None and view["cash_usd"] is not None
                if not view["complete"]:
                    result["errors"].append(f"Account totals are incomplete for {view['label']}.")
            except (BrokerError, ValueError, TypeError, AttributeError):
                # Venue exceptions may contain account identifiers or payloads; keep them private.
                result["errors"].append(f"Could not completely read account {view['label']}.")

        for capability, key in (("watchlists", "watchlists"), ("scans", "scans")):
            if capability not in broker.tools:
                result["errors"].append(f"Robinhood {capability} are unavailable in this connection.")
                continue
            try:
                collections = _rows(broker._call(capability), key)
                if len(collections) > 20:
                    result["errors"].append(f"Only the first 20 {capability} were read.")
                for collection in collections[:20]:
                    identifier = collection.get("id")
                    name = str(_first(collection, "display_name", "title", default=capability))[:100]
                    if not identifier:
                        continue
                    allowed = collection.get("allowed_object_types")
                    if allowed and not set(allowed).intersection({"instrument", "equity", "stock"}):
                        continue
                    cap = "watchlist_items" if capability == "watchlists" else "scan_results"
                    args = {"list_id": identifier} if cap == "watchlist_items" else {"scan_id": identifier}
                    try:
                        data = _unwrap(broker._call(cap, args))
                        for row in _rows(data, "items", "results", "instruments", "rows"):
                            if row.get("object_type") not in (None, "instrument", "equity", "stock"):
                                continue
                            add(_first(row, "symbol", "ticker"), capability + ":" + name)
                        if isinstance(data, dict) and data.get("next"):
                            result["errors"].append(f"{name}: only the first candidate page was read.")
                    except (BrokerError, ValueError, TypeError):
                        result["errors"].append(f"Could not read candidate source: {name}.")
            except (BrokerError, ValueError, TypeError):
                result["errors"].append(f"Could not read Robinhood {capability}.")

        symbols = list(dict.fromkeys(p["symbol"] for a in result["accounts"] for p in a["positions"]))
        quotes = {}
        for offset in range(0, len(symbols), 20):
            try:
                for row in _quote_rows(broker._call("quotes", {"symbols": symbols[offset:offset + 20]})):
                    quotes[str(row.get("symbol", "")).upper()] = row
            except (BrokerError, ValueError, TypeError):
                result["errors"].append("Some live equity quotes could not be read.")
        for account in result["accounts"]:
            for pos in account["positions"]:
                quote = quotes.get(pos["symbol"], {})
                price = number(_first(quote, "last_trade_price", "last_price", "price"))
                pos["quote_at"] = _first(quote, "venue_last_trade_time", "last_trade_at", "updated_at", "quote_timestamp")
                if price is not None and price > 0:
                    pos["price"] = price
                    pos["market_value_usd"] = price * pos["quantity"]
                    if pos["average_cost"] and pos["average_cost"] > 0:
                        pos["unrealized_pct"] = (price / pos["average_cost"] - 1) * 100
                else:
                    account["complete"] = False
                    result["errors"].append(f"No valid price for {pos['symbol']}.")
                equity = account["equity_usd"]
                pos["weight_pct"] = (pos["market_value_usd"] / equity * 100
                                     if equity and pos["market_value_usd"] is not None else None)
    finally:
        broker.close()
    # Prefer user's candidates, then profile ETFs; preserve source provenance and deduplicate.
    result["candidates"] = sorted(candidates.values(), key=lambda c: (
        not any(not s.startswith("profile:") for s in c["sources"]), c["symbol"]))[:200]
    if len(candidates) > 200:
        result["errors"].append("Candidate coverage is limited to 200 unique stocks/ETFs.")
    result["complete"] = not result["errors"] and all(a["complete"] for a in result["accounts"])
    return result


def profile_context(profiles, notes="") -> dict:
    return {"objective": profiles.investor.get("objective"),
            "horizon": profiles.investor.get("horizon"),
            "risk_tolerance": profiles.investor.get("risk_tolerance"),
            "preferences": profiles.investor.get("preferences"),
            "unresolved_fields": profiles.unresolved,
            "personal_notes": notes,
            "note_boundary": "Personal notes do not change execution permissions or risk limits."}


class CandidateIdea(BaseModel):
    symbol: str
    reason: str


class AssistantReply(BaseModel):
    answer: str = Field(max_length=16000)
    research_candidates: list[CandidateIdea] = Field(default_factory=list, max_length=10)


def readable_answer(text: str, candidates: list[dict] | None = None) -> str:
    """Remove leaked wire-format fields, without turning them into research actions.

    Also used when reading old records: keep their bytes intact, and retain valid
    embedded ideas as prose. Only the separately stored candidate field may create
    buttons; recovering it here would bypass a shortlist's saved-source filter.
    """
    shown = {str(c.get("symbol", "")).upper() for c in candidates or [] if isinstance(c, dict)}

    def ideas(match: re.Match) -> str:
        payload = match[1].strip()
        payload = re.sub(r"^```(?:json)?\s*|\s*```$", "", payload, flags=re.IGNORECASE)
        try:
            rows = json.loads(payload)
        except (ValueError, TypeError):
            return ""
        if not isinstance(rows, list):
            return ""
        lines = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            symbol, reason = row.get("symbol"), row.get("reason")
            if not isinstance(symbol, str) or not isinstance(reason, str):
                continue
            symbol = symbol.upper()
            if not SYMBOL.fullmatch(symbol) or symbol in shown:
                continue
            shown.add(symbol)
            lines.append(f"- {symbol}: {reason.strip()}")
        return "\n\n" + "\n".join(lines) + "\n\n" if lines else ""

    text = re.sub(r"(?:\s*</answer>\s*)?<research_candidates>\s*(.*?)(?:</research_candidates>|$)",
                  ideas, text, flags=re.DOTALL | re.IGNORECASE)
    return re.sub(r"(?:\s*</(?:answer|invoke|research_candidates)>\s*)+$",
                  "", text, flags=re.IGNORECASE).strip()


def answer(profiles, context, message, history, language="zh", llm=None):
    llm = llm or ClaudeCodeChatModel(
        model=profiles.tradingagents.quick_think_llm, effort=profiles.llm.quick_effort,
        timeout_seconds=min(profiles.llm.timeout_seconds, 240), max_turns=8,
        claude_bin=profiles.llm.claude_bin,
        work_dir=str(profiles.project_dir / "runs" / ".advisor_workdir"),
        config_dir=str(profiles.config_dir), role="personal_assistant",
        tool_names=["get_verified_market_snapshot", "get_news", "get_fundamentals", "get_indicators"],
    )
    agentic = (context or {}).get("scope") == "agentic"
    system = (
        ("You are explaining the Agentic account: the real-money account this system manages. "
         "Use only the supplied recorded research, proposals, orders and the effective authority; "
         "there is no personal account in view and you must not speak of one. You explain what was "
         "decided, what was refused and why, and what the authority allows; you cannot start "
         "research, trade, approve, or change candidates, configuration or authority, and you never "
         "suggest that a conversation could. "
         if agentic else
         "You are the user's personal investment assistant. Use the supplied profile, observed "
         "Robinhood accounts, previous conversation and existing research decisions. ")
        + "Reply in " + ("Simplified Chinese" if language == "zh" else "English") + ". "
        + UNTRUSTED_CONTENT_NOTICE + " "
        "Account observations are read-only and distinct from the Demo core/"
        "sandbox buckets. Do not apply a $100 experiment's sizing limits to other accounts. "
        "You cannot trade, approve orders, change settings or start a task yourself. For a request "
        "to research a symbol, include it in research_candidates; the UI offers the existing research "
        "workflow. Never claim actions were executed. Cite existing run IDs and dated data/tool "
        "sources for claims. Put only readable Markdown in answer, and candidate objects only "
        "in the separate research_candidates field; do not embed XML tags or serialized fields. "
        "Explain missing and stale data. Check current facts with the existing "
        "read-only data tools when needed. Review concentration, liquidity needs, taxes and "
        "investment horizon only when known; ask for unknown personal details instead of inventing "
        "them. For shortlist requests rank ONLY the supplied candidate symbols by suitability and "
        "evidence, explain strengths and risks; candidates are research priorities, not approved "
        "trades. Use at most five fresh symbol lookups per reply. For existing holdings cite the "
        "latest full research action if present, otherwise explain that a deep review is needed. "
        "Distinguish HOLD from unreviewed. Entry/exit conditions should come from full research; "
        "never invent target prices or promise to find a top or bottom. A scenario is hypothetical.\n"
        "Any TODO/unresolved field and its recommended_default are unconfirmed suggestions, not "
        "personal facts. Never describe a recommended horizon, allocation or limit as user-confirmed.\n"
        + profiles.mandate_context()
    )
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": "Observed context:\n" + json.dumps(context, ensure_ascii=False)}]
    messages.extend(history[-12:])
    messages.append({"role": "user", "content": message})
    reply = llm.with_structured_output(AssistantReply).invoke(messages)
    reply.research_candidates = [c for c in reply.research_candidates
                                 if SYMBOL.fullmatch(c.symbol.upper())]
    reply.answer = readable_answer(reply.answer, [c.model_dump() for c in reply.research_candidates])
    return reply.model_dump()


class SubjectUnavailable(ValueError):
    """What a review was asked to study is not there to study.

    A scheduled review picks its holdings from the last reading of the owner's accounts, which
    can be days old; the worker then reads the accounts afresh. A holding sold in between is not
    a defect in anything, and on 2026-10-02 two of them ended as a traceback and a "research
    failed" notice. It is its own ending, so the queue can say what happened.
    """


def read_only_research(profiles, job, snapshot, notes):
    """Reuse the full upstream graph and the same policy mapping; never call an order store."""
    from .evidence import check_evidence, collect_evidence
    from .graph import AgenticTradingGraph, classify_instrument
    from .llm.claude_code import USAGE_LOG, reset_usage_log, usage_summary
    from .policy import run_policy
    from .profile import build_tradingagents_config
    from .records import build_record

    ticker, task = job["ticker"], job["task"]
    account_id = task.get("account_id")
    account = next((a for a in snapshot["accounts"] if a["id"] == account_id), None)
    if account_id and (account is None or not account["complete"]):
        raise SubjectUnavailable("Selected account is unavailable or incomplete; refresh it before review.")
    has_position = bool(account and any(p["symbol"] == ticker for p in account["positions"]))
    if account_id and not has_position:
        raise SubjectUnavailable("This holding is no longer present; refresh the portfolio.")
    kind = classify_instrument(ticker, strict=True)
    if kind not in ("stock", "etf"):
        raise ValueError("Read-only reviews currently support stocks and ETFs.")
    context = json.dumps({"scope": "advisory", "read_only": True,
                          "execution_boundary": "No sizing or orders. These observed accounts are not "
                          "the Demo core/sandbox buckets or the $100 live experiment.",
                          "observed_account": account,
                          "all_accounts": snapshot["accounts"], "fetched_at": snapshot["fetched_at"],
                          "profile": profile_context(profiles, notes)}, ensure_ascii=False)
    config = build_tradingagents_config(profiles)
    root = profiles.project_dir / "runs" / job["run_key"]
    # Isolate upstream checkpoints and memories from other accounts and executable research.
    config.update(results_dir=str(root / "upstream"), memory_log_path=None,
                  checkpoint_enabled=False,
                  output_language="Simplified Chinese" if task.get("language", "zh") == "zh" else "English")
    reset_usage_log()
    started = time.monotonic()
    graph = AgenticTradingGraph(profiles, instrument_type=kind, bucket="advisory",
                                     portfolio_text=context, config=config)
    final_state, rating = graph.propagate(ticker, job["trade_date"], asset_type="stock")
    report = graph.save_reports(final_state, ticker, root / "reports")
    policy = run_policy(profiles, final_state, ticker=ticker, instrument_type=kind,
                        bucket="advisory", pm_rating=rating, trade_date=job["trade_date"],
                        has_position=has_position, portfolio_text=context)
    if policy.final_action in ("BUY", "ADD"):
        keys = {"market": "market_report", "news": "news_report", "social": "sentiment_report",
                "fundamentals": "fundamentals_report"}
        evidence = collect_evidence(ticker, job["trade_date"], reports=final_state,
                                    required_reports=[keys[k] for k in profiles.analysts_for(kind)],
                                    bucket="advisory", known_buckets=("advisory",))
        valid, reason = check_evidence(evidence, ticker, job["trade_date"], 1)
        if (not valid or (policy.proposal or {}).get("data_quality") == "poor"
                or (policy.proposal or {}).get("missing_information")):
            policy.final_action = "NO_TRADE"
            policy.reasons.append("Evidence incomplete for an opening recommendation: " + reason)
    record = build_record(
        run_id=job["run_key"], ticker=ticker, instrument_type=kind, bucket="advisory",
        trade_date=job["trade_date"], profiles=profiles, analysts=profiles.analysts_for(kind),
        pm_signal=rating, final_state=final_state, policy=policy, report_path=report,
        state_log_path=None, llm_usage=usage_summary(), llm_calls=list(USAGE_LOG),
        elapsed_seconds=time.monotonic() - started,
        hard_risk={"verdict": "READ_ONLY", "status": "not_applicable_outside_execution_buckets"},
        final_recommendation={"action": policy.final_action, "executable": False,
                              "scope": "read_only", "account_id": account_id},
        portfolio={"snapshot_text": context, "as_of": snapshot["fetched_at"]},
    )
    record["account_id"] = account_id
    record["advisory"] = True
    return record


def run_task(profiles, job):
    from .service import Service

    service = Service(profiles.config_dir)
    db = service.state_db(profiles)
    task = job["task"]
    scope = task.get("scope", "personal")
    account_id = task.get("account_id")
    started = time.monotonic()
    try:
        snapshot = db.read_meta(SNAPSHOT, {}) or {}
        # The Agentic conversation never reads the personal accounts, not even the saved copy.
        if scope != "agentic" and (task["kind"] == "refresh" or age(snapshot) > 300):
            try:
                snapshot = fetch_snapshot(profiles)
                db.write_meta(SNAPSHOT, snapshot)
            except BrokerError:
                if task["kind"] in ("refresh", "analysis"):
                    raise
                snapshot = {**snapshot, "stale": True, "errors": [
                    *snapshot.get("errors", []), "Robinhood refresh unavailable; saved data may be stale."]}
        record = {"run_id": job["run_key"], "ticker": job["ticker"], "bucket": "advisory",
                  "created_at": now(), "trade_date": job["trade_date"], "orders_placed": False,
                  "advisory": True, "task_kind": task["kind"], "scope": scope}
        notes = (db.read_meta(NOTES, {}) or {}).get("text", "")
        # What the model may be told, by the rules the plan set out. A single account sees its
        # own observations and its own records; the all-accounts view sees every account,
        # each labelled; the Agentic view sees the real-money account's records and authority
        # and nothing personal. Records that never said which account they were about are
        # shown in a list but are never put in front of a model.
        if scope == "agentic":
            authority = service.effective_authority()
            context = {"scope": "agentic", "account": {"scope": "agentic", "account_id": "micro_live",
                                                       "account_label": "Agentic"},
                       "profile": profile_context(profiles, notes),
                       "research": [d.model_dump() for d in service.advisor_decisions(
                           limit=40, scope="agentic", include_unknown=False)],
                       "authority": {k: authority.get(k) for k in (
                           "version", "checked_at", "stage", "real_money_enabled", "execution_mode",
                           "kill_switch", "withdrawn", "actions", "pending_changes", "consistency")},
                       "proposals": [p.model_dump() for p in service.proposals(include_closed=True)[:20]]}
            scoped_snapshot = {}
        else:
            scoped_snapshot = dict(snapshot)
            if account_id:
                scoped_snapshot["accounts"] = [a for a in snapshot.get("accounts") or []
                                               if a.get("id") == account_id]
                scoped_snapshot["account_scope"] = "one observed account; other accounts are not in view"
            context = {"scope": "personal",
                       "account": {"scope": "personal", "account_scope_kind": task.get("account_scope_kind"),
                                   "account_id": account_id, "account_ids": task.get("account_ids") or []},
                       "profile": profile_context(profiles, notes), "snapshot": scoped_snapshot,
                       # The model is handed rows, not the console's shape.
                       "research": [d.model_dump() for d in service.advisor_decisions(
                           limit=40, scope="personal", account_id=account_id, include_unknown=False)]}
        if task["kind"] == "refresh":
            record["assistant_result"] = {"answer": "Robinhood data refreshed.", "snapshot": snapshot}
        elif task["kind"] == "analysis":
            record = read_only_research(profiles, job, snapshot, notes)
            record["scope"] = scope
        else:
            # Bound the shortlist, explicitly stating coverage. Never silently call it all-market.
            if scope != "agentic":
                context["snapshot"] = {**scoped_snapshot, "candidates": snapshot.get("candidates", [])[:40],
                                       "candidate_coverage": "first 40 from saved sources and profile ETFs"}
            history = service.advisor_conversation(task.get("conversation_id", "default"))
            messages = [{"role": m.role, "content": m.content} for m in history.messages
                        if m.run_key != job["run_key"]]
            record["assistant_result"] = answer(profiles, context, task["message"], messages,
                                                  task.get("language", "zh"))
            if task["kind"] == "shortlist":
                allowed = {c["symbol"] for c in context["snapshot"]["candidates"]}
                record["assistant_result"]["research_candidates"] = [
                    c for c in record["assistant_result"]["research_candidates"] if c["symbol"].upper() in allowed]
        # Bound to an account scope by the server, from the task, never from the model: a
        # single observed account, or explicitly all of them as the snapshot listed them at
        # the time. A later reader tells this apart from a record that never said.
        record["account_id"] = account_id
        record["account_scope_kind"] = task.get("account_scope_kind") or ("single" if account_id else "all_personal")
        record["account_ids"] = list(task.get("account_ids") or (
            [] if account_id else [a["id"] for a in snapshot.get("accounts") or [] if a.get("id")]))
        # Which route produced it: the read-only analysis runs the TradingAgents graph; the
        # conversation and the refresh are the assistant's own single call.
        record["route"] = {"name": "tradingagents" if task["kind"] == "analysis" else "assistant",
                           "skill_version": None}
        record["elapsed_seconds"] = round(time.monotonic() - started, 1)
        if task["kind"] == "analysis":
            record["diagnosis"] = diagnosis.classify_record(record).as_dict()
        write_record(profiles.project_dir / "runs" / job["run_key"] / "decision_record.json", record)
        return diagnosis.EXIT_OK
    except SubjectUnavailable as exc:
        print(f"[{job['ticker']}] SKIPPED: {exc}", flush=True)
        return diagnosis.EXIT_SUBJECT
    except ClaudeCodeError as exc:
        if exc.rate_limited:
            return diagnosis.EXIT_LIMIT
        if exc.login_expired:
            print(f"[{job['ticker']}] STOPPED: the Claude login on this Mac has expired ({exc}). "
                  "Sign in again (run `claude`, then /login); the task is retried.", flush=True)
            return diagnosis.EXIT_LOGIN
        raise
