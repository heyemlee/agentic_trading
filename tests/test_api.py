"""Application interface: product status mapping, service operations, security boundaries.

Offline. No broker session, no model call, no writes to the real state directory.
"""

from __future__ import annotations

import json
import re
import shutil
from datetime import timedelta
from uuid import uuid4

import pytest
import yaml

from agentic_trading.execution_scenarios import _proposal
from agentic_trading.proposals import (
    APPROVED,
    EXECUTED,
    INVALIDATED,
    NEEDS_REANALYSIS,
    PROPOSED,
    ProposalStore,
    positions_fingerprint,
    utcnow,
)
from agentic_trading.research_jobs import ResearchJobManager
from agentic_trading.service import Service, ServiceError
from agentic_trading.service.shapes import (
    Fill,
    HardRiskView,
    Health,
    MarketStatus,
    MicroLive,
    Orders,
    Portfolio,
    PortfolioAccount,
    PortfolioBucket,
    PortfolioPosition,
    ProposalDetail,
    ProposalSummary,
    TrackedOrder,
)
from agentic_trading.service.status import (
    MicroLiveStatus,
    ProposalStatus,
    SystemStatus,
    proposal_status,
)
from agentic_trading.state_db import StateDB
from tests.conftest import ROOT, copy_example_ledger, research_as_written


@pytest.fixture
def sandbox_service(tmp_path, offline_market_reads):
    """A Service whose every state path points at a temp directory.

    Its session and market regime are answered offline as well: see `offline_market_reads`
    in tests/conftest.py. A test about either patches it again itself.
    """
    cfg = tmp_path / "config"
    shutil.copytree(ROOT / "config", cfg)
    state = tmp_path / "state"
    state.mkdir()
    copy_example_ledger("portfolio.example.yaml", state / "portfolio.yaml")
    copy_example_ledger("micro_live_portfolio.example.yaml", state / "micro_live_portfolio.yaml")
    (tmp_path / "runs").mkdir()
    disarm(cfg / "risk_profile.yaml")
    # And for the same reason, which research the owner has switched on today.
    research_as_written(cfg / "risk_profile.yaml")
    return Service(cfg)


def disarm(risk_profile):
    """Force real-money execution off in a copied config.

    The sandbox copies the deployment's own config, which is what makes these tests exercise the
    real rules rather than a fixture nobody maintains. It also means the suite inherits whatever
    the machine is currently set to, and on the day the micro-live switch was armed for real,
    three tests that read "real money is off" as a fact started failing. Whether this deployment
    is trading is an operational decision; it is not something a test may depend on either way.
    """
    text = risk_profile.read_text()
    text, count = re.subn(r"(micro_live_execution:\n(?:.*\n)*?  enabled: )true",
                          r"\1false", text, count=1)
    risk_profile.write_text(text)
    return count


def arm(risk_profile):
    """The inverse of `disarm`, for the one branch that only exists when the switch is on.

    A test that needs the real-money path has to say so in its own config copy rather than hope
    the machine is armed today — the same reason `disarm` exists, read from the other side.
    """
    text, count = re.subn(r"(micro_live_execution:\n(?:.*\n)*?  enabled: )false",
                          r"\1true", risk_profile.read_text(), count=1)
    risk_profile.write_text(text)
    assert count == 1, "the sandbox config carries no micro-live switch to arm"


def test_health_reports_a_product_status(sandbox_service):
    health = sandbox_service.health()
    assert health.status in set(SystemStatus)
    assert isinstance(health.warnings, list)
    assert health.micro_live_enabled is False           # the sandbox is always disarmed
    assert health.claude is not None
    assert health.claude.billing == "claude_subscription_no_api_charges"


def test_a_legacy_run_log_does_not_report_the_subscription_as_limited(sandbox_service):
    """Nothing writes runs/*.log any more; workers log to runs/api and launchd to runs/service. A
    limit line left in an old file reported CLAUDE_LIMIT_REACHED for good, and scheduled research
    was skipped because of it. The job store is what records a limit now."""
    runs = sandbox_service.profiles().project_dir / "runs"
    (runs / "2026-08-30_VTI.log").write_text(
        "STOPPED: You've hit your session limit · resets 1:30pm\n", encoding="utf-8")
    assert sandbox_service.claude_status().state == "OK"
    assert sandbox_service.health().status != SystemStatus.CLAUDE_LIMIT_REACHED


def test_calls_today_are_counted_by_utc_date_from_the_end_of_the_audit(sandbox_service, monkeypatch):
    """Timestamps carry the Mac's offset, and the count compared them with the UTC date as text,
    missing every call made while the two dates differ; one bad line ended the count."""
    import json as _json
    from datetime import UTC, datetime, timedelta, timezone

    from agentic_trading.service import system as service_module

    now = datetime.now(UTC)
    # an offset that puts the local date on a different day from the UTC one, whatever the hour
    offset = timezone(timedelta(hours=13 if now.hour >= 12 else -13))
    stamp = lambda moment: moment.astimezone(offset).strftime("%Y-%m-%dT%H:%M:%S%z")
    audit = sandbox_service.profiles().project_dir / "runs" / "llm_calls.jsonl"
    rows = [_json.dumps({"ts": stamp(now - timedelta(days=2)), "note": "x" * 900}) for _ in range(40)]
    rows += [_json.dumps({"ts": stamp(now)}), "{not json", _json.dumps({"ts": stamp(now)})]
    audit.write_text("\n".join(rows) + "\n", encoding="utf-8")

    monkeypatch.setattr(service_module, "AUDIT_TAIL_BYTES", 4000)     # only the end is read
    status = sandbox_service.claude_status()
    assert status.calls_today == 2
    assert status.last_call_at == stamp(now)


def test_a_halt_that_needs_a_person_is_not_hidden_behind_a_quota_wait(sandbox_service, monkeypatch):
    from agentic_trading.service.shapes import ClaudeStatus

    monkeypatch.setattr(sandbox_service, "risk_state", lambda: {
        "total": {"level": "HARD_HALT", "manual_reset_required": True}})
    monkeypatch.setattr(sandbox_service, "claude_status", lambda: ClaudeStatus(
        state="LIMIT_REACHED", detail="resets 1:30pm", billing="claude_subscription_no_api_charges"))
    health = sandbox_service.health()
    assert health.status == SystemStatus.HALTED
    assert health.claude.state == "LIMIT_REACHED"


def test_health_never_exposes_credentials(sandbox_service):
    blob = sandbox_service.health().model_dump_json().lower()
    for secret in ("access_token", "refresh_token", "client_secret", "api_key", "bearer"):
        assert secret not in blob


def test_health_still_answers_when_the_configuration_is_what_broke(
        auth_client, monkeypatch, sandbox_service):
    """The endpoint you ask what went wrong must not be the one that went down.

    Nothing below the status can be answered without a config, so /health replies with the
    status, the reason and the time, and every other field as null. Declaring any of them
    required would make this exact call a 500 — the one moment its answer is the only one
    available.
    """
    (sandbox_service.config_dir / "risk_profile.yaml").write_text(
        "micro_live_execution: [not a mapping]\n", encoding="utf-8")
    monkeypatch.setattr("agentic_trading.api.app._service", sandbox_service)

    result = auth_client.get("/health")
    assert result.status_code == 200, result.text
    body = result.json()

    assert body["status"] == SystemStatus.CONFIG_ERROR
    assert "risk_profile" in body["detail"]              # the reason, not a generic failure
    assert set(body) == set(Health.model_fields)         # present as null, not missing
    for absent in ("paused", "stage", "market", "claude", "micro_live_enabled"):
        assert body[absent] is None, absent

    # The console offers a real-money approval only on `micro_live_enabled === true`, so a
    # config that will not load closes that gate rather than leaving it undefined.
    assert body["micro_live_enabled"] is not True


