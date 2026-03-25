"""Configuration edits from the console: section text, form values, classification, cooling.

Offline. Every test works on a copy of config/ under tmp_path; the deployment's files are
read, never written, and git runs only inside a repository created for the test.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import timedelta
from importlib import import_module

import pytest
import yaml

from agentic_trading import config_changes as cc
from agentic_trading import launchd, schedule, sources
from agentic_trading.proposals import utcnow
from agentic_trading.service import Service, ServiceConflict, ServiceError
from tests.conftest import ROOT, copy_example_ledger

RISK = "risk_profile.yaml"
INVESTOR = "investor_profile.yaml"
CALENDAR = "event_calendar.yaml"


@pytest.fixture
def sandbox(tmp_path, offline_market_reads):
    cfg = tmp_path / "config"
    shutil.copytree(ROOT / "config", cfg)
    state = tmp_path / "state"
    state.mkdir()
    copy_example_ledger("portfolio.example.yaml", state / "portfolio.yaml")
    copy_example_ledger("micro_live_portfolio.example.yaml", state / "micro_live_portfolio.yaml")
    (tmp_path / "runs").mkdir()
    return Service(cfg)


def read(service, name):
    return (service.config_dir / name).read_text(encoding="utf-8")


# ------------------------------------------------------------------ text-level editing
def test_sections_cover_every_top_level_key_and_round_trip(config_dir):
    for name in cc.FILES:
        text = (config_dir / name).read_text(encoding="utf-8")
        found = [s.key for s in cc.sections(text)]
        assert found == list(yaml.safe_load(text))
        rebuilt = text
        for item in cc.sections(text):
            rebuilt = cc.replace_section(rebuilt, item.key, item.text)
        assert rebuilt == text


def test_a_section_carries_the_comment_block_glued_above_it():
    text = "# file header\n\na: 1\n\n# floating paragraph\n\n# about b\nb:\n  c: 2\n\n\nd: 3\n"
    found = {s.key: s.text for s in cc.sections(text)}
    assert found["a"] == "a: 1"
    assert found["b"] == "# about b\nb:\n  c: 2"
    assert found["d"] == "d: 3"
    swapped = cc.replace_section(text, "b", "# about b, revised\nb:\n  c: 3")
    assert "# floating paragraph" in swapped and "revised" in swapped
    assert yaml.safe_load(swapped) == {"a": 1, "b": {"c": 3}, "d": 3}


def test_a_section_edit_may_not_rename_or_add_keys():
    text = "a: 1\nb: 2\n"
    with pytest.raises(cc.ConfigChangeError, match="exactly the top-level key"):
        cc.replace_section(text, "a", "a: 1\nz: 9")
    with pytest.raises(cc.ConfigChangeError, match="exactly the top-level key"):
        cc.replace_section(text, "a", "c: 1")
    with pytest.raises(cc.ConfigChangeError, match="not valid YAML"):
        cc.replace_section(text, "a", "a: [1,")


def test_set_values_changes_only_the_lines_it_touches(config_dir):
    text = (config_dir / RISK).read_text(encoding="utf-8")
    # The hour is the owner's and moves (Settings changes it in two taps), so it is read, not assumed.
    hour = cc.get_value(yaml.safe_load(text), "research_schedule.launchd.hour")
    new = cc.set_values(text, {"research_schedule.launchd.hour": (hour + 1) % 24,
                               "micro_live_execution.max_capital_usd": 2500})
    diff = cc.unified_diff(text, new, RISK)
    removed = [line for line in diff.splitlines() if line.startswith("-") and not line.startswith("---")]
    assert len(removed) == 2
    assert any(f"hour: {hour}" in line for line in removed)
    assert any("max_capital_usd: 2000" in line for line in removed)
    assert "# mirrored into the launchd job" in new  # comments survive
    assert cc.get_value(yaml.safe_load(new), "research_schedule.launchd.hour") == (hour + 1) % 24


def test_set_values_fills_a_recommended_default_node_in_place(config_dir):
    text = (config_dir / INVESTOR).read_text(encoding="utf-8")
    new = cc.set_values(text, {"universe.core.individual_stocks.watchlist": ["AAPL", "MSFT"]})
    node = yaml.safe_load(new)["universe"]["core"]["individual_stocks"]["watchlist"]
    assert node["value"] == ["AAPL", "MSFT"]
    assert node["recommended_default"] == [] and "reasoning" in node
    with pytest.raises(cc.ConfigChangeError, match="no such setting"):
        cc.set_values(text, {"universe.core.nothing_here": 1})


# ------------------------------------------------------------------------ classification
@pytest.mark.parametrize("updates, expected", [
    ({"micro_live_execution.max_capital_usd": 2500}, cc.COOLING),
    ({"micro_live_execution.max_capital_usd": 1500}, cc.IMMEDIATE),
    ({"micro_live_execution.enabled": False}, cc.IMMEDIATE),
    ({"execution_policy.stage": 4}, cc.COOLING),
    ({"buckets.micro_live.position_limits.max_open_positions": 2}, cc.IMMEDIATE),
    ({"buckets.micro_live.position_limits.max_open_positions": 4}, cc.COOLING),
    ({"confidence_requirements.minimum_to_open.micro_live_individual_stock": 90}, cc.IMMEDIATE),
    ({"confidence_requirements.minimum_to_open.micro_live_individual_stock": 70}, cc.COOLING),
    ({"buckets.micro_live.tiers.high.classes": []}, cc.IMMEDIATE),
    ({"buckets.micro_live.tiers.low.classes": ["broad_diversified_etf", "bond_or_cash_like_etf", "individual_stock"]}, cc.COOLING),
    ({"research_schedule.launchd.hour": 15, "research_schedule.tickers": ["VTI"]}, cc.IMMEDIATE),
    ({"notifications.min_interval_seconds": 60}, cc.IMMEDIATE),
    ({"micro_live_execution.order_rules.order_type": "limit"}, cc.COOLING),  # no rule: fails closed
])
def test_classification_by_direction(config_dir, updates, expected):
    text = (config_dir / RISK).read_text(encoding="utf-8")
    verdict = cc.classify(RISK, text, cc.set_values(text, updates))
    assert verdict["classification"] == expected, verdict


def test_classification_reads_effective_values_and_names_every_reason(config_dir):
    text = (config_dir / RISK).read_text(encoding="utf-8")
    # Replacing TODO with the recommended default itself changes nothing that counts.
    same = cc.set_values(text, {"buckets.micro_live.position_limits.max_open_positions": 3})
    assert cc.classify(RISK, text, same)["changes"] == []
    both = cc.set_values(text, {"micro_live_execution.max_capital_usd": 3000,
                                "micro_live_execution.capital_tolerance_usd": 10})
    verdict = cc.classify(RISK, text, both)
    assert verdict["reasons"] == ["micro_live_execution.max_capital_usd"]
    effects = {c["path"]: c["effect"] for c in verdict["changes"]}
    assert effects["micro_live_execution.capital_tolerance_usd"] == cc.TIGHTENING


def test_investor_and_calendar_guards(config_dir):
    text = (config_dir / INVESTOR).read_text(encoding="utf-8")
    watch = cc.set_values(text, {"universe.core.individual_stocks.watchlist": ["NVDA"]})
    assert cc.classify(INVESTOR, text, watch)["classification"] == cc.IMMEDIATE
    excluded = cc.set_values(text, {"universe.core.always_excluded": ["leveraged_etf"]})
    assert cc.classify(INVESTOR, text, excluded)["classification"] == cc.COOLING
    cal = (config_dir / CALENDAR).read_text(encoding="utf-8")
    moved = cc.set_values(cal, {"maintained_through": "2027-03-01"})
    assert cc.classify(CALENDAR, cal, moved)["classification"] == cc.IMMEDIATE


# ----------------------------------------------------------------- the autonomy switches
# The deployment's own switches are not read here: whether this machine is armed today is an
# operational fact the suite must not depend on either way.
SWITCHES = """\
buckets:
  micro_live:
    autonomy:
      open:
        enabled: false                     # ARM LAST, and separately from reduce
        max_pct_of_account: 20
        classes: [broad_diversified_etf, bond_or_cash_like_etf]
        max_new_positions_per_day: 1
      reduce:
        enabled: false                     # ARM SEPARATELY, and only once positions exist
        min_confidence: 80
        max_positions_reduced_per_day: 1
    require_stock_eligibility_screen: true
