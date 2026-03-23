"""Information sources beside prices: what was asked of each one, and what came back.

Two sources, both free and both auxiliary (docs/todo.md, P2.6): Reddit, which the upstream
sentiment analyst already reads, and Form 4 filings read from the SEC's own EDGAR. Neither can
approve, size or refuse an order. Research reads them and proposes; ``risk/``, ``policy.py`` and
``execution.py`` decide what may happen and import nothing from here (a test holds that).

What this package adds is not the reading. It is the difference between three things a prompt
used to receive as one: the source answered and had something, the source answered and had
nothing, and the source could not be read. Upstream's Reddit reader returns "no posts found"
for all of the last two, and its analyst is told that silence from every source is "Neutral".
A blocked feed therefore read as a calm market. Every read here ends in exactly one outcome
from the list below, and only :data:`ANSWERED` says anything about the subject.

Each read is also written down (:func:`record`): which source, for what, when it was asked,
each request and how it ended, and for every item its link, the time the source says it was
published, and the time this system first saw it. A conclusion that used an item can be traced
to that line; an item nobody could have had yet cannot be used and is not there.

Both sources are off unless ``research_web_access.sources.<name>.enabled`` is literally
``true`` in the risk profile. Absent means off. The switches sit inside that existing section
on purpose: the console's Settings page may change what is inside a section but may not add a
new one, so a section of their own could only ever have been edited by hand.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import urllib.error
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

REDDIT, FORM4 = "reddit", "sec_form4"
#: Where the settings live in the risk profile.
SETTINGS = ("research_web_access", "sources")


def switch(name: str) -> str:
    """The setting that turns a source on, as a person would type it."""
    return ".".join((*SETTINGS, name, "enabled"))

# How one read ended. The first two are answers; everything else is the absence of one.
OK = "ok"
EMPTY = "empty"
PARTIAL = "partial"
RESTRICTED = "access_restricted"
RATE_LIMITED = "rate_limited"
NOT_FOUND = "not_found"
TIMEOUT = "timeout"
UNREACHABLE = "unreachable"
SERVER_ERROR = "server_error"
MALFORMED = "malformed"
BUDGET = "request_budget_spent"
NOT_CONFIGURED = "not_configured"
NOT_APPLICABLE = "not_applicable"
DISABLED = "disabled"
ADAPTER_ERROR = "adapter_error"

#: The outcomes that are a statement about the subject. A caller that treats anything else as
#: "nothing to report" has turned a failure into evidence.
ANSWERED = (OK, EMPTY)

SAYS: dict[str, tuple[str, str]] = {
    OK: ("来源有应答，并且有内容。", "The source answered and had something."),
    EMPTY: ("来源有应答，但没有相关内容。", "The source answered and had nothing for this."),
    PARTIAL: ("只读到一部分，其余没有读到。", "Part of what was asked came back and part did not."),
    RESTRICTED: ("来源拒绝访问。", "The source refused access."),
    RATE_LIMITED: ("来源限制了请求频率。", "The source is limiting how often it may be asked."),
    NOT_FOUND: ("来源说没有这个地址。", "The source says there is nothing at that address."),
    TIMEOUT: ("来源在时限内没有应答。", "The source did not answer in time."),
    UNREACHABLE: ("连不上来源。", "The source could not be reached."),
    SERVER_ERROR: ("来源自己出错了。", "The source failed on its own side."),
    MALFORMED: ("来源的应答读不懂。", "The source answered with something that could not be read."),
    BUDGET: ("本次读取的请求次数已用完。", "This read had used all the requests it is allowed."),
    NOT_CONFIGURED: ("这个来源还没有配置好。", "This source is not set up on this deployment."),
    NOT_APPLICABLE: ("这个来源不适用于这个标的。", "This source does not apply to this subject."),
    DISABLED: ("这个来源在本部署中是关闭的。", "This source is switched off on this deployment."),
    ADAPTER_ERROR: ("读取这个来源的代码出错了。", "The code that reads this source failed."),
}

#: The environment variable naming the file a run's reads are appended to. A tool that runs in
#: its own process (the MCP tool server) is given it; a read made in the worker is given the path.
LOG_ENV = "CT_SOURCES_LOG"
#: The date a run is an analysis *of*, for a tool that must not read past it.
AS_OF_ENV = "CT_RESEARCH_AS_OF"
#: Sources to leave out of this process's research, by name, whatever the profile says. It can
#: only narrow: a source the profile has off stays off. It is how a control is run beside a
#: run that reads a source, with nothing else changed (``ct-research --without-sources``).
WITHOUT_ENV = "CT_RESEARCH_WITHOUT_SOURCES"
LOG_NAME = "sources.jsonl"


def says(outcome: str) -> dict[str, str]:
    zh, en = SAYS.get(outcome, SAYS[ADAPTER_ERROR])
    return {"zh": zh, "en": en}


def classify(exc: BaseException) -> tuple[str, str]:
    """(outcome, short detail) for an exception raised while asking a source for something.

    The order matters: an HTTP error is also a URL error and an OS error, and a timeout arrives
    either as itself or wrapped as the reason of a URL error.
    """
    if isinstance(exc, urllib.error.HTTPError):
        status = int(exc.code)
        if status == 429:
            return RATE_LIMITED, "HTTP 429"
        if status in (404, 410):
            return NOT_FOUND, f"HTTP {status}"
        if status >= 500:
            return SERVER_ERROR, f"HTTP {status}"
        # 401, 403, 451 and any other refusal of the request as it was sent.
        return RESTRICTED, f"HTTP {status}"
    reason = getattr(exc, "reason", None)
    if isinstance(exc, TimeoutError) or isinstance(reason, TimeoutError) or "timed out" in str(exc).lower():
        return TIMEOUT, "no answer in time"
    if isinstance(exc, (ElementTree.ParseError, json.JSONDecodeError, UnicodeDecodeError)):
        return MALFORMED, type(exc).__name__
    if isinstance(exc, http.client.HTTPException):
        return UNREACHABLE, f"transfer broke off ({type(exc).__name__})"
    if isinstance(exc, OSError):
        return UNREACHABLE, type(reason or exc).__name__
    return ADAPTER_ERROR, f"{type(exc).__name__}: {exc}"[:160]


# ------------------------------------------------------------------------------- settings
@dataclass(frozen=True)
class RedditSettings:
    enabled: bool = False
    #: Empty means the list upstream reads by default.
    subreddits: tuple[str, ...] = ()
    #: One read is one request per subreddit and at most one retry each.
    max_requests: int = 6
    pause_seconds: float = 1.0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Form4Settings:
    enabled: bool = False
    #: The *name* of the variable holding the contact the SEC asks every client to declare.
    #: The value names this deployment, so it lives in ``.env`` and never in a tracked file.
    contact_env: str = "CT_SEC_CONTACT"
    lookback_days: int = 180
    max_filings: int = 12
    max_requests: int = 30
    timeout_seconds: int = 20
    #: Which upstream analyst is handed the tool. Fundamentals runs for stocks only, which is
    #: what keeps a fund's research from being asked about insiders it does not have.
    analyst: str = "fundamentals"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Settings:
    reddit: RedditSettings
    form4: Form4Settings

    def as_dict(self) -> dict[str, Any]:
        return {REDDIT: self.reddit.as_dict(), FORM4: self.form4.as_dict()}


def _positive(value: Any, default: int, *, least: int = 1, most: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(least, min(most, number))


def load(profiles: Any) -> Settings:
    """The sources' settings from the risk profile. A source is on only if it literally says so."""
    def cfg(*keys: str, default: Any = None) -> Any:
        return profiles.risk_cfg(*SETTINGS, *keys, default=default)

    # A name goes into a URL that upstream builds, so only what a subreddit can be called is
    # kept; anything else is dropped here rather than escaped there.
    subs = [str(s).strip().strip("/").removeprefix("r/")
            for s in (cfg(REDDIT, "subreddits", default=[]) or [])]
    subs = [s for s in subs if re.fullmatch(r"[A-Za-z0-9_]{2,21}", s)]
    try:
        pause = max(0.0, min(30.0, float(cfg(REDDIT, "pause_seconds", default=1.0))))
    except (TypeError, ValueError):
        pause = 1.0
    left_out = {name.strip() for name in os.environ.get(WITHOUT_ENV, "").split(",")}
    reddit = RedditSettings(
        enabled=cfg(REDDIT, "enabled", default=False) is True and REDDIT not in left_out,
        subreddits=tuple(dict.fromkeys(subs)),
        max_requests=_positive(cfg(REDDIT, "max_requests", default=6), 6, most=24),
        pause_seconds=pause,
    )
    form4 = Form4Settings(
        enabled=cfg(FORM4, "enabled", default=False) is True and FORM4 not in left_out,
        contact_env=str(cfg(FORM4, "contact_env", default="CT_SEC_CONTACT") or "CT_SEC_CONTACT"),
        lookback_days=_positive(cfg(FORM4, "lookback_days", default=180), 180, most=730),
        max_filings=_positive(cfg(FORM4, "max_filings", default=12), 12, most=40),
        max_requests=_positive(cfg(FORM4, "max_requests", default=30), 30, most=60),
        timeout_seconds=_positive(cfg(FORM4, "timeout_seconds", default=20), 20, least=2, most=60),
        analyst=str(cfg(FORM4, "analyst", default="fundamentals") or "fundamentals"),
    )
    return Settings(reddit=reddit, form4=form4)


