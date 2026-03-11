"""The fixed Thesis Tracker case pack and its no-tools runner stay offline."""

from __future__ import annotations

from agentic_trading.thesis_trial import (
    ROUTES,
    UPSTREAM_COMMIT,
    UPSTREAM_LICENSE,
    UPSTREAM_SKILL,
    ThesisAssessment,
    load_cases,
    report_name,
    run_case,
    score,
    write_report,
)
from tests.conftest import ROOT

CASES = ROOT / "tests" / "fixtures" / "thesis_tracker_cases.json"


def test_the_frozen_pack_has_twenty_distinct_cases_and_the_required_adversarial_inputs():
    cases = load_cases(CASES)
    assert len(cases) == 20
    assert {case.case_id for case in cases} == {f"TT{i:02}" for i in range(1, 21)}
    assert {case.instrument_type for case in cases} == {"stock", "etf"}
    assert {case.expected_classification for case in cases} == {
        "strengthened", "intact", "weakened", "broken", "insufficient_evidence"}
    text = "\n".join(" ".join(case.developments) for case in cases).lower()
    for marker in ("no company filing", "undated", "eighteen months", "invalidated"):
        assert marker in text


def test_score_uses_declared_labels_and_mentions_not_an_unfounded_market_claim():
    case = load_cases(CASES)[2]
    right = ThesisAssessment(classification="weakened", requires_research=True,
                             affected_pillars=["pricing power"],
                             disconfirming_evidence=["discounting increased by 500 basis points"],
                             missing_information=[], rationale="Discounting and 500 basis points of margin loss weaken it.")
    wrong = right.model_copy(update={"classification": "intact"})
    assert score(case, right)["passed"] is True
    assert score(case, wrong)["passed"] is False


def test_the_runner_uses_the_subscription_adapter_without_tools_and_writes_only_the_requested_trial_dir(
        fake_claude, tmp_path):
    case = load_cases(CASES)[0]
    row = run_case(case, route="thesis-tracker", model="claude-sonnet-5", effort="low",
                   timeout_seconds=30, output_dir=tmp_path)
    assert row["case_id"] == "TT01" and row["route"] == "thesis-tracker"
    assert row["tools_available"] == [] and row["proposal_created"] is False
    assert row["external_tool_calls"] == []
    assert row["source"] == {"upstream_commit": UPSTREAM_COMMIT, "skill": UPSTREAM_SKILL,
                             "license": UPSTREAM_LICENSE}
    assert row["assessment"]["classification"] == "strengthened"  # fake selects first enum
    # The adapter persists audit entries beside the work directory, still inside the trial root.
    assert (tmp_path / "TT01" / "llm_calls.jsonl").exists()


def test_report_keeps_the_two_routes_separate_and_does_not_invent_unmeasured_accuracy(tmp_path):
    cases = load_cases(CASES)
    assessment = ThesisAssessment(classification="intact", requires_research=False,
                                  affected_pillars=[], disconfirming_evidence=[],
                                  missing_information=[], rationale="No material change.")
    rows = [{"case_id": cases[1].case_id, "route": "control", "score": score(cases[1], assessment)}]
    target = write_report(tmp_path / report_name("control"), rows)
    text = target.read_text(encoding="utf-8")
    assert target.name == "control.json" and "fact accuracy" in text
    assert '"external_tool_calls": []' in text
    assert set(ROUTES) == {"control", "thesis-tracker"}
