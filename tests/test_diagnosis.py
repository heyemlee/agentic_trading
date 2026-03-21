"""Why a run ended without an order: the vocabulary, and that each reason is told apart.

The acceptance this file holds is the plan's (docs/todo.md, P1.2): a strategy that declined, an
account without the cash, an order boxed in by its limits, missing evidence and research that
never finished are five different answers, and none of them may read as a bare "no trade".
Fully offline: every result comes from the real Hard Risk engine over injected prices.
"""

import re
from pathlib import Path

import pytest

from agentic_trading import diagnosis
from agentic_trading.evidence import collect_evidence
from agentic_trading.policy import decide
from agentic_trading.portfolio import PortfolioState, build_snapshot, classify_instrument
from agentic_trading.risk import HardRiskEngine, Proposal
from agentic_trading.risk.ladder import LadderState, LadderStates, evaluate_ladders
from agentic_trading.risk_scenarios import (
    GOOD_STOCK_INFO,
    TRADE_DATE,
    earnings_lookup,
    identity_lookup,
    price_lookup,
    regime,
)

MICRO = "micro_live"
ENGINE_SOURCE = Path(diagnosis.__file__).with_name("risk") / "engine.py"


def evaluate(profiles, ticker, *, cash=100.0, positions=(), size="normal", confidence=85,
             action="BUY", missing=(), data_quality="good", regime_level="NORMAL",
             halt_file="/nonexistent/HALT"):
    """One Agentic-account proposal through the real engine, as a decision record would hold it."""
    state = PortfolioState(as_of=TRADE_DATE, buckets={MICRO: {"cash_usd": cash}},
                           positions=[dict(p) for p in positions], recent_opens=[])
    snap = build_snapshot(state, profiles, TRADE_DATE, price_lookup, identity_lookup)
    ladders = LadderStates(LadderState(3000.0), LadderState(2500.0), LadderState(500.0),
                           LadderState(snap.buckets[MICRO].equity))
    evaluate_ladders(ladders, profiles, TRADE_DATE, None, None, None,
                     micro_live_equity=snap.buckets[MICRO].equity)
    prop = Proposal(ticker=ticker, bucket=MICRO, action=action, size_hint=size,
                    confidence=confidence, trade_date=TRADE_DATE,
                    cls=classify_instrument(ticker, profiles, identity_lookup),
                    data_quality=data_quality, missing_information=list(missing),
                    evidence=collect_evidence(ticker, TRADE_DATE, price_lookup=price_lookup))
    engine = HardRiskEngine(profiles, halt_file=halt_file,
                            stock_info_lookup=lambda _symbol: GOOD_STOCK_INFO,
                            earnings_lookup=earnings_lookup)
    return engine.evaluate(prop, snap, ladders, regime(regime_level))


def record_for(result, *, policy_action="BUY", rating="Buy", confidence=85, codes=None):
    return {"upstream_decision": {"portfolio_manager_signal": rating},
            "policy": {"final_action": policy_action, "confidence": confidence,
                       "reason_codes": codes or []},
            "hard_risk": result.as_dict(),
            "final_recommendation": {"action": result.final_action}}


def no_trade_record(profiles, rating, *, confidence, reviewer=None, has_position=False):
    """A record whose policy step ended in NO_TRADE, with the codes that step writes."""
    action, _size, _reasons, codes = decide(
        rating, confidence=confidence, llm_action=reviewer, bucket=MICRO, instrument_type="etf",
        profiles=profiles, has_position=has_position)
    return {"upstream_decision": {"portfolio_manager_signal": rating},
            "policy": {"final_action": action, "confidence": confidence,
                       "reviewer_action": reviewer, "reason_codes": codes},
            "hard_risk": {"verdict": "NO_ACTION", "rules": []},
            "final_recommendation": {"action": "NO_TRADE"}}


# ---------------------------------------------------------------- the vocabulary itself
def test_every_code_reads_in_both_languages_and_belongs_to_a_known_category():
    for code, (category, zh, en) in diagnosis._TEXT.items():
        assert category in diagnosis.CATEGORIES, code
        plain = diagnosis.Reason(code).text()
        for language, template in (("zh", zh), ("en", en)):
            assert template.strip(), (code, language)
            # With no facts on record the sentence falls back to its plain form, whole.
            assert plain[language] and "{" not in plain[language], (code, language, plain)
    assert set(diagnosis.LABELS) == set(diagnosis.CATEGORIES)


