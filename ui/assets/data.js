/* ─────────────────────────────────────────────────────────────────────────
 * Fixture payloads — copied verbatim from docs/examples/ in the backend repo.
 * These are the real, sanitized shapes the API returns. Nothing here is
 * invented: no number, string or timestamp has been added or edited.
 *
 * In the built frontend these objects are replaced by fetch() calls against
 * http://127.0.0.1:8787 on the polling intervals in FRONTEND_HANDOFF.md §15.
 * ───────────────────────────────────────────────────────────────────── */

window.CT = (function () {
  const health = {
    status: "READY",
    detail: "able to research and to accept approvals",
    paused: false,
    stage: 3,
    market: {
      is_open: false,
      calendar: "XNYS",
      next_open: "2026-09-04 13:30:00+00:00",
      next_close: null,
    },
    claude: {
      state: "OK",
      detail: "subscription-backed Claude Code runtime",
      billing: "claude_subscription_no_api_charges",
      calls_today: 0,
      last_call_at: "2026-09-03T17:09:52-0700",
    },
    micro_live_enabled: false,
    warnings: [
      "FRED macro data is unavailable, so inflation, rate and labour context cannot be verified. The system records the gap, lowers confidence and prefers NO_TRADE. This is an optional enhancement, not a blocker.",
      "68 risk parameters are documented defaults you have not explicitly accepted; real-money execution is blocked until you do.",
    ],
    checked_at: "2026-09-04T01:32:16.572941+00:00",
  };

  const portfolio = {
    as_of: "2026-09-02",
    buckets: {
      core: {
        allocation_usd: 2500.0, cash_usd: 2500.0, positions_value_usd: 0.0,
        equity_usd: 2500.0, position_count: 0, is_real_money: false,
        venue: null, positions: [], sleeves_pct: { cash: 100.0 },
      },
      sandbox: {
        allocation_usd: 500.0, cash_usd: 500.0, positions_value_usd: 0.0,
        equity_usd: 500.0, position_count: 0, is_real_money: false,
        venue: null, positions: [], sleeves_pct: { cash: 100.0 },
      },
      micro_live: {
        allocation_usd: 100.0, cash_usd: 100.0, positions_value_usd: 0.0,
        equity_usd: 100.0, position_count: 0, is_real_money: true,
        venue: "robinhood", positions: [], sleeves_pct: { cash: 100.0 },
      },
    },
    errors: {},
    total_equity_usd: 3100.0,
  };

  const risk = {
    total:      { high_water_mark_usd: 3000.0, level: "NORMAL", drawdown_pct: 0.0, manual_reset_required: false, updated: "2026-09-02", history: [] },
    core:       { high_water_mark_usd: 2500.0, level: "NORMAL", drawdown_pct: 0.0, manual_reset_required: false, updated: "2026-09-02", history: [] },
    sandbox:    { high_water_mark_usd:  500.0, level: "NORMAL", drawdown_pct: 0.0, manual_reset_required: false, updated: "2026-09-02", history: [] },
    micro_live: { high_water_mark_usd:  100.0, level: "NORMAL", drawdown_pct: 0.0, manual_reset_required: false, updated: "2026-09-02", history: [] },
    regime: {
      level: "NORMAL",
      available: true,
      detail: "SPY 765.16 above 200-day average 708.78 (2026-09-02); ^VIX 15.20 (2026-09-02)",
      index_symbol: "SPY",
      index_close: 765.1599731445312,
      index_sma200: 708.7847973632812,
      index_date: "2026-09-02",
      vix: 15.199999809265137,
      vix_date: "2026-09-02",
    },
  };

  const microLive = {
    status: "DISABLED",
    enabled: false,
    connected: true,
    connect_error: null,
    capital_boundary_usd: 100.0,
    account: { account_id_masked: "••••0000", cash: 100.0, equity: 100.0, buying_power: 100.0, type: "individual" },
    preflight_ready: false,
    checks: [
      { id: "PF00", name: "micro-live master switch", passed: false, detail: "micro_live_execution.enabled is false: real-money orders are disabled (this is the intended default)" },
      { id: "PF04", name: "human approval active", passed: true, detail: "execution_mode=human_approval, autonomous opening=False" },
      { id: "PF05", name: "stale-proposal protection active", passed: true, detail: "5 freshness classes configured; the micro-live class is core_conservative" },
      { id: "PF06", name: "execution-time revalidation active", passed: true, detail: "outcomes: ['VALID', 'REDUCED', 'STALE', 'INVALIDATED', 'REANALYZE', 'REJECTED']" },
      { id: "PF07", name: "duplicate protection active", passed: true, detail: "client-order-id prefix 'ct', broker checked before submit, max attempts 1" },
      { id: "PF09", name: "unsupported strategies disabled", passed: true, detail: "disabled: options, crypto, short_selling, margin, extended_hours; enabled: equities_long, fractional" },
      { id: "PF09b", name: "sensitive strategies permanently human-gated", passed: true, detail: "always human-approved: ['momentum', 'short', 'event_driven', 'options', 'crypto', 'leverage', 'margin']" },
      { id: "PF10", name: "audit logging active", passed: true, detail: "micro-live ledger micro_live_portfolio.yaml present, fills log and proposal store directories writable, decision records under runs/" },
      { id: "PF08", name: "reconciliation active", passed: true, detail: "every snapshot is rebuilt from broker cash, quantities and prices; an unattributed position or a cash mismatch beyond 0.1% fails closed; order state is followed to a terminal state through the venue's own order-status tool and only filled quantity is recorded" },
      { id: "PF12", name: "market calendar available", passed: true, detail: "XNYS exchange calendar loaded, holidays and half days included; market open now: False" },
      { id: "PF13", name: "risk parameters acknowledged", passed: true, detail: "all risk parameters are explicit values" },
      { id: "PF11", name: "research runtime has no broker tools", passed: true, detail: "built-in tools disabled, strict MCP config, only our data tools exposed (mcp__ta__get_stock_data); no trading tool is reachable by the model" },
      { id: "PF03", name: "hard risk active", passed: true, detail: "the deterministic layer runs at proposal time and again at execution time" },
      { id: "PF01", name: "agentic account connected", passed: true, detail: "account ••••4118, equity $100.00, cash $100.00, trading blocked: False" },
      { id: "PF02", name: "account equity within the capital boundary", passed: true, detail: "equity $100.00 vs boundary $100.00 (+$5.00 tolerance)" },
    ],
    blocking: ["PF00"],
    permissions: {
      equities_long: true, fractional: true, options: false, crypto: false,
      short_selling: false, margin: false, extended_hours: false,
    },
    execution_mode: "human_approval",
  };

  const proposals = [
    {
      proposal_id: "2026-09-08-VTI-f4c6ec7f",
      status: "WAITING_FOR_APPROVAL",
      ticker: "VTI",
      bucket: "micro_live",
      is_real_money: true,
      action: "BUY",
      strategy: "long_only_diversified",
      notional_usd: 35.0,
      target_weight_pct: 35.0,
      confidence: 78,
      requires_human_approval: true,
      created_at: "2026-09-04T01:33:01.850005+00:00",
      approved_at: null,
      freshness: {
        state: "VALID",
        label: "current",
        detail: "core_conservative: 0 min old, inside the 960 min window",
        class: "core_conservative",
        ttl_minutes: 2880,
        age_minutes: 0.0,
        max_price_drift_pct: 3.0,
      },
      reference_price: 381.42,
      run_id: "2026-09-08_VTI_a1b2c3d4",
    },
  ];

  const proposalDetail = {
    proposal_id: "2026-09-08-VTI-f4c6ec7f",
    status: "WAITING_FOR_APPROVAL",
    ticker: "VTI",
    bucket: "micro_live",
    is_real_money: true,
    action: "BUY",
    strategy: "long_only_diversified",
    notional_usd: 35.0,
    target_weight_pct: 35.0,
    confidence: 78,
    requires_human_approval: true,
    created_at: "2026-09-04T01:33:01.850005+00:00",
    approved_at: null,
    freshness: {
      state: "VALID",
      label: "current",
      detail: "core_conservative: 0 min old, inside the 960 min window",
      class: "core_conservative",
      ttl_minutes: 2880,
      age_minutes: 0.0,
      max_price_drift_pct: 3.0,
    },
    reference_price: 381.42,
    run_id: "2026-09-08_VTI_a1b2c3d4",
    history: [
      { at: "2026-09-04T01:33:01.850146+00:00", event: "created", detail: "BUY VTI in micro_live for $35.00 (long_only_diversified)" },
    ],
    world_at_proposal: {
      ladder_level: "NORMAL",
      regime: "NORMAL",
      portfolio_equity_usd: 100.0,
      bucket_cash_usd: 100.0,
      data_quality: "partial",
    },
    research: {
      portfolio_manager_rating: "Buy",
      policy_action: "BUY",
      confidence: 78,
      data_quality: "partial",
      market_risk_level: "NORMAL",
      policy_reasons: ["confidence 78 below full-size floor 75 -> normal position"],
      investment_thesis: "VTI is the most mandate-compliant instrument available: ~3,600 holdings, a 0.03% expense ratio, deep liquidity and no single-name risk. Price has held the 50-day average on a retest and momentum has turned back up, while the position would be the account's first and only exposure.",
      bull_case: "Broad, cheap, liquid exposure with no issuer risk; the support retest held and the trend structure is intact; entry at a modest discount to the recent high.",
      bear_case: "The macro backdrop is unverified this cycle, cap-weighting concentrates roughly a third of assets in ten mega-cap names, and a broad-market entry inherits that correlation.",
      primary_risks: [
        "unverified macro backdrop (FRED unavailable)",
        "mega-cap concentration inside a nominally diversified fund",
        "single-position account: no diversification across holdings yet",
      ],
      what_could_make_this_wrong: "A rate-path surprise or an earnings-led repricing of the mega-cap complex.",
      why_doing_nothing_may_be_better: "Cash carries no drawdown risk and the account has no clock forcing it to be invested.",
      missing_information: [
        "FRED macro series unavailable (no API key)",
        "no fundamentals pass for an ETF instrument",
      ],
      entry_logic: "Single tranche of roughly one third of the account at the current quote, regular hours only.",
      exit_logic: "Reviewed if the position falls 12% from cost or the drawdown ladder reaches WARNING.",
      expected_holding_period: "indefinite; this is a core-style long-term holding",
    },
    hard_risk: {
      verdict: "APPROVE",
      final_action: "BUY",
      notional_usd: 35.0,
      post_trade_weight_pct: 35.0,
      requires_human_approval: true,
      rules: [
        { id: "HR00", name: "preconditions", passed: true, detail: "proposal complete and bucket known" },
        { id: "HR01", name: "kill switch", passed: true, detail: "kill switch clear" },
        { id: "HR02", name: "drawdown ladder (micro_live)", passed: true, detail: "micro_live ladder level NORMAL" },
        { id: "HR03", name: "market regime", passed: true, detail: "regime NORMAL: SPY above its 200-day average; VIX 15.2" },
        { id: "HR15", name: "strategy policy", passed: true, detail: "sandbox strategy 'long_only_diversified' carries no special gate" },
        { id: "HR17", name: "micro-live master switch", passed: true, detail: "real-money execution is enabled for the Micro-Live bucket" },
        { id: "HR06", name: "sizing", passed: true, detail: "$35.00 toward a 35.0% target of the micro_live allocation" },
        { id: "HR07", name: "bucket funding", passed: true, detail: "micro_live cash $100.00 vs order $35.00; the other bucket is never a funding source" },
        { id: "HR08", name: "position cap", passed: true, detail: "post-trade 35.0% vs cap 40.0%" },
      ],
      failed_rule_ids: [],
      advisories: [],
      ladder: { effective_level_for_bucket: "NORMAL" },
      regime: { level: "NORMAL", available: true },
    },
  };

  const approveExecuted = {
    proposal_id: "2026-09-08-VTI-f4c6ec7f",
    status: "EXECUTED",
    outcome: "SUBMITTED",
    submitted: true,
    duplicate_prevented: false,
    order: {
      order_id: "b7f3...",
      client_order_id: "ct-2026-09-08-VTI-a1b2c3d4-1",
      symbol: "VTI",
      side: "buy",
      quantity: 0.0918,
      notional: 35.0,
      order_type: "market",
      status: "filled",
      filled_quantity: 0.0918,
      filled_avg_price: 381.55,
      limit_price: null,
      submitted_at: "2026-09-08T14:32:11+00:00",
      reason: null,
    },
    error: null,
    reasons: [
      "venue binding: micro_live may execute at robinhood",
      "freshness: core_conservative: 3 min old, inside the 960 min window",
      "approval: approval is 0 min old (limit 60 min)",
      "venue price 381.5500 (last_trade, spread 0.02%)",
      "price 381.55 vs 381.42 at analysis: +0.03% (band ±3.0%)",
      "venue pre-trade review passed with no alerts",
      "ledger updated: buy 0.0918 VTI @ 381.5500 in micro_live",
    ],
    message: "the order was submitted and the portfolio was updated",
  };

  const approveReanalysis = {
    proposal_id: "2026-09-08-VTI-f4c6ec7f",
    status: "REANALYSIS_REQUIRED",
    outcome: "REANALYZE",
    submitted: false,
    duplicate_prevented: false,
    order: null,
    error: null,
    reasons: [
      "venue binding: micro_live may execute at robinhood",
      "freshness: core_conservative: 47 min old, inside the 960 min window",
      "venue price 396.80 (last_trade, spread 0.03%)",
      "price 396.80 vs 381.42 at analysis: +4.03% (band ±3.0%)",
      "the move is larger than this strategy tolerates; the thesis needs a fresh analysis, not a re-priced old order",
    ],
    message: "the world changed materially since the analysis; this idea needs fresh research rather than a re-priced old order",
  };

  const research = [
    { run_id: "2026-09-02_VTI_2bf5a8b5", ticker: "VTI", bucket: "micro_live", trade_date: "2026-09-02", created_at: "2026-09-04T00:09:52.569799+00:00", portfolio_manager_rating: "Hold", policy_action: "NO_TRADE", confidence: 38, hard_risk_verdict: "NO_ACTION", final_action: "NO_TRADE", proposal_id: null, llm_calls: 13, elapsed_seconds: 420.9 },
    { run_id: "2026-09-02_VTI_f819eaa6", ticker: "VTI", bucket: "core",       trade_date: "2026-09-02", created_at: "2026-09-03T21:05:24.598310+00:00", portfolio_manager_rating: "Hold", policy_action: "NO_TRADE", confidence: 57, hard_risk_verdict: "NO_ACTION", final_action: "NO_TRADE", proposal_id: null, llm_calls: 13, elapsed_seconds: 582.7 },
    { run_id: "2026-09-02_VTI_56717678", ticker: "VTI", bucket: "core",       trade_date: "2026-09-02", created_at: "2026-09-03T20:37:50.909749+00:00", portfolio_manager_rating: "Hold", policy_action: "NO_TRADE", confidence: 44, hard_risk_verdict: null,        final_action: null,       proposal_id: null, llm_calls: 7,  elapsed_seconds: 302.9 },
  ];

  const orders = { fills: [], open_orders: [], error: null };

  return {
    health, portfolio, risk, microLive, proposals, proposalDetail,
    approveExecuted, approveReanalysis, research, orders,
  };
})();
