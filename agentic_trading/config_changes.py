"""Configuration edits made from the console, with the same discipline as an order.

The console can now write ``config/``. Every edit, whether it came from a form or from a
person retyping one section of a file, goes through one pipeline:

1. the change is described (a section's new text, or values for dotted paths),
2. the whole file is rebuilt from it, comments intact,
3. the rebuilt file is validated by the same loader the service runs on,
4. the change is classified, leaf by leaf, as tightening or loosening,
5. it is applied at once, or parked for a cooling period if it loosens anything.

Loosening is decided by a rule table and it fails closed: a guarded path whose direction the
table cannot tell is treated as loosening. Paths outside the guarded subtrees -- a schedule, a
watchlist, a notification channel -- are never held. A parked change is re-applied against the
file as it is when the period ends, and refuses if the part it touches moved meanwhile.

Nothing here decides *whether* the resulting configuration is safe; that remains the loader's
and the risk engine's job. This module only makes sure the file a person wrote from a phone
is the file they saw, and that widening a boundary is a slow act.
"""

from __future__ import annotations

import copy
import difflib
import hashlib
import io
import re
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from datetime import date as calendar_date
from pathlib import Path
from typing import Any

import yaml
from ruamel.yaml import YAML

from .profile import TODO, ProfileError, load_profiles

#: The files the console may edit. The mandate documents are prompts, not settings, and `.env`
#: holds this deployment's secrets; neither is offered.
FILES = ("risk_profile.yaml", "investor_profile.yaml", "event_calendar.yaml")

#: How long a loosening change waits before it is applied, and can still be withdrawn.
COOLING_HOURS = 24

#: The deployment-owned document the change records live in, and how many are kept.
RECORD = "config_changes"
MAX_RECORDS = 200

PENDING, APPLIED, CANCELLED, FAILED = "pending", "applied", "cancelled", "failed"
#: How a change lands: at once, after the cooling period, or -- for the two autonomy switches
#: alone (see ``switch_change``) -- at once because the console password confirmed a loosening.
IMMEDIATE, COOLING, CONFIRMED = "immediate", "cooling", "confirmed"
TIGHTENING, LOOSENING, NEUTRAL, UNKNOWN = "tightening", "loosening", "neutral", "unknown"

_MISSING = object()


class ConfigChangeError(ValueError):
    """The change cannot be made as described; the message says why, for a person."""


# ---------------------------------------------------------------------------- text helpers
def revision(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


_TOP_KEY = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):")


@dataclass
class Section:
    key: str
    start: int          # first line, inclusive, comment block included
    end: int            # last line, exclusive; trailing blank lines excluded
    text: str

    @property
    def lines(self) -> int:
        return self.end - self.start


def sections(text: str) -> list[Section]:
    """The file's top-level keys, each with the comment block directly above it.

    A comment block belongs to the key it sits against with no blank line between. Blank lines
    and comment paragraphs separated from the next key by a blank line stay between sections
    and are never edited.
    """
    lines = text.splitlines()
    key_lines = [i for i, line in enumerate(lines) if _TOP_KEY.match(line)]
    starts = []
    for key_line in key_lines:
        start = key_line
        while start > 0 and lines[start - 1].startswith("#"):
            start -= 1
        starts.append(start)
    out: list[Section] = []
    for n, key_line in enumerate(key_lines):
        end = starts[n + 1] if n + 1 < len(key_lines) else len(lines)
        while True:
            while end > key_line + 1 and not lines[end - 1].strip():
                end -= 1
            # A comment paragraph at the tail, set off by a blank line, is floating text between
            # sections rather than part of this one.
            floating = end
            while floating > key_line + 1 and lines[floating - 1].startswith("#"):
                floating -= 1
            if floating < end and floating > key_line + 1 and not lines[floating - 1].strip():
                end = floating
                continue
            break
        key = _TOP_KEY.match(lines[key_line]).group(1)
        out.append(Section(key=key, start=starts[n], end=end, text="\n".join(lines[starts[n]:end])))
    return out


def section(text: str, key: str) -> Section:
    for item in sections(text):
        if item.key == key:
            return item
    raise ConfigChangeError(f"no top-level section named {key!r}")


def replace_section(text: str, key: str, new_text: str) -> str:
    current = section(text, key)
    new_lines = new_text.rstrip("\n").splitlines()
    parsed = _parse(new_text)
    if list(parsed) != [key]:
        raise ConfigChangeError(
            f"the section must contain exactly the top-level key {key!r}; it contains "
            f"{sorted(parsed) or 'nothing'}")
    lines = text.splitlines()
    rebuilt = lines[:current.start] + new_lines + lines[current.end:]
    return "\n".join(rebuilt) + ("\n" if text.endswith("\n") else "")