def in_effect(profiles: Any, instrument_types: tuple[str, ...] | list[str]) -> dict[str, str]:
    """Which of these sources reach the research of the given kinds of instrument.

    A source is an input only where an analyst that reads it runs: Reddit through the sentiment
    analyst, Form 4 through whichever analyst is handed the tool. Funds are researched by
    neither, so for a plan that buys only funds this is empty whatever the switches say -- and
    turning a source on does not make a new strategy of one whose research never sees it.
    """
    settings = load(profiles)
    reads: dict[str, str] = {}
    for kind in dict.fromkeys(str(k) for k in instrument_types):
        analysts = set(profiles.analysts_for(kind))
        if settings.reddit.enabled and "social" in analysts:
            reads[REDDIT] = "sentiment analyst"
        if settings.form4.enabled and settings.form4.analyst in analysts:
            reads[FORM4] = f"{settings.form4.analyst} analyst"
    return dict(sorted(reads.items()))


def cache_dir(profiles: Any) -> Path:
    """Where what was fetched is kept between runs. Beside the runs, outside git, never state."""
    return Path(profiles.project_dir) / "runs" / ".sources"


# --------------------------------------------------------------------------- the run's log
def now_iso(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).astimezone(UTC).isoformat(timespec="seconds")


def record(reading: dict[str, Any], path: str | os.PathLike[str] | None = None) -> None:
    """Append one read to the run's log. Like the usage audit, it must never break an analysis."""
    target = path or os.environ.get(LOG_ENV)
    if not target:
        return
    try:
        file = Path(target)
        file.parent.mkdir(parents=True, exist_ok=True)
        with file.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(reading, ensure_ascii=False, default=str) + "\n")
    except OSError:
        pass


def readings(path: str | os.PathLike[str] | None) -> list[dict[str, Any]]:
    """Every read a run made, oldest first. A line that cannot be parsed is skipped, not fatal."""
    if not path:
        return []
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Per source: how many reads, how each ended, how many items. For a record's front page."""
    out: dict[str, Any] = {}
    for row in rows:
        entry = out.setdefault(str(row.get("source")), {"reads": 0, "outcomes": {}, "items": 0})
        entry["reads"] += 1
        outcome = str(row.get("outcome"))
        entry["outcomes"][outcome] = entry["outcomes"].get(outcome, 0) + 1
        entry["items"] += len(row.get("items") or [])
    return out
