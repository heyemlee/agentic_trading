"""What a source changes, and what it is not allowed to change.

A source that reaches a strategy's research makes it a different strategy, with a validation of
its own to earn; one that does not reach it changes nothing. And the comparison between runs
with and without a source counts what exists, and says "waiting for samples" until there is
enough -- it never prints a return, because there is no account a source could have moved.
"""

import json
import shutil
from datetime import UTC, datetime

import pytest

from agentic_trading import sources
from agentic_trading.auto import store
from agentic_trading.sources import cli as sources_cli
from agentic_trading.sources import comparison
from agentic_trading.state_db import StateDB

FUNDS = {"universe": ["VTI", "BND"], "max_positions": 2, "position_pct": 30}
WITH_A_STOCK = {**FUNDS, "universe": ["VTI", "AAPL"]}


def configure(profiles, **named):
    profiles.risk.setdefault("research_web_access", {})["sources"] = named


def version(profiles, strategy):
    return store.version_id("steady", strategy, store.code_identity(profiles, strategy))


# -------------------------------------------------------------------------- strategy versions
def test_a_plan_of_funds_is_the_same_strategy_whatever_the_sources_are_set_to(profiles):
    assert store.research_kinds(profiles, FUNDS) == ("etf",)
    before = store.code_identity(profiles, FUNDS)
    assert "sources" not in before, "the identity a plan had before the sources existed"
    configure(profiles, reddit={"enabled": True}, sec_form4={"enabled": True})
    assert store.code_identity(profiles, FUNDS) == before
    assert version(profiles, FUNDS) == store.version_id("steady", FUNDS, before)


def test_a_source_that_reaches_the_research_makes_a_new_strategy_version(profiles):
    assert store.research_kinds(profiles, WITH_A_STOCK) == ("etf", "stock"), (
        "a name that is not a listed fund is researched as a stock")
    off = version(profiles, WITH_A_STOCK)
    assert "sources" not in store.code_identity(profiles, WITH_A_STOCK)

    configure(profiles, sec_form4={"enabled": True})
    with_filings = version(profiles, WITH_A_STOCK)
    assert with_filings != off
    assert set(store.code_identity(profiles, WITH_A_STOCK)["sources"]) == {"sec_form4"}

    # What it is asked for is part of what the model is shown.
    configure(profiles, sec_form4={"enabled": True, "lookback_days": 90})
    assert version(profiles, WITH_A_STOCK) not in (off, with_filings)

    configure(profiles, sec_form4={"enabled": True}, reddit={"enabled": True})
    both = version(profiles, WITH_A_STOCK)
    assert both not in (off, with_filings)
    assert set(store.code_identity(profiles, WITH_A_STOCK)["sources"]) == {"reddit", "sec_form4"}

    # Switched off again it is the strategy it was: the old record is the old version's, intact.
    configure(profiles)
    assert version(profiles, WITH_A_STOCK) == off


def test_a_setting_that_changes_how_fast_it_reads_but_not_what_does_not_restart_anything(profiles):
    configure(profiles, sec_form4={"enabled": True})
    before = version(profiles, WITH_A_STOCK)
    configure(profiles, sec_form4={"enabled": True, "max_requests": 40, "timeout_seconds": 10,
                                   "contact_env": "CT_OTHER"})
    assert version(profiles, WITH_A_STOCK) == before


# ---------------------------------------------------------------------------- a control run
def test_a_control_run_can_leave_a_source_out_and_can_never_bring_one_in(profiles, monkeypatch):
    configure(profiles, reddit={"enabled": True})
    monkeypatch.setenv(sources.WITHOUT_ENV, "reddit, sec_form4")
    loaded = sources.load(profiles)
    assert loaded.reddit.enabled is False and loaded.form4.enabled is False
    monkeypatch.setenv(sources.WITHOUT_ENV, "")
    loaded = sources.load(profiles)
    assert loaded.reddit.enabled is True
    assert loaded.form4.enabled is False, "the profile has it off, and nothing here turns it on"


