"""What a source added, measured on research that was actually run.

The question (docs/todo.md, P2.6) is whether reading a source changes anything for the better,
with the model, the parameters, the capital and the risk held still. This module answers the
part of it that the records can answer today, and says plainly which part they cannot.

**What is measured.** Every decision record says which sources were switched on when it was
made, and holds each read as it happened. Runs are grouped by that set -- a *variant* -- and
for each one: how often each source could be read, and how it failed when it could not; what
the research concluded; and what it cost in calls and tokens of the subscription. Where the
same ticker was researched for the same date under two variants with the same models and the
same upstream, the two are a *pair*, and the difference between their conclusions is the
nearest thing here to an effect. A control run is made with ``ct-research --without-sources``.

**What is recorded by a person.** Whether something a source said turned out to be wrong is
not a thing code can know. It is written down by hand against the run (:func:`flag`) and
counted beside the rest. A count of zero means nobody has flagged anything, not that nothing
was wrong.

**What is not here.** Net return and drawdown. Both sources are read only by the research of
individual stocks, and no plan may buy a stock: there is no account, simulated or real, whose
result a source could have moved. When a plan's universe includes stocks, the strategy version
already separates the variants (``auto.store.source_identity``), and the comparison of two
versions' results under one set of conditions is the one that exists for every other change of
strategy (``auto.comparison``, ``auto.replacement``). Until then the answer is "waiting for
samples", and :func:`summarise` says so rather than printing a figure made some other way.

Nothing here reads a source, a venue or a model. It reads decision records.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import ANSWERED, DISABLED, FORM4, NOT_APPLICABLE, NOT_CONFIGURED, PARTIAL, REDDIT

BASELINE = "baseline"
#: A record made before the sources were written down. Not a variant of anything.
UNRECORDED = "unrecorded"
#: Below these, a difference between two variants is a handful of runs and is reported as that.
MIN_RUNS = 20
MIN_PAIRS = 10
FLAGS = "sources.flags"
MAX_FLAGS = 500
#: Outcomes that are not attempts: nothing was asked, so nothing can have failed.
NOT_ATTEMPTED = (DISABLED, NOT_APPLICABLE, NOT_CONFIGURED)


def load_records(runs_dir: str | Path, *, since: str | None = None) -> list[dict[str, Any]]:
    """Every decision record under a runs directory, oldest first. Unreadable ones are skipped."""
    out = []
    for path in sorted(Path(runs_dir).glob("*/decision_record.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(record, dict) and (since is None or str(record.get("trade_date") or "") >= since):
            out.append(record)
    return sorted(out, key=lambda r: str(r.get("created_at") or ""))


def _sources(record: dict[str, Any]) -> dict[str, Any] | None:
    block = (record.get("data_sources") or {}).get("research_sources")
    return block if isinstance(block, dict) else None


def variant_of(record: dict[str, Any]) -> str:
    """Which sources this run could read: "baseline", or their names joined with a plus."""
    block = _sources(record)
    if block is None:
        return UNRECORDED
    return "+".join(sorted(str(name) for name in block.get("switched_on") or [])) or BASELINE


def conditions_of(record: dict[str, Any]) -> tuple[Any, ...]:
    """What two runs must share to be a pair: the question, the models and the upstream."""
    models = record.get("models") or {}
    return (str(record.get("ticker") or "").upper(), record.get("trade_date"),
            record.get("instrument_type"), record.get("bucket"),
            models.get("deep"), models.get("quick"), record.get("upstream_tradingagents_commit"))


def _conclusion(record: dict[str, Any]) -> dict[str, Any]:
    policy = record.get("policy") or {}
    return {"run_id": record.get("run_id"),
            "action": (record.get("final_recommendation") or {}).get("action"),
            "policy_action": policy.get("final_action"), "confidence": policy.get("confidence")}


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 2) if values else None


def _reads(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for record in records:
        for reading in (_sources(record) or {}).get("readings") or []:
            entry = out.setdefault(str(reading.get("source")), {"reads": 0, "outcomes": Counter(),
                                                                "items": 0})
            entry["reads"] += 1
            entry["outcomes"][str(reading.get("outcome"))] += 1
            entry["items"] += len(reading.get("items") or [])
    for entry in out.values():
        outcomes = entry["outcomes"]
        attempted = sum(n for code, n in outcomes.items() if code not in NOT_ATTEMPTED)
        answered = sum(outcomes.get(code, 0) for code in ANSWERED)
        failed = attempted - answered - outcomes.get(PARTIAL, 0)
        entry.update(outcomes=dict(sorted(outcomes.items())), attempted=attempted, answered=answered,
                     partial=outcomes.get(PARTIAL, 0), failed=failed,
                     failure_rate_pct=round(failed / attempted * 100, 1) if attempted else None,
                     items_per_answered_read=round(entry["items"] / answered, 2) if answered else None)
    return dict(sorted(out.items()))


def _usage(records: list[dict[str, Any]]) -> dict[str, Any]:
    def per_run(key: str) -> float | None:
        return _mean([float((r.get("llm_usage") or {}).get(key) or 0) for r in records])

    return {"calls_per_run": per_run("calls"), "input_tokens_per_run": per_run("input_tokens"),
            "output_tokens_per_run": per_run("output_tokens"),
            "tool_calls_per_run": per_run("tool_calls"), "wall_seconds_per_run": per_run("wall_seconds"),
            # What the same calls would have cost at list prices. Nothing was billed: the calls
            # draw on the subscription. It is here because it is the one figure that adds up
            # input and output in a single number.
            "list_price_equivalent_usd_per_run": per_run("list_price_equivalent_usd")}


def flags(db: Any) -> list[dict[str, Any]]:
    return list((db.read_meta(FLAGS, {}) or {}).get("flags") or [])


def flag(db: Any, *, run_id: str, source: str, note: str, identity: str = "local-operator",
         now: datetime | None = None) -> dict[str, Any]:
    """Write down that something a source said in a run turned out to be wrong.

    A person's judgement, recorded as one: who, when, about which run and source, and why.
    """
    if source not in (REDDIT, FORM4):
        raise ValueError(f"no such source: {source}")
    if not str(run_id).strip() or not str(note).strip():
        raise ValueError("a flag needs the run it is about and what was wrong")
    row = {"run_id": str(run_id).strip(), "source": source, "note": str(note).strip()[:500],
           "by": identity, "at": (now or datetime.now(UTC)).isoformat(timespec="seconds")}
    with db.transaction() as tx:
        kept = list((db.read_meta(FLAGS, {}) or {}).get("flags") or [])
        kept.append(row)
        db.write_meta(FLAGS, {"flags": kept[-MAX_FLAGS:]}, tx)
    return row


def summarise(records: list[dict[str, Any]], *, flagged: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Runs by variant, the pairs among them, and whether there is enough to say anything."""
    by_variant: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        by_variant.setdefault(variant_of(record), []).append(record)
    wrong: dict[str, Counter[str]] = {}
    run_variant = {str(r.get("run_id")): variant_of(r) for r in records}
    for row in flagged or []:
        where = run_variant.get(str(row.get("run_id")))
        if where is not None:
            wrong.setdefault(where, Counter())[str(row.get("source"))] += 1

    variants = {}
    for name, runs in sorted(by_variant.items()):
        confidences = [float(c["confidence"]) for c in map(_conclusion, runs)
                       if isinstance(c["confidence"], int | float)]
        variants[name] = {
            "runs": len(runs),
            "tickers": sorted({str(r.get("ticker") or "").upper() for r in runs}),
            "instrument_types": dict(Counter(str(r.get("instrument_type")) for r in runs)),
            "conclusions": {
                "actions": dict(Counter(str(_conclusion(r)["action"]) for r in runs)),
                "policy_actions": dict(Counter(str(_conclusion(r)["policy_action"]) for r in runs)),
                "mean_confidence": _mean(confidences),
                "why": dict(Counter(str((r.get("diagnosis") or {}).get("code")) for r in runs))},
            "usage": _usage(runs), "sources": _reads(runs),
            "flagged_wrong": dict(wrong.get(name, {})),
        }

    # Pairs: one question, the same models, the same upstream -- and a different set of sources.
    grouped: dict[tuple[Any, ...], dict[str, dict[str, Any]]] = {}
    for record in records:
        name = variant_of(record)
        if name != UNRECORDED:
            # The newest run of a variant for a question is the one that stands for it.
            grouped.setdefault(conditions_of(record), {})[name] = record
    rows, effects = [], {}
    for conditions, found in sorted(grouped.items(), key=lambda kv: [str(x) for x in kv[0]]):
        if BASELINE not in found or len(found) < 2:
            continue
        base = _conclusion(found[BASELINE])
        row = {"ticker": conditions[0], "trade_date": conditions[1], BASELINE: base, "with": {}}
        for name, record in sorted(found.items()):
            if name == BASELINE:
                continue
            other = _conclusion(record)
            both = all(isinstance(c["confidence"], int | float) for c in (base, other))
            delta = round(float(other["confidence"]) - float(base["confidence"]), 2) if both else None
            row["with"][name] = {**other, "action_changed": other["action"] != base["action"],
                                 "confidence_points": delta}
            effect = effects.setdefault(name, {"pairs": 0, "action_changed": 0, "_deltas": []})
            effect["pairs"] += 1
            effect["action_changed"] += 1 if other["action"] != base["action"] else 0
            if delta is not None:
                effect["_deltas"].append(delta)
        rows.append(row)
    for effect in effects.values():
        effect["mean_confidence_points"] = _mean(effect.pop("_deltas"))

    treated = {name: v for name, v in variants.items() if name not in (BASELINE, UNRECORDED)}
    enough = (variants.get(BASELINE, {}).get("runs", 0) >= MIN_RUNS
              and any(v["runs"] >= MIN_RUNS for v in treated.values())
              and any(e["pairs"] >= MIN_PAIRS for e in effects.values()))
    if enough:
        verdict = {"state": "comparable",
                   "zh": "两种信息来源配置下都有足够的研究记录和配对，可以比较研究结论的差异；仍然不是收益比较。",
                   "en": "There are enough runs and pairs under both sets of sources to compare what "
                         "research concluded. It is still not a comparison of returns."}
    else:
        verdict = {"state": "waiting_for_samples",
                   "zh": f"等待样本：每种配置至少 {MIN_RUNS} 次研究、至少 {MIN_PAIRS} 组同题配对之后，"
                         "才谈得上差异。现在的数字只是已有记录的计数。",
                   "en": f"Waiting for samples: a difference means something only after {MIN_RUNS} "
                         f"runs under each set of sources and {MIN_PAIRS} pairs on the same question. "
                         "The figures here are counts of what exists."}
    return {
        "runs": len(records), "variants": variants,
        "pairs": {"count": len(rows), "rows": rows, "effects": effects},
        "returns": {
            "available": False,
            "zh": "没有收益和回撤的比较：这两个来源只进入个股研究，而目前没有任何方案可以买个股，"
                  "所以不存在一个会被它们影响的账户结果。等待样本，并且依赖个股扩展。",
            "en": "No comparison of return or drawdown. Both sources reach the research of "
                  "individual stocks only, and no plan may buy a stock, so there is no account "
                  "whose result they could have moved. Waiting for samples, and for the stock "
                  "extension."},
        "verdict": verdict,
        "needed": {"runs_per_variant": MIN_RUNS, "pairs": MIN_PAIRS},
    }