def _parse(text: str) -> dict[str, Any]:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigChangeError(f"not valid YAML: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigChangeError("the document must be a mapping")
    return data


def _yaml() -> YAML:
    rt = YAML(typ="rt")
    rt.preserve_quotes = True
    rt.width = 4096
    rt.indent(mapping=2, sequence=4, offset=2)
    return rt


def set_values(text: str, updates: dict[str, Any], *, create: frozenset[str] = frozenset()) -> str:
    """The file with each dotted path set, comments and layout preserved.

    A path that lands on a ``{value, recommended_default}`` node sets its ``value``, so the
    reasoning kept beside a recommended default survives the decision to replace it.

    A path that does not exist is refused -- a misspelt key would otherwise become a new setting
    nothing reads -- unless it is named in ``create``, which adds it to the end of its parent
    mapping. Only code that names its keys itself passes ``create``; no request text reaches it.
    """
    changed = _dump_with(text, updates, create)
    if not updates:
        return changed
    # ruamel re-aligns flow mappings and spells `null` its own way on the way out, which would
    # make a one-line edit read as fifty. Its untouched output maps line for line onto the
    # original, so the original's lines are kept wherever ruamel did not change anything.
    untouched = _dump_with(text, {})
    original_lines, untouched_lines = text.splitlines(), untouched.splitlines()
    if len(original_lines) != len(untouched_lines):
        return changed
    changed_lines = changed.splitlines()
    out: list[str] = []
    matcher = difflib.SequenceMatcher(a=untouched_lines, b=changed_lines, autojunk=False)
    for tag, a0, a1, b0, b1 in matcher.get_opcodes():
        out.extend(original_lines[a0:a1] if tag == "equal" else changed_lines[b0:b1])
    return "\n".join(out) + ("\n" if changed.endswith("\n") else "")


def _dump_with(text: str, updates: dict[str, Any], create: frozenset[str] = frozenset()) -> str:
    rt = _yaml()
    try:
        doc = rt.load(text)
    except Exception as exc:  # ruamel raises a family of its own
        raise ConfigChangeError(f"not valid YAML: {exc}") from exc
    if doc is None or not hasattr(doc, "items"):
        raise ConfigChangeError("the document must be a mapping")
    for path, value in updates.items():
        parts = [p for p in str(path).split(".") if p]
        if not parts:
            raise ConfigChangeError("a value needs a non-empty path")
        node = doc
        for part in parts[:-1]:
            if not hasattr(node, "get") or node.get(part) is None or not hasattr(node[part], "get"):
                raise ConfigChangeError(f"no such setting: {path}")
            node = node[part]
        leaf = parts[-1]
        if not hasattr(node, "get") or (leaf not in node and path not in create):
            raise ConfigChangeError(f"no such setting: {path}")
        target = node.get(leaf)
        if (hasattr(target, "get") and "value" in target and "recommended_default" in target
                and not isinstance(value, dict)):
            target["value"] = _plain(value)
        else:
            node[leaf] = _plain(value)
    out = io.StringIO()
    rt.dump(doc, out)
    return out.getvalue()


def _plain(value: Any) -> Any:
    """JSON-shaped input as plain Python, dates included, so ruamel writes it as a scalar."""
    if isinstance(value, str):
        try:
            return calendar_date.fromisoformat(value) if len(value) == 10 else value
        except ValueError:
            return value
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    return value


def effective(node: Any) -> Any:
    """A leaf's value as the loader would use it: the recommended default when it is TODO."""
    if isinstance(node, dict) and "value" in node and "recommended_default" in node:
        return node["recommended_default"] if node["value"] == TODO else node["value"]
    return node


def get_value(doc: dict[str, Any], path: str) -> Any:
    node: Any = doc
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return effective(node)


def unified_diff(old: str, new: str, name: str) -> str:
    return "".join(difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True),
        fromfile=f"{name} (current)", tofile=f"{name} (proposed)", n=2))


# ------------------------------------------------------------------------ classification
#: Top-level subtrees whose changes are examined; anything outside them applies at once.
GUARDED: dict[str, tuple[str, ...]] = {
    "risk_profile.yaml": (
        "micro_live_execution", "buckets", "execution_policy", "permissions", "drawdown_ladder",
        "sizing", "confidence_requirements", "approval", "revalidation", "proposal_freshness",
        "data_freshness", "strategy_policy", "idempotency", "broker", "event_rules",
        "unresolved_policy", "robinhood"),
    "investor_profile.yaml": (
        "accept_all_recommended_defaults", "unresolved_policy", "capital", "risk_tolerance",
        "universe.core.always_excluded", "universe.core.individual_stocks.allowed",
        "universe.core.individual_stocks.evidence_bar", "universe.core.max_etfs_per_exposure_group",
        "universe.core.foundation", "llm.never_use_api_key", "tradingagents.llm_provider"),
    "event_calendar.yaml": (),
}