"""


def test_a_switch_turned_on_is_confirmed_rather_than_cooled_and_sets_its_number():
    spec, new, verdict = cc.switch_change(SWITCHES, "reduce", True, 50)
    assert verdict["classification"] == cc.CONFIRMED
    assert set(verdict["reasons"]) == {"buckets.micro_live.autonomy.reduce.enabled",
                                       "buckets.micro_live.autonomy.reduce.min_remaining_pct_of_opened"}
    assert spec.file == RISK and spec.set == {"buckets.micro_live.autonomy.reduce.enabled": True,
                                              "buckets.micro_live.autonomy.reduce.min_remaining_pct_of_opened": 50}
    lines = [line for line in cc.unified_diff(SWITCHES, new, RISK).splitlines()
             if line[:1] in "+-" and line[:3] not in ("+++", "---")]
    assert lines == [
        "-        enabled: false                     # ARM SEPARATELY, and only once positions exist",
        "+        enabled: true                      # ARM SEPARATELY, and only once positions exist",
        "+        min_remaining_pct_of_opened: 50",
    ]
    _, opened, verdict = cc.switch_change(new, "open", True, 85)
    assert verdict["classification"] == cc.CONFIRMED
    assert yaml.safe_load(opened)["buckets"]["micro_live"]["autonomy"]["open"]["min_confidence"] == 85
    # Off is a tightening: at once, for anyone signed in, with no password.
    _, off, verdict = cc.switch_change(opened, "reduce", False)
    assert verdict["classification"] == cc.IMMEDIATE
    reduce = yaml.safe_load(off)["buckets"]["micro_live"]["autonomy"]["reduce"]
    assert reduce["enabled"] is False and reduce["min_remaining_pct_of_opened"] == 50
    # On again keeps the number it has; raising it tightens and needs no password either.
    assert cc.switch_change(off, "reduce", True)[2]["classification"] == cc.CONFIRMED
    assert cc.switch_change(new, "reduce", True, 60)[2]["classification"] == cc.IMMEDIATE


@pytest.mark.parametrize("block, on, value, match", [
    ("sell", True, 50, "no autonomy switch named"),
    ("reduce", True, 0, "between 1 and 99"),
    ("reduce", True, 100, "between 1 and 99"),
    ("reduce", True, True, "whole number"),
    ("open", True, 79, "between 80 and 100"),
    ("open", True, 101, "between 80 and 100"),
    ("reduce", False, 50, "only when a switch is turned on"),
    ("reduce", True, None, "does not declare min_remaining_pct_of_opened"),
    ("open", True, None, "does not declare min_confidence"),
])
def test_a_switch_refuses_what_it_could_not_honour(block, on, value, match):
    with pytest.raises(cc.ConfigChangeError, match=match):
        cc.switch_change(SWITCHES, block, on, value)


def test_the_same_keys_edited_by_hand_still_cool_and_may_not_invent_a_key():
    by_hand = cc.set_values(SWITCHES, {"buckets.micro_live.autonomy.open.enabled": True})
    assert cc.classify(RISK, SWITCHES, by_hand)["classification"] == cc.COOLING
    lowered = cc.set_values(SWITCHES, {"buckets.micro_live.autonomy.reduce.min_confidence": 70})
    assert cc.classify(RISK, SWITCHES, lowered)["classification"] == cc.COOLING
    with pytest.raises(cc.ConfigChangeError, match="no such setting"):
        cc.set_values(SWITCHES, {"buckets.micro_live.autonomy.reduce.min_remaining_pct_of_opened": 50})
    # A lower share kept is a looser floor; a higher one a tighter floor.
    kept = cc.switch_change(SWITCHES, "reduce", True, 50)[1]
    for share, expected in ((40, cc.COOLING), (60, cc.IMMEDIATE)):
        moved = cc.set_values(kept, {"buckets.micro_live.autonomy.reduce.min_remaining_pct_of_opened": share})
        assert cc.classify(RISK, kept, moved)["classification"] == expected


# ----------------------------------------------------------------- the research switches
# As with the autonomy switches, the deployment's own file is not what these read: whether this
# machine studies candidates or reads the SEC today is the owner's setting, and it moves.
RESEARCH = """\
research_schedule:
  enabled: true
  period: weekly
  max_tickers_per_run: 3
  accounts:
    micro_live:
      holdings:   {period: weekly, max_tickers_per_run: 2}
      candidates: {period: weekly, max_tickers_per_run: 3}
research_web_access:
  # What research may read besides prices. Off unless true here.
  sources:
    sec_form4: {enabled: true}
  fetch:
    enabled: true
