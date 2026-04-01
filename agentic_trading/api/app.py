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
import hashlib
import hmac
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from html import escape
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import quote
from uuid import UUID

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers, MutableHeaders
from starlette.middleware.gzip import GZipMiddleware

from ..env import load_env
from ..service import Service, ServiceConflict, ServiceError
from ..service.shapes import (
    AccountScopes,
    AdvisorConversation,
    AdvisorState,
    Attention,
    Authority,
    ClaudeStatus,
    Evaluation,
    ExitContinuation,
    Health,
    MarketStatus,
    MicroLive,
    Orders,
    PersonalNotes,
    Portfolio,
    ProposalDetail,
    ProposalSummary,
    ResearchJob,
    ResearchRecord,
    ResearchSubjects,
    RouteQuality,
)
from ..state_db import StateConflict
from .auth import COOKIE, SESSION_SECONDS, ConsoleAuth

logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app):
    try:
        # Records when each background activity became expected, once per deployment. Cheap, and
        # it must not stop the API coming up: a missing origin costs a grace period, nothing more.
        from .. import operations as _ops
        _ops.declare(_service.state_db(_service.profiles()))
    except Exception:
        logger.debug("could not declare the activity origins at startup", exc_info=True)

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
                # A loosening config change made from the console waits a day; this is where
                # it lands once the day is up. Returns at once while nothing is due.
                await asyncio.to_thread(_service.apply_config_changes)
            except Exception:
                logger.exception("applying cooled-down configuration changes failed")
            try:
                await asyncio.to_thread(_service.dispatch_notifications)
            except Exception:
                logger.exception("notification dispatch failed")
            try:
                # The venue session kept for reads closes once nobody has read through it lately,
                # rather than holding a thread and a connection open for a screen nobody watches.
                await asyncio.to_thread(_service.release_idle_venue_session)
            except Exception:
                logger.exception("closing the idle venue session failed")
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

    async def refresh_loop():
        """Renews the kept readings the console polls -- regime, venue account, portfolio -- so
        a poll finds them computed. Separate from reconcile_loop, whose fifteen seconds recover
        orders and must not wait behind a venue handshake or a portfolio being priced."""
        while True:
            try:
                await asyncio.to_thread(_service.refresh_kept_readings)
            except Exception:
                logger.exception("renewing the kept readings failed")
            await asyncio.sleep(15)

    async def simulation_loop():
        """Runs automatic trading's pass once a minute, for whatever a person has started.

        Its own loop, so that pricing a book can never sit in front of the fifteen seconds that
        recover real orders. Until something has been started it makes a few small reads and
        returns. A simulation that has been started cannot reach a real venue (auto.paper). A
        plan a person put in force on the real account is read and decided here too, and writes
        proposals only: approving them stays with reconcile_loop above, which is the one place
        this account's orders have ever been approved from without a person.
        """
        while True:
            try:
                await asyncio.to_thread(_service.auto_tick)
            except Exception:
                logger.exception("the automatic-trading pass failed")
            await asyncio.sleep(60)

    tasks = [asyncio.create_task(reconcile_loop()), asyncio.create_task(heartbeat_loop()),
             asyncio.create_task(refresh_loop()), asyncio.create_task(simulation_loop())]
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
        await asyncio.to_thread(_service.close)


app = FastAPI(
    lifespan=lifespan,
    title="Agentic Trading Backend",
    version="0.1.0",
    summary="Product interface for a conservative, human-approved investment system.",
    description=(
        "Research runs on a Claude subscription, a deterministic Hard Risk Layer decides what is "
        "permitted, and the investor approves anything that touches money. Approval authorises an "
        "idea, never a stored order: the world is re-read and re-checked before anything is sent.\n\n"
        "Real-money execution is confined to a $100 Agentic account at Robinhood and is disabled "
        "by default."
    ),
)
_service = Service("config")
_auth = ConsoleAuth()
#: Reads slower than this are logged by name; a page cannot show before its reads answer.
_SLOW_MS = 400


def _cache_policy(request: Request) -> str:
    """How long a browser may keep a response, decided by what the response is.

    Everything the API says about money, orders or the session is ``no-store``: a page must
    never show an old price, an old drawdown level or an old proposal as if it were current.
    The console's own files say nothing of the kind, and every response used to be no-store
    all the same, so each tap on the tab bar downloaded a quarter of a megabyte of unchanged
    script again. That is what made moving between screens slow.

    A page is ``no-cache``: kept, but revalidated with the server on every visit, so a new
    release is seen at once, a signed-out session still lands on the login screen, and with no
    connection there is no page at all rather than a stale one. The scripts and stylesheets a
    page names are stamped by :class:`ConsoleFiles` with the release they belong to, so a
    stamped URL can be kept for a year without going stale: the next release's pages ask for
    different URLs. An unstamped asset, one a script loads by itself, revalidates like a page.
    The service worker and the manifest keep the policy they always had.
    """
    path = request.url.path
    if path.startswith(("/ui/assets/", "/ui/icons/")):
        return "max-age=31536000, immutable" if request.query_params.get("v") else "no-cache"
    if path.startswith("/ui/") and path.endswith(("/", ".html")):
        return "no-cache"
    return "no-store"


