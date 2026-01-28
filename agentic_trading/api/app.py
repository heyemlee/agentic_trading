"""HTTP layer over :mod:`agentic_trading.service`.

Thin by design: each route calls one service function and returns its dict. FastAPI is used for
one reason beyond routing, which is that it publishes an OpenAPI schema at ``/openapi.json``, so
a frontend can be generated against a machine-readable contract rather than prose.

Security boundary: this process holds the Robinhood token and the Claude session. Neither ever
crosses the wire. The API exposes product operations only, and every write path runs the same
engine functions the CLI does, so no client can bypass Hard Risk, human approval,
execution-time revalidation, the Micro-Live capital boundary or the venue's own review.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import quote
from uuid import UUID

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from ..env import load_env
from ..service import Service, ServiceConflict, ServiceError
from ..state_db import StateConflict
from .auth import COOKIE, SESSION_SECONDS, ConsoleAuth

logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app):
    async def reconcile_loop():
        while True:
            try:
                await asyncio.to_thread(_service.reconcile_orders)
            except Exception:
                logger.exception("background order reconciliation failed")
            try:
                await asyncio.to_thread(_service.recover_research_jobs)
            except Exception:
                logger.exception("background research recovery failed")
            try:
                # Returns immediately, without opening a venue session, whenever autonomy is
                # off, withdrawn, or the market is closed — which is almost always.
                await asyncio.to_thread(_service.autonomous_reductions)
            except Exception:
                logger.exception("autonomous reduction pass failed")
            try:
                await asyncio.to_thread(_service.dispatch_notifications)
            except Exception:
                logger.exception("notification dispatch failed")
            await asyncio.sleep(15)

    async def heartbeat_loop():
        """An outage cannot be reported from inside the outage, so a watcher elsewhere is
        told this machine is alive and infers the opposite from silence."""
        while True:
            minutes = 15
            try:
                result = await asyncio.to_thread(_service.send_heartbeat)
                minutes = int(result.get("every_minutes") or 15)
            except Exception:
                logger.exception("external heartbeat failed")
            await asyncio.sleep(max(60, minutes * 60))

    tasks = [asyncio.create_task(reconcile_loop()), asyncio.create_task(heartbeat_loop())]
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass


app = FastAPI(
    lifespan=lifespan,
    title="Agentic Trading Backend",
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
_auth = ConsoleAuth()


@app.middleware("http")
async def security_boundary(request: Request, call_next):
    path = request.url.path
    response = None
    if not _auth.allowed_host(request.headers.get("host", "")):
        response = JSONResponse({"detail": "untrusted host"}, status_code=400)
    unsafe = request.method not in ("GET", "HEAD", "OPTIONS")
    if response is None and unsafe and (
                not _auth.allowed_origin(request.headers.get("origin"))
                or request.headers.get("sec-fetch-site") == "cross-site"
                or request.headers.get("x-ct-request") != "1"
                or request.headers.get("content-type", "").split(";")[0] != "application/json"):
        response = JSONResponse({"detail": "same-origin JSON request required"}, status_code=403)
    session = _auth.session(request.cookies.get(COOKIE)) if response is None else None
    # The app shell is static and says nothing the login page does not already say. It has to
    # be reachable unauthenticated or "Add to Home Screen" from the login screen gets no icon,
    # and iOS only delivers push to an app added from that icon.
    public = (path in ("/auth/login", "/auth/session", "/ui/login.html",
                       "/ui/manifest.webmanifest", "/ui/sw.js")
              or path.startswith(("/ui/assets/", "/ui/icons/"))
              or (path.startswith("/ui/") and request.query_params.get("demo") == "1" and not unsafe))
    if response is None and not public and session is None:
        if path.startswith("/ui"):
            # Carry where they were going. A push notification about a waiting
            # approval, opened on an expired session, would otherwise land on
            # Home with its subject lost. Only this origin's path and query
            # travel, fully encoded so they cannot break out of the parameter,
            # and the console re-checks the value before following it back.
            here = path + (f"?{request.url.query}" if request.url.query else "")
            response = RedirectResponse("/ui/login.html?next=" + quote(here, safe=""),
                                        status_code=303)
        else:
            configured = _auth.credentials() is not None
            response = JSONResponse({"detail": "login required" if configured else
                                     "console credentials not configured; run ct-auth locally"},
                                    status_code=401 if configured else 503)
    if (response is None and unsafe and path != "/auth/login" and (
            session is None or not hmac.compare_digest(request.headers.get("x-csrf-token", ""), session.csrf))):
        response = JSONResponse({"detail": "invalid session or CSRF token"}, status_code=403)
    if response is None:
        request.state.identity = session.username if session else None
        response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    # The console uses external scripts only. Swagger's vendor-rendered documentation
    # needs its own CDN/inline policy; all business pages retain the strict policy.
    scripts = "'self'" if path not in ("/docs", "/redoc") else "'self' 'unsafe-inline' https://cdn.jsdelivr.net"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src " + scripts + "; style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; "
        "frame-ancestors 'none'; form-action 'self'")
    return response


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=1024)


@app.post("/auth/login", tags=["authentication"])
def login(body: LoginRequest, request: Request):
    token, status = _auth.login(body.username, body.password,
                                user_agent=request.headers.get("user-agent"))
    if token is None:
        codes = {"unconfigured": 503, "rate_limited": 429, "invalid": 401}
        raise HTTPException(codes[status], "console credentials not configured; run ct-auth locally" if status == "unconfigured"
                            else "try again in five minutes" if status == "rate_limited" else "invalid credentials")
    _auth.logout(request.cookies.get(COOKIE))
    session = _auth.session(token)
    response = JSONResponse({"authenticated": True, "username": session.username, "csrf_token": session.csrf})
    response.set_cookie(COOKIE, token, max_age=SESSION_SECONDS, httponly=True,
                        secure=request.url.scheme == "https", samesite="strict", path="/")
    return response


@app.get("/auth/session", tags=["authentication"])
def session_status(request: Request):
    session = _auth.session(request.cookies.get(COOKIE))
    return {"authenticated": session is not None, "configured": _auth.credentials() is not None,
            "username": session.username if session else None,
            "csrf_token": session.csrf if session else None}


@app.post("/auth/logout", tags=["authentication"])
def logout(request: Request):
    _auth.logout(request.cookies.get(COOKIE))
    response = JSONResponse({"authenticated": False})
    response.delete_cookie(COOKIE, path="/")
    return response


@app.get("/auth/sessions", tags=["authentication"],
         summary="Devices signed in, with when each was last used")
def sessions(request: Request) -> dict[str, Any]:
    return {"sessions": _auth.sessions(request.cookies.get(COOKIE)),
            "max_age_days": SESSION_SECONDS // 86400}


@app.post("/auth/sessions/{session_id}/revoke", tags=["authentication"],
          summary="Sign one device out")
def revoke_session(session_id: str, request: Request) -> dict[str, Any]:
    token = request.cookies.get(COOKIE)
    revoked = _auth.revoke(session_id, current_token=token)
    if not revoked:
        raise HTTPException(status_code=404, detail="no such session; it may already be signed out")
    response = JSONResponse({"revoked": True, "session_id": session_id})
    # Revoking the device you are holding signs it out here too, rather than leaving a cookie
    # that will be rejected on its next request without explanation.
    if not any(item["current"] for item in _auth.sessions(token)):
        response.delete_cookie(COOKIE, path="/")
    return response


@app.post("/auth/sessions/revoke-others", tags=["authentication"],
          summary="Sign out every device except this one")
def revoke_other_sessions(request: Request) -> dict[str, Any]:
    return {"revoked": _auth.revoke_others(request.cookies.get(COOKIE))}


def _ok(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except ServiceConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except StateConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("unhandled error in %s", getattr(fn, "__name__", fn))
        raise HTTPException(status_code=500, detail="internal error; inspect the local server log") from exc


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


class PushSubscription(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subscription: dict[str, Any]
    label: str = Field("", max_length=100)
    renewed: bool = False


@app.get("/notifications/key", tags=["system"],
         summary="The public VAPID key a browser needs to subscribe")
def push_key() -> dict[str, Any]:
    return _ok(_service.push_key)


@app.post("/notifications/subscribe", tags=["system"],
          summary="Record this device's push subscription")
def push_subscribe(body: PushSubscription) -> dict[str, Any]:
    return _ok(_service.push_subscribe, body.subscription, body.label)


@app.get("/notifications/devices", tags=["system"],
         summary="Devices subscribed to push, without exposing any endpoint")
def push_devices() -> dict[str, Any]:
    return _ok(_service.push_devices)


@app.post("/notifications/devices/{device_id}/unsubscribe", tags=["system"],
          summary="Stop pushing to one device")
def push_unsubscribe(device_id: str) -> dict[str, Any]:
    return _ok(_service.push_unsubscribe, device_id)


@app.post("/notifications/test", tags=["system"],
          summary="Send one notification now, to prove the path works")
def push_test() -> dict[str, Any]:
    return _ok(_service.push_test)


@app.get("/system/notifications", tags=["system"],
         summary="Notification channel, what is enabled, and the delivery record")
def notification_status() -> dict[str, Any]:
    return _ok(_service.notification_status)


@app.get("/system/operations", tags=["system"],
         summary="Background activities, disk headroom and retention")
def operations() -> dict[str, Any]:
    return _ok(_service.operations)


@app.get("/evaluation", tags=["system"],
         summary="The forward record against a passive benchmark, including what it did not do")
def evaluation_report() -> dict[str, Any]:
    return _ok(_service.evaluation_report)


@app.get("/system/schedule", tags=["system"],
         summary="Next scheduled research, the last result, and why a run was skipped")
def schedule_status() -> dict[str, Any]:
    return _ok(_service.schedule_status)


class PauseRequest(BaseModel):
    reason: str = Field("paused from the interface", description="Recorded in the halt file and the audit trail.")


@app.post("/system/pause", tags=["system"], summary="Engage the kill switch: no orders may be submitted")
def pause(request: Request, body: PauseRequest | None = None) -> dict[str, Any]:
    return _ok(_service.pause, (body or PauseRequest()).reason, identity=request.state.identity)


@app.post("/system/resume", tags=["system"], summary="Release the kill switch")
def resume(request: Request) -> dict[str, Any]:
    return _ok(_service.resume, identity=request.state.identity)


# ----------------------------------------------------------------- portfolio and risk
@app.get("/portfolio", tags=["portfolio"], summary="Every bucket, its cash, positions and sleeves")
def portfolio() -> dict[str, Any]:
    return _ok(_service.portfolio)


class PortfolioConfirm(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ledger: Literal["all", "conceptual", "micro_live"] = "all"
    dry_run: bool = False


@app.post("/portfolio/confirm", tags=["portfolio"],
          summary="Confirm a ledger against what can contradict it, then renew its as_of")
def confirm_portfolio(body: PortfolioConfirm) -> dict[str, Any]:
    return _ok(_service.confirm_portfolio, ledger=body.ledger, dry_run=body.dry_run)


@app.get("/risk", tags=["portfolio"], summary="Drawdown ladders and the market regime")
def risk_state() -> dict[str, Any]:
    return _ok(_service.risk_state)


@app.get("/micro-live", tags=["portfolio"], summary="Real-money workflow status and preflight gates")
def micro_live() -> dict[str, Any]:
    return _ok(_service.micro_live)


# ----------------------------------------------------------------- research
class AdvisorRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["refresh", "chat", "shortlist", "analysis"]
    request_id: str
    message: str = Field("", max_length=4000)
    conversation_id: str = Field("default", max_length=64)
    ticker: str = Field("", max_length=20)
    account_id: str | None = None
    language: Literal["zh", "en"] = "zh"


class AdvisorNotes(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(max_length=4000)
    revision: int = Field(ge=0)


class AdvisorReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str
    language: Literal["zh", "en"] = "zh"


@app.get("/advisor", tags=["assistant"])
def advisor_state() -> dict[str, Any]:
    return _ok(_service.advisor_state)


@app.get("/advisor/conversations/{conversation_id}", tags=["assistant"])
def advisor_conversation(conversation_id: str) -> dict[str, Any]:
    return _ok(_service.advisor_conversation, conversation_id)


@app.post("/advisor/jobs", tags=["assistant"])
def advisor_start(body: AdvisorRequest) -> dict[str, Any]:
    return _ok(_service.start_advisor, **body.model_dump())


@app.post("/advisor/notes", tags=["assistant"])
def advisor_notes(body: AdvisorNotes) -> dict[str, Any]:
    return _ok(_service.advisor_notes, body.text, body.revision)


@app.post("/advisor/review", tags=["assistant"])
def advisor_review(body: AdvisorReview) -> dict[str, Any]:
    return _ok(_service.review_observed_holdings, body.request_id, body.language)


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


@app.get("/research/jobs", tags=["research"], summary="Persistent research queue, newest first")
def research_jobs(limit: Annotated[int, Query(ge=1, le=100)] = 50) -> list[dict[str, Any]]:
    return _ok(_service.research_jobs, limit)


# Declared before /research/{run_key} so the literal path wins the match, exactly as
# /research/jobs above it does.
@app.get("/research/roster", tags=["research"],
         summary="The research team, and which analysts each instrument type starts")
def research_roster() -> dict[str, Any]:
    return _ok(_service.research_roster)


class CancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str | None = Field(None, max_length=500)


@app.post("/research/{run_key}/cancel", tags=["research"],
          summary="Stop a queued or running research job")
def cancel_research(run_key: str, body: CancelRequest | None = None) -> dict[str, Any]:
    return _ok(_service.cancel_research, run_key,
               (body.reason if body and body.reason else "cancelled from the console"))


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
    model_config = ConfigDict(extra="forbid")
    note: str | None = Field(None, max_length=2000)
    confirm: StrictBool
    version: str = Field(pattern=r"^[a-f0-9]{64}$")
    idempotency_key: UUID
    ticker: str = Field(min_length=1, max_length=32)
    bucket: Literal["core", "sandbox", "micro_live"]
    action: Literal["BUY", "ADD", "SELL", "REDUCE"]
    notional_usd: float = Field(gt=0, allow_inf_nan=False)
    sell_quantity: float | None = Field(None, gt=0, allow_inf_nan=False)


@app.post("/proposals/{proposal_id}/approve", tags=["proposals"],
          summary="Authorise this version; execute only if current conditions still support it")
def approve(proposal_id: str, body: ApproveRequest, request: Request) -> dict[str, Any]:
    if body.confirm is not True:
        raise HTTPException(status_code=400, detail="approval requires explicit confirm=true")
    return _ok(_service.approve, proposal_id, body.note,
               identity=request.state.identity, version=body.version,
               request_id=str(body.idempotency_key),
               terms=body.model_dump(include={"ticker", "bucket", "action", "notional_usd", "sell_quantity"}))


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
@app.get("/activity", tags=["orders"],
         summary="The audit journal: every proposal, fill, run and kill-switch change, paged")
def activity(kind: Annotated[list[str] | None, Query()] = None,
             subject: Annotated[str | None, Query(max_length=128)] = None,
             cursor: Annotated[int | None, Query(ge=1)] = None,
             limit: Annotated[int, Query(ge=1, le=200)] = 50) -> dict[str, Any]:
    return _ok(_service.activity, kinds=tuple(kind or ()), subject=subject,
               cursor=cursor, limit=limit)


@app.get("/orders", tags=["orders"], summary="Recorded fills and live open orders")
def orders(limit: Annotated[int, Query(ge=1, le=200)] = 50) -> dict[str, Any]:
    return _ok(_service.orders, limit)


def main() -> None:
    """``ct-api``: run the backend for a local frontend."""
    load_env()
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(prog="ct-api", description="Run the backend API on localhost.")
    parser.add_argument("--host", default="127.0.0.1", help="bound to localhost by default; this process holds "
                                                            "broker credentials and must not be exposed")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    # The private HTTPS entry point terminates TLS on this machine and forwards to loopback, so
    # the request arrives as plain HTTP and the protocol is only knowable from a forwarded
    # header. Trust that header from loopback and nowhere else: otherwise anything that could
    # reach this port could claim HTTPS and the session cookie would lose its Secure flag.
    uvicorn.run("agentic_trading.api.app:app", host=args.host, port=args.port,
                reload=args.reload, proxy_headers=True, forwarded_allow_ips="127.0.0.1")


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
