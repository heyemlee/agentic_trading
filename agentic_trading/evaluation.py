"""A forward record of what this system decided, and what a passive alternative would have done.

The test suite proves the software behaves. It says nothing about whether multi-agent research
is worth its cost, and no amount of engineering evidence ever will: only a continuous sample,
fixed in advance, can answer that.

Three properties make the record worth reading.

It is declared before it runs. Paper capital, universe, schedule, risk profile and model
versions are frozen when a study starts, and every report says whether the configuration has
drifted since. Choosing the rules after seeing the outcome is how a strategy is talked into
looking good.

It counts everything. A record of trades alone would be a record of the occasions this system
chose to act, which is a biased sample of its own judgement. NO_TRADE decisions, rejected
proposals, expired proposals, failed runs and runs that stopped on a subscription limit all
carry information about the cost and reliability of the approach, so all of them are counted.

It excludes deposits from return. Equity rises when money is paid in, and a comparison that
missed that would flatter the strategy and the benchmark differently, because they receive the
same deposits at different prices. Both sides are converted to a unit value that is continuous
across a cash flow, which is what makes their returns and drawdowns comparable at all.
"""

from __future__ import annotations

import hashlib
import json
import statistics
from datetime import UTC, datetime
from itertools import pairwise
from typing import Any

STUDY = "evaluation_study"
SNAPSHOTS = "evaluation_snapshots"
MAX_SNAPSHOTS = 2000
#: An arbitrary starting unit count. Only ratios of unit value are ever reported.
STARTING_UNITS = 1000.0

#: One study per scope, and the buckets each scope counts. The paper book and the real-money
#: account are separate samples: they hold different money, under different rules, over
#: different periods, and a figure that mixed them would describe neither. Rows recorded before
#: a bucket was written down belong to the paper book, which was the only study there was.
SCOPES: dict[str, tuple[str, ...]] = {"demo": ("core", "sandbox"), "agentic": ("micro_live",)}
DEFAULT_SCOPE = "demo"
ACCOUNTS: dict[str, dict[str, Any]] = {
    "demo": {"scope": "demo", "account_id": "conceptual", "account_label": "Demo", "is_real_money": False},
    "agentic": {"scope": "agentic", "account_id": "micro_live", "account_label": "Agentic",
                "is_real_money": True},
}
#: A snapshot this many sessions behind the calendar is a record nobody is keeping up.
STALE_AFTER_SESSIONS = 2


class EvaluationError(RuntimeError):
    """The study cannot be started, advanced or reported on as asked."""


def scope_keys(scope: str) -> tuple[str, str]:
    """The state documents a scope's study and snapshots live in.

    The paper book keeps the keys from before scopes existed, so a study already running
    there goes on being the same study rather than starting over under a new name.
    """
    if scope not in SCOPES:
        raise EvaluationError(f"unknown evaluation scope {scope!r}; one of {', '.join(SCOPES)}")
    if scope == DEFAULT_SCOPE:
        return STUDY, SNAPSHOTS
    return f"{STUDY}:{scope}", f"{SNAPSHOTS}:{scope}"


def in_scope(row: dict[str, Any], scope: str) -> bool:
    """Whether a research, job, proposal or fill row belongs to a scope's sample."""
    bucket = row.get("bucket")
    if scope == DEFAULT_SCOPE:
        return bucket is None or bucket in SCOPES[scope]
    return bucket in SCOPES[scope]


def running(db, scope: str) -> bool:
    study_key, _ = scope_keys(scope)
    study = db.read_meta(study_key, None)
    return bool(study) and study.get("state") == "RUNNING"


def settings(profiles) -> dict[str, Any]:
    cfg = dict(profiles.risk.get("evaluation") or {})
    cfg.setdefault("enabled", True)
    cfg.setdefault("benchmark", {"VTI": 60, "BND": 40})
    cfg.setdefault("observation_days", 30)
    cfg.setdefault("minimum_days_for_judgement", 180)
    cfg.setdefault("stop_rules", {})
    return cfg