def test_every_rule_the_engine_can_fail_has_a_reason():
    """A rule added to the engine without a sentence here would be reported as "unknown"."""
    source = ENGINE_SOURCE.read_text(encoding="utf-8")
    rule_ids = set(re.findall(r'\b(?:rule|add|cap)\("(HR\d+[a-z]?)"', source))
    assert len(rule_ids) > 30, "the scan found too few rules to be reading the engine"
    unnamed = sorted(rule_ids - set(diagnosis._RULES) - {"HR06"})
    assert unnamed == [], f"rules with no diagnosis: {unnamed}"
    closed = set(re.findall(r'\bclosed\("([a-z_]+)"', source)) | {"engine_exception"}
    assert sorted(closed - set(diagnosis._FAIL_CLOSED)) == []
    for code in (*diagnosis._RULES.values(), *diagnosis._FAIL_CLOSED.values()):
        assert code in diagnosis._TEXT, code


def test_every_limit_sizing_can_name_has_a_name_in_both_languages():
    source = ENGINE_SOURCE.read_text(encoding="utf-8")
    codes = set(re.findall(r'add\("HR\d+[a-z]?", "([a-z_]+)"', source))
    assert codes and sorted(codes - set(diagnosis.LIMIT_NAMES)) == []


# ---------------------------------------------------------------- the five the plan names
def test_a_strategy_that_declined_says_what_it_weighed(profiles):
    hold = diagnosis.classify_record(no_trade_record(profiles, "Hold", confidence=38))
    assert (hold.category, hold.code) == (diagnosis.STRATEGY, "rating_hold_no_position")
    assert "持有" in hold.lead.text()["zh"]

    weak = diagnosis.classify_record(no_trade_record(profiles, "Buy", confidence=62))
    assert (weak.category, weak.code) == (diagnosis.STRATEGY, "confidence_below_floor")
    assert "62" in weak.lead.text()["zh"] and "70" in weak.lead.text()["zh"]

    cautious = diagnosis.classify_record(
        no_trade_record(profiles, "Buy", confidence=90, reviewer="HOLD"))
    assert cautious.code == "reviewer_cautious" and "持有" in cautious.lead.text()["zh"]


def test_an_account_without_the_cash_says_how_much_it_was_short(profiles):
    # About $349 is asked for (35% of a $998 account); $150 of it is cash, the rest a bond fund.
    held = [{"symbol": "BND", "bucket": MICRO, "quantity": 11.8, "cost_basis_per_share": 71.83}]
    result = evaluate(profiles, "VTI", cash=150.0, positions=held)
    assert result.verdict == "REJECT" and "HR07" in {r.rule_id for r in result.failed_rules}
    outcome = diagnosis.classify_record(record_for(result))
    funds = next(r for r in outcome.reasons if r.code == "cash_short")
    assert funds.category == diagnosis.FUNDS
    assert "$150.00" in funds.text()["zh"] and "$349.16" in funds.text()["zh"]
    assert "$150.00" in funds.text()["en"] and "$349.16" in funds.text()["en"]


def test_limits_that_contradict_each_other_are_named_with_their_figures(profiles):
    # 35% of $1,000 is $350 and the low tier's ticket is $200: refused, and it says by what.
    result = evaluate(profiles, "VTI", cash=1000.0)
    outcome = diagnosis.classify_record(record_for(result))
    assert (outcome.category, outcome.code) == (diagnosis.ORDER_LIMIT, "tier_order_cap")
    text = outcome.lead.text()
    assert "$350.00" in text["zh"] and "$200.00" in text["zh"] and "低风险" in text["zh"]
    assert outcome.as_dict()["label"]["zh"] == "订单限制冲突"


def test_an_order_below_the_minimum_is_not_called_already_at_target(profiles):
    """The $100 account: "small" is $20 and the minimum order is $25."""
    result = evaluate(profiles, "VTI", cash=100.0, size="small")
    assert result.verdict == "NO_ACTION" and result.sizing["code"] == "target_below_minimum"
    outcome = diagnosis.classify_record(record_for(result))
    assert (outcome.category, outcome.code) == (diagnosis.ORDER_LIMIT, "target_below_minimum")
    assert "$20.00" in outcome.lead.text()["zh"] and "$25.00" in outcome.lead.text()["zh"]

    # And a holding that really is at its target says that instead: $226 of a $576 account is
    # 39%, past the 35% the hint asks for.
    held = [{"symbol": "VTI", "bucket": MICRO, "quantity": 0.6, "cost_basis_per_share": 376.88}]
    full = evaluate(profiles, "VTI", cash=350.0, positions=held, action="ADD")
    assert full.sizing["code"] == "at_target"
    assert diagnosis.classify_record(record_for(full, policy_action="ADD")).code == "at_target"


def test_missing_evidence_is_data_not_strategy(profiles):
    result = evaluate(profiles, "VTI", missing=("FRED macro series unavailable", "fund flows"))
    outcome = diagnosis.classify_record(record_for(result))
    assert (outcome.category, outcome.code) == (diagnosis.DATA, "material_information_missing")
    assert "2" in outcome.lead.text()["zh"]

    blind = diagnosis.classify_record(record_for(evaluate(profiles, "VTI", regime_level="UNKNOWN")))
    assert (blind.category, blind.code) == (diagnosis.DATA, "regime_unknown")


