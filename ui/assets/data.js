/* ─────────────────────────────────────────────────────────────────────────
 * Fixture payloads for ?demo=1, sanitized, in the shapes the API returns.
 * They began as verbatim copies of recorded payloads and are maintained here;
 * /openapi.json is the contract when the two disagree.
 *
 * In the built frontend these objects are replaced by fetch() calls against
 * http://127.0.0.1:8787 on the polling intervals in FRONTEND_HANDOFF.md §15.
 * ───────────────────────────────────────────────────────────────────── */

window.CT_DEMO = new URLSearchParams(window.location.search).get("demo") === "1";
window.CT = (function () {
  if (!window.CT_DEMO) return {
    health: {status: "UNKNOWN", paused: null, market: {}, warnings: []},
    portfolio: {buckets: {}, accounts: {}, errors: {}, combined: null}, risk: {}, microLive: {},
    schedule: null, operations: null, notifications: null, pushDevices: null, authSessions: null,
    config: null, authority: null, attention: null,
    auto: null, autoLive: null,
    evaluation: null,
    activity: null,
    proposals: [], proposalDetail: null, research: [], researchJobs: [], researchSubjects: null, researchRun: null,
    labRoster: null, labTrace: {},
    orders: {fills: [], tracked_orders: []}
  };
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
        account: "conceptual",
        allocation_usd: 2500.0, cash_usd: 2500.0, positions_value_usd: 0.0,
        equity_usd: 2500.0, position_count: 0, is_real_money: false,
        venue: null, positions: [], sleeves_pct: { cash: 100.0 },
      },
      sandbox: {
        account: "conceptual",
        allocation_usd: 500.0, cash_usd: 500.0, positions_value_usd: 0.0,
        equity_usd: 500.0, position_count: 0, is_real_money: false,
        venue: null, positions: [], sleeves_pct: { cash: 100.0 },
      },
      micro_live: {
        account: "micro_live",
        allocation_usd: 100.0, cash_usd: 100.0, positions_value_usd: 0.0,
        equity_usd: 100.0, position_count: 0, is_real_money: true,
        venue: "robinhood", positions: [], sleeves_pct: { cash: 100.0 },
      },
    },
    accounts: {
      conceptual: {
        buckets: ["core", "sandbox"], is_real_money: false, venue: null,
        source: "local demo ledger", as_of: "2026-09-02", priced_for: "2026-09-02",
        equity_usd: 3000.0, cash_usd: 3000.0, positions_value_usd: 0.0,
        net_deposits_usd: 3000.0, opening_allocation_usd: 3000.0, recorded_flows_usd: 0.0,
        net_deposits_basis: "configured allocation plus recorded deposits and withdrawals",
        investment_pnl_usd: 0.0, return_pct: 0.0,
        high_water_mark_usd: 3000.0, drawdown_pct: 0.0, ladder_level: "NORMAL",
      },
      micro_live: {
        buckets: ["micro_live"], is_real_money: true, venue: "robinhood",
        source: "robinhood account, reconciled", as_of: "2026-09-02", priced_for: "2026-09-02",
        equity_usd: 100.0, cash_usd: 100.0, positions_value_usd: 0.0,
        net_deposits_usd: 100.0, opening_allocation_usd: 100.0, recorded_flows_usd: 0.0,
        net_deposits_basis: "configured allocation plus recorded deposits and withdrawals",
        investment_pnl_usd: 0.0, return_pct: 0.0,
        high_water_mark_usd: 100.0, drawdown_pct: 0.0, ladder_level: "NORMAL",
      },
    },
    errors: {},
    combined: {
      equity_usd: 3100.0, accounts: ["conceptual", "micro_live"],
      complete: true, mixes_real_and_conceptual: true,
    },
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
      { id: "PF04", name: "opening a position: a person, or the bounded open switch only", passed: true, detail: "execution_mode=human_approval, blanket autonomous opening=False; the open switch is off: every opening waits for a person" },
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
    checked_at: "2026-09-04T20:32:16+00:00",
    // The demo ledger holds cash only, so the venue lists nothing either.
    holdings: [],
    holdings_error: null,
  };

  const authorityVersion = "a11ce" + "0".repeat(59);
  const authorityBlock = {
    code: "real_money_disabled",
    detail: "Demo data cannot place a real-money order.",
  };
  const authorityAction = (action) => ({
    mode: "blocked",
    autonomous: { permitted: false, blocked_by: [authorityBlock] },
    human_approval: { permitted: false, blocked_by: [authorityBlock] },
    // Each action's limits in the server's own vocabulary (autonomy.effective_authority).
    limits: action === "SELL" ? { quantity_basis: "full ledger holding" }
      : action === "REDUCE" ? { min_confidence: 80, max_positions_reduced_per_day: 1, max_fraction_of_holding: 0.5,
                                min_remaining_pct_of_opened: null, used_today: 0 }
      : { classes: ["broad_diversified_etf", "bond_or_cash_like_etf"], max_pct_of_account: 20,
          max_new_positions_per_day: 1, min_confidence: null, used_today: 0 },
  });
  const authority = {
    checked_at: "2026-09-04T20:32:16+00:00",
    status: "READY",
    detail: "Demo data shows the effective authority shape; all writes remain disabled.",
    version: authorityVersion,
    history: [{ version: authorityVersion, at: "2026-09-04T20:32:16+00:00" }],
    config_revisions: { risk_profile: "demo-risk", investor_profile: "demo-investor" },
    account: { scope: "agentic", account_id: "micro_live", venue: "robinhood", is_real_money: true },
    stage: 3,
    stage_allows: ["BUY", "ADD", "REDUCE", "SELL"],
    real_money_enabled: false,
    execution_mode: "human_approval",
    kill_switch: { engaged: false, since: null, reason: null },
    withdrawn: { tripped: false, reason: null, at: null },
    switches: { reduce: false, open: false },
    actions: Object.fromEntries(["BUY", "ADD", "REDUCE", "SELL"].map(action => [action, authorityAction(action)])),
    limits: { capital: { max_capital_usd: 100.0 }, tiers: { low: { max_order_usd: 35.0 } },
              confidence_floors: { autonomous_any: 80 } },
    pending_changes: [],
    preflight: { ready: false, blocking: ["PF00"], checked_at: "2026-09-04T20:32:16+00:00", stale: false },
    consistency: [],
  };

  const personalAccountA = "a".repeat(20);
  const personalAccountB = "b".repeat(20);
  const accounts = {
    checked_at: "2026-09-04T20:32:16+00:00",
    accounts: [
      { scope: "agentic", account_scope_kind: "single", account_id: "micro_live", account_ids: [],
        account_label: "Agentic", is_real_money: true, venue: "robinhood", source: "ledger, reconciled",
        as_of: "2026-09-02", complete: true },
      { scope: "personal", account_scope_kind: "single", account_id: personalAccountA, account_ids: [],
        account_label: "••••1111", is_real_money: true, venue: "robinhood", source: "observed snapshot",
        as_of: "2026-09-04T20:31:00+00:00", complete: true },
      { scope: "personal", account_scope_kind: "single", account_id: personalAccountB, account_ids: [],
        account_label: "••••2222", is_real_money: true, venue: "robinhood", source: "observed snapshot",
        as_of: "2026-09-04T20:31:00+00:00", complete: true },
      { scope: "personal", account_scope_kind: "all_personal", account_id: null,
        account_ids: [personalAccountA, personalAccountB], account_label: "all personal accounts",
        is_real_money: true, venue: "robinhood", source: "observed snapshot",
        as_of: "2026-09-04T20:31:00+00:00", complete: true },
    ],
    errors: {},
    personal_snapshot: { fetched_at: "2026-09-04T20:31:00+00:00", stale: false, errors: [] },
  };

  // The version is half of the identity an approval is made against: the console pins the
  // detail to the list entry's (proposal_id, version) pair, so both carry the same one even
  // though demo mode never approves anything.
  const demoVersion = "c0ffee" + "0".repeat(58);

  const proposals = [
    {
      proposal_id: "2026-09-08-VTI-f4c6ec7f",
      version: demoVersion,
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
      authority_version: authorityVersion,
    },
  ];

  const proposalDetail = {
    proposal_id: "2026-09-08-VTI-f4c6ec7f",
    version: demoVersion,
    status: "WAITING_FOR_APPROVAL",
    sell_quantity: null,
    order_status: null,
    reconciliation_error: null,
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
    authority_version: authorityVersion,
    authority_changed: null,
    history: [
      { at: "2026-09-04T01:33:01.850146+00:00", event: "created", detail: "BUY VTI in micro_live for $35.00 (long_only_diversified)" },
    ],
    world_at_proposal: {
      ladder_level: "NORMAL",
      regime: "NORMAL",
      portfolio_equity_usd: 100.0,
      bucket_cash_usd: 100.0,
      data_quality: "partial",
      evidence: {},
      missing_information: [],
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
        { id: "HR17", name: "micro-live master switch", passed: true, detail: "real-money execution is enabled for the Agentic bucket" },
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

  /* Why each of these runs ended as it did, in the shape the API returns it
     (agentic_trading/diagnosis.py). All four were rated Hold with nothing held. */
  const DECLINED = {
    category: "strategy_no_trade", code: "rating_hold_no_position",
    label: { zh: "策略选择不交易", en: "Strategy chose not to trade" },
    summary: { zh: "研究评级为“持有”，目前没有持仓，所以不买入。",
               en: "Research rated it Hold and nothing is held, so there is nothing to buy." },
    reasons: [{ category: "strategy_no_trade", code: "rating_hold_no_position", rule_id: null,
                text: { zh: "研究评级为“持有”，目前没有持仓，所以不买入。",
                        en: "Research rated it Hold and nothing is held, so there is nothing to buy." } }],
  };
  const research = [
    { run_id: "2026-09-04_VTI_873e64ae", ticker: "VTI", bucket: "micro_live", trade_date: "2026-09-04", instrument_type: "etf", created_at: "2026-09-05T06:18:43.090801+00:00", portfolio_manager_rating: "Hold", policy_action: "NO_TRADE", confidence: 38, hard_risk_verdict: "NO_ACTION", final_action: "NO_TRADE", proposal_id: null, llm_calls: 13, elapsed_seconds: 412.6 },
    { run_id: "2026-09-02_VTI_2bf5a8b5", ticker: "VTI", bucket: "micro_live", trade_date: "2026-09-02", instrument_type: "etf", created_at: "2026-09-04T00:09:52.569799+00:00", portfolio_manager_rating: "Hold", policy_action: "NO_TRADE", confidence: 38, hard_risk_verdict: "NO_ACTION", final_action: "NO_TRADE", proposal_id: null, llm_calls: 13, elapsed_seconds: 420.9 },
    { run_id: "2026-09-02_VTI_f819eaa6", ticker: "VTI", bucket: "core",       trade_date: "2026-09-02", instrument_type: "etf", created_at: "2026-09-03T21:05:24.598310+00:00", portfolio_manager_rating: "Hold", policy_action: "NO_TRADE", confidence: 57, hard_risk_verdict: "NO_ACTION", final_action: "NO_TRADE", proposal_id: null, llm_calls: 13, elapsed_seconds: 582.7 },
    { run_id: "2026-09-02_VTI_56717678", ticker: "VTI", bucket: "core",       trade_date: "2026-09-02", instrument_type: "etf", created_at: "2026-09-03T20:37:50.909749+00:00", portfolio_manager_rating: "Hold", policy_action: "NO_TRADE", confidence: 44, hard_risk_verdict: null,        final_action: null,       proposal_id: null, llm_calls: 7,  elapsed_seconds: 302.9 },
  ].map((row) => ({ ...row, diagnosis: DECLINED }));

  const researchJobs = [{
    run_key: "2026-09-04_VTI_873e64ae", dedupe_key: "demo:VTI:2026-09-04",
    ticker: "VTI", bucket: "micro_live", trade_date: "2026-09-04", instrument_type: "etf",
    trigger: "scheduled", state: "COMPLETE", created_at: "2026-09-04T20:30:00+00:00",
    updated_at: "2026-09-04T20:31:30+00:00", started_at: "2026-09-04T20:30:02+00:00",
    finished_at: "2026-09-04T20:31:30+00:00", deadline_at: "2026-09-04T21:30:02+00:00",
    pid: null, attempts: 1, max_attempts: 2,
    timeout_seconds: 3600, log: "Research completed without a proposal.", exit_code: 0, error: null,
    limit_recoveries: 0, proposal_created: false, proposal_id: null, task: null,
    authority_version: authorityVersion, opened_at: null, purpose: "candidate",
    request_id: "11111111-1111-4111-8111-111111111111", scope: "agentic",
    account_scope_kind: "single", account_id: "micro_live", account_ids: [],
    account_label: "Agentic", task_type: "full_research",
    route: { name: "tradingagents", inferred: false, package_version: "demo",
      upstream_commit: "0000000000000000000000000000000000000000", skill_version: null,
      models: { quick: "sample-model", deep: "sample-model" } },
    cancellable: false,
  }];

  const schedule = {
    enabled: true, period: "daily", calendar: "XNYS", market_timezone: "America/New_York",
    on_non_session_day: "previous_session", run_while_paused: true,
    tickers: ["VTI", "BND"],
    next_run_at: "2026-09-05T13:30:00-07:00",
    current_period: "daily:micro_live:holdings:2026-09-04", would_run_now: false,
    reason: "already executed for daily:micro_live:holdings:2026-09-04",
    analyses_today: 1, daily_budget: 6,
    fires_every: "daily", reserve_for_exits: 1, per_account: true,
    last_period: "daily:micro_live:holdings:2026-09-04",
    last_run: {
      at: "2026-09-04T20:30:11+00:00", trade_date: "2026-09-03",
      tickers: ["VTI", "BND"], state: "STARTED", bucket: "micro_live", kind: "holdings",
      run_keys: ["2026-09-03_VTI_2bf5a8b5"], errors: [],
    },
    last_skip: {
      at: "2026-08-28T20:30:07+00:00", period_key: "weekly:2026-W35",
      reason: "2026-08-28 is not a trading session for XNYS",
    },
    tasks: [
      { bucket: "micro_live", kind: "holdings", period: "daily", max_tickers_per_run: 2,
        current_period: "daily:micro_live:holdings:2026-09-04", would_run_now: false,
        reason: "already executed for daily:micro_live:holdings:2026-09-04",
        tickers: ["VTI", "BND"], trade_date: "2026-09-04",
        last_run: { at: "2026-09-04T20:30:11+00:00", trade_date: "2026-09-03",
          tickers: ["VTI", "BND"], state: "STARTED", bucket: "micro_live", kind: "holdings",
          run_keys: ["2026-09-03_VTI_2bf5a8b5"], errors: [] } },
      { bucket: "micro_live", kind: "candidates", period: "weekly", max_tickers_per_run: 1,
        current_period: "weekly:micro_live:candidates:2026-W36", would_run_now: false,
        reason: "one analysis is reserved for holding and exit reviews",
        tickers: ["VTI"], trade_date: "2026-09-04", last_run: null },
    ],
  };

  const operations = {
    checked_at: "2026-09-04T20:32:16+00:00",
    activities: [
      { id: "reconciliation", last_success_at: "2026-09-04T20:32:01+00:00", age_minutes: 0.3,
        state: "current", expected_since: null, due_by: "2026-09-04T20:42:01+00:00", last_attempt_at: null, last_failure: null,
        expected_every_minutes: 5, overdue: false, detail: "background loop in the API process" },
      { id: "research_dispatch", last_success_at: "2026-09-04T20:32:01+00:00", age_minutes: 0.3,
        state: "current", expected_since: null, due_by: "2026-09-04T20:42:01+00:00", last_attempt_at: null, last_failure: null,
        expected_every_minutes: 5, overdue: false, detail: "queue drained, 0 waiting" },
      { id: "backup", last_success_at: "2026-09-04T10:15:04+00:00", age_minutes: 617.2,
        state: "current", expected_since: null, due_by: "2026-09-06T10:15:04+00:00", last_attempt_at: null, last_failure: null,
        expected_every_minutes: 1440, overdue: false, detail: "5 local copies, integrity ok" },
      { id: "maintenance", last_success_at: null, age_minutes: null,
        state: "awaiting_first_run", expected_since: "2026-09-04T10:00:00+00:00", due_by: "2026-09-05T10:00:00+00:00",
        last_attempt_at: null, last_failure: null, expected_every_minutes: 1440, overdue: true, detail: "" },
      { id: "schedule", state: "overdue", last_success_at: null, age_minutes: null,
        expected_since: null, due_by: null, last_attempt_at: null, last_failure: null,
        expected_every_minutes: 10080, overdue: true, detail: "Synthetic activity with no known monitoring origin" },
      { id: "ledger_confirm", state: "failed", last_success_at: "2026-09-03T10:45:00+00:00", age_minutes: 2027.3,
        expected_since: null, due_by: "2026-09-05T10:45:00+00:00", last_attempt_at: "2026-09-04T10:45:00+00:00",
        last_failure: { at: "2026-09-04T10:45:00+00:00", reason: "Synthetic example: the check could not read the venue account." },
        expected_every_minutes: 1440, overdue: false, detail: "Synthetic failed check; no live account was read" },
    ],
    disk: { free_gb: 214.7, free_pct: 43.0, low: false },
    retention: { runs_days: 120, logs_mb: 32, backups_days: 30 },
  };

  const notifications = {
    channel: "none", configured: false,
    detail: "no channel is configured, so nothing is sent. Set notifications.channel and supply its credentials to authorise sending.",
    events_enabled: ["proposal_waiting", "research_failed", "research_limit",
                     "reconciliation_error", "credential_invalid", "risk_level_changed",
                     "backup_overdue"],
    min_interval_seconds: 900, heartbeat_configured: false,
    last_dispatch_at: "2026-09-04T20:32:01+00:00", events: {},
  };

  const evaluation = {
    scope: 'demo', sample: { basis: 'sessions', last_snapshot_on: '2026-09-01', days_recorded: 1, gaps: [], stale: false, sessions_behind: 0 },
    account: { scope: 'demo', account_id: 'conceptual', label: 'Demo' }, buckets: ['core', 'sandbox'],
    study_id: "demo-forward-record", state: "RUNNING", started_on: "2026-09-01",
    days_recorded: 1, config_drifted: false, readable: false, conclusive: false,
    declared: {
      observation_days: 30, minimum_days_for_judgement: 180,
      benchmark_weights_pct: { VTI: 60, BND: 40 },
      stop_rules: { max_drawdown_pct: -15, trails_benchmark_pct_points: -10 },
    },
    strategy: { return_pct: 0, max_drawdown_pct: 0, equity_usd: 3000, net_deposits_usd: 3000 },
    benchmark: { return_pct: 0, max_drawdown_pct: 0, equity_usd: 3000, weights_pct: { VTI: 60, BND: 40 } },
    strategy_minus_benchmark_pct_points: 0,
    decisions: { research_runs: 0, run_outcomes: {}, job_states: {}, proposal_statuses: {},
      incomplete_data_rate_pct: null, llm_calls: 0, human_decision_minutes: { median: null } },
    execution: { fills: 0, traded_usd: 0, turnover_pct_of_equity: 0,
      slippage_bps: { count: 0, median: null }, commission_usd: 0 },
    stop_rules_triggered: [],
    caveat: "1 day(s) recorded. This is a record of what happened, not evidence about the strategy: 180 days were declared as the minimum before it can support a judgement.",
  };

  const evaluationExamples = {
    demo: evaluation,
    agentic: { scope: 'agentic', account: { scope: 'agentic', account_id: 'micro_live', label: 'Agentic' },
      buckets: ['micro_live'], study_id: null, state: 'NOT_STARTED', started_on: null, days_recorded: 0,
      declared: null, config_drifted: false, frozen_fingerprint: null, current_fingerprint: null,
      strategy: {}, benchmark: {}, strategy_minus_benchmark_pct_points: null,
      decisions: { research_runs: 0, llm_calls: 0 }, execution: { fills: 0, traded_usd: 0 },
      stop_rules_triggered: [], sample: { basis: 'sessions', gaps: [], stale: false, sessions_behind: 0 },
      readable: true, conclusive: false, caveat: 'No Agentic study has been declared.' },
    route_quality: { scope: 'route_quality', checked_at: '2026-09-04T20:32:01+00:00', since: '2026-09-01',
      routes: [{ route: 'tradingagents', runs: 3, complete: 2, failed: 0, timed_out: 1, limit_hits: 0,
        median_elapsed_seconds: 1800, llm_calls_total: 42, evidence_check_pass_rate_pct: 50,
        format_pass_rate_pct: null, fact_accuracy_pct: null, human_edit_rate_pct: null,
        evaluated: { timing: true, calls: true, failures: true, evidence: true, format: false, facts: false, edits: false } }],
      note: 'Fact accuracy, format quality and human edits have not been evaluated.' },
  };

  const attentionItem = {
    id: "proposal_waiting:2026-09-08-VTI-f4c6ec7f", kind: "proposal_waiting",
    severity: "warning", priority: 20, requires_person: true, scope: "agentic",
    account_scope_kind: "single", account_id: "micro_live", account_ids: [],
    account_label: "Agentic", subject: "2026-09-08-VTI-f4c6ec7f",
    title: "VTI proposal is waiting for a decision",
    detail: "Review the current proposal and effective authority.",
    since: "2026-09-04T01:33:01.850005+00:00",
    link: { page: "approvals", params: { proposal: "2026-09-08-VTI-f4c6ec7f" } },
    actions: ["review"],
  };
  const attention = {
    checked_at: "2026-09-04T20:32:16+00:00", readable: true, unreadable: [],
    top: attentionItem, items: [attentionItem],
    summary: { total: 1, requires_person: 1,
      by_severity: { critical: 0, warning: 1, info: 0 } },
    context: { market_open: false, paused: false, autonomy_armed: false,
      autonomous_queue: 0, next_scheduled_at: "2026-09-11T13:30:00-07:00" },
  };

  const authSessions = {
    max_age_days: 30,
    sessions: [
      { session_id: "a1b2c3d4e5f60718", label: "Mac · Chrome", current: true,
        created_at: "2026-09-04T23:20:54+00:00", last_seen_at: "2026-09-04T20:32:10+00:00",
        expires_at: "2026-10-04T23:20:54+00:00", days_left: 29.95 },
      { session_id: "99aa88bb77cc66dd", label: "iPhone · Safari", current: false,
        created_at: "2026-09-02T15:04:11+00:00", last_seen_at: "2026-09-04T18:47:02+00:00",
        expires_at: "2026-10-02T15:04:11+00:00", days_left: 27.6 },
    ],
  };

  const activity = {
    next_cursor: 41,
    events: [
      { id: 44, at: "2026-09-04T01:33:01.850146+00:00", kind: "proposal_created",
        subject: "2026-09-08-VTI-f4c6ec7f", actor: "system", version: null,
        outcome: "PROPOSED", description: "a proposal was created",
        detail: { ticker: "VTI", bucket: "micro_live", action: "BUY", notional_usd: 35.0 } },
      { id: 43, at: "2026-09-04T00:09:52.569799+00:00", kind: "research_completed",
        subject: "2026-09-02_VTI_2bf5a8b5", actor: "system", version: null,
        outcome: "COMPLETE", description: "a research run finished",
        detail: { ticker: "VTI", bucket: "micro_live", trade_date: "2026-09-02",
                  trigger: "scheduled", proposal_id: null } },
      { id: 42, at: "2026-09-03T22:14:00.000000+00:00", kind: "system_resumed",
        subject: null, actor: "owner", version: null, outcome: null,
        description: "the kill switch was released", detail: {} },
    ],
    kinds: {},
  };

  const labRoster = {
    "groups": [
      {
        "key": "analysts",
        "directory": "1_analysts"
      },
      {
        "key": "research",
        "directory": "2_research"
      },
      {
        "key": "trading",
        "directory": "3_trading"
      },
      {
        "key": "risk",
        "directory": "4_risk"
      },
      {
        "key": "portfolio",
        "directory": "5_portfolio"
      }
    ],
    "roles": [
      {
        "key": "market",
        "group": "analysts",
        "node": "Market Analyst",
        "report": "market",
        "analyst": "market"
      },
      {
        "key": "social",
        "group": "analysts",
        "node": "Sentiment Analyst",
        "report": "social",
        "analyst": "social"
      },
      {
        "key": "news",
        "group": "analysts",
        "node": "News Analyst",
        "report": "news",
        "analyst": "news"
      },
      {
        "key": "fundamentals",
        "group": "analysts",
        "node": "Fundamentals Analyst",
        "report": "fundamentals",
        "analyst": "fundamentals"
      },
      {
        "key": "bull",
        "group": "research",
        "node": "Bull Researcher",
        "report": "bull",
        "analyst": null
      },
      {
        "key": "bear",
        "group": "research",
        "node": "Bear Researcher",
        "report": "bear",
        "analyst": null
      },
      {
        "key": "manager",
        "group": "research",
        "node": "Research Manager",
        "report": "manager",
        "analyst": null
      },
      {
        "key": "trader",
        "group": "trading",
        "node": "Trader",
        "report": "trader",
        "analyst": null
      },
      {
        "key": "aggressive",
        "group": "risk",
        "node": "Aggressive Analyst",
        "report": "aggressive",
        "analyst": null
      },
      {
        "key": "neutral",
        "group": "risk",
        "node": "Neutral Analyst",
        "report": "neutral",
        "analyst": null
      },
      {
        "key": "conservative",
        "group": "risk",
        "node": "Conservative Analyst",
        "report": "conservative",
        "analyst": null
      },
      {
        "key": "portfolio",
        "group": "portfolio",
        "node": "Portfolio Manager",
        "report": "decision",
        "analyst": null
      }
    ],
    "analysts": {
      "etf": [
        "market",
        "news"
      ],
      "stock": [
        "market",
        "social",
        "news",
        "fundamentals"
      ],
      "crypto": [
        "market",
        "social",
        "news"
      ]
    },
    "call_timeout_seconds": 900
  };

  const labTrace = {
    "2026-09-04_VTI_873e64ae": {
      "run_id": "2026-09-04_VTI_873e64ae",
      "ticker": "VTI",
      "bucket": "micro_live",
      "trade_date": "2026-09-04",
      "created_at": "2026-09-05T06:18:43.090801+00:00",
      "stage": 3,
      "instrument_type": "etf",
      "elapsed_seconds": 412.6,
      "analysts": [
        "market",
        "news"
      ],
      "llm_usage": {
        "calls": 13,
        "input_tokens": 32,
        "output_tokens": 28011,
        "cache_creation_input_tokens": 150833,
        "cache_read_input_tokens": 71336,
        "list_price_equivalent_usd": 1.424,
        "wall_seconds": 411.4,
        "tool_calls": 16,
        "models": [
          "claude-opus-5",
          "claude-sonnet-5"
        ],
        "billing": "claude_subscription_via_claude_code_cli"
      },
      "confidence": 38,
      "policy_action": "NO_TRADE",
      "hard_risk_verdict": "NO_ACTION",
      "final_action": "NO_TRADE",
      "proposal_id": null,
      "error": null,
      "steps": [
        {
          "order": 1,
          "group": "1_analysts",
          "group_label": "Analysts",
          "key": "market",
          "role": "market",
          "label": "Market analyst",
          "chars": 6743,
          "truncated": false,
          "markdown": "**Interpretation (INTERPRETATION):** The stacked order (200 SMA < 50 SMA < 10 EMA ≈ price) is a classic bullish trend alignment. However, the gap between price and the 50/200 SMAs has widened meaningfully since June, which increases the risk of mean-reversion if momentum stalls — this is a broad-mar"
        },
        {
          "order": 2,
          "group": "1_analysts",
          "group_label": "Analysts",
          "key": "news",
          "role": "news",
          "label": "News analyst",
          "chars": 6475,
          "truncated": false,
          "markdown": "- 24/7 Wall St. and Motley Fool published multiple long-horizon \"buy and hold\" pieces framing VTI as a default core holding for retirement accounts (401(k) comparisons, 10–20 year projections). These are **editorial/marketing-style content**, not new fundamental data — no changes to fund flows, expe"
        },
        {
          "order": 3,
          "group": "2_research",
          "group_label": "Research debate",
          "key": "bear",
          "role": "bear",
          "label": "Bear case",
          "chars": 10392,
          "truncated": false,
          "markdown": "Bear Analyst: **Bear case for VTI — rebuttal**"
        },
        {
          "order": 4,
          "group": "2_research",
          "group_label": "Research debate",
          "key": "bull",
          "role": "bull",
          "label": "Bull case",
          "chars": 8567,
          "truncated": false,
          "markdown": "Bull Analyst: Let me open the debate, since the bear hasn't spoken yet."
        },
        {
          "order": 5,
          "group": "2_research",
          "group_label": "Research debate",
          "key": "manager",
          "role": "manager",
          "label": "Research manager",
          "chars": 5475,
          "truncated": false,
          "markdown": "**Recommendation**: Hold"
        },
        {
          "order": 6,
          "group": "3_trading",
          "group_label": "Trader",
          "key": "trader",
          "role": "trader",
          "label": "Trade plan",
          "chars": 1106,
          "truncated": false,
          "markdown": "**Action**: Hold"
        },
        {
          "order": 7,
          "group": "4_risk",
          "group_label": "Risk debate",
          "key": "aggressive",
          "role": "aggressive",
          "label": "Aggressive risk analyst",
          "chars": 3870,
          "truncated": false,
          "markdown": "Aggressive Analyst: Let me open the debate, since neither the conservative nor neutral analyst has spoken yet."
        },
        {
          "order": 8,
          "group": "4_risk",
          "group_label": "Risk debate",
          "key": "conservative",
          "role": "conservative",
          "label": "Conservative risk analyst",
          "chars": 3657,
          "truncated": false,
          "markdown": "Conservative Analyst: Let me push back directly on the aggressive analyst's case, because several of these arguments sound compelling but don't hold up under a capital-preservation mandate."
        },
        {
          "order": 9,
          "group": "4_risk",
          "group_label": "Risk debate",
          "key": "neutral",
          "role": "neutral",
          "label": "Neutral risk analyst",
          "chars": 3313,
          "truncated": false,
          "markdown": "Neutral Analyst: Both sides are overreaching from the same data set, and it's worth naming exactly where each one slips."
        },
        {
          "order": 10,
          "group": "5_portfolio",
          "group_label": "Portfolio manager",
          "key": "decision",
          "role": "portfolio",
          "label": "Portfolio decision",
          "chars": 4759,
          "truncated": false,
          "markdown": "**Rating**: Hold"
        }
      ]
    }
  };

  const config = {
    files: [
      { file: "risk_profile.yaml", revision: "demo-risk", sections: [
        { key: "execution_policy", lines: 78, guarded: true }, { key: "buckets", lines: 217, guarded: true },
        { key: "research_schedule", lines: 19, guarded: false }, { key: "notifications", lines: 65, guarded: false },
        { key: "micro_live_execution", lines: 89, guarded: true } ] },
      { file: "investor_profile.yaml", revision: "demo-investor", sections: [
        { key: "capital", lines: 13, guarded: true }, { key: "universe", lines: 27, guarded: false } ] },
      { file: "event_calendar.yaml", revision: "demo-calendar", sections: [
        { key: "maintained_through", lines: 4, guarded: false }, { key: "events", lines: 36, guarded: false } ] },
    ],
    values: {
      "research_schedule.enabled": true, "research_schedule.period": "weekly",
      "research_schedule.launchd.weekday": 5, "research_schedule.launchd.hour": 13,
      "research_schedule.launchd.minute": 30, "research_schedule.on_non_session_day": "previous_session",
      "research_schedule.tickers": [], "research_schedule.max_tickers_per_run": 3,
      "research_schedule.max_analyses_per_day": 6, "research_schedule.run_while_paused": true,
      "maintained_through": "2026-12-10",
      "events": [{ date: "2026-10-28", kind: "fomc_decision", note: "October 27-28 meeting; statement day" },
                 { date: "2026-11-10", kind: "cpi", note: "October 2026 CPI" }],
    },
    cooling_hours: 24,
    pending: [{ id: "demo1", file: "risk_profile.yaml", kind: "values", key: null, classification: "cooling",
                changes: [{ path: "micro_live_execution.max_capital_usd", from: 2000, to: 2500, effect: "loosening" }],
                reasons: ["micro_live_execution.max_capital_usd"], paths: ["micro_live_execution.max_capital_usd"],
                requested_at: "2026-09-16T10:00:00+00:00", requested_by: "owner", reason: "",
                apply_after: "2026-09-17T10:00:00+00:00", status: "pending", applied_at: null, outcome: {}, error: null }],
    recent: [{ id: "demo0", file: "investor_profile.yaml", kind: "values", key: null, classification: "immediate",
               changes: [{ path: "universe.core.individual_stocks.watchlist", from: [], to: ["AAPL"], effect: "neutral" }],
               reasons: [], paths: ["universe.core.individual_stocks.watchlist"],
               requested_at: "2026-09-15T09:00:00+00:00", requested_by: "owner", reason: "first candidate",
               apply_after: null, status: "applied", applied_at: "2026-09-15T09:00:00+00:00",
               outcome: { git: "committed 1a2b3c4" }, error: null }],
    autonomy: { armed: false, withdrawn: false, reason: null, at: null },
    /* What research studies and reads, as Settings draws its three switches. The SEC contact
       is only ever "set" or not: the words themselves stay in .env. */
    research: {
      fires_every: "weekly",
      switches: {
        candidates: { on: true, path: "research_schedule.accounts.micro_live.candidates.enabled", available: true,
                      period: "weekly", max_tickers_per_run: 3, tickers: ["VTI", "VXUS", "BND"] },
        sec_form4: { on: true, path: "research_web_access.sources.sec_form4.enabled", available: true,
                     contact: { set: true, variable: "CT_SEC_CONTACT" } },
        reddit: { on: false, path: "research_web_access.sources.reddit.enabled", available: true },
      },
    },
    launchd_managed: true,
  };

  /* GET /advisor with no account: the saved Robinhood snapshot and what the assistant has
     recorded about it. One fixture for every screen that reads it: Home draws the accounts
     and each holding's analyses; the Assistant's Discover reads the candidates and jobs.
     Every figure inside a holding agrees with the others: value = quantity x price, weight
     = value / the account's equity, return = price / average cost - 1. */
  const quoteAt = "2026-09-04T20:00:00+00:00";
  const holding = (symbol, quantity, average_cost, price, market_value_usd, unrealized_pct, weight_pct) =>
    ({ symbol, quantity, average_cost, price, market_value_usd, unrealized_pct, quote_at: quoteAt, weight_pct });
  const personalAll = { scope: "personal", account_scope_kind: "all_personal", account_id: null,
                        account_ids: [personalAccountA, personalAccountB] };
  const personalTask = (kind, account_id) => account_id
    ? { kind, language: "zh", scope: "personal", account_scope_kind: "single", account_id, account_ids: [] }
    : { kind, language: "zh", ...personalAll };
  const analysis = (run_id, ticker, account_id, trade_date, created_at, action, confidence, proposal, superseded_by = null) => ({
    run_id, ticker, bucket: "advisory", account_id, created_at, trade_date, action, advisory: true, proposal_id: null,
    policy: { final_action: action, size_hint: "none", pm_rating: { ADD: "Overweight", HOLD: "Hold", REDUCE: "Underweight", NO_TRADE: "Hold" }[action],
              llm_action: action, confidence, reasons: [], error: null,
              proposal: { ticker, recommended_action: action, confidence, market_risk_level: "NORMAL", data_quality: "partial",
                          expected_holding_period: "multi-year, reviewed each earnings season", ...proposal } },
    scope: "personal", account_scope_kind: account_id ? "single" : "all_personal",
    account_label: account_id === personalAccountA ? "••••1111" : account_id === personalAccountB ? "••••2222" : "all personal accounts",
    task_type: "analysis", superseded_by });
  const nvdaRun = "advisor_2f6c1d8e4b7a49c3a1e0d5b8c7f6e3a2";
  const tslaRun = "advisor_9a4e7c2b1d8f4e6a8b3c5d7e9f1a2b4c";
  const advisor = {
    snapshot: {
      fetched_at: "2026-09-04T20:31:00+00:00", complete: true,
      accounts: [
        { id: personalAccountA, label: "••••1111", agentic: false, complete: true, equity_usd: 45249.5, cash_usd: 3737.0, positions: [
          holding("VOO", 30, 412.1, 548.2, 16446.0, 33.026, 36.3452),
          holding("AAPL", 25, 182.3, 226.8, 5670.0, 24.4103, 12.5305),
          holding("NVDA", 40, 98.5, 131.4, 5256.0, 33.401, 11.6156),
          holding("MSFT", 12, 402.0, 431.5, 5178.0, 7.3383, 11.4432),
          holding("SCHD", 150, 26.4, 27.95, 4192.5, 5.8712, 9.2653),
          holding("AMZN", 20, 186.0, 178.9, 3578.0, -3.8172, 7.9073),
          holding("TSLA", 5, 262.0, 238.4, 1192.0, -9.0076, 2.6343) ] },
        { id: personalAccountB, label: "••••2222", agentic: false, complete: true, equity_usd: 12480.4, cash_usd: 612.4, positions: [
          holding("VTI", 20, 238.0, 281.3, 5626.0, 18.1933, 45.0787),
          holding("QQQ", 10, 402.0, 478.6, 4786.0, 19.0547, 38.3481),
          holding("BND", 20, 73.1, 72.8, 1456.0, -0.4104, 11.6663) ] },
      ],
      candidates: [
        ["NVDA", "watchlists:AI & chips", "scans:52-week highs"], ["AVGO", "watchlists:AI & chips", "scans:52-week highs"],
        ["AMD", "watchlists:AI & chips"], ["TSM", "watchlists:AI & chips"], ["ARM", "watchlists:AI & chips"],
        ["META", "scans:52-week highs"], ["NFLX", "scans:52-week highs"], ["COST", "scans:52-week highs", "watchlists:Quality compounders"],
        ["SCHD", "watchlists:Dividend income", "profile:defensive_equity_optional"], ["VYM", "watchlists:Dividend income"],
        ["JNJ", "watchlists:Dividend income"], ["KO", "watchlists:Dividend income"], ["PG", "watchlists:Dividend income", "watchlists:Quality compounders"],
        ["V", "watchlists:Quality compounders"], ["MA", "watchlists:Quality compounders"], ["LLY", "scans:Top analyst upgrades"],
        ["UNH", "scans:Top analyst upgrades"], ["VOO", "profile:us_broad_equity"], ["VTI", "profile:us_broad_equity"],
        ["VXUS", "profile:international_equity"], ["BND", "profile:us_total_bond"], ["VGIT", "profile:us_treasury_intermediate"],
        ["SGOV", "profile:cash_like_tbill"], ["IAU", "profile:gold_optional"],
      ].map(([symbol, ...sources]) => ({ symbol, sources })),
      errors: ["Top analyst upgrades: only the first candidate page was read."],
    },
    stale: false,
    notes: { text: "Long-term investor, 15+ year horizon. Keep single stocks under 15% each; no leverage or options.",
             revision: 3, updated_at: "2026-09-01T16:20:00+00:00" },
    jobs: [
      { run_key: "advisor_5d1f3a9c7e2b4d6f8a0c2e4f6a8b0d2e", ticker: "ASSISTANT", state: "COMPLETE", error: null,
        created_at: "2026-09-04T20:30:12+00:00", task: personalTask("refresh") },
      { run_key: "advisor_4b7e0d1c9a2f4e8b8c5d6e7f8a9b0c1d", ticker: "AVGO", state: "RUNNING", error: null,
        created_at: "2026-09-04T20:25:40+00:00", task: personalTask("analysis") },
      { run_key: nvdaRun, ticker: "NVDA", state: "COMPLETE", error: null,
        created_at: "2026-09-04T00:52:10+00:00", task: personalTask("analysis", personalAccountA) },
      { run_key: "advisor_1c3e5a7b9d0f4a2c8e6b4d2f0a8c6e4a", ticker: "AMD", state: "COMPLETE", error: null,
        created_at: "2026-09-03T23:40:05+00:00", task: personalTask("analysis") },
      { run_key: tslaRun, ticker: "TSLA", state: "COMPLETE", error: null,
        created_at: "2026-08-29T01:02:44+00:00", task: personalTask("analysis", personalAccountA) },
    ],
    conversations: [],
    decisions: [
      analysis(nvdaRun, "NVDA", personalAccountA, "2026-09-03", "2026-09-04T01:10:22+00:00", "HOLD", 54, {
        investment_thesis: "Data-centre demand still carries the story: the last two quarters beat on revenue and guided higher, and the position is already up about a third from cost. At roughly 12% of this account it is a meaningful weight, so adding more would concentrate one theme further while the next guidance is unknown.",
        bull_case: "Hyperscaler capex plans for next year were raised again.", bear_case: "Valuation assumes several more years of above-trend growth.",
        primary_risks: ["Export restrictions on advanced chips widen", "Hyperscaler capex slows after the current build-out", "Valuation leaves little room for a guidance miss"],
        what_could_make_this_thesis_wrong: "A guidance cut at the next report, or a second quarter of falling gross margin.",
        entry_logic: "No addition at this weight. Revisit after the next earnings report if guidance holds and the position is below 10% of the account.",
        exit_logic: "Trim toward 8% of the account if it closes more than 25% below its 200-day average, or if data-centre revenue guidance is cut.",
        maximum_acceptable_loss: "A 30% drawdown from here before the thesis is re-examined in full.",
        reason_this_improves_the_portfolio: "Keeps growth exposure without adding to an already large single-theme weight.",
        alternative_why_doing_nothing_may_be_preferable: "Holding is already the decision; the position reflects the thesis.",
        missing_information: ["No options-implied volatility was available", "Next earnings date not confirmed from a primary source"] }),
      analysis("advisor_1c3e5a7b9d0f4a2c8e6b4d2f0a8c6e4a", "AMD", null, "2026-09-03", "2026-09-04T00:21:47+00:00", "NO_TRADE", 41, {
        investment_thesis: "A credible second source for AI accelerators, but the case for share gains still rests on guidance rather than shipped revenue. Your accounts already hold NVDA and QQQ, so a new position would add to the same semiconductor exposure.",
        bull_case: "New accelerator generation is sampling with two hyperscalers.", bear_case: "Share gains have been promised before and arrived late.",
        primary_risks: ["Accelerator roadmap slips a generation", "PC and gaming segments stay weak"],
        what_could_make_this_thesis_wrong: "Two consecutive quarters of reported data-centre GPU revenue growth above 40%.",
        entry_logic: "Consider a starter position only after two quarters of reported data-centre GPU revenue growth.",
        exit_logic: "Not applicable until a position exists.",
        maximum_acceptable_loss: "Not applicable.", reason_this_improves_the_portfolio: "It would not, at current overlap.",
        alternative_why_doing_nothing_may_be_preferable: "Existing NVDA and QQQ holdings already carry this exposure.",
        missing_information: ["Customer-level accelerator orders are not disclosed"] }),
      analysis("advisor_6f8a0c2e4b1d4f3a9e7c5b3d1f0e2a4c", "QQQ", personalAccountB, "2026-08-25", "2026-08-26T02:14:09+00:00", "HOLD", 58, {
        investment_thesis: "A low-cost growth core for a long-horizon account. Nothing in the evidence argues for changing it; note that it overlaps with NVDA and MSFT held in your other account.",
        bull_case: "Earnings growth of the largest holdings remains above the market's.", bear_case: "Top ten names are over half the fund.",
        primary_risks: ["Concentration in a handful of mega-cap names", "Rate-driven de-rating of growth stocks"],
        what_could_make_this_thesis_wrong: "A multi-year stretch in which the largest names lag the broad market.",
        entry_logic: "Add only through regular contributions, not in a lump sum.",
        exit_logic: "No price-based exit; rebalance if it exceeds half of this account.",
        maximum_acceptable_loss: "Index-level drawdowns are expected and tolerated.", reason_this_improves_the_portfolio: "Growth tilt for the long horizon.",
        alternative_why_doing_nothing_may_be_preferable: "Holding is the plan.", missing_information: [] }),
      analysis(tslaRun, "TSLA", personalAccountA, "2026-08-28", "2026-08-29T01:31:52+00:00", "REDUCE", 61, {
        investment_thesis: "Deliveries have fallen year over year for three quarters and price cuts are compressing margins. The position is small, under 3% of the account and 9% below cost; the thesis that made it a buy has weakened more than the price has.",
        bull_case: "Energy storage is growing quickly.", bear_case: "Automotive margins keep falling with each price cut.",
        primary_risks: ["Further price cuts", "Key-person risk", "Regulatory review of driver-assistance claims"],
        what_could_make_this_thesis_wrong: "A return to delivery growth with stable automotive gross margin.",
        entry_logic: "No additions while deliveries are falling.",
        exit_logic: "Reduce by half; exit the rest if next quarter's automotive gross margin is below 15%.",
        maximum_acceptable_loss: "The remaining half is small enough to hold through the next report.",
        reason_this_improves_the_portfolio: "Frees capital from a weakening thesis.",
        alternative_why_doing_nothing_may_be_preferable: "The position is too small to move the account either way.",
        missing_information: ["No reliable timeline for robotaxi revenue"] }),
      analysis("advisor_3a5c7e9b1d2f4a6c8e0b2d4f6a8c0e2b", "NVDA", personalAccountA, "2026-08-20", "2026-08-21T01:48:33+00:00", "ADD", 63, {
        investment_thesis: "Before the last report: data-centre revenue growth and a position under 10% of the account made a measured addition reasonable.",
        bull_case: "Order visibility extends into next year.", bear_case: "Expectations are already high.",
        primary_risks: ["Guidance disappointment at the next report", "Export restrictions"],
        what_could_make_this_thesis_wrong: "A guidance cut.",
        entry_logic: "Add up to 10 shares below $125.", exit_logic: "Reassess after the next earnings report.",
        maximum_acceptable_loss: "25% from the addition price.", reason_this_improves_the_portfolio: "Brings a winning position toward its target weight.",
        alternative_why_doing_nothing_may_be_preferable: "The position was already profitable.",
        missing_information: ["Next earnings date not confirmed from a primary source"] }, nvdaRun),
    ],
    coverage: "All Robinhood brokerage accounts, stocks/ETFs only; analysis is read-only.",
    ...personalAll, account_label: "all personal accounts",
    coverage_info: { scope: "personal", instruments: "stocks_and_etfs",
                     note: "All Robinhood brokerage accounts, stocks/ETFs only; analysis is read-only." },
    conversation_summaries: [],
  };

  const orders = { fills: [], tracked_orders: [] };
  research.forEach(row => Object.assign(row, { scope: row.bucket === 'micro_live' ? 'agentic' : 'demo',
    account_scope_kind: 'single', account_id: row.bucket === 'micro_live' ? 'micro_live' : 'conceptual',
    account_label: row.bucket === 'micro_live' ? 'Agentic' : 'Demo', task_type: 'full_research' }));

  /* The Lab's two lists, read from the same snapshot the Assistant shows: what the accounts
     hold, then the watchlist names they do not, each with its newest record, the way
     GET /research/subjects answers. */
  const researchSubjects = (() => {
    const records = [...research.filter(r => r.scope !== 'demo'), ...advisor.decisions.filter(d => d.task_type === 'analysis')
      .map(d => ({ ...d, final_action: d.policy.final_action, policy_action: d.policy.final_action, confidence: d.policy.confidence }))];
    const latest = symbol => records.filter(r => r.ticker === symbol)
      .sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)))[0] || null;
    const holders = new Map();
    for (const account of advisor.snapshot.accounts) {
      for (const position of account.positions) {
        if (!holders.has(position.symbol)) holders.set(position.symbol, []);
        holders.get(position.symbol).push({ scope: 'personal', account_id: account.id, account_label: account.label });
      }
    }
    const lists = symbol => (advisor.snapshot.candidates.find(c => c.symbol === symbol)?.sources || [])
      .filter(source => source.startsWith('watchlists:')).map(source => source.slice('watchlists:'.length));
    const subject = (symbol, held) => ({ symbol, holders: held, lists: lists(symbol), latest: latest(symbol), running: null,
      start: held.length ? { scope: 'personal', kind: 'analysis', account_id: held[0].account_id }
        : { scope: 'agentic', bucket: 'micro_live', purpose: 'candidate' } });
    return {
      checked_at: advisor.snapshot.fetched_at,
      held: [...holders].map(([symbol, held]) => subject(symbol, held)),
      added: advisor.snapshot.candidates.filter(c => !holders.has(c.symbol) && lists(c.symbol).length)
        .map(c => subject(c.symbol, [])),
      snapshot: { read: true, fetched_at: advisor.snapshot.fetched_at, age_seconds: 600, errors: advisor.snapshot.errors },
      errors: [],
    };
  })();

  /* Automatic trading, as /auto-trading and /auto-trading/live answer. Invented: a steady plan
     twelve sessions into its simulation, nothing in force on the real account, and the
     conditions that stand in front of it. No figure here is a result of anything. */
  const pair = (zh, en) => ({ zh, en });
  const steady = { template: 'research_long_only', max_positions: 3, position_pct: 33.0, min_confidence: 80,
    stop_loss_pct: 10.0, take_profit_pct: 0.0, max_holding_days: 90, trend_window: 0, universe: ['VTI', 'VXUS', 'BND'],
    rationale: pair('三只宽基和债券基金各占三分之一，研究说买且把握度够才买。', 'A third each in three broad and bond funds, bought only when research says so with enough confidence.'),
    suggested_by: 'template', suggestion_error: null };
  const autoEnvelope = { mode: 'steady', capital_usd: 100.0, loss_pause_pct: 10.0, account: 'micro_live', loss_pause_usd: 10.0 };
  const autoModes = [
    { key: 'steady', order: 1, label: pair('稳健', 'Steady'), cadence: 'weekly', simulated_days: 12, open_for_real_money: false, can_simulate: true,
      runs_as: pair('每周研究一次；研究说买且把握度够才买；跌到止损线或持有到期卖出。', 'Researched weekly; bought when research says so with enough confidence; sold at its stop or at the end of its holding period.'),
      blockers: [
        { code: 'simulation_record_short', waits_on: 'record', text: pair('实时模拟只有 12 个有效交易日，需要至少 30 个（等待样本）。', 'Only 12 session(s) of live simulation are on record; 30 are needed.') },
        { code: 'exit_not_exercised', waits_on: 'record', text: pair('模拟账户里还没有完成过一次“买入→卖出”的完整流程。', 'The simulated account has not yet carried one position from purchase to sale.') }] },
    { key: 'swing', order: 2, label: pair('波段', 'Swing'), cadence: 'daily', simulated_days: 0, open_for_real_money: false, can_simulate: true,
      runs_as: pair('每个交易日研究一次；研究说买、并且收盘价在自己的均线上方才买；跌到止损线、跌破均线或持有到期卖出。', 'Researched every session; bought when research says so and the close is above its own average; sold at its stop, below that average, or at the end of a shorter holding period.'),
      blockers: [
        { code: 'earlier_mode_not_open', waits_on: 'record', text: pair('按顺序开放：需要先开放稳健模式。', 'Modes open in order: Steady must open first.') },
        { code: 'swing_policy_undecided', waits_on: 'owner', text: pair('波段模式用趋势规则择时，是否属于“动量策略须人工确认”需要你来定。', 'Swing times purchases with a trend rule; whether that falls under the rule for momentum strategies is yours to decide.') }] },
    { key: 'intraday', order: 3, label: pair('短线', 'Intraday'), cadence: 'intraday', simulated_days: 0, open_for_real_money: false, can_simulate: false,
      runs_as: pair('开盘前由模型定一次当天的计划；盘中由本地规则执行。尚未实现。', 'A model sets the day\'s plan once before the open; local rules carry it out. Not built.'),
      blockers: [{ code: 'intraday_not_built', waits_on: 'build', text: pair('分钟级行情接入、行情时效检查和盘中规则执行尚未实现。', 'Minute data, its freshness checks and the in-session rule runner are not built.') }] }];
  const autoPlan = { plan_id: 'demo0plan0steady', envelope: autoEnvelope, strategy: steady, opening_version: 'sv-demo00000001',
    managing: [], confirmed_at: '2026-09-18T20:05:00+00:00', confirmed_by: 'owner', deployable_usd: 99.0 };
  const auto = {
    checked_at: health.checked_at, account: { scope: 'agentic', account_id: 'micro_live' }, modes: autoModes,
    extensions: [{ key: 'stocks', label: pair('美股个股', 'Single stocks'), open_for_real_money: false, can_simulate: false,
      blockers: [{ code: 'stocks_need_their_own_record', waits_on: 'record', text: pair('ETF 模式的模拟记录和实盘开放不能算作个股的记录。', 'A fund mode\'s simulated record, and its being open, do not count for stocks.') }] }],
    simulation: {
      exists: true, plan: autoPlan, state: { paused: false, reason: null, kind: null, at: null, by: null, loss_baseline_usd: 0.0 },
      record: { sessions: 12, from: '2026-09-18', to: '2026-10-05', net_return_pct: 0.42, max_drawdown_pct: -0.31,
        equity_usd: 100.42, funded_usd: 100.0, result_usd: 0.42, average_exposure_pct: 33.1, measured_from: 'funding' },
      version: { version_id: 'sv-demo00000001', mode: 'steady', validation: { status: 'simulating', simulated_days: 12, exits_verified: false } },
      sessions: { counted: 12, needed_to_open: 30, needed_to_replace: 180, recorded: 13,
        not_counted: [{ session: '2026-09-24', problems: ['regime_unknown'], open_passes: 9 }] },
      account: { as_of: '2026-10-05', at: '2026-10-05T19:58:00+00:00', equity_usd: 100.42, cash_usd: 67.0, invested_usd: 33.42,
        positions: 1, funded_usd: 100.0, result_usd: 0.42, result_pct: 0.42 },
      funding: { net_usd: 100.0, deposits_usd: 100.0, withdrawals_usd: 0.0, cash_usd: 67.0 },
      positions: [{ symbol: 'VTI', bucket: 'micro_live', managed_quantity: 0.087562, strategy_version: 'sv-demo00000001',
        opened_at: '2026-09-22T14:02:00+00:00', cost_basis: 376.88, other_quantity: 0.0, reason: null }],
      trades: [], signals: [],
      research: { universe: ['VTI', 'VXUS', 'BND'], covered: ['VTI'], missing: ['VXUS', 'BND'], candidates_enabled: false, on_watchlist: ['VTI'],
        text: pair('每周研究目前不会研究 VXUS、BND，所以模拟账户不会收到它们的买入信号。', 'Scheduled research does not currently study VXUS, BND, so the simulated account will get no signal to buy them.') },
    },
    real_account: { plan: null, open: false, mode: null, state: { paused: false }, opened_modes: [], request: null },
  };
  const autoGate = (code, ok, waits_on, zh, en) => ({ code, ok, waits_on, text: pair(zh, en) });
  const autoChanges = [
    { file: 'risk_profile.yaml', path: 'buckets.micro_live.autonomy.open.enabled', from: false, to: true, kind: 'boundary',
      why: pair('打开“自动买入”开关。实盘上这一步需要你的控制台密码。', 'Turns the automatic-buying switch on. On the real account this takes your console password.') },
    { file: 'risk_profile.yaml', path: 'buckets.micro_live.autonomy.managed_exit.enabled', from: null, to: true, kind: 'boundary',
      why: pair('允许代码卖出它自己按方案买入的仓位。你自己买入的仓位不在其内。', 'Lets code sell positions it opened itself under the plan. Positions you bought are not covered.') }];
  const autoPlanExample = {
    plan_id: 'demo0plan0steady', envelope: autoEnvelope, mode: autoModes[0], strategy: steady, clamps: [],
    summary: pair('按这个方案，可用资金 $100 里实际能投入约 $99：3 个仓位（$33.00、$33.00、$33.00）。亏损达到 $10.00（10%）时暂停新开仓。',
      'Under this plan about $99 of the $100 can actually be invested: 3 position(s) ($33.00, $33.00, $33.00). New positions stop once the loss reaches $10.00 (10%).'),
    account: { account_id: 'micro_live', equity_usd: 100.0, cash_usd: 100.0, positions: 0, as_of: '2026-10-05' },
    today: { any_order: false, deployable_usd: 0.0, positions: 0, path: [] },
    under_plan: { any_order: true, deployable_usd: 99.0, positions: 3,
      path: [{ symbol: 'VTI', feasible: true, order_usd: 33.0, why_not: null }, { symbol: 'VXUS', feasible: true, order_usd: 33.0, why_not: null },
        { symbol: 'BND', feasible: true, order_usd: 33.0, why_not: null }] },
    changes: autoChanges,
    landing: { classification: 'cooling', needs_password: true, cooling_hours: 24, errors: [],
      changes: autoChanges.map(c => ({ file: c.file, path: c.path, effect: 'loosening', waits: true })),
      summary: pair('其中 2 项会放宽现有限制：在实盘账户上需要控制台密码确认，并等待 24 小时后生效，期间可以撤回。', '2 of these loosen a limit: on the real account they need the console password and take effect after 24 hours, and can be withdrawn until then.') },
    notes: { loss_line: pair('亏损暂停线（$10.00）是本地检查，不是最大亏损的保证。', 'The loss line ($10.00) is a local check. It is not a guarantee of the most that can be lost.') },
  };
  const autoLive = {
    checked_at: health.checked_at, account: { scope: 'agentic', account_id: 'micro_live' },
    plan: null, state: { paused: false, reason: null, kind: null }, record: null, request: null, ended: null,
    positions: [], trades: [], account_mark: null, last_pass: null,
    replacement: { automatic: false, last: null, looked: 0 },
    candidate: { available: true, plan: autoPlanExample, version_id: 'sv-demo00000001', may_confirm: false, open: false,
      inputs: { plan_id: 'demo0plan0steady', mode: 'steady', capital_usd: 100.0, loss_pause_pct: 10.0, strategy: steady },
      gates: [
        autoGate('simulation_record', false, 'record', '策略版本 sv-demo00000001 只有 12 个有效模拟交易日，需要至少 30 个（等待样本）。', 'Strategy version sv-demo00000001 has 12 valid simulated session(s) and needs 30.'),
        autoGate('exit_exercised', false, 'record', '这个策略版本在模拟账户里还没有完成过一次“代码买入→代码卖出”的完整流程。', 'This strategy version has not yet carried one position from its own purchase to its own sale.'),
        autoGate('real_money_on', true, 'owner', '这个账户的实盘执行开关是打开的。', 'Real-money execution is switched on for this account.'),
        autoGate('orders_settled', true, 'account', '没有结果不明或尚未对账的订单。', 'No order is unresolved or waiting to be reconciled.'),
        autoGate('book_confirmed', true, 'account', 'Agentic 账本已与券商核对一致（账本日期 2026-10-05）。', 'The Agentic ledger agrees with the venue (dated 2026-10-05).'),
        autoGate('settings_pending', false, 'settings', '方案需要的 2 项设置还没有生效。放宽限制的需要控制台密码，并等待 24 小时后生效，期间可以撤回。', '2 setting(s) the plan needs are not in force. One that loosens a limit takes the console password and 24 hours.')],
      advisories: [{ code: 'pause_is_not_a_guarantee', text: autoPlanExample.notes.loss_line }] },
  };
  const autoPerformanceExample = {
    exists: true, note: pair('实时模拟和历史回测是两种不同的记录，分开列出，不相加。', 'The live simulation and the historical backtests are different kinds of record and are listed apart.'),
    simulation: { comparators_unavailable: null,
      caveat: pair('已记录 12 个交易日。这是发生过的事情的记录，不是策略有效的证据。', '12 session(s) on record. This is a record of what happened, not evidence that the strategy works.'),
      rows: {
        strategy: { figures: { net_return_pct: 0.42, max_drawdown_pct: -0.31 }, says: pair('策略：模拟账户自己的记录。', 'Strategy: the simulated account\'s own record.') },
        passive: { figures: { net_return_pct: 0.61, max_drawdown_pct: -0.88 }, says: pair('被动持有：买入方案里同样的基金、同样的仓位，一直持有不卖。', 'Passive: the plan\'s own funds at the plan\'s own size, bought and held.') },
        cash: { figures: { net_return_pct: 0.0, max_drawdown_pct: 0.0 }, says: pair('现金：钱放着不动。', 'Cash: the money left alone.') } } },
  };

  return {
    health, portfolio, risk, microLive, proposals, proposalDetail,
    approveExecuted, approveReanalysis, research, researchJobs, researchSubjects, orders,
    schedule, operations, notifications, evaluation, evaluationExamples, labRoster, labTrace,
    pushDevices: { devices: [] }, authSessions, activity, config, authority, attention, accounts, advisor,
    auto, autoLive, autoPlanExample, autoPerformanceExample,
  };
})();
