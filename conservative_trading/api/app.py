"""HTTP layer over :mod:`conservative_trading.api.service`.

Thin by design: each route calls one service function and returns its dict. FastAPI is used for
one reason beyond routing, which is that it publishes an OpenAPI schema at ``/openapi.json``, so
a frontend can be generated against a machine-readable contract rather than prose.

Security boundary: this process holds the Robinhood token and the Claude session. Neither ever
crosses the wire. The API exposes product operations only, and every write path runs the same
engine functions the CLI does, so no client can bypass Hard Risk, human approval,
execution-time revalidation, the Micro-Live capital boundary or the venue's own review.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .service import Service, ServiceError

logger = logging.getLogger(__name__)

app = FastAPI(
    title="Conservative Trading Backend",
    version="0.1.0",
    summary="Product interface for a conservative, human-approved investment system.",
    description=(
        "Research runs on a Claude subscription, a deterministic Hard Risk Layer decides what is "
        "permitted, and the investor approves anything that touches money. Approval authorises an "
        "idea, never a stored order: the world is re-read and re-checked before anything is sent.\n\n"
        "Real-money execution is confined to a $100 Micro-Live account at Robinhood and is disabled "
        "by default."
    ),
)
_service = Service("config")


def _ok(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except ServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("unhandled error in %s", getattr(fn, "__name__", fn))
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc


# ----------------------------------------------------------------- system
@app.get("/health", tags=["system"], summary="Can the system act, and what is blocking it")
def health() -> dict[str, Any]:
    return _ok(_service.health)


@app.get("/system/claude", tags=["system"], summary="Claude subscription runtime state")
def claude_status() -> dict[str, Any]:
    return _ok(_service.claude_status)


@app.get("/system/market", tags=["system"], summary="Market session from the exchange calendar")
def market_status() -> dict[str, Any]:
    return _ok(_service.market_status)


class PauseRequest(BaseModel):
    reason: str = Field("paused from the interface", description="Recorded in the halt file and the audit trail.")


@app.post("/system/pause", tags=["system"], summary="Engage the kill switch: no orders may be submitted")
def pause(body: PauseRequest | None = None) -> dict[str, Any]:
    return _ok(_service.pause, (body or PauseRequest()).reason)


@app.post("/system/resume", tags=["system"], summary="Release the kill switch")
def resume() -> dict[str, Any]:
    return _ok(_service.resume)


# ----------------------------------------------------------------- portfolio and risk
@app.get("/portfolio", tags=["portfolio"], summary="Every bucket, its cash, positions and sleeves")
def portfolio() -> dict[str, Any]:
    return _ok(_service.portfolio)


@app.get("/risk", tags=["portfolio"], summary="Drawdown ladders and the market regime")
def risk_state() -> dict[str, Any]:
    return _ok(_service.risk_state)


@app.get("/micro-live", tags=["portfolio"], summary="Real-money workflow status and preflight gates")
def micro_live() -> dict[str, Any]:
    return _ok(_service.micro_live)


# ----------------------------------------------------------------- research
class ResearchRequest(BaseModel):
    ticker: str = Field(description="Symbol to analyse, for example VTI.")
    bucket: Literal["core", "sandbox", "micro_live"] = "core"
    instrument_type: Literal["auto", "etf", "stock", "crypto"] = "auto"
    trade_date: str | None = Field(None, description="yyyy-mm-dd. Defaults to the last completed session.")
    trigger: Literal["manual", "scheduled", "event", "reanalysis"] = "manual"


@app.post("/research", tags=["research"], summary="Start a research run in the background")
def start_research(body: ResearchRequest) -> dict[str, Any]:
    return _ok(_service.start_research, body.ticker, bucket=body.bucket,
               instrument_type=body.instrument_type, trade_date=body.trade_date, trigger=body.trigger)


@app.get("/research/{run_key}", tags=["research"], summary="Progress of one research run")
def research_run(run_key: str) -> dict[str, Any]:
    return _ok(_service.research_run, run_key)


@app.get("/research/{run_id}/trace", tags=["research"],
         summary="One run's agent trace: every stage report the pipeline wrote")
def research_trace(run_id: str,
                   full: Annotated[bool, Query()] = True) -> dict[str, Any]:
    return _ok(_service.research_trace, run_id, full=full)


@app.get("/research", tags=["research"], summary="Completed decision records, newest first")
def research_history(limit: Annotated[int, Query(ge=1, le=100)] = 20) -> list[dict[str, Any]]:
    return _ok(_service.research_history, limit)


# ----------------------------------------------------------------- proposals
@app.get("/proposals", tags=["proposals"], summary="Proposals and their product status")
def proposals(include_closed: bool = False) -> list[dict[str, Any]]:
    return _ok(_service.proposals, include_closed)


@app.get("/proposals/{proposal_id}", tags=["proposals"],
         summary="Full proposal: reasoning, bull and bear cases, Hard Risk rules")
def proposal(proposal_id: str) -> dict[str, Any]:
    return _ok(_service.proposal, proposal_id)


@app.get("/proposals/{proposal_id}/revalidate", tags=["proposals"],
         summary="What would happen on approval, changing nothing")
def revalidate(proposal_id: str) -> dict[str, Any]:
    return _ok(_service.revalidate_proposal, proposal_id)


class ApproveRequest(BaseModel):
    note: str | None = Field(None, description="Why you approved it. Kept in the audit trail.")
    confirm: bool = Field(True, description="Must be true. A guard against an accidental POST.")


@app.post("/proposals/{proposal_id}/approve", tags=["proposals"],
          summary="Authorise the idea; execute only if current conditions still support it")
def approve(proposal_id: str, body: ApproveRequest | None = None) -> dict[str, Any]:
    body = body or ApproveRequest()
    if not body.confirm:
        raise HTTPException(status_code=400, detail="approval requires confirm=true")
    return _ok(_service.approve, proposal_id, body.note)


class RejectRequest(BaseModel):
    reason: str | None = None


@app.post("/proposals/{proposal_id}/reject", tags=["proposals"], summary="Reject a proposal")
def reject(proposal_id: str, body: RejectRequest | None = None) -> dict[str, Any]:
    return _ok(_service.reject, proposal_id, (body or RejectRequest()).reason)


@app.post("/proposals/{proposal_id}/reanalyze", tags=["proposals"],
          summary="Mark the proposal as needing fresh research and start it")
def reanalyze(proposal_id: str) -> dict[str, Any]:
    return _ok(_service.request_reanalysis, proposal_id)


# ----------------------------------------------------------------- orders
@app.get("/orders", tags=["orders"], summary="Recorded fills and live open orders")
def orders(limit: Annotated[int, Query(ge=1, le=200)] = 50) -> dict[str, Any]:
    return _ok(_service.orders, limit)


def main() -> None:
    """``ct-api``: run the backend for a local frontend."""
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(prog="ct-api", description="Run the backend API on localhost.")
    parser.add_argument("--host", default="127.0.0.1", help="bound to localhost by default; this process holds "
                                                            "broker credentials and must not be exposed")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    uvicorn.run("conservative_trading.api.app:app", host=args.host, port=args.port, reload=args.reload)


# ---------------------------------------------------------------------------
# The console, served from this process.
#
# Same origin as the API, so no CORS middleware is needed and none is added:
# this stays one local listener on 127.0.0.1 with nothing cross-site allowed.
# Absent `ui/`, the API runs exactly as before.
_UI_DIR = Path(__file__).resolve().parents[2] / "ui"
if _UI_DIR.is_dir():
    app.mount("/ui", StaticFiles(directory=str(_UI_DIR), html=True), name="ui")
    logger.info("console mounted at /ui from %s", _UI_DIR)