def fingerprint(profiles) -> dict[str, Any]:
    """What must not change during a study, and a hash of it.

    Reported rather than enforced: a person may have a good reason to change the rules, and the
    honest response is to say the sample is no longer homogeneous, not to pretend it is.
    """
    watched = {
        "buckets": profiles.risk.get("buckets"),
        "drawdown_ladder": profiles.risk.get("drawdown_ladder"),
        "sizing": profiles.risk.get("sizing"),
        "confidence_requirements": profiles.risk.get("confidence_requirements"),
        "strategy_policy": profiles.risk.get("strategy_policy"),
        "research_schedule": profiles.risk.get("research_schedule"),
        "universe": (profiles.investor.get("universe") or {}),
        "models": (profiles.investor.get("llm") or {}),
    }
    blob = json.dumps(watched, sort_keys=True, default=str)
    return {"sha256": hashlib.sha256(blob.encode()).hexdigest()[:16], "sections": sorted(watched)}


def _price(symbol: str, as_of: str) -> tuple[float, str]:
    from .portfolio import upstream_price_lookup

    return upstream_price_lookup(symbol, as_of)


def start(db, profiles, *, on: str, equity_usd: float, net_deposits_usd: float,
          note: str = "", scope: str = DEFAULT_SCOPE) -> dict[str, Any]:
    """Declare a study. Refuses to replace a running one without an explicit stop."""
    cfg = settings(profiles)
    study_key, snapshots_key = scope_keys(scope)
    existing = db.read_meta(study_key, None)
    if existing and existing.get("state") == "RUNNING":
        raise EvaluationError(
            f"study {existing['study_id']} has been running since {existing['started_on']}; "
            "stop it before starting another, and keep its report")

    weights = {str(k).upper(): float(v) for k, v in (cfg.get("benchmark") or {}).items()}
    total = sum(weights.values())
    if not weights or total <= 0:
        raise EvaluationError("evaluation.benchmark must name at least one symbol with a weight")

    holdings, prices = {}, {}
    for symbol, weight in weights.items():
        try:
            price, priced_on = _price(symbol, on)
        except Exception as exc:   # a benchmark that cannot be priced is not a study
            raise EvaluationError(f"cannot price benchmark {symbol} on {on}: {exc}") from exc
        holdings[symbol] = equity_usd * (weight / total) / price
        prices[symbol] = {"price": price, "as_of": priced_on}

    study = {
        "study_id": f"{on}-{hashlib.sha256(f'{scope}:{on}'.encode()).hexdigest()[:6]}",
        "scope": scope,
        "buckets": list(SCOPES[scope]),
        "state": "RUNNING",
        "started_on": on,
        "started_at": datetime.now(UTC).isoformat(),
        "note": note,
        "declared": {
            "paper_capital_usd": round(equity_usd, 2),
            "net_deposits_usd": round(net_deposits_usd, 2),
            "benchmark_weights_pct": {k: round(v / total * 100, 2) for k, v in weights.items()},
            "observation_days": int(cfg.get("observation_days", 30)),
            "minimum_days_for_judgement": int(cfg.get("minimum_days_for_judgement", 180)),
            "stop_rules": dict(cfg.get("stop_rules") or {}),
        },
        "frozen": fingerprint(profiles),
        "benchmark_holdings": holdings,
        "opening_prices": prices,
    }
    db.write_meta(study_key, study)
    db.write_meta(snapshots_key, {"snapshots": []})
    return study


def stop(db, reason: str, scope: str = DEFAULT_SCOPE) -> dict[str, Any]:
    study_key, _ = scope_keys(scope)
    study = db.read_meta(study_key, None)
    if not study:
        raise EvaluationError("no study has been started")
    study.update({"state": "STOPPED", "stopped_at": datetime.now(UTC).isoformat(),
                  "stop_reason": reason})
    db.write_meta(study_key, study)
    return study


def _advance(units: float, equity: float, flow: float) -> tuple[float, float]:
    """Units and unit value after a cash flow, so the unit value is continuous across it.

    Money paid in buys units at the value they had immediately before it arrived. This is what
    keeps a deposit from registering as a gain, on either side of the comparison.
    """
    before = equity - flow
    if units <= 0 or before <= 0:
        return (STARTING_UNITS, equity / STARTING_UNITS) if equity > 0 else (STARTING_UNITS, 0.0)
    value_before = before / units
    units_after = units + (flow / value_before if value_before else 0.0)
    return units_after, (equity / units_after if units_after else 0.0)