"""


def _changed(old, new):
    return [line for line in cc.unified_diff(old, new, RISK).splitlines()
            if line[:1] in "+-" and line[:3] not in ("+++", "---")]


def test_a_research_switch_is_read_the_way_the_code_that_acts_on_it_reads_it():
    doc = yaml.safe_load(RESEARCH)
    # A study's block that says nothing about enabled runs; a source nobody mentioned is off.
    assert cc.research_switch_on(doc, "candidates") is True
    assert cc.research_switch_on(doc, "sec_form4") is True
    assert cc.research_switch_on(doc, "reddit") is False
    assert [task.kind for task in schedule.tasks(doc["research_schedule"], bucket="micro_live")] == [
        schedule.HOLDINGS, schedule.CANDIDATES]
    # The names here are the names the sources package reads its settings under.
    assert cc.RESEARCH_SWITCHES["reddit"] == sources.switch(sources.REDDIT)
    assert cc.RESEARCH_SWITCHES["sec_form4"] == sources.switch(sources.FORM4)
    assert cc.RESEARCH_SWITCHES["candidates"] == f"research_schedule.accounts.micro_live.{schedule.CANDIDATES}.enabled"


def test_a_research_switch_writes_one_line_and_creates_the_key_it_needs():
    off = cc.research_switch(RESEARCH, "candidates", False)
    assert off.file == RISK and off.kind == "values"
    assert off.set == {cc.RESEARCH_SWITCHES["candidates"]: False} and off.create == frozenset(off.set)
    studied = cc.build(RESEARCH, off)
    assert _changed(RESEARCH, studied) == [
        "-      candidates: {period: weekly, max_tickers_per_run: 3}",
        "+      candidates: {period: weekly, max_tickers_per_run: 3, enabled: false}",
    ]
    assert cc.research_switch_on(yaml.safe_load(studied), "candidates") is False
    assert schedule.CANDIDATES not in [t.kind for t in schedule.tasks(yaml.safe_load(studied)["research_schedule"])]
    back = cc.build(studied, cc.research_switch(studied, "candidates", True))
    assert cc.research_switch_on(yaml.safe_load(back), "candidates") is True

    # A source the file never mentioned: on has to write the block, beside the one that is there.
    read = cc.build(RESEARCH, cc.research_switch(RESEARCH, "reddit", True))
    assert _changed(RESEARCH, read) == ["+    reddit:", "+      enabled: true"]
    assert "# What research may read besides prices. Off unless true here." in read
    assert yaml.safe_load(read)["research_web_access"]["sources"] == {
        "sec_form4": {"enabled": True}, "reddit": {"enabled": True}}
    silent = cc.build(read, cc.research_switch(read, "reddit", False))
    assert _changed(read, silent) == ["-      enabled: true", "+      enabled: false"]

    # A file that has never had the sources block gets it, inside the section it belongs to.
    unnamed = RESEARCH.replace("  # What research may read besides prices. Off unless true here.\n"
                               "  sources:\n    sec_form4: {enabled: true}\n", "")
    assert "sources" not in yaml.safe_load(unnamed)["research_web_access"]
    first = cc.build(unnamed, cc.research_switch(unnamed, "sec_form4", True))
    assert yaml.safe_load(first)["research_web_access"] == {
        "fetch": {"enabled": True}, "sources": {"sec_form4": {"enabled": True}}}
    assert list(yaml.safe_load(first)) == list(yaml.safe_load(unnamed))      # no new section

    # None of it is guarded: what research looks at applies at once, in either direction.
    for new in (studied, back, read, silent):
        assert cc.classify(RISK, RESEARCH, new)["classification"] == cc.IMMEDIATE
    assert cc.classify(RISK, unnamed, first)["classification"] == cc.IMMEDIATE


def test_a_candidate_study_is_created_only_for_an_account_that_is_already_scheduled():
    bare = RESEARCH.replace("      candidates: {period: weekly, max_tickers_per_run: 3}\n", "")
    assert cc.research_switch_on(yaml.safe_load(bare), "candidates") is False
    added = cc.build(bare, cc.research_switch(bare, "candidates", True))
    assert _changed(bare, added) == ["+      candidates:", "+        enabled: true"]
    kinds = [task.kind for task in schedule.tasks(yaml.safe_load(added)["research_schedule"])]
    assert kinds == [schedule.HOLDINGS, schedule.CANDIDATES]
    # Naming the first account would replace the single review the deployment runs.
    single = "research_schedule:\n  enabled: true\n  period: weekly\n"
    for unscheduled in (single, single + "  accounts: {}\n", single + "  accounts:\n    advisory:\n      holdings: {period: weekly}\n"):
        with pytest.raises(cc.ConfigChangeError, match="not scheduled by account"):
            cc.research_switch(unscheduled, "candidates", True)


@pytest.mark.parametrize("name, on, match", [
    ("stocktwits", True, "no research switch named"),
    ("sec_form4", True, "already on"),
    ("candidates", True, "already on"),
    ("reddit", False, "already off"),
    ("reddit", "yes", "turned on or off"),
    ("reddit", 1, "turned on or off"),
])
def test_a_research_switch_refuses_what_changes_nothing_or_names_nothing(name, on, match):
    with pytest.raises(cc.ConfigChangeError, match=match):
        cc.research_switch(RESEARCH, name, on)


def test_what_a_research_switch_may_create_is_a_closed_list_of_its_own():
    assert cc.RESEARCH_CREATES == frozenset(cc.RESEARCH_SWITCHES.values())
    # Beside the plan's list, never inside it: that one stays exactly what a plan names.
    assert cc.RESEARCH_CREATES.isdisjoint(cc.CREATABLE)
    reddit = cc.RESEARCH_SWITCHES["reddit"]
    # The same key asked for as plain values, the shape a request arrives in, creates nothing.
    with pytest.raises(cc.ConfigChangeError, match="no such setting"):
        cc.build(RESEARCH, cc.ChangeSpec(file=RISK, set={reddit: True}))
    # And nothing outside the two lists may be created by anyone.
    stray = "research_web_access.sources.stocktwits.enabled"
    with pytest.raises(cc.ConfigChangeError, match="may not be created from here"):
        cc.build(RESEARCH, cc.ChangeSpec(file=RISK, set={stray: True}, create=frozenset({stray})))
    with pytest.raises(cc.ConfigChangeError, match="only ever created in risk_profile.yaml"):
        cc.ChangeSpec(file=INVESTOR, set={reddit: True}, create=frozenset({reddit})).check()
    # A stored record puts its create back and is held to the same lists when it is rebuilt.
    spec = cc.research_switch(RESEARCH, "reddit", True)
    record = cc.new_record(spec, base=cc.base_of(RESEARCH, spec), identity="owner", reason="",
                           verdict=cc.classify(RISK, RESEARCH, cc.build(RESEARCH, spec)))
    assert record["status"] == cc.APPLIED and record["create"] == [reddit]
    assert cc.build(RESEARCH, cc.spec_of(record)) == cc.build(RESEARCH, spec)


# --------------------------------------------------------------------------- validation
def test_validation_uses_the_real_loader(sandbox):
    text = read(sandbox, RISK)
    bad = cc.set_values(text, {"execution_policy.stage": 9})
    with pytest.raises(cc.ConfigChangeError, match="stage must be 1..6"):
        cc.validate(sandbox.config_dir, RISK, bad)
    with pytest.raises(cc.ConfigChangeError, match="may not be added or removed"):
        cc.validate(sandbox.config_dir, RISK, text + "\nextra_key: 1\n")
    cal = read(sandbox, CALENDAR)
    # Whatever date the owner has renewed it to.
    broken, found = re.subn(r"(?m)^maintained_through: .*$", "maintained_through: soon", cal)
    assert found == 1
    with pytest.raises(cc.ConfigChangeError, match="maintained_through"):
        cc.validate(sandbox.config_dir, CALENDAR, broken)
    assert read(sandbox, RISK) == text  # validation never writes


# ------------------------------------------------------------------------------ service
def test_immediate_change_is_written_recorded_and_committed(sandbox, monkeypatch):
    subprocess.run(["git", "init", "-q", str(sandbox.config_dir.parent)], check=True)
    subprocess.run(["git", "-C", str(sandbox.config_dir.parent), "-c", "user.name=t",
                    "-c", "user.email=t@t", "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(sandbox.config_dir.parent), "-c", "user.name=t",
                    "-c", "user.email=t@t", "commit", "-q", "-m", "base"], check=True)
    overview = sandbox.config_overview()
    revision = next(f["revision"] for f in overview["files"] if f["file"] == INVESTOR)
    preview = sandbox.preview_config_change(
        file=INVESTOR, revision=revision, set={"universe.core.individual_stocks.watchlist": ["AAPL"]})
    assert preview["classification"] == cc.IMMEDIATE and "+          - AAPL" in preview["diff"]
    result = sandbox.submit_config_change(
        file=INVESTOR, revision=revision, set={"universe.core.individual_stocks.watchlist": ["AAPL"]},
        identity="owner", reason="first candidate")
    assert result["status"] == cc.APPLIED
    assert result["outcome"]["git"].startswith("committed")
    assert "launchd" not in result["outcome"]
    assert sandbox.profiles().investor_cfg(
        "universe", "core", "individual_stocks", "watchlist") == ["AAPL"]
    log = subprocess.run(["git", "-C", str(sandbox.config_dir.parent), "log", "-1", "--format=%an %s"],
                         check=True, capture_output=True, text=True).stdout
    assert log.strip() == "owner via console config: investor_profile.yaml universe.core.individual_stocks.watchlist (console, owner)"
    kinds = [e["kind"] for e in sandbox.activity(kinds=("config_changed",))["events"]]
    assert kinds == ["config_changed"]
    assert sandbox.config_overview()["recent"][0]["id"] == result["id"]
    # A stale revision is a conflict, not a silent overwrite.
    with pytest.raises(ServiceConflict):
        sandbox.submit_config_change(file=INVESTOR, revision=revision,
                                     set={"universe.core.individual_stocks.watchlist": []})


def test_loosening_change_needs_confirmation_then_waits(sandbox):
    text = read(sandbox, RISK)
    with pytest.raises(ServiceError, match="console password"):
        sandbox.submit_config_change(file=RISK, set={"micro_live_execution.max_capital_usd": 2500},
                                     identity="owner")
    assert read(sandbox, RISK) == text
    result = sandbox.submit_config_change(file=RISK, set={"micro_live_execution.max_capital_usd": 2500},
                                          identity="owner", confirmed=True)
    assert result["status"] == cc.PENDING and result["apply_after"]
    assert read(sandbox, RISK) == text                      # nothing written yet
    assert sandbox.apply_config_changes() == []             # not due
    pending = sandbox.config_overview()["pending"]
    assert [p["id"] for p in pending] == [result["id"]]
    titles = [e.title for e in sandbox.collect_notifications() if e.kind == "config_change_pending"]
    assert titles and "risk_profile.yaml" in titles[0]
    # Time passes.
    db = sandbox.state_db()
    items = cc.records(db)
    items[0]["apply_after"] = (utcnow() - timedelta(minutes=1)).isoformat()
    cc.save(db, items)
    applied = sandbox.apply_config_changes()
    assert [a["status"] for a in applied] == [cc.APPLIED]
    assert sandbox.profiles().risk_cfg("micro_live_execution", "max_capital_usd") == 2500
    assert sandbox.config_overview()["pending"] == []
    kinds = [e.kind for e in sandbox.collect_notifications()]
    assert "config_change_applied" in kinds and "config_change_pending" not in kinds


def test_pending_change_can_be_withdrawn_and_refuses_moved_ground(sandbox):
    first = sandbox.submit_config_change(file=RISK, set={"micro_live_execution.max_capital_usd": 2500},
                                         identity="owner", confirmed=True)
    cancelled = sandbox.cancel_config_change(first["id"], identity="owner")
    assert cancelled["status"] == cc.CANCELLED
    with pytest.raises(ServiceConflict):
        sandbox.cancel_config_change(first["id"])
    with pytest.raises(ServiceError, match="no such change"):
        sandbox.cancel_config_change("nope")

    second = sandbox.submit_config_change(file=RISK, set={"micro_live_execution.max_capital_usd": 2500},
                                          identity="owner", confirmed=True)
    # A tightening edit to the same setting lands at once; the parked one now stands on moved
    # ground and fails rather than re-raising the ceiling from a stale premise.
    sandbox.submit_config_change(file=RISK, set={"micro_live_execution.max_capital_usd": 1000},
                                 identity="owner")
    db = sandbox.state_db()
    items = cc.records(db)
    for item in items:
        if item["id"] == second["id"]:
            item["apply_after"] = (utcnow() - timedelta(minutes=1)).isoformat()
    cc.save(db, items)
    applied = sandbox.apply_config_changes()
    assert applied[0]["status"] == cc.FAILED and "cooling period" in applied[0]["error"]
    assert sandbox.profiles().risk_cfg("micro_live_execution", "max_capital_usd") == 1000


def test_section_edit_from_the_console(sandbox):
    section = sandbox.config_section(RISK, "research_schedule")
    assert "\nresearch_schedule:\n" in section["text"] and section["guarded"] is False
    # The first occurrence is the section's own cap; a per-account block may repeat the phrase.
    edited = section["text"].replace("max_tickers_per_run: 3", "max_tickers_per_run: 2", 1)
    result = sandbox.submit_config_change(file=RISK, revision=section["revision"],
                                          key="research_schedule", text=edited, identity="owner")
    assert result["status"] == cc.APPLIED
    assert result["paths"] == ["research_schedule.max_tickers_per_run"]
    assert "launchd" in result["outcome"]  # a schedule edit asks launchd, whatever the answer
    assert sandbox.schedule_status()["tickers"].__len__() <= 2
    with pytest.raises(ServiceError, match="nothing changed"):
        sandbox.submit_config_change(file=RISK, key="research_schedule", text=edited, identity="owner")


def test_cashflow_and_autonomy_controls(sandbox):
    before = sandbox.portfolio()
    result = sandbox.record_cashflow(bucket="micro_live", amount_usd=50, reason="top up",
                                     identity="owner")
    assert result["bucket"] == "micro_live" and result["net_deposits"]["micro_live"] == 50
    with pytest.raises(ServiceError):
        sandbox.record_cashflow(bucket="micro_live", amount_usd=10_000, kind="withdrawal")
    assert sandbox.portfolio() != before
    stopped = sandbox.autonomy_stop("looked wrong", identity="owner")
    assert stopped["withdrawn"] is True and "looked wrong" in stopped["reason"]
    assert sandbox.config_overview()["autonomy"]["withdrawn"] is True
    with pytest.raises(ServiceError):
        sandbox.autonomy_resume("   ")
    resumed = sandbox.autonomy_resume("checked the ledger", identity="owner")
    assert resumed["withdrawn"] is False
    kinds = {e["kind"] for e in sandbox.activity()["events"]}
    assert {"cashflow_recorded", "autonomy_stopped", "autonomy_resumed"} <= kinds


def _switched(service, block):
    return service.profiles().risk_cfg("buckets", "micro_live", "autonomy", block, "enabled") is True


def test_a_switch_turned_on_applies_at_once_on_the_password_and_says_so(sandbox):
    # From off, whatever this deployment's own file says today.
    sandbox.autonomy_switch("reduce", False, identity="owner")
    text = read(sandbox, RISK)
    with pytest.raises(ServiceError, match="console password"):
        sandbox.autonomy_switch("reduce", True, value=50, identity="owner")
    assert read(sandbox, RISK) == text and not _switched(sandbox, "reduce")

    result = sandbox.autonomy_switch("reduce", True, value=50, identity="owner", confirmed=True)
    change = result["change"]
    assert result["changed"] is True and change["status"] == cc.APPLIED
    assert change["classification"] == cc.CONFIRMED and change["apply_after"] is None
    assert change["switch"] == {"block": "reduce", "on": True}
    assert _switched(sandbox, "reduce")
    assert sandbox.profiles().risk_cfg("buckets", "micro_live", "autonomy", "reduce",
                                       "min_remaining_pct_of_opened") == 50
    authority = sandbox.effective_authority()
    assert authority["switches"]["reduce"] is True
    assert "not_enabled" not in {b["code"] for b in authority["actions"]["REDUCE"]["autonomous"]["blocked_by"]}
    assert sandbox.config_overview()["pending"] == []
    assert sandbox.config_overview()["recent"][0]["id"] == change["id"]
    # Pushed under a kind the deployment already sends (notifications.events), urgently.
    notices = [n for n in sandbox.collect_notifications() if n.key.startswith("autonomy_switched_on:")]
    assert len(notices) == 1 and notices[0].urgent and notices[0].kind == "config_change_applied"
    assert notices[0].title == "autonomous selling was switched on"
    assert notices[0].link == "/ui/index.html?scope=agentic"
    events = sandbox.activity(kinds=("config_changed",))["events"]
    assert any((e.get("detail") or {}).get("switch") == {"block": "reduce", "on": True} for e in events)

    # Asked again: already so, nothing written or recorded.
    again = sandbox.autonomy_switch("reduce", True, identity="owner", confirmed=True)
    assert again == {"block": "reduce", "on": True, "changed": False, "change": None}
    # Off needs no password, applies at once and raises no notice of its own.
    off = sandbox.autonomy_switch("reduce", False, identity="owner")
    assert off["change"]["classification"] == cc.IMMEDIATE and not _switched(sandbox, "reduce")
    assert len([n for n in sandbox.collect_notifications()
                if n.key.startswith("autonomy_switched_on:")]) == 1


def test_a_switch_waits_for_a_change_to_it_still_cooling_but_stopping_never_does(sandbox):
    sandbox.autonomy_switch("open", False, identity="owner")
    parked = sandbox.submit_config_change(
        file=RISK, set={"buckets.micro_live.autonomy.open.max_pct_of_account": 30},
        identity="owner", confirmed=True)
    assert parked["status"] == cc.PENDING
    with pytest.raises(ServiceConflict, match="still cooling"):
        sandbox.autonomy_switch("open", True, value=80, identity="owner", confirmed=True)
    sandbox.cancel_config_change(parked["id"], identity="owner")
    opened = sandbox.autonomy_switch("open", True, value=80, identity="owner", confirmed=True)
    assert opened["change"]["classification"] == cc.CONFIRMED and _switched(sandbox, "open")
    with pytest.raises(ServiceError, match="between 80 and 100"):
        sandbox.autonomy_switch("open", True, value=70, identity="owner", confirmed=True)

    # Stopping is never held up by what is waiting.
    waiting = sandbox.submit_config_change(
        file=RISK, set={"buckets.micro_live.autonomy.open.max_pct_of_account": 30},
        identity="owner", confirmed=True)
    closed = sandbox.autonomy_switch("open", False, identity="owner")
    assert closed["changed"] is True and not _switched(sandbox, "open")
    sandbox.cancel_config_change(waiting["id"], identity="owner")


def test_a_switch_refuses_to_write_over_a_file_that_moved_under_it(sandbox, monkeypatch):
    sandbox.autonomy_switch("reduce", False, identity="owner")
    reads, real = [], sandbox._config_text

    def moving(profiles, file):
        text = real(profiles, file)
        reads.append(file)
        return text + "\n" if len(reads) > 1 else text      # edited elsewhere between the reads

    monkeypatch.setattr(sandbox, "_config_text", moving)
    before = read(sandbox, RISK)
    with pytest.raises(ServiceConflict, match="changed while the switch was being set"):
        sandbox.autonomy_switch("reduce", True, value=50, identity="owner", confirmed=True)
    assert read(sandbox, RISK) == before and cc.records(sandbox.state_db()) == []


# ---------------------------------------------------------------------------------- API
def test_api_routes_step_up_and_shape(auth_client, monkeypatch, sandbox):
    monkeypatch.setattr("agentic_trading.api.app._service", sandbox)
    overview = auth_client.get("/config").json()
    assert {f["file"] for f in overview["files"]} == set(cc.FILES)
    assert overview["values"]["research_schedule.launchd.hour"] == sandbox.profiles().risk_cfg(
        "research_schedule", "launchd", "hour")
    revision = next(f["revision"] for f in overview["files"] if f["file"] == RISK)

    body = {"file": RISK, "revision": revision, "set": {"micro_live_execution.max_capital_usd": 2500}}
    preview = auth_client.post("/config/preview", json=body)
    assert preview.status_code == 200 and preview.json()["classification"] == cc.COOLING

    assert auth_client.post("/config/changes", json=body).status_code == 400
    wrong = auth_client.post("/config/changes", json={**body, "password": "not-it"})
    assert wrong.status_code == 400 and "did not match" in wrong.json()["detail"]
    assert auth_client.post("/config/changes", json={**body, "pasword": "typo"}).status_code == 422
    parked = auth_client.post("/config/changes", json={**body, "password": "Test-Only-Console-Pw1"})
    assert parked.status_code == 200 and parked.json()["status"] == cc.PENDING
    assert parked.json()["requested_by"] == "test-owner"
    cancel = auth_client.post("/config/changes/" + parked.json()["id"] + "/cancel", json={})
    assert cancel.status_code == 200 and cancel.json()["status"] == cc.CANCELLED

    section = auth_client.get("/config/section", params={"file": RISK, "key": "research_schedule"})
    assert section.status_code == 200 and section.json()["key"] == "research_schedule"
    assert auth_client.get("/config/section", params={"file": "MANDATE.md", "key": "x"}).status_code == 400

    cash = auth_client.post("/portfolio/cashflow", json={"bucket": "core", "amount_usd": 100})
    assert cash.status_code == 200 and cash.json()["bucket"] == "core"
    assert auth_client.post("/portfolio/cashflow", json={"bucket": "core", "amount_usd": -1}).status_code == 422

    assert auth_client.post("/autonomy/stop", json={"reason": "test"}).status_code == 200
    assert auth_client.post("/autonomy/resume", json={"reason": "test"}).status_code == 422
    assert auth_client.post("/autonomy/resume", json={"reason": "test", "password": "x"}).status_code == 400
    assert auth_client.post("/autonomy/resume",
                            json={"reason": "test", "password": "Test-Only-Console-Pw1"}).status_code == 200


def test_wrong_step_up_passwords_share_the_login_limiter(auth_client, monkeypatch, sandbox):
    monkeypatch.setattr("agentic_trading.api.app._service", sandbox)
    body = {"file": RISK, "set": {"micro_live_execution.max_capital_usd": 2500}, "password": "no"}
    for _ in range(5):
        assert auth_client.post("/config/changes", json=body).status_code == 400
    assert auth_client.post("/config/changes", json=body).status_code == 429
    assert auth_client.post("/auth/login", json={"username": "test-owner",
                                                 "password": "Test-Only-Console-Pw1"}).status_code == 429


def test_password_change_rotates_and_signs_out(auth_client, monkeypatch, sandbox, console_auth):
    monkeypatch.setattr("agentic_trading.api.app._service", sandbox)
    assert auth_client.post("/auth/password", json={"current": "wrong", "new": "Another-Long-Passphrase-1"}).status_code == 400
    weak = auth_client.post("/auth/password", json={"current": "Test-Only-Console-Pw1", "new": "short"})
    assert weak.status_code == 400
    ok = auth_client.post("/auth/password", json={"current": "Test-Only-Console-Pw1",
                                                  "new": "Another-Long-Passphrase-1"})
    assert ok.status_code == 200 and ok.json()["rotated"] is True
    assert auth_client.get("/config").status_code == 401
    assert console_auth.verify_password("test-owner", "Another-Long-Passphrase-1") == "ok"
    assert console_auth.verify_password("test-owner", "Test-Only-Console-Pw1") == "invalid"


def test_the_switch_route_asks_the_password_to_turn_on_and_only_then(auth_client, monkeypatch, sandbox):
    monkeypatch.setattr("agentic_trading.api.app._service", sandbox)
    assert auth_client.post("/autonomy/switch", json={"block": "reduce", "on": False}).status_code == 200
    body = {"block": "reduce", "on": True, "value": 50}
    bare = auth_client.post("/autonomy/switch", json=body)
    assert bare.status_code == 400 and "console password" in bare.json()["detail"]
    wrong = auth_client.post("/autonomy/switch", json={**body, "password": "not-it"})
    assert wrong.status_code == 400 and "did not match" in wrong.json()["detail"]
    for refused in ({**body, "block": "sell"}, {**body, "value": 101}, {**body, "on": "maybe"},
                    {**body, "pasword": "typo"}):
        assert auth_client.post("/autonomy/switch", json=refused).status_code == 422, refused
    out_of_range = auth_client.post("/autonomy/switch", json={**body, "value": 0, "password": "Test-Only-Console-Pw1"})
    assert out_of_range.status_code == 400 and "between 1 and 99" in out_of_range.json()["detail"]
    on = auth_client.post("/autonomy/switch", json={**body, "password": "Test-Only-Console-Pw1"})
    assert on.status_code == 200 and on.json()["change"]["classification"] == cc.CONFIRMED
    assert on.json()["change"]["requested_by"] == "test-owner"
    assert auth_client.get("/config/authority").json()["switches"]["reduce"] is True
    off = auth_client.post("/autonomy/switch", json={"block": "reduce", "on": False})
    assert off.status_code == 200 and off.json()["change"]["classification"] == cc.IMMEDIATE
    assert auth_client.get("/config/authority").json()["switches"]["reduce"] is False


def test_config_writes_need_a_session_and_csrf(auth_client, monkeypatch):
    calls = []
    module = import_module("agentic_trading.api.app")
    monkeypatch.setattr(module, "_service", type("S", (), {
        "submit_config_change": lambda self, **k: calls.append("submit"),
        "autonomy_switch": lambda self, *a, **k: calls.append("switch"),
        "record_cashflow": lambda self, **k: calls.append("cash")})())
    for path in ("/config/changes", "/autonomy/switch", "/portfolio/cashflow"):
        assert auth_client.post(path, headers={"X-CSRF-Token": "wrong"}, json={}).status_code == 403
        assert auth_client.post(path, headers={"Origin": "https://attacker.example"}, json={}).status_code == 403
    assert calls == []


# ---------------------------------------------------------------- the research switches
CONTACT = "Test Owner owner@example.test"
SCHEDULED = """\
research_schedule:
  enabled: true
  period: weekly
  launchd: {weekday: 5, hour: 13, minute: 30}
  tickers: [VTI, VXUS, BND, VGIT]
  max_tickers_per_run: 4
  accounts:
    micro_live:
      holdings:   {period: weekly, max_tickers_per_run: 2}
      candidates: {period: weekly, max_tickers_per_run: 3}"""


@pytest.fixture
def scheduled(sandbox):
    """The sandbox with a research schedule of the suite's own; this deployment's is the owner's."""
    path = sandbox.config_dir / RISK
    path.write_text(cc.replace_section(path.read_text(encoding="utf-8"), "research_schedule", SCHEDULED),
                    encoding="utf-8")
    return sandbox


