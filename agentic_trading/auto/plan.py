"""The automatic-trading plan: what a person confirms once, and what code may adjust inside it.

Two layers, kept apart on purpose (docs/todo.md, P1.3 and P1.4).

**The envelope** is the person's: which mode, how much of the account automatic trading may
have deployed at once, and the loss at which it stops opening anything. Nothing in this module
chooses those. It checks them against the risk profile's own ceilings and refuses what is past
one; it never raises a ceiling to make a plan fit.

**The strategy** is adjustable: how many positions, how large, how sure research must be, when
to leave. A suggestion for it may come from a model or from the template below, and either way
it is the same kind of thing -- a suggestion, clamped to the bounds before anything reads it.
Every clamp is recorded, so "the model asked for five positions and three is the limit" is a
line in the plan and not something that quietly happened.

Between the two sits the question a small account actually has: *does any order exist?* At $100
the answer today is no, for reasons that are all in the configuration and none of them the
market's -- the size settings ask for less than the minimum order, the limits refuse rather than
trim, and the share code may open is itself below the minimum. A plan therefore carries two
feasibility tables, sized by the Hard Risk engine's own arithmetic: the account as configured
today, and the account under the changes the plan proposes. The changes are listed one by one
with what each is and why; applying them to the real account is the console's confirmation flow
(``config_changes``), which this module never calls.

Pure over what it is given: profiles and a priced snapshot in, a plan out. No venue, no write.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
from dataclasses import asdict, dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from pydantic.json_schema import SkipJsonSchema

from .. import autonomy, diagnosis, sizing
from ..broker import requirements
from ..portfolio import Position, Snapshot, build_snapshot, classify_instrument
from ..profile import ConfidenceRequirements, Profiles
from ..risk import HardRiskEngine
from ..risk import Proposal as RiskProposal
from . import modes

RISK_FILE = "risk_profile.yaml"

#: What a change in a plan is, which decides who may make it. A boundary is the person's and
#: is only ever proposed; a product setting shapes orders inside the boundaries; a schedule
#: setting decides what gets researched.
BOUNDARY, PRODUCT, SCHEDULE = "boundary", "product", "schedule"

#: The loss line a person may choose, as a share of the plan's capital. The floor keeps a line
#: from being set so tight that one ordinary day trips it; the ceiling is the drawdown the
#: account's own ladder halts at, past which a looser line here would never be reached.
LOSS_PAUSE_PCT = (2.0, 15.0)
DEFAULT_LOSS_PAUSE_PCT = 10.0

#: The strategy templates this system has code for, by mode. A model fills in a template's
#: parameters and nothing else: it writes no rule, no condition and no code, and a template name
#: outside this table is replaced (``clamp``). What each template does is ``auto.trader`` --
#: research in, a position sized by the plan, out at the stop, the target or the holding period.
TEMPLATES: dict[str, tuple[str, ...]] = {
    modes.STEADY: ("research_etf", "baseline_etf"),
    modes.SWING: ("trend_etf",),
    modes.INTRADAY: ("trend_etf",),
}
RATIONALE_LIMIT = 400

#: The baseline template (docs/AUTO_BASELINE_CONTRACT_2026-10-15.md, 2.1; D1-D6 are the owner's,
#: 2026-10-15). Research does not talk code into a purchase under it: the owner's own baseline
#: weights are bought toward in tranches, and research can only say no. Which template a plan
#: runs is the person's choice, like the envelope; a model never picks it.
BASELINE = autonomy.BASELINE_TEMPLATE
#: Its way out, fixed by the template (D5): a holding meant to be long has no term, and a stop
#: at the widest the steady mode allows.
BASELINE_EXITS = {"stop_loss_pct": 15.0, "take_profit_pct": 0.0, "max_holding_days": 0,
                  "trend_window": 0}
#: What it waits for and what it skips, fixed by the template too (D2, D3, D5).
BASELINE_FIXED_RULES = {"veto_window_days": 14,
                        "regime_skips": ["ELEVATED_RISK", "HIGH_RISK", "UNKNOWN"],
                        "after_stop_cooldown_sessions": 20}
#: The most tranches a fund is split into, when one order could not carry a configured tranche.
MAX_TRANCHES = 10
#: The minimum order a baseline preview compares against the account's own. Shown, never applied.
WHAT_IF_MINIMUM_USD = 10.0


class PlanError(ValueError):
    """The plan cannot be made as asked; the message says why, for a person."""


@dataclass(frozen=True)
class Envelope:
    """What the person confirms. Dollars and a percentage, because those are what a person sets."""

    mode: str
    capital_usd: float
    loss_pause_pct: float = DEFAULT_LOSS_PAUSE_PCT
    account: str = "micro_live"

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "loss_pause_usd": round(self.capital_usd * self.loss_pause_pct / 100.0, 2)}


class Strategy(BaseModel):
    """What code may adjust inside the envelope. Every field is clamped before it is used."""

    model_config = ConfigDict(extra="forbid")

    template: str = "research_etf"
    #: How many positions the plan holds at most.
    max_positions: int = Field(3, description="Maximum number of positions held at once.")
    #: The size of one position, as a share of the plan's capital.
    position_pct: float = Field(33.0, description="Target size of one position, percent of the plan's capital.")
    #: How sure research must be before code buys. Never below the risk profile's own floor.
    min_confidence: int = Field(80, description="Research confidence needed before buying, 0-100.")
    #: A position down this much from its cost is closed. A positive number of percent.
    stop_loss_pct: float = Field(10.0, description="Close a position that is down this many percent from cost.")
    #: A position up this much is closed; zero means no profit target.
    take_profit_pct: float = Field(0.0, description="Close a position that is up this many percent; 0 for none.")
    #: The longest a position is held, in calendar days.
    max_holding_days: int = Field(90, description="Longest holding period in calendar days.")
    #: Sessions of a fund's own closes it is measured against: bought only while its last
    #: close is above their average, and left once it falls below. Zero where the template has
    #: no trend rule. Timing and exit only -- research still has to say buy.
    trend_window: int = Field(0, description="Sessions in the trend average; 0 for no trend rule.")
    #: What may be bought, as symbols from the configured universe.
    universe: list[str] = Field(default_factory=list, description="Symbols that may be bought.")
    #: The baseline template's targets, fund by fund, worked out when the plan is drawn
    #: (``baseline_deployment``) and held from then on, so a version is the same version for as
    #: long as it runs. None for every other template. Not part of what a model is asked for.
    deployment: SkipJsonSchema[dict[str, Any] | None] = None
    #: Why, in a sentence each language the console speaks.
    rationale_zh: str = Field("", description="One or two short sentences in Chinese explaining the choice.")
    rationale_en: str = Field("", description="The same explanation in English.")


@dataclass(frozen=True)
class Change:
    """One setting the plan needs different from what the real account has today."""

    path: str
    value: Any
    kind: str
    why_zh: str
    why_en: str
    file: str = RISK_FILE
    current: Any = None

    def as_dict(self) -> dict[str, Any]:
        return {"file": self.file, "path": self.path, "from": self.current, "to": self.value,
                "kind": self.kind, "why": {"zh": self.why_zh, "en": self.why_en}}


@dataclass
class Clamp:
    field_name: str
    asked: Any
    used: Any
    why_zh: str
    why_en: str

    def as_dict(self) -> dict[str, Any]:
        return {"field": self.field_name, "asked": self.asked, "used": self.used,
                "why": {"zh": self.why_zh, "en": self.why_en}}


@dataclass
class Bounds:
    max_positions: tuple[int, int]
    position_pct: tuple[float, float]
    min_confidence: tuple[int, int]
    stop_loss_pct: tuple[float, float]
    take_profit_pct: tuple[float, float]
    max_holding_days: tuple[int, int]
    trend_window: tuple[int, int]
    universe: tuple[str, ...]
    classes: tuple[str, ...] = field(default_factory=tuple)
    #: The templates local code knows how to carry out for this mode. A name outside them is
    #: not a strategy this system can run, whoever suggested it.
    templates: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return {name: list(value) for name, value in asdict(self).items()}


# ---------------------------------------------------------------- reading the profile
def _get(profiles: Profiles, *keys: str, default: Any = None) -> Any:
    return profiles.risk_cfg(*keys, default=default)


def with_changes(profiles: Profiles, changes: list[Change]) -> Profiles:
    """The same profiles with a plan's risk-profile changes applied, in memory only.

    For asking "what would this account be able to do under the plan", and for the simulated
    account, which runs under exactly that. The real configuration is not touched: this returns
    a copy, and nothing here knows where the file is.
    """
    risk = copy.deepcopy(profiles.risk)
    for change in changes:
        if change.file != RISK_FILE:
            continue
        node = risk
        parts = change.path.split(".")
        for part in parts[:-1]:
            if not isinstance(node.get(part), dict):
                node[part] = {}
            node = node[part]
        leaf = node.get(parts[-1])
        if isinstance(leaf, dict) and "value" in leaf and "recommended_default" in leaf:
            leaf["value"] = change.value
        else:
            node[parts[-1]] = change.value
    derived = copy.copy(profiles)
    derived.risk = risk
    derived.confidence = ConfidenceRequirements(**(risk.get("confidence_requirements") or {}))
    derived.defaults_used = list(profiles.defaults_used)
    return derived


def allowed_classes(profiles: Profiles, account: str) -> tuple[str, ...]:
    """The instrument classes code may open on its own, as the risk profile names them."""
    classes = _get(profiles, "buckets", account, "autonomy", "open", "classes", default=[]) or []
    return tuple(str(c) for c in classes)


def universe_of(profiles: Profiles, classes: tuple[str, ...]) -> list[str]:
    """One fund per exposure group whose class code may open, in the profile's own order.

    One per group, because the risk profile holds one fund per group: a second from the same
    group would be refused by HR09, and offering it would only be offering a refusal.
    """
    out = []
    for group, tickers in profiles.exposure_groups().items():
        if not tickers:
            continue
        cls = classify_instrument(tickers[0], profiles, lambda _symbol: {"quote_type": "ETF"})
        if cls.exposure_group == group and cls.limit_class in classes:
            out.append(tickers[0].upper())
    return out


def autonomous_floor(profiles: Profiles, account: str) -> int:
    """The confidence below which code may not buy, whatever a strategy asks for."""
    floors = [profiles.confidence.floor(account, "etf", "small", limit_class=cls, autonomous=True)
              for cls in ("broad_diversified_etf", "bond_or_cash_like_etf")]
    return int(max(floors))


def bounds_for(profiles: Profiles, envelope: Envelope) -> Bounds:
    """What a strategy may ask for under this envelope: the mode's range, held to the profile."""
    mode = modes.TABLE[envelope.mode]
    account = envelope.account
    cap_positions = int(_get(profiles, "buckets", account, "position_limits", "max_open_positions",
                             default=3))
    classes = allowed_classes(profiles, account)
    floor = min(100, autonomous_floor(profiles, account))
    stop = (5.0, 15.0) if envelope.mode == modes.STEADY else (2.0, 10.0)
    return Bounds(
        max_positions=(1, max(1, cap_positions)),
        position_pct=(5.0, 100.0),
        min_confidence=(floor, 100),
        stop_loss_pct=stop,
        take_profit_pct=(0.0, 50.0),
        max_holding_days=mode.holding_days,
        trend_window=mode.trend_window,
        universe=tuple(universe_of(profiles, classes)),
        classes=classes,
        templates=TEMPLATES[envelope.mode],
    )