def snapshot(db, profiles, *, on: str, equity_usd: float,
             net_deposits_usd: float, scope: str = DEFAULT_SCOPE) -> dict[str, Any]:
    """Mark both sides to market for one day. Idempotent per date."""
    study_key, snapshots_key = scope_keys(scope)
    study = db.read_meta(study_key, None)
    if not study or study.get("state") != "RUNNING":
        raise EvaluationError(f"no {scope} study is running; start one first")

    record = db.read_meta(snapshots_key, {"snapshots": []}) or {"snapshots": []}
    series = [row for row in record.get("snapshots", []) if row["date"] != on]
    previous = max(series, key=lambda row: row["date"]) if series else None

    holdings = dict(study["benchmark_holdings"])
    flow = round(net_deposits_usd - (previous["net_deposits_usd"] if previous
                                     else study["declared"]["net_deposits_usd"]), 2)

    benchmark_equity, missing = 0.0, []
    weights = study["declared"]["benchmark_weights_pct"]
    for symbol, units in holdings.items():
        try:
            price, _priced_on = _price(symbol, on)
        except Exception as exc:  # noqa: BLE001 - one unpriced symbol must not lose the day
            missing.append(f"{symbol}: {type(exc).__name__}")
            continue
        # The same deposit reaches the benchmark on the same day, at that day's price.
        if flow:
            units += flow * (weights[symbol] / 100.0) / price
            holdings[symbol] = units
        benchmark_equity += units * price

    if missing:
        # A partial mark would understate the benchmark and flatter the strategy.
        return {"date": on, "recorded": False, "missing_prices": missing}

    strategy_units, strategy_value = _advance(
        previous["strategy_units"] if previous else STARTING_UNITS, equity_usd, flow)
    benchmark_units, benchmark_value = _advance(
        previous["benchmark_units"] if previous else STARTING_UNITS, benchmark_equity, flow)

    row = {
        "date": on,
        "at": datetime.now(UTC).isoformat(),
        "net_deposits_usd": round(net_deposits_usd, 2),
        "flow_usd": flow,
        "strategy_equity_usd": round(equity_usd, 2),
        "strategy_units": strategy_units,
        "strategy_unit_value": strategy_value,
        "benchmark_equity_usd": round(benchmark_equity, 2),
        "benchmark_units": benchmark_units,
        "benchmark_unit_value": benchmark_value,
        "config_drifted": fingerprint(profiles)["sha256"] != study["frozen"]["sha256"],
    }
    series.append(row)
    series.sort(key=lambda item: item["date"])
    record["snapshots"] = series[-MAX_SNAPSHOTS:]
    db.write_meta(snapshots_key, record)
    study["benchmark_holdings"] = holdings
    db.write_meta(study_key, study)
    return {**row, "recorded": True}


def _drawdown(values: list[float]) -> float:
    peak, worst = None, 0.0
    for value in values:
        peak = value if peak is None else max(peak, value)
        if peak > 0:
            worst = min(worst, (value / peak - 1.0) * 100.0)
    return round(worst, 2)


def _return_pct(series: list[dict[str, Any]], key: str) -> float | None:
    if len(series) < 2:
        return None
    first, last = series[0][key], series[-1][key]
    return round((last / first - 1.0) * 100.0, 2) if first else None


