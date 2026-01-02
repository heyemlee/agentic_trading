import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from conservative_trading.llm.claude_code import (
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
    llm = ClaudeCodeChatModel(model="claude-sonnet-5", work_dir=str(tmp_path), config_dir="config")
    bound = llm.bind_tools([get_stock_data, get_indicators])
    msg = bound.invoke("analyze")
    assert "ALLOWED=mcp__ta__get_stock_data,mcp__ta__get_indicators" in msg.content
    calls = msg.response_metadata["tool_calls_executed"]
    assert calls and calls[0]["tool"] == "mcp__ta__get_stock_data"
    assert "more chars" in calls[0]["result_excerpt"]
    assert msg.tool_calls == []  # the loop ran inside the CLI; LangGraph sees a finished report
    assert USAGE_LOG[-1]["num_turns"] == 2


def test_mcp_config_points_at_our_tool_server(tmp_path):
    llm = ClaudeCodeChatModel(model="claude-sonnet-5", work_dir=str(tmp_path), config_dir="/cfg",
                              tool_names=["get_news"])
    args = llm._mcp_args()
    cfg = json.loads(args[args.index("--mcp-config") + 1])
    server = cfg["mcpServers"]["ta"]
    assert server["args"][:2] == ["-m", "conservative_trading.llm.ta_tool_server"]
    assert server["args"][-1] == "get_news" and server["env"]["CT_CONFIG_DIR"] == "/cfg"


@pytest.mark.parametrize("text", [
    "You've hit your session limit \u00b7 resets 1:30pm (America/Los_Angeles)",
    "You've hit your weekly limit. Resets Monday.",
    "Rate limit exceeded (429)",
    "Usage limit reached for this period",
])
def test_real_limit_wordings_are_classified(text):
    from conservative_trading.llm.claude_code import _LIMIT_RE

    assert _LIMIT_RE.search(text)