def _research(service, name):
    return service.config_overview()["research"]["switches"][name]


def _put(service, name, on):
    """One switch brought to a known state, whatever this deployment's own file says today."""
    if _research(service, name)["on"] is not on:
        service.submit_config_change(file=RISK, switch=name, on=on, identity="setup")


def test_research_switches_are_reported_by_the_code_that_acts_on_them(scheduled, monkeypatch):
    sandbox = scheduled
    monkeypatch.setenv("CT_SEC_CONTACT", CONTACT)
    overview = sandbox.config_overview()
    research = overview["research"]
    assert set(research["switches"]) == set(cc.RESEARCH_SWITCHES)
    assert research["fires_every"] == "weekly"
    for name, row in research["switches"].items():
        assert row["path"] == cc.RESEARCH_SWITCHES[name] and row["available"] is True
        assert row["on"] is cc.research_switch_on(yaml.safe_load(read(sandbox, RISK)), name)
    settings = sources.load(sandbox.profiles())
    assert research["switches"]["reddit"]["on"] is settings.reddit.enabled
    assert research["switches"]["sec_form4"]["on"] is settings.form4.enabled
    # What the study would look at: the universe, less what the account holds, cut to its cap.
    held = {p["symbol"] for p in yaml.safe_load(
        (sandbox.config_dir.parent / "state" / "micro_live_portfolio.yaml").read_text())["positions"]}
    study = research["switches"]["candidates"]
    assert study["on"] is True and (study["period"], study["max_tickers_per_run"]) == ("weekly", 3)
    assert study["tickers"] == [t for t in ("VTI", "VXUS", "BND", "VGIT") if t not in held][:3]
    # Who is asking the SEC is said as set or not set; the words themselves never leave .env.
    assert research["switches"]["sec_form4"]["contact"] == {"set": True, "variable": "CT_SEC_CONTACT"}
    assert "owner@example.test" not in json.dumps(overview)
    monkeypatch.delenv("CT_SEC_CONTACT")
    contact = _research(sandbox, "sec_form4")["contact"]
    assert contact["set"] is False and contact["variable"] == "CT_SEC_CONTACT"
    assert "CT_SEC_CONTACT is not set" in contact["why"]
    # The watchlist form is gone from the page, and nothing else ever read that value.
    assert "universe.core.individual_stocks.watchlist" not in overview["values"]

    # A deployment that schedules no account has no candidate study to switch, and is told so.
    path = sandbox.config_dir / RISK
    path.write_text(cc.replace_section(read(sandbox, RISK), "research_schedule",
                                       "research_schedule:\n  enabled: true\n  period: daily"), encoding="utf-8")
    research = sandbox.config_overview()["research"]
    assert research["fires_every"] == "daily"
    assert research["switches"]["candidates"]["available"] is False
    assert research["switches"]["candidates"]["on"] is False
    with pytest.raises(ServiceError, match="not scheduled by account"):
        sandbox.preview_config_change(file=RISK, switch="candidates", on=True)

    # A schedule the scheduler itself could not read must not take this page down with it: the
    # page is where that section is put right. The switch is withheld, and says why.
    path.write_text(cc.replace_section(read(sandbox, RISK), "research_schedule", SCHEDULED.replace(
        "candidates: {period: weekly, max_tickers_per_run: 3}",
        "candidates: {period: weekly, max_tickers_per_run: lots}")), encoding="utf-8")
    research = sandbox.config_overview()["research"]
    study = research["switches"]["candidates"]
    assert study["on"] is False and study["available"] is False
    assert study["why"].startswith("research_schedule could not be read")
    assert research["fires_every"] == "weekly" and set(research["switches"]) == set(cc.RESEARCH_SWITCHES)


