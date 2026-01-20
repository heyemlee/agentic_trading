"""Application interface: product status mapping, service operations, security boundaries.

Offline. No broker session, no model call, no writes to the real state directory.
"""

from __future__ import annotations

import json
import re
import shutil
from datetime import timedelta

import pytest

from agentic_trading.execution_scenarios import _proposal
from agentic_trading.proposals import (
    APPROVED,
    EXECUTED,
    INVALIDATED,
    NEEDS_REANALYSIS,
    PROPOSED,
    ProposalStore,
    utcnow,
)
from agentic_trading.service import Service, ServiceError
from agentic_trading.service.status import (
    MicroLiveStatus,
    ProposalStatus,
    SystemStatus,
    proposal_status,
)
from tests.conftest import ROOT


@pytest.fixture
def sandbox_service(tmp_path):
    """A Service whose every state path points at a temp directory."""
    cfg = tmp_path / "config"
    shutil.copytree(ROOT / "config", cfg)
    state = tmp_path / "state"
    state.mkdir()
    shutil.copy(ROOT / "config" / "examples" / "portfolio.example.yaml", state / "portfolio.yaml")
    shutil.copy(ROOT / "config" / "examples" / "micro_live_portfolio.example.yaml", state / "micro_live_portfolio.yaml")
    (tmp_path / "runs").mkdir()
    disarm(cfg / "risk_profile.yaml")
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


def test_health_reports_a_product_status(sandbox_service):
    health = sandbox_service.health()
    assert health["status"] in set(SystemStatus)
    assert isinstance(health["warnings"], list)
    assert health["micro_live_enabled"] is False        # the sandbox is always disarmed
    assert "claude" in health and health["claude"]["billing"] == "claude_subscription_no_api_charges"


def test_health_never_exposes_credentials(sandbox_service):
    blob = json.dumps(sandbox_service.health(), default=str).lower()
    for secret in ("access_token", "refresh_token", "client_secret", "api_key", "bearer"):
        assert secret not in blob


def test_pause_and_resume_move_the_kill_switch(sandbox_service):
    assert sandbox_service.health()["paused"] is False
    sandbox_service.pause("test")
    assert sandbox_service.health()["paused"] is True
    assert sandbox_service.health()["status"] == SystemStatus.PAUSED
    sandbox_service.resume()
    assert sandbox_service.health()["paused"] is False


def test_portfolio_exposes_every_bucket_and_marks_real_money(sandbox_service):
    portfolio = sandbox_service.portfolio()
    assert set(portfolio["buckets"]) == {"core", "sandbox", "micro_live"}
    assert portfolio["buckets"]["micro_live"]["is_real_money"] is True
    assert portfolio["buckets"]["micro_live"]["venue"] == "robinhood"
    assert portfolio["buckets"]["core"]["is_real_money"] is False


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


def test_proposal_listing_and_detail(sandbox_service):
    profiles = sandbox_service.profiles()
    store = ProposalStore(profiles.project_dir / "state" / "proposals.json")
    profiles.risk["broker"]["state_file"] = "state/proposals.json"
    p = _proposal(status=PROPOSED)
    store.save(p)

    listed = sandbox_service.proposals()
    assert len(listed) == 1
    row = listed[0]
    assert row["status"] == ProposalStatus.WAITING_FOR_APPROVAL
    assert row["requires_human_approval"] is True
    assert row["freshness"]["state"] == "VALID" and row["freshness"]["ttl_minutes"] == 2880

    detail = sandbox_service.proposal(p.proposal_id)
    assert detail["world_at_proposal"]["ladder_level"] == "NORMAL"
    assert "history" in detail


def test_approving_an_unknown_proposal_is_a_clean_error(sandbox_service):
    with pytest.raises(ServiceError, match="no proposal"):
        sandbox_service.approve("does-not-exist")


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


