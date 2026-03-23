"""Reddit, as upstream reads it, with each request watched.

Nothing here fetches Reddit by itself, and nothing of upstream's reader is copied. Upstream's
``fetch_reddit_posts`` is called exactly as its sentiment analyst calls it; what this module
does is stand where that function reaches for the network, so that what came back can be told
apart afterwards:

* upstream turns every failure into an empty list, and an empty list into the sentence "no
  posts found". A feed that answered 403 and a ticker nobody mentioned reach the model as the
  same words, and the analyst's own instructions call silence from every source "Neutral";
* so each subreddit is asked for separately, through the same upstream function, and the
  request it makes is recorded: the status, how many entries, how it failed if it did. The
  text handed on is upstream's own wherever a feed answered, and says *unavailable* -- the
  word upstream's prompt already tells the analyst to treat as a gap -- wherever one did not.

The channel is the public search feed, read without a key. Whether that is a channel this
deployment may use is not something code can decide. It is a setting,
``research_web_access.sources.reddit.enabled``, off unless a person turns it on; and when it
is off no request is made and the analyst is told the source is unavailable rather than quiet.
Reddit was reported on 2026-09-30 to have announced that it stops serving these feeds on
2026-11-13 (docs/todo.md, P2.6). From that day every read here ends in an outcome that says
it could not be read, which is the point of the module.

Posts are opinion from anonymous accounts. Their text goes to the analyst and is not kept:
what is written down is each post's link, the time Reddit gives for it and the time it was
first seen here, and the last of those is forgotten after :data:`SEEN_DAYS`.
"""

from __future__ import annotations

import io
import re
import time as _time
import urllib.request
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Self
from unittest import mock
from urllib.parse import parse_qs, urlsplit
from xml.etree import ElementTree

from . import (
    ADAPTER_ERROR,
    ANSWERED,
    BUDGET,
    DISABLED,
    EMPTY,
    MALFORMED,
    OK,
    PARTIAL,
    REDDIT,
    RedditSettings,
    cache_dir,
    classify,
    load,
    now_iso,
    record,
    says,
    seen,
    switch,
)

CHANNEL = "public search feed (Atom), no key, through tradingagents.dataflows.reddit"
KIND = "opinion"
#: How long the time a post was first seen is remembered. Posts are people's words and can be
#: deleted by the people who wrote them; nothing about one is kept here longer than a month.
SEEN_DAYS = 30
_ATOM = {"atom": "http://www.w3.org/2005/Atom"}
_FALLBACK_CAP = 5 * 1024 * 1024
#: The markers upstream's prompt puts round each block of fetched text. A post that contained
#: one could end the block early and have the rest of itself read as part of the prompt.
_MARKER = re.compile(r"<\s*(/?)\s*(start|end)_of_", re.IGNORECASE)


def upstream() -> Any:
    from tradingagents.dataflows import reddit as module

    return module


def leans_on() -> dict[str, bool]:
    """What this module needs of the pinned upstream, by name. A test reads this after a bump."""
    module = upstream()
    from tradingagents.agents.analysts import sentiment_analyst

    return {
        "fetch_reddit_posts": callable(getattr(module, "fetch_reddit_posts", None)),
        "urlopen": hasattr(module, "urlopen"),
        "time": hasattr(module, "time"),
        "DEFAULT_SUBREDDITS": bool(getattr(module, "DEFAULT_SUBREDDITS", ())),
        "analyst_calls_it_by_name": hasattr(sentiment_analyst, "fetch_reddit_posts"),
    }


class _Kept(io.BytesIO):
    """A response already read, handed to upstream to read again."""

    status = 200

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class _Clock:
    """Upstream's ``time``, with the waiting done by whoever is running the read."""

    def __init__(self, sleep: Callable[[float], None]):
        self._sleep = sleep

    def sleep(self, seconds: float) -> None:
        self._sleep(seconds)

    def __getattr__(self, name: str) -> Any:
        return getattr(_time, name)