def clamp(strategy: Strategy, bounds: Bounds) -> tuple[Strategy, list[Clamp]]:
    """The strategy with every figure inside its bounds, and a note for each one that was not."""
    notes: list[Clamp] = []
    data = strategy.model_dump()

    # A strategy is one of the templates local code carries out, with parameters. Whatever
    # else a suggestion names -- a strategy this system has no code for, or anything that is
    # not a name at all -- is not run: the mode's own template stands in, and the plan says so.
    if bounds.templates and data.get("template") not in bounds.templates:
        asked_template = str(data.get("template"))[:60]
        notes.append(Clamp("template", asked_template, bounds.templates[0],
                           f"“{asked_template}” 不是本系统能执行的策略模板，已改用 {bounds.templates[0]}。",
                           f"{asked_template!r} is not a template this system can carry out; "
                           f"{bounds.templates[0]} is used instead."))
        data["template"] = bounds.templates[0]
    # The baseline template's way out is the template's, not a parameter (D5).
    fixed = BASELINE_EXITS if data.get("template") == BASELINE else {}
    if data.get("template") != BASELINE:
        data["deployment"] = None

    def hold(name: str, low: float, high: float, integer: bool = False) -> None:
        asked = data[name]
        if name in fixed:
            used = fixed[name]
            if asked != used:
                notes.append(Clamp(name, asked, used,
                                   f"基准建仓模板的 {name} 固定为 {used:g}，已从 {asked} 调整。",
                                   f"The baseline template fixes {name} at {used:g}; {asked} became {used:g}."))
            data[name] = used
            return
        try:
            value = float(asked)
        except (TypeError, ValueError):
            value = math.nan
        used = low if not math.isfinite(value) else min(max(value, low), high)
        used = round(used) if integer else round(float(used), 2)
        if used != asked:
            notes.append(Clamp(name, asked, used,
                               f"{name} 只能在 {low:g} 到 {high:g} 之间，已从 {asked} 调整为 {used}。",
                               f"{name} must be between {low:g} and {high:g}; {asked} became {used}."))
        data[name] = used

    hold("max_positions", *bounds.max_positions, integer=True)
    hold("position_pct", *bounds.position_pct)
    hold("min_confidence", *bounds.min_confidence, integer=True)
    hold("stop_loss_pct", *bounds.stop_loss_pct)
    hold("take_profit_pct", *bounds.take_profit_pct)
    hold("max_holding_days", *bounds.max_holding_days, integer=True)
    hold("trend_window", *bounds.trend_window, integer=True)

    # The explanation is words about the strategy and is shown as words. It is kept short.
    for key in ("rationale_zh", "rationale_en"):
        data[key] = str(data.get(key) or "")[:RATIONALE_LIMIT]

    asked_universe = [str(s).upper() for s in data.get("universe") or []]
    kept = [s for s in dict.fromkeys(asked_universe) if s in bounds.universe]
    dropped = [s for s in dict.fromkeys(asked_universe) if s not in bounds.universe]
    if dropped:
        notes.append(Clamp("universe", asked_universe, kept,
                           f"{', '.join(dropped)} 不在允许自动买入的范围内，已去掉。",
                           f"{', '.join(dropped)} is outside what may be bought automatically "
                           "and was removed."))
    if not kept:
        kept = list(bounds.universe)
    data["universe"] = kept
    return Strategy(**data), notes


