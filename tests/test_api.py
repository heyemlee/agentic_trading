"""Application interface: product status mapping, service operations, security boundaries.

Offline. No broker session, no model call, no writes to the real state directory.
"""

from __future__ import annotations

import json
import shutil
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from conservative_trading.api.app import app
from conservative_trading.api.service import Service, ServiceError
from conservative_trading.api.status import (
    MicroLiveStatus,
    ProposalStatus,
    SystemStatus,
    proposal_status,
)
from conservative_trading.proposals import (
    APPROVED,
    EXECUTED,
    INVALIDATED,
    NEEDS_REANALYSIS,
    PROPOSED,
    ProposalStore,
    utcnow,
)
from conservative_trading.stage3_scenarios import _proposal
from tests.conftest import ROOT


@pytest.fixture
def sandbox_service(tmp_path):
    """A Service whose every state path points at a temp directory."""
    cfg = tmp_path / "config"
    shutil.copytree(ROOT / "config", cfg)
    shutil.copy(ROOT / "MANDATE.md", tmp_path / "MANDATE.md")
    state = tmp_path / "state"
    state.mkdir()
    shutil.copy(ROOT / "state" / "portfolio.yaml", state / "portfolio.yaml")
    shutil.copy(ROOT / "state" / "micro_live_portfolio.yaml", state / "micro_live_portfolio.yaml")
    (tmp_path / "runs").mkdir()
    return Service(cfg)


def test_health_reports_a_product_status(sandbox_service):
    health = sandbox_service.health()
    assert health["status"] in set(SystemStatus)
    assert isinstance(health["warnings"], list)
    assert health["micro_live_enabled"] is False
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
    from conservative_trading.api.status import micro_live_status

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
    with pytest.raises(ServiceError, match="disabled"):
        sandbox_service._broker(profiles, "micro_live")


def test_openapi_schema_is_published_for_frontend_generation():
    client = TestClient(app)
    schema = client.get("/openapi.json").json()
    paths = set(schema["paths"])
    for required in ("/health", "/portfolio", "/risk", "/micro-live", "/proposals",
                     "/proposals/{proposal_id}", "/proposals/{proposal_id}/approve", "/research", "/orders"):
        assert required in paths, required
    approve = schema["paths"]["/proposals/{proposal_id}/approve"]["post"]
    assert "current conditions" in approve["summary"]


def test_scheduled_trigger_refuses_to_run_while_paused(sandbox_service):
    from conservative_trading.triggers import scheduled_review

    sandbox_service.pause("test")
    result = scheduled_review(sandbox_service)
    assert result["skipped"] is True and "PAUSED" in result["reason"]
    assert result["started"] == []


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