TRUE_LOOSER, FALSE_LOOSER = "true_looser", "false_looser"
HIGHER_LOOSER, LOWER_LOOSER = "higher_looser", "lower_looser"
LONGER_LOOSER, SHORTER_LOOSER = "longer_looser", "shorter_looser"

#: First match wins. ``*`` is one path segment; a trailing ``**`` is the rest of the path.
#: A guarded path with no rule, or whose values the rule cannot compare, counts as loosening.
RULES: tuple[tuple[str, Any], ...] = (
    ("**.reasoning", NEUTRAL),
    # --- real money
    ("micro_live_execution.enabled", TRUE_LOOSER),
    ("micro_live_execution.allow_autonomous_opening", TRUE_LOOSER),
    ("micro_live_execution.permissions.*", TRUE_LOOSER),
    ("micro_live_execution.max_capital_usd", HIGHER_LOOSER),
    ("micro_live_execution.capital_tolerance_usd", HIGHER_LOOSER),
    ("micro_live_execution.refuse_if_account_equity_above_usd", HIGHER_LOOSER),
    ("micro_live_execution.execution_mode", ["recommend_only", "human_approval", "autonomous"]),
    ("micro_live_execution.order_rules.max_review_quote_drift_pct", HIGHER_LOOSER),
    ("micro_live_execution.preflight_checks", SHORTER_LOOSER),
    ("micro_live_execution.bucket_name", NEUTRAL),
    ("micro_live_execution.ledger_file", NEUTRAL),
    ("micro_live_execution.risk_state_file", NEUTRAL),
    ("micro_live_execution.freshness_class", NEUTRAL),
    ("robinhood.use_broker_pretrade_review", FALSE_LOOSER),
    ("robinhood.**", NEUTRAL),
    # --- execution policy
    ("execution_policy.stage", HIGHER_LOOSER),
    ("execution_policy.broker_environment", ["paper", "live"]),
    ("execution_policy.live_trading_approved", TRUE_LOOSER),
    ("execution_policy.buckets.*.execution_mode", ["recommend_only", "human_approval", "autonomous"]),
    ("execution_policy.stage_allowed_modes.*", LONGER_LOOSER),
    ("execution_policy.human_approval.channel", NEUTRAL),
    ("execution_policy.human_approval.proposal_ttl_minutes", HIGHER_LOOSER),
    ("execution_policy.human_approval.revalidate_all_rules_at_submit", FALSE_LOOSER),
    ("execution_policy.human_approval.max_price_drift_pct_since_proposal", HIGHER_LOOSER),
    ("execution_policy.autonomy.*.enabled", TRUE_LOOSER),
    ("execution_policy.autonomy.*.allowed_actions", LONGER_LOOSER),
    ("execution_policy.autonomy.*.allowed_purposes", LONGER_LOOSER),
    ("execution_policy.autonomy.*.max_*", HIGHER_LOOSER),
    ("execution_policy.autonomy.*.min_confidence", LOWER_LOOSER),
    ("execution_policy.autonomy.common_conditions.require_ladder_level",
     ["NORMAL", "WARNING", "RISK_REDUCTION", "HARD_HALT", "EMERGENCY"]),
    ("execution_policy.autonomy.common_conditions.require_stage_at_least", LOWER_LOOSER),
    ("execution_policy.autonomy.common_conditions.*", FALSE_LOOSER),
    ("execution_policy.autonomy.auto_downgrade.sticky_until_manual_reset", FALSE_LOOSER),
    ("execution_policy.autonomy.auto_downgrade.triggers", SHORTER_LOOSER),
    ("execution_policy.kill_switch.halt_file", NEUTRAL),
    ("execution_policy.kill_switch.trading_halted", FALSE_LOOSER),
    ("execution_policy.kill_switch.broker_suspend_trade_on_halt", FALSE_LOOSER),
    # --- buckets
    ("buckets.*.allocation_usd", HIGHER_LOOSER),
    ("buckets.*.allocation_usd_max", HIGHER_LOOSER),
    ("buckets.*.sizing_basis", NEUTRAL),
    ("buckets.*.may_stay_all_cash", NEUTRAL),
    ("buckets.*.profit_sweep", NEUTRAL),
    ("buckets.core.asset_class_bands_pct_of_core.*.min", LOWER_LOOSER),
    ("buckets.core.asset_class_bands_pct_of_core.*.max", HIGHER_LOOSER),
    ("buckets.core.baseline_allocation_pct_of_core.*", NEUTRAL),
    ("buckets.core.initial_deployment.tranches", LOWER_LOOSER),
    ("buckets.core.initial_deployment.spacing_trading_days", LOWER_LOOSER),
    ("buckets.core.initial_deployment.regime_gate", FALSE_LOOSER),
    ("buckets.core.position_limits_pct_of_core.*", HIGHER_LOOSER),
    ("buckets.core.concentration_pct_of_core.*", HIGHER_LOOSER),
    ("buckets.*.concentration_pct_of_allocation.*", HIGHER_LOOSER),
    ("buckets.*.cash.min_cash_pct_of_core", LOWER_LOOSER),
    ("buckets.*.cash.min_cash_pct_of_allocation", LOWER_LOOSER),
    ("buckets.*.activity_limits.no_same_day_round_trips", FALSE_LOOSER),
    ("buckets.*.activity_limits.turnover_exempt", LONGER_LOOSER),
    ("buckets.*.activity_limits.discretionary_turnover_pct_of_core.*", HIGHER_LOOSER),
    ("buckets.*.activity_limits.max_*", HIGHER_LOOSER),
    ("buckets.*.per_position_loss_rules_pct_from_cost.*.*", LOWER_LOOSER),
    ("buckets.*.loss_limits.monthly_realized_loss_budget_usd", HIGHER_LOOSER),
    ("buckets.*.loss_limits.*", LOWER_LOOSER),
    ("buckets.core.stock_eligibility_screen.listed_on", LONGER_LOOSER),
    ("buckets.core.stock_eligibility_screen.min_*", LOWER_LOOSER),
    ("buckets.core.stock_eligibility_screen.max_*", HIGHER_LOOSER),
    ("buckets.core.stock_eligibility_screen.positive_*", FALSE_LOOSER),
    ("buckets.*.position_limits.max_*", HIGHER_LOOSER),
    ("buckets.*.position_limits.max_pct_of_allocation_by_class.*", HIGHER_LOOSER),
    ("buckets.*.tiers.*.classes", LONGER_LOOSER),
    ("buckets.*.tiers.*.max_*", HIGHER_LOOSER),
    ("buckets.*.autonomy.*.enabled", TRUE_LOOSER),
    ("buckets.*.autonomy.*.classes", LONGER_LOOSER),
    ("buckets.*.autonomy.*.max_*", HIGHER_LOOSER),
    ("buckets.*.autonomy.*.min_confidence", LOWER_LOOSER),
    ("buckets.*.autonomy.*.min_remaining_pct_of_opened", LOWER_LOOSER),
    ("buckets.*.require_stock_eligibility_screen", FALSE_LOOSER),
    ("buckets.*.order_rules.regular_hours_only", FALSE_LOOSER),
    ("buckets.*.order_rules.broker_side_stop_for_equities", FALSE_LOOSER),
    ("buckets.*.order_rules.marketable_limit_band_pct.*", HIGHER_LOOSER),
    ("buckets.*.order_rules.market_orders_allowed_for", LONGER_LOOSER),
    ("buckets.*.order_rules.max_single_order_pct_of_core", HIGHER_LOOSER),
    ("buckets.*.order_rules.fractional_shares", NEUTRAL),
    ("buckets.*.order_rules.time_in_force", NEUTRAL),
    ("buckets.*.order_rules.default_order_type", NEUTRAL),
    # --- permissions, ladders, sizing, confidence
    ("permissions.fractional_shares.*", NEUTRAL),
    ("permissions.*.*", TRUE_LOOSER),
    ("drawdown_ladder.measurement.basis", NEUTRAL),
    ("drawdown_ladder.measurement.persisted_state_file", NEUTRAL),
    ("drawdown_ladder.measurement.high_water_mark_start_usd", LOWER_LOOSER),
    ("drawdown_ladder.measurement.cashflow_adjusted", FALSE_LOOSER),
    ("drawdown_ladder.measurement.evaluated_on", SHORTER_LOOSER),
    ("drawdown_ladder.*.levels.*.name", NEUTRAL),
    ("drawdown_ladder.*.levels.*.trigger_pct", LOWER_LOOSER),
    ("drawdown_ladder.*.levels.*.actions", SHORTER_LOOSER),
    ("drawdown_ladder.*.actions.*", SHORTER_LOOSER),
    ("drawdown_ladder.de_escalation.hysteresis_pct_points", LOWER_LOOSER),
    ("drawdown_ladder.de_escalation.manual_reset_levels", SHORTER_LOOSER),
    ("drawdown_ladder.core.mirror_total_portfolio_levels", FALSE_LOOSER),
    ("sizing.core.baseline_sleeve_classes", LONGER_LOOSER),
    ("sizing.sizing_basis", NEUTRAL),
    ("sizing.**", HIGHER_LOOSER),
    ("confidence_requirements.**", LOWER_LOOSER),
    # --- proposals, freshness, approval
    ("approval.required_always_for_strategies", SHORTER_LOOSER),
    ("approval.approval_ttl_minutes", HIGHER_LOOSER),
    ("revalidation.allow_size_reduction", NEUTRAL),
    ("revalidation.min_reduction_ratio", LOWER_LOOSER),
    ("revalidation.reanalysis_triggers", SHORTER_LOOSER),
    ("revalidation.invalidate_triggers", SHORTER_LOOSER),
    ("revalidation.outcomes", NEUTRAL),
    ("proposal_freshness.default_class", NEUTRAL),
    ("proposal_freshness.classes.*.ttl_minutes", HIGHER_LOOSER),
    ("proposal_freshness.classes.*.stale_after_minutes", HIGHER_LOOSER),
    ("proposal_freshness.classes.*.max_price_drift_pct", HIGHER_LOOSER),
    ("proposal_freshness.classes.*.require_market_open", FALSE_LOOSER),
    ("proposal_freshness.classes.*.news_recheck", FALSE_LOOSER),
    ("proposal_freshness.classes.*.news_recheck_mandatory", FALSE_LOOSER),
    ("data_freshness.*.max_*", HIGHER_LOOSER),
    ("data_freshness.*.event_driven_max_age_minutes", HIGHER_LOOSER),
    ("data_freshness.*.fields", SHORTER_LOOSER),
    ("strategy_policy.*.allowed_strategies", LONGER_LOOSER),
    ("strategy_policy.*.forbidden_strategies", SHORTER_LOOSER),
    ("strategy_policy.*.strategies_requiring_human_approval_always", SHORTER_LOOSER),
    ("strategy_policy.*.never_autonomous", FALSE_LOOSER),
    ("idempotency.client_order_id_prefix", NEUTRAL),
    ("idempotency.check_broker_before_submit", FALSE_LOOSER),
    ("idempotency.max_submit_attempts", HIGHER_LOOSER),
    ("broker.order_defaults.extended_hours", TRUE_LOOSER),
    ("broker.order_defaults.fractional", NEUTRAL),
    ("broker.order_defaults.time_in_force", NEUTRAL),
    ("broker.marketable_limit_band_pct.*", HIGHER_LOOSER),
    ("broker.submit_timeout_seconds", NEUTRAL),
    ("broker.reconcile_after_submit_seconds", NEUTRAL),
    ("broker.state_file", NEUTRAL),
    ("broker.fills_file", NEUTRAL),
    ("event_rules.earnings.*_days_*", LOWER_LOOSER),
    ("event_rules.earnings.source", NEUTRAL),
    ("event_rules.earnings.applies_to", NEUTRAL),
    ("event_rules.macro_blackout_autonomous_only.fail_closed_if_missing", FALSE_LOOSER),
    # --- investor profile
    ("accept_all_recommended_defaults", TRUE_LOOSER),
    ("capital.total_usd", HIGHER_LOOSER),
    ("capital.buckets.*.allocation_usd", HIGHER_LOOSER),
    ("capital.buckets.*.allocation_usd_max", HIGHER_LOOSER),
    ("capital.buckets.*.purpose", NEUTRAL),
    ("capital.buckets.*.may_stay_all_cash", NEUTRAL),
    ("capital.brokerage_layout", NEUTRAL),
    ("capital.external_cashflows_expected", NEUTRAL),
    ("risk_tolerance.max_portfolio_drawdown_pct", HIGHER_LOOSER),
    ("risk_tolerance.react_before_limit", FALSE_LOOSER),
    ("risk_tolerance.preferred_operating_model", ["recommend_only", "human_approval", "autonomous"]),
    ("universe.core.always_excluded", SHORTER_LOOSER),
    ("universe.core.individual_stocks.allowed", TRUE_LOOSER),
    ("universe.core.max_etfs_per_exposure_group", HIGHER_LOOSER),
    ("llm.never_use_api_key", FALSE_LOOSER),
)


