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
from typing import Any

STUDY = "evaluation_study"
SNAPSHOTS = "evaluation_snapshots"
MAX_SNAPSHOTS = 2000
#: An arbitrary starting unit count. Only ratios of unit value are ever reported.
STARTING_UNITS = 1000.0


class EvaluationError(RuntimeError):
    """The study cannot be started, advanced or reported on as asked."""


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
          note: str = "") -> dict[str, Any]:
    """Declare a study. Refuses to replace a running one without an explicit stop."""
    cfg = settings(profiles)
    existing = db.read_meta(STUDY, None)
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
        "study_id": f"{on}-{hashlib.sha256(on.encode()).hexdigest()[:6]}",
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
    db.write_meta(STUDY, study)
    db.write_meta(SNAPSHOTS, {"snapshots": []})
    return study


def stop(db, reason: str) -> dict[str, Any]:
    study = db.read_meta(STUDY, None)
    if not study:
        raise EvaluationError("no study has been started")
    study.update({"state": "STOPPED", "stopped_at": datetime.now(UTC).isoformat(),
                  "stop_reason": reason})
    db.write_meta(STUDY, study)
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
             net_deposits_usd: float) -> dict[str, Any]:
    """Mark both sides to market for one day. Idempotent per date."""
    study = db.read_meta(STUDY, None)
    if not study or study.get("state") != "RUNNING":
        raise EvaluationError("no study is running; start one first")

    record = db.read_meta(SNAPSHOTS, {"snapshots": []}) or {"snapshots": []}
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
    db.write_meta(SNAPSHOTS, record)
    study["benchmark_holdings"] = holdings
    db.write_meta(STUDY, study)
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
    is a biased sample of its own judgement.
    """
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


def report(db, profiles, *, research, jobs, proposals, fills) -> dict[str, Any]:
    """The whole record: what it decided, what it cost, and how it compares. Never a summary
    of the good days."""
    study = db.read_meta(STUDY, None)
    if not study:
        raise EvaluationError("no study has been started; run 'ct-evaluate start'")
    series = (db.read_meta(SNAPSHOTS, {"snapshots": []}) or {}).get("snapshots", [])
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