class _Watch:
    """Stands in for ``urlopen`` inside upstream's reader and writes down each request."""

    def __init__(self, opener: Callable[..., Any], *, max_requests: int, cap: int):
        self._open = opener
        self._max = max_requests
        self._cap = cap
        self.made = 0
        self.known: set[str] = set()
        self.requests: list[dict[str, Any]] = []
        self.entries: list[dict[str, Any]] = []
        self.dropped = 0

    def __call__(self, request: Any, timeout: float | None = None, **more: Any) -> Any:
        url = getattr(request, "full_url", None) or str(request)
        row: dict[str, Any] = {"url": url, "at": now_iso()}
        self.requests.append(row)
        if self.made >= self._max:
            row.update(outcome=BUDGET, detail=f"{self._max} requests were allowed for this read")
            raise OSError("request budget for this read is spent")
        self.made += 1
        try:
            with self._open(request, timeout=timeout, **more) as response:
                row["http_status"] = int(getattr(response, "status", 200) or 200)
                raw = response.read(self._cap + 1)
        except Exception as exc:
            outcome, detail = classify(exc)
            row.update(outcome=outcome, detail=detail)
            if getattr(exc, "code", None):
                row["http_status"] = int(exc.code)
            raise
        row["bytes"] = len(raw)
        if len(raw) > self._cap:
            row.update(outcome=MALFORMED, detail="the feed was larger than a feed is")
            return _Kept(raw)
        try:
            root = ElementTree.fromstring(raw)
        except ElementTree.ParseError as exc:
            row.update(outcome=MALFORMED, detail=f"not a feed ({type(exc).__name__})")
            return _Kept(raw)
        # Upstream asks for a number of posts and keeps that many, whatever the feed sends.
        asked_for = parse_qs(urlsplit(url).query).get("limit", [""])[0]
        limit = int(asked_for) if asked_for.isdigit() and int(asked_for) > 0 else None
        kept, dropped = 0, 0
        for entry in root.findall("atom:entry", _ATOM):
            ident = (entry.findtext("atom:id", default="", namespaces=_ATOM) or "").strip()
            link = entry.find("atom:link", _ATOM)
            href = (link.get("href") if link is not None else "") or ""
            key = ident or href
            if key and key in self.known:
                # The same post under a second subreddit. Upstream would print it twice and
                # the analyst would count one voice as two.
                root.remove(entry)
                dropped += 1
                continue
            if limit is not None and kept >= limit:
                continue
            if key:
                self.known.add(key)
            kept += 1
            self.entries.append({
                "id": key, "url": href,
                "published_at": (entry.findtext("atom:published", default="",
                                                namespaces=_ATOM) or "").strip() or None,
            })
        self.dropped += dropped
        row.update(outcome=OK if kept else EMPTY, entries=kept)
        if dropped:
            row["duplicates_dropped"] = dropped
        return _Kept(ElementTree.tostring(root) if dropped else raw)


def _in_window(entries: list[dict[str, Any]], start: str | None, end: str | None) -> list[dict[str, Any]]:
    """The entries upstream will keep for this window, by upstream's own rule."""
    module = upstream()
    stamp = getattr(module, "_iso_to_timestamp", None)
    within = getattr(module, "_within_window", None)
    if not (start and end) or stamp is None or within is None:
        return entries
    posts = [{**entry, "created_utc": stamp(entry.get("published_at"))} for entry in entries]
    return [{k: v for k, v in post.items() if k != "created_utc"} for post in within(posts, start, end)]


def _clean(text: str) -> str:
    return _MARKER.sub(lambda m: f"<{m.group(1)}{m.group(2).lower()}-of-", text)


def _off(ticker: str) -> tuple[str, dict[str, Any]]:
    text = ("<reddit unavailable: this source is switched off on this deployment and no "
            "request was made. It says nothing about whether anyone is discussing "
            f"{ticker.upper()}.>")
    return text, {"source": REDDIT, "kind": KIND, "subject": ticker.upper(), "asked_at": now_iso(),
                  "outcome": DISABLED, "detail": f"{switch(REDDIT)} is not true",
                  "coverage": {"asked": [], "answered": [], "not_read": {}},
                  "counts": {"requests": 0, "entries": 0, "in_window": 0, "duplicates_dropped": 0},
                  "requests": [], "items": [], "channel": CHANNEL}