def _decisions(research: list[dict[str, Any]], jobs: list[dict[str, Any]],
               proposals: list[dict[str, Any]], since: str) -> dict[str, Any]:
    """Every outcome, including the ones that produced nothing.

    A record of trades alone would describe only the occasions this system chose to act, which
    is a biased sample of its own judgement. The caller narrows the rows to one scope's
    buckets; personal-account observations are refused here as well, whoever calls, because
    they were never part of any declared study.
    """
    research = [row for row in research if row.get("bucket") != "advisory"]
    jobs = [row for row in jobs if row.get("bucket") != "advisory"]
    runs = [row for row in research if str(row.get("trade_date") or "") >= since]
    actions: dict[str, int] = {}
    incomplete_data = 0
    for row in runs:
        actions[str(row.get("final_action") or "NO_RESULT")] = 1 + actions.get(
            str(row.get("final_action") or "NO_RESULT"), 0)
        if row.get("data_quality") not in (None, "complete") or row.get("missing_information"):
            incomplete_data += 1
    states: dict[str, int] = {}
    for job in jobs:
        if str(job.get("created_at") or "") >= since:
            states[job["state"]] = 1 + states.get(job["state"], 0)
    statuses: dict[str, int] = {}
    latencies = []
    for proposal in proposals:
        if str(proposal.get("created_at") or "") < since:
            continue
        statuses[proposal["status"]] = 1 + statuses.get(proposal["status"], 0)
        if proposal.get("approved_at"):
            try:
                decided = (datetime.fromisoformat(proposal["approved_at"])
                           - datetime.fromisoformat(proposal["created_at"])).total_seconds()
                latencies.append(round(decided / 60, 1))
            except (TypeError, ValueError):
                pass
    return {
        "research_runs": len(runs),
        "run_outcomes": actions,
        "job_states": states,
        "proposal_statuses": statuses,
        "runs_with_incomplete_data": incomplete_data,
        "incomplete_data_rate_pct": round(incomplete_data / len(runs) * 100, 1) if runs else None,
        "llm_calls": sum(int(row.get("llm_calls") or 0) for row in runs),
        "research_seconds": round(sum(float(row.get("elapsed_seconds") or 0) for row in runs), 1),
        "human_decision_minutes": {
            "count": len(latencies),
            "median": round(statistics.median(latencies), 1) if latencies else None,
            "total": round(sum(latencies), 1) if latencies else 0.0,
        },
    }


def _execution(fills: list[dict[str, Any]], proposals: list[dict[str, Any]],
               average_equity: float) -> dict[str, Any]:
    reference = {p["proposal_id"]: p.get("reference_price") for p in proposals}
    traded, slippage = 0.0, []
    for fill in fills:
        quantity = float(fill.get("quantity") or 0)
        price = float(fill.get("price") or 0)
        traded += abs(quantity * price)
        expected = reference.get(fill.get("proposal_id"))
        if expected:
            slippage.append(round((price / float(expected) - 1.0) * 10_000, 1))
    return {
        "fills": len(fills),
        "traded_usd": round(traded, 2),
        "turnover_pct_of_equity": round(traded / average_equity * 100, 2) if average_equity else None,
        "slippage_bps": {
            "count": len(slippage),
            "median": round(statistics.median(slippage), 1) if slippage else None,
        },
        # No venue in use charges commission on equities, so the cost that shows up is the
        # difference between the price the analysis used and the price that filled.
        "commission_usd": 0.0,
    }


def _sample(series: list[dict[str, Any]], started_on: str | None,
            today: str | None = None) -> dict[str, Any]:
    """How continuous the record is: where sessions went unmarked, and whether it is current.

    Gaps and staleness are counted in exchange sessions, from the calendar, so a weekend or a
    holiday is not a gap. Without the calendar both answers are unknown, and say so.
    """
    dates = [row["date"] for row in series]
    out: dict[str, Any] = {"started_on": started_on, "last_snapshot_on": dates[-1] if dates else None,
                           "days_recorded": len(dates), "gaps": [], "stale": None, "basis": "sessions"}
    try:
        import exchange_calendars

        calendar = exchange_calendars.get_calendar("XNYS")
    except Exception as exc:  # noqa: BLE001 - no calendar, no claim about continuity
        out["basis"] = f"unknown: exchange calendar unavailable ({type(exc).__name__})"
        return out
    for earlier, later in pairwise(dates):
        missing = len(calendar.sessions_in_range(earlier, later)) - 2
        if missing > 0:
            out["gaps"].append({"from": earlier, "to": later, "missing_sessions": missing,
                                "reason": "no snapshot"})
    if dates:
        today = today or datetime.now(UTC).date().isoformat()
        behind = max(0, len(calendar.sessions_in_range(dates[-1], today)) - 1)
        out["stale"] = behind > STALE_AFTER_SESSIONS
        out["sessions_behind"] = behind
    return out