def test_a_calendar_that_cannot_be_read_reports_the_session_closed(sandbox_service, monkeypatch):
    """An unknown session must never be the one that lets an order through."""
    def broken():
        raise RuntimeError("calendar data unavailable")

    monkeypatch.setattr("agentic_trading.broker.robinhood._nyse_calendar", broken)

    market = sandbox_service.market_status()
    assert market.is_open is False                       # closed, not unknown
    assert market.error and "calendar data unavailable" in market.error
    assert market.next_open is None and market.next_close is None
    assert set(market.model_dump()) == set(MarketStatus.model_fields)


def test_pause_and_resume_move_the_kill_switch(sandbox_service):
    assert sandbox_service.health().paused is False
    sandbox_service.pause("test")
    assert sandbox_service.health().paused is True
    assert sandbox_service.health().status == SystemStatus.PAUSED
    sandbox_service.resume()
    assert sandbox_service.health().paused is False


def test_portfolio_exposes_every_bucket_and_marks_real_money(sandbox_service):
    portfolio = sandbox_service.portfolio()
    assert set(portfolio.buckets) == {"core", "sandbox", "micro_live"}
    assert portfolio.buckets["micro_live"].is_real_money is True
    assert portfolio.buckets["micro_live"].venue == "robinhood"
    assert portfolio.buckets["core"].is_real_money is False


def test_proposal_status_covers_the_lifecycle(profiles):
    assert proposal_status(_proposal(status=PROPOSED), profiles) == ProposalStatus.WAITING_FOR_APPROVAL
    assert proposal_status(_proposal(status=APPROVED), profiles) == ProposalStatus.APPROVED_AWAITING_EXECUTION

    executed = _proposal(status=EXECUTED)
    executed.executed_notional_usd = executed.notional_usd
    assert proposal_status(executed, profiles) == ProposalStatus.EXECUTED

    partial = _proposal(status=EXECUTED)
    partial.executed_notional_usd = partial.notional_usd / 2
    assert proposal_status(partial, profiles) == ProposalStatus.PARTIALLY_FILLED

    assert proposal_status(_proposal(status=NEEDS_REANALYSIS), profiles) == ProposalStatus.REANALYSIS_REQUIRED
    assert proposal_status(_proposal(status=INVALIDATED), profiles) == ProposalStatus.INVALIDATED
    assert proposal_status(_proposal(age_minutes=5000), profiles) == ProposalStatus.STALE


def test_micro_live_status_is_disabled_until_the_switch_is_flipped(profiles):
    from agentic_trading.service.status import micro_live_status

    assert micro_live_status(enabled=False, connected=True, preflight_ready=True,
                             open_proposal=None, profiles=profiles) == MicroLiveStatus.DISABLED
    assert micro_live_status(enabled=True, connected=False, preflight_ready=False,
                             open_proposal=None, profiles=profiles) == MicroLiveStatus.NOT_CONNECTED
    assert micro_live_status(enabled=True, connected=True, preflight_ready=False,
                             open_proposal=None, profiles=profiles) == MicroLiveStatus.NOT_READY
    assert micro_live_status(enabled=True, connected=True, preflight_ready=True,
                             open_proposal=None, profiles=profiles) == MicroLiveStatus.WAITING_FOR_PROPOSAL
    assert micro_live_status(enabled=True, connected=True, preflight_ready=True,
                             open_proposal=_proposal(status=PROPOSED),
                             profiles=profiles) == MicroLiveStatus.WAITING_FOR_APPROVAL


def test_micro_live_answers_with_a_declared_shape_when_the_venue_is_unreachable(
        auth_client, monkeypatch, sandbox_service):
    """The response model has to describe the degraded path, not only a connected one.

    Every screen polls this route, and three of them read it without a guard of their own: the
    risk page iterates checks, blocking and permissions, and the approvals page will not offer a
    real-money approval unless preflight_ready is true. A required field the disconnected path
    cannot supply would turn a broker outage into a 500 here, and that 500 into a blank risk
    screen — a shape that fails exactly when the venue does is worse than no shape at all.
    """
    monkeypatch.setattr("agentic_trading.api.app._service", sandbox_service)
    result = auth_client.get("/micro-live")
    assert result.status_code == 200, result.text
    body = result.json()

    assert body["connected"] is False and body["connect_error"]
    assert body["status"] == MicroLiveStatus.DISABLED
    assert body["account"] is None                       # no half-read account, ever
    # Nothing is dropped on the way out: the wire carries every field the model declares.
    assert set(body) == set(MicroLive.model_fields)
    # The unguarded reads: a list to iterate and a map to enumerate, never a missing key.
    assert body["preflight_ready"] is False
    assert body["checks"] and isinstance(body["checks"], list)
    assert isinstance(body["blocking"], list) and isinstance(body["permissions"], dict)


def test_proposal_listing_and_detail(sandbox_service):
    profiles = sandbox_service.profiles()
    store = ProposalStore(profiles.project_dir / "state" / "proposals.json")
    profiles.risk["broker"]["state_file"] = "state/proposals.json"
    p = _proposal(status=PROPOSED)
    store.save(p)

    listed = sandbox_service.proposals()
    assert len(listed) == 1
    row = listed[0]
    assert row.status == ProposalStatus.WAITING_FOR_APPROVAL
    assert row.requires_human_approval is True
    assert row.freshness.state == "VALID" and row.freshness.ttl_minutes == 2880

    detail = sandbox_service.proposal(p.proposal_id)
    assert detail.world_at_proposal.ladder_level == "NORMAL"
    assert [row["event"] for row in detail.history] == ["created"]
    # No run record in the sandbox, so the reasoning blocks are absent together and the
    # identity the approval screen refuses against is still whole.
    assert detail.research is None and detail.hard_risk is None
    assert detail.proposal_id == p.proposal_id and detail.version == row.version


def test_approving_an_unknown_proposal_is_a_clean_error(sandbox_service):
    with pytest.raises(ServiceError, match="no proposal"):
        sandbox_service.approve("does-not-exist")


def test_the_queue_and_its_detail_carry_the_identity_an_approval_is_made_against(
        auth_client, monkeypatch, sandbox_service):
    """proposal_id and version have to survive the wire intact, and so does `class`.

    The console pins an open detail view to this pair and refuses an approval whose version has
    moved underneath it; the approve route then takes both values back and checks them again.
    A field renamed anywhere along that path does not degrade — it unpins the screen from the
    thing it is about.
    """
    profiles = sandbox_service.profiles()
    profiles.risk["broker"]["state_file"] = "state/proposals.json"
    stored = _proposal(status=PROPOSED)
    ProposalStore(profiles.project_dir / "state" / "proposals.json").save(stored)
    monkeypatch.setattr("agentic_trading.api.app._service", sandbox_service)

    listed = auth_client.get("/proposals")
    assert listed.status_code == 200, listed.text
    row = listed.json()[0]
    assert set(row) == set(ProposalSummary.model_fields)
    assert row["proposal_id"] == stored.proposal_id and row["version"] == stored.version()

    detail = auth_client.get(f"/proposals/{stored.proposal_id}")
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert set(body) == set(ProposalDetail.model_fields)
    assert body["proposal_id"] == row["proposal_id"] and body["version"] == row["version"]

    # The freshness block is serialised by alias: the console reads "class", which is not a
    # name a Python field can carry.
    assert set(body["freshness"]) == {"state", "label", "detail", "class",
                                      "ttl_minutes", "age_minutes", "max_price_drift_pct"}
    # No run record in the sandbox: the reasoning is absent, explicitly, and the approval
    # screen still has every field it refuses or allows against.
    assert body["research"] is None and body["hard_risk"] is None
    assert body["status"] and body["requires_human_approval"] is True


