import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from agentic_trading.llm.claude_code import (
    USAGE_LOG,
    ClaudeCodeChatModel,
    ClaudeCodeError,
    render_messages,
    reset_usage_log,
    scrubbed_environment,
    usage_summary,
)


def test_render_single_human_message_passes_through():
    system, prompt = render_messages([SystemMessage(content="SYS"), HumanMessage(content="hello")])
    assert system == "SYS"
    assert prompt == "hello"


def test_render_transcript_keeps_roles_and_tool_results():
    ai = AIMessage(content="thinking", tool_calls=[{"name": "get_stock_data", "args": {"symbol": "X"}, "id": "1"}])
    system, prompt = render_messages([
        SystemMessage(content="A"), SystemMessage(content="B"), HumanMessage(content="q"),
        ai, ToolMessage(content="rows", tool_call_id="1", name="get_stock_data"),
    ])
    assert system == "A\n\nB"
    assert "[USER]\nq" in prompt and "[ASSISTANT]\nthinking" in prompt
    assert "get_stock_data" in prompt and "[TOOL RESULT get_stock_data]\nrows" in prompt
    assert prompt.endswith("Reply with your next message only.")


def test_environment_is_scrubbed_of_billing_and_nesting_vars():
    env = scrubbed_environment({"ANTHROPIC_API_KEY": "sk", "ANTHROPIC_AUTH_TOKEN": "t", "CLAUDECODE": "1",
                                "CLAUDE_CODE_USE_BEDROCK": "1", "PATH": "/bin"})
    assert env == {"PATH": "/bin"}


@pytest.mark.parametrize("name", ["ANTHROPIC_BASE_URL", "ANTHROPIC_CUSTOM_HEADERS",
                                  "ANTHROPIC_BEDROCK_BASE_URL", "ANTHROPIC_VERTEX_BASE_URL"])
def test_a_redirected_endpoint_never_reaches_the_cli(name):
    """The subscription's OAuth token and every prompt go wherever these point."""
    assert scrubbed_environment({name: "https://proxy.example", "PATH": "/bin"}) == {"PATH": "/bin"}


def test_invoke_uses_subscription_cli_with_builtin_tools_disabled(fake_claude, tmp_path):
    reset_usage_log()
    llm = ClaudeCodeChatModel(model="claude-sonnet-5", effort="low", work_dir=str(tmp_path))
    msg = llm.invoke([SystemMessage(content="be brief"), HumanMessage(content="hello there")])
    assert "FAKE RESPONSE model=claude-sonnet-5" in msg.content
    assert "BUILTIN_TOOLS_DISABLED" in msg.content and "STRICT_MCP" in msg.content
    assert "SYSTEM_LEN=8" in msg.content
    assert msg.response_metadata["billing"] == "claude_subscription_via_claude_code_cli"
    assert len(USAGE_LOG) == 1 and usage_summary()["calls"] == 1