def _match(pattern: str, path: str) -> bool:
    p_parts, parts = pattern.split("."), path.split(".")
    if p_parts and p_parts[-1] == "**":
        head = p_parts[:-1]
        if len(parts) < len(head) + 1:
            return False
        return all(_seg(a, b) for a, b in zip(head, parts, strict=False))
    if len(p_parts) != len(parts):
        return False
    return all(_seg(a, b) for a, b in zip(p_parts, parts, strict=True))


def _seg(pattern: str, part: str) -> bool:
    if pattern == "*":
        return True
    if "*" in pattern:
        return re.fullmatch(re.escape(pattern).replace(r"\*", ".*"), part) is not None
    return pattern == part


def _rule(path: str) -> Any:
    for pattern, rule in RULES:
        if _match(pattern, path):
            return rule
    return None


def leaf_diff(old: Any, new: Any, path: str = "") -> list[tuple[str, Any, Any]]:
    """Every leaf whose effective value differs, as (path, old, new); missing is ``None``."""
    old, new = effective(old), effective(new)
    out: list[tuple[str, Any, Any]] = []
    if isinstance(old, dict) and isinstance(new, dict):
        for key in dict.fromkeys([*old, *new]):
            sub = f"{path}.{key}" if path else str(key)
            out.extend(leaf_diff(old.get(key, _MISSING), new.get(key, _MISSING), sub))
        return out
    if (isinstance(old, list) and isinstance(new, list) and old and new
            and all(isinstance(x, dict) for x in old + new)):
        for i in range(max(len(old), len(new))):
            sub = f"{path}.{i}"
            out.extend(leaf_diff(old[i] if i < len(old) else _MISSING,
                                 new[i] if i < len(new) else _MISSING, sub))
        return out
    if old is _MISSING and new is _MISSING:
        return out
    if old is not _MISSING and new is not _MISSING and _same(old, new):
        return out
    return [(path, None if old is _MISSING else old, None if new is _MISSING else new)]


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    if isinstance(a, int | float) and isinstance(b, int | float):
        return float(a) == float(b)
    return a == b