def test_a_research_switch_goes_through_preview_and_submit_like_any_other_change(scheduled):
    sandbox = scheduled
    revision = next(f["revision"] for f in sandbox.config_overview()["files"] if f["file"] == RISK)
    preview = sandbox.preview_config_change(file=RISK, revision=revision, switch="candidates", on=False)
    assert preview["changed"] is True and preview["classification"] == cc.IMMEDIATE
    assert preview["reasons"] == [] and "enabled: false" in preview["diff"]
    assert [c["path"] for c in preview["changes"]] == [cc.RESEARCH_SWITCHES["candidates"]]
    assert _research(sandbox, "candidates")["on"] is True          # a preview writes nothing

    result = sandbox.submit_config_change(file=RISK, revision=revision, switch="candidates", on=False,
                                          identity="owner", reason="quiet week")
    assert result["status"] == cc.APPLIED and result["classification"] == cc.IMMEDIATE
    assert result["paths"] == [cc.RESEARCH_SWITCHES["candidates"]] and result["requested_by"] == "owner"
    assert "launchd" in result["outcome"]           # under research_schedule, so launchd is asked
    assert _research(sandbox, "candidates")["on"] is False
    cfg = schedule.settings(sandbox.profiles())
    assert schedule.CANDIDATES not in [task.kind for task in schedule.tasks(cfg, bucket="micro_live")]
    assert sandbox.config_overview()["recent"][0]["id"] == result["id"]
    assert sandbox.activity(kinds=("config_changed",))["events"][0]["subject"] == result["id"]
    # The file moved, so the revision the page loaded is stale: a conflict, never an overwrite.
    with pytest.raises(ServiceConflict):
        sandbox.submit_config_change(file=RISK, revision=revision, switch="candidates", on=True)
    with pytest.raises(ServiceError, match="already off"):
        sandbox.preview_config_change(file=RISK, switch="candidates", on=False)
    sandbox.submit_config_change(file=RISK, switch="candidates", on=True, identity="owner")
    assert _research(sandbox, "candidates")["on"] is True


