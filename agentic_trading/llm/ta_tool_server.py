"""Serve TradingAgents' own data tools to the Claude Code CLI over MCP (stdio).

Launched by the CLI for each analyst call that has tools bound. It imports the
upstream tool functions unchanged, applies the same data-vendor configuration
the main process uses, and exposes only the tool names requested on the
command line. stdout is reserved for the MCP protocol, so anything a tool
prints is redirected to stderr.
"""

from __future__ import annotations

import argparse
import functools
import os
import sys
from collections.abc import Callable


def _quiet(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        real_stdout = sys.stdout
        sys.stdout = sys.stderr
        try:
            out = fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - report to the model, never kill the server
            return f"TOOL_ERROR: {type(exc).__name__}: {exc}"
        finally:
            sys.stdout = real_stdout
        return out if isinstance(out, str) else str(out)

    return wrapper


def _server_class():
    try:  # official MCP Python SDK 2.x
        from mcp.server.mcpserver import MCPServer

        return MCPServer
    except ModuleNotFoundError:  # SDK 1.x
        from mcp.server.fastmcp import FastMCP

        return FastMCP


# Tools this project owns rather than upstream. Consulted before the agent_utils lookup, so an
# unknown name still fails the same way. Each factory takes the loaded profiles and returns the
# callable, which lets a tool close over configuration without a second env var.
def _build_fetch_url(profiles):
    from agentic_trading.llm.web_fetch import build_fetch_tool

    return build_fetch_tool(profiles)


def _build_sec_form4(profiles):
    from agentic_trading.sources.sec_form4 import build_tool

    return build_tool(profiles)


LOCAL_TOOLS = {"fetch_url": _build_fetch_url, "get_sec_form4": _build_sec_form4}


# Answers an upstream tool gives that read as a fact about the market when they are a fact about
# the source. Rewritten after the call, keeping upstream's own words first; upstream is unchanged.
def _news_feed_said_nothing(answer: str) -> str:
    """Yahoo's per-symbol feed returning nothing at all is a gap, not a quiet week.

    Upstream answers "No news found for X" both when the feed is empty and, with "between A and
    B", when it had articles but none in the window. Only the first is rewritten. From about
    2026-10-02 the feed returned nothing for every symbol (Yahoo's change; upstream's fix,
    4429078, is after the commit this project pins), and research read the bare sentence as
    "nothing was reported" -- the opposite of what the project holds a source to: a feed that
    could not be read is never "nothing to report".
    """
    said = answer.strip()
    if not said.startswith("No news found for ") or " between " in said or "\n" in said:
        return answer
    return (f"{said}. Yahoo returned no articles at all for this symbol -- not even outside the "
            "dates asked for. For a listed fund or company that is a gap in the source, not a "
            "quiet period (Yahoo's per-symbol news feed went empty for every symbol in October "
            "2026). Treat company news as unavailable for this run, not as evidence that "
            "nothing was reported.")


ANSWER_NOTES: dict[str, Callable[[str], str]] = {"get_news": _news_feed_said_nothing}


def _served(name: str, fn):
    """The callable as the model is given it: quiet on stdout, and any answer note applied."""
    note = ANSWER_NOTES.get(name)
    quiet = _quiet(fn)
    if note is None:
        return quiet

    @functools.wraps(fn)
    def noted(*args, **kwargs):
        return note(quiet(*args, **kwargs))

    return noted


def build_server(tool_names: list[str], config_dir: str | None):
    from tradingagents.agents.utils import agent_utils
    from tradingagents.dataflows.config import set_config

    from agentic_trading.profile import build_tradingagents_config, load_profiles

    profiles = load_profiles(config_dir or "config")
    set_config(build_tradingagents_config(profiles))

    server = _server_class()("ta")
    for name in tool_names:
        if name in LOCAL_TOOLS:
            fn = LOCAL_TOOLS[name](profiles)
            description = fn.__doc__ or name
        else:
            tool = getattr(agent_utils, name, None)
            fn = getattr(tool, "func", None)
            if fn is None:
                raise SystemExit(f"unknown TradingAgents tool: {name}")
            description = getattr(tool, "description", None) or fn.__doc__ or name
        # structured_output=False: every upstream tool returns markdown/CSV text.
        server.add_tool(_served(name, fn), name=name, description=description, structured_output=False)
    return server


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="MCP server exposing TradingAgents data tools")
    parser.add_argument("--tools", required=True, help="comma-separated upstream tool names")
    args = parser.parse_args(argv)
    names = [n.strip() for n in args.tools.split(",") if n.strip()]
    server = build_server(names, os.environ.get("CT_CONFIG_DIR"))
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