def _not_started(scope: str, *, research, jobs, proposals, fills, profiles) -> dict[str, Any]:
    """The answer when no study was declared: the record's counts, and no performance at all."""
    cfg = settings(profiles)
    weights = {str(k).upper(): float(v) for k, v in (cfg.get("benchmark") or {}).items()}
    total = sum(weights.values()) or 1.0
    return {
        "scope": scope, "account": dict(ACCOUNTS[scope]), "buckets": list(SCOPES[scope]),
        "study_id": None, "state": "NOT_STARTED", "started_on": None, "declared": None,
        "days_recorded": 0, "config_drifted": False,
        "frozen_fingerprint": None, "current_fingerprint": fingerprint(profiles)["sha256"],
        "strategy": {"return_pct": None, "max_drawdown_pct": None, "equity_usd": None,
                     "net_deposits_usd": None},
        "benchmark": {"weights_pct": {k: round(v / total * 100, 2) for k, v in weights.items()},
                      "return_pct": None, "max_drawdown_pct": None, "equity_usd": None},
        "strategy_minus_benchmark_pct_points": None,
        "decisions": _decisions(research, jobs, proposals, ""),
        "execution": _execution(fills, proposals, 0.0),
        "stop_rules_triggered": [],
        "sample": _sample([], None),
        "readable": False, "conclusive": False,
        "caveat": (f"no {ACCOUNTS[scope]['account_label']} study has been declared; decisions are "
                   "counted from the record, performance is not"),
    }


def report(db, profiles, *, research, jobs, proposals, fills,
           scope: str = DEFAULT_SCOPE, today: str | None = None) -> dict[str, Any]:
    """The whole record: what it decided, what it cost, and how it compares. Never a summary
    of the good days, and never a figure that mixes the paper book with the real account."""
    study_key, snapshots_key = scope_keys(scope)
    research = [row for row in research if in_scope(row, scope)]
    jobs = [row for row in jobs if in_scope(row, scope)]
    proposals = [row for row in proposals if in_scope(row, scope)]
    fills = [row for row in fills if in_scope(row, scope)]
    study = db.read_meta(study_key, None)
    if not study:
        return _not_started(scope, research=research, jobs=jobs, proposals=proposals, fills=fills,
                            profiles=profiles)
    series = (db.read_meta(snapshots_key, {"snapshots": []}) or {}).get("snapshots", [])
    days = len(series)
    declared = study["declared"]

    strategy_return = _return_pct(series, "strategy_unit_value")
    benchmark_return = _return_pct(series, "benchmark_unit_value")
    lead = (round(strategy_return - benchmark_return, 2)
            if strategy_return is not None and benchmark_return is not None else None)
    equities = [row["strategy_equity_usd"] for row in series]
    decisions = _decisions(research, jobs, proposals, study["started_on"])

    stop_rules = declared.get("stop_rules") or {}
    drawdown = _drawdown([row["strategy_unit_value"] for row in series])
    triggered = []
    if stop_rules.get("max_drawdown_pct") is not None and drawdown <= float(stop_rules["max_drawdown_pct"]):
        triggered.append(f"drawdown {drawdown}% reached the declared stop of "
                         f"{stop_rules['max_drawdown_pct']}%")
    if (stop_rules.get("trails_benchmark_pct_points") is not None and lead is not None
            and lead <= float(stop_rules["trails_benchmark_pct_points"])):
        triggered.append(f"trailing the benchmark by {abs(lead)} points, past the declared "
                         f"{abs(float(stop_rules['trails_benchmark_pct_points']))}")

    enough = days >= int(declared.get("minimum_days_for_judgement", 180))
    return {
        "scope": scope,
        "account": dict(ACCOUNTS[scope]),
        "buckets": list(SCOPES[scope]),
        "study_id": study["study_id"],
        "state": study["state"],
        "started_on": study["started_on"],
        "declared": declared,
        "days_recorded": days,
        "config_drifted": any(row.get("config_drifted") for row in series),
        "frozen_fingerprint": study["frozen"]["sha256"],
        "current_fingerprint": fingerprint(profiles)["sha256"],
        "strategy": {
            "return_pct": strategy_return,
            "max_drawdown_pct": drawdown,
            "equity_usd": series[-1]["strategy_equity_usd"] if series else None,
            "net_deposits_usd": series[-1]["net_deposits_usd"] if series else None,
        },
        "benchmark": {
            "weights_pct": declared["benchmark_weights_pct"],
            "return_pct": benchmark_return,
            "max_drawdown_pct": _drawdown([row["benchmark_unit_value"] for row in series]),
            "equity_usd": series[-1]["benchmark_equity_usd"] if series else None,
        },
        "strategy_minus_benchmark_pct_points": lead,
        "decisions": decisions,
        "execution": _execution(fills, proposals,
                                sum(equities) / len(equities) if equities else 0.0),
        "stop_rules_triggered": triggered,
        "sample": _sample(series, study["started_on"], today),
        # Stated on every report, so a short sample cannot be read as a verdict.
        "readable": days >= int(declared.get("observation_days", 30)),
        "conclusive": enough,
        "caveat": (
            f"{days} day(s) recorded. "
            + ("This is a record of what happened, not evidence about the strategy: "
               f"{declared.get('minimum_days_for_judgement', 180)} days were declared as the "
               "minimum before it can support a judgement." if not enough else
               "Long enough to read against the declared minimum, and still a single sample "
               "from one market period.")
            + (" Configuration changed during the study, so the sample is not homogeneous."
               if any(row.get("config_drifted") for row in series) else "")),
    }