def test_a_source_is_switched_on_only_once_it_could_be_read(sandbox, monkeypatch):
    monkeypatch.setenv("CT_SEC_CONTACT", CONTACT)
    _put(sandbox, "sec_form4", False)
    _put(sandbox, "reddit", False)
    text, recorded = read(sandbox, RISK), len(cc.records(sandbox.state_db()))
    # Without a contact every read would end "not configured": on would be a word, not a fact.
    monkeypatch.delenv("CT_SEC_CONTACT")
    for ask in (sandbox.preview_config_change, sandbox.submit_config_change):
        with pytest.raises(ServiceError, match="CT_SEC_CONTACT is not set"):
            ask(file=RISK, switch="sec_form4", on=True)
    monkeypatch.setenv("CT_SEC_CONTACT", "nobody")
    with pytest.raises(ServiceError, match="a name and an e-mail address"):
        sandbox.submit_config_change(file=RISK, switch="sec_form4", on=True)
    assert read(sandbox, RISK) == text and len(cc.records(sandbox.state_db())) == recorded
    monkeypatch.setenv("CT_SEC_CONTACT", CONTACT)
    assert sandbox.submit_config_change(file=RISK, switch="sec_form4", on=True)["status"] == cc.APPLIED
    assert sources.load(sandbox.profiles()).form4.enabled is True
    # Off asks nothing of anyone.
    monkeypatch.delenv("CT_SEC_CONTACT")
    assert sandbox.submit_config_change(file=RISK, switch="sec_form4", on=False)["status"] == cc.APPLIED
    assert sources.load(sandbox.profiles()).form4.enabled is False

    # Reddit needs no contact, and its first on writes a block the file never had.
    on = sandbox.submit_config_change(file=RISK, switch="reddit", on=True, identity="owner")
    # A block that was not there is reported as the block; one that was, as its own line.
    assert on["status"] == cc.APPLIED and len(on["paths"]) == 1
    assert cc.RESEARCH_SWITCHES["reddit"].startswith(on["paths"][0])
    assert "launchd" not in on["outcome"]
    assert sources.load(sandbox.profiles()).reddit.enabled is True and _research(sandbox, "reddit")["on"] is True