def test_openapi_schema_is_published_for_frontend_generation(auth_client):
    client = auth_client
    schema = client.get("/openapi.json").json()
    paths = set(schema["paths"])
    for required in ("/health", "/portfolio", "/risk", "/micro-live", "/proposals",
                     "/proposals/{proposal_id}", "/proposals/{proposal_id}/approve", "/research",
                     "/research/jobs", "/orders"):
        assert required in paths, required
    approve = schema["paths"]["/proposals/{proposal_id}/approve"]["post"]
    assert "current conditions" in approve["summary"]


def test_pausing_stops_orders_while_research_keeps_running(sandbox_service, monkeypatch):
    """One answer for the whole system: the kill switch is about orders, not about analysis.

    The trigger used to decline while paused, which contradicted both the API's own description
    of the kill switch and the operations guide. It now runs, and only a status that makes
    research impossible stops it.
    """
    from agentic_trading.triggers import scheduled_review

    started = []
    monkeypatch.setattr(sandbox_service, "start_research",
                        lambda ticker, **kw: started.append(ticker) or {"run_key": ticker})
    sandbox_service.pause("test")
    assert sandbox_service.health()["status"] == "PAUSED"

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
    assert row["freshness"]["class"] == "momentum"
    assert row["freshness"]["ttl_minutes"] == 90
    assert row["freshness"]["state"] == "STALE"          # past the 30 minute mark, inside the 90 minute life
    assert row["freshness"]["max_price_drift_pct"] == 1.5


def test_accounts_are_reported_separately_and_never_summed_into_one_drawdown(sandbox_service):
    """The paper book and the funded account keep separate equity and high-water marks."""
    portfolio = sandbox_service.portfolio()
    conceptual, micro = portfolio["accounts"]["conceptual"], portfolio["accounts"]["micro_live"]

    assert conceptual["buckets"] == ["core", "sandbox"] and micro["buckets"] == ["micro_live"]
    assert conceptual["is_real_money"] is False and micro["is_real_money"] is True
    assert conceptual["equity_usd"] == sum(
        portfolio["buckets"][b]["equity_usd"] for b in ("core", "sandbox"))
    # each account's equity belongs to its own ladder, and neither to the other's
    assert conceptual["high_water_mark_usd"] != micro["high_water_mark_usd"]
    assert portfolio["combined"]["mixes_real_and_conceptual"] is True
    assert portfolio["combined"]["equity_usd"] == round(
        conceptual["equity_usd"] + micro["equity_usd"], 2)
    for bucket, expected in (("core", "conceptual"), ("micro_live", "micro_live")):
        assert portfolio["buckets"][bucket]["account"] == expected


def test_a_deposit_is_funding_and_never_a_return(sandbox_service):
    """Paying money in must move equity and funding by the same amount, leaving result alone."""
    from agentic_trading.portfolio import record_cash_flow

    ledger = sandbox_service.profiles().project_dir / "state" / "portfolio.yaml"
    before = sandbox_service.portfolio()["accounts"]["conceptual"]

    record_cash_flow(ledger, bucket="core", amount_usd=250.0, kind="deposit", date="2026-09-03")
    after = sandbox_service.portfolio()["accounts"]["conceptual"]

    assert after["equity_usd"] == before["equity_usd"] + 250.0
    assert after["net_deposits_usd"] == before["net_deposits_usd"] + 250.0
    assert after["recorded_flows_usd"] == 250.0
    assert after["opening_allocation_usd"] == before["opening_allocation_usd"]
    assert after["investment_pnl_usd"] == before["investment_pnl_usd"] == 0.0
    assert after["return_pct"] == 0.0

    record_cash_flow(ledger, bucket="core", amount_usd=100.0, kind="withdrawal", date="2026-09-03")
    out = sandbox_service.portfolio()["accounts"]["conceptual"]
    assert out["recorded_flows_usd"] == 150.0
    assert out["investment_pnl_usd"] == 0.0