def _guarded(file: str, path: str) -> bool:
    return any(path == prefix or path.startswith(prefix + ".") for prefix in GUARDED.get(file, ()))


def _effect(rule: Any, old: Any, new: Any) -> str:
    if rule == NEUTRAL:
        return NEUTRAL
    if rule is None or old is None or new is None:
        return UNKNOWN
    try:
        if rule in (TRUE_LOOSER, FALSE_LOOSER):
            if not isinstance(old, bool) or not isinstance(new, bool):
                return UNKNOWN
            looser = new is (rule == TRUE_LOOSER)
            return LOOSENING if looser else TIGHTENING
        if rule in (HIGHER_LOOSER, LOWER_LOOSER):
            if isinstance(old, bool) or isinstance(new, bool):
                return UNKNOWN
            a, b = float(old), float(new)
            if a == b:
                return NEUTRAL
            return LOOSENING if (b > a) == (rule == HIGHER_LOOSER) else TIGHTENING
        if rule in (LONGER_LOOSER, SHORTER_LOOSER):
            if not isinstance(old, list) or not isinstance(new, list):
                return UNKNOWN
            added = [x for x in new if x not in old]
            removed = [x for x in old if x not in new]
            widened = bool(added) if rule == LONGER_LOOSER else bool(removed)
            if widened:
                return LOOSENING
            return TIGHTENING if (added or removed) else NEUTRAL
        if isinstance(rule, list):
            if old not in rule or new not in rule:
                return UNKNOWN
            a, b = rule.index(old), rule.index(new)
            return NEUTRAL if a == b else (LOOSENING if b > a else TIGHTENING)
    except (TypeError, ValueError):
        return UNKNOWN
    return UNKNOWN


