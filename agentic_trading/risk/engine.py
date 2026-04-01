"""Deterministic Hard Risk Layer.

The LLM side (upstream TradingAgents and the Conservative Policy step) may
recommend a trade. This layer decides whether the portfolio can accept it. It
is plain Python over a proposal, a portfolio snapshot, the drawdown ladders and
the market regime. It never calls a model, it evaluates every rule (so the
record shows everything that failed, not just the first), and any exception or
missing input rejects the proposal. Rules and thresholds come from
config/risk_profile.yaml.
"""

from __future__ import annotations

import math
import traceback
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .. import sizing
from ..autonomy import (
    AUTONOMOUS_ACTION,
    MANAGED_EXIT,
    MANAGED_EXIT_SOURCE,
    TRANCHE_CLASSES,
    TRANCHE_PURPOSE,
)
from ..event_calendar import earnings_blackout, macro_blackout, yfinance_next_earnings
from ..portfolio import BucketView, InstrumentClass, Snapshot, realized_on
from ..profile import Profiles
from .ladder import LadderStates, effective_core_level
from .regime import RegimeResult

OPENING_ACTIONS = ("BUY", "ADD")
REDUCING_ACTIONS = ("REDUCE", "SELL")
NEUTRAL_ACTIONS = ("HOLD", "NO_TRADE")


@dataclass(frozen=True)
class _Cap:
    """One limit on how large an opening order may be, in the two forms it is needed in.

    ``limit`` is the room it leaves in dollars, which is what an order is cut to. ``judge`` is
    the rule itself, exactly as it has always been written, put to a given amount: whether it
    passes, the sentence for the record, and the figures behind that sentence. Both come from
    the same numbers, read once, so the room an order is cut to and the rule it is then held
    to cannot drift apart.
    """

    limit: sizing.Limit
    name: str
    judge: Callable[[float], tuple[bool, str, dict[str, Any]]]


@dataclass
class Proposal:
    ticker: str
    bucket: str
    action: str
    size_hint: str
    confidence: int | None
    trade_date: str
    cls: InstrumentClass
    data_quality: str | None = None
    missing_information: list[str] = field(default_factory=list)
    source: str = "conservative_policy"
    # Strategy tag drives the deterministic strategy policy (HR15/HR16). Core accepts only
    # conservative long-only strategies; some sandbox strategies are permanently human-gated.
    strategy: str = "long_only_diversified"
    sell_quantity: float | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    # Whether deterministic code, rather than a person, is standing behind this action. Only
    # the macro blackout reads it: a human approving on an FOMC day has the calendar in view,
    # unattended code does not. Defaults to the safe answer, so a caller that forgets to say
    # gets the human-approved treatment and never the autonomous exemption.
    autonomous: bool = False
    # The size a confirmed strategy asks for, as a share of the allocation, in place of the
    # size hint's own table. It can only ask for less than that table would give at full size:
    # anything above the configured normal size is held to it, and every limit on an order
    # still applies afterwards. None, which is what research produces, sizes from the hint.
    weight_override_pct: float | None = None


def _is_tranche(prop: Proposal) -> bool:
    """Whether this is a baseline plan's tranche: said so in its evidence, and one in fact.

    Only a purchase, only in the real-money bucket, only of a low-tier fund, and only without a
    confidence of its own -- anything else that calls itself a tranche is judged as what it is.
    """
    return (str((prop.evidence or {}).get("purpose") or "") == TRANCHE_PURPOSE
            and prop.action in ("BUY", "ADD") and prop.bucket == "micro_live"
            and prop.cls.limit_class in TRANCHE_CLASSES and prop.confidence is None)


@dataclass
class RuleResult:
    rule_id: str
    name: str
    passed: bool
    detail: str
    blocking: bool = True
    #: The figures ``detail`` was written from, for a reader that has to say the same thing in
    #: another language (agentic_trading.diagnosis). Nothing reads ``detail`` for a number.
    facts: dict[str, Any] = field(default_factory=dict)


@dataclass
class HardRiskResult:
    verdict: str                      # APPROVE | REJECT | NO_ACTION
    final_action: str
    ticker: str
    bucket: str
    proposed_notional_usd: float
    target_weight_pct_of_allocation: float
    post_trade_weight_pct: float
    rules: list[RuleResult]
    ladder: dict[str, Any]
    regime: dict[str, Any]
    advisories: list[str]
    defaults_used: list[str]
    fail_closed_reasons: list[str]
    executable: bool = False          # Stage 2 never executes
    # Set by HR16 for strategies the investor has permanently gated behind explicit
    # confirmation. No autonomy setting in any later stage may clear it.
    requires_human_approval: bool = True
    human_approval_reason: str = "stage 2 produces recommendations only; nothing is executable"
    proposed_quantity: float | None = None
    #: One code per entry of ``fail_closed_reasons``, in the same order: what the engine closed
    #: on, for a reader that must not parse the sentence to find out.
    fail_closed_codes: list[str] = field(default_factory=list)
    #: How the size of an opening order was arrived at (agentic_trading.sizing), once sizing was
    #: reached; None for a reduction, a neutral action, or a proposal refused before it.
    sizing: dict[str, Any] | None = None

    @property
    def failed_rules(self) -> list[RuleResult]:
        return [r for r in self.rules if not r.passed and r.blocking]

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["failed_rule_ids"] = [r.rule_id for r in self.failed_rules]
        return d


StockInfoLookup = Callable[[str], dict[str, Any]]


def yfinance_stock_info(symbol: str) -> dict[str, Any]:
    import yfinance as yf

    return yf.Ticker(symbol).info or {}