def test_a_proposal_whose_research_survives_carries_the_case_and_the_rules(sandbox_service):
    """The reasoning blocks, mapped out of a decision record the pipeline could have written.

    The research document is not free-form: the reviewer fills `ConservativeProposal`, so the
    record here is built from that model rather than typed out. A field renamed on the mandate
    side therefore changes this test's input, and the assertion that every field arrives is
    what catches a rename this projection did not follow.
    """
    from agentic_trading.policy import ConservativeProposal

    profiles = sandbox_service.profiles()
    profiles.risk["broker"]["state_file"] = "state/proposals.json"
    stored = _proposal(status=PROPOSED)
    stored.run_id = "2026-09-08_VTI_offline"
    ProposalStore(profiles.project_dir / "state" / "proposals.json").save(stored)

    doc = ConservativeProposal(
        ticker="VTI", recommended_action="BUY", confidence=78, market_risk_level="NORMAL",
        investment_thesis="broad, cheap, liquid", bull_case="trend intact", bear_case="macro unverified",
        primary_risks=["mega-cap concentration"], what_could_make_this_thesis_wrong="a rate surprise",
        entry_logic="single tranche", exit_logic="reviewed at -12%",
        expected_holding_period="indefinite", maximum_acceptable_loss="12%",
        reason_this_improves_the_portfolio="first diversified exposure",
        alternative_why_doing_nothing_may_be_preferable="cash carries no drawdown",
        missing_information=["FRED unavailable"], data_quality="partial")
    run = profiles.project_dir / "runs" / stored.run_id
    run.mkdir(parents=True)
    (run / "decision_record.json").write_text(json.dumps({
        "upstream_decision": {"portfolio_manager_signal": "Buy"},
        "policy": {"final_action": "BUY", "reasons": ["confidence 78"], "confidence": 78,
                   "proposal": doc.model_dump()},
        "hard_risk": {"verdict": "APPROVE", "final_action": "BUY", "proposed_notional_usd": 35.0,
                      "post_trade_weight_pct": 35.0, "requires_human_approval": True,
                      "rules": [{"rule_id": "HR08", "name": "position cap", "passed": True,
                                 "detail": "post-trade 35.0% vs cap 40.0%"}],
                      "failed_rule_ids": [], "advisories": [],
                      "ladder": {"effective_level_for_bucket": "NORMAL"},
                      "regime": {"level": "NORMAL", "available": True}},
    }), encoding="utf-8")

    detail = sandbox_service.proposal(stored.proposal_id)

    assert detail.research is not None and detail.hard_risk is not None
    # Every projected field arrives: a rename on either side shows up here as a None.
    assert all(value is not None for value in detail.research.model_dump().values())
    assert detail.research.investment_thesis == "broad, cheap, liquid"
    assert detail.research.what_could_make_this_wrong == "a rate surprise"
    assert detail.research.why_doing_nothing_may_be_better == "cash carries no drawdown"
    assert detail.research.portfolio_manager_rating == "Buy"

    assert detail.hard_risk.verdict == "APPROVE"
    assert detail.hard_risk.notional_usd == 35.0            # proposed_notional_usd on the record
    assert [r.id for r in detail.hard_risk.rules] == ["HR08"]
    assert detail.hard_risk.ladder == {"effective_level_for_bucket": "NORMAL"}


def test_a_record_that_says_nothing_never_reads_as_a_waived_human(sandbox_service):
    """An empty Hard Risk block must not come back saying no approval is needed."""
    from agentic_trading.service.approvals import _hard_risk_view

    view = _hard_risk_view({})
    assert view.requires_human_approval is True
    assert view.rules == [] and view.failed_rule_ids == [] and view.advisories == []
    assert view.verdict is None and view.ladder is None
    assert set(view.model_dump()) == set(HardRiskView.model_fields)


def _tracked(profiles, **fields):
    """A proposal that reached the venue, so it shows up under tracked_orders."""
    p = _proposal(status=PROPOSED)
    p.client_order_id = "ct-test-1"
    for key, value in fields.items():
        setattr(p, key, value)
    ProposalStore(profiles.project_dir / "state" / "proposals.json").save(p)
    return p


def test_the_orders_screen_is_read_without_asking_the_venue(sandbox_service, monkeypatch):
    """Fills and tracked orders are this side's own record, and reading them opens no session.

    The venue's open-order list used to be read here as well, once per poll of the Activity
    screen, to show what the Robinhood app already shows. A venue that would not answer then
    had to be kept from emptying the local part; now there is no venue part to fail.
    """
    profiles = sandbox_service.profiles()
    stored = _tracked(profiles, order_status="filled", reconciliation_error="venue lost the order")
    StateDB(profiles.project_dir / "state" / "fills.jsonl").append_fill(
        {"fill_id": "f1", "proposal_id": stored.proposal_id, "at": "2026-09-10T00:00:00+00:00",
         "symbol": "VTI", "side": "buy", "quantity": 0.1, "price": 376.88, "notional": 37.69},
        profiles.project_dir / "state" / "fills.jsonl")
    arm(sandbox_service.config_dir / "risk_profile.yaml")
    monkeypatch.setattr(sandbox_service, "_broker",
                        lambda *_args: pytest.fail("the orders screen needs no venue session"))

    out = sandbox_service.orders()

    assert [f.fill_id for f in out.fills] == ["f1"]
    assert [t.proposal_id for t in out.tracked_orders] == [stored.proposal_id]

    # The venue sounded certain; this side could not account for it. The two must not be merged.
    tracked = out.tracked_orders[0]
    assert tracked.venue_status == "filled"
    assert tracked.status == ProposalStatus.UNKNOWN
    assert tracked.reconciliation_error == "venue lost the order"
    assert set(tracked.model_dump()) == set(TrackedOrder.model_fields)


def test_a_fill_written_by_an_older_version_still_reaches_the_screen_whole(
        auth_client, monkeypatch, sandbox_service):
    """Fill is the one shape here that allows extras, and this is what that buys.

    The journal is append-only and is read back through a JSONL fallback for databases that
    were never migrated, so it hands back rows this code did not write. Forbidding what it
    cannot recognise would drop a real fill off a real ledger silently.
    """
    profiles = sandbox_service.profiles()
    path = profiles.project_dir / "state" / "fills.jsonl"
    StateDB(path).append_fill(
        {"fill_id": "old-1", "proposal_id": "P1", "at": "2026-09-10T00:00:00+00:00",
         "symbol": "VTI", "side": "buy", "quantity": 0.1, "price": 376.88, "notional": 37.69,
         "venue_note": "a key no model declares"}, path)
    monkeypatch.setattr("agentic_trading.api.app._service", sandbox_service)

    body = auth_client.get("/orders").json()
    assert set(body) == set(Orders.model_fields)

    fill = body["fills"][0]
    assert fill["venue_note"] == "a key no model declares"    # survives the response model
    assert set(Fill.model_fields) <= set(fill)                # and so does every declared key
    assert fill["symbol"] == "VTI" and fill["notional"] == 37.69
    assert fill["bucket"] is None                              # absent in the row, not invented