def classify(file: str, old_text: str, new_text: str) -> dict[str, Any]:
    """Every changed leaf with its effect, and the verdict for the change as a whole."""
    changes = []
    for path, old, new in leaf_diff(_parse(old_text), _parse(new_text)):
        if not _guarded(file, path):
            effect = NEUTRAL
        else:
            effect = _effect(_rule(path), old, new)
        changes.append({"path": path, "from": old, "to": new, "effect": effect})
    held = [c["path"] for c in changes if c["effect"] in (LOOSENING, UNKNOWN)]
    return {"classification": COOLING if held else IMMEDIATE, "changes": changes,
            "reasons": held}


# -------------------------------------------------------------------------- validation
def validate(config_dir: Path, file: str, new_text: str) -> None:
    """Refuse a file the running service could not load. The loader is the authority."""
    if file not in FILES:
        raise ConfigChangeError(f"{file} is not a file the console may edit")
    current = _parse((Path(config_dir) / file).read_text(encoding="utf-8"))
    proposed = _parse(new_text)
    if set(current) != set(proposed):
        gone = sorted(set(current) - set(proposed))
        new = sorted(set(proposed) - set(current))
        raise ConfigChangeError(
            "top-level sections may not be added or removed from the console"
            + (f"; removed: {gone}" if gone else "") + (f"; added: {new}" if new else ""))
    with tempfile.TemporaryDirectory(prefix="ct-config-") as tmp:
        cfg = Path(tmp) / "config"
        shutil.copytree(config_dir, cfg)
        (cfg / file).write_text(new_text, encoding="utf-8")
        try:
            profiles = load_profiles(cfg)
        except ProfileError as exc:
            raise ConfigChangeError(str(exc)) from exc
        if file == "event_calendar.yaml":
            from .event_calendar import load_macro_calendar

            calendar = load_macro_calendar(profiles)
            if calendar.error:
                raise ConfigChangeError(calendar.error)