def read(ticker: str, *, settings: RedditSettings, start_date: str | None = None,
         end_date: str | None = None, cache: Path | None = None,
         opener: Callable[..., Any] | None = None, sleep: Callable[[float], None] = _time.sleep,
         now: datetime | None = None, **passed: Any) -> tuple[str, dict[str, Any]]:
    """(the block for the analyst's prompt, the record of the read). Never raises.

    ``passed`` is whatever else upstream's analyst handed its reader (a limit, a timeout); it
    goes on to upstream unchanged.
    """
    if not settings.enabled:
        return _off(ticker)
    asked_at = now_iso(now)
    try:
        module = upstream()
        subs = settings.subreddits or tuple(module.DEFAULT_SUBREDDITS)
        watch = _Watch(opener or urllib.request.urlopen, max_requests=settings.max_requests,
                       cap=int(getattr(module, "_MAX_FEED_BYTES", _FALLBACK_CAP)))
        clock = _Clock(sleep)
        parts: list[dict[str, Any]] = []
        for index, sub in enumerate(subs):
            if index and settings.pause_seconds:
                sleep(settings.pause_seconds)
            first_request, first_entry = len(watch.requests), len(watch.entries)
            with mock.patch.object(module, "urlopen", watch), mock.patch.object(module, "time", clock):
                text = module.fetch_reddit_posts(
                    ticker, subreddits=(sub,), start_date=start_date, end_date=end_date,
                    **{**passed, "inter_request_delay": 0.0})
            asked = watch.requests[first_request:]
            found = _in_window(watch.entries[first_entry:], start_date, end_date)
            last = asked[-1] if asked else {"outcome": ADAPTER_ERROR,
                                            "detail": "upstream made no request for this feed"}
            outcome = last["outcome"]
            if outcome in ANSWERED:
                outcome = OK if found else EMPTY
            parts.append({"subreddit": sub, "outcome": outcome, "detail": last.get("detail"),
                          "text": str(text), "items": found})
    except Exception as exc:  # noqa: BLE001 - a reader that broke is a gap, never a failed run
        text = (f"<reddit unavailable: the reader failed before it could finish "
                f"({type(exc).__name__}). This is a failure to read, not an absence of discussion.>")
        return text, {"source": REDDIT, "kind": KIND, "subject": ticker.upper(), "asked_at": asked_at,
                      "outcome": ADAPTER_ERROR, "detail": f"{type(exc).__name__}: {exc}"[:200],
                      "coverage": {"asked": [], "answered": [], "not_read": {}},
                      "counts": {"requests": 0, "entries": 0, "in_window": 0, "duplicates_dropped": 0},
                      "requests": [], "items": [], "channel": CHANNEL}

    name = ticker.upper()
    window = f"{start_date} to {end_date}" if start_date and end_date else "the past week"
    answered = [p for p in parts if p["outcome"] in ANSWERED]
    missed = [p for p in parts if p["outcome"] not in ANSWERED]
    items = [{**item, "subreddit": p["subreddit"]} for p in answered for item in p["items"]]

    def why(part: dict[str, Any]) -> str:
        detail = f" ({part['detail']})" if part.get("detail") else ""
        return says(part["outcome"])["en"].rstrip(".").lower() + detail

    if not answered:
        reasons = "; ".join(f"r/{p['subreddit']}: {why(p)}" for p in missed)
        block = (f"<reddit unavailable: none of the {len(parts)} feeds could be read ({reasons}). "
                 "This is a failure to read, not an absence of discussion.>")
    elif not items and not missed:
        feeds = ", ".join(f"r/{p['subreddit']}" for p in parts)
        block = (f"<no Reddit posts found mentioning {name} across {feeds} for {window}; "
                 f"all {len(parts)} feeds answered>")
    else:
        lines = ["Class: opinion from anonymous accounts. Unverified; a claim made here is a "
                 "rumour until a named source confirms it.\n"
                 f"Coverage: {len(answered)} of {len(parts)} subreddit feeds answered, "
                 f"{len(items)} post(s) for {window}."
                 + ("" if not missed else " Not read: "
                    + "; ".join(f"r/{p['subreddit']} ({why(p)})" for p in missed)
                    + ". A feed that was not read is a gap, not silence.")]
        for part in parts:
            if part["outcome"] == OK:
                lines.append(part["text"])
            elif part["outcome"] == EMPTY:
                lines.append(f"r/{part['subreddit']}: <no posts found mentioning {name} for "
                             f"{window}; the feed answered>")
            else:
                lines.append(f"r/{part['subreddit']}: <unavailable: {why(part)}>")
        block = "\n\n".join(lines)

    if not answered:
        codes = [p["outcome"] for p in missed]
        outcome = max(dict.fromkeys(codes), key=codes.count)
    elif missed:
        outcome = PARTIAL
    else:
        outcome = OK if items else EMPTY

    root = cache or None
    first = seen.first_seen(root, REDDIT, [i["id"] for i in items if i.get("id")], now=now) if root else {}
    if root:
        seen.forget(root, REDDIT, older_than_days=SEEN_DAYS, now=now)
    retrieved = now_iso(now)
    reading = {
        "source": REDDIT, "kind": KIND, "subject": name, "asked_at": asked_at,
        "window": {"start": start_date, "end": end_date},
        "outcome": outcome,
        "coverage": {"asked": [p["subreddit"] for p in parts],
                     "answered": [p["subreddit"] for p in answered],
                     "not_read": {p["subreddit"]: {"outcome": p["outcome"], "detail": p.get("detail")}
                                  for p in missed}},
        "counts": {"requests": watch.made, "entries": len(watch.entries), "in_window": len(items),
                   "duplicates_dropped": watch.dropped},
        "requests": watch.requests,
        # The link and the times, and nothing a person wrote.
        "items": [{"id": i.get("id"), "url": i.get("url"), "subreddit": i["subreddit"],
                   "published_at": i.get("published_at"),
                   "first_seen_at": first.get(str(i.get("id")), retrieved),
                   "retrieved_at": retrieved} for i in items],
        "channel": CHANNEL,
    }
    return _clean(block), reading