@pytest.mark.parametrize("ask, match", [
    ({"switch": "reddit", "on": True, "set": {"research_schedule.launchd.hour": 14}}, "not both"),
    ({"switch": "reddit", "on": True, "key": "research_web_access", "text": "research_web_access: {}"}, "not both"),
    ({"switch": "reddit"}, "say which"),
    ({"on": True, "set": {"research_schedule.launchd.hour": 14}}, "name the switch"),
    ({"switch": "stocktwits", "on": True}, "no research switch named"),
])
def test_a_switch_is_asked_for_by_name_and_as_nothing_else(scheduled, ask, match):
    sandbox = scheduled
    _put(sandbox, "reddit", False)
    text, recorded = read(sandbox, RISK), len(cc.records(sandbox.state_db()))
    for call in (sandbox.preview_config_change, sandbox.submit_config_change):
        with pytest.raises(ServiceError, match=match):
            call(file=RISK, **ask)
    with pytest.raises(ServiceError, match="is a setting in risk_profile.yaml"):
        sandbox.preview_config_change(file=INVESTOR, switch="candidates", on=False)
    assert read(sandbox, RISK) == text and len(cc.records(sandbox.state_db())) == recorded


def test_the_api_takes_a_research_switch_by_name(auth_client, monkeypatch, sandbox):
    monkeypatch.setattr("agentic_trading.api.app._service", sandbox)
    _put(sandbox, "reddit", False)
    overview = auth_client.get("/config").json()
    assert overview["research"]["switches"]["reddit"] == {
        "on": False, "path": cc.RESEARCH_SWITCHES["reddit"], "available": True}
    revision = next(f["revision"] for f in overview["files"] if f["file"] == RISK)
    body = {"file": RISK, "revision": revision, "switch": "reddit", "on": True}
    preview = auth_client.post("/config/preview", json=body)
    assert preview.status_code == 200 and preview.json()["classification"] == cc.IMMEDIATE
    assert preview.json()["changed"] is True and "reddit" in preview.json()["diff"]
    for refused in ({**body, "switch": "stocktwits"}, {**body, "on": "yes"}, {**body, "on": 1},
                    {**body, "create": [cc.RESEARCH_SWITCHES["reddit"]]}):
        assert auth_client.post("/config/preview", json=refused).status_code == 422, refused
        assert auth_client.post("/config/changes", json=refused).status_code == 422, refused
    mixed = auth_client.post("/config/changes", json={**body, "set": {"research_schedule.launchd.hour": 14}})
    assert mixed.status_code == 400 and "not both" in mixed.json()["detail"]
    # A key as plain values, without a name: the request route still creates nothing. (A source
    # nobody has named, because the owner may well have switched Reddit once and declared its key.)
    by_path = auth_client.post("/config/changes", json={
        "file": RISK, "set": {"research_web_access.sources.stocktwits.enabled": True}})
    assert by_path.status_code == 400 and "no such setting" in by_path.json()["detail"]
    applied = auth_client.post("/config/changes", json=body)
    assert applied.status_code == 200 and applied.json()["status"] == cc.APPLIED
    assert applied.json()["requested_by"] == "test-owner"
    assert auth_client.get("/config").json()["research"]["switches"]["reddit"]["on"] is True
    again = auth_client.post("/config/preview", json={**body, "revision": ""})
    assert again.status_code == 400 and "already on" in again.json()["detail"]