def test_the_research_command_refuses_a_source_it_does_not_know(capsys, monkeypatch):
    from agentic_trading import cli

    monkeypatch.delenv(sources.WITHOUT_ENV, raising=False)
    assert cli.main(["AAPL", "--without-sources", "twitter"]) == 2
    assert "names no such source: twitter" in capsys.readouterr().out
    import os

    assert sources.WITHOUT_ENV not in os.environ


# -------------------------------------------------------------------------------- comparing
def record(run_id, ticker, variant, *, action="NO_TRADE", confidence=50, date="2026-10-01",
           readings=(), model="claude-opus-5", calls=12):
    on = [] if variant == comparison.BASELINE else variant.split("+")
    return {
        "run_id": run_id, "ticker": ticker, "trade_date": date, "instrument_type": "stock",
        "bucket": "core", "created_at": f"{date}T12:00:00+00:00",
        "models": {"deep": model, "quick": "claude-sonnet-5"},
        "upstream_tradingagents_commit": "abc", "policy": {"final_action": action, "confidence": confidence},
        "final_recommendation": {"action": action}, "diagnosis": {"code": "confidence_below_no_trade"},
        "llm_usage": {"calls": calls, "input_tokens": 1000 * calls, "output_tokens": 100 * calls,
                      "tool_calls": 3, "wall_seconds": 60.0, "list_price_equivalent_usd": 0.5},
        "data_sources": {"research_sources": {"switched_on": on, "readings": list(readings)}},
    }


def reading(source, outcome, items=0):
    return {"source": source, "outcome": outcome, "items": [{"id": n} for n in range(items)]}


def test_runs_are_grouped_by_the_sources_they_could_read_and_a_record_from_before_is_neither():
    old = {"run_id": "old", "ticker": "AAPL", "policy": {}, "data_sources": {"declared": []}}
    found = comparison.summarise([
        old,
        record("a", "AAPL", "baseline"),
        record("b", "AAPL", "sec_form4", readings=[reading("sec_form4", sources.OK, 4)]),
        record("c", "MSFT", "reddit+sec_form4", readings=[
            reading("reddit", sources.RESTRICTED), reading("sec_form4", sources.EMPTY)]),
    ])
    assert sorted(found["variants"]) == ["baseline", "reddit+sec_form4", "sec_form4", "unrecorded"]
    assert found["variants"]["unrecorded"]["runs"] == 1
    assert found["variants"]["sec_form4"]["sources"]["sec_form4"]["items_per_answered_read"] == 4.0


def test_how_often_a_source_could_not_be_read_counts_failures_and_not_switches():
    runs = [record(f"r{n}", "AAPL", "reddit", readings=[reading("reddit", outcome, items)])
            for n, (outcome, items) in enumerate([
                (sources.OK, 5), (sources.EMPTY, 0), (sources.RESTRICTED, 0), (sources.TIMEOUT, 0),
                (sources.PARTIAL, 2), (sources.DISABLED, 0), (sources.NOT_APPLICABLE, 0)])]
    reads = comparison.summarise(runs)["variants"]["reddit"]["sources"]["reddit"]
    assert reads["reads"] == 7 and reads["attempted"] == 5, "switched off and not applicable ask nothing"
    assert (reads["answered"], reads["partial"], reads["failed"]) == (2, 1, 2)
    assert reads["failure_rate_pct"] == 40.0
    assert reads["outcomes"][sources.RESTRICTED] == 1