def test_research_that_never_finished_is_not_a_decision():
    job = {"state": "FAILED", "failure_code": "llm_login_expired", "ticker": "AMZN"}
    outcome = diagnosis.classify_job(job)
    assert (outcome.category, outcome.code) == (diagnosis.RESEARCH, "llm_login_expired")
    assert "重新登录" in outcome.lead.text()["zh"]


def test_the_five_kinds_of_no_trade_are_five_different_answers(profiles):
    """The acceptance: nothing here may collapse into one "no trade"."""
    held = [{"symbol": "BND", "bucket": MICRO, "quantity": 11.8, "cost_basis_per_share": 71.83}]
    outcomes = {
        diagnosis.STRATEGY: diagnosis.classify_record(no_trade_record(profiles, "Hold", confidence=38)),
        diagnosis.FUNDS: diagnosis.Outcome((next(
            r for r in diagnosis.classify_record(record_for(
                evaluate(profiles, "VTI", cash=150.0, positions=held))).reasons
            if r.code == "cash_short"),)),
        diagnosis.ORDER_LIMIT: diagnosis.classify_record(record_for(
            evaluate(profiles, "VTI", cash=100.0, size="small"))),
        diagnosis.DATA: diagnosis.classify_record(record_for(
            evaluate(profiles, "VTI", missing=("one thing",)))),
        diagnosis.RESEARCH: diagnosis.classify_job({"state": "FAILED", "failure_code": "pipeline_error"}),
    }
    for category, outcome in outcomes.items():
        assert outcome.category == category
    shown = [outcome.as_dict() for outcome in outcomes.values()]
    assert len({item["label"]["zh"] for item in shown}) == 5
    assert len({item["summary"]["zh"] for item in shown}) == 5
    assert all(item["label"]["zh"] != "不交易" and item["summary"]["zh"] for item in shown)


# ---------------------------------------------------------------- the others the record needs
def test_a_pause_outranks_every_question_about_the_order(profiles, tmp_path):
    halt = tmp_path / "HALT"
    halt.write_text("2026-09-02T00:00:00+00:00 paused for the test\n", encoding="utf-8")
    result = evaluate(profiles, "VTI", cash=1000.0, halt_file=str(halt))
    outcome = diagnosis.classify_record(record_for(result))
    # The tier ticket refuses it too, but a paused account is what a person must hear first.
    assert (outcome.category, outcome.code) == (diagnosis.RISK_PAUSE, "kill_switch")
    assert "tier_order_cap" in {reason.code for reason in outcome.reasons}


def test_a_run_that_ended_in_a_proposal_says_so_and_who_decides():
    waiting = diagnosis.classify_record({"proposal_id": "p1", "proposal": {
        "action": "BUY", "requires_human_approval": True}})
    assert (waiting.category, waiting.code) == (diagnosis.PROPOSAL, "proposal_waiting")
    assert "买入" in waiting.lead.text()["zh"] and "批准" in waiting.lead.text()["zh"]
    automatic = diagnosis.classify_record({"proposal_id": "p2", "proposal": {
        "action": "REDUCE", "requires_human_approval": False}})
    assert automatic.code == "proposal_automatic" and "减仓" in automatic.lead.text()["zh"]


def test_a_read_only_review_is_advice_and_never_reads_as_a_refused_trade():
    record = {"advisory": True, "policy": {"final_action": "HOLD"},
              "hard_risk": {"verdict": "READ_ONLY"},
              "final_recommendation": {"action": "HOLD", "scope": "read_only"}}
    outcome = diagnosis.classify_record(record)
    assert outcome.category == diagnosis.ADVICE and "不会下单" in outcome.lead.text()["zh"]


def test_an_engine_that_failed_is_a_fault_not_a_judgement(profiles):
    result = HardRiskEngine(profiles).evaluate(
        Proposal("VTI", MICRO, "BUY", "normal", 85, TRADE_DATE, None), None, None, None)
    assert result.verdict == "REJECT" and "engine_exception" in result.fail_closed_codes
    outcome = diagnosis.classify_record(record_for(result))
    assert (outcome.category, outcome.code) == (diagnosis.SYSTEM, "risk_engine_error")