def template_strategy(profiles: Profiles, envelope: Envelope, bounds: Bounds, *,
                      deployable_usd: float, minimum_usd: float) -> Strategy:
    """The default suggestion: as many equal positions as the capital can hold above the minimum.

    Deterministic, and what a plan uses whenever no model is asked or the model cannot answer.
    """
    most = bounds.max_positions[1]
    # A position smaller than the minimum order cannot be opened, so fewer, larger ones.
    affordable = int(deployable_usd // minimum_usd) if minimum_usd > 0 else most
    count = max(1, min(most, affordable, len(bounds.universe) or most))
    if envelope.mode == modes.SWING:
        count = max(1, min(count, 2))
    share = math.floor(100.0 / count)
    steady = envelope.mode == modes.STEADY
    window = 0 if steady else min(max(20, bounds.trend_window[0]), bounds.trend_window[1])
    trend_zh = "" if not window else f"买入还要求收盘价在自己 {window} 日均线上方，跌破均线也退出。"
    trend_en = "" if not window else (f" A purchase also needs the close above its own {window}-session "
                                      "average, and a close below it is an exit.")
    return Strategy(
        template="research_etf" if steady else "trend_etf",
        max_positions=count, position_pct=float(share),
        min_confidence=bounds.min_confidence[0],
        stop_loss_pct=10.0 if steady else 6.0, take_profit_pct=0.0,
        max_holding_days=90 if steady else 20, trend_window=window,
        universe=list(bounds.universe),
        rationale_zh=(f"把可用资金平均分成 {count} 份，每份 {share}%；研究把握度达到 "
                      f"{bounds.min_confidence[0]} 才买入，跌 {10 if steady else 6}% 或持有满 "
                      f"{90 if steady else 20} 天退出。" + trend_zh),
        rationale_en=(f"The capital is split into {count} equal position(s) of {share}% each; "
                      f"research must reach confidence {bounds.min_confidence[0]} to buy, and a "
                      f"position is closed at a {10 if steady else 6}% loss or after "
                      f"{90 if steady else 20} days." + trend_en),
    )


# ---------------------------------------------------------------- the baseline template
def baseline_rules(profiles: Profiles) -> dict[str, Any]:
    """The baseline template's rules: the owner's tranches and spacing, and the fixed ones."""
    configured = _get(profiles, "buckets", "core", "initial_deployment", default={}) or {}
    if not isinstance(configured, dict):
        configured = {}
    return {"tranches": max(1, int(configured.get("tranches") or 3)),
            "spacing_sessions": max(0, int(configured.get("spacing_trading_days") or 5)),
            **copy.deepcopy(BASELINE_FIXED_RULES)}


def baseline_weights(profiles: Profiles, universe: list[str]) -> dict[str, float]:
    """The owner's baseline weights (``buckets.core.baseline_allocation_pct_of_core``) for the
    funds the plan may buy, in the universe's order. Cash, and anything else outside it, is not
    bought toward: its share stays cash."""
    table = _get(profiles, "buckets", "core", "baseline_allocation_pct_of_core", default={}) or {}
    wanted = {str(key).upper(): value for key, value in table.items()} if isinstance(table, dict) else {}
    out: dict[str, float] = {}
    for symbol in universe:
        try:
            weight = float(wanted.get(symbol.upper()) or 0.0)
        except (TypeError, ValueError):
            continue
        if math.isfinite(weight) and weight > 0:
            out[symbol.upper()] = weight
    return out


def _baseline_rooms(profiles: Profiles, snap: Snapshot, envelope: Envelope,
                    symbols: list[str]) -> tuple[float, dict[str, float], dict[str, float]]:
    """What these funds may take together, the most one position of each may be worth, and the
    most one order of each may be -- by the Hard Risk engine's own arithmetic, on the book as it is.

    Together: the plan's capital, the account's cash, and the share of the account their tier
    may hold. One position: the engine's normal size, which is the most it ever sizes a position
    a strategy asks for, and the position cap. One order: the order caps. Nothing is approved
    here; every tranche is still put to the whole engine when it is written and again when sent.
    """
    account = envelope.account
    engine = HardRiskEngine(profiles, halt_file="/nonexistent/plan-preview")
    bucket = snap.buckets[account]
    together = [float(envelope.capital_usd)]
    position: dict[str, float] = {}
    order: dict[str, float] = {}
    for symbol in symbols:
        cls = classify_instrument(symbol, profiles, lambda _symbol: {"quote_type": "ETF"})
        prop = RiskProposal(ticker=symbol, bucket=account, action="BUY", size_hint="normal",
                            confidence=100, trade_date=snap.trade_date, cls=cls, autonomous=True,
                            weight_override_pct=100.0)
        room = engine.order_room(prop, snap)
        limits = {item["rule_id"]: item["room"] for item in room["limits"]}
        held = bucket.market_value_of(symbol)
        most = float(room["cut_to_fit"]["requested"]) + held
        if limits.get("HR08") is not None:
            most = min(most, float(limits["HR08"]) + held)
        position[symbol] = max(0.0, most)
        caps = [float(limits[rule]) for rule in ("HR08b", "HR21b") if limits.get(rule) is not None]
        order[symbol] = min(caps) if caps else math.inf
        together += [float(limits[rule]) for rule in ("HR07", "HR07c", "HR21")
                     if limits.get(rule) is not None]
    return max(0.0, min(together)), position, order


def baseline_targets(weights: dict[str, float], *, deployable_usd: float,
                     position_usd: dict[str, float], order_usd: dict[str, float],
                     minimum_usd: float, tranches: int,
                     max_positions: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Each fund's target and how many tranches it is bought in, and the funds left out and why.

    Contract 2.1, in order: the weights scaled to what can be deployed, each held to what one
    position may be worth; below the minimum order, the smallest weight is left out and the rest
    shared again, one at a time; then the tranches, as many as the configuration says and no
    more than the minimum order allows -- more only where one order could not carry a tranche.
    Beyond the positions the account may hold, the smallest weights are left out first.
    """
    order_of = list(weights)
    kept = dict(sorted(weights.items(), key=lambda item: (-item[1], order_of.index(item[0]))))
    left_out: list[dict[str, Any]] = []
    for symbol in list(kept)[max(0, max_positions):]:
        left_out.append({"symbol": symbol, "baseline_pct": weights[symbol], "share_usd": None,
                         "code": "tranche_position_limit"})
        kept.pop(symbol)
    shares: dict[str, float] = {}
    while kept:
        total = sum(kept.values())
        shares = {symbol: min(deployable_usd * weight / total, position_usd.get(symbol, math.inf))
                  for symbol, weight in kept.items()}
        short = [symbol for symbol in kept if shares[symbol] + 1e-9 < minimum_usd]
        if not short:
            break
        smallest = min(short, key=lambda symbol: (kept[symbol], -order_of.index(symbol)))
        left_out.append({"symbol": smallest, "baseline_pct": weights[smallest],
                         "share_usd": sizing.floor_cents(shares[smallest]),
                         "code": "tranche_below_minimum"})
        kept.pop(smallest)
    targets = []
    for symbol, weight in kept.items():
        target = sizing.floor_cents(shares[symbol])
        count = max(1, min(int(tranches), int(target // minimum_usd))) if minimum_usd > 0 else int(tranches)
        one = order_usd.get(symbol, math.inf)
        if target / count > one + 1e-9:
            count = min(MAX_TRANCHES, math.ceil(target / one))
        targets.append({"symbol": symbol, "baseline_pct": weight, "target_usd": target,
                        "tranches": count})
    return targets, left_out


def baseline_deployment(profiles: Profiles, snap: Snapshot, envelope: Envelope, bounds: Bounds, *,
                        minimum_usd: float | None = None) -> dict[str, Any]:
    """What a baseline plan buys toward, fund by fund: worked out once, when the plan is drawn.

    Held in the strategy from then on (``Strategy.deployment``), so what the owner confirmed is
    what runs, and a version is the same version for as long as it runs -- the simulated record
    and the real account's start are of one strategy, not of two that came out alike.
    """
    weights = baseline_weights(profiles, list(bounds.universe))
    minimum = minimum_usd if minimum_usd is not None else sizing.minimum_order(profiles, envelope.account)[0]
    rules = baseline_rules(profiles)
    deployable, position, order = _baseline_rooms(profiles, snap, envelope, list(weights))
    targets, left_out = baseline_targets(
        weights, deployable_usd=deployable, position_usd=position, order_usd=order,
        minimum_usd=minimum, tranches=rules["tranches"], max_positions=bounds.max_positions[1])
    return {"baseline_pct": weights, "targets": targets, "left_out": left_out,
            "max_positions": bounds.max_positions[1],
            "deployable_usd": sizing.floor_cents(deployable), "minimum_order_usd": float(minimum),
            "cash_left_usd": round(float(envelope.capital_usd) - sum(t["target_usd"] for t in targets), 2),
            "rules": rules}


def _valid_deployment(found: Any, bounds: Bounds, envelope: Envelope) -> dict[str, Any] | None:
    """A deployment handed back with a strategy, if it is one this envelope could have drawn."""
    try:
        targets = list(found["targets"])
        total = 0.0
        for row in targets:
            if str(row["symbol"]).upper() not in bounds.universe:
                return None
            amount, count = float(row["target_usd"]), int(row["tranches"])
            if not (math.isfinite(amount) and amount > 0 and 1 <= count <= MAX_TRANCHES):
                return None
            total += amount
        if total > float(envelope.capital_usd) + 0.01:
            return None
        return {**copy.deepcopy(found), "rules": {**copy.deepcopy(found["rules"]),
                                                  **copy.deepcopy(BASELINE_FIXED_RULES)}}
    except (KeyError, TypeError, ValueError):
        return None


def baseline_strategy(envelope: Envelope, bounds: Bounds, deployment: dict[str, Any]) -> Strategy:
    """The baseline template, for a deployment worked out from the account."""
    targets = deployment["targets"]
    capital = float(envelope.capital_usd)
    largest = max((row["target_usd"] for row in targets), default=0.0)
    share = min(100.0, max(5.0, round(largest / capital * 100.0, 2))) if capital > 0 else 5.0
    bought = "、".join(f"{row['symbol']} ${row['target_usd']:,.2f}（{row['tranches']} 批）" for row in targets)
    bought_en = ", ".join(f"{row['symbol']} ${row['target_usd']:,.2f} in {row['tranches']} tranche(s)"
                          for row in targets)
    rules = deployment["rules"]
    return Strategy(
        template=BASELINE, max_positions=max(1, len(targets)), position_pct=share,
        min_confidence=bounds.min_confidence[0], **BASELINE_EXITS,
        universe=list(bounds.universe), deployment=deployment,
        rationale_zh=((f"按你的基准比例分批买入：{bought}；" if targets else
                       "按你的基准比例，没有一只基金分到的金额够最小下单额，一笔也不买；")
                      + f"其余 ${deployment['cash_left_usd']:,.2f} 留现金。研究只用来否决："
                      f"{rules['veto_window_days']} 天内研究给出减持或卖出就不买，没研究过的不买。"
                      f"跌 {BASELINE_EXITS['stop_loss_pct']:g}% 止损，不设最长持有期；止损后 "
                      f"{rules['after_stop_cooldown_sessions']} 个交易日内不买回。"),
        rationale_en=((f"Bought toward the owner's baseline weights in tranches: {bought_en}; " if targets
                       else "No fund's share of the owner's baseline weights reaches the minimum "
                            "order, so nothing is bought; ")
                      + f"the other ${deployment['cash_left_usd']:,.2f} stays cash. Research can "
                      f"only say no: a Sell or Underweight within {rules['veto_window_days']} days "
                      "vetoes a tranche, and a fund nobody has researched is not bought. A "
                      f"{BASELINE_EXITS['stop_loss_pct']:g}% stop, no longest holding period, "
                      f"and {rules['after_stop_cooldown_sessions']} sessions before buying back "
                      "after a stop."),
    )


# ---------------------------------------------------------------- the envelope
def check_envelope(profiles: Profiles, envelope: Envelope) -> None:
    """Refuse an envelope past one of the risk profile's own ceilings. Never raises a ceiling."""
    if envelope.mode not in modes.MODES:
        raise PlanError(f"unknown mode {envelope.mode!r}; one of {', '.join(modes.MODES)}")
    if envelope.account != "micro_live":
        raise PlanError("automatic trading is planned for the Agentic account only")
    capital = envelope.capital_usd
    if isinstance(capital, bool) or not isinstance(capital, (int, float)) or not math.isfinite(capital) \
            or capital <= 0:
        raise PlanError("the capital for automatic trading must be a positive amount of dollars")
    ceiling = float((profiles.risk.get("micro_live_execution") or {}).get("max_capital_usd", 0) or 0)
    if ceiling and capital > ceiling + 1e-9:
        raise PlanError(f"${capital:,.2f} is above the account's own ceiling of ${ceiling:,.2f} "
                        "(micro_live_execution.max_capital_usd); a plan cannot raise it")
    low, high = LOSS_PAUSE_PCT
    if not isinstance(envelope.loss_pause_pct, (int, float)) or isinstance(envelope.loss_pause_pct, bool) \
            or not low <= float(envelope.loss_pause_pct) <= high:
        raise PlanError(f"the loss line must be between {low:g}% and {high:g}% of the plan's capital")


# ---------------------------------------------------------------- what the plan needs changed
def proposed_changes(profiles: Profiles, snap: Snapshot, envelope: Envelope,
                     strategy: Strategy) -> list[Change]:
    """Every setting the real account would need different for this plan to place an order.

    Each is named for what it is. A boundary is the person's own limit and appears here only as
    a proposal with its reason; whether to make it is theirs, through the console's confirmation
    flow. Nothing is proposed that the plan does not need, and confidence floors are never
    among them: a plan that would only work with a lower bar says so instead.
    """
    account = envelope.account
    bucket = snap.buckets[account]
    equity = bucket.allocation_usd or bucket.equity
    out: list[Change] = []

    def now(*keys: str) -> Any:
        return _get(profiles, *keys, default=None)

    opening = f"buckets.{account}.autonomy.open"
    # --- the allowance: the person's capital figure is the allowance, in the terms HR23 reads
    capital = min(envelope.capital_usd, equity) if equity else envelope.capital_usd
    share = min(100, math.ceil(capital / equity * 100.0 - 1e-9)) if equity else 100
    have_share = now("buckets", account, "autonomy", "open", "max_pct_of_account")
    if have_share is None or float(have_share) != float(share):
        out.append(Change(
            f"{opening}.max_pct_of_account", share, BOUNDARY,
            f"你设定可用资金 ${envelope.capital_usd:,.0f}，约为账户的 {share}%；当前允许代码自行开仓的"
            f"比例是 {have_share if have_share is not None else '未设置'}%。",
            f"You set ${envelope.capital_usd:,.0f} of capital, about {share}% of the account; code "
            f"may currently open {have_share if have_share is not None else 'an undeclared share'}% "
            "on its own.", current=have_share))
    have_usd = now("buckets", account, "autonomy", "open", "max_usd")
    if have_usd is None or float(have_usd) != float(envelope.capital_usd):
        out.append(Change(
            f"{opening}.max_usd", round(float(envelope.capital_usd), 2), BOUNDARY,
            f"把自动交易能占用的资金封顶在 ${envelope.capital_usd:,.0f}，账户以后入金也不会跟着放大。",
            f"Caps what automatic trading may hold at ${envelope.capital_usd:,.0f}, so a later "
            "deposit does not widen it.", current=have_usd))
    floor = now("buckets", account, "autonomy", "open", "min_confidence")
    if floor is None or int(floor) != int(strategy.min_confidence):
        out.append(Change(
            f"{opening}.min_confidence", int(strategy.min_confidence), BOUNDARY,
            f"代码自行买入所需的研究把握度：{strategy.min_confidence}（不低于风控规定的下限）。",
            f"The research confidence code needs before buying on its own: "
            f"{strategy.min_confidence}, never below the risk profile's floor.", current=floor))
    if now("buckets", account, "autonomy", "open", "enabled") is not True:
        out.append(Change(
            f"{opening}.enabled", True, BOUNDARY,
            "打开“自动买入”开关。实盘上这一步需要你的控制台密码。",
            "Turns the automatic-buying switch on. On the real account this takes your console "
            "password.", current=now("buckets", account, "autonomy", "open", "enabled")))

    # --- the mode itself, where the owner has not yet said code may open under it. For swing
    # this is a question only they can answer, and it is put to them here in so many words
    # rather than settled by the label a proposal happens to carry.
    allowed = modes.allowed_modes(profiles, account)
    if envelope.mode not in allowed:
        label = modes.TABLE[envelope.mode].label
        declared = now("buckets", account, "autonomy", "open", "modes")
        if envelope.mode == modes.SWING:
            zh = ("允许代码在“波段”模式下自行开仓。这是只有你能做的决定：风控配置规定动量类策略每一笔都必须人工确认，"
                  "而波段模式是在研究已经建议买入的基金上，用趋势规则来择时和退出。确认这一项，就是你认定这种用法"
                  "不属于那条规定；不确认，波段模式的每一笔买入仍然要等你批准。动量类策略须人工确认的规定本身不会被改动。")
            en = ("Lets code open positions on its own under the swing mode. This is a decision "
                  "only you can make: the risk profile requires a person to confirm every "
                  "momentum order, and swing uses a trend rule to time, and to leave, funds "
                  "research has already said to buy. Confirming this is you deciding that such "
                  "use is not what that rule is about; without it every swing purchase still "
                  "waits for you. The rule about momentum strategies is itself left as it is.")
        else:
            zh = f"允许代码在“{label[0]}”模式下自行开仓。目前允许的模式：{'、'.join(allowed) or '无'}。"
            en = (f"Lets code open positions on its own under the {label[1].lower()} mode. "
                  f"Allowed now: {', '.join(allowed) or 'none'}.")
        out.append(Change(f"{opening}.modes", [*allowed, envelope.mode], BOUNDARY, zh, en,
                          current=list(declared) if isinstance(declared, (list, tuple)) else None))

    # --- the way out. A position code opens has to be one code may close, or it is a position
    # nobody is watching; and the authority is exactly that wide.
    if now("buckets", account, "autonomy", "managed_exit", "enabled") is not True:
        out.append(Change(
            f"buckets.{account}.autonomy.managed_exit.enabled", True, BOUNDARY,
            "允许代码卖出它自己按方案买入的仓位（止损、持有到期，或研究建议卖出）。你自己买入的仓位不在其内，"
            "卖出仍然要你确认。",
            "Lets code sell positions it opened itself under the plan: at its stop, at the end of "
            "its holding period, or when research says to. Positions you bought are not covered, "
            "and selling them still takes you.",
            current=now("buckets", account, "autonomy", "managed_exit", "enabled")))

    # --- product settings: how an order is shaped inside the limits
    if not sizing.fits_to_limits(profiles, account):
        out.append(Change(
            "sizing.fit_to_limits", True, PRODUCT,
            "订单超过某项上限时，先缩小到上限以内再下单，而不是整笔拒绝。金额只会缩小，不会放大。",
            "An order above a limit is cut to fit it instead of being refused outright. It can "
            "only shrink.", current=_get(profiles, "sizing", "fit_to_limits", default=None)))

    minimum, _source = sizing.minimum_order(profiles, account)
    intended = round(envelope.capital_usd * strategy.position_pct / 100.0, 2)
    half = round(intended / 2.0, 2)
    lowest, lowest_why = requirements.lowest_product_minimum(profiles, account)
    # The engine halves a position when evidence is thin or the regime is tense. A minimum above
    # that half size means every such order is dropped, so the minimum is proposed down to it --
    # and only as far as the venue's own minimum, which has to be on record to lower it at all.
    # Not for the baseline template: its tranches are worked out from the minimum the account
    # has, and a lower one is a decision of the owner's that its preview only compares
    # (``what_if_minimum_usd``; contract 2.1).
    if strategy.template == BASELINE:
        pass
    elif half + 1e-9 < minimum and lowest is not None:
        wanted = max(float(math.floor(half)), float(lowest), 1.0)
        if wanted + 1e-9 < minimum:
            out.append(Change(
                "sizing.min_order_usd", wanted, PRODUCT,
                f"当前最低下单金额 ${minimum:,.0f} 是本产品自己的设置，不是券商的要求（券商最低 "
                f"${lowest:,.2f}）。方案里的半仓是 ${half:,.2f}，低于它就永远下不了单，所以建议调到 "
                f"${wanted:,.0f}。",
                f"The ${minimum:,.0f} minimum order is this product's own setting, not the venue's "
                f"(the venue accepts ${lowest:,.2f}). The plan's half-size position is ${half:,.2f}, "
                f"which that setting would always drop, so ${wanted:,.0f} is proposed.",
                current=minimum))
    elif half + 1e-9 < minimum and lowest is None:
        out.append(Change(
            "sizing.min_order_usd", minimum, PRODUCT,
            f"半仓 ${half:,.2f} 低于最低下单金额 ${minimum:,.0f}，但券商的最低要求没有记录，"
            "不能下调这项设置；这类订单会被放弃。",
            f"The half-size position of ${half:,.2f} is below the ${minimum:,.0f} minimum order, "
            f"and that setting cannot be lowered: {lowest_why}.", current=minimum))

    # --- what gets researched: a plan buys what research recommends, so research has to look
    # at the funds the plan may buy -- weekly for a steady plan, every session for a swing one.
    # Research runs on the same subscription the owner uses, so each of these says what it costs.
    if envelope.mode in (modes.STEADY, modes.SWING):
        schedule = ((profiles.risk.get("research_schedule") or {}).get("accounts") or {}).get(account) or {}
        candidates = schedule.get("candidates") or {}
        per_run = int(candidates.get("max_tickers_per_run", 3) or 3)
        if candidates.get("enabled") is False or not candidates:
            out.append(Change(
                f"research_schedule.accounts.{account}.candidates.enabled", True, SCHEDULE,
                "研究安排里“候选 ETF”这一项目前是关闭的。方案买的是研究推荐的 ETF，"
                f"需要打开；每次最多研究 {per_run} 只。",
                "The study of candidate ETFs is switched off. A plan buys what research "
                f"recommends, so it has to be on; it studies up to {per_run} funds a run.",
                current=candidates.get("enabled")))
        if envelope.mode == modes.SWING and candidates and str(candidates.get("period", "weekly")) != "daily":
            out.append(Change(
                f"research_schedule.accounts.{account}.candidates.period", "daily", SCHEDULE,
                f"波段模式按每个交易日研究设计，现在是每周一次。改成每日后，每个交易日最多多用 {per_run} 次"
                f"研究额度（每周约 {per_run * 5} 次，现在每周最多 {per_run} 次）；研究用的是你的 Claude 订阅额度。",
                f"A swing plan is designed on research every session; it is weekly now. Daily, it "
                f"costs up to {per_run} analyses a session, about {per_run * 5} a week against "
                f"{per_run} now, on the same Claude subscription you use.",
                current=candidates.get("period", "weekly")))
    return out


# ---------------------------------------------------------------- does any order exist
def feasibility(profiles: Profiles, snap: Snapshot, envelope: Envelope,
                strategy: Strategy) -> dict[str, Any]:
    """What this account could order under these profiles, sized by the engine's own arithmetic.

    One row per fund the strategy may buy, for the full size and for the half size the engine
    falls back to when evidence is thin. A row that cannot be ordered carries the reason in the
    same words a failed run would give (``diagnosis``), so a plan and a run never describe the
    same refusal two ways.
    """
    account = envelope.account
    bucket = snap.buckets[account]
    equity = bucket.allocation_usd or bucket.equity
    engine = HardRiskEngine(profiles, halt_file="/nonexistent/plan-preview")
    fit = sizing.fits_to_limits(profiles, account)
    # The plan's position, as the share of the account the engine sizes by.
    weight = (envelope.capital_usd * strategy.position_pct / 100.0) / equity * 100.0 if equity else 0.0
    rows = []
    for symbol in strategy.universe:
        cls = classify_instrument(symbol, profiles, lambda _symbol: {"quote_type": "ETF"})
        for hint in ("normal", "small"):
            if weight <= 0:
                break
            prop = RiskProposal(ticker=symbol, bucket=account, action="BUY", size_hint=hint,
                                confidence=100, trade_date=snap.trade_date, cls=cls,
                                autonomous=True, weight_override_pct=weight)
            room = engine.order_room(prop, snap)
            view = room["cut_to_fit"] if fit else room["as_asked"]
            feasible = bool(view["feasible"])
            rows.append({"symbol": symbol, "limit_class": cls.limit_class, "size": hint,
                         "requested_usd": view["requested"],
                         "order_usd": float(view["order"]) if feasible else 0.0,
                         "feasible": feasible, "code": view["code"],
                         "binding": list(view.get("binding") or []),
                         "why_not": None if feasible else _why_not(view, room, fit)})
    path = deployment_path(profiles, snap, envelope, strategy)
    bought = [step for step in path if step["feasible"]]
    limits = ((profiles.risk.get("buckets") or {}).get(account) or {}).get("activity_limits") or {}
    return {
        "fits_to_limits": fit,
        "rows": rows,
        "any_order": bool(bought),
        "order_usd": bought[0]["order_usd"] if bought else 0.0,
        "positions": len(bought),
        #: What the plan could actually have invested once every limit is taken together: the
        #: positions bought one after another, each sized against the account the last one left.
        "deployable_usd": round(sum(step["order_usd"] for step in bought), 2),
        "path": path,
        #: How fast it may get there. The limits on new positions are the account's, not the plan's.
        "pace": {"new_positions_per_day": profiles.effective(limits.get("max_new_positions_per_day"), "pace.day"),
                 "new_positions_per_week": profiles.effective(limits.get("max_new_positions_per_week"), "pace.week"),
                 "orders_per_week": profiles.effective(limits.get("max_orders_per_week"), "pace.orders")},
        "cash_usd": round(bucket.cash_usd, 2),
        "minimum_order": dict(zip(("usd", "source"), sizing.minimum_order(profiles, account), strict=True)),
    }


def deployment_path(profiles: Profiles, snap: Snapshot, envelope: Envelope,
                    strategy: Strategy) -> list[dict[str, Any]]:
    """The plan's positions bought one after another, each against the account the last one left.

    One order's feasibility says little about a plan: the second purchase meets a tier that is
    already part full and cash that is already part spent. So the purchases are put to the
    engine in turn on a book that carries the earlier ones, and what comes out is what the plan
    could actually hold. Nothing is bought; the book is a copy that is thrown away.
    """
    account = envelope.account
    engine = HardRiskEngine(profiles, halt_file="/nonexistent/plan-preview")
    fit = sizing.fits_to_limits(profiles, account)
    state = snap.state.model_copy(deep=True)
    prices: dict[str, tuple[float, str]] = {}
    identity: dict[str, dict[str, Any]] = {}
    for view in snap.buckets.values():
        for held in view.positions:
            prices[held.symbol.upper()] = (held.price, held.price_date)
            identity[held.symbol.upper()] = {
                "quote_type": "ETF" if held.cls.instrument_type == "etf" else "EQUITY",
                "sector": held.cls.sector}
    planned_usd = envelope.capital_usd * strategy.position_pct / 100.0
    current = snap
    path: list[dict[str, Any]] = []
    for symbol in strategy.universe:
        if sum(1 for step in path if step["feasible"]) >= strategy.max_positions:
            break
        if current.has_position(symbol, account):
            continue                                  # already held; a plan opens, it does not top up
        bucket = current.buckets[account]
        equity = bucket.allocation_usd or bucket.equity
        if equity <= 0:
            break
        cls = classify_instrument(symbol, profiles, lambda _symbol: {"quote_type": "ETF"})
        prop = RiskProposal(ticker=symbol, bucket=account, action="BUY", size_hint="normal",
                            confidence=100, trade_date=snap.trade_date, cls=cls, autonomous=True,
                            weight_override_pct=planned_usd / equity * 100.0)
        room = engine.order_room(prop, current)
        view = room["cut_to_fit"] if fit else room["as_asked"]
        positions_cap = int(_get(profiles, "buckets", account, "position_limits",
                                 "max_open_positions", default=3))
        if bucket.n_positions >= positions_cap:
            path.append({"symbol": symbol, "feasible": False, "order_usd": 0.0, "binding": [],
                         "why_not": diagnosis.Reason("max_positions", {"max": positions_cap}).text()})
            continue
        if not view["feasible"]:
            path.append({"symbol": symbol, "feasible": False, "order_usd": 0.0,
                         "binding": list(view.get("binding") or []),
                         "why_not": _why_not(view, room, fit)})
            continue
        order = float(view["order"])
        state.buckets[account].cash_usd = round(state.buckets[account].cash_usd - order, 2)
        # A dollar a share, so the position is worth exactly what was ordered.
        state.positions.append(Position(symbol=symbol, bucket=account, quantity=order,
                                        cost_basis_per_share=1.0, opened_by="autonomy:plan"))
        prices[symbol.upper()] = (1.0, snap.trade_date)
        identity[symbol.upper()] = {"quote_type": "ETF"}
        current = build_snapshot(state, profiles, snap.trade_date,
                                 price_lookup=lambda s, _d: prices[s.upper()],
                                 identity_lookup=lambda s: identity.get(s.upper(), {}))
        path.append({"symbol": symbol, "feasible": True, "order_usd": order,
                     "requested_usd": view["requested"], "binding": list(view.get("binding") or []),
                     "why_not": None})
    return path


def _why_not(view: dict[str, Any], room: dict[str, Any], fit: bool) -> dict[str, str]:
    """Why sizing gave no order, in the words a failed run would use for the same refusal."""
    code = view["code"]
    facts = {k: view.get(k) for k in ("requested", "legal_max", "minimum")}
    if code == sizing.OK and not fit:
        # Left as asked for, and a limit refused it: nothing was cut to fit.
        refused = (view.get("refused_by") or [None])[0]
        hit = next((item for item in room["limits"] if item["rule_id"] == refused), {})
        return _reason_text("limit_refuses_uncut",
                            {**facts, "legal_max": hit.get("room"), "limit": hit.get("code")})
    if view.get("binding"):
        facts["limit"] = view["binding"][0]
    return _reason_text(code, facts)


def _reason_text(code: str, facts: dict[str, Any]) -> dict[str, str]:
    if code == "limit_refuses_uncut":
        limit = diagnosis.LIMIT_NAMES.get(str(facts.get("limit")), ("某项上限", "A limit"))
        asked, room = facts.get("requested"), facts.get("legal_max")
        try:
            return {"zh": f"这笔要 ${float(asked):,.2f}，超过{limit[0]}（只剩 ${float(room):,.2f}）；"
                          "当前设置是整笔拒绝，不会缩小后再下。",
                    "en": f"${float(asked):,.2f} is asked for and {limit[1].lower()} leaves "
                          f"${float(room):,.2f}; as configured the order is refused, not cut to fit."}
        except (TypeError, ValueError):
            return {"zh": f"超过{limit[0]}；当前设置是整笔拒绝，不会缩小后再下。",
                    "en": f"It is past {limit[1].lower()}; as configured the order is refused, "
                          "not cut to fit."}
    return diagnosis.Reason(code, {k: v for k, v in facts.items() if v is not None}).text()


# ---------------------------------------------------------------- the plan
def build(profiles: Profiles, snap: Snapshot, envelope: Envelope, *,
          suggestion: Strategy | None = None, suggested_by: str = "template",
          suggestion_error: str | None = None, template: str | None = None) -> dict[str, Any]:
    """The whole plan for one envelope: the strategy, both feasibility tables, and the changes.

    ``suggestion`` is a strategy somebody proposed -- a model, usually. It is clamped like any
    other; given none, the template stands in and the plan says which it was.

    ``template`` is the person's choice of template, and it stands whatever a suggestion names.
    Absent, a suggestion's own template is kept -- the strategy a simulation ran, handed back to
    start the real account with -- and failing that the mode's first. A baseline plan's targets
    are worked out here from the account, unless the strategy handed back already carries the
    ones it was simulated with (``Strategy.deployment``).
    """
    check_envelope(profiles, envelope)
    account = envelope.account
    if account not in snap.buckets:
        raise PlanError(f"the {account} account is not in the ledger that was read")
    bucket = snap.buckets[account]
    equity = bucket.allocation_usd or bucket.equity
    bounds = bounds_for(profiles, envelope)
    if not bounds.universe:
        raise PlanError("the risk profile names no instrument class code may open on its own "
                        f"(buckets.{account}.autonomy.open.classes), so there is nothing to plan")
    if template is not None and template not in bounds.templates:
        raise PlanError(f"{template!r} is not a template the {envelope.mode} mode can run; "
                        f"one of {', '.join(bounds.templates)}")
    # A model fills in a template's numbers; which template is not its to say.
    chosen = template or (suggestion.template if suggestion is not None and suggested_by != "model"
                          else None)
    if chosen not in bounds.templates:
        chosen = bounds.templates[0]
    minimum, _source = sizing.minimum_order(profiles, account)
    deployment = None
    if chosen == BASELINE:
        if (suggestion is not None and suggestion.template == BASELINE
                and suggested_by == "template" and suggestion.deployment):
            deployment = _valid_deployment(suggestion.deployment, bounds, envelope)
        if deployment is None:
            deployment = baseline_deployment(profiles, snap, envelope, bounds, minimum_usd=minimum)
            suggestion = None
        default = baseline_strategy(envelope, bounds, deployment)
    else:
        default = template_strategy(profiles, envelope, bounds,
                                    deployable_usd=min(envelope.capital_usd, equity), minimum_usd=minimum)
    asked = suggestion or default
    strategy, clamps = clamp(asked, bounds)
    if strategy.template != chosen:
        clamps.append(Clamp("template", strategy.template, chosen,
                            f"策略模板由你选择，已用 {chosen}。",
                            f"The template is the person's choice; {chosen} is used."))
        strategy = default if chosen == BASELINE else strategy.model_copy(update={"template": chosen})
        strategy, _again = clamp(strategy, bounds)
    if chosen == BASELINE:
        strategy = strategy.model_copy(update={"deployment": deployment})
    if suggestion is None:
        suggested_by = "template"

    changes = proposed_changes(profiles, snap, envelope, strategy)
    under_plan = with_changes(profiles, changes)
    today = feasibility(profiles, snap, envelope, strategy)
    proposed = feasibility(under_plan, snap, envelope, strategy)
    venue = requirements.for_venue(requirements.venue_of(profiles, account))

    body = {
        "envelope": envelope.as_dict(),
        "mode": modes.TABLE[envelope.mode].as_dict(),
        "strategy": {**strategy.model_dump(exclude={"rationale_zh", "rationale_en"}),
                     "rationale": {"zh": strategy.rationale_zh, "en": strategy.rationale_en},
                     "suggested_by": suggested_by, "suggestion_error": suggestion_error},
        "bounds": bounds.as_dict(),
        "clamps": [note.as_dict() for note in clamps],
        "account": {"account_id": account, "equity_usd": round(equity, 2),
                    "cash_usd": round(bucket.cash_usd, 2), "positions": bucket.n_positions,
                    "as_of": snap.as_of},
        "today": today,
        "under_plan": proposed,
        "changes": [change.as_dict() for change in changes],
        "venue": None if venue is None else {
            "name": venue.venue, "min_order_usd": venue.min_order_usd,
            "fractional": venue.fractional, "documented": venue.documented,
            "confirmed_for_account": venue.confirmed_for_account,
            "evidence": [asdict(item) for item in venue.evidence],
            "open_questions": [{"zh": zh, "en": en} for zh, en in venue.open_questions]},
    }
    # Where a baseline plan stands, fund by fund: the service's to fill in, because it reads what
    # research has said (``trader.preview_view``). None for every other template.
    body["deployment"] = None
    if strategy.template == BASELINE:
        # The minimum order is the owner's to change. What it would mean is shown, and that is all.
        if minimum > WHAT_IF_MINIMUM_USD + 1e-9:
            weights = (strategy.deployment or {}).get("baseline_pct") or {}
            deployable, position, order = _baseline_rooms(profiles, snap, envelope, list(weights))
            rules = (strategy.deployment or {}).get("rules") or baseline_rules(profiles)
            compared, _out = baseline_targets(
                weights, deployable_usd=deployable, position_usd=position, order_usd=order,
                minimum_usd=WHAT_IF_MINIMUM_USD, tranches=int(rules["tranches"]),
                max_positions=bounds.max_positions[1])
            body["what_if_minimum_usd"] = {f"{WHAT_IF_MINIMUM_USD:g}": [
                {"symbol": row["symbol"], "target_usd": row["target_usd"], "tranches": row["tranches"]}
                for row in compared]}
        else:
            body["what_if_minimum_usd"] = None
    else:
        body["what_if_minimum_usd"] = None
    body["summary"] = _summary(body)
    # What the loss line is and is not, said wherever the line is shown: it is a check this
    # machine makes between passes, and nothing about it limits what a market can do.
    body["notes"] = {"loss_line": {
        "zh": f"亏损暂停线（${body['envelope']['loss_pause_usd']:,.2f}）是本地检查：方案自己的交易亏到这条线时停止新开仓，"
              "已有仓位仍按各自的止损和持有期退出。它不是最大亏损的保证——跳空、停牌、行情或券商中断、"
              "这台电脑离线时，实际亏损都可能超过它。从暂停线恢复后，这条线从当时的盈亏重新起算，"
              "也就是再允许同样大小的一段亏损。",
        "en": f"The loss line (${body['envelope']['loss_pause_usd']:,.2f}) is a local check: when the plan's "
              "own trading is down by that much, nothing new is opened, and what is held still "
              "leaves on its own stop and term. It is not a guarantee of the most that can be "
              "lost -- a gap, a halt, an outage of the data or the venue, or this machine being "
              "offline can each take the loss past it. Resuming from the line starts measuring "
              "it again from the result as it then stands, which allows a further loss of the "
              "same size."}}
    body["plan_id"] = hashlib.sha256(json.dumps(
        {k: body[k] for k in ("envelope", "strategy", "changes")}, sort_keys=True, default=str,
        ensure_ascii=False).encode()).hexdigest()[:16]
    return body


def _summary(plan: dict[str, Any]) -> dict[str, str]:
    """What the plan comes to, in one short paragraph per language."""
    envelope, today, proposed = plan["envelope"], plan["today"], plan["under_plan"]
    capital = float(envelope["capital_usd"])
    deployment = (plan.get("strategy") or {}).get("deployment")
    if (plan.get("strategy") or {}).get("template") == BASELINE and deployment is not None:
        rows = deployment.get("targets") or []
        line = (f"亏损达到 ${envelope['loss_pause_usd']:,.2f}（{envelope['loss_pause_pct']:g}%）时暂停新开仓。",
                (f"New positions stop once the loss reaches ${envelope['loss_pause_usd']:,.2f} "
                 f"({envelope['loss_pause_pct']:g}%)."))
        if rows:
            zh = ("基准建仓：按你的基准比例分批买入 "
                  + "、".join(f"{row['symbol']} ${row['target_usd']:,.2f}（{row['tranches']} 批）" for row in rows)
                  + f"，其余 ${deployment['cash_left_usd']:,.2f} 留现金。" + line[0])
            en = ("Baseline: bought toward the owner's baseline weights in tranches, "
                  + ", ".join(f"{row['symbol']} ${row['target_usd']:,.2f} in {row['tranches']}" for row in rows)
                  + f"; the other ${deployment['cash_left_usd']:,.2f} stays cash. " + line[1])
        else:
            zh = (f"基准建仓：按你的基准比例，没有一只基金分到的金额够最小下单额 "
                  f"${deployment['minimum_order_usd']:,.2f}，一笔也不买。")
            en = (f"Baseline: no fund's share of the baseline weights reaches the "
                  f"${deployment['minimum_order_usd']:,.2f} minimum order, so nothing is bought.")
        return {"zh": zh, "en": en}
    if proposed["any_order"]:
        # Each position by its own figure: the second is sized against what the first left.
        orders = [f"${step['order_usd']:,.2f}" for step in proposed["path"] if step["feasible"]]
        zh = (f"按这个方案，可用资金 ${capital:,.0f} 里实际能投入约 ${proposed['deployable_usd']:,.0f}："
              f"{proposed['positions']} 个仓位（{'、'.join(orders)}）。"
              f"亏损达到 ${envelope['loss_pause_usd']:,.2f}（{envelope['loss_pause_pct']:g}%）时暂停新开仓。")
        en = (f"Under this plan about ${proposed['deployable_usd']:,.0f} of the ${capital:,.0f} can "
              f"actually be invested: {proposed['positions']} position(s) ({', '.join(orders)}). "
              f"New positions stop once the loss reaches "
              f"${envelope['loss_pause_usd']:,.2f} ({envelope['loss_pause_pct']:g}%).")
    else:
        first = next((row["why_not"] for row in proposed["rows"] if row["why_not"]), None)
        zh = "即使按方案调整，也没有一笔订单能同时满足全部限制。" + (first["zh"] if first else "")
        en = ("Even with the plan's changes, no order satisfies every limit at once. "
              + (first["en"] if first else ""))
    if not today["any_order"] and proposed["any_order"]:
        zh += f" 按当前设置则一笔也下不了，需要先确认下面 {len(plan['changes'])} 项调整。"
        en += (f" As configured today no order can be placed at all; the {len(plan['changes'])} "
               "change(s) below have to be confirmed first.")
    return {"zh": zh, "en": en}