def route_of(row: dict[str, Any]) -> str:
    """Which research route a record or job ran on.

    A record that names its route is believed. Before records carried one, every run of a
    trading bucket and every personal ``analysis`` went through the TradingAgents graph, and
    every other personal task (a chat, a shortlist, a refresh) through the assistant's single
    call; that is what the code did, so it is what is stated.
    """
    route = row.get("route")
    if isinstance(route, dict) and route.get("name"):
        return str(route["name"])
    task_kind = (row.get("task") or {}).get("kind") or row.get("task_kind")
    if row.get("task") or row.get("bucket") == "advisory":
        return "tradingagents" if task_kind == "analysis" else "assistant"
    return "tradingagents"


def route_quality(research: list[dict[str, Any]], jobs: list[dict[str, Any]], *,
                  now: datetime | None = None) -> dict[str, Any]:
    """What the record alone can say about each research route, and what it cannot.

    Timing, model calls, failures and the evidence check are read from the jobs and records.
    Fact accuracy, format compliance and the amount of human editing come from the fixed-case
    review the plan describes, and stay null until it has run: null means not evaluated, never
    zero, and a route is never ranked on a number nobody measured.
    """
    routes: dict[str, dict[str, Any]] = {}

    def entry(name: str) -> dict[str, Any]:
        return routes.setdefault(name, {"route": name, "runs": 0, "complete": 0, "failed": 0,
                                        "timed_out": 0, "limit_hits": 0,
                                        "_elapsed": [], "_calls": 0, "_evidence": []})

    since: str | None = None

    def earliest(stamp: Any) -> None:
        nonlocal since
        day = str(stamp or "")[:10]
        if day and (since is None or day < since):
            since = day

    for job in jobs:
        row = entry(route_of(job))
        row["runs"] += 1
        state = job.get("state")
        if state == "COMPLETE":
            row["complete"] += 1
        elif state == "FAILED":
            row["failed"] += 1
        elif state == "TIMED_OUT":
            row["timed_out"] += 1
        if state == "CLAUDE_LIMIT_REACHED" or int(job.get("limit_recoveries") or 0) > 0:
            row["limit_hits"] += 1
        earliest(job.get("created_at"))
    for record in research:
        name = route_of(record)
        row = entry(name)
        if record.get("elapsed_seconds") is not None:
            row["_elapsed"].append(float(record["elapsed_seconds"]))
        row["_calls"] += int(record.get("llm_calls") or 0)
        if name == "tradingagents":
            row["_evidence"].append(record.get("data_quality") == "good"
                                    and not record.get("missing_information"))
        earliest(record.get("created_at") or record.get("trade_date"))

    out = []
    for name in sorted(routes):
        row = routes[name]
        elapsed, calls, evidence = row.pop("_elapsed"), row.pop("_calls"), row.pop("_evidence")
        out.append({
            **row,
            "median_elapsed_seconds": round(statistics.median(elapsed), 1) if elapsed else None,
            "llm_calls_total": calls,
            "format_pass_rate_pct": None,
            "evidence_check_pass_rate_pct": (round(sum(evidence) / len(evidence) * 100, 1)
                                             if evidence else None),
            "fact_accuracy_pct": None,
            "human_edit_rate_pct": None,
            "evaluated": {"timing": bool(elapsed), "calls": calls > 0, "failures": row["runs"] > 0,
                          "format": False, "evidence": bool(evidence), "facts": False, "edits": False},
        })
    return {
        "scope": "route_quality",
        "checked_at": (now or datetime.now(UTC)).isoformat(),
        "since": since,
        "routes": out,
        "note": ("fact accuracy and human edits come from the fixed-case review (plan section 4); "
                 "null means not evaluated, not zero"),
    }