class HardRiskEngine:
    def __init__(self, profiles: Profiles, *, halt_file: str | Path | None = None,
                 stock_info_lookup: StockInfoLookup = yfinance_stock_info,
                 earnings_lookup=yfinance_next_earnings):
        self.p = profiles
        self.halt_file = Path(halt_file) if halt_file else profiles.project_dir / str(
            profiles.risk_cfg("execution_policy", "kill_switch", "halt_file", default="state/HALT"))
        self.stock_info_lookup = stock_info_lookup
        self.earnings_lookup = earnings_lookup

    # ------------------------------------------------------------------ helpers
    def _cfg(self, *keys: str, default: Any = None) -> Any:
        return self.p.risk_cfg(*keys, default=default)

    def _kill_switch_active(self) -> tuple[bool, str]:
        if bool(self._cfg("execution_policy", "kill_switch", "trading_halted", default=False)):
            return True, "risk_profile execution_policy.kill_switch.trading_halted is true"
        if self.halt_file.exists():
            return True, f"halt file present: {self.halt_file}"
        return False, "kill switch clear"

    def _hint_weight(self, prop: Proposal, hint: str) -> tuple[float, str]:
        """(target weight, where it came from) for a size hint, from the sizing table."""
        settings = self.p.risk.get("sizing") or {}
        if prop.bucket != "core":
            key = "normal_pct_of_allocation" if hint == "normal" else "small_pct_of_allocation"
            weight = float((settings.get(prop.bucket) or {}).get(key, 30 if hint == "normal" else 15))
            return weight, f"{prop.bucket} {key}"
        core = settings.get("core") or {}
        if prop.cls.limit_class in (core.get("baseline_sleeve_classes") or ["broad_diversified_etf", "bond_or_cash_like_etf"]):
            baseline = self._cfg("buckets", "core", "baseline_allocation_pct_of_core", default={}) or {}
            weight = float(baseline.get(prop.ticker.upper(), core.get("unlisted_baseline_fallback_pct_of_core", 10)))
            basis = "baseline weight" if prop.ticker.upper() in baseline else "unlisted baseline fallback"
            if hint != "normal":
                weight /= 2.0
                basis += ", halved for small size"
            return weight, basis
        key = "satellite_normal_pct_of_core" if hint == "normal" else "satellite_small_pct_of_core"
        return float(core.get(key, 5 if hint == "normal" else 2.5)), f"core {key}"

    def _size(self, prop: Proposal, snap: Snapshot) -> tuple[float, float, str]:
        """Return (notional_usd, target_weight_pct_of_allocation, basis)."""
        bucket = snap.buckets[prop.bucket]
        alloc = bucket.allocation_usd
        weight, basis = self._hint_weight(prop, prop.size_hint)
        asked = prop.weight_override_pct
        if asked is not None:
            # A strategy's own size, held to the table's full size: the table is the setting a
            # person confirmed, and a strategy may ask for less than it but never for more.
            # The engine's own reasons for a small size (thin evidence, a tense regime, a
            # confidence short of the full-size floor) halve it exactly as they halve the table.
            ceiling, _source = self._hint_weight(prop, "normal")
            if not math.isfinite(float(asked)) or float(asked) <= 0:
                raise ValueError(f"a strategy's target weight must be a positive number, got {asked!r}")
            weight = min(float(asked), ceiling)
            basis = ("strategy target weight" if float(asked) <= ceiling + 1e-9 else
                     f"strategy target weight, held to the configured {ceiling:g}% normal size")
            if prop.size_hint != "normal":
                weight /= 2.0
                basis += ", halved for small size"
        target_value = alloc * weight / 100.0
        current = bucket.market_value_of(prop.ticker)
        notional = max(0.0, target_value - current)
        return round(notional, 2), weight, basis

    # ------------------------------------------------------------------ evaluate
    def evaluate(self, prop: Proposal, snap: Snapshot, ladders: LadderStates, regime: RegimeResult) -> HardRiskResult:
        try:
            return self._evaluate(prop, snap, ladders, regime)
        except Exception as exc:  # noqa: BLE001 - fail closed on anything unexpected
            return HardRiskResult(
                verdict="REJECT", final_action="NO_TRADE", ticker=prop.ticker, bucket=prop.bucket,
                proposed_notional_usd=0.0, target_weight_pct_of_allocation=0.0, post_trade_weight_pct=0.0,
                rules=[RuleResult("HR00", "preconditions", False, f"exception in risk engine: {type(exc).__name__}: {exc}")],
                ladder=ladders.as_dict() if ladders else {}, regime=regime.as_dict() if regime else {},
                advisories=[], defaults_used=list(self.p.defaults_used),
                fail_closed_reasons=[f"exception: {type(exc).__name__}: {exc}", traceback.format_exc()[-800:]],
                requires_human_approval=True,
                human_approval_reason="risk engine failed; nothing may proceed without a human",
                fail_closed_codes=["engine_exception", "engine_exception"],
            )

    def _evaluate(self, prop: Proposal, snap: Snapshot, ladders: LadderStates, regime: RegimeResult) -> HardRiskResult:
        rules: list[RuleResult] = []
        advisories: list[str] = []
        fail_closed: list[str] = []
        closed_codes: list[str] = []
        ticker = prop.ticker.upper()

        def rule(rid: str, name: str, passed: bool, detail: str, blocking: bool = True,
                 facts: dict[str, Any] | None = None) -> bool:
            rules.append(RuleResult(rid, name, passed, detail, blocking, dict(facts or {})))
            return passed

        def closed(code: str, reason: str) -> None:
            """Refuse on something no rule names, saying which thing it was beside the sentence."""
            fail_closed.append(reason)
            closed_codes.append(code)

        def done(verdict: str, action: str, notional: float, target_weight: float,
                 post_weight: float, **more: Any) -> HardRiskResult:
            return self._stamp(HardRiskResult(
                verdict, action, ticker, prop.bucket, notional, target_weight, post_weight, rules,
                ladder_view, reg, advisories, list(self.p.defaults_used), fail_closed,
                fail_closed_codes=closed_codes, **more), prop)

        # HR00 preconditions: complete proposal, fresh snapshot, known bucket
        ok = prop.bucket in snap.buckets and prop.action in OPENING_ACTIONS + REDUCING_ACTIONS + NEUTRAL_ACTIONS
        rule("HR00", "preconditions", ok, "proposal complete and bucket known" if ok else f"bad bucket/action: {prop.bucket}/{prop.action}")
        if not ok:
            closed("incomplete_proposal", "incomplete proposal")
        if snap.warnings:
            rule("HR00b", "snapshot warnings", False, "; ".join(snap.warnings))
            closed("snapshot_warnings", "portfolio snapshot warnings")
        max_age = int((self.p.risk.get("portfolio_state") or {}).get("max_price_age_trading_days", 3))
        rule("HR13", "price freshness", snap.max_price_age_trading_days <= max_age,
             f"oldest holding price is {snap.max_price_age_trading_days} trading day(s) old (limit {max_age})",
             facts={"age": snap.max_price_age_trading_days, "max": max_age})

        ladder_level = (effective_core_level(ladders) if prop.bucket == "core"
                        else ladders.for_bucket(prop.bucket).level)
        ladder_view = {**ladders.as_dict(), "effective_level_for_bucket": ladder_level}
        reg = regime.as_dict()

        # Ladder advisories apply regardless of the proposal
        advisories.extend(self._ladder_advisories(ladders, snap))

        # Micro-Live is architecture only: no code path may deploy real money while it is disabled.
        if prop.bucket == "micro_live":
            micro_cfg = self.p.risk.get("micro_live_execution") or self.p.risk.get("micro_live") or {}
            live_on = bool(micro_cfg.get("enabled", False))
            rule("HR17", "micro-live master switch", live_on,
                 "real-money execution is enabled for the Agentic bucket" if live_on
                 else "micro_live_execution.enabled is false: real money is disabled")
            if not live_on:
                closed("real_money_disabled", "micro-live execution disabled")

        if prop.bucket not in snap.buckets:
            closed("bucket_missing", f"bucket {prop.bucket!r} does not exist in the portfolio")
            return done("REJECT", "NO_TRADE", 0.0, 0.0, 0.0)

        # Neutral actions: nothing to check, NO_ACTION
        if prop.action in NEUTRAL_ACTIONS:
            return done("NO_ACTION", "NO_TRADE", 0.0, 0.0,
                        snap.buckets[prop.bucket].weight_of(ticker) if prop.bucket in snap.buckets else 0.0)

        # Risk-reducing actions: allowed whenever the position exists in that bucket
        if prop.action in REDUCING_ACTIONS:
            has = snap.has_position(ticker, prop.bucket)
            rule("HR14", "position exists for reduction", has,
                 f"{ticker} {'is' if has else 'is not'} held in {prop.bucket}")
            position = snap.state.position(ticker, prop.bucket)
            quantity = 0.0
            if position:
                # REDUCE means half the current holding unless an already-approved
                # quantity is being revalidated. SELL means the full holding.
                requested = prop.sell_quantity
                if requested is None:
                    requested = position.quantity * (1.0 if prop.action == "SELL" else 0.5)
                valid_quantity = math.isfinite(requested) and requested > 0
                rule("HR14b", "valid reduction quantity", valid_quantity, f"requested shares: {requested}")
                if valid_quantity:
                    quantity = min(requested, position.quantity)
            verdict = "APPROVE" if has and not fail_closed and all(r.passed or not r.blocking for r in rules) else "REJECT"
            action = prop.action if verdict == "APPROVE" else "NO_TRADE"
            bucket = snap.buckets[prop.bucket]
            current = bucket.market_value_of(ticker)
            notional = current * quantity / position.quantity if position and verdict == "APPROVE" else 0.0
            remaining_weight = max(0.0, current - notional) / bucket.allocation_usd * 100.0
            return done(verdict, action, notional, remaining_weight, remaining_weight,
                        proposed_quantity=quantity if verdict == "APPROVE" else None)

        # ---------------- opening actions ----------------
        bucket = snap.buckets[prop.bucket]
        active, why = self._kill_switch_active()
        rule("HR01", "kill switch", not active, why)

        # HR02 ladder gate
        gate_min_conf: int | None = None
        size_cap_small = False
        if prop.bucket != "core":
            blocked = ladder_level != "NORMAL"
            if prop.bucket == "sandbox":
                blocked = blocked or ladders.total.level != "NORMAL"
            rule("HR02", f"drawdown ladder ({prop.bucket})", not blocked,
                 f"{prop.bucket} ladder level {ladder_level}; total ladder level {ladders.total.level}",
                 facts={"level": ladder_level})
        else:
            if ladder_level in ("RISK_REDUCTION", "HARD_HALT", "EMERGENCY"):
                rule("HR02", "drawdown ladder (core)", False, f"level {ladder_level}: no opening positions in any bucket"
                     + ("; manual reset required" if ladders.total.manual_reset_required or ladders.core.manual_reset_required else ""))
            elif ladder_level == "WARNING":
                allowed_class = prop.cls.limit_class in ("broad_diversified_etf", "bond_or_cash_like_etf")
                rule("HR02", "drawdown ladder (core)", allowed_class,
                     "WARNING: core opens limited to diversified or bond/cash-like ETFs" + ("" if allowed_class else f"; {prop.cls.limit_class} not allowed"))
                gate_min_conf = 75
                size_cap_small = True
            else:
                rule("HR02", "drawdown ladder (core)", True, f"level {ladder_level}")

        # HR03 market regime gate
        gates = (self.p.risk.get("market_regime") or {}).get("gates") or {}
        gate = gates.get(regime.level)
        if isinstance(gate, str) and gate == "no_new_opening_positions":
            rule("HR03", "market regime", False, f"regime {regime.level}: {regime.detail}",
                 facts={"level": regime.level})
            if regime.level == "UNKNOWN":
                closed("regime_unavailable", "market regime data unavailable")
        elif isinstance(gate, dict):
            gate_min_conf = max(gate_min_conf or 0, int(gate.get("min_confidence_to_open", 0)))
            size_cap_small = size_cap_small or gate.get("size_cap") == "small"
            rule("HR03", "market regime", True, f"regime {regime.level}: confidence floor {gate_min_conf}, size capped small")
        else:
            rule("HR03", "market regime", True, f"regime {regime.level}: {regime.detail}")

        # HR18/HR19 event blackouts. They sit with the environment gates because they answer the
        # same question: may a position be opened at all today. Reducing actions returned long
        # before this point, so neither can ever stand between a holding and its exit.
        if prop.cls.limit_class == "individual_stock":
            when, known = self.earnings_lookup(ticker)
            blocked, detail = earnings_blackout(self.p, ticker, prop.trade_date, action=prop.action,
                                                next_earnings=when, known=known)
            rule("HR18", "earnings blackout", not blocked, detail)
            if not known:
                closed("earnings_date_unknown",
                       f"the next earnings date for {ticker} could not be determined")
        blocked, detail = macro_blackout(self.p, prop.trade_date, autonomous=prop.autonomous)
        rule("HR19", "macro release blackout", not blocked, detail)

        # HR15 strategy policy (core is long-only and conservative by construction)
        strat_ok, strat_detail = self._strategy_allowed(prop)
        rule("HR15", "strategy policy", strat_ok, strat_detail)

        # HR04 instrument permissions
        perm_ok, perm_detail = self._permission(prop)
        rule("HR04", "instrument permission", perm_ok, perm_detail)

        # HR05 confidence and data quality
        from ..evidence import check_evidence, check_web_sources

        evidence_ok, evidence_detail = check_evidence(prop.evidence, ticker, prop.trade_date, max_age)
        rule("HR05c", "required market evidence", evidence_ok, evidence_detail)
        from ..proposals import web_evidence_window  # deferred like the import it replaced

        web_ok, web_detail = check_web_sources(prop.evidence, web_evidence_window(self.p, prop.strategy))
        rule("HR05e", "web evidence provenance and freshness", web_ok, web_detail)
        conf = prop.confidence
        floor_small = self.p.confidence.floor(prop.bucket, prop.cls.instrument_type, "small",
                                              limit_class=prop.cls.limit_class,
                                              autonomous=prop.autonomous)
        floor_normal = self.p.confidence.floor(prop.bucket, prop.cls.instrument_type, "normal",
                                               limit_class=prop.cls.limit_class,
                                               autonomous=prop.autonomous)
        floor = max(floor_small, gate_min_conf or 0)
        if _is_tranche(prop):
            # A baseline tranche is bought toward the owner's own weights, not on a research
            # conclusion (contract 2.1, D4): it has no confidence, no list of missing information
            # and no reviewed evidence to be held to. In their place it is only ever a purchase
            # of a low-tier fund in the real-money bucket, which _is_tranche requires, and the
            # price it is sized on is still checked above (HR05c). A regime that asks a purchase
            # for a confidence is one a tranche cannot meet, and is refused here as well as by
            # the plan, which skips such a regime before writing one.
            rule("HR05d", "missing material information", True,
                 "a baseline tranche: no research conclusion, so nothing is listed as missing",
                 blocking=False)
            if gate_min_conf:
                rule("HR05", "confidence", False,
                     f"regime {regime.level} asks a purchase for confidence {gate_min_conf}; a "
                     "baseline tranche carries none", facts={"confidence": None, "floor": floor})
            else:
                rule("HR05", "confidence", True,
                     "a baseline tranche: bought toward the owner's baseline weights, so no "
                     "research confidence is asked of it", blocking=False)
        else:
            rule("HR05d", "missing material information", not prop.missing_information,
                 "; ".join(prop.missing_information) or "no unresolved material information",
                 facts={"count": len(prop.missing_information)})
            if conf is None:
                rule("HR05", "confidence", False, "no confidence supplied")
                closed("missing_confidence", "missing confidence")
            else:
                rule("HR05", "confidence", conf >= floor, f"confidence {conf} vs floor {floor}",
                     facts={"confidence": conf, "floor": floor})
        if _is_tranche(prop):
            rule("HR05b", "data quality", True, "a baseline tranche: no research evidence to rate",
                 blocking=False)
        elif prop.data_quality == "poor":
            rule("HR05b", "data quality", False, "policy reviewer rated the evidence poor; missing: " + "; ".join(prop.missing_information[:4]))
            closed("evidence_poor", "evidence quality poor")
        elif prop.data_quality == "partial":
            size_cap_small = True
            rule("HR05b", "data quality", True, "partial evidence: size capped small", blocking=False)
        else:
            rule("HR05b", "data quality", prop.data_quality == "good", f"data quality {prop.data_quality or 'unrated'}")

        # HR06 deterministic sizing. What the hint asks for is put to every limit on the size of
        # an order at once (agentic_trading.sizing): each is computed below as the room it
        # leaves, and where the bucket has opted in the order is cut to the smallest of them
        # before the minimum order is asked about. Cutting can only shrink. Without the opt-in
        # the order stays at what the hint asked for and each limit refuses it or does not, as
        # it always has.
        effective_hint = prop.size_hint
        if size_cap_small or (conf is not None and conf < floor_normal):
            effective_hint = "small"
        sized = Proposal(**{**asdict(prop), "cls": prop.cls, "size_hint": effective_hint})
        requested, target_weight, basis = self._size(sized, snap)
        minimum, minimum_source = sizing.minimum_order(self.p, prop.bucket)
        caps = self._caps(prop, bucket, ticker)
        plan = sizing.fit(requested, [entry.limit for entry in caps.values()],
                          minimum_usd=minimum, minimum_source=minimum_source,
                          shrink=sizing.fits_to_limits(self.p, prop.bucket))
        notional = self._hold_to_caps(plan, caps)
        sized_as = {**plan.as_dict(), "order": notional, "target_weight_pct": target_weight,
                    "basis": basis, "size_hint": effective_hint}
        if plan.code == sizing.AT_TARGET:
            rule("HR06", "sizing", True,
                 f"nothing to add: {ticker} already at or above its {target_weight:.1f}% target ({basis})",
                 facts=sized_as)
            return done("NO_ACTION", "NO_TRADE", 0.0, target_weight, bucket.weight_of(ticker),
                        sizing=sized_as)
        if plan.code == sizing.TARGET_BELOW_MINIMUM:
            # Not "already at its target", which is what this used to say for both. The hint
            # asks for something; it is just smaller than an order may be. No limit caused that:
            # what conflicts is the size setting and the minimum order at this account size.
            rule("HR06", "sizing", True,
                 f"nothing to order: ${plan.requested_usd:,.2f} toward a {target_weight:.1f}% target "
                 f"({basis}) is below the ${minimum:,.2f} minimum order",
                 facts=sized_as)
            return done("NO_ACTION", "NO_TRADE", 0.0, target_weight, bucket.weight_of(ticker),
                        sizing=sized_as)
        if not plan.feasible:
            # Only where orders are cut to fit: the limits leave less room than the smallest
            # order. The rules below are then put to what was asked for, not to nothing, so the
            # record still shows which limit it was.
            rule("HR06", "sizing", False,
                 f"no order fits: ${plan.requested_usd:,.2f} was asked for, the limits leave room for "
                 f"${plan.legal_max_usd or 0.0:,.2f} ({', '.join(plan.binding) or 'no room'}), and the "
                 f"minimum order is ${minimum:,.2f}",
                 facts=sized_as)
        else:
            rule("HR06", "sizing", True,
                 f"${notional:,.2f} toward a {target_weight:.1f}% target of the {prop.bucket} allocation "
                 f"({basis}; size {effective_hint})"
                 + (f"; cut from ${plan.requested_usd:,.2f} to the room left by {', '.join(plan.binding)}"
                    if plan.code == sizing.SHRUNK else ""),
                 facts=sized_as)
        judged = notional if plan.feasible else plan.requested_usd

        def cap(rule_id: str) -> None:
            entry = caps.get(rule_id)
            if entry is not None:
                passed, detail, facts = entry.judge(judged)
                rule(rule_id, entry.name, passed, detail, facts=facts)

        current_mv = bucket.market_value_of(ticker)
        post_mv = current_mv + notional
        post_weight = post_mv / bucket.allocation_usd * 100.0 if bucket.allocation_usd else 0.0

        # HR07 bucket funding and isolation
        cap("HR07")
        # Every other bucket, not one hard-coded counterpart. The old pairing checked sandbox
        # for a micro_live proposal and silently ignored core, so the rule only half applied to
        # the bucket that deploys real money.
        others = [name for name in snap.buckets if name != prop.bucket]
        elsewhere = [name for name in others if snap.has_position(ticker, name)]
        rule("HR07b", "one symbol one bucket", not elsewhere,
             f"{ticker} is " + (f"held in {', '.join(elsewhere)}" if elsewhere
                                else f"not held in {', '.join(others) or 'any other bucket'}"))
        cap("HR07c")

        # HR08 position and order caps. A per-class cap, where the bucket declares one, is
        # applied as well as the flat cap and never instead of it: a bucket holding both broad
        # ETFs and single names should not size a single name like an index fund.
        cap("HR08")
        cap("HR08b")

        # HR21 risk tiers. A tier is a set of instrument classes sharing one share of the
        # account and one order cap, rather than a separate sub-account: one brokerage account
        # holds one pool of cash, and splitting it into notional envelopes would refuse a buy
        # while the account plainly had the money. classify_instrument decides which tier an
        # order falls in, so a model cannot argue a single name into the low tier.
        tier_name, _tier = self._tier_for(prop.bucket, prop.cls.limit_class)
        if tier_name is None and prop.bucket == "micro_live":
            closed("no_tier", f"{prop.cls.limit_class} belongs to no tier declared for {prop.bucket}")
        cap("HR21")
        cap("HR21b")

        if prop.bucket == "core":
            # HR09 exposure-group overlap (fake diversification)
            max_per_group = int(self._cfg("buckets", "core", "concentration_pct_of_core", "max_etfs_per_exposure_group", default=1))
            group = prop.cls.exposure_group
            held = [s for s in bucket.groups_held.get(group, []) if s != ticker] if group else []
            rule("HR09", "exposure group overlap", len(held) < max_per_group,
                 f"group {group or 'none'}: already held {held or 'nothing'} (max {max_per_group} per group)",
                 facts={"held": ", ".join(held)} if held else None)

            # HR10 sleeve maximum and cash minimum (minimums are targets, maximums are hard)
            cap("HR10")
            cap("HR10b")

            # HR11 concentration
            cap("HR11")
            cap("HR11b")
            max_pos = int(self._cfg("buckets", "core", "concentration_pct_of_core", "max_open_positions", default=8))
            new_count = bucket.n_positions + (0 if snap.has_position(ticker, "core") else 1)
            rule("HR11c", "max open positions", new_count <= max_pos, f"{new_count} positions vs max {max_pos}",
                 facts={"max": max_pos})
            # HR04b core stock eligibility screen
            if prop.cls.limit_class == "individual_stock":
                ok_s, det = self._stock_screen(ticker)
                rule("HR04b", "core stock eligibility screen", ok_s, det)
        else:
            # This branch used to carry a single max-positions check, which left the bucket that
            # deploys actual capital with none of the diversification rules the conceptual core
            # book has: no exposure-group check, no cash floor, no combined-stock or sector cap
            # and no eligibility screen. At $100 of ETFs that mattered little. With real capital
            # and individual stocks permitted it is the gap that matters most, so the same rules
            # apply here, from this bucket's own numbers.
            section = ("buckets", prop.bucket)
            conc = self._cfg(*section, "concentration_pct_of_allocation", default={}) or {}
            # For real money, silence is a refusal rather than a bypass. A conceptual bucket may
            # legitimately have no diversification limits; the one holding real capital may not.
            if prop.bucket == "micro_live" and not conc:
                closed("limits_missing", f"{prop.bucket} has no concentration_pct_of_allocation limits")

            if conc.get("max_etfs_per_exposure_group") is not None:
                max_per_group = int(conc["max_etfs_per_exposure_group"])
                group = prop.cls.exposure_group
                held = [s for s in bucket.groups_held.get(group, []) if s != ticker] if group else []
                rule("HR09", "exposure group overlap", len(held) < max_per_group,
                     f"group {group or 'none'}: already held {held or 'nothing'} (max {max_per_group} per group)",
                     facts={"held": ", ".join(held)} if held else None)
            cap("HR10b")
            cap("HR11")
            cap("HR11b")
            max_pos = int(self._cfg(*section, "position_limits", "max_open_positions", default=3))
            # Was hard-coded to "sandbox", so adding to an existing micro_live holding counted as
            # a new position and was refused once the bucket held its maximum.
            new_count = bucket.n_positions + (0 if snap.has_position(ticker, prop.bucket) else 1)
            rule("HR11c", "max open positions", new_count <= max_pos, f"{new_count} positions vs max {max_pos}",
                 facts={"max": max_pos})
            if prop.cls.limit_class == "individual_stock":
                screened = bool(self._cfg(*section, "require_stock_eligibility_screen", default=False))
                if prop.bucket == "micro_live" and not screened:
                    closed("no_screen",
                           f"{prop.bucket} would trade an individual stock with no eligibility screen")
                if screened:
                    ok_s, det = self._stock_screen(ticker)
                    rule("HR04b", "stock eligibility screen", ok_s, det)

        # HR12 activity limits (new positions per day / week) from the recorded opens
        if not snap.has_position(ticker, prop.bucket):
            section = ("buckets", prop.bucket, "activity_limits")
            per_day = int(self._cfg(*section, "max_new_positions_per_day", default=1))
            per_week = int(self._cfg(*section, "max_new_positions_per_week", default=3 if prop.bucket == "core" else 2))
            today, week = self._recent_open_counts(snap, prop.bucket, prop.trade_date)
            rule("HR12", "new positions per day", today < per_day, f"{today} opened today vs max {per_day}",
                 facts={"max": per_day})
            rule("HR12b", "new positions per week", week < per_week, f"{week} opened this week vs max {per_week}",
                 facts={"max": per_week})

        # HR23 the autonomous allowance: the share of the account code may have opened for
        # itself at any one time. Measured on provenance rather than on a separate sub-account,
        # so it is one pool of cash with a boundary drawn through it. A position a person later
        # adds to still counts, because the alternative would free capacity by touching it.
        cap("HR23")

        # HR22 daily loss gate. It stops the day from compounding: after a bad enough day the
        # answer to "should we open something else" is no, whatever the analysis says. Realized
        # only — a gate measured on unrealized marks would fire because the market moved, which
        # is not something stopping trading fixes. It cannot reach a reduction, which is the
        # point: the day it fires is a day cutting risk should still be possible.
        gate_pct = self._cfg("buckets", prop.bucket, "loss_limits",
                             "daily_loss_gate_pct_of_allocation", default=None)
        if gate_pct is not None and bucket.allocation_usd:
            realized = realized_on(snap.state, prop.bucket, prop.trade_date)
            floor_usd = bucket.allocation_usd * float(gate_pct) / 100.0
            rule("HR22", "daily loss gate", realized > floor_usd,
                 f"realized ${realized:,.2f} today vs a floor of ${floor_usd:,.2f} "
                 f"({float(gate_pct):g}% of ${bucket.allocation_usd:,.2f})")

        # HR20 order-rate limits, outside the new-position guard above because they count every
        # order that reached the venue and not only the ones that opened something. Both keys
        # were declared in config with nothing reading them.
        orders_week, sold_today = self._order_activity(snap, prop.bucket, prop.trade_date)
        max_orders = self._cfg("buckets", prop.bucket, "activity_limits", "max_orders_per_week",
                               default=None)
        if max_orders is not None:
            rule("HR20", "orders per week", orders_week < int(max_orders),
                 f"{orders_week} order(s) in the trailing week vs max {int(max_orders)}",
                 facts={"max": int(max_orders)})
        if bool(self._cfg("buckets", prop.bucket, "activity_limits", "no_same_day_round_trips",
                          default=False)):
            # Deliberately one-directional. Buying back what was sold hours ago is the churn this
            # rule exists to stop; selling something bought this morning is risk reduction, and a
            # reduction never reaches this code anyway.
            repurchase = ticker in sold_today
            rule("HR20b", "no same-day round trip", not repurchase,
                 f"{ticker} was sold in {prop.bucket} today, so buying it back is a round trip"
                 if repurchase else f"{ticker} was not sold in {prop.bucket} today")

        blocking_failures = [r for r in rules if not r.passed and r.blocking]
        verdict = "REJECT" if blocking_failures or fail_closed else "APPROVE"
        final_action = prop.action if verdict == "APPROVE" else "NO_TRADE"
        return done(verdict, final_action, notional if verdict == "APPROVE" else 0.0, target_weight,
                    post_weight if verdict == "APPROVE" else bucket.weight_of(ticker),
                    sizing=sized_as)

    # ------------------------------------------------------------------ sizing
    def order_room(self, prop: Proposal, snap: Snapshot) -> dict[str, Any]:
        """What an opening order of this instrument could be, sized exactly as ``evaluate`` sizes it.

        For a plan that has to say, before any research is run, what this account can and cannot
        buy. Only the arithmetic of size: the hint, the room each limit leaves, the order both
        ways of treating that room would give, and the minimum. It judges nothing that is not a
        question of size -- no confidence, no evidence, no ladder, no regime -- so an answer here
        is "an order of this size exists", never "this order would be approved".
        """
        bucket = snap.buckets[prop.bucket]
        ticker = prop.ticker.upper()
        requested, target_weight, basis = self._size(prop, snap)
        minimum, minimum_source = sizing.minimum_order(self.p, prop.bucket)
        caps = self._caps(prop, bucket, ticker)
        limits = [entry.limit for entry in caps.values()]
        cut = sizing.fit(requested, limits, minimum_usd=minimum, minimum_source=minimum_source,
                         shrink=True)
        kept = sizing.fit(requested, limits, minimum_usd=minimum, minimum_source=minimum_source,
                          shrink=False)
        # Left as asked for, an order stands only if no limit refuses it.
        refused = [rule_id for rule_id, entry in caps.items()
                   if kept.feasible and not entry.judge(kept.order_usd)[0]]
        return {
            "ticker": ticker, "limit_class": prop.cls.limit_class, "size_hint": prop.size_hint,
            "target_weight_pct": target_weight, "basis": basis,
            "fits_to_limits": sizing.fits_to_limits(self.p, prop.bucket),
            "cut_to_fit": {**cut.as_dict(),
                           "order": self._hold_to_caps(cut, caps) if cut.feasible else 0.0},
            "as_asked": {**kept.as_dict(), "refused_by": refused,
                         "feasible": kept.feasible and not refused},
            "limits": [{"code": entry.limit.code, "rule_id": rule_id,
                        "room": (None if math.isinf(entry.limit.room)
                                 else sizing.floor_cents(entry.limit.room))}
                       for rule_id, entry in caps.items()],
        }

    def _caps(self, prop: Proposal, bucket: BucketView, ticker: str) -> dict[str, _Cap]:
        """Every limit on the size of this opening order, keyed by the rule that enforces it.

        Each is built once, from the same figures, in both the forms it is used in: the room it
        leaves, and the rule as it has always been written. A limit a bucket does not declare is
        simply absent, exactly as its rule used to be absent from the record.
        """
        caps: dict[str, _Cap] = {}
        name = prop.bucket
        alloc = bucket.allocation_usd
        cash = bucket.cash_usd
        limit_class = prop.cls.limit_class

        def share(usd: float) -> float:
            return usd / alloc * 100.0 if alloc else 0.0

        def room(pct: float) -> float:
            """Dollars in a share of the allocation; unbounded when there is no allocation,
            which is how the percentage rules have always read an empty account."""
            return pct / 100.0 * alloc if alloc else math.inf

        def add(rule_id: str, code: str, max_usd: float, rule_name: str,
                judge: Callable[[float], tuple[bool, str, dict[str, Any]]]) -> None:
            caps[rule_id] = _Cap(sizing.Limit(code, rule_id, max_usd), rule_name, judge)

        add("HR07", "cash", cash, "bucket funding", lambda n: (
            cash + 1e-6 >= n,
            f"{name} cash ${cash:,.2f} vs order ${n:,.2f}; the other bucket is never a funding source",
            {"cash": cash, "order": n}))

        if name != "core":
            deployed = bucket.deployed_cost_usd
            add("HR07c", "deployed_cap", alloc - deployed, f"{name} deployed cap", lambda n: (
                deployed + n <= alloc + 1e-6,
                f"deployed cost ${deployed:,.2f} + ${n:,.2f} vs cap ${alloc:,.0f}",
                {"deployed": deployed, "order": n, "cap": alloc}))

        if name == "core":
            cap_pct = float(self._cfg("buckets", "core", "position_limits_pct_of_core", limit_class, default=5))
            order_cap = float(self._cfg("buckets", "core", "order_rules", "max_single_order_pct_of_core", default=40))
        else:
            flat = float(self._cfg("buckets", name, "position_limits",
                                   "max_single_position_pct_of_allocation", default=30))
            by_class = self._cfg("buckets", name, "position_limits",
                                 "max_pct_of_allocation_by_class", default=None) or {}
            per_class = by_class.get(limit_class)
            cap_pct = min(flat, float(per_class)) if per_class is not None else flat
            order_cap = cap_pct
        current_mv = bucket.market_value_of(ticker)

        def judge_position(n: float) -> tuple[bool, str, dict[str, Any]]:
            post = share(current_mv + n)
            return (post <= cap_pct + 1e-9,
                    f"post-trade {post:.1f}% vs cap {cap_pct:.1f}% for {limit_class}",
                    {"post_pct": post, "cap_pct": cap_pct})

        add("HR08", "position_cap", room(cap_pct) - current_mv, "position cap", judge_position)
        order_cap_usd = alloc * order_cap / 100.0
        add("HR08b", "order_cap", order_cap_usd, "single order cap", lambda n: (
            n <= order_cap_usd + 1e-6, f"order ${n:,.2f} vs {order_cap:.0f}% of allocation",
            {"order": n, "cap": order_cap_usd}))

        tier_name, tier = self._tier_for(name, limit_class)
        if tier_name is not None:
            tier_held = sum(bucket.classes_pct.get(c, 0.0) for c in tier.get("classes") or ())
            if tier.get("max_pct_of_account") is not None:
                tier_cap = float(tier["max_pct_of_account"])

                def judge_tier(n: float) -> tuple[bool, str, dict[str, Any]]:
                    post = tier_held + share(n)
                    return (post <= tier_cap + 1e-9,
                            f"{tier_name} tier post-trade {post:.1f}% vs max {tier_cap:.0f}%",
                            {"tier": tier_name, "post_pct": post, "cap_pct": tier_cap})

                add("HR21", "tier_share", room(tier_cap - tier_held),
                    f"{tier_name} tier share of the account", judge_tier)
            if tier.get("max_order_usd") is not None:
                tier_order = float(tier["max_order_usd"])
                add("HR21b", "tier_order", tier_order, f"{tier_name} tier order cap", lambda n: (
                    n <= tier_order + 1e-6,
                    f"order ${n:,.2f} vs the {tier_name} tier's ${tier_order:,.0f} maximum",
                    {"tier": tier_name, "order": n, "cap": tier_order}))

        if name == "core":
            sleeve = prop.cls.sleeve
            if sleeve:
                band = self._cfg("buckets", "core", "asset_class_bands_pct_of_core", sleeve, default={"max": 100}) or {}
                band_max = band.get("max", 100)
                sleeve_pct = bucket.sleeves_pct.get(sleeve, 0.0)

                def judge_sleeve(n: float) -> tuple[bool, str, dict[str, Any]]:
                    post = sleeve_pct + share(n)
                    return (post <= float(band_max) + 1e-9,
                            f"{sleeve} post-trade {post:.1f}% vs max {band_max}%",
                            {"post_pct": post, "cap_pct": float(band_max)})

                add("HR10", "sleeve_max", room(float(band_max) - sleeve_pct), "sleeve maximum", judge_sleeve)
            min_cash: Any = float(self._cfg("buckets", "core", "cash", "min_cash_pct_of_core", default=10))
            max_stocks: Any = (float(self._cfg("buckets", "core", "concentration_pct_of_core",
                                               "all_individual_stocks_combined", default=15))
                               if limit_class == "individual_stock" else None)
            max_sector: Any = (float(self._cfg("buckets", "core", "concentration_pct_of_core",
                                               "direct_sector_exposure", default=10))
                               if limit_class in ("individual_stock", "sector_factor_thematic_etf") else None)
        else:
            conc = self._cfg("buckets", name, "concentration_pct_of_allocation", default={}) or {}
            min_cash = self._cfg("buckets", name, "cash", "min_cash_pct_of_allocation", default=None)
            max_stocks = (conc.get("all_individual_stocks_combined")
                          if limit_class == "individual_stock" else None)
            max_sector = (conc.get("direct_sector_exposure")
                          if limit_class in ("individual_stock", "sector_factor_thematic_etf") else None)

        if min_cash is not None:
            floor_pct = float(min_cash)

            def judge_cash_floor(n: float) -> tuple[bool, str, dict[str, Any]]:
                post = share(cash - n)
                return (post >= floor_pct - 1e-9,
                        f"post-trade cash {post:.1f}% vs minimum {floor_pct:.0f}%",
                        {"post_pct": post, "min_cash_pct": floor_pct})

            add("HR10b", "cash_floor", cash - (room(floor_pct) if alloc else 0.0), "cash minimum",
                judge_cash_floor)
        if max_stocks is not None:
            stocks_cap = float(max_stocks)

            def judge_stocks(n: float) -> tuple[bool, str, dict[str, Any]]:
                post = bucket.stocks_pct + share(n)
                return (post <= stocks_cap + 1e-9, f"stocks {post:.1f}% vs max {stocks_cap:.0f}%",
                        {"post_pct": post, "cap_pct": stocks_cap})

            add("HR11", "stocks_combined", room(stocks_cap - bucket.stocks_pct), "stocks combined",
                judge_stocks)
        if max_sector is not None:
            sector_cap = float(max_sector)
            sector = prop.cls.sector or f"unclassified:{ticker}"
            sector_pct = bucket.sectors_pct.get(sector, 0.0)

            def judge_sector(n: float) -> tuple[bool, str, dict[str, Any]]:
                post = sector_pct + share(n)
                return (post <= sector_cap + 1e-9,
                        f"sector {sector} post-trade {post:.1f}% vs max {sector_cap:.0f}%",
                        {"post_pct": post, "cap_pct": sector_cap})

            add("HR11b", "sector_cap", room(sector_cap - sector_pct), "direct sector exposure",
                judge_sector)

        allowance = self._cfg("buckets", name, "autonomy", "open", "max_pct_of_account", default=None)
        if prop.autonomous and allowance is not None and alloc:
            allowed = float(allowance)
            # An optional ceiling in dollars beside the share. A share follows the account, so a
            # deposit widens it; a person who said "this much" meant an amount, and the smaller
            # of the two is what holds. Absent, the share alone decides, as it always has.
            declared = self._cfg("buckets", name, "autonomy", "open", "max_usd", default=None)
            dollars = float(declared) if declared is not None else math.inf
            opened_by_code = sum(p.market_value for p in bucket.positions
                                 if str(p.opened_by or "").startswith("autonomy:"))

            def judge_allowance(n: float) -> tuple[bool, str, dict[str, Any]]:
                held = opened_by_code + n
                post = held / alloc * 100.0
                detail = (f"positions opened without a person would be {post:.1f}% of the "
                          f"account vs a limit of {allowed:.0f}%")
                if math.isfinite(dollars):
                    detail += f", and ${held:,.2f} vs a ceiling of ${dollars:,.2f}"
                return (post <= allowed + 1e-9 and held <= dollars + 1e-6, detail,
                        {"post_pct": post, "cap_pct": allowed})

            add("HR23", "autonomous_allowance", min(room(allowed), dollars) - opened_by_code,
                "autonomous allowance", judge_allowance)
        return caps

    @staticmethod
    def _hold_to_caps(plan: sizing.Sizing, caps: dict[str, _Cap]) -> float:
        """The amount to order, proved against every rule it was cut to fit.

        The room a limit leaves and the rule that enforces it are two readings of one figure, in
        dollars and in shares of the account, so the last cent can fall either side of a
        rounding. An order that was cut to fit is therefore put to each rule before it is
        accepted, and gives up a cent at a time until all of them pass. If it cannot, or what is
        left is below the minimum, that is an inconsistency this layer does not guess past.
        """
        if not plan.feasible:
            return 0.0
        if not plan.shrink:
            return plan.order_usd
        order = plan.order_usd
        for _ in range(5):
            if all(entry.judge(order)[0] for entry in caps.values()):
                if order + 1e-9 < plan.minimum_usd:
                    break
                return order
            order = sizing.floor_cents(order - sizing.CENT)
        raise RuntimeError("an order cut to its limits could not be made to satisfy all of them")

    # ------------------------------------------------------------------ pieces
    def _strategy_allowed(self, prop: Proposal) -> tuple[bool, str]:
        """Core is long-only and conservative; sandbox strategies must be separately permissioned."""
        sp = (self.p.risk.get("strategy_policy") or {}).get(prop.bucket) or {}
        strategy = (prop.strategy or "unspecified").lower()
        if prop.bucket == "core":
            forbidden = {str(x).lower() for x in sp.get("forbidden_strategies", [])}
            allowed = {str(x).lower() for x in sp.get("allowed_strategies", [])}
            if strategy in forbidden:
                return False, f"core is {sp.get('stance', 'long_only')}: strategy {strategy!r} is forbidden in core"
            if allowed and strategy not in allowed:
                return False, f"core accepts only {sorted(allowed)}; got {strategy!r}"
            return True, f"core {sp.get('stance', 'long_only')} strategy {strategy!r} allowed (momentum may inform timing only)"
        gated = {str(x).lower() for x in ((self.p.risk.get("strategy_policy") or {}).get("sandbox") or {}).get(
            "strategies_requiring_human_approval_always", [])}
        if strategy in gated:
            perms = self.p.risk.get("permissions") or {}
            node = (perms.get(strategy) or {}).get(prop.bucket, False)
            val = self.p.effective(node, f"risk_profile.permissions.{strategy}.{prop.bucket}")
            enabled = bool(val.get("enabled", False)) if isinstance(val, dict) else bool(val)
            return enabled, (f"sandbox strategy {strategy!r} is {'permitted' if enabled else 'not permitted yet'}; "
                             "it permanently requires explicit human approval before any execution")
        return True, f"sandbox strategy {strategy!r} carries no special gate"


    def _human_gated(self, prop: Proposal) -> tuple[bool, str]:
        sp = (self.p.risk.get("strategy_policy") or {}).get("sandbox") or {}
        gated = {str(x).lower() for x in sp.get("strategies_requiring_human_approval_always", [])}
        strategy = (prop.strategy or "unspecified").lower()
        if prop.bucket != "core" and strategy in gated:
            return True, (f"strategy {strategy!r} permanently requires explicit human confirmation before execution; "
                          "no autonomy setting in any later stage can clear this")
        if prop.bucket == "micro_live":
            # Real capital is human-gated by default, and stays so for everything that takes on
            # risk. The one exception is a REDUCE in a bucket that names reduce-autonomy in its
            # own config: cutting a position is the single action whose worst outcome is being
            # out of a trade rather than in one. This is eligibility only — agentic_trading
            # .autonomy decides at execution time, against live state this layer cannot see.
            if (prop.action == AUTONOMOUS_ACTION and self._cfg(
                    "buckets", prop.bucket, "autonomy", "reduce", "enabled", default=False) is True):
                return False, ""
            # An exit from shares code itself bought under a confirmed plan, where the bucket
            # has switched that on. The one case a SELL is eligible: the claim is the proposal's
            # own, and autonomy.permits holds it to the fills before anything is approved.
            if (prop.action in REDUCING_ACTIONS and prop.source == MANAGED_EXIT_SOURCE and self._cfg(
                    "buckets", prop.bucket, "autonomy", MANAGED_EXIT, "enabled", default=False) is True):
                return False, ""
            # Opening is the other block, switched separately because it creates risk rather
            # than cutting it, and additionally limited to the instrument classes named there.
            if prop.action in OPENING_ACTIONS and self._cfg(
                    "buckets", prop.bucket, "autonomy", "open", "enabled", default=False) is True:
                openable = self._cfg("buckets", prop.bucket, "autonomy", "open", "classes",
                                     default=[]) or []
                if prop.cls.limit_class in openable:
                    return False, ""
            return True, "micro-live deploys real capital and always requires explicit human confirmation"
        return False, ""

    def _stamp(self, result: HardRiskResult, prop: Proposal) -> HardRiskResult:
        """Attach the permanent human-approval requirement. Never weakens it."""
        gated, reason = self._human_gated(prop)
        result.requires_human_approval = True
        if gated:
            result.human_approval_reason = reason
        elif prop.bucket == "micro_live":
            # Reaching here means _human_gated found no permanent gate for a real-money
            # proposal, which it only does for the named action-and-switch combinations. The
            # decision is not repeated here, so the two cannot drift apart.
            result.requires_human_approval = False
            result.human_approval_reason = (
                f"the bucket permits autonomy for {prop.action}; whether it is actually taken "
                "is decided at execution time by agentic_trading.autonomy"
            )
        else:
            result.human_approval_reason = (
                "stage 2 produces recommendations only; nothing is executable and every future "
                "execution mode above recommend_only starts at human_approval"
            )
        return result

    def _permission(self, prop: Proposal) -> tuple[bool, str]:
        perms = self.p.risk.get("permissions") or {}
        if prop.cls.product_flags:
            return False, "excluded or unverified product structure: " + ", ".join(prop.cls.product_flags)
        if prop.cls.instrument_type in ("etf", "stock") and not prop.cls.identity_verified:
            return False, "unverified instrument: ETFs must belong to the explicit configured universe"

        def allowed(name: str) -> bool:
            node = (perms.get(name) or {}).get(prop.bucket, False)
            val = self.p.effective(node, f"risk_profile.permissions.{name}.{prop.bucket}")
            if isinstance(val, dict):
                return bool(val.get("enabled", False))
            return bool(val)

        if prop.cls.instrument_type == "crypto":
            return allowed("crypto"), f"crypto in {prop.bucket}: {'allowed' if allowed('crypto') else 'not permitted'}"
        if prop.bucket == "core" and prop.cls.limit_class == "individual_stock":
            ok = bool(self.p.investor_cfg("universe", "core", "individual_stocks", "allowed", default=False))
            return ok, f"individual stocks in core: {'allowed with stronger evidence' if ok else 'not permitted'}"
        return True, f"{prop.cls.limit_class} permitted in {prop.bucket}"

    def _stock_screen(self, ticker: str) -> tuple[bool, str]:
        screen = self._cfg("buckets", "core", "stock_eligibility_screen", default={}) or {}
        try:
            info = self.stock_info_lookup(ticker) or {}
        except Exception as exc:  # noqa: BLE001
            return False, f"screen data unavailable ({type(exc).__name__}); failing closed"
        checks: list[tuple[str, bool, str]] = []
        mc = info.get("marketCap")
        checks.append(("market cap", mc is not None and mc >= float(screen.get("min_market_cap_usd", 1e10)), f"{mc}"))
        fcf = info.get("freeCashflow")
        if screen.get("positive_free_cash_flow_ttm", True):
            checks.append(("positive FCF", fcf is not None and fcf > 0, f"{fcf}"))
        ni = info.get("netIncomeToCommon")
        if screen.get("positive_net_income_ttm", True):
            checks.append(("positive net income", ni is not None and ni > 0, f"{ni}"))
        de = info.get("debtToEquity")
        checks.append(("debt/equity", de is not None and de <= float(screen.get("max_debt_to_equity_pct", 150)), f"{de}"))
        cr = info.get("currentRatio")
        checks.append(("current ratio", cr is not None and cr >= float(screen.get("min_current_ratio", 1.0)), f"{cr}"))
        failed = [f"{n} ({v})" for n, ok, v in checks if not ok]
        return (not failed), ("passed all screen checks" if not failed else "failed: " + ", ".join(failed))

    @staticmethod
    def _recent_open_counts(snap: Snapshot, bucket: str, trade_date: str) -> tuple[int, int]:
        import pandas as pd

        td = pd.to_datetime(trade_date)
        today = week = 0
        for o in snap.state.recent_opens:
            if o.bucket != bucket:
                continue
            d = pd.to_datetime(o.date)
            if d.normalize() == td.normalize():
                today += 1
            if 0 <= (td - d).days < 7:
                week += 1
        return today, week

    def _tier_for(self, bucket: str, limit_class: str) -> tuple[str | None, dict[str, Any]]:
        """The tier a class of instrument belongs to, or (None, {}) if it belongs to none.

        Belonging to no tier is not permission: for the bucket holding real money the caller
        fails closed on it, so adding an instrument class the table does not mention refuses
        rather than sizing it against nothing.
        """
        for name, tier in (self._cfg("buckets", bucket, "tiers", default={}) or {}).items():
            if limit_class in (tier.get("classes") or []):
                return str(name), dict(tier)
        return None, {}

    @staticmethod
    def _order_activity(snap: Snapshot, bucket: str, trade_date: str) -> tuple[int, set[str]]:
        """(orders in the trailing week, symbols sold today) for one bucket.

        Read from the ledger's applied_orders, which is the durable record of what actually
        reached the venue. recent_opens cannot serve here: it only knows about positions that
        were opened, so it sees neither an add nor a sell, and those are most of what an
        order-rate limit is about.
        """
        import pandas as pd

        td = pd.to_datetime(trade_date).normalize()
        week = 0
        sold_today: set[str] = set()
        for order in (snap.state.applied_orders or {}).values():
            if not isinstance(order, dict) or order.get("bucket") != bucket:
                continue
            dates = [pd.to_datetime(e["trade_date"]) for e in (order.get("events") or [])
                     if isinstance(e, dict) and e.get("trade_date")]
            if not dates:
                continue
            placed = min(dates).normalize()
            if 0 <= (td - placed).days < 7:
                week += 1
            if placed == td and str(order.get("side", "")).lower() == "sell":
                sold_today.add(str(order.get("symbol", "")).upper())
        return week, sold_today

    @staticmethod
    def _ladder_advisories(ladders: LadderStates, snap: Snapshot) -> list[str]:
        """Advisories about the conceptual portfolio. A ledger holding only the real-money
        bucket has no core or sandbox to de-risk, so there is nothing to say."""
        out: list[str] = []
        if "core" not in snap.buckets:
            micro = ladders.micro_live
            if micro is not None and micro.level != "NORMAL":
                out.append(f"Agentic ladder at {micro.level} ({micro.drawdown_pct:+.1f}% from its high-water "
                           "mark): no new opens; review the positions and escalate rather than adding risk.")
            return out
        lvl = effective_core_level(ladders)
        if lvl == "WARNING":
            out.append("WARNING level: no new sandbox positions; core opens limited to diversified ETFs at confidence 75+.")
        if lvl in ("RISK_REDUCTION", "HARD_HALT", "EMERGENCY"):
            sandbox = snap.buckets.get("sandbox")
            core = snap.buckets.get("core")
            sb = [p.symbol for p in sandbox.positions] if sandbox else []
            sat = ([p.symbol for p in core.positions
                    if p.cls.limit_class in ("individual_stock", "sector_factor_thematic_etf")] if core else [])
            out.append(f"{lvl}: no opening positions. Proposed de-risking for human review: close sandbox {sb or 'none'}, "
                       f"close core satellites {sat or 'none'}, reduce core broad equity toward "
                       f"{'35' if lvl == 'RISK_REDUCTION' else '20'}% of core. Diversified core holdings are never sold automatically.")
        if lvl in ("HARD_HALT", "EMERGENCY"):
            out.append(f"{lvl} is sticky: a manual reset with a written note is required before any opening resumes.")
        if ladders.sandbox.level == "HALT":
            out.append("Sandbox HALT: close all sandbox positions (if defensive autonomy is ever enabled) and freeze the sandbox until manual reset.")
        return out
