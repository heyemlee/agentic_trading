"""Research that did not finish: told apart, recovered where it can be, and run from the release.

Three defects, all read off the runs of 2026-10-02 (docs/todo.md, P1.2):

* the CLI's login had expired, and every run was stored as "exited with code 1";
* a holding the owner had sold ended a scheduled review in a traceback;
* the worker's traceback named the checkout's own source under a service started from a release,
  because ``python -m`` puts the working directory -- the data directory, which is the checkout
  -- first on the import path.

Fully offline: the CLI is the suite's fake, and no worker process is started except the one
that proves which package a launch imports.
"""

import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agentic_trading import cli, diagnosis, research_jobs, research_worker, risk_scenarios, schedule
from agentic_trading.advisor import SNAPSHOT, SubjectUnavailable, run_task
from agentic_trading.llm.claude_code import (
    ClaudeCodeChatModel,
    ClaudeCodeError,
    _looks_limited,
    _looks_logged_out,
)
from agentic_trading.policy import run_policy
from agentic_trading.portfolio import build_snapshot
from agentic_trading.profile import load_profiles
from agentic_trading.research_jobs import ResearchJobManager, worker_command
from agentic_trading.service.status import SystemStatus
from tests.conftest import ROOT, copy_example_ledger
from tests.test_api import sandbox_service  # noqa: F401 - shared isolated fixture

EXPIRED = "Failed to authenticate: OAuth session expired and could not be refreshed"


# ---------------------------------------------------------------- naming an expired login
def test_the_cli_answer_of_2026_10_02_is_recognised_as_an_expired_login():
    assert _looks_logged_out(EXPIRED, "") is True
    assert _looks_logged_out("", "Error: Invalid API key · Please run /login") is True
    # A limit is not a login, and neither is a model's own long answer about authentication.
    assert _looks_logged_out("You've hit your session limit · resets 1:30pm", "") is False
    assert _looks_limited(EXPIRED, "") is False
    essay = ("The company's OAuth session expired during the outage, and customers who were not "
             "logged in could not authenticate. " * 8)
    assert len(essay) > 400 and _looks_logged_out(essay, "") is False


def test_a_signed_out_cli_raises_login_expired_and_not_a_limit(fake_claude, tmp_path):
    llm = ClaudeCodeChatModel(model="claude-sonnet-5", work_dir=str(tmp_path))
    with pytest.raises(ClaudeCodeError) as info:
        llm.invoke("FORCE_LOGOUT please")
    assert info.value.login_expired is True and info.value.rate_limited is False
    assert "OAuth session expired" in str(info.value)
    with pytest.raises(ClaudeCodeError) as limited:
        llm.invoke("FORCE_LIMIT please")
    assert limited.value.rate_limited is True and limited.value.login_expired is False


class _SignedOut:
    """A model whose every call is refused the way a signed-out CLI refuses it."""

    def with_structured_output(self, _schema):
        return self

    def invoke(self, _messages):
        raise ClaudeCodeError(f"claude failed (exit 1, subtype 'success'): {EXPIRED}",
                              login_expired=True, exit_code=1)


def test_the_policy_step_does_not_file_a_signed_out_reviewer_under_no_trade(profiles):
    """Nobody could ask the reviewer; that is not the reviewer saying no."""
    with pytest.raises(ClaudeCodeError) as info:
        run_policy(profiles, {}, ticker="VTI", instrument_type="etf", bucket="core",
                   pm_rating="Buy", trade_date=risk_scenarios.TRADE_DATE, llm=_SignedOut())
    assert info.value.login_expired is True