@app.middleware("http")
async def security_boundary(request: Request, call_next):
    path = request.url.path
    response = None
    if not _auth.allowed_host(request.headers.get("host", "")):
        response = JSONResponse({"detail": "untrusted host"}, status_code=400)
    unsafe = request.method not in ("GET", "HEAD", "OPTIONS")
    # A bare origin is what a person types on a phone, and it used to answer with the API's
    # "login required" JSON because the console is mounted under /ui. Temporary on purpose: a
    # permanent redirect would sit in the cache of every browser that ever followed it, long
    # outliving any decision about where the console lives.
    if response is None and path == "/" and request.method in ("GET", "HEAD"):
        response = RedirectResponse("/ui/", status_code=307)
    # Research became part of the Lab on 2026-09-23. A link written before then -- a bookmark,
    # a notification already delivered -- still reaches the report it named, with its query.
    if response is None and path == "/ui/research.html" and request.method in ("GET", "HEAD"):
        response = RedirectResponse("/ui/lab.html" + (f"?{request.url.query}" if request.url.query else ""),
                                    status_code=307)
    # Assets became Home the same day. A holding's address, or a notification already delivered,
    # still opens the account view it named.
    if response is None and path == "/ui/portfolio.html" and request.method in ("GET", "HEAD"):
        response = RedirectResponse("/ui/index.html" + (f"?{request.url.query}" if request.url.query else ""),
                                    status_code=307)
    if response is None and unsafe and (
                not _auth.allowed_origin(request.headers.get("origin"))
                or request.headers.get("sec-fetch-site") == "cross-site"
                or request.headers.get("x-ct-request") != "1"
                or request.headers.get("content-type", "").split(";")[0] != "application/json"):
        response = JSONResponse({"detail": "same-origin JSON request required"}, status_code=403)
    # The app shell is static and says nothing the login page does not already say. It has to
    # be reachable unauthenticated or "Add to Home Screen" from the login screen gets no icon,
    # and iOS only delivers push to an app added from that icon.
    public = (path in ("/auth/login", "/auth/session", "/ui/login.html",
                       "/ui/manifest.webmanifest", "/ui/sw.js")
              or path.startswith(("/ui/assets/", "/ui/icons/"))
              or (path.startswith("/ui/") and request.query_params.get("demo") == "1" and not unsafe))
    # A read of something public needs no session, and /auth/session looks its own up. The
    # lookup reads the auth file and the database, and once a minute writes to it; it used to
    # run here, on the event loop, for every script and icon a page loads, and a write waiting
    # on the database lock stalled every request in the process. It now runs in a thread.
    session = None
    if response is None and (unsafe or not public):
        session = await run_in_threadpool(_auth.session, request.cookies.get(COOKIE))
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
            # Bytes, not str: headers arrive decoded as latin-1, and compare_digest raises
            # TypeError on a str with a non-ASCII character, which answered 500 without the
            # security headers instead of refusing the request.
            session is None or not hmac.compare_digest(request.headers.get("x-csrf-token", "").encode(),
                                                       session.csrf.encode()))):
        response = JSONResponse({"detail": "invalid session or CSRF token"}, status_code=403)
    if response is None:
        request.state.identity = session.username if session else None
        started = time.perf_counter()
        response = await call_next(request)
        elapsed_ms = (time.perf_counter() - started) * 1000
        # A screen that waits on one of these is what "the console is slow" turns out to mean,
        # and nothing else records which read it was. One line, only past the threshold, so the
        # log names the endpoint instead of leaving it to be guessed from the page.
        if elapsed_ms >= _SLOW_MS:
            logger.info("slow request: %s %s answered in %d ms", request.method, path, elapsed_ms)
    response.headers["Cache-Control"] = _cache_policy(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    # same-origin, not no-referrer: the back link reads document.referrer to tell a step back
    # inside the console from a cold open, and no-referrer emptied it on every navigation, so
    # "back" always went to the section root. Nothing is still sent to another origin.
    response.headers["Referrer-Policy"] = "same-origin"
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


def _step_up(request: Request, password: str | None) -> bool:
    """Re-verify the console password for an action that needs more than a session.

    Returns whether a password was given and matched. A wrong one is a 400 with a message,
    never a 401 or a 403: the console treats those as "signed out" and "not sent".
    """
    if password is None:
        return False
    status = _auth.verify_password(request.state.identity, password)
    if status == "rate_limited":
        raise HTTPException(429, "try again in five minutes")
    if status != "ok":
        raise HTTPException(400, "the console password did not match")
    return True


class PasswordChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    current: str = Field(min_length=1, max_length=1024)
    new: str = Field(min_length=1, max_length=1024)


@app.post("/auth/password", tags=["authentication"],
          summary="Rotate the console password; every device is signed out, this one included")
def change_password(body: PasswordChange, request: Request):
    from .auth import configure_password

    _step_up(request, body.current)
    try:
        configure_password(_auth.path, request.state.identity, body.new)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    _ok(_service.record_password_change, identity=request.state.identity)
    response = JSONResponse({"authenticated": False, "rotated": True})
    response.delete_cookie(COOKIE, path="/")
    return response


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
def health() -> Health:
    return _ok(_service.health)


@app.get("/system/claude", tags=["system"], summary="Claude subscription runtime state")
def claude_status() -> ClaudeStatus:
    return _ok(_service.claude_status)


@app.get("/system/market", tags=["system"], summary="Market session from the exchange calendar")
def market_status() -> MarketStatus:
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
def evaluation_report(scope: Literal["demo", "agentic", "route_quality"] = "demo") -> Evaluation | RouteQuality:
    return _ok(_service.evaluation_report, scope)


@app.get("/system/schedule", tags=["system"],
         summary="Next scheduled research, the last result, and why a run was skipped")
def schedule_status() -> dict[str, Any]:
    return _ok(_service.schedule_status)


@app.get("/attention", tags=["system"],
         summary="What still needs a person, computed from unresolved state and never from what was sent")
def attention() -> Attention:
    return _ok(_service.attention)


class PauseRequest(BaseModel):
    # Unknown fields are refused like every other write model here: a misspelt field was
    # silently ignored. The reason is written whole into the halt file, so it is bounded.
    model_config = ConfigDict(extra="forbid")
    reason: str = Field("paused from the interface", max_length=500,
                        description="Recorded in the halt file and the audit trail.")


@app.post("/system/pause", tags=["system"], summary="Engage the kill switch: no orders may be submitted")
def pause(request: Request, body: PauseRequest | None = None) -> dict[str, Any]:
    return _ok(_service.pause, (body or PauseRequest()).reason, identity=request.state.identity)


@app.post("/system/resume", tags=["system"], summary="Release the kill switch")
def resume(request: Request) -> dict[str, Any]:
    return _ok(_service.resume, identity=request.state.identity)


# ----------------------------------------------------------------- portfolio and risk
@app.get("/portfolio", tags=["portfolio"], summary="Every bucket, its cash, positions and sleeves")
def portfolio() -> Portfolio:
    # Kept between polls and renewed by refresh_loop; checked_at says when it was read.
    return _ok(_service.kept_portfolio)


class PortfolioConfirm(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ledger: Literal["all", "conceptual", "micro_live"] = "all"
    dry_run: bool = False


@app.post("/portfolio/confirm", tags=["portfolio"],
          summary="Confirm a ledger against what can contradict it, then renew its as_of")
def confirm_portfolio(body: PortfolioConfirm) -> dict[str, Any]:
    return _ok(_service.confirm_portfolio, ledger=body.ledger, dry_run=body.dry_run)


@app.get("/portfolio/ledgers", tags=["portfolio"],
         summary="The last ledger check's report, refusal figures included; opens no venue session")
def ledger_check() -> dict[str, Any]:
    return _ok(_service.ledger_check)


class CashflowRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bucket: Literal["core", "sandbox", "micro_live"]
    amount_usd: float = Field(gt=0, le=1_000_000)
    # deposit/withdrawal is funding the person moved and never counts as return;
    # income/fee is interest, a dividend or a charge, and does.
    kind: Literal["deposit", "withdrawal", "income", "fee"] = "deposit"
    date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    reason: str = Field(default="", max_length=500)


@app.post("/portfolio/cashflow", tags=["portfolio"],
          summary="Record a deposit or withdrawal, so the drawdown ladder does not read it as performance")
def record_cashflow(body: CashflowRequest, request: Request) -> dict[str, Any]:
    return _ok(_service.record_cashflow, bucket=body.bucket, amount_usd=body.amount_usd,
               kind=body.kind, date=body.date, reason=body.reason,
               identity=request.state.identity)


# ----------------------------------------------------------------- configuration
class ConfigChange(BaseModel):
    """One section's new text, values for dotted paths, or one research switch by name."""

    model_config = ConfigDict(extra="forbid")
    file: Literal["risk_profile.yaml", "investor_profile.yaml", "event_calendar.yaml"]
    revision: str = Field(default="", max_length=64)
    key: str | None = Field(default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    text: str | None = Field(default=None, max_length=200_000)
    set: dict[str, Any] | None = None
    # A research switch, named rather than addressed: the key it sets, and may create, is the
    # service's to name (config_changes.RESEARCH_SWITCHES). It goes through the same preview,
    # classification and record as the other two shapes.
    switch: Literal["candidates", "sec_form4", "reddit"] | None = None
    on: StrictBool | None = None


class ConfigSubmit(ConfigChange):
    password: str | None = Field(default=None, max_length=1024)
    reason: str = Field(default="", max_length=500)


@app.get("/config", tags=["configuration"],
         summary="Editable configuration: files, sections, form values, pending and recent changes")
def config_overview() -> dict[str, Any]:
    return _ok(_service.config_overview)


@app.get("/config/authority", tags=["configuration"],
         summary="What may happen to the real-money account, from every rule that decides it")
def config_authority() -> Authority:
    return _ok(_service.effective_authority)


@app.get("/config/section", tags=["configuration"], summary="One top-level section's text")
def config_section(file: Annotated[str, Query(max_length=40)],
                   key: Annotated[str, Query(max_length=80)]) -> dict[str, Any]:
    return _ok(_service.config_section, file, key)


@app.post("/config/preview", tags=["configuration"],
          summary="What a change would do: the diff, and whether it loosens anything")
def config_preview(body: ConfigChange) -> dict[str, Any]:
    return _ok(_service.preview_config_change, **body.model_dump())


@app.post("/config/changes", tags=["configuration"],
          summary="Apply a change now, or start its cooling period if it loosens a limit")
def config_submit(body: ConfigSubmit, request: Request) -> dict[str, Any]:
    confirmed = _step_up(request, body.password)
    return _ok(_service.submit_config_change, **body.model_dump(exclude={"password"}),
               identity=request.state.identity, confirmed=confirmed)


@app.post("/config/changes/{change_id}/cancel", tags=["configuration"],
          summary="Withdraw a change that is still in its cooling period")
def config_cancel(change_id: str, request: Request) -> dict[str, Any]:
    return _ok(_service.cancel_config_change, change_id, identity=request.state.identity)


class AutonomyStop(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=500)


class AutonomyResume(AutonomyStop):
    password: str = Field(min_length=1, max_length=1024)


@app.post("/autonomy/stop", tags=["configuration"],
          summary="Stop autonomous action in one gesture; it stays off until restored by hand")
def autonomy_stop(body: AutonomyStop, request: Request) -> dict[str, Any]:
    return _ok(_service.autonomy_stop, body.reason, identity=request.state.identity)


@app.post("/autonomy/resume", tags=["configuration"],
          summary="Restore autonomous action after looking at why it stopped")
def autonomy_resume(body: AutonomyResume, request: Request) -> dict[str, Any]:
    _step_up(request, body.password)
    return _ok(_service.autonomy_resume, body.reason, identity=request.state.identity)


class AutonomySwitch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    block: Literal["reduce", "open"]
    on: bool
    # The switch's number, set in the same change when turning it on: the share of a holding
    # kept for reduce, the confidence needed for open. The service checks each one's range.
    value: int | None = Field(default=None, ge=0, le=100)
    password: str | None = Field(default=None, max_length=1024)
    reason: str = Field(default="", max_length=500)


@app.post("/autonomy/switch", tags=["configuration"],
          summary="Turn one Agentic autonomy switch on (with the password, at once) or off")
def autonomy_switch(body: AutonomySwitch, request: Request) -> dict[str, Any]:
    confirmed = _step_up(request, body.password)
    return _ok(_service.autonomy_switch, body.block, body.on, value=body.value, reason=body.reason,
               identity=request.state.identity, confirmed=confirmed)


# ----------------------------------------------------------------- automatic trading
class AutoPlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["steady", "swing", "intraday"] = "steady"
    # Absent, the plan is drawn at the size the account already is.
    capital_usd: float | None = Field(default=None, gt=0, le=10_000_000)
    loss_pause_pct: float | None = Field(default=None, ge=0, le=100)
    # Asks the model for the strategy's parameters, on the subscription research runs on.
    # Off, the deterministic template is used and the plan says so.
    ask_model: StrictBool = False
    # Which of the mode's templates: the person's choice. Absent, the mode's first.
    template: Literal["research_etf", "baseline_etf", "trend_etf"] | None = None


@app.get("/auto-trading", tags=["automatic trading"],
         summary="Where automatic trading stands: every mode, open or not, and why not")
def auto_status() -> dict[str, Any]:
    return _ok(_service.auto_status)


@app.post("/auto-trading/plan", tags=["automatic trading"],
          summary="Preview a plan: what could be invested, why not more, and what would have to change")
def auto_plan(body: AutoPlanRequest) -> dict[str, Any]:
    return _ok(_service.auto_plan, **body.model_dump())


class AutoStartRequest(BaseModel):
    """A previewed plan, handed back to be put in force. Nothing here is free text that is acted
    on: the envelope is three figures, the strategy is clamped again, and ``plan_id`` has to
    match what those produce or nothing is started."""

    model_config = ConfigDict(extra="forbid")
    plan_id: str = Field(min_length=8, max_length=64)
    mode: Literal["steady", "swing", "intraday"] = "steady"
    capital_usd: float | None = Field(default=None, gt=0, le=10_000_000)
    loss_pause_pct: float | None = Field(default=None, ge=0, le=100)
    # The strategy block of the preview, when a model suggested it: asking the model again
    # would be asking a different question. Absent, the deterministic template.
    strategy: dict[str, Any] | None = None
    # The template the preview was drawn with, when the person chose one.
    template: Literal["research_etf", "baseline_etf", "trend_etf"] | None = None
    # The simulated account, or the real one. The real one is refused unless every condition in
    # GET /auto-trading/live holds, and then takes the console password.
    scope: Literal["paper", "live"] = "paper"
    password: str | None = Field(default=None, max_length=1024)


class AutoReasonRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field("", max_length=500)
    scope: Literal["paper", "live"] = "paper"
    # Resuming on the real account takes it; pausing and stopping never do.
    password: str | None = Field(default=None, max_length=1024)


class AutoFundRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["deposit", "withdrawal"]
    amount_usd: float = Field(gt=0, le=10_000_000)
    note: str = Field("", max_length=200)


class AutoAdjustRequest(BaseModel):
    """What to change in the strategy of the plan in force. Every figure is clamped to the plan's
    bounds, and one that would need a setting the plan does not carry is refused."""

    model_config = ConfigDict(extra="forbid")
    max_positions: int | None = Field(default=None, ge=1, le=50)
    position_pct: float | None = Field(default=None, gt=0, le=100)
    min_confidence: int | None = Field(default=None, ge=0, le=100)
    stop_loss_pct: float | None = Field(default=None, ge=0, le=100)
    take_profit_pct: float | None = Field(default=None, ge=0, le=1000)
    max_holding_days: int | None = Field(default=None, ge=0, le=3650)
    trend_window: int | None = Field(default=None, ge=0, le=400)
    universe: list[str] | None = Field(default=None, max_length=50)
    ask_model: StrictBool = False
    reason: str = Field("", max_length=500)


@app.get("/auto-trading/simulation", tags=["automatic trading"],
         summary="The simulated account: its plan, its record, what it holds and why it waits")
def auto_simulation() -> dict[str, Any]:
    return _ok(_service.auto_simulation)


@app.post("/auto-trading/start", tags=["automatic trading"],
          summary="Put a previewed plan in force: simulated, or -- behind every condition -- real")
def auto_start(body: AutoStartRequest, request: Request) -> dict[str, Any]:
    # The password is only ever looked at for the real account, and only checked here: the
    # service is told whether it matched, never what it was.
    confirmed = _step_up(request, body.password) if body.scope == "live" else False
    return _ok(_service.auto_start, **body.model_dump(exclude={"password"}),
               identity=request.state.identity, confirmed=confirmed)


@app.get("/auto-trading/live", tags=["automatic trading"],
         summary="The real account: the plan in force, a start that is waiting, and every condition")
def auto_live() -> dict[str, Any]:
    return _ok(_service.auto_live)


@app.post("/auto-trading/live/withdraw", tags=["automatic trading"],
          summary="Take back a start that is waiting, with the settings it has not yet changed")
def auto_withdraw_request(body: AutoReasonRequest, request: Request) -> dict[str, Any]:
    return _ok(_service.auto_withdraw_request, reason=body.reason, identity=request.state.identity)


class AutoPasswordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password: str | None = Field(default=None, max_length=1024)


class AutoReplacementSetting(BaseModel):
    model_config = ConfigDict(extra="forbid")
    on: StrictBool
    # Turning it on takes the console password; turning it off takes nothing.
    password: str | None = Field(default=None, max_length=1024)


@app.post("/auto-trading/candidate/evaluate", tags=["automatic trading"],
          summary="Look at the simulated candidate against the strategy in force; changes nothing "
                  "unless the plan was set to replace by itself")
def auto_evaluate_candidate(request: Request) -> dict[str, Any]:
    return _ok(_service.auto_evaluate_candidate, identity=request.state.identity)


@app.post("/auto-trading/candidate/replace", tags=["automatic trading"],
          summary="Replace the strategy in force with a candidate that passes every line")
def auto_replace_candidate(body: AutoPasswordRequest, request: Request) -> dict[str, Any]:
    confirmed = _step_up(request, body.password)
    return _ok(_service.auto_evaluate_candidate, identity=request.state.identity, apply=True,
               confirmed=confirmed)


@app.post("/auto-trading/replacement", tags=["automatic trading"],
          summary="Choose whether a validated candidate takes over by itself under this plan")
def auto_replacement_setting(body: AutoReplacementSetting, request: Request) -> dict[str, Any]:
    confirmed = _step_up(request, body.password) if body.on else False
    return _ok(_service.auto_replacement_setting, on=body.on, identity=request.state.identity,
               confirmed=confirmed)


@app.post("/auto-trading/pause", tags=["automatic trading"],
          summary="Stop a plan opening positions; what it holds is still looked after")
def auto_pause(body: AutoReasonRequest, request: Request) -> dict[str, Any]:
    return _ok(_service.auto_pause, reason=body.reason, identity=request.state.identity,
               scope=body.scope)


@app.post("/auto-trading/resume", tags=["automatic trading"],
          summary="Let a plan open positions again; says where the loss line now starts")
def auto_resume(body: AutoReasonRequest, request: Request) -> dict[str, Any]:
    confirmed = _step_up(request, body.password) if body.scope == "live" else False
    return _ok(_service.auto_resume, note=body.reason, identity=request.state.identity,
               scope=body.scope, confirmed=confirmed)


@app.post("/auto-trading/stop", tags=["automatic trading"],
          summary="Take a plan out of force; on the real account, a waiting start with it")
def auto_stop(body: AutoReasonRequest, request: Request) -> dict[str, Any]:
    return _ok(_service.auto_stop, reason=body.reason, identity=request.state.identity,
               scope=body.scope)


@app.post("/auto-trading/fund", tags=["automatic trading"],
          summary="Deposit into or withdraw from the simulated account, recorded as funding")
def auto_fund(body: AutoFundRequest, request: Request) -> dict[str, Any]:
    return _ok(_service.auto_fund, **body.model_dump(), identity=request.state.identity)


class AutoBacktestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Absent: the two years up to the last session that has finished.
    start: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    end: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")


@app.get("/auto-trading/intraday-checks", tags=["automatic trading"],
         summary="What an intraday mode would need that is missing: data, timeliness, venue, pace")
def auto_intraday_checks() -> dict[str, Any]:
    return _ok(_service.auto_intraday_checks)


@app.get("/auto-trading/performance", tags=["automatic trading"],
         summary="The simulated record beside passive, a simple rule, 60/40 and cash; backtests apart")
def auto_performance() -> dict[str, Any]:
    return _ok(_service.auto_performance)


@app.post("/auto-trading/backtest", tags=["automatic trading"],
          summary="Run the plan's comparators and recorded research over history, and keep the record")
def auto_backtest(body: AutoBacktestRequest, request: Request) -> dict[str, Any]:
    return _ok(_service.auto_backtest, start=body.start, end=body.end,
               identity=request.state.identity)


@app.post("/auto-trading/adjust", tags=["automatic trading"],
          summary="Change the simulated plan's strategy inside its confirmed envelope")
def auto_adjust(body: AutoAdjustRequest, request: Request) -> dict[str, Any]:
    wanted = body.model_dump(exclude={"ask_model", "reason"}, exclude_none=True)
    return _ok(_service.auto_adjust, strategy=wanted, ask_model=body.ask_model, reason=body.reason,
               identity=request.state.identity)


@app.get("/risk", tags=["portfolio"], summary="Drawdown ladders and the market regime")
def risk_state() -> dict[str, Any]:
    return _ok(_service.risk_state)


@app.get("/micro-live", tags=["portfolio"], summary="Real-money workflow status and preflight gates")
def micro_live() -> MicroLive:
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
    # Which account the conversation is about. The server binds the task and the conversation
    # to it; agentic admits chat only, and a personal account must be one the snapshot holds.
    scope: Literal["personal", "agentic"] = "personal"


class AdvisorNotes(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(max_length=4000)
    revision: int = Field(ge=0)


@app.get("/advisor", tags=["assistant"])
def advisor_state(scope: Literal["personal", "agentic"] = "personal",
                  account_id: Annotated[str | None, Query(max_length=40)] = None) -> AdvisorState:
    return _ok(_service.advisor_state, scope, account_id)


@app.get("/accounts", tags=["assistant"],
         summary="The account scopes a screen may bind to; opens no venue session")
def account_scopes() -> AccountScopes:
    return _ok(_service.account_scopes)


@app.get("/advisor/conversations/{conversation_id}", tags=["assistant"])
def advisor_conversation(conversation_id: str) -> AdvisorConversation:
    return _ok(_service.advisor_conversation, conversation_id)


@app.post("/advisor/jobs", tags=["assistant"])
def advisor_start(body: AdvisorRequest) -> dict[str, Any]:
    return _ok(_service.start_advisor, **body.model_dump())


@app.post("/advisor/notes", tags=["assistant"])
def advisor_notes(body: AdvisorNotes) -> PersonalNotes:
    return _ok(_service.advisor_notes, body.text, body.revision)


class ResearchRequest(BaseModel):
    ticker: str = Field(description="Symbol to analyse, for example VTI.")
    bucket: Literal["core", "sandbox", "micro_live"] = "core"
    instrument_type: Literal["auto", "etf", "stock", "crypto"] = "auto"
    trade_date: str | None = Field(None, description="yyyy-mm-dd. Defaults to the last completed session.")
    trigger: Literal["manual", "scheduled", "event", "reanalysis"] = "manual"
    # The account the console is looking at; refused when it does not hold the bucket.
    scope: Literal["agentic", "demo"] | None = None
    # The console's idempotency key: a repeated tap returns the run it already started.
    request_id: UUID | None = None
    # Why the run is asked for. The two that speak of a holding are refused for a symbol the
    # book does not hold, rather than researched as if it did.
    purpose: Literal["review_holding", "candidate", "exit_review"] | None = None


@app.post("/research", tags=["research"], summary="Start a research run in the background")
def start_research(body: ResearchRequest) -> ResearchJob:
    return _ok(_service.start_research, body.ticker, bucket=body.bucket,
               instrument_type=body.instrument_type, trade_date=body.trade_date, trigger=body.trigger,
               scope=body.scope, request_id=str(body.request_id) if body.request_id else None,
               purpose=body.purpose)


@app.get("/research/jobs", tags=["research"], summary="Persistent research queue, newest first")
def research_jobs(limit: Annotated[int, Query(ge=1, le=100)] = 50,
                  scope: Literal["agentic", "demo", "personal", "unspecified"] | None = None,
                  account_id: Annotated[str | None, Query(max_length=40)] = None) -> list[ResearchJob]:
    return _ok(_service.research_jobs, limit, scope=scope, account_id=account_id)


# Declared before /research/{run_key} so the literal path wins the match, exactly as
# /research/jobs above it does.
@app.get("/research/roster", tags=["research"],
         summary="The research team, and which analysts each instrument type starts")
def research_roster() -> dict[str, Any]:
    return _ok(_service.research_roster)


@app.get("/research/subjects", tags=["research"],
         summary="What research is about: what the owner holds, then what the owner added")
def research_subjects() -> ResearchSubjects:
    return _ok(_service.research_subjects)


class CancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str | None = Field(None, max_length=500)


@app.post("/research/{run_key}/cancel", tags=["research"],
          summary="Stop a queued or running research job")
def cancel_research(run_key: str, body: CancelRequest | None = None) -> dict[str, Any]:
    return _ok(_service.cancel_research, run_key,
               (body.reason if body and body.reason else "cancelled from the console"))


@app.post("/research/{run_key}/opened", tags=["research"],
          summary="The finished run's result was shown to the person; idempotent, never a side effect of a read")
def research_opened(run_key: str) -> ResearchJob:
    return _ok(_service.mark_opened, run_key)


@app.get("/research/{run_key}", tags=["research"], summary="Progress of one research run")
def research_run(run_key: str) -> dict[str, Any]:
    return _ok(_service.research_run, run_key)


@app.get("/research/{run_id}/trace", tags=["research"],
         summary="One run's agent trace: every stage report the pipeline wrote")
def research_trace(run_id: str,
                   full: Annotated[bool, Query()] = True) -> dict[str, Any]:
    return _ok(_service.research_trace, run_id, full=full)


@app.get("/research", tags=["research"], summary="Completed decision records, newest first")
def research_history(limit: Annotated[int, Query(ge=1, le=100)] = 20,
                     scope: Literal["agentic", "demo", "personal", "unspecified"] | None = None,
                     account_id: Annotated[str | None, Query(max_length=40)] = None,
                     task_type: Literal["full_research", "analysis"] | None = None,
                     include_all_personal: bool = False) -> list[ResearchRecord]:
    return _ok(_service.research_history, limit, scope=scope, account_id=account_id,
               task_type=task_type, include_all_personal=include_all_personal)


# ----------------------------------------------------------------- proposals
@app.get("/proposals", tags=["proposals"], summary="Proposals and their product status")
def proposals(include_closed: bool = False) -> list[ProposalSummary]:
    return _ok(_service.proposals, include_closed)


@app.get("/proposals/{proposal_id}", tags=["proposals"],
         summary="Full proposal: reasoning, bull and bear cases, Hard Risk rules")
def proposal(proposal_id: str) -> ProposalDetail:
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
    # The authority version the screen showed (GET /config/authority). Optional until the
    # console sends it; when sent, a version that has moved is refused like a moved proposal.
    authority_version: str | None = Field(None, min_length=1, max_length=32)
    # Required true when the authority loosened, or moved in a way the record cannot classify,
    # since the proposal was made: the person says they looked at what changed. It must come
    # with authority_version, the version the change was acknowledged against.
    acknowledge_authority_change: bool = False


@app.post("/proposals/{proposal_id}/approve", tags=["proposals"],
          summary="Authorise this version; execute only if current conditions still support it")
def approve(proposal_id: str, body: ApproveRequest, request: Request) -> dict[str, Any]:
    if body.confirm is not True:
        raise HTTPException(status_code=400, detail="approval requires explicit confirm=true")
    return _ok(_service.approve, proposal_id, body.note,
               identity=request.state.identity, version=body.version,
               request_id=str(body.idempotency_key),
               terms=body.model_dump(include={"ticker", "bucket", "action", "notional_usd", "sell_quantity"}),
               authority_version=body.authority_version,
               acknowledge_authority_change=body.acknowledge_authority_change)


class ContinueExitRequest(BaseModel):
    """Take up the unsold part of an exit. Creates a proposal; approves and executes nothing."""

    model_config = ConfigDict(extra="forbid")
    confirm: StrictBool
    #: One continuation per id. A repeated tap returns the one it already made; the same id
    #: pointed at another exit is refused rather than quietly making a second branch.
    request_id: UUID
    authority_version: str | None = Field(None, min_length=1, max_length=32)
    acknowledge_authority_change: bool = False


@app.post("/proposals/{proposal_id}/continue_exit", tags=["proposals"],
          summary="Prepare the unsold remainder of this exit as a proposal for a person")
def continue_exit(proposal_id: str, body: ContinueExitRequest,
                  request: Request) -> ExitContinuation:
    if body.confirm is not True:
        raise HTTPException(status_code=400, detail="continuing an exit requires explicit confirm=true")
    return _ok(_service.continue_exit, proposal_id, request_id=str(body.request_id),
               identity=request.state.identity, authority_version=body.authority_version,
               acknowledge_authority_change=body.acknowledge_authority_change)


class RejectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str | None = Field(None, max_length=500)


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


@app.get("/orders", tags=["orders"], summary="Recorded fills and the orders this system is tracking")
def orders(limit: Annotated[int, Query(ge=1, le=200)] = 50) -> Orders:
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
_ASSET_REF = re.compile(r'((?:src|href)=")((?:assets|icons)/[^"?#]+)(?:\?[^"#]*)?(")')

_PRIMARY_PAGES = ("index", "advisor", "more", "risk", "activity", "evaluation",
                  "profile", "notifications", "system", "approvals", "lab", "settings", "auto")


class ConsoleFiles(StaticFiles):
    """The console's files, with every page's asset references stamped by release.

    Still no build step: the pages on disk name ``assets/console.js`` and nothing more, and
    are rewritten on the way out so that the name carries the files it belongs to,
    ``assets/console.js?v=8f0c…``, which is what lets :func:`_cache_policy` hand that URL to
    the browser as immutable. In a release the stamp is a digest of what the asset files
    contain, taken once, since a release never changes: a release that changes no asset keeps
    the stamp, so a phone keeps the console it has instead of downloading all of it again. It
    was the release's name, and on a day of eleven releases every device fetched the whole
    console eleven times. In a checkout run by hand it is a digest of the asset files' sizes and
    modification times, so an edit shows on the next load without anyone remembering to bump
    anything. A page carries an ETag over its rewritten body, so a page that has not changed
    costs one round trip and no bytes.
    """

    def __init__(self, directory: Path):
        super().__init__(directory=str(directory), html=True)
        self.root = directory
        self.release = directory.parent / "RELEASE.json"
        self._release_stamp: str | None = None
        self._pages: dict[str, tuple[tuple, bytes, str]] = {}

    def stamp(self) -> str:
        if self._release_stamp is None:
            self._release_stamp = self._digest(contents=True) if self.release.is_file() else ""
        return self._release_stamp or self._digest(contents=False)

    def _digest(self, *, contents: bool) -> str:
        digest = hashlib.sha256()
        for folder in ("assets", "icons"):
            for base, dirs, files in os.walk(self.root / folder):
                dirs.sort()
                for name in sorted(files):
                    path = os.path.join(base, name)
                    if contents:
                        with open(path, "rb") as handle:
                            marker = hashlib.sha256(handle.read()).hexdigest()
                    else:
                        info = os.stat(path)
                        marker = f"{info.st_mtime_ns}:{info.st_size}"
                    digest.update(f"{os.path.relpath(path, self.root)}:{marker}\n".encode())
        return digest.hexdigest()[:12]

    def primary_templates(self, page: str, name: str) -> str:
        """Ship console layouts in the initial response, before navigation runs.

        Inert templates contain no account data. Even a first tap on a slow network can
        switch the layout while that page's script or API reads are still in flight.
        """
        if name not in _PRIMARY_PAGES:
            return page
        templates = []
        for other in _PRIMARY_PAGES:
            if other == name or not (self.root / f"{other}.html").is_file():
                continue
            source = (self.root / f"{other}.html").read_text(encoding="utf-8")
            body = re.search(r'<body([^>]*)>(.*?)</body>', source, re.DOTALL)
            title = re.search(r'<title>(.*?)</title>', source, re.DOTALL)
            classes = re.search(r'class="([^"]*)"', body[1])
            markup = re.sub(r'<script\b[^>]*>.*?</script>', '', body[2], flags=re.DOTALL)
            templates.append(f'<template data-ct-page="{other}" '
                             f'data-title="{escape(title[1], quote=True)}" '
                             f'data-class="{escape(classes[1] if classes else "", quote=True)}">'
                             f'{markup}</template>')
        page = page.replace('<script src="assets/i18n.js"', ''.join(templates) + '\n<script src="assets/i18n.js"', 1)
        if 'href="assets/advisor.css"' not in page:
            page = page.replace('</head>', '<link rel="stylesheet" href="assets/advisor.css" />\n</head>')
        if 'href="assets/lab.css"' not in page:
            page = page.replace('</head>', '<link rel="stylesheet" href="assets/lab.css" />\n</head>')
        if 'href="assets/portfolio.css"' not in page:
            page = page.replace('</head>', '<link rel="stylesheet" href="assets/portfolio.css" />\n</head>')
        startup = self.root / 'assets' / 'startup.html'
        if startup.is_file():
            page = re.sub(r'<html\b', '<html data-ct-starting', page, count=1)
            page = page.replace('</head>', '<link rel="stylesheet" href="assets/startup.css" />\n</head>')
            page = re.sub(r'(<body[^>]*>)', lambda m: m[1] + '\n' + startup.read_text(encoding='utf-8'), page, count=1)
        return page

    def file_response(self, full_path, stat_result, scope, status_code=200):
        if status_code != 200 or not str(full_path).endswith(".html"):
            return super().file_response(full_path, stat_result, scope, status_code)
        name = Path(full_path).stem
        layouts = tuple((p.stat().st_mtime_ns, p.stat().st_size)
                        for item in _PRIMARY_PAGES if (p := self.root / f"{item}.html").is_file())
        key = (stat_result.st_mtime_ns, stat_result.st_size, self.stamp(), layouts)
        cached = self._pages.get(str(full_path))
        if cached is None or cached[0] != key:
            page = self.primary_templates(Path(full_path).read_text(encoding="utf-8"), name)
            body = _ASSET_REF.sub(lambda m: f"{m.group(1)}{m.group(2)}?v={key[2]}{m.group(3)}", page).encode()
            cached = (key, body, '"' + hashlib.sha256(body).hexdigest()[:32] + '"')
            self._pages[str(full_path)] = cached
        _, body, etag = cached
        headers = {"etag": etag}
        offered = Headers(scope=scope).get("if-none-match", "")
        if etag in [tag.strip().removeprefix("W/") for tag in offered.split(",")]:
            return Response(status_code=304, headers=headers)
        if scope["method"] == "HEAD":
            return Response(b"", headers={**headers, "content-length": str(len(body))}, media_type="text/html")
        return Response(body, headers=headers, media_type="text/html")


class _WeakTags:
    """Marks the console's ETags weak.

    A file goes out compressed or not depending on who asks, so its tag cannot name exact
    bytes, and a HEAD, which is never compressed, must still carry the tag a GET would. A weak
    tag is all a revalidation needs: :meth:`ConsoleFiles.file_response` and Starlette's static
    files both compare weakly.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        async def sending(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(raw=message["headers"])
                tag = headers.get("etag")
                if tag and not tag.startswith("W/"):
                    headers["etag"] = "W/" + tag
            await send(message)

        await self.app(scope, receive, sending)


_UI_DIR = Path(__file__).resolve().parents[2] / "ui"
if _UI_DIR.is_dir():
    # Compressed, because a phone downloads the whole console again after a release: about
    # 600 KB of script, style and page that gzip brings under 180 KB. Only the console's own
    # files, never an API answer: those are small, and they carry the session's CSRF token.
    app.mount("/ui", _WeakTags(GZipMiddleware(ConsoleFiles(_UI_DIR), minimum_size=1024,
                                             compresslevel=6)), name="ui")
    logger.info("console mounted at /ui from %s", _UI_DIR)