# ------------------------------------------------------------------------- change specs
@dataclass
class ChangeSpec:
    """What a person asked for: one section's new text, or values for dotted paths."""

    file: str
    key: str | None = None
    text: str | None = None
    set: dict[str, Any] = field(default_factory=dict)

    @property
    def kind(self) -> str:
        return "section" if self.key is not None else "values"

    def check(self) -> None:
        if self.file not in FILES:
            raise ConfigChangeError(f"{self.file} is not a file the console may edit")
        if (self.key is None) == (not self.set):
            raise ConfigChangeError("describe the change as one section's text or as values, not both")
        if self.key is not None and self.text is None:
            raise ConfigChangeError("a section change needs its text")


def build(current: str, spec: ChangeSpec) -> str:
    spec.check()
    if spec.kind == "section":
        return replace_section(current, spec.key, spec.text or "")
    return set_values(current, spec.set)


def base_of(current: str, spec: ChangeSpec) -> Any:
    """What the change is relative to, so a parked change can tell whether its ground moved."""
    if spec.kind == "section":
        return section(current, spec.key).text
    doc = _parse(current)
    return {path: get_value(doc, path) for path in spec.set}


def moved(current: str, spec: ChangeSpec, base: Any) -> str | None:
    """Why a parked change can no longer be applied, or ``None`` if its ground is unchanged."""
    if spec.kind == "section":
        try:
            now = section(current, spec.key).text
        except ConfigChangeError as exc:
            return str(exc)
        return None if now == base else f"section {spec.key!r} was edited during the cooling period"
    doc = _parse(current)
    for path, before in (base or {}).items():
        if not _same(get_value(doc, path), before):
            return f"{path} was edited during the cooling period"
    return None


def paths_of(spec: ChangeSpec, changes: list[dict[str, Any]]) -> list[str]:
    if changes:
        return [c["path"] for c in changes]
    return [spec.key] if spec.kind == "section" else list(spec.set)


# ------------------------------------------------------------------ the autonomy switches
#: The Agentic account's two autonomy switches, each with the one number it cannot act without:
#: how much of a holding code must leave in place, and how sure research must be before code
#: buys on its own.
#:
#: Owner's decision, 2026-09-23: turning either switch on applies the moment the console
#: password confirms it, instead of after the cooling period. The switch is the whole of that
#: decision, so a day of waiting only delayed it. What stands in for the wait is the password,
#: an urgent notice to every signed-in device, and an off that is one gesture and applies at
#: once. Every other loosening still waits -- including these same keys edited as section text,
#: which goes through ``classify`` like any other change.
SWITCH_BUCKET = "micro_live"
SWITCHES: dict[str, str] = {"reduce": "min_remaining_pct_of_opened", "open": "min_confidence"}

#: What each block must declare before ``autonomy.permits`` will ever say yes. A switch that is
#: on without them would read as on while nothing could act, so it is not turned on at all.
SWITCH_REQUIRES: dict[str, tuple[str, ...]] = {
    "reduce": ("min_confidence", "max_positions_reduced_per_day", "min_remaining_pct_of_opened"),
    "open": ("classes", "min_confidence", "max_new_positions_per_day"),
}


def switch_path(block: str, leaf: str) -> str:
    return f"buckets.{SWITCH_BUCKET}.autonomy.{block}.{leaf}"


def switch_change(current: str, block: str, on: bool, value: int | None = None, *,
                  confidence_floor: int = 80) -> tuple[ChangeSpec, str, dict[str, Any]]:
    """One switch turned on or off: the change, the rebuilt risk_profile.yaml and its verdict.

    Turning a switch on may set its number in the same change, and creates the key when the file
    never declared it. The verdict is the ordinary classification, except that a loosening made
    of this switch and its number alone becomes ``CONFIRMED`` rather than ``COOLING``: the caller
    applies it at once, and only on the console password. Off is a tightening and needs neither.
    """
    if block not in SWITCHES:
        raise ConfigChangeError(f"no autonomy switch named {block!r}; the switches are "
                                + " and ".join(sorted(SWITCHES)))
    number = SWITCHES[block]
    updates: dict[str, Any] = {switch_path(block, "enabled"): bool(on)}
    if value is not None:
        if not on:
            raise ConfigChangeError("a number is set only when a switch is turned on")
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigChangeError(f"{number} must be a whole number")
        if block == "reduce" and not 1 <= value <= 99:
            raise ConfigChangeError("the share of each holding kept must be between 1 and 99 per cent")
        if block == "open" and not confidence_floor <= value <= 100:
            # Below the floor would change nothing: confidence_requirements.minimum_to_open
            # .autonomous_any holds every autonomous action at it, so the number would lie.
            raise ConfigChangeError(f"the confidence needed to buy must be between {confidence_floor} "
                                    f"and 100; confidence_requirements holds every autonomous action "
                                    f"at {confidence_floor} or more")
        updates[switch_path(block, number)] = value
    new_text = set_values(current, updates, create=frozenset({switch_path(block, number)}))
    if on:
        declared = get_value(_parse(new_text), f"buckets.{SWITCH_BUCKET}.autonomy.{block}")
        declared = declared if isinstance(declared, dict) else {}
        missing = [key for key in SWITCH_REQUIRES[block] if declared.get(key) in (None, [], "")]
        if missing:
            raise ConfigChangeError(
                f"buckets.{SWITCH_BUCKET}.autonomy.{block} does not declare {', '.join(missing)}; "
                "turned on without it, the switch would read as on while nothing could ever act")
    verdict = classify("risk_profile.yaml", current, new_text)
    if verdict["classification"] == COOLING:
        allowed = {switch_path(block, "enabled"), switch_path(block, number)}
        stray = [path for path in verdict["reasons"] if path not in allowed]
        if stray:   # nothing above can produce one; refused rather than carried along at once
            raise ConfigChangeError("only the switch and its number may change at once; also "
                                    "changed: " + ", ".join(stray))
        verdict = {**verdict, "classification": CONFIRMED}
    return ChangeSpec(file="risk_profile.yaml", set=updates), new_text, verdict