# ---------------------------------------------------------------- records from before the codes
@pytest.mark.parametrize("rating,confidence,reviewer,expected", [
    ("Hold", 38, "NO_TRADE", "rating_hold_no_position"),
    ("Underweight", 55, "NO_TRADE", "rating_bearish_no_position"),
    ("Buy", 44, "BUY", "confidence_below_no_trade"),
    ("Buy", 66, "BUY", "confidence_below_floor"),
    ("Buy", 90, "NO_TRADE", "reviewer_cautious"),
    ("Review", 80, None, "rating_not_tradeable"),
])
def test_an_older_record_is_read_from_the_fields_it_always_carried(rating, confidence, reviewer,
                                                                   expected):
    record = {"upstream_decision": {"portfolio_manager_signal": rating},
              "policy": {"final_action": "NO_TRADE", "confidence": confidence,
                         "reviewer_action": reviewer},
              "hard_risk": {"verdict": "NO_ACTION", "rules": []},
              "final_recommendation": {"action": "NO_TRADE"}}
    outcome = diagnosis.classify_record(record, no_trade_below=50, floor=70)
    assert outcome.code == expected and outcome.category == diagnosis.STRATEGY


def test_an_older_sizing_record_is_told_apart_by_the_two_shares_it_kept():
    below = {"policy": {"final_action": "BUY"}, "hard_risk": {
        "verdict": "NO_ACTION", "rules": [], "target_weight_pct_of_allocation": 20.0,
        "post_trade_weight_pct": 0.0}}
    assert diagnosis.classify_record(below).code == "target_below_minimum"
    full = {"policy": {"final_action": "ADD"}, "hard_risk": {
        "verdict": "NO_ACTION", "rules": [], "target_weight_pct_of_allocation": 35.0,
        "post_trade_weight_pct": 36.2}}
    assert diagnosis.classify_record(full).code == "at_target"


# ---------------------------------------------------------------- research jobs
LOG_OF_2026_10_02 = (
    "# attempt=1 trigger=scheduled started=2026-10-02T20:33:45.127215+00:00\n"
    "[AMZN] stock | bucket=micro_live | analysts=['market', 'social', 'news', 'fundamentals']\n"
    "[AMZN] ladder=NORMAL (+0.00%) | regime=NORMAL | bucket cash=$100.00\n"
    "[AMZN] FAILED: LLM backend error: claude failed (exit 1, subtype 'success'): Failed to "
    "authenticate: OAuth session expired and could not be refreshed\n")


def test_the_runs_of_2026_10_02_are_read_as_an_expired_login_from_their_own_log():
    """They were stored as "exited with code 1"; only the log says what happened."""
    job = {"state": "FAILED", "error": "research pipeline exited with code 1"}
    assert diagnosis.classify_job(job).code == "pipeline_error"          # the row alone cannot say
    assert diagnosis.classify_job(job, log_tail=LOG_OF_2026_10_02).code == "llm_login_expired"


@pytest.mark.parametrize("job,expected", [
    ({"state": "CLAUDE_LIMIT_REACHED"}, "llm_limit"),
    ({"state": "TIMED_OUT", "timeout_seconds": 5400}, "timeout"),
    ({"state": "CANCELLED", "error": "cancelled from the console"}, "cancelled"),
    ({"state": "FAILED", "failure_code": "subject_unavailable"}, "subject_unavailable"),
    ({"state": "FAILED", "error": "worker failed: ValueError: This holding is no longer present; "
                                  "refresh the portfolio."}, "subject_unavailable"),
    ({"state": "FAILED", "error": "research pipeline exited with code 2"}, "portfolio_unavailable"),
    ({"state": "FAILED", "error": "worker exited without a result and recovery attempts are "
                                  "exhausted"}, "worker_crashed"),
    ({"state": "FAILED", "failure_code": "something a newer version wrote"}, "pipeline_error"),
])
def test_each_way_of_not_finishing_has_its_own_answer(job, expected):
    outcome = diagnosis.classify_job(job)
    assert outcome.category == diagnosis.RESEARCH and outcome.code == expected


def test_a_run_that_is_still_going_or_that_completed_has_no_failure_to_explain():
    for state in ("QUEUED", "RUNNING", "COMPLETE"):
        assert diagnosis.classify_job({"state": state}) is None


def test_a_timeout_says_how_long_it_was_given():
    text = diagnosis.classify_job({"state": "TIMED_OUT", "timeout_seconds": 5400}).lead.text()
    assert "90" in text["zh"] and "90-minute" in text["en"]


@pytest.mark.parametrize("code,has_record,failure", [
    (diagnosis.EXIT_LOGIN, False, "llm_login_expired"),
    (diagnosis.EXIT_LIMIT, False, "llm_limit"),
    (diagnosis.EXIT_FAIL_CLOSED, False, "portfolio_unavailable"),
    (diagnosis.EXIT_SUBJECT, False, "subject_unavailable"),
    (diagnosis.EXIT_OK, False, "no_record"),
    (diagnosis.EXIT_PIPELINE, False, "pipeline_error"),
    (17, False, "pipeline_error"),
])
def test_an_exit_code_becomes_a_failure_code_and_a_sentence(code, has_record, failure):
    named, sentence = diagnosis.failure_for_exit(code, has_record=has_record)
    assert named == failure and sentence
    assert named in diagnosis._TEXT