def test_real_money_broker_is_refused_while_micro_live_is_disabled(sandbox_service):
    profiles = sandbox_service.profiles()
    assert (profiles.risk["micro_live_execution"]["enabled"]) is False
    with pytest.raises(ServiceError, match="disabled"):
        sandbox_service._broker(profiles, "micro_live")


def test_a_sandbox_never_inherits_an_armed_deployment(tmp_path):
    """The guard for the fixture above: an armed config must not reach a test unchanged."""
    armed = tmp_path / "risk_profile.yaml"
    armed.write_text("robinhood:\n  enabled: true\n"
                     "micro_live_execution:\n  enabled: true\n  max_capital_usd: 100\n")
    assert disarm(armed) == 1
    text = armed.read_text()
    assert "micro_live_execution:\n  enabled: false" in text
    assert "robinhood:\n  enabled: true" in text          # only the one switch is touched


def test_the_authority_is_published_and_the_config_overview_carries_it(
        auth_client, monkeypatch, sandbox_service):
    """One object for the settings, risk and home screens, computed without the venue."""
    monkeypatch.setattr("agentic_trading.api.app._service", sandbox_service)
    response = auth_client.get("/config/authority")
    assert response.status_code == 200, response.text
    body = response.json()
    assert {"checked_at", "version", "history", "actions", "limits", "pending_changes",
            "preflight", "consistency"} <= set(body)
    assert set(body["actions"]) == {"BUY", "ADD", "REDUCE", "SELL"}
    # Real money is off in the sandbox copy (see disarm), which stops a person as much as code.
    buy = body["actions"]["BUY"]
    assert buy["mode"] == "blocked" and buy["human_approval"]["permitted"] is False
    assert "real_money_disabled" in {b["code"] for b in buy["human_approval"]["blocked_by"]}
    # No venue was asked: the preflight block says so instead of pretending a reading.
    assert body["preflight"]["checked_at"] is None and body["preflight"]["stale"] is True
    overview = auth_client.get("/config").json()
    assert overview["authority"]["version"] == body["version"]
    assert "autonomy" in overview                       # kept one round for the console
    schema = auth_client.get("/openapi.json").json()
    approve = schema["components"]["schemas"]["ApproveRequest"]["properties"]
    assert {"authority_version", "acknowledge_authority_change"} <= set(approve)


def test_attention_and_the_opened_mark_are_published(auth_client, monkeypatch, sandbox_service):
    """Home reads the to-do list from here, and says "seen" through a write of its own."""
    monkeypatch.setattr("agentic_trading.api.app._service", sandbox_service)
    monkeypatch.setattr(sandbox_service, "_broker_connect_error", lambda: None)
    response = auth_client.get("/attention")
    assert response.status_code == 200, response.text
    body = response.json()
    assert {"checked_at", "readable", "unreadable", "top", "items", "summary", "context"} <= set(body)
    assert body["readable"] is True

    manager = sandbox_service._research_manager(sandbox_service.profiles())
    manager.create(run_key="advisor_seen", ticker="VTI", bucket="advisory",
                   trade_date="2026-09-17", instrument_type="auto", trigger="manual",
                   task={"kind": "analysis", "account_id": None, "language": "zh"})
    # Not finished: nothing to have seen yet.
    early = auth_client.post("/research/advisor_seen/opened", json={})
    assert early.status_code == 409, early.text
    manager.store.update("advisor_seen", {"state": "COMPLETE", "finished_at": "2026-09-18T20:00:00+00:00"})
    first = auth_client.post("/research/advisor_seen/opened", json={})
    assert first.status_code == 200 and first.json()["opened_at"], first.text
    again = auth_client.post("/research/advisor_seen/opened", json={})
    assert again.json()["opened_at"] == first.json()["opened_at"]
    # A read never marks anything.
    assert auth_client.get("/research/advisor_seen").json()["opened_at"] == first.json()["opened_at"]


def test_the_evaluation_answers_by_scope_and_never_500s_without_a_study(
        auth_client, monkeypatch, sandbox_service):
    """No study is declared in the sandbox: every scope still answers, and says so."""
    monkeypatch.setattr("agentic_trading.api.app._service", sandbox_service)
    for scope, label in (("demo", "Demo"), ("agentic", "Agentic")):
        response = auth_client.get(f"/evaluation?scope={scope}")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["scope"] == scope and body["state"] == "NOT_STARTED" and body["readable"] is False
        assert body["account"]["account_label"] == label and body["strategy"]["return_pct"] is None
    assert auth_client.get("/evaluation").json()["scope"] == "demo"          # the study that exists
    quality = auth_client.get("/evaluation?scope=route_quality")
    assert quality.status_code == 200 and quality.json()["scope"] == "route_quality"
    assert isinstance(quality.json()["routes"], list)
    assert auth_client.get("/evaluation?scope=live").status_code == 422     # not a scope


def test_the_new_reads_answer_through_their_declared_models(auth_client, monkeypatch, sandbox_service):
    """Every endpoint the upgrade added declares a response model with extra="forbid": a field the
    service grew and the model did not is a 500 here, before a release, not a value that silently
    never reaches the screen. The sandbox is the degraded world -- no venue, no study, no snapshot
    -- which is the shape those models had to fit first."""
    monkeypatch.setattr("agentic_trading.api.app._service", sandbox_service)
    monkeypatch.setattr(sandbox_service, "_broker_connect_error", lambda: None)
    monkeypatch.setattr(ResearchJobManager, "dispatch", lambda self: [])
    runs = sandbox_service.profiles().project_dir / "runs"
    (runs / "old_run").mkdir(parents=True)
    (runs / "old_run" / "decision_record.json").write_text(json.dumps({
        "run_id": "old_run", "ticker": "VTI", "trade_date": "2026-09-10",
        "policy": {"final_action": "NO_TRADE"}, "final_recommendation": {"action": "NO_TRADE"}}))

    started = auth_client.post("/research", json={"ticker": "VTI", "bucket": "core", "scope": "demo",
                                                  "purpose": "candidate",
                                                  "request_id": "6f1d3c2e-2b4a-4f0e-9c1d-0a1b2c3d4e5f"})
    assert started.status_code == 200, started.text
    assert started.json()["scope"] == "demo" and started.json()["deduplicated"] is False
    for path, key in (("/config/authority", "version"), ("/attention", "readable"),
                      ("/accounts", "accounts"), ("/research/jobs", None), ("/research", None),
                      ("/research?scope=unspecified", None), ("/evaluation?scope=demo", "state"),
                      ("/evaluation?scope=agentic", "state"), ("/evaluation?scope=route_quality", "routes")):
        response = auth_client.get(path)
        assert response.status_code == 200, f"{path}: {response.text}"
        body = response.json()
        if key:
            assert key in body, path
    listed = auth_client.get("/research/jobs").json()
    assert [j["run_key"] for j in listed] == [started.json()["run_key"]] and listed[0]["cancellable"]
    old = auth_client.get("/research?scope=unspecified").json()
    assert [r["run_id"] for r in old] == ["old_run"] and old[0]["route"]["inferred"] is True
    assert auth_client.get("/research?scope=demo").json() == []
    schema = auth_client.get("/openapi.json").json()["components"]["schemas"]
    for model in ("Authority", "Attention", "AccountScopes", "ResearchJob", "ResearchRecord",
                  "Evaluation", "RouteQuality", "ExitContinuation"):
        assert model in schema and schema[model].get("additionalProperties") is False, model