@contextmanager
def installed(profiles: Any, *, log_path: str | Path | None = None) -> Iterator[None]:
    """While open, the sentiment analyst's Reddit read goes through :func:`read`.

    Upstream's analyst calls ``fetch_reddit_posts`` by that name from its own module, and has
    no hook for replacing it, so the name is pointed here for the length of one run and put
    back. If a later upstream no longer has the name there is nothing to stand in for: the run
    goes on as upstream wrote it, and the log says its Reddit read was not watched.
    """
    try:
        from tradingagents.agents.analysts import sentiment_analyst as analyst
    except Exception:  # noqa: BLE001 - no analyst, nothing to watch
        yield
        return
    if not hasattr(analyst, "fetch_reddit_posts"):
        record({"source": REDDIT, "kind": KIND, "asked_at": now_iso(), "outcome": ADAPTER_ERROR,
                "detail": "upstream's sentiment analyst no longer reads Reddit through "
                          "fetch_reddit_posts; its read was not watched"}, log_path)
        yield
        return
    settings = load(profiles).reddit
    root = cache_dir(profiles)

    def through(ticker: str, *_positional: Any, **kwargs: Any) -> str:
        # Which feeds, and how fast, are this deployment's to say; the rest is upstream's.
        kwargs.pop("subreddits", None)
        kwargs.pop("inter_request_delay", None)
        text, reading = read(ticker, settings=settings, cache=root,
                             start_date=kwargs.pop("start_date", None),
                             end_date=kwargs.pop("end_date", None), **kwargs)
        record(reading, log_path)
        return text

    with mock.patch.object(analyst, "fetch_reddit_posts", through):
        yield
