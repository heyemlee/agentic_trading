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

from ..autonomy import AUTONOMOUS_ACTION
from ..event_calendar import earnings_blackout, macro_blackout, yfinance_next_earnings
from ..portfolio import InstrumentClass, Snapshot, realized_on
from ..profile import Profiles
from .ladder import LadderStates, effective_core_level
from .regime import RegimeResult

OPENING_ACTIONS = ("BUY", "ADD")
REDUCING_ACTIONS = ("REDUCE", "SELL")
NEUTRAL_ACTIONS = ("HOLD", "NO_TRADE")


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


@dataclass
class RuleResult:
    rule_id: str
    name: str
    passed: bool
    detail: str
    blocking: bool = True


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

    def _size(self, prop: Proposal, snap: Snapshot) -> tuple[float, float, str]:
        """Return (notional_usd, target_weight_pct_of_allocation, basis)."""
        sizing = self.p.risk.get("sizing") or {}
        bucket = snap.buckets[prop.bucket]
        alloc = bucket.allocation_usd
        if prop.bucket != "core":
            key = "normal_pct_of_allocation" if prop.size_hint == "normal" else "small_pct_of_allocation"
            weight = float((sizing.get(prop.bucket) or {}).get(key, 30 if prop.size_hint == "normal" else 15))
            basis = f"{prop.bucket} {key}"
        else:
            core = sizing.get("core") or {}
            if prop.cls.limit_class in (core.get("baseline_sleeve_classes") or ["broad_diversified_etf", "bond_or_cash_like_etf"]):
                baseline = self._cfg("buckets", "core", "baseline_allocation_pct_of_core", default={}) or {}
                weight = float(baseline.get(prop.ticker.upper(), core.get("unlisted_baseline_fallback_pct_of_core", 10)))
                basis = "baseline weight" if prop.ticker.upper() in baseline else "unlisted baseline fallback"
                if prop.size_hint != "normal":
                    weight /= 2.0
                    basis += ", halved for small size"
            else:
                key = "satellite_normal_pct_of_core" if prop.size_hint == "normal" else "satellite_small_pct_of_core"
                weight = float(core.get(key, 5 if prop.size_hint == "normal" else 2.5))
                basis = f"core {key}"
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
            )

    def _evaluate(self, prop: Proposal, snap: Snapshot, ladders: LadderStates, regime: RegimeResult) -> HardRiskResult:
        rules: list[RuleResult] = []
        advisories: list[str] = []
        fail_closed: list[str] = []
        ticker = prop.ticker.upper()

        def rule(rid: str, name: str, passed: bool, detail: str, blocking: bool = True) -> bool:
            rules.append(RuleResult(rid, name, passed, detail, blocking))
            return passed

        # HR00 preconditions: complete proposal, fresh snapshot, known bucket
        ok = prop.bucket in snap.buckets and prop.action in OPENING_ACTIONS + REDUCING_ACTIONS + NEUTRAL_ACTIONS
        rule("HR00", "preconditions", ok, "proposal complete and bucket known" if ok else f"bad bucket/action: {prop.bucket}/{prop.action}")
        if not ok:
            fail_closed.append("incomplete proposal")
        if snap.warnings:
            rule("HR00b", "snapshot warnings", False, "; ".join(snap.warnings))
            fail_closed.append("portfolio snapshot warnings")
        max_age = int((self.p.risk.get("portfolio_state") or {}).get("max_price_age_trading_days", 3))
        rule("HR13", "price freshness", snap.max_price_age_trading_days <= max_age,
             f"oldest holding price is {snap.max_price_age_trading_days} trading day(s) old (limit {max_age})")

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
                 "real-money execution is enabled for the Micro-Live bucket" if live_on
                 else "micro_live_execution.enabled is false: real money is disabled")
            if not live_on:
                fail_closed.append("micro-live execution disabled")

        if prop.bucket not in snap.buckets:
            fail_closed.append(f"bucket {prop.bucket!r} does not exist in the portfolio")
            return self._stamp(HardRiskResult("REJECT", "NO_TRADE", ticker, prop.bucket, 0.0, 0.0, 0.0, rules,
                                              ladder_view, reg, advisories, list(self.p.defaults_used), fail_closed), prop)

        # Neutral actions: nothing to check, NO_ACTION
        if prop.action in NEUTRAL_ACTIONS:
            return self._stamp(HardRiskResult("NO_ACTION", "NO_TRADE", ticker, prop.bucket, 0.0, 0.0,
                                              snap.buckets[prop.bucket].weight_of(ticker) if prop.bucket in snap.buckets else 0.0,
                                              rules, ladder_view, reg, advisories, list(self.p.defaults_used), fail_closed), prop)

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
            return self._stamp(HardRiskResult(verdict, action, ticker, prop.bucket, notional,
                                              remaining_weight, remaining_weight, rules, ladder_view, reg,
                                              advisories, list(self.p.defaults_used), fail_closed,
                                              proposed_quantity=quantity if verdict == "APPROVE" else None), prop)

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
                 f"{prop.bucket} ladder level {ladder_level}; total ladder level {ladders.total.level}")
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
            rule("HR03", "market regime", False, f"regime {regime.level}: {regime.detail}")
            if regime.level == "UNKNOWN":
                fail_closed.append("market regime data unavailable")
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
                fail_closed.append(f"the next earnings date for {ticker} could not be determined")
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
        web_ok, web_detail = check_web_sources(prop.evidence, self._web_evidence_window(prop))
        rule("HR05e", "web evidence provenance and freshness", web_ok, web_detail)
        rule("HR05d", "missing material information", not prop.missing_information,
             "; ".join(prop.missing_information) or "no unresolved material information")
        conf = prop.confidence
        floor_small = self.p.confidence.floor(prop.bucket, prop.cls.instrument_type, "small",
                                              limit_class=prop.cls.limit_class,
                                              autonomous=prop.autonomous)
        floor_normal = self.p.confidence.floor(prop.bucket, prop.cls.instrument_type, "normal",
                                               limit_class=prop.cls.limit_class,
                                               autonomous=prop.autonomous)
        floor = max(floor_small, gate_min_conf or 0)
        if conf is None:
            rule("HR05", "confidence", False, "no confidence supplied")
            fail_closed.append("missing confidence")
        else:
            rule("HR05", "confidence", conf >= floor, f"confidence {conf} vs floor {floor}")
        if prop.data_quality == "poor":
            rule("HR05b", "data quality", False, "policy reviewer rated the evidence poor; missing: " + "; ".join(prop.missing_information[:4]))
            fail_closed.append("evidence quality poor")
        elif prop.data_quality == "partial":
            size_cap_small = True
            rule("HR05b", "data quality", True, "partial evidence: size capped small", blocking=False)
        else:
            rule("HR05b", "data quality", prop.data_quality == "good", f"data quality {prop.data_quality or 'unrated'}")

        # HR06 deterministic sizing
        effective_hint = prop.size_hint
        if size_cap_small or (conf is not None and conf < floor_normal):
            effective_hint = "small"
        sized = Proposal(**{**asdict(prop), "cls": prop.cls, "size_hint": effective_hint})
        notional, target_weight, basis = self._size(sized, snap)
        min_order = float((self.p.risk.get("sizing") or {}).get("min_order_usd", 25))
        if notional < min_order:
            rule("HR06", "sizing", True, f"nothing to add: {ticker} already at or above its {target_weight:.1f}% target ({basis})")
            return self._stamp(HardRiskResult("NO_ACTION", "NO_TRADE", ticker, prop.bucket, 0.0, target_weight,
                                              bucket.weight_of(ticker), rules, ladder_view, reg, advisories,
                                              list(self.p.defaults_used), fail_closed), prop)
        rule("HR06", "sizing", True, f"${notional:,.2f} toward a {target_weight:.1f}% target of the {prop.bucket} allocation ({basis}; size {effective_hint})")

        current_mv = bucket.market_value_of(ticker)
        post_mv = current_mv + notional
        post_weight = post_mv / bucket.allocation_usd * 100.0 if bucket.allocation_usd else 0.0

        # HR07 bucket funding and isolation
        rule("HR07", "bucket funding", bucket.cash_usd + 1e-6 >= notional,
             f"{prop.bucket} cash ${bucket.cash_usd:,.2f} vs order ${notional:,.2f}; the other bucket is never a funding source")
        # Every other bucket, not one hard-coded counterpart. The old pairing checked sandbox
        # for a micro_live proposal and silently ignored core, so the rule only half applied to
        # the bucket that deploys real money.
        others = [name for name in snap.buckets if name != prop.bucket]
        elsewhere = [name for name in others if snap.has_position(ticker, name)]
        rule("HR07b", "one symbol one bucket", not elsewhere,
             f"{ticker} is " + (f"held in {', '.join(elsewhere)}" if elsewhere
                                else f"not held in {', '.join(others) or 'any other bucket'}"))
        if prop.bucket != "core":
            cap = bucket.allocation_usd
            rule("HR07c", f"{prop.bucket} deployed cap", bucket.deployed_cost_usd + notional <= cap + 1e-6,
                 f"deployed cost ${bucket.deployed_cost_usd:,.2f} + ${notional:,.2f} vs cap ${cap:,.0f}")

        # HR08 position and order caps
        if prop.bucket == "core":
            cap_pct = float(self._cfg("buckets", "core", "position_limits_pct_of_core", prop.cls.limit_class, default=5))
            order_cap = float(self._cfg("buckets", "core", "order_rules", "max_single_order_pct_of_core", default=40))
        else:
            flat = float(self._cfg("buckets", prop.bucket, "position_limits",
                                   "max_single_position_pct_of_allocation", default=30))
            # A per-class cap, where the bucket declares one, is applied as well as the flat cap
            # and never instead of it: a bucket holding both broad ETFs and single names should
            # not size a single name like an index fund.
            by_class = self._cfg("buckets", prop.bucket, "position_limits",
                                 "max_pct_of_allocation_by_class", default=None) or {}
            per_class = by_class.get(prop.cls.limit_class)
            cap_pct = min(flat, float(per_class)) if per_class is not None else flat
            order_cap = cap_pct
        rule("HR08", "position cap", post_weight <= cap_pct + 1e-9,
             f"post-trade {post_weight:.1f}% vs cap {cap_pct:.1f}% for {prop.cls.limit_class}")
        rule("HR08b", "single order cap", notional <= bucket.allocation_usd * order_cap / 100.0 + 1e-6,
             f"order ${notional:,.2f} vs {order_cap:.0f}% of allocation")

        # HR21 risk tiers. A tier is a set of instrument classes sharing one share of the
        # account and one order cap, rather than a separate sub-account: one brokerage account
        # holds one pool of cash, and splitting it into notional envelopes would refuse a buy
        # while the account plainly had the money. classify_instrument decides which tier an
        # order falls in, so a model cannot argue a single name into the low tier.
        tier_name, tier = self._tier_for(prop.bucket, prop.cls.limit_class)
        if tier_name is None:
            if prop.bucket == "micro_live":
                fail_closed.append(
                    f"{prop.cls.limit_class} belongs to no tier declared for {prop.bucket}")
        else:
            held = sum(bucket.classes_pct.get(c, 0.0) for c in tier.get("classes") or ())
            tier_cap = tier.get("max_pct_of_account")
            if tier_cap is not None:
                post_tier = held + (notional / bucket.allocation_usd * 100.0
                                    if bucket.allocation_usd else 0.0)
                rule("HR21", f"{tier_name} tier share of the account",
                     post_tier <= float(tier_cap) + 1e-9,
                     f"{tier_name} tier post-trade {post_tier:.1f}% vs max {float(tier_cap):.0f}%")
            tier_order = tier.get("max_order_usd")
            if tier_order is not None:
                rule("HR21b", f"{tier_name} tier order cap", notional <= float(tier_order) + 1e-6,
                     f"order ${notional:,.2f} vs the {tier_name} tier's ${float(tier_order):,.0f} maximum")

        if prop.bucket == "core":
            # HR09 exposure-group overlap (fake diversification)
            max_per_group = int(self._cfg("buckets", "core", "concentration_pct_of_core", "max_etfs_per_exposure_group", default=1))
            group = prop.cls.exposure_group
            held = [s for s in bucket.groups_held.get(group, []) if s != ticker] if group else []
            rule("HR09", "exposure group overlap", len(held) < max_per_group,
                 f"group {group or 'none'}: already held {held or 'nothing'} (max {max_per_group} per group)")

            # HR10 sleeve maximum and cash minimum (minimums are targets, maximums are hard)
            sleeve = prop.cls.sleeve
            if sleeve:
                band = self._cfg("buckets", "core", "asset_class_bands_pct_of_core", sleeve, default={"max": 100}) or {}
                post_sleeve = bucket.sleeves_pct.get(sleeve, 0.0) + notional / bucket.allocation_usd * 100.0
                rule("HR10", "sleeve maximum", post_sleeve <= float(band.get("max", 100)) + 1e-9,
                     f"{sleeve} post-trade {post_sleeve:.1f}% vs max {band.get('max', 100)}%")
            min_cash = float(self._cfg("buckets", "core", "cash", "min_cash_pct_of_core", default=10))
            post_cash_pct = (bucket.cash_usd - notional) / bucket.allocation_usd * 100.0
            rule("HR10b", "cash minimum", post_cash_pct >= min_cash - 1e-9,
                 f"post-trade cash {post_cash_pct:.1f}% vs minimum {min_cash:.0f}%")

            # HR11 concentration
            if prop.cls.limit_class == "individual_stock":
                max_stocks = float(self._cfg("buckets", "core", "concentration_pct_of_core", "all_individual_stocks_combined", default=15))
                post_stocks = bucket.stocks_pct + notional / bucket.allocation_usd * 100.0
                rule("HR11", "stocks combined", post_stocks <= max_stocks + 1e-9, f"stocks {post_stocks:.1f}% vs max {max_stocks:.0f}%")
            if prop.cls.limit_class in ("individual_stock", "sector_factor_thematic_etf"):
                max_sector = float(self._cfg("buckets", "core", "concentration_pct_of_core", "direct_sector_exposure", default=10))
                sector = prop.cls.sector or f"unclassified:{ticker}"
                post_sector = bucket.sectors_pct.get(sector, 0.0) + notional / bucket.allocation_usd * 100.0
                rule("HR11b", "direct sector exposure", post_sector <= max_sector + 1e-9,
                     f"sector {sector} post-trade {post_sector:.1f}% vs max {max_sector:.0f}%")
            max_pos = int(self._cfg("buckets", "core", "concentration_pct_of_core", "max_open_positions", default=8))
            new_count = bucket.n_positions + (0 if snap.has_position(ticker, "core") else 1)
            rule("HR11c", "max open positions", new_count <= max_pos, f"{new_count} positions vs max {max_pos}")
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
                fail_closed.append(f"{prop.bucket} has no concentration_pct_of_allocation limits")

            if conc.get("max_etfs_per_exposure_group") is not None:
                max_per_group = int(conc["max_etfs_per_exposure_group"])
                group = prop.cls.exposure_group
                held = [s for s in bucket.groups_held.get(group, []) if s != ticker] if group else []
                rule("HR09", "exposure group overlap", len(held) < max_per_group,
                     f"group {group or 'none'}: already held {held or 'nothing'} (max {max_per_group} per group)")
            min_cash = self._cfg(*section, "cash", "min_cash_pct_of_allocation", default=None)
            if min_cash is not None:
                post_cash_pct = (bucket.cash_usd - notional) / bucket.allocation_usd * 100.0
                rule("HR10b", "cash minimum", post_cash_pct >= float(min_cash) - 1e-9,
                     f"post-trade cash {post_cash_pct:.1f}% vs minimum {float(min_cash):.0f}%")
            if prop.cls.limit_class == "individual_stock" and conc.get("all_individual_stocks_combined") is not None:
                max_stocks = float(conc["all_individual_stocks_combined"])
                post_stocks = bucket.stocks_pct + notional / bucket.allocation_usd * 100.0
                rule("HR11", "stocks combined", post_stocks <= max_stocks + 1e-9,
                     f"stocks {post_stocks:.1f}% vs max {max_stocks:.0f}%")
            if (prop.cls.limit_class in ("individual_stock", "sector_factor_thematic_etf")
                    and conc.get("direct_sector_exposure") is not None):
                max_sector = float(conc["direct_sector_exposure"])
                sector = prop.cls.sector or f"unclassified:{ticker}"
                post_sector = bucket.sectors_pct.get(sector, 0.0) + notional / bucket.allocation_usd * 100.0
                rule("HR11b", "direct sector exposure", post_sector <= max_sector + 1e-9,
                     f"sector {sector} post-trade {post_sector:.1f}% vs max {max_sector:.0f}%")
            max_pos = int(self._cfg(*section, "position_limits", "max_open_positions", default=3))
            # Was hard-coded to "sandbox", so adding to an existing micro_live holding counted as
            # a new position and was refused once the bucket held its maximum.
            new_count = bucket.n_positions + (0 if snap.has_position(ticker, prop.bucket) else 1)
            rule("HR11c", "max open positions", new_count <= max_pos, f"{new_count} positions vs max {max_pos}")
            if prop.cls.limit_class == "individual_stock":
                screened = bool(self._cfg(*section, "require_stock_eligibility_screen", default=False))
                if prop.bucket == "micro_live" and not screened:
                    fail_closed.append(
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
            rule("HR12", "new positions per day", today < per_day, f"{today} opened today vs max {per_day}")
            rule("HR12b", "new positions per week", week < per_week, f"{week} opened this week vs max {per_week}")

        # HR23 the autonomous allowance: the share of the account code may have opened for
        # itself at any one time. Measured on provenance rather than on a separate sub-account,
        # so it is one pool of cash with a boundary drawn through it. A position a person later
        # adds to still counts, because the alternative would free capacity by touching it.
        allowance = self._cfg("buckets", prop.bucket, "autonomy", "open", "max_pct_of_account",
                              default=None)
        if prop.autonomous and allowance is not None and bucket.allocation_usd:
            held = sum(p.market_value for p in bucket.positions
                       if str(p.opened_by or "").startswith("autonomy:"))
            post = (held + notional) / bucket.allocation_usd * 100.0
            rule("HR23", "autonomous allowance", post <= float(allowance) + 1e-9,
                 f"positions opened without a person would be {post:.1f}% of the account "
                 f"vs a limit of {float(allowance):.0f}%")

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
                 f"{orders_week} order(s) in the trailing week vs max {int(max_orders)}")
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
        return self._stamp(HardRiskResult(verdict, final_action, ticker, prop.bucket, notional if verdict == "APPROVE" else 0.0,
                                          target_weight, post_weight if verdict == "APPROVE" else bucket.weight_of(ticker),
                                          rules, ladder_view, reg, advisories, list(self.p.defaults_used), fail_closed), prop)

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

    def _web_evidence_window(self, prop: Proposal) -> float:
        """How old a web retrieval may be, from ``data_freshness.medium``.

        An event-driven thesis is a statement about the current tape, so it takes the shorter
        window the same config already declares for it.
        """
        from ..proposals import STRATEGY_FRESHNESS_CLASS

        medium = (self.p.risk.get("data_freshness") or {}).get("medium") or {}
        default = float(medium.get("max_age_minutes", 1440))
        if STRATEGY_FRESHNESS_CLASS.get((prop.strategy or "").lower()) == "event_driven":
            return float(medium.get("event_driven_max_age_minutes", default))
        return default

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
            return True, "micro-live deploys real capital and always requires explicit human confirmation"
        return False, ""

    def _stamp(self, result: HardRiskResult, prop: Proposal) -> HardRiskResult:
        """Attach the permanent human-approval requirement. Never weakens it."""
        gated, reason = self._human_gated(prop)
        result.requires_human_approval = True
        if gated:
            result.human_approval_reason = reason
        elif prop.bucket == "micro_live" and prop.action == AUTONOMOUS_ACTION:
            # The single case that may start without a person. Narrow on purpose: named bucket,
            # named action, and its own config switch. Everything else keeps the default above,
            # so a caller reading this field cannot mistake silence for permission.
            result.requires_human_approval = False
            result.human_approval_reason = (
                "a reduction where the bucket permits reduce-autonomy; whether it is actually "
                "taken is decided at execution time by agentic_trading.autonomy"
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
                out.append(f"Micro-Live ladder at {micro.level} ({micro.drawdown_pct:+.1f}% from its high-water "
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