def _isolated(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "config"
    shutil.copytree(ROOT / "config", cfg_dir)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    copy_example_ledger("portfolio.example.yaml", state_dir / "portfolio.yaml",
                        as_of=risk_scenarios.TRADE_DATE)
    profiles = load_profiles(cfg_dir)
    monkeypatch.setattr(cli, "build_snapshot", lambda state, p, d: build_snapshot(
        state, p, d, risk_scenarios.price_lookup, risk_scenarios.identity_lookup))
    monkeypatch.setattr(cli, "assess_regime", lambda p, d: risk_scenarios.regime("NORMAL"))
    return profiles


def test_a_run_that_meets_a_signed_out_cli_ends_with_its_own_exit_code(tmp_path, monkeypatch, capsys):
    profiles = _isolated(tmp_path, monkeypatch)

    class Graph:
        def __init__(self, *_args, **_kwargs):
            pass

        def propagate(self, *_args, **_kwargs):
            raise ClaudeCodeError(f"claude failed (exit 1, subtype 'success'): {EXPIRED}",
                                  login_expired=True, exit_code=1)

    monkeypatch.setattr(cli, "AgenticTradingGraph", Graph)
    code = cli.run_one(profiles, "VTI", risk_scenarios.TRADE_DATE, "core", "etf",
                       tmp_path / "runs", debug=False)
    assert code == diagnosis.EXIT_LOGIN
    assert "Sign in again" in capsys.readouterr().out
    # Nothing was decided, so nothing is on record as a decision.
    assert list((tmp_path / "runs").glob("*/decision_record.json")) == []


# ---------------------------------------------------------------- the worker and the queue
def _manager(tmp_path, **settings):
    config = tmp_path / "config"
    config.mkdir(exist_ok=True)
    return ResearchJobManager(
        state_source=tmp_path / "state" / "proposals.json", project_dir=tmp_path,
        config_dir=config, settings={"max_concurrent": 2, "timeout_seconds": 120,
                                     "max_attempts": 2, **settings})


def _run_worker(tmp_path, monkeypatch, exit_code, *, raises=None):
    """One worker over a stubbed pipeline that ends the way a real one would have."""
    cfg_dir = tmp_path / "config"
    shutil.copytree(ROOT / "config", cfg_dir)
    (tmp_path / "state").mkdir()
    profiles = load_profiles(cfg_dir)
    manager = ResearchJobManager(
        state_source=profiles.project_dir / "state" / "proposals.json",
        project_dir=profiles.project_dir, config_dir=cfg_dir, settings={})
    manager.create(run_key="job", ticker="AMZN", bucket="micro_live", trade_date="2026-10-02",
                   instrument_type="stock", trigger="scheduled")
    manager.store.claim(1)

    def pipeline(*_args, **_kwargs):
        if raises is not None:
            raise raises
        return exit_code

    monkeypatch.setattr(research_worker, "run_one", pipeline)
    arguments = ["--config-dir", str(cfg_dir), "--run-key", "job"]
    if raises is None:
        returned = research_worker.main(arguments)
    else:
        # The worker records the crash and then lets it out, so the process exits non-zero.
        with pytest.raises(type(raises)):
            research_worker.main(arguments)
        returned = None
    return returned, manager.store.get("job")


@pytest.mark.parametrize("exit_code,state,failure", [
    (diagnosis.EXIT_LOGIN, "FAILED", "llm_login_expired"),
    (diagnosis.EXIT_LIMIT, "CLAUDE_LIMIT_REACHED", "llm_limit"),
    (diagnosis.EXIT_FAIL_CLOSED, "FAILED", "portfolio_unavailable"),
    (diagnosis.EXIT_SUBJECT, "FAILED", "subject_unavailable"),
    (diagnosis.EXIT_PIPELINE, "FAILED", "pipeline_error"),
    (diagnosis.EXIT_OK, "FAILED", "no_record"),                 # exited cleanly, wrote nothing
])
def test_the_worker_stores_which_way_of_not_finishing_it_was(tmp_path, monkeypatch, exit_code,
                                                           state, failure):
    returned, job = _run_worker(tmp_path, monkeypatch, exit_code)
    assert returned == exit_code
    assert (job["state"], job["failure_code"]) == (state, failure)
    assert job["error"] and job["exit_code"] == exit_code
    outcome = diagnosis.classify_job(job)
    assert outcome.category == diagnosis.RESEARCH and outcome.code == failure


def test_an_expired_login_tells_the_person_what_to_do_in_the_row_a_notice_quotes(tmp_path, monkeypatch):
    _returned, job = _run_worker(tmp_path, monkeypatch, diagnosis.EXIT_LOGIN)
    assert "sign in again" in job["error"] and "exited with code" not in job["error"]


def test_a_worker_that_crashes_is_recorded_as_a_crash(tmp_path, monkeypatch):
    _returned, job = _run_worker(tmp_path, monkeypatch, 0, raises=RuntimeError("boom"))
    assert (job["state"], job["failure_code"]) == ("FAILED", "worker_crashed")
    assert "RuntimeError: boom" in job["error"]


def _fail_for_login(manager, run_key, *, hours_ago, **extra):
    manager.store.update(run_key, {
        "state": "FAILED", "failure_code": "llm_login_expired", "exit_code": diagnosis.EXIT_LOGIN,
        "error": "the Claude login on this Mac has expired",
        "finished_at": (datetime.now(UTC) - timedelta(hours=hours_ago)).isoformat(), **extra,
    }, expected_states=("RUNNING", "QUEUED", "FAILED"))


def test_a_run_stopped_by_an_expired_login_is_retried_and_only_so_many_times(tmp_path, monkeypatch):
    """A retry is how recovery is noticed: the call is refused in seconds, or it works."""
    manager = _manager(tmp_path, claude_login_retry_after_hours=6, max_login_recoveries=1)
    manager.create(run_key="amzn", ticker="AMZN", bucket="micro_live", trade_date="2026-10-02",
                   instrument_type="stock", trigger="scheduled")
    manager.store.claim(2)
    _fail_for_login(manager, "amzn", hours_ago=1)
    monkeypatch.setattr(manager, "dispatch", list)

    assert manager.tick()["resumed_after_login"] == []            # not yet
    _fail_for_login(manager, "amzn", hours_ago=7)
    assert manager.tick()["resumed_after_login"] == ["amzn"]
    job = manager.store.get("amzn")
    assert job["state"] == "QUEUED" and job["login_recoveries"] == 1
    assert job["failure_code"] == "llm_login_expired"            # still what it is waiting out

    # Claimed again, the reason is cleared: while it runs, nothing says the login is expired.
    claimed = manager.store.claim(2)[0]
    assert claimed["failure_code"] is None and claimed["error"] is None
    _fail_for_login(manager, "amzn", hours_ago=7)
    assert manager.tick()["resumed_after_login"] == []            # the allowance is spent


def test_only_an_expired_login_is_retried_this_way(tmp_path, monkeypatch):
    manager = _manager(tmp_path)
    manager.create(run_key="broken", ticker="VTI", bucket="core", trade_date="2026-10-02",
                   instrument_type="etf", trigger="manual")
    manager.store.claim(2)
    manager.store.update("broken", {
        "state": "FAILED", "failure_code": "pipeline_error",
        "finished_at": (datetime.now(UTC) - timedelta(hours=30)).isoformat()},
        expected_states=("RUNNING",))
    monkeypatch.setattr(manager, "dispatch", list)
    assert manager.tick()["resumed_after_login"] == []
    assert manager.store.get("broken")["state"] == "FAILED"


def test_a_question_that_was_asked_again_is_not_requeued_beside_its_newer_run(tmp_path, monkeypatch):
    manager = _manager(tmp_path)
    manager.create(run_key="old", ticker="AMZN", bucket="micro_live", trade_date="2026-10-02",
                   instrument_type="stock", trigger="scheduled")
    manager.store.claim(2)
    _fail_for_login(manager, "old", hours_ago=9)
    # The same question, asked again by hand once the owner had signed in.
    _newer, created = manager.create(run_key="new", ticker="AMZN", bucket="micro_live",
                                     trade_date="2026-10-02", instrument_type="stock", trigger="manual")
    assert created is True
    monkeypatch.setattr(manager, "dispatch", list)
    assert manager.tick()["resumed_after_login"] == []
    assert manager.store.get("old")["state"] == "FAILED"
    assert manager.store.get("new")["state"] == "QUEUED"


def test_the_login_retry_keeps_the_limits_rhythm_unless_it_is_given_its_own(tmp_path):
    inherited = _manager(tmp_path, claude_limit_retry_after_hours=3, max_limit_recoveries=4)
    assert (inherited.login_retry_after, inherited.max_login_recoveries) == (3.0, 4)
    own = _manager(tmp_path, claude_login_retry_after_hours=1, max_login_recoveries=0)
    assert (own.login_retry_after, own.max_login_recoveries) == (1.0, 0)


# ---------------------------------------------------------------- which code a worker runs
def test_a_worker_is_started_with_the_working_directory_off_the_import_path(tmp_path, monkeypatch):
    command = worker_command(tmp_path / "config", "2026-10-02_AMZN_55ed6631")
    assert command[:4] == [sys.executable, "-P", "-m", "agentic_trading.research_worker"]
    assert command[-2:] == ["--run-key", "2026-10-02_AMZN_55ed6631"]

    manager = _manager(tmp_path)
    manager.create(run_key="job", ticker="VTI", bucket="core", trade_date="2026-10-02",
                   instrument_type="etf", trigger="manual")
    launched = []

    class Process:
        pid = 4321

    monkeypatch.setattr(research_jobs.subprocess, "Popen",
                        lambda cmd, **kwargs: launched.append((cmd, kwargs)) or Process())
    monkeypatch.setattr(research_jobs, "_scrubbed_environment", dict)
    assert manager.dispatch() == ["job"]
    cmd, kwargs = launched[0]
    assert cmd == worker_command(manager.config_dir, "job")
    # The working directory is still the data directory: relative state paths depend on it.
    assert Path(kwargs["cwd"]) == manager.project_dir
    # And a worker the supervisor asks about is still recognised as its own.
    monkeypatch.setattr(research_jobs, "_worker_command", lambda _pid: " ".join(cmd))
    monkeypatch.setattr(research_jobs.os, "kill", lambda _pid, _sig: None)
    assert research_jobs._worker_state(4321, "job") == research_jobs.WORKER_OURS


def test_the_flag_is_what_keeps_a_package_in_the_data_directory_from_being_imported(tmp_path):
    """The mechanism itself, with a real interpreter, in a directory shaped like the checkout.

    The data directory of a release install is the developer's checkout, which holds a package
    of the same name as the installed one. Started there with ``-m`` or ``-c``, the interpreter
    imports that one. ``-P`` is the difference, and this is what it was on 2026-10-02.
    """
    decoy = tmp_path / "agentic_trading"
    decoy.mkdir()
    (decoy / "__init__.py").write_text("raise SystemExit(97)\n", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONSAFEPATH", "PYTHONPATH")}

    def start(*flags):
        return subprocess.run([sys.executable, *flags, "-c", "import agentic_trading"],
                              cwd=tmp_path, env=env, capture_output=True, text=True,
                              timeout=60, check=False).returncode

    assert start() == 97, "without the flag the working directory's package wins"
    assert start("-P") == 0, "with it, the installed package is the one imported"
    assert "-P" in worker_command(tmp_path, "any")


# ---------------------------------------------------------------- what health and the schedule say
def _newest_job(service, **fields):
    profiles = service.profiles()
    manager = service._research_manager(profiles)
    manager.create(run_key="newest", ticker="AMZN", bucket="micro_live",
                   trade_date="2026-10-02", instrument_type="stock", trigger="scheduled")
    manager.store.update("newest", {"finished_at": datetime.now(UTC).isoformat(), **fields},
                         expected_states=("QUEUED",))
    return profiles


def test_health_says_the_login_expired_and_scheduled_research_is_not_blocked_by_it(sandbox_service):  # noqa: F811
    profiles = _newest_job(sandbox_service, state="FAILED", failure_code="llm_login_expired",
                           error="the Claude login on this Mac has expired")
    health = sandbox_service.health()
    assert health.claude.state == "LOGIN_REQUIRED" and "signs in again" in health.claude.detail
    assert health.status == SystemStatus.CLAUDE_LOGIN_REQUIRED
    assert health.detail == health.claude.detail
    # Not a blocking status. A blocked schedule would start nothing, so nothing newer than the
    # failed job would ever exist and the status could never clear; a run that goes ahead costs
    # one refused call and is how the recovery is noticed.
    assert SystemStatus.CLAUDE_LOGIN_REQUIRED not in schedule.BLOCKING_STATUS
    decision = schedule.decide(profiles, health_status=health.status, paused=bool(health.paused),
                               executed={}, jobs=[], subjects=["VTI"],
                               now=datetime(2026, 10, 2, 21, 0, tzinfo=UTC))
    assert decision.run is True


def test_health_clears_as_soon_as_a_newer_run_is_not_a_login_failure(sandbox_service):  # noqa: F811
    _newest_job(sandbox_service, state="FAILED", failure_code="pipeline_error", error="boom")
    assert sandbox_service.health().claude.state == "OK"
    assert sandbox_service.health().status == SystemStatus.READY


def test_a_job_row_carries_its_diagnosis_and_a_legacy_row_is_read_from_its_log(sandbox_service):  # noqa: F811
    profiles = sandbox_service.profiles()
    manager = sandbox_service._research_manager(profiles)
    job, _ = manager.create(run_key="legacy", ticker="AMZN", bucket="micro_live",
                            trade_date="2026-10-02", instrument_type="stock", trigger="scheduled")
    Path(job["log"]).write_text(
        "[AMZN] FAILED: LLM backend error: claude failed (exit 1, subtype 'success'): "
        f"{EXPIRED}\n", encoding="utf-8")
    # As the rows of 2026-10-02 were stored: a sentence, and no code beside it.
    manager.store.update("legacy", {"state": "FAILED", "failure_code": None, "exit_code": 1,
                                    "error": "research pipeline exited with code 1",
                                    "finished_at": datetime.now(UTC).isoformat()},
                         expected_states=("QUEUED",))
    row = next(j for j in sandbox_service.research_jobs() if j["run_key"] == "legacy")
    assert row["diagnosis"]["category"] == "research_failed"
    assert row["diagnosis"]["code"] == "llm_login_expired"
    assert "登录已过期" in row["diagnosis"]["summary"]["zh"]
    # The shape the API returns it in accepts it: an unknown field there is a 500.
    from agentic_trading.service.shapes import ResearchJob

    assert ResearchJob(**row).diagnosis.code == "llm_login_expired"


def test_the_to_do_list_says_why_research_failed_in_both_languages(sandbox_service):  # noqa: F811
    _newest_job(sandbox_service, state="FAILED", failure_code="llm_login_expired",
                error="the Claude login on this Mac has expired; sign in again")
    items = [item for item in sandbox_service.attention()["items"] if item["kind"] == "research_failed"]
    assert len(items) == 1
    assert "sign in again" in items[0]["detail"]                 # what a notification still quotes
    assert "登录已过期" in items[0]["detail_i18n"]["zh"] and items[0]["detail_i18n"]["en"]
    from agentic_trading.service.shapes import AttentionItem

    assert AttentionItem(**items[0]).detail_i18n == items[0]["detail_i18n"]


# ---------------------------------------------------------------- a holding sold before its review
def test_a_review_of_a_holding_that_was_sold_is_skipped_not_crashed(sandbox_service, monkeypatch, capsys):  # noqa: F811
    profiles = sandbox_service.profiles()
    db = sandbox_service.state_db(profiles)
    # Read a minute ago, so the task does not reach for the venue; the holding is not in it.
    snapshot = {"fetched_at": datetime.now(UTC).isoformat(), "errors": [], "candidates": [],
                "accounts": [{"id": "acct-1", "label": "Individual", "complete": True,
                              "positions": [{"symbol": "VTI", "quantity": 1.0}]}]}
    db.write_meta(SNAPSHOT, snapshot)
    job = {"run_key": "advisor_gone", "ticker": "NVDA", "trade_date": "2026-10-02",
           "task": {"kind": "analysis", "account_id": "acct-1", "scope": "personal",
                    "language": "zh"}}
    assert run_task(profiles, job) == diagnosis.EXIT_SUBJECT
    assert "no longer present" in capsys.readouterr().out
    assert not (profiles.project_dir / "runs" / "advisor_gone" / "decision_record.json").exists()
    named, _sentence = diagnosis.failure_for_exit(diagnosis.EXIT_SUBJECT, has_record=False)
    assert named == "subject_unavailable"
    assert issubclass(SubjectUnavailable, ValueError)             # callers that caught it still do


def test_the_decision_record_says_why_it_ended_as_it_did(tmp_path):
    """What the pipeline writes into a record is the same reading a screen is given."""
    record = {"upstream_decision": {"portfolio_manager_signal": "Hold"},
              "policy": {"final_action": "NO_TRADE", "confidence": 38,
                         "reason_codes": [{"code": "rating_hold_no_position"}]},
              "hard_risk": {"verdict": "NO_ACTION", "rules": []},
              "final_recommendation": {"action": "NO_TRADE"}}
    written = diagnosis.classify_record(record).as_dict()
    assert json.loads(json.dumps(written, ensure_ascii=False))["category"] == "strategy_no_trade"
    assert set(written) == {"category", "code", "label", "summary", "reasons"}
