"""Does the research describe the account it was given? Fully offline.

A core-bucket run on 2026-09-04 reasoned throughout about "a $100 MICRO_LIVE bucket" while the
snapshot it was handed said CORE, $2,500. Nothing in the pipeline noticed; the run was caught
only because the model happened to mention the contradiction itself. These cases pin down the
check that now does the noticing, and — as importantly — the nine archived runs it must stay
quiet about.
"""

import pytest

from conservative_trading.evidence import check_evidence, collect_evidence, foreign_bucket_mentions
from conservative_trading.scenarios import TRADE_DATE, price_lookup

# Verbatim from runs/2026-09-04_VTI_873e64ae, the contaminated run.
CONTAMINATED = ("The bull framed cash as an opportunity cost, but this is a $100 MICRO_LIVE "
                "bucket sitting at 100% cash with zero drawdown.")
# Verbatim from runs/2026-09-04_VTI_5068daad, a sound core run. It names SANDBOX in order to
# exclude it, which is what a correctly scoped analysis is supposed to do.
SOUND = ("Target CORE position in VTI stays at 0 shares. Hold the full $2,500 CORE cash. "
         "No SANDBOX action - this analysis is CORE-scoped only.")


def test_the_account_the_run_was_never_shown_is_flagged():
    assert foreign_bucket_mentions({"trader": CONTAMINATED}, ("core", "sandbox")) == ["micro_live"]


def test_naming_the_other_bucket_in_the_same_snapshot_is_not_a_defect():
    """A core run is handed CORE and SANDBOX together; saying "not the sandbox" is correct."""
    assert foreign_bucket_mentions({"trader": SOUND}, ("core", "sandbox")) == []


@pytest.mark.parametrize("text", [
    "VTI is a diversified core holding, trending above every major moving average.",
    "That is the core reason this instrument belongs in a conservative mandate.",
    "A long-horizon core position for retirement accounts.",
])
def test_core_as_an_ordinary_english_word_is_not_an_account_claim(text):
    """This is the whole reason the check is not a substring search: every report says this."""
    assert foreign_bucket_mentions({"market": text}, ("micro_live",)) == []


@pytest.mark.parametrize("text", [
    "recommend-only HOLD for VTI in the MICRO_LIVE bucket",
    "roughly a third of the $100 micro-live allocation on the first tranche",
    "the micro live account is at 100% cash",
])
def test_the_account_is_recognised_however_it_is_spelled(text):
    assert foreign_bucket_mentions({"trader": text}, ("core", "sandbox")) == ["micro_live"]


def test_a_report_about_the_wrong_account_cannot_support_an_opening():
    evidence = collect_evidence("VTI", TRADE_DATE, price_lookup=price_lookup, bucket="core",
                                known_buckets=("core", "sandbox"),
                                reports={"trader": CONTAMINATED})
    ok, reason = check_evidence(evidence, "VTI", TRADE_DATE)
    assert ok is False
    assert "MICRO_LIVE" in reason and "not in the snapshot" in reason


def test_a_sound_report_still_passes():
    evidence = collect_evidence("VTI", TRADE_DATE, price_lookup=price_lookup, bucket="core",
                                known_buckets=("core", "sandbox"), reports={"trader": SOUND})
    assert evidence["foreign_bucket_mentions"] == []
    assert check_evidence(evidence, "VTI", TRADE_DATE)[0] is True


def test_evidence_collected_without_a_bucket_is_unchanged():
    """The scenario harnesses call it without one; they must not acquire a new failure mode."""
    evidence = collect_evidence("VTI", TRADE_DATE, price_lookup=price_lookup,
                                reports={"trader": CONTAMINATED})
    assert "foreign_bucket_mentions" not in evidence
    assert check_evidence(evidence, "VTI", TRADE_DATE)[0] is True


def test_a_debate_transcript_nested_in_the_state_is_read_too():
    """The pipeline's state nests the bull/bear case; the top level alone is not enough."""
    state = {"market_report": "VTI is a core holding.",
             "investment_debate_state": {"judge_decision": CONTAMINATED, "count": 3},
             "risk_debate_state": {"history": ["neutral analyst: no view"]}}
    assert foreign_bucket_mentions(state, ("core", "sandbox")) == ["micro_live"]