def test_openapi_schema_is_published_for_frontend_generation(auth_client):
    client = auth_client
    schema = client.get("/openapi.json").json()
    paths = set(schema["paths"])
    for required in ("/health", "/portfolio", "/risk", "/micro-live", "/proposals",
                     "/proposals/{proposal_id}", "/proposals/{proposal_id}/approve",
                     "/proposals/{proposal_id}/continue_exit", "/research",
                     "/research/jobs", "/orders"):
        assert required in paths, required
    approve = schema["paths"]["/proposals/{proposal_id}/approve"]["post"]
    assert "current conditions" in approve["summary"]
    # Continuing an exit is a write that creates a proposal and nothing else. The request model
    # says so: a confirmation, an idempotent id, and the authority the screen showed.
    onward = schema["paths"]["/proposals/{proposal_id}/continue_exit"]["post"]
    assert "for a person" in onward["summary"]
    assert set(schema["components"]["schemas"]["ContinueExitRequest"]["properties"]) == {
        "confirm", "request_id", "authority_version", "acknowledge_authority_change"}

    # A published path is not a generatable document. A route that returns dict[str, Any]
    # publishes `additionalProperties: true`, and everything generated from it is `any`, which
    # is how this schema stood when the test was first written. Each route that declares a
    # response model belongs here, so the claim in this test's name stays true as they land.
    for route, model, named in (
            ("/micro-live", "MicroLive", {"status", "preflight_ready", "checks", "blocking",
                                          "holdings", "holdings_error"}),
            ("/portfolio", "Portfolio", {"as_of", "buckets", "accounts", "errors", "error_codes",
                                         "combined"}),
            ("/proposals/{proposal_id}", "ProposalDetail",
             {"proposal_id", "version", "status", "freshness", "research", "hard_risk"}),
            ("/orders", "Orders", {"fills", "tracked_orders"}),
            ("/health", "Health", {"status", "detail", "checked_at", "market", "claude"}),
            ("/system/market", "MarketStatus", {"is_open", "calendar", "error"}),
            ("/system/claude", "ClaudeStatus", {"state", "billing", "calls_today"}),
            ("/advisor", "AdvisorState",
             {"snapshot", "stale", "notes", "jobs", "conversations", "decisions", "coverage"}),
            ("/advisor/conversations/{conversation_id}", "AdvisorConversation",
             {"conversation_id", "messages"})):
        body = schema["paths"][route]["get"]["responses"]["200"]["content"]["application/json"]
        assert body["schema"] == {"$ref": f"#/components/schemas/{model}"}, route
        assert named <= set(schema["components"]["schemas"][model]["properties"]), route

    listing = schema["paths"]["/proposals"]["get"]["responses"]["200"]["content"]["application/json"]
    assert listing["schema"]["items"] == {"$ref": "#/components/schemas/ProposalSummary"}


def test_pausing_stops_orders_while_research_keeps_running(sandbox_service, monkeypatch):
    """One answer for the whole system: the kill switch is about orders, not about analysis.

    The trigger used to decline while paused, which contradicted both the API's own description
    of the kill switch and the operations guide. It now runs, and only a status that makes
    research impossible stops it.
    """
    from agentic_trading.triggers import scheduled_review

    # The single review always has a universe to study. The deployment's own schedule, which the
    # sandbox copies, can name nothing to study today, and that would say nothing about pausing.
    path = sandbox_service.profiles().config_dir / "risk_profile.yaml"
    doc = yaml.safe_load(path.read_text())
    doc["research_schedule"]["accounts"] = {}
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    started = []
    monkeypatch.setattr(sandbox_service, "start_research",
                        lambda ticker, **kw: started.append(ticker) or {"run_key": ticker})
    sandbox_service.pause("test")
    assert sandbox_service.health().status == "PAUSED"

    result = scheduled_review(sandbox_service)
    assert result["skipped"] is False and started

    profiles = sandbox_service.profiles()
    monkeypatch.setattr(sandbox_service, "profiles", lambda: profiles)
    profiles.risk["research_schedule"]["run_while_paused"] = False
    withheld = scheduled_review(sandbox_service, force=True)
    assert withheld["skipped"] is True and "paused" in withheld["reason"]


def test_freshness_is_exposed_with_the_numbers_a_screen_needs(sandbox_service):
    profiles = sandbox_service.profiles()
    profiles.risk["broker"]["state_file"] = "state/proposals.json"
    store = ProposalStore(profiles.project_dir / "state" / "proposals.json")
    momentum = _proposal(status=PROPOSED, strategy="momentum", bucket="sandbox")
    momentum.created_at = (utcnow() - timedelta(minutes=40)).isoformat()
    store.save(momentum)

    row = sandbox_service.proposals()[0]
    assert row.freshness.freshness_class == "momentum"   # serialised as "class"; see ProposalFreshness
    assert row.freshness.ttl_minutes == 90
    assert row.freshness.state == "STALE"                # past the 30 minute mark, inside the 90 minute life
    assert row.freshness.max_price_drift_pct == 1.5


def test_accounts_are_reported_separately_and_never_summed_into_one_drawdown(sandbox_service):
    """The paper book and the funded account keep separate equity and high-water marks."""
    portfolio = sandbox_service.portfolio()
    conceptual, micro = portfolio.accounts["conceptual"], portfolio.accounts["micro_live"]

    assert conceptual.buckets == ["core", "sandbox"] and micro.buckets == ["micro_live"]
    assert conceptual.is_real_money is False and micro.is_real_money is True
    assert conceptual.equity_usd == sum(
        portfolio.buckets[b].equity_usd for b in ("core", "sandbox"))
    # each account's equity belongs to its own ladder, and neither to the other's
    assert conceptual.high_water_mark_usd != micro.high_water_mark_usd
    assert portfolio.combined.mixes_real_and_conceptual is True
    assert portfolio.combined.equity_usd == round(
        conceptual.equity_usd + micro.equity_usd, 2)
    for bucket, expected in (("core", "conceptual"), ("micro_live", "micro_live")):
        assert portfolio.buckets[bucket].account == expected


