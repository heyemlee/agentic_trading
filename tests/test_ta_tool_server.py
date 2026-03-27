"""The upstream tools as the model is handed them (``llm.ta_tool_server``).

Upstream's functions are served unchanged. What this file holds is the one answer rewritten after
the call: a news feed that returned nothing at all is said to be a gap, not a quiet week.
"""

from agentic_trading.llm import ta_tool_server

GAP = "Treat company news as unavailable for this run"


class _Ticker:
    def __init__(self, articles):
        self.articles = articles

    def get_news(self, count=10):
        return list(self.articles)


def _upstream_answer(monkeypatch, articles, start="2026-10-02", end="2026-10-09"):
    """What upstream's own news function says, with Yahoo's answer scripted."""
    from tradingagents.dataflows import yfinance_news

    monkeypatch.setattr(yfinance_news.yf, "Ticker", lambda _symbol: _Ticker(articles))
    return yfinance_news.get_news_yfinance("VTI", start, end)


def test_an_empty_feed_is_said_to_be_a_gap_in_upstreams_own_words_first(monkeypatch):
    """Found 2026-10-15: every 10-09 study was told "No news found", read as a quiet week."""
    said = _upstream_answer(monkeypatch, [])
    assert said == "No news found for VTI", "upstream's sentence for an empty feed has changed"
    noted = ta_tool_server._news_feed_said_nothing(said)
    assert noted.startswith("No news found for VTI. ") and GAP in noted
    assert "not as evidence that nothing was reported" in noted


def test_a_feed_with_nothing_in_the_window_is_an_answer_and_left_alone(monkeypatch):
    old = {"content": {"title": "Old news", "pubDate": "2026-01-02T10:00:00Z",
                       "provider": {"displayName": "Wire"}}}
    said = _upstream_answer(monkeypatch, [old])
    assert said == "No news found for VTI between 2026-10-02 and 2026-10-09"
    assert ta_tool_server._news_feed_said_nothing(said) == said


def test_news_and_errors_pass_through_untouched(monkeypatch):
    fresh = {"content": {"title": "Fund flows", "summary": "Inflows rose.", "pubDate": "2026-10-05T10:00:00Z",
                         "provider": {"displayName": "Wire"}}}
    said = _upstream_answer(monkeypatch, [fresh])
    assert said.startswith("## VTI News") and ta_tool_server._news_feed_said_nothing(said) == said
    for other in ("TOOL_ERROR: RuntimeError: down", "Error fetching news for VTI: boom", ""):
        assert ta_tool_server._news_feed_said_nothing(other) == other


def test_only_the_news_tool_is_noted_and_every_tool_stays_quiet(capsys):
    def loud(**_kwargs):
        print("chatter that would corrupt the protocol")
        return "No news found for VTI"

    assert GAP in ta_tool_server._served("get_news", loud)(ticker="VTI")
    assert ta_tool_server._served("get_global_news", loud)() == "No news found for VTI"
    assert capsys.readouterr().out == "", "stdout is the MCP protocol's"