# ------------------------------------------------------------------------------ launchd
def test_reregister_research_is_a_no_op_outside_a_managed_install(tmp_path, monkeypatch):
    monkeypatch.delenv("CT_RELEASE_DIR", raising=False)
    summary = launchd.reregister_research(data_dir=tmp_path, launch_agents=tmp_path / "agents")
    assert summary.startswith("launchd not managed here")


def test_reregister_research_rewrites_and_bootstraps_only_when_the_plist_changed(tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    shutil.copytree(ROOT / "config", cfg)
    agents = tmp_path / "agents"
    release = tmp_path / "releases" / "2026"
    release.mkdir(parents=True)
    launchd.write(agents, launchd.render(data_dir=tmp_path, release_dir=release, home=tmp_path,
                                        origins=launchd.allowed_origins(origin="https://mac.tail.ts.net", keep=[])))
    calls: list[tuple[str, ...]] = []
    registered = {"on": True}

    def fake_run(*args):
        calls.append(args)
        if args[1] == "print":
            return subprocess.CompletedProcess(args, 0 if registered["on"] else 1, "", "")
        if args[1] == "bootout":
            registered["on"] = False
        if args[1] == "bootstrap":
            registered["on"] = True
        return subprocess.CompletedProcess(args, 0, "", "")

    unchanged = launchd.reregister_research(data_dir=tmp_path, launch_agents=agents,
                                            release_dir=release, home=tmp_path, run=fake_run)
    assert unchanged == "launchd job unchanged" and calls == []

    text = (cfg / RISK).read_text(encoding="utf-8")
    moved = (cc.get_value(yaml.safe_load(text), "research_schedule.launchd.hour") + 1) % 24
    (cfg / RISK).write_text(cc.set_values(text, {"research_schedule.launchd.hour": moved}), encoding="utf-8")
    summary = launchd.reregister_research(data_dir=tmp_path, launch_agents=agents,
                                          release_dir=release, home=tmp_path, run=fake_run)
    assert summary == "launchd job re-registered"
    verbs = [c[1] for c in calls]
    assert verbs[:2] == ["bootout", "print"] and "bootstrap" in verbs and verbs[-1] == "enable"
    assert not any(launchd.API in " ".join(c) for c in calls)
    written = launchd.installed_origins(agents)
    assert written == ["https://mac.tail.ts.net"]  # the API job and its origins were left alone
    import plistlib

    with (agents / f"{launchd.RESEARCH}.plist").open("rb") as stream:
        assert plistlib.load(stream)["StartCalendarInterval"]["Hour"] == moved
