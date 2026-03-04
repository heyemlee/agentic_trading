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
    evaluation: null,
    activity: null,
    proposals: [], proposalDetail: null, research: [], researchJobs: [], researchHistory: [], researchQueue: [], researchRun: null,
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

  const authorityVersion = "a11ce" + "0".repeat(59);
  const authorityBlock = {
    code: "real_money_disabled",
    detail: "Demo data cannot place a real-money order.",
  };
  const authorityAction = (action) => ({
    mode: "blocked",
    autonomous: { permitted: false, blocked_by: [authorityBlock] },
    human_approval: { permitted: false, blocked_by: [authorityBlock] },
    limits: action === "SELL"
      ? { quantity_basis: "full ledger holding" }
      : { max_order_usd: 35.0, max_position_pct: 40.0 },
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
    kill_switch: { engaged: false, reason: null, at: null },
    withdrawn: { active: false, reason: null, at: null },
    actions: Object.fromEntries(["BUY", "ADD", "REDUCE", "SELL"].map(action => [action, authorityAction(action)])),
    limits: { capital: { max_capital_usd: 100.0 }, tiers: { low: { max_order_usd: 35.0 } } },
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

  const research = [
    { run_id: "2026-09-04_VTI_873e64ae", ticker: "VTI", bucket: "core", trade_date: "2026-09-04", instrument_type: "etf", created_at: "2026-09-05T06:18:43.090801+00:00", portfolio_manager_rating: "Hold", policy_action: "NO_TRADE", confidence: 38, hard_risk_verdict: "NO_ACTION", final_action: "NO_TRADE", proposal_id: null, llm_calls: 13, elapsed_seconds: 412.6 },
    { run_id: "2026-09-02_VTI_2bf5a8b5", ticker: "VTI", bucket: "micro_live", trade_date: "2026-09-02", instrument_type: "etf", created_at: "2026-09-04T00:09:52.569799+00:00", portfolio_manager_rating: "Hold", policy_action: "NO_TRADE", confidence: 38, hard_risk_verdict: "NO_ACTION", final_action: "NO_TRADE", proposal_id: null, llm_calls: 13, elapsed_seconds: 420.9 },
    { run_id: "2026-09-02_VTI_f819eaa6", ticker: "VTI", bucket: "core",       trade_date: "2026-09-02", instrument_type: "etf", created_at: "2026-09-03T21:05:24.598310+00:00", portfolio_manager_rating: "Hold", policy_action: "NO_TRADE", confidence: 57, hard_risk_verdict: "NO_ACTION", final_action: "NO_TRADE", proposal_id: null, llm_calls: 13, elapsed_seconds: 582.7 },
    { run_id: "2026-09-02_VTI_56717678", ticker: "VTI", bucket: "core",       trade_date: "2026-09-02", instrument_type: "etf", created_at: "2026-09-03T20:37:50.909749+00:00", portfolio_manager_rating: "Hold", policy_action: "NO_TRADE", confidence: 44, hard_risk_verdict: null,        final_action: null,       proposal_id: null, llm_calls: 7,  elapsed_seconds: 302.9 },
  ];

  const researchJobs = [{
    run_key: "2026-09-04_VTI_873e64ae", dedupe_key: "demo:VTI:2026-09-04",
    ticker: "VTI", bucket: "core", trade_date: "2026-09-04", instrument_type: "etf",
    trigger: "scheduled", state: "COMPLETE", created_at: "2026-09-04T20:30:00+00:00",
    updated_at: "2026-09-04T20:31:30+00:00", started_at: "2026-09-04T20:30:02+00:00",
    finished_at: "2026-09-04T20:31:30+00:00", deadline_at: "2026-09-04T21:30:02+00:00",
    pid: null, attempts: 1, max_attempts: 2,
    timeout_seconds: 3600, log: "Research completed without a proposal.", exit_code: 0, error: null,
    limit_recoveries: 0, proposal_created: false, proposal_id: null, task: null,
    authority_version: authorityVersion, opened_at: null, purpose: "candidate",
    request_id: "11111111-1111-4111-8111-111111111111", scope: "demo",
    account_scope_kind: "single", account_id: "conceptual", account_ids: [],
    account_label: "Demo", task_type: "full_research",
    route: { name: "tradingagents", inferred: false, package_version: "demo",
      upstream_commit: "0000000000000000000000000000000000000000", skill_version: null,
      models: { quick: "sample-model", deep: "sample-model" } },
    cancellable: false,
  }];

  const schedule = {
    enabled: true, period: "weekly", calendar: "XNYS", market_timezone: "America/New_York",
    on_non_session_day: "previous_session", run_while_paused: true,
    tickers: ["VTI", "BND", "BIL"],
    next_run_at: "2026-09-11T13:30:00-07:00",
    current_period: "weekly:2026-W36", would_run_now: false,
    reason: "already executed for weekly:2026-W36 at 2026-09-04T20:30:11+00:00",
    analyses_today: 1, daily_budget: 6,
    last_period: "weekly:2026-W36",
    last_run: {
      at: "2026-09-04T20:30:11+00:00", trade_date: "2026-09-03",
      tickers: ["VTI", "BND", "BIL"], state: "STARTED",
      run_keys: ["2026-09-03_VTI_2bf5a8b5"], errors: [],
    },
    last_skip: {
      at: "2026-08-28T20:30:07+00:00", period_key: "weekly:2026-W35",
      reason: "2026-08-28 is not a trading session for XNYS",
    },
  };

  const operations = {
    checked_at: "2026-09-04T20:32:16+00:00",
    activities: [
      { id: "reconciliation", last_success_at: "2026-09-04T20:32:01+00:00", age_minutes: 0.3,
        expected_every_minutes: 5, overdue: false, detail: "background loop in the API process" },
      { id: "research_dispatch", last_success_at: "2026-09-04T20:32:01+00:00", age_minutes: 0.3,
        expected_every_minutes: 5, overdue: false, detail: "queue drained, 0 waiting" },
      { id: "backup", last_success_at: "2026-09-04T10:15:04+00:00", age_minutes: 617.2,
        expected_every_minutes: 1440, overdue: false, detail: "5 local copies, integrity ok" },
      { id: "maintenance", last_success_at: null, age_minutes: null,
        expected_every_minutes: 1440, overdue: true, detail: "has not run since installation" },
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
      "bucket": "core",
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
      "universe.core.individual_stocks.watchlist": ["AAPL"],
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
    launchd_managed: true,
  };

  const orders = { fills: [], tracked_orders: [] };
  research.forEach(row => Object.assign(row, { scope: row.bucket === 'micro_live' ? 'agentic' : 'demo',
    account_scope_kind: 'single', account_id: row.bucket === 'micro_live' ? 'micro_live' : 'conceptual',
    account_label: row.bucket === 'micro_live' ? 'Agentic' : 'Demo', task_type: 'full_research' }));

  return {
    health, portfolio, risk, microLive, proposals, proposalDetail,
    approveExecuted, approveReanalysis, research, researchJobs, orders,
    schedule, operations, notifications, evaluation, evaluationExamples, labRoster, labTrace,
    pushDevices: { devices: [] }, authSessions, activity, config, authority, attention, accounts,
  };
})();