def test_a_deposit_is_funding_and_never_a_return(sandbox_service):
    """Paying money in must move equity and funding by the same amount, leaving result alone."""
    from agentic_trading.portfolio import record_cash_flow

    ledger = sandbox_service.profiles().project_dir / "state" / "portfolio.yaml"
    before = sandbox_service.portfolio().accounts["conceptual"]

    record_cash_flow(ledger, bucket="core", amount_usd=250.0, kind="deposit", date="2026-09-03")
    after = sandbox_service.portfolio().accounts["conceptual"]

    assert after.equity_usd == before.equity_usd + 250.0
    assert after.net_deposits_usd == before.net_deposits_usd + 250.0
    assert after.recorded_flows_usd == 250.0
    assert after.opening_allocation_usd == before.opening_allocation_usd
    assert after.investment_pnl_usd == before.investment_pnl_usd == 0.0
    assert after.return_pct == 0.0

    record_cash_flow(ledger, bucket="core", amount_usd=100.0, kind="withdrawal", date="2026-09-03")
    out = sandbox_service.portfolio().accounts["conceptual"]
    assert out.recorded_flows_usd == 150.0
    assert out.investment_pnl_usd == 0.0

    # Interest, a dividend or a fee moves cash the same way, but it is the account's return,
    # not its funding: equity moves, net deposits do not, and the result shows it.
    record_cash_flow(ledger, bucket="core", amount_usd=3.0, kind="income", date="2026-09-03")
    record_cash_flow(ledger, bucket="core", amount_usd=1.0, kind="fee", date="2026-09-03")
    earned = sandbox_service.portfolio().accounts["conceptual"]
    assert earned.equity_usd == out.equity_usd + 2.0
    assert earned.recorded_flows_usd == 150.0 and earned.net_deposits_usd == out.net_deposits_usd
    assert earned.investment_pnl_usd == 2.0
    with pytest.raises(ValueError, match="kind must be"):
        record_cash_flow(ledger, bucket="core", amount_usd=1.0, kind="gift")


def test_portfolio_says_when_the_prices_are_behind_the_ledger(sandbox_service, monkeypatch):
    """The failure of 2026-09-23: a ledger confirmed that morning, priced for a session two days
    back because the vendor's bars for the one between had no close. Confirming it again would
    stamp the same date, so the code tells the console not to offer it."""
    monkeypatch.setattr("agentic_trading.service.books._last_session", lambda: "2000-01-03")
    portfolio = sandbox_service.portfolio()
    assert portfolio.error_codes == {"conceptual": "ledger_ahead", "micro_live": "ledger_ahead"}
    assert "after the trade date" in portfolio.errors["micro_live"]
    assert portfolio.buckets == {} and portfolio.accounts == {}


def test_portfolio_reports_the_book_it_read_and_names_the_one_it_could_not(
        auth_client, monkeypatch, sandbox_service):
    """One unreadable ledger must not blank the screen that shows the other.

    Both pages that render this iterate `buckets` without a guard of their own, so on a partial
    read the contract is a short map plus a reason in `errors` — never a missing key, and never
    a zero standing in for money nobody could count.
    """
    (sandbox_service.profiles().project_dir / "state" / "micro_live_portfolio.yaml").unlink()
    monkeypatch.setattr("agentic_trading.api.app._service", sandbox_service)

    result = auth_client.get("/portfolio")
    assert result.status_code == 200, result.text
    body = result.json()

    assert "not found" in body["errors"]["micro_live"]
    assert body["error_codes"] == {"micro_live": "unreadable"}
    assert set(body["buckets"]) == {"core", "sandbox"}        # the readable book still reports
    assert set(body["accounts"]) == {"conceptual"}
    assert body["combined"]["complete"] is False
    # The total is now one book's, and the flag says so rather than implying it spans both.
    assert body["combined"]["mixes_real_and_conceptual"] is False
    assert body["combined"]["equity_usd"] == body["accounts"]["conceptual"]["equity_usd"]

    # Nothing is dropped on the way out, at any level of the shape.
    assert set(body) == set(Portfolio.model_fields)
    assert set(body["accounts"]["conceptual"]) == set(PortfolioAccount.model_fields)
    assert set(body["buckets"]["core"]) == set(PortfolioBucket.model_fields)


def test_a_holding_reaches_the_screen_with_its_classification_intact(sandbox_service, monkeypatch):
    """The position mapping, against a real priced snapshot.

    The example ledgers are all cash and pricing a holding reaches for market data, so the
    sandbox on its own never builds a single PortfolioPosition — the one shape in this response
    that no other test would touch. These are the scenario fixtures the risk engine is validated
    against, priced offline by a lookup, pushed through the mapping the console actually reads.
    """
    from agentic_trading.portfolio import build_snapshot
    from agentic_trading.risk_scenarios import (
        TRADE_DATE,
        identity_lookup,
        make_ladders,
        make_state,
        price_lookup,
        regime,
    )

    profiles = sandbox_service.profiles()
    state = make_state(positions=[{"symbol": "VTI", "bucket": "core", "quantity": 2,
                                   "cost_basis_per_share": 300.0}])
    snap = build_snapshot(state, profiles, TRADE_DATE, price_lookup, identity_lookup)
    ladders = make_ladders(3000.0, 2500.0, 500.0, profiles)
    monkeypatch.setattr("agentic_trading.cli.portfolio_context",
                        lambda *args, **kwargs: (snap, ladders, regime(), ""))

    core = sandbox_service.portfolio().buckets["core"]
    assert core.position_count == 1
    held, view = core.positions[0], snap.buckets["core"].positions[0]

    assert held.symbol == "VTI" and held.quantity == 2.0
    assert held.classification == view.cls.limit_class      # the class a limit is written against
    assert held.exposure_group == view.cls.exposure_group   # None outside the universe, not ""
    assert held.cost_basis_usd == 600.0 and held.market_value_usd > held.cost_basis_usd
    assert set(held.model_dump()) == set(PortfolioPosition.model_fields)


# ----------------------------------------------------- the research-room console
def test_research_roster_is_the_one_copy_of_the_team(sandbox_service):
    """The console draws desks from this, so it must answer from config, not a second list."""
    roster = sandbox_service.research_roster()
    keys = [role["key"] for role in roster["roles"]]

    assert len(keys) == 12 and len(set(keys)) == 12
    # The portfolio manager writes decision.md; its desk is still called "portfolio".
    assert {"key": "portfolio", "group": "portfolio", "node": "Portfolio Manager",
            "report": "decision", "analyst": None} in roster["roles"]

    # The stall threshold the console shows comes from config, not from a guess in the UI.
    assert roster["call_timeout_seconds"] == sandbox_service.profiles().llm.timeout_seconds

    analysts = roster["analysts"]
    assert analysts["stock"] == sandbox_service.profiles().analysts_for("stock")
    assert "fundamentals" not in analysts["etf"], "an ETF has no statements to analyse"
    assert "fundamentals" not in analysts["crypto"]
    # Every selectable analyst has a desk to sit at.
    for kind in ("etf", "stock", "crypto"):
        assert set(analysts[kind]) <= {r["analyst"] for r in roster["roles"] if r["analyst"]}


def test_research_progress_reports_only_what_the_run_wrote(sandbox_service, tmp_path):
    """Two desk states, both facts: a call is open, or the last one returned.

    Idle, skipped and stalled are judgements about the roster and the clock, and are the
    caller's to make. Inventing them here would put a desk state in the record that no
    event supports.
    """
    run_dir = tmp_path / "runs" / "2026-09-10_VTI_abc"
    run_dir.mkdir(parents=True)
    (run_dir / "progress.jsonl").write_text("\n".join([
        json.dumps({"t": "2026-09-10T14:00:00-0700", "kind": "run_started", "ticker": "VTI",
                    "analysts": ["market", "news"], "instrument_type": "etf"}),
        json.dumps({"t": "2026-09-10T14:00:01-0700", "kind": "call_started",
                    "node": "Market Analyst", "role": "market"}),
        json.dumps({"t": "2026-09-10T14:00:29-0700", "kind": "call_finished",
                    "node": "Market Analyst", "role": "market", "wall_seconds": 28.0,
                    "tools": ["get_indicators"]}),
        json.dumps({"t": "2026-09-10T14:00:30-0700", "kind": "call_started",
                    "node": "Bull Researcher", "role": "bull"}),
    ]) + "\n")

    progress = sandbox_service._research_progress(run_dir)

    assert progress["analysts"] == ["market", "news"]
    assert progress["desks"]["market"] == {
        "calls": 1, "wall_seconds": 28.0, "tools": ["get_indicators"],
        "last_at": "2026-09-10T14:00:29-0700", "state": "done"}
    assert progress["desks"]["bull"]["state"] == "working"
    assert "bear" not in progress["desks"], "a desk with no event has no reported state"
    assert progress["result"] is None  # the run has not reached the deterministic gate
    assert progress["last_event_at"] == "2026-09-10T14:00:30-0700"