def test_a_pair_is_one_question_under_the_same_models_with_a_different_set_of_sources():
    found = comparison.summarise([
        record("a", "AAPL", "baseline", confidence=55),
        record("b", "AAPL", "sec_form4", confidence=62, action="BUY"),
        record("c", "MSFT", "baseline", confidence=40),
        record("d", "MSFT", "sec_form4", confidence=40, model="another-model"),   # not the same models
        record("e", "NVDA", "sec_form4", confidence=70),                          # no control run
        record("f", "AAPL", "sec_form4", confidence=30, date="2026-10-02"),       # another day
    ])
    pairs = found["pairs"]
    assert pairs["count"] == 1
    (row,) = pairs["rows"]
    assert row["ticker"] == "AAPL" and row["baseline"]["run_id"] == "a"
    assert row["with"]["sec_form4"] == {"run_id": "b", "action": "BUY", "policy_action": "BUY",
                                         "confidence": 62, "action_changed": True,
                                         "confidence_points": 7.0}
    assert pairs["effects"] == {"sec_form4": {"pairs": 1, "action_changed": 1,
                                              "mean_confidence_points": 7.0}}


def test_what_the_subscription_spent_is_per_run_and_nothing_is_called_a_bill():
    found = comparison.summarise([record("a", "AAPL", "baseline", calls=10),
                                  record("b", "MSFT", "baseline", calls=14)])
    usage = found["variants"]["baseline"]["usage"]
    assert usage["calls_per_run"] == 12.0 and usage["input_tokens_per_run"] == 12000.0
    assert set(usage) == {"calls_per_run", "input_tokens_per_run", "output_tokens_per_run",
                          "tool_calls_per_run", "wall_seconds_per_run",
                          "list_price_equivalent_usd_per_run"}


def test_until_there_is_enough_it_says_waiting_and_it_never_reports_a_return():
    few = comparison.summarise([record("a", "AAPL", "baseline"), record("b", "AAPL", "sec_form4")])
    assert few["verdict"]["state"] == "waiting_for_samples" and "等待样本" in few["verdict"]["zh"]
    assert few["returns"]["available"] is False and "个股" in few["returns"]["zh"]
    assert not any("return_pct" in json.dumps(v) or "drawdown_pct" in json.dumps(v)
                   for v in few["variants"].values())

    many = []
    for n in range(comparison.MIN_RUNS):
        many.append(record(f"b{n}", f"T{n}", "baseline", confidence=50))
        many.append(record(f"f{n}", f"T{n}", "sec_form4", confidence=52))
    enough = comparison.summarise(many)
    assert enough["pairs"]["count"] == comparison.MIN_RUNS
    assert enough["verdict"]["state"] == "comparable"
    assert enough["returns"]["available"] is False, "enough research is still not a result on money"
    assert enough["pairs"]["effects"]["sec_form4"]["mean_confidence_points"] == 2.0


def test_wrong_information_is_a_persons_note_against_a_run_and_is_counted_with_it(tmp_path):
    db = StateDB(tmp_path / "state" / "proposals.json")
    with pytest.raises(ValueError, match="no such source"):
        comparison.flag(db, run_id="b", source="twitter", note="x")
    with pytest.raises(ValueError, match="what was wrong"):
        comparison.flag(db, run_id="b", source="reddit", note="  ")
    row = comparison.flag(db, run_id="b", source="reddit", identity="owner",
                          note="the post claimed a buyback that was never announced",
                          now=datetime(2026, 10, 6, 12, 0, tzinfo=UTC))
    assert row == {"run_id": "b", "source": "reddit", "by": "owner", "at": "2026-10-06T12:00:00+00:00",
                   "note": "the post claimed a buyback that was never announced"}
    comparison.flag(db, run_id="unknown-run", source="reddit", note="about a run not in the window")
    found = comparison.summarise([record("a", "AAPL", "baseline"), record("b", "AAPL", "reddit")],
                                 flagged=comparison.flags(db))
    assert found["variants"]["reddit"]["flagged_wrong"] == {"reddit": 1}
    assert found["variants"]["baseline"]["flagged_wrong"] == {}


