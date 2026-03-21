"""A record of one experiment that can be checked against what it was made from.

The approach is borrowed from how Vibe-Trading keeps its backtests (a run card per experiment,
figures traced back to the artefacts they were computed from, the evaluation window kept apart
from the data a strategy was built on); none of its code is used, and none of it runs here.
What is kept for each experiment (docs/todo.md, P2.2 and P2.3):

**What exactly was run.** The strategy version -- its parameters, the models it reasons with,
a digest of the trading logic -- the window, the data and its digest, the fill rule, the cost
model, and the conditions every comparator shared.

**What it was made from.** Every row's whole ledger is written beside the card, and the card
carries each file's hash. ``verify`` recomputes every figure on the card from those files. A
card whose figures cannot be reproduced from its own artefacts is not evidence, and says so.

**Whether it is a test at all.** A strategy's parameters were fixed on a day. Data up to that
day is what it was built on; only what came after can test it. And a window that has already
been used to compare several variants has been used to choose among them, so its result for
the one chosen is a selection, not a test. Both are recorded on the card as facts about it --
``sample`` and ``earlier_trials_on_window`` -- and ``independent`` is true only when neither
applies. Nothing a person or a model writes can set it.

**What it cannot say.** The limits are on the card: what the backtest does not model, what
the prices are, and that the research a strategy acts on comes from a language model that may
have read the period being tested, which is why research is replayed only as it was recorded
live and never regenerated for a past date.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..storage import atomic_write
from . import metrics, store

REGISTRY = "auto.experiments"
BACKTEST = "backtest"
#: The one experiment a candidate is put to before it may take over: itself and the strategy in
#: force over the same research, money, prices and costs (``auto.replacement``).
REPLACEMENT = "replacement"
IN_SAMPLE, OUT_OF_SAMPLE, MIXED, UNKNOWN = "in_sample", "out_of_sample", "mixed", "unknown"
MAX_REGISTERED = 2000

def _both(zh: str, en: str) -> tuple[str, str]:
    return zh, en


LIMITS: tuple[tuple[str, str], ...] = (
    _both("回测只模拟策略自身的规则（可买范围、仓位数、仓位大小、止损、止盈、持有期）和资金上限，"
          "没有逐条重放硬风控：品种档位上限、每日和每周的开仓节奏、行情与回撤暂停都不在其内。",
          "The backtest models the strategy's own rules -- what it may buy, how many, how large, "
          "its stop, target and holding period -- and the cap on capital. It does not replay the "
          "Hard Risk rules one by one: tier caps, the daily and weekly pace, and the regime and "
          "drawdown pauses are not in it."),
    _both("价格是复权日线，分红的影响已含在价格里；实时模拟按当日实际价格成交、不计分红。"
          "两者口径不同，不能相加，也不能直接比较。",
          "Prices are adjusted daily bars, with distributions inside them; the live simulation "
          "fills at the day's own prices and credits no dividends. The two are different measures "
          "and are neither added together nor compared directly."),
    _both("按决定之后的下一个开盘价成交。盘中的止损不被模拟：跌破止损线后跳空，按跳空后的开盘价成交。",
          "Fills are at the next open after the decision. A stop inside the session is not "
          "modelled: a gap through the stop fills at the open after the gap."),
    _both("价差和滑点是假设值，尚未用真实成交校准。",
          "The spread and the slippage are assumptions, not yet calibrated against real fills."),
)

MODEL_CAVEAT = _both(
    "研究结论来自语言模型，模型的训练数据可能已经包含被回测的这段历史。因此不能在过去的日期上重新运行模型来"
    "“回测”研究——那样的结果看过答案。这里只重放当时实时记录下来的研究结论；没有记录的区间，研究驱动的策略"
    "就没有回测结果。",
    "The research a strategy acts on comes from a language model whose training data may include "
    "the period being tested. Research is therefore never regenerated for a past date -- a result "
    "made that way has seen the answer. Only conclusions recorded live at the time are replayed, "
    "and where none were recorded a research-driven strategy has no backtest result at all.")


def lineage_of(db, version_id: str | None) -> str | None:
    """The first ancestor of a version: every variant adjusted from the same start shares it."""
    seen, current = set(), version_id
    while current and current not in seen:
        seen.add(current)
        parent = (store.version(db, current) or {}).get("parent")
        if not parent:
            return current
        current = parent
    return current


def sample_of(*, start: str, end: str, designed_on: str | None) -> dict[str, Any]:
    """Which part of a window a strategy could have been built on, and which part could test it."""
    if not designed_on:
        return {"sample": UNKNOWN, "designed_on": None, "held_out_from": None,
                "says": {"zh": "不知道这个策略是哪一天定下来的，所以无法判断这段数据是否参与了设计。",
                         "en": "The day this strategy was fixed is not on record, so whether this "
                               "window was part of designing it cannot be said."}}
    designed_on = str(designed_on)[:10]
    if start > designed_on:
        return {"sample": OUT_OF_SAMPLE, "designed_on": designed_on, "held_out_from": start,
                "says": {"zh": f"整段区间都在策略定下来（{designed_on}）之后，没有参与设计。",
                         "en": f"The whole window is after the strategy was fixed ({designed_on}); "
                               "none of it was available when it was designed."}}
    if end <= designed_on:
        return {"sample": IN_SAMPLE, "designed_on": designed_on, "held_out_from": None,
                "says": {"zh": f"整段区间都在策略定下来（{designed_on}）之前。这是设计时已经存在的数据，"
                               "只能说明规则在已见数据上的表现，不是独立验证。",
                         "en": f"The whole window is on or before the day the strategy was fixed "
                               f"({designed_on}). It is data that already existed then: it shows how "
                               "the rules do on data they could have been fitted to, and is not an "
                               "independent test."}}
    return {"sample": MIXED, "designed_on": designed_on, "held_out_from": None,
            "says": {"zh": f"区间跨过了策略定下来的日期（{designed_on}），其中一部分是设计时已有的数据，"
                           "整体不能算独立验证。",
                     "en": f"The window straddles the day the strategy was fixed ({designed_on}); "
                           "part of it existed then, so as a whole it is not an independent test."}}


def registered(db) -> list[dict[str, Any]]:
    return list((db.read_meta(REGISTRY, {}) or {}).get("cards") or [])


def earlier_trials(db, *, lineage: str | None, version_id: str, start: str, end: str,
                   card_id: str | None = None) -> list[str]:
    """Other variants of the same strategy already tried on a window that overlaps this one."""
    tried = {row["version_id"] for row in registered(db)
             if row.get("lineage") == lineage and row.get("version_id") != version_id
             and row.get("card_id") != card_id
             and str(row.get("start")) <= end and str(row.get("end")) >= start}
    return sorted(tried)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False)


def build(db, *, kind: str, version: dict[str, Any], start: str, end: str, data: dict[str, Any],
          conditions: dict[str, Any], rows: dict[str, dict[str, Any]],
          differences: dict[str, Any] | None = None, now: datetime | None = None) -> dict[str, Any]:
    """The card for one experiment. What it says about independence is computed, not supplied."""
    version_id = version["version_id"]
    lineage = lineage_of(db, version_id)
    card_id = "xp-" + hashlib.sha256(_canonical({
        "kind": kind, "version": version_id, "start": start, "end": end,
        "data": data.get("digest"), "conditions": conditions}).encode()).hexdigest()[:12]
    design = sample_of(start=start, end=end, designed_on=version.get("data_as_of"))
    others = earlier_trials(db, lineage=lineage, version_id=version_id, start=start, end=end,
                            card_id=card_id)
    design["earlier_trials_on_window"] = others
    design["independent"] = design["sample"] == OUT_OF_SAMPLE and not others
    if others:
        design["selection"] = {
            "zh": f"同一策略的另外 {len(others)} 个版本已经在与此重叠的区间上试过。在同一段数据上比较多个版本"
                  "就是在用它做选择，选出来的那个在这段数据上的成绩不能当作独立验证。",
            "en": f"{len(others)} other variant(s) of this strategy have already been run on a "
                  "window overlapping this one. Comparing variants on the same data is choosing "
                  "with it, and the chosen one's result on that data is not an independent test."}
    code = version.get("code") or {}
    return {
        "card_id": card_id, "kind": kind,
        "recorded_at": (now or datetime.now(UTC)).isoformat(),
        "version_id": version_id, "lineage": lineage, "mode": version.get("mode"),
        "strategy": version.get("strategy"), "code": code,
        "window": {"start": start, "end": end, "sessions": conditions.get("sessions")},
        "design": design, "data": data, "conditions": conditions, "rows": rows,
        "differences": differences or {},
        "model": {"deep": code.get("deep_model"), "quick": code.get("quick_model"),
                  "training_cutoff": None,
                  "caveat": {"zh": MODEL_CAVEAT[0], "en": MODEL_CAVEAT[1]}},
        "limits": [{"zh": zh, "en": en} for zh, en in LIMITS],
        "artifacts": {},
    }


def write(root: Path, card: dict[str, Any], ledgers: dict[str, dict[str, Any]]) -> Path:
    """Write the card and every ledger it was computed from. Returns the experiment's directory."""
    directory = Path(root) / card["card_id"]
    directory.mkdir(parents=True, exist_ok=True)
    artifacts = {}
    for name, ledger in sorted(ledgers.items()):
        text = _canonical(ledger)
        atomic_write(directory / f"{name}.json", text)
        artifacts[name] = {"file": f"{name}.json",
                           "sha256": hashlib.sha256(text.encode()).hexdigest()}
    card["artifacts"] = artifacts
    atomic_write(directory / "card.json", json.dumps(card, indent=2, ensure_ascii=False, default=str,
                                                     allow_nan=False))
    return directory