def test_research_progress_tolerates_a_line_still_being_written(sandbox_service, tmp_path):
    """The worker appends while the console polls, so the last line can be half a line."""
    run_dir = tmp_path / "runs" / "2026-09-10_VTI_def"
    run_dir.mkdir(parents=True)
    (run_dir / "progress.jsonl").write_text(
        json.dumps({"t": "2026-09-10T14:00:01-0700", "kind": "call_started", "role": "market"})
        + '\n{"t": "2026-09-10T14:00:29-0700", "kind": "call_fin')

    progress = sandbox_service._research_progress(run_dir)

    assert progress["desks"]["market"]["state"] == "working"
    assert len(progress["events"]) == 1


def test_research_progress_is_absent_rather_than_empty_before_a_run_writes(sandbox_service, tmp_path):
    assert sandbox_service._research_progress(tmp_path / "runs" / "nothing-here") is None


def test_roster_route_is_matched_before_the_run_key_route(auth_client):
    """/research/roster must not be read as a run whose key happens to be "roster"."""
    schema = auth_client.get("/openapi.json").json()
    paths = list(schema["paths"])
    assert "/research/roster" in paths
    assert paths.index("/research/roster") < paths.index("/research/{run_key}")


def test_confirming_the_paper_book_renews_its_date_and_says_what_it_checked(sandbox_service):
    from agentic_trading.portfolio import load_portfolio

    report = sandbox_service.confirm_portfolio(ledger="conceptual")
    assert report["confirmed"] is True, report
    row = report["ledgers"][0]
    assert "no venue behind this book" in row["checked_against"]
    ledger = sandbox_service.profiles().project_dir / "state" / "portfolio.yaml"
    assert load_portfolio(ledger).as_of == report["date"] == row["as_of"]


def test_a_dry_run_confirmation_writes_nothing(sandbox_service):
    from agentic_trading.portfolio import load_portfolio

    ledger = sandbox_service.profiles().project_dir / "state" / "portfolio.yaml"
    before = load_portfolio(ledger).as_of
    report = sandbox_service.confirm_portfolio(ledger="conceptual", dry_run=True)
    assert report["confirmed"] is True and report["dry_run"] is True
    assert load_portfolio(ledger).as_of == before


def test_the_last_ledger_check_is_read_back_without_opening_the_venue(sandbox_service, monkeypatch):
    """The Portfolio screen shows the last check on every visit and offers its refusal
    figures for recording, so the report is read from the database, never by checking again."""
    assert sandbox_service.ledger_check() == {}                  # nothing has checked yet
    monkeypatch.setattr(sandbox_service, "_broker",
                        lambda *_args: pytest.fail("reading the last check must not open the venue"))
    sandbox_service.confirm_portfolio(ledger="conceptual")
    kept = sandbox_service.ledger_check()
    assert kept["confirmed"] is True and kept["ledgers"][0]["ledger"] == "conceptual" and kept["at"]


def test_confirm_route_needs_a_session(auth_client, monkeypatch, sandbox_service):
    from fastapi.testclient import TestClient

    from agentic_trading.api.app import app

    monkeypatch.setattr("agentic_trading.api.app._service", sandbox_service)
    result = auth_client.post("/portfolio/confirm", json={"ledger": "conceptual"})
    assert result.status_code == 200
    assert result.json()["confirmed"] is True
    kept = auth_client.get("/portfolio/ledgers")
    assert kept.status_code == 200 and kept.json()["confirmed"] is True

    anonymous = TestClient(app, base_url="https://testserver")
    anonymous.headers.update({"Origin": "https://testserver", "X-CT-Request": "1",
                              "Content-Type": "application/json"})
    assert anonymous.post("/portfolio/confirm", json={}).status_code in (401, 403)
    anonymous.close()


def test_the_bare_origin_lands_on_the_console_not_on_api_json(console_auth):
    """A phone is given the origin, not the mount path, and /ui is not something to type.

    The console is mounted under /ui, so the bare origin used to answer with the API's
    "login required" JSON. Anyone who added that page to a home screen got an icon pointing at
    the API root, which is also the one path iOS will never deliver push from.
    """
    from fastapi.testclient import TestClient

    from agentic_trading.api.app import app

    anonymous = TestClient(app, base_url="https://testserver", follow_redirects=False)
    landing = anonymous.get("/")
    assert landing.status_code == 307
    assert landing.headers["location"] == "/ui/"
    # Temporary on purpose: a permanent redirect outlives any decision to move the console.
    assert "max-age" not in landing.headers.get("Cache-Control", "")
    anonymous.close()


def test_the_bare_origin_still_arrives_at_a_login_when_signed_out(console_auth):
    """Following the redirect must reach the sign-in page, carrying where it was going."""
    from fastapi.testclient import TestClient

    from agentic_trading.api.app import app

    anonymous = TestClient(app, base_url="https://testserver")
    landing = anonymous.get("/")
    assert landing.status_code == 200
    assert landing.url.path == "/ui/login.html"
    anonymous.close()


def test_a_write_to_the_bare_origin_is_refused_rather_than_redirected(console_auth):
    """A redirect that preserved the method would replay a cross-site POST against the console."""
    from fastapi.testclient import TestClient

    from agentic_trading.api.app import app

    anonymous = TestClient(app, base_url="https://testserver", follow_redirects=False)
    assert anonymous.post("/", json={}).status_code in (401, 403)
    anonymous.close()


# ---------------------------------------------------------------- readings kept between polls
def test_the_regime_is_assessed_once_for_everything_that_polls_it(sandbox_service, monkeypatch):
    """/health, /risk, the notification pass and the schedule all read the market regime, and
    every assessment asks the data vendor: once a poll, from four places, every few seconds."""
    from agentic_trading import risk
    from agentic_trading.risk.regime import RegimeResult

    assessed = []
    monkeypatch.setattr(risk, "assess_regime", lambda profiles, day: assessed.append(day)
                        or RegimeResult("NORMAL", True, "SPY above its 200-day average"))
    for _ in range(3):
        sandbox_service.health()
        sandbox_service.risk_state()
    regime = sandbox_service.risk_state()["regime"]
    assert len(assessed) == 1
    assert regime["level"] == "NORMAL" and regime["checked_at"]

    # A new session is a new assessment, at once.
    monkeypatch.setattr("agentic_trading.service.base._last_session", lambda: "2031-01-02")
    assert sandbox_service.risk_state()["regime"]["checked_at"]
    assert assessed == [assessed[0], "2031-01-02"]


