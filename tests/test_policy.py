

from agentic_trading.policy import ConservativeProposal, deterministic_action, run_policy


def act(profiles, rating, **kw):
    defaults = {"confidence": 80, "llm_action": "BUY", "bucket": "core", "instrument_type": "etf", "profiles": profiles}
    defaults.update(kw)
    return deterministic_action(rating, **defaults)


def test_hold_without_position_is_no_trade(profiles):
    action, size, _ = act(profiles, "Hold", llm_action="HOLD")
    assert action == "NO_TRADE" and size == "none"


def test_review_sentinel_is_never_tradeable(profiles):
    action, _, reasons = act(profiles, "REVIEW")
    assert action == "NO_TRADE" and "not tradeable" in reasons[0]


def test_buy_with_full_confidence_on_core_etf(profiles):
    assert act(profiles, "Buy", confidence=80)[:2] == ("BUY", "normal")


def test_overweight_maps_to_small_buy(profiles):
    assert act(profiles, "Overweight", confidence=80)[:2] == ("BUY", "small")


def test_buy_between_floors_is_small_only(profiles):
    action, size, reasons = act(profiles, "Buy", confidence=70)
    assert (action, size) == ("BUY", "small") and any("small position only" in r for r in reasons)


def test_buy_below_opening_floor_is_no_trade(profiles):
    assert act(profiles, "Buy", confidence=60)[0] == "NO_TRADE"


def test_stock_needs_higher_floor_than_etf(profiles):
    assert act(profiles, "Buy", confidence=70, instrument_type="stock")[0] == "NO_TRADE"
    assert act(profiles, "Buy", confidence=76, instrument_type="stock")[:2] == ("BUY", "normal")


def test_reviewer_caution_wins_over_pm_buy(profiles):
    action, _, reasons = act(profiles, "Buy", confidence=90, llm_action="NO_TRADE")
    assert action == "NO_TRADE" and any("most cautious view wins" in r for r in reasons)


def test_crypto_never_opens_in_core(profiles):
    assert act(profiles, "Buy", confidence=95, instrument_type="crypto")[0] == "NO_TRADE"


def test_missing_confidence_is_no_trade(profiles):
    assert act(profiles, "Buy", confidence=None)[0] == "NO_TRADE"


def test_sell_with_position_is_sell(profiles):
    assert act(profiles, "Sell", llm_action="SELL", has_position=True)[0] == "SELL"


def test_run_policy_fails_closed_on_low_confidence(fake_claude, profiles, tmp_path, real_audit_guard):
    from agentic_trading.llm.claude_code import ClaudeCodeChatModel

    state = {"market_report": "m", "news_report": "n", "investment_plan": "p",
             "trader_investment_plan": "t", "final_trade_decision": "**Rating**: Buy"}
    # Explicit work_dir keeps the offline audit trail out of the real runs/ directory.
    llm = ClaudeCodeChatModel(model="claude-opus-5", work_dir=str(tmp_path / "runs" / ".claude_workdir"),
                              role="policy")
    decision = run_policy(profiles, state, ticker="VTI", instrument_type="etf", bucket="core",
                          pm_rating="Buy", trade_date="2026-09-02", llm=llm)
    assert decision.proposal is not None and decision.confidence == 0  # fake returns the schema minimum
    assert decision.final_action == "NO_TRADE"
    assert any("below no-trade threshold" in r for r in decision.reasons)


def test_proposal_schema_matches_mandate_fields():
    fields = set(ConservativeProposal.model_fields)
    for required in ("bull_case", "bear_case", "what_could_make_this_thesis_wrong",
                     "alternative_why_doing_nothing_may_be_preferable", "missing_information", "confidence"):
        assert required in fields