def test_records_are_read_from_a_runs_directory_and_a_broken_one_is_skipped(tmp_path):
    for name, body in (("r1", json.dumps(record("r1", "AAPL", "baseline", date="2026-09-01"))),
                       ("r2", json.dumps(record("r2", "AAPL", "sec_form4", date="2026-10-01"))),
                       ("r3", "{not json"), ("r4", "[]")):
        (tmp_path / name).mkdir()
        (tmp_path / name / "decision_record.json").write_text(body)
    (tmp_path / ".sources").mkdir()
    assert [r["run_id"] for r in comparison.load_records(tmp_path)] == ["r1", "r2"]
    assert [r["run_id"] for r in comparison.load_records(tmp_path, since="2026-09-15")] == ["r2"]


# ----------------------------------------------------------------------------- command line
def test_status_says_what_is_on_what_it_reaches_and_never_prints_the_contact(profiles, capsys, tmp_path):
    found = sources_cli.status(profiles, environ={})
    assert found["sources"]["reddit"]["on"] is False and found["sources"]["sec_form4"]["on"] is False
    assert found["sources"]["sec_form4"]["contact"]["set"] is False
    assert found["reaches"] == {"etf": {}, "stock": {}, "crypto": {}}

    configure(profiles, sec_form4={"enabled": True})
    secret = "Test Owner owner@example.com"
    found = sources_cli.status(profiles, environ={"CT_SEC_CONTACT": secret})
    assert found["sources"]["sec_form4"]["contact"] == {"set": True, "variable": "CT_SEC_CONTACT"}
    assert found["reaches"]["stock"] == {"sec_form4": "fundamentals analyst"}
    assert found["reaches"]["etf"] == {}
    sources_cli._print_status(found, "zh")
    sources_cli._print_status(found, "en")
    printed = capsys.readouterr().out
    assert secret not in printed and "owner@example.com" not in json.dumps(found)
    assert "research_web_access.sources.sec_form4.enabled" in printed


def test_the_command_compares_flags_and_reports_without_reaching_anything(
        config_dir, tmp_path, capsys, monkeypatch):
    # A deployment of its own: the command opens the state beside the config it is given.
    deployment = tmp_path / "deployment"
    shutil.copytree(config_dir, deployment / "config")
    runs = deployment / "runs"
    for name, body in (("r1", record("r1", "AAPL", "baseline")),
                       ("r2", record("r2", "AAPL", "sec_form4", confidence=58, readings=[
                           reading("sec_form4", sources.OK, 3)]))):
        (runs / name).mkdir(parents=True)
        (runs / name / "decision_record.json").write_text(json.dumps(body))
    monkeypatch.setenv("CT_DATA_DIR", str(deployment))
    monkeypatch.setenv("USER", "owner")
    base = ["--config-dir", str(deployment / "config")]
    # With no state at all there are simply no flags.
    assert sources_cli.main([*base, "--json", "compare"]) == 0
    found = json.loads(capsys.readouterr().out)
    assert found["runs"] == 2 and found["pairs"]["count"] == 1
    assert found["verdict"]["state"] == "waiting_for_samples"
    assert found["variants"]["sec_form4"]["flagged_wrong"] == {}

    assert sources_cli.main([*base, "flag", "r2", "--source", "sec_form4",
                             "--note", "amended a week later"]) == 0
    assert "已记录：r2 的 sec_form4" in capsys.readouterr().out
    assert sources_cli.main([*base, "--lang", "en", "compare"]) == 0
    printed = capsys.readouterr().out
    assert "[sec_form4]" in printed and "flagged as wrong: {'sec_form4': 1}" in printed
    assert "Waiting for samples" in printed and "No comparison of return or drawdown" in printed


def test_checking_a_deployment_with_both_sources_off_asks_nothing_and_says_so(profiles, tmp_path, monkeypatch):
    monkeypatch.setattr(sources, "cache_dir", lambda _profiles: tmp_path)
    found = sources_cli.check(profiles, "AAPL", environ={})
    assert {name: r["outcome"] for name, r in found["reads"].items()} == {
        "reddit": sources.DISABLED, "sec_form4": sources.DISABLED}
    assert all(r["requests"] == [] for r in found["reads"].values())