def test_the_console_portfolio_is_kept_until_something_it_was_read_from_changes(
        sandbox_service, monkeypatch):
    """/portfolio priced every holding, looked each one up and rewrote the drawdown ladders on
    every poll. It is read once and kept -- and read again the moment a ledger is written, the
    configuration is edited or the session rolls over, never answered from before that."""
    import os

    from agentic_trading import cli, risk
    from agentic_trading.portfolio import load_portfolio, migrate_portfolio
    from agentic_trading.risk.regime import RegimeResult

    # The sandbox simulates prices, but the regime the portfolio carries asks the data vendor for
    # SPY, which the offline suite refuses (tests/conftest.py).
    monkeypatch.setattr(risk, "assess_regime", lambda profiles, day: RegimeResult(
        "NORMAL", True, "SPY above its 200-day average"))

    books = []
    original = cli.portfolio_context
    monkeypatch.setattr(cli, "portfolio_context",
                        lambda *args, **kwargs: books.append(args[2]) or original(*args, **kwargs))
    assert sandbox_service.refresh_kept_readings()["portfolio"] is False   # nobody is reading it

    first = sandbox_service.kept_portfolio()
    assert first.checked_at and len(books) == 2                    # both books, once
    assert sandbox_service.kept_portfolio().checked_at == first.checked_at
    assert len(books) == 2

    profiles = sandbox_service.profiles()
    ledger = profiles.project_dir / "state" / "portfolio.yaml"
    migrate_portfolio(ledger)                                      # a ledger written to the database
    sandbox_service.kept_portfolio()
    assert len(books) == 4
    state = load_portfolio(ledger)
    StateDB(ledger).write_document(ledger, state.model_dump(mode="json"))
    sandbox_service.kept_portfolio()
    assert len(books) == 6

    config = profiles.config_dir / "risk_profile.yaml"
    stamp = config.stat().st_mtime_ns + 1_000_000_000
    os.utime(config, ns=(stamp, stamp))                           # the configuration edited
    sandbox_service.kept_portfolio()
    assert len(books) == 8

    monkeypatch.setattr("agentic_trading.service.base._last_session", lambda: "2031-01-02")
    sandbox_service.kept_portfolio()                               # the session rolled over
    assert len(books) == 10


def test_get_portfolio_is_answered_from_the_kept_reading(auth_client, sandbox_service, monkeypatch):
    monkeypatch.setattr("agentic_trading.api.app._service", sandbox_service)
    computed = []
    original = sandbox_service.portfolio
    monkeypatch.setattr(sandbox_service, "portfolio", lambda: computed.append(1) or original())
    bodies = [auth_client.get("/portfolio").json() for _ in range(3)]
    assert len(computed) == 1
    assert bodies[0]["checked_at"]
    assert all(body["checked_at"] == bodies[0]["checked_at"] for body in bodies)


def test_continuing_an_exit_over_http(auth_client, monkeypatch, sandbox_service):
    """One real round trip for the continuation endpoint, request model to response model.

    The service tests cover the decision and the frontend's own cases cover the screen; what
    only a call can check is the seam between them -- that the body the console sends validates,
    and that every field it reads comes back over the wire.
    """
    from agentic_trading import autonomy

    monkeypatch.setattr("agentic_trading.api.app._service", sandbox_service)
    profiles = sandbox_service.profiles()
    profiles.risk["broker"]["state_file"] = "state/proposals.json"
    store = ProposalStore(profiles.project_dir / "state" / "proposals.json")
    version = autonomy.note_version(profiles, store.db)

    # The book has to hold what the exit is about: the example ledger carries cash, not VTI.
    ledger = profiles.project_dir / "state" / "portfolio.yaml"
    book = yaml.safe_load(ledger.read_text(encoding="utf-8"))
    book["positions"] = [{"symbol": "VTI", "bucket": "core", "quantity": 0.2,
                          "cost_basis_per_share": 370.0}]
    ledger.write_text(yaml.safe_dump(book), encoding="utf-8")

    # An exit that reached the venue, sold part of what it was authorised for, and stopped.
    p = _proposal(status=EXECUTED, action="REDUCE", ticker="VTI", bucket="core",
                  fingerprint=positions_fingerprint({"VTI": 0.2}))
    p.sell_quantity, p.filled_quantity = 0.2, 0.12
    p.client_order_id = "co-over-http"
    p.reconciled_at = utcnow().isoformat()
    p.authority_version = version
    store.save(p)

    detail = auth_client.get(f"/proposals/{p.proposal_id}").json()
    assert detail["exit"]["continuable"]["eligible"] is True, detail["exit"]
    assert detail["exit"]["autonomous"] is not None       # REDUCE carries the floor block

    body = {"confirm": True, "request_id": str(uuid4()), "authority_version": version,
            "acknowledge_authority_change": False}
    made = auth_client.post(f"/proposals/{p.proposal_id}/continue_exit", json=body)
    assert made.status_code == 200, made.text
    answer = made.json()
    assert set(answer) == {"deduplicated", "continuation_of", "root_proposal_id",
                           "authorised_quantity", "research_anchor_at", "continued_at",
                           "proposal"}
    assert answer["deduplicated"] is False
    assert answer["continuation_of"] == answer["root_proposal_id"] == p.proposal_id
    assert answer["authorised_quantity"] == pytest.approx(0.08)
    assert answer["proposal"]["status"] == "WAITING_FOR_APPROVAL"

    # The same body again is the same continuation, not a second branch.
    again = auth_client.post(f"/proposals/{p.proposal_id}/continue_exit", json=body)
    assert again.status_code == 200 and again.json()["deduplicated"] is True
    assert again.json()["proposal"]["proposal_id"] == answer["proposal"]["proposal_id"]

    # An authority the screen no longer holds is refused like a moved proposal version.
    stale = auth_client.post(f"/proposals/{p.proposal_id}/continue_exit",
                             json={**body, "request_id": str(uuid4()),
                                   "authority_version": "a" * 12})
    assert stale.status_code == 409, stale.text
    assert auth_client.post(f"/proposals/{p.proposal_id}/continue_exit",
                            json={**body, "confirm": False}).status_code == 400
    assert auth_client.post(f"/proposals/{p.proposal_id}/continue_exit",
                            json={"confirm": True}).status_code == 422, "request_id is required"

    # And the to-do that asked for it is gone, because a continuation now exists.
    items = [i for i in auth_client.get("/attention").json()["items"]
             if i["kind"] == "exit_incomplete"]
    assert items == []


def test_activity_states_reach_the_wire(auth_client, monkeypatch, sandbox_service):
    """The console reads `state`, so it has to arrive with the dates behind it."""
    from agentic_trading import operations

    monkeypatch.setattr("agentic_trading.api.app._service", sandbox_service)
    db = sandbox_service.state_db()
    operations.declare(db, origin=(utcnow() - timedelta(hours=1)).isoformat())

    body = auth_client.get("/system/operations").json()
    rows = {row["id"]: row for row in body["activities"]}
    assert set(rows) == set(operations.ACTIVITIES)
    for row in rows.values():
        assert set(row) == {"id", "last_success_at", "age_minutes", "expected_every_minutes",
                            "overdue", "detail", "state", "expected_since", "due_by",
                            "last_attempt_at", "last_failure"}
    # Declared an hour ago, due in a day: not a to-do, whatever the legacy boolean says.
    awaiting = rows["ledger_confirm"]
    assert awaiting["state"] == "awaiting_first_run"
    assert awaiting["due_by"] > utcnow().isoformat() and awaiting["overdue"] is True
    assert "backup_overdue" not in [i["kind"] for i in auth_client.get("/attention").json()["items"]
                                    if i["subject"] == "ledger_confirm"]