def register(db, card: dict[str, Any]) -> None:
    """Note that this version was tried on this window. Once per card, however often it is run."""
    with db.transaction() as conn:
        record = db.read_meta(REGISTRY, {}) or {}
        cards = [row for row in record.get("cards") or [] if row.get("card_id") != card["card_id"]]
        cards.append({"card_id": card["card_id"], "kind": card["kind"],
                      "version_id": card["version_id"], "lineage": card["lineage"],
                      "start": card["window"]["start"], "end": card["window"]["end"],
                      "sample": card["design"]["sample"], "recorded_at": card["recorded_at"]})
        db.write_meta(REGISTRY, {"cards": cards[-MAX_REGISTERED:]}, conn)


def verify(directory: Path) -> list[str]:
    """Recompute a card's figures from its own artefacts. Empty when every one is reproduced.

    Each entry is one thing that does not hold: a ledger that is missing or is not the file the
    card recorded, or a figure the ledger does not produce. This is what makes a number on a
    card a number about something.
    """
    directory = Path(directory)
    try:
        card = json.loads((directory / "card.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"the card cannot be read: {type(exc).__name__}"]
    wrong: list[str] = []
    opening = (card.get("conditions") or {}).get("opening_cash_usd")
    for name, row in (card.get("rows") or {}).items():
        figures = row.get("figures")
        if figures is None:
            continue
        artifact = (card.get("artifacts") or {}).get(name)
        if not artifact:
            wrong.append(f"{name}: figures with no ledger behind them")
            continue
        try:
            text = (directory / artifact["file"]).read_text(encoding="utf-8")
        except OSError:
            wrong.append(f"{name}: the ledger file is missing")
            continue
        if hashlib.sha256(text.encode()).hexdigest() != artifact.get("sha256"):
            wrong.append(f"{name}: the ledger is not the file the card recorded")
            continue
        ledger = json.loads(text)
        again = metrics.summarize(ledger.get("equity") or [], fills=ledger.get("fills") or [],
                                  trades=ledger.get("trades") or [],
                                  skipped=ledger.get("skipped") or [], opening_usd=opening)
        for key, value in figures.items():
            if again.get(key) != value:
                wrong.append(f"{name}.{key}: the card says {value!r}, the ledger gives {again.get(key)!r}")
    return wrong


def summary(card: dict[str, Any], directory: Path | None = None) -> dict[str, Any]:
    """What is attached to the strategy version: enough to find the card and read its standing."""
    return {"card_id": card["card_id"], "kind": card["kind"], "window": card["window"],
            "sample": card["design"]["sample"], "independent": card["design"]["independent"],
            "earlier_trials_on_window": len(card["design"]["earlier_trials_on_window"]),
            "data_digest": (card.get("data") or {}).get("digest"),
            "figures": {name: {key: (row.get("figures") or {}).get(key)
                               for key in ("net_return_pct", "max_drawdown_pct", "turnover_pct",
                                           "costs_usd", "trades_closed")}
                        for name, row in (card.get("rows") or {}).items()},
            "path": str(directory) if directory is not None else None}