def test_api_key_in_callers_shell_never_reaches_the_cli(fake_claude, monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-never-be-used")
    llm = ClaudeCodeChatModel(model="claude-sonnet-5", work_dir=str(tmp_path))
    assert "FAKE RESPONSE" in llm.invoke("hello").content


def test_usage_limit_raises_and_never_falls_back(fake_claude, tmp_path):
    llm = ClaudeCodeChatModel(model="claude-sonnet-5", work_dir=str(tmp_path))
    with pytest.raises(ClaudeCodeError) as info:
        llm.invoke("FORCE_LIMIT please")
    assert info.value.rate_limited is True


def test_structured_output_parses_upstream_schema(fake_claude, tmp_path):
    from tradingagents.agents.schemas import PortfolioDecision, PortfolioRating

    llm = ClaudeCodeChatModel(model="claude-opus-5", work_dir=str(tmp_path))
    decision = llm.with_structured_output(PortfolioDecision).invoke("decide")
    assert isinstance(decision, PortfolioDecision)
    assert decision.rating == PortfolioRating.BUY  # first enum value from the fake


def test_bound_tools_are_served_over_mcp_and_audited(fake_claude, tmp_path):
    from tradingagents.agents.utils.agent_utils import get_indicators, get_stock_data

    reset_usage_log()
    llm = ClaudeCodeChatModel(model="claude-sonnet-5", work_dir=str(tmp_path), config_dir="config",
                              progress_path=str(tmp_path / "progress.jsonl"))
    bound = llm.bind_tools([get_stock_data, get_indicators])
    msg = bound.invoke("analyze")
    assert "ALLOWED=mcp__ta__get_stock_data,mcp__ta__get_indicators" in msg.content
    calls = msg.response_metadata["tool_calls_executed"]
    assert calls and calls[0]["tool"] == "mcp__ta__get_stock_data"
    finished = [json.loads(line) for line in (tmp_path / "progress.jsonl").read_text().splitlines()
                if json.loads(line)["kind"] == "call_finished"]
    assert finished and finished[-1]["tools"] == ["mcp__ta__get_stock_data"]
    assert "more chars" in calls[0]["result_excerpt"]
    assert msg.tool_calls == []  # the loop ran inside the CLI; LangGraph sees a finished report
    assert USAGE_LOG[-1]["num_turns"] == 2


def test_mcp_config_points_at_our_tool_server(tmp_path):
    llm = ClaudeCodeChatModel(model="claude-sonnet-5", work_dir=str(tmp_path), config_dir="/cfg",
                              tool_names=["get_news"])
    args = llm._mcp_args()
    cfg = json.loads(args[args.index("--mcp-config") + 1])
    server = cfg["mcpServers"]["ta"]
    assert server["args"][:2] == ["-m", "agentic_trading.llm.ta_tool_server"]
    assert server["args"][-1] == "get_news" and server["env"]["CT_CONFIG_DIR"] == "/cfg"


@pytest.mark.parametrize("text", [
    "You've hit your session limit \u00b7 resets 1:30pm (America/Los_Angeles)",
    "You've hit your weekly limit. Resets Monday.",
    "Rate limit exceeded (429)",
    "Usage limit reached for this period",
])
def test_real_limit_wordings_are_classified(text):
    from agentic_trading.llm.claude_code import _LIMIT_RE

    assert _LIMIT_RE.search(text)


# -- web access scoping ------------------------------------------------------------
#
# The adapter's "built-in tools are disabled" property becomes conditional here: --tools is
# empty on every call except the news analyst's, and that exception is derived in code from an
# exact match on the bound tool names. These tests are what makes the exception auditable.

NEWS_TOOLS = ["get_news", "get_global_news", "get_macro_indicators", "get_prediction_markets"]
MARKET_TOOLS = ["get_stock_data", "get_indicators", "get_verified_market_snapshot"]
BOTH_ON = {"fetch_enabled": True, "search_enabled": True, "extra_turns": 6}


def _flag(args, name):
    return args[args.index(name) + 1] if name in args else None


def _bind(names, policy=None, **kwargs):
    llm = ClaudeCodeChatModel(model="claude-sonnet-5", config_dir="/cfg", web_policy=policy, **kwargs)
    return llm.bind_tools([{"name": n} for n in names])


def test_a_call_with_no_tools_still_disables_builtin_tools():
    args = ClaudeCodeChatModel(model="claude-sonnet-5", claude_bin="/bin/true")._build_command("")
    assert _flag(args, "--tools") == ""
    assert "--allowedTools" not in args


def test_the_news_analyst_gets_the_cli_web_tools_only_when_search_is_enabled():
    bound = _bind(NEWS_TOOLS, BOTH_ON, claude_bin="/bin/true")
    assert bound.analyst_role == "news"
    args = bound._build_command("")
    assert _flag(args, "--tools") == "WebSearch,WebFetch"
    assert "WebSearch" in _flag(args, "--allowedTools")

    off = _bind(NEWS_TOOLS, {"fetch_enabled": True, "search_enabled": False}, claude_bin="/bin/true")
    assert _flag(off._build_command(""), "--tools") == ""


@pytest.mark.parametrize("names", [MARKET_TOOLS, ["get_fundamentals", "get_balance_sheet",
                                                 "get_cashflow", "get_income_statement"]])
def test_no_other_analyst_ever_gets_the_cli_web_tools(names):
    bound = _bind(names, BOTH_ON, claude_bin="/bin/true")
    assert bound.analyst_role in ("market", "fundamentals")
    assert _flag(bound._build_command(""), "--tools") == ""


def test_the_allowlisted_fetch_tool_is_added_for_an_analyst_and_only_when_enabled():
    assert "fetch_url" in _bind(MARKET_TOOLS, BOTH_ON).tool_names
    assert "fetch_url" not in _bind(MARKET_TOOLS, {"fetch_enabled": False}).tool_names
    # Not an analyst: the personal assistant's overlapping set matches no signature.
    assert "fetch_url" not in _bind(
        ["get_verified_market_snapshot", "get_news", "get_fundamentals", "get_indicators"],
        BOTH_ON).tool_names


def test_the_briefing_names_every_allowlisted_domain(profiles):
    # The upstream prompt renders "you have access to" from the list it binds itself, so
    # fetch_url -- appended afterwards -- is absent from it, and the tool's description names
    # no domain. Fifteen runs called it zero times. The briefing is the only place the model
    # learns where it may point the tool, and it is built from config, so a domain dropped
    # there disappears from the prompt rather than becoming a stale promise.
    from agentic_trading.llm.web_fetch import load_policy

    policy = load_policy(profiles)
    text = _bind(NEWS_TOOLS, policy.as_dict())._web_briefing()
    assert policy.allowed_domains
    for domain in policy.allowed_domains:
        assert domain in text
    assert "mcp__ta__fetch_url" in text
    assert str(policy.max_calls) in text


def test_the_briefing_is_silent_wherever_the_tool_is():
    # Same gate as the tool itself: no analyst role, or fetch disabled, grants no briefing.
    assert _bind(NEWS_TOOLS, {"fetch_enabled": False})._web_briefing() == ""
    assert _bind(["get_verified_market_snapshot", "get_news", "get_fundamentals",
                  "get_indicators"], BOTH_ON)._web_briefing() == ""


def test_the_briefing_reaches_the_cli_system_prompt(fake_claude, tmp_path, profiles):
    from agentic_trading.llm.web_fetch import load_policy

    bound = _bind(NEWS_TOOLS, load_policy(profiles).as_dict(), work_dir=str(tmp_path))
    briefing = bound._web_briefing()
    msg = bound.invoke([SystemMessage(content="be brief"), HumanMessage(content="hi")])
    # "be brief" is 8 characters; the briefing is appended after a blank line.
    assert f"SYSTEM_LEN={8 + 2 + len(briefing)}" in msg.content


def test_builtin_tools_are_allowlisted_even_with_no_mcp_tools_bound():
    # _mcp_args used to be the only source of --allowedTools, so a built-in tool on a call with
    # no MCP tools would have been offered and then denied, with no way to prompt in -p.
    llm = ClaudeCodeChatModel(model="claude-sonnet-5", claude_bin="/bin/true",
                              analyst_role="news", web_policy=BOTH_ON)
    args = llm._build_command("")
    assert "--mcp-config" not in args
    assert _flag(args, "--allowedTools") == "WebSearch,WebFetch"


def test_only_a_searching_call_gets_the_extra_turn_allowance():
    searching = _bind(NEWS_TOOLS, BOTH_ON, max_turns=12, claude_bin="/bin/true")
    assert _flag(searching._build_command(""), "--max-turns") == "18"
    fetching = _bind(MARKET_TOOLS, BOTH_ON, max_turns=12, claude_bin="/bin/true")
    assert _flag(fetching._build_command(""), "--max-turns") == "12"


def test_a_web_search_result_block_is_captured_in_the_audit_trail():
    from agentic_trading.llm.claude_code import _extract_tool_calls

    calls = _extract_tool_calls([
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "WebSearch", "input": {"query": "AAPL 10-Q"}}]}},
        {"type": "user", "message": {"content": [
            {"type": "web_search_tool_result", "tool_use_id": "t1",
             "content": [{"type": "text", "text": "three results"}]}]}},
    ])
    assert calls == [{"tool": "WebSearch", "input": {"query": "AAPL 10-Q"},
                      "result_excerpt": "three results"}]


def test_a_searching_news_call_reaches_the_cli_with_both_routes(fake_claude, tmp_path):
    llm = ClaudeCodeChatModel(model="claude-sonnet-5", work_dir=str(tmp_path), config_dir="config",
                              web_policy=BOTH_ON)
    msg = llm.bind_tools([{"name": n} for n in NEWS_TOOLS]).invoke("what happened")
    assert "BUILTIN_TOOLS_ENABLED" in msg.content
    assert "mcp__ta__fetch_url" in msg.content and "WebSearch" in msg.content