# ------------------------------------------------------------------------------ records
def new_record(spec: ChangeSpec, *, base: Any, verdict: dict[str, Any], identity: str,
               reason: str, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    pending = verdict["classification"] == COOLING
    return {
        "id": uuid.uuid4().hex[:12],
        "file": spec.file,
        "kind": spec.kind,
        "key": spec.key,
        "text": spec.text,
        "set": copy.deepcopy(spec.set) if spec.set else None,
        "base": base,
        "classification": verdict["classification"],
        "changes": verdict["changes"],
        "reasons": verdict["reasons"],
        "paths": paths_of(spec, verdict["changes"]),
        "requested_at": now.isoformat(),
        "requested_by": identity,
        "reason": reason,
        "apply_after": (now + timedelta(hours=COOLING_HOURS)).isoformat() if pending else None,
        "status": PENDING if pending else APPLIED,
        "applied_at": None if pending else now.isoformat(),
        "outcome": {},
        "error": None,
    }


def spec_of(record: dict[str, Any]) -> ChangeSpec:
    return ChangeSpec(file=record["file"], key=record.get("key"), text=record.get("text"),
                      set=dict(record.get("set") or {}))


def records(db) -> list[dict[str, Any]]:
    return list((db.read_meta(RECORD, {}) or {}).get("changes") or [])


def save(db, items: list[dict[str, Any]], conn=None) -> None:
    db.write_meta(RECORD, {"changes": items[-MAX_RECORDS:]}, conn)


def due(items: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    out = []
    for item in items:
        if item.get("status") != PENDING or not item.get("apply_after"):
            continue
        try:
            if datetime.fromisoformat(item["apply_after"]) <= now:
                out.append(item)
        except ValueError:
            continue
    return out


def public(record: dict[str, Any]) -> dict[str, Any]:
    """A record as the console sees it: no section text, which can be long, and no base."""
    keep = ("id", "file", "kind", "key", "classification", "changes", "reasons", "paths",
            "requested_at", "requested_by", "reason", "apply_after", "status", "applied_at",
            "outcome", "error", "switch")
    return {k: record.get(k) for k in keep}


# ----------------------------------------------------------------------------- the trail
def commit(project_dir: Path, file: str, message: str, *, author: str) -> str:
    """Commit one config file locally. Never pushes, never moves HEAD elsewhere.

    The checkout is the production data directory (see CLAUDE.md), so a change made from the
    phone lands in the same history a change made in an editor would. A failure here is
    reported, not raised: the file is already written and in force.
    """
    project_dir = Path(project_dir)
    rel = f"config/{file}"
    if not (project_dir / ".git").exists():
        return "not a git checkout; nothing committed"
    try:
        subprocess.run(["git", "-C", str(project_dir), "add", "--", rel],
                       check=True, capture_output=True, text=True, timeout=30)
        result = subprocess.run(
            ["git", "-C", str(project_dir), "-c", "user.name=agentic-trading console",
             "-c", "user.email=console@agentic-trading.local", "commit", "--only", "--quiet",
             "-m", message, f"--author={author}", "--", rel],
            check=False, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"git failed: {exc}"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        if "nothing to commit" in detail or "no changes added" in detail:
            return "nothing to commit"
        return f"git commit failed: {detail[:300]}"
    head = subprocess.run(["git", "-C", str(project_dir), "rev-parse", "--short", "HEAD"],
                          check=False, capture_output=True, text=True, timeout=10)
    return f"committed {head.stdout.strip()}" if head.returncode == 0 else "committed"
