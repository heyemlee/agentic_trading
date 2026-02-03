"""The shapes the API returns.

Every route in ``api/`` is a one-line pass-through of a ``service`` call, so what a screen
receives is decided here and nowhere else. Until these models existed that shape was only ever
``dict[str, Any]``: the published OpenAPI document described each response as
``additionalProperties: true``, and the console's test fixtures were hand-written guesses that
stayed green when a field was renamed underneath them.

A response model is enforcement, not documentation — FastAPI validates against it, and a
required field the service did not supply becomes a 500. Two rules follow, and both are
load-bearing for a system that holds real money:

1. **Model the degraded shape first.** These endpoints fail closed on purpose: a broker that
   will not connect, a ledger nothing has confirmed, a config that will not load. Every field a
   degraded path can omit is optional here. Get this wrong and the endpoint stops answering at
   exactly the moment its answer matters.
2. **``extra="forbid"`` everywhere.** Pydantic's default is to drop unknown fields, which would
   turn "the service grew a field and the model did not" into a value that silently never
   reaches the screen — the same drift these models exist to end, just moved one layer down.
   Forbidding makes it a loud failure at construction, which a test catches before a release.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .status import MicroLiveStatus, ProposalStatus, SystemStatus


class PreflightCheck(BaseModel):
    """One preflight gate, as the risk screen lists it."""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    passed: bool
    detail: str


class MicroLiveAccount(BaseModel):
    """The real-money account, masked. Absent whenever the venue could not be read.

    Built by ``run_preflight`` inside a live broker session, so every field here is present or
    the whole object is ``None`` — there is no half-read account.
    """

    model_config = ConfigDict(extra="forbid")

    account_id_masked: str
    cash: float
    equity: float
    buying_power: float
    type: str


class MicroLive(BaseModel):
    """Real-money workflow status, the preflight gates, and the capital boundary.

    ``preflight_ready`` is the field the console reads before it will offer a real-money
    approval at all, and ``checks``/``blocking``/``permissions`` are rendered without a guard on
    the risk screen. They default to empty rather than being optional: a page that asks what is
    blocking must get a list it can iterate, and "nothing was read" is an empty list plus a
    ``connect_error``, never a missing key.
    """

    model_config = ConfigDict(extra="forbid")

    status: MicroLiveStatus
    enabled: bool
    connected: bool
    connect_error: str | None = None
    capital_boundary_usd: float
    account: MicroLiveAccount | None = None
    preflight_ready: bool
    checks: list[PreflightCheck] = Field(default_factory=list)
    blocking: list[str] = Field(default_factory=list)
    permissions: dict[str, bool] = Field(default_factory=dict)
    execution_mode: str


class PortfolioPosition(BaseModel):
    """One holding inside a bucket, priced as of the account's snapshot."""

    model_config = ConfigDict(extra="forbid")

    symbol: str
    quantity: float
    price: float
    market_value_usd: float
    cost_basis_usd: float
    unrealized_pct: float
    weight_pct_of_allocation: float
    classification: str
    exposure_group: str | None = None      # None whenever the holding sits outside the universe


class PortfolioBucket(BaseModel):
    """A mandate's own money: its allocation, what is deployed, and which account holds it."""

    model_config = ConfigDict(extra="forbid")

    account: str
    allocation_usd: float
    cash_usd: float
    positions_value_usd: float
    equity_usd: float
    position_count: int
    is_real_money: bool
    venue: str | None = None               # None for the conceptual book, which has no venue
    positions: list[PortfolioPosition] = Field(default_factory=list)
    sleeves_pct: dict[str, float] = Field(default_factory=dict)


class PortfolioAccount(BaseModel):
    """One book, reported on its own terms.

    The ladder fields are optional because a bucket can be configured before any drawdown state
    exists for it, and ``return_pct`` is None rather than zero when nothing has been paid in —
    a division that has no answer must not be reported as a flat return.
    """

    model_config = ConfigDict(extra="forbid")

    buckets: list[str]
    is_real_money: bool
    venue: str | None = None
    source: str
    as_of: str
    priced_for: str
    equity_usd: float
    cash_usd: float
    positions_value_usd: float
    net_deposits_usd: float
    opening_allocation_usd: float
    recorded_flows_usd: float
    net_deposits_basis: str
    investment_pnl_usd: float
    return_pct: float | None = None
    high_water_mark_usd: float | None = None
    drawdown_pct: float | None = None
    ladder_level: str | None = None


class PortfolioCombined(BaseModel):
    """The sum across accounts, carrying the warning that makes it safe to read.

    ``mixes_real_and_conceptual`` is true exactly when this total spans both books, which is
    precisely when it must not be compared against any single account's high-water mark.
    """

    model_config = ConfigDict(extra="forbid")

    equity_usd: float
    accounts: list[str]
    complete: bool
    mixes_real_and_conceptual: bool


class Portfolio(BaseModel):
    """Every bucket a screen can show, and the account each one belongs to.

    ``buckets`` and ``accounts`` are empty rather than absent when a book cannot be read: the
    account's reason lands in ``errors`` and the rest of the response still answers. Both screens
    that render this iterate ``buckets`` without a guard, so an empty map is the contract and a
    missing key would be a blank page. ``combined`` is always present — it is computed from
    whatever was readable, and ``complete`` says whether that was everything.
    """

    model_config = ConfigDict(extra="forbid")

    as_of: str
    buckets: dict[str, PortfolioBucket] = Field(default_factory=dict)
    accounts: dict[str, PortfolioAccount] = Field(default_factory=dict)
    errors: dict[str, str] = Field(default_factory=dict)
    combined: PortfolioCombined


class ProposalFreshness(BaseModel):
    """How current a proposal still is, and what the answer was measured against.

    The wire name of ``freshness_class`` is ``class``, which is not a Python identifier. FastAPI
    serialises by alias, so the console keeps reading the key it always read.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    state: str
    label: str
    detail: str
    freshness_class: str = Field(alias="class")
    ttl_minutes: int | None = None
    age_minutes: float
    max_price_drift_pct: float | None = None


class ProposalSummary(BaseModel):
    """One proposal as the approval queue lists it.

    ``proposal_id`` and ``version`` are an identity, not decoration: the console pins a detail
    view to the pair and refuses an approval whose version has moved underneath it, and the
    approve route takes the same two values back. Everything here comes from the stored
    Proposal, whose types are known, so almost nothing is optional.
    """

    model_config = ConfigDict(extra="forbid")

    proposal_id: str
    version: str
    sell_quantity: float | None = None
    order_status: str | None = None
    reconciliation_error: str | None = None
    status: ProposalStatus
    ticker: str
    bucket: str
    is_real_money: bool
    action: str
    strategy: str
    notional_usd: float
    target_weight_pct: float
    confidence: int | None = None
    requires_human_approval: bool
    created_at: str
    approved_at: str | None = None
    freshness: ProposalFreshness
    reference_price: float | None = None
    run_id: str | None = None


class ProposalWorld(BaseModel):
    """The world the proposal was born into, as recorded at the time."""

    model_config = ConfigDict(extra="forbid")

    ladder_level: str | None = None
    regime: str | None = None
    portfolio_equity_usd: float | None = None
    bucket_cash_usd: float | None = None
    data_quality: str | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)
    missing_information: list[str] = Field(default_factory=list)


class ProposalResearch(BaseModel):
    """The case the research made, projected out of the stored decision record.

    Every field is optional because this block is read from a run artefact on disk. The record
    was written by whatever version of the pipeline produced it, the retention job removes old
    runs, and a proposal that outlives its record must still be readable and refusable — so an
    absent field is normal here in a way it is not anywhere else in this module.
    """

    model_config = ConfigDict(extra="forbid")

    portfolio_manager_rating: str | None = None
    policy_action: str | None = None
    policy_reasons: list[str] | None = None
    confidence: int | None = None
    data_quality: str | None = None
    market_risk_level: str | None = None
    investment_thesis: str | None = None
    bull_case: str | None = None
    bear_case: str | None = None
    primary_risks: list[str] | None = None
    what_could_make_this_wrong: str | None = None
    why_doing_nothing_may_be_better: str | None = None
    missing_information: list[str] | None = None
    entry_logic: str | None = None
    exit_logic: str | None = None
    expected_holding_period: str | None = None


class HardRiskRule(BaseModel):
    """One deterministic rule and how it answered. Optional throughout, for the reason above."""

    model_config = ConfigDict(extra="forbid")

    id: str | None = None
    name: str | None = None
    passed: bool | None = None
    detail: str | None = None


class HardRiskView(BaseModel):
    """The verdict the deterministic engine reached, and every rule behind it.

    ``requires_human_approval`` defaults to True rather than False: a record that does not say
    must not be read as one that waived the human.

    ``ladder`` and ``regime`` stay open maps on purpose. They are the risk engine's own
    serialisation, passed through whole, and naming their fields here would be a second copy of
    a shape this layer does not own.
    """

    model_config = ConfigDict(extra="forbid")

    verdict: str | None = None
    final_action: str | None = None
    notional_usd: float | None = None
    post_trade_weight_pct: float | None = None
    requires_human_approval: bool = True
    rules: list[HardRiskRule] = Field(default_factory=list)
    failed_rule_ids: list[str] = Field(default_factory=list)
    advisories: list[str] = Field(default_factory=list)
    ladder: dict[str, Any] | None = None
    regime: dict[str, Any] | None = None


class ProposalDetail(ProposalSummary):
    """Everything the queue shows, plus the reasoning and the rules behind it.

    ``research`` and ``hard_risk`` are absent together: both are read from the same decision
    record, and a proposal whose run has been pruned keeps its identity, its freshness and its
    Hard Risk status — which is what the approval screen refuses or allows against.
    """

    model_config = ConfigDict(extra="forbid")

    history: list[dict[str, Any]] = Field(default_factory=list)
    world_at_proposal: ProposalWorld
    research: ProposalResearch | None = None
    hard_risk: HardRiskView | None = None


class Fill(BaseModel):
    """One recorded fill, as the ledger exported it.

    This is the one shape in this module that allows extra fields, and the exception is the
    point rather than an oversight. A fill entry is a fixed set of keys plus whatever the
    ledger event carried, and the reader falls back to parsing the raw JSONL log line by line
    when the database has not been migrated — so it can hand back rows written by an older
    version of this code. Forbidding extras there would reject history; requiring any field
    would make one unparseable old line take down the whole journal. Everything is therefore
    optional, and anything unrecognised is passed through to the screen untouched.
    """

    model_config = ConfigDict(extra="allow")

    fill_id: str | None = None
    at: str | None = None
    proposal_id: str | None = None
    run_id: str | None = None
    bucket: str | None = None
    strategy: str | None = None
    symbol: str | None = None
    side: str | None = None
    order_id: str | None = None
    client_order_id: str | None = None
    status: str | None = None
    quantity: float | None = None
    price: float | None = None
    notional: float | None = None
    trade_date: str | None = None


class OpenOrder(BaseModel):
    """An order the venue still considers live, read from the venue itself."""

    model_config = ConfigDict(extra="forbid")

    order_id: str
    symbol: str
    side: str
    status: str
    quantity: float | None = None
    filled_quantity: float = 0.0
    filled_avg_price: float | None = None
    is_open: bool


class TrackedOrder(BaseModel):
    """An order this system submitted, and what it knows about where it ended up.

    ``status`` is the product state; ``venue_status`` is the venue's own word for it. They are
    separate fields because they disagree in exactly the case that matters: an order this side
    cannot account for is UNKNOWN here however confident the venue sounds.
    """

    model_config = ConfigDict(extra="forbid")

    proposal_id: str
    order_id: str | None = None
    client_order_id: str
    symbol: str
    status: ProposalStatus
    venue_status: str | None = None
    bucket: str
    side: str
    filled_quantity: float = 0.0
    filled_avg_price: float | None = None
    reconciled_at: str | None = None
    reconciliation_error: str | None = None


class Orders(BaseModel):
    """Fills recorded locally, plus live order state from the venue when it can be reached.

    ``error`` carries a venue that would not answer. It does not empty the rest: the locally
    recorded fills and the orders this system is tracking are what the screen falls back to,
    and an unreachable venue must read as an incomplete picture rather than as no orders.
    """

    model_config = ConfigDict(extra="forbid")

    fills: list[Fill] = Field(default_factory=list)
    open_orders: list[OpenOrder] = Field(default_factory=list)
    tracked_orders: list[TrackedOrder] = Field(default_factory=list)
    error: str | None = None


class MarketStatus(BaseModel):
    """The exchange session, read from the calendar.

    A calendar that cannot be read reports the session closed and says why. That asymmetry is
    deliberate — an unknown session must never be the one that lets an order through — and it
    is why ``next_open``/``next_close`` and ``error`` are alternatives rather than companions.
    """

    model_config = ConfigDict(extra="forbid")

    is_open: bool
    calendar: str | None = None
    next_open: str | None = None
    next_close: str | None = None
    error: str | None = None


class ClaudeStatus(BaseModel):
    """Subscription runtime state, counted from the call audit. Never carries a credential."""

    model_config = ConfigDict(extra="forbid")

    state: str
    detail: str
    billing: str
    calls_today: int = 0
    last_call_at: str | None = None


class Health(BaseModel):
    """One call a dashboard can poll: can the system act, and what is blocking it.

    Everything below ``checked_at`` is optional, and that is the whole point of this shape. A
    configuration that will not load answers with the status, the reason and the time and
    nothing else — there is no stage, no market session and no subscription state to report
    when the file describing them is the thing that broke. Requiring any of them would make
    /health fail exactly when it is the endpoint being asked what went wrong.

    The console reads ``micro_live_enabled is true`` before it will offer a real-money
    approval, so absent reads as not-enabled and the gate stays shut.
    """

    model_config = ConfigDict(extra="forbid")

    status: SystemStatus
    detail: str
    warnings: list[str] = Field(default_factory=list)
    checked_at: str
    paused: bool | None = None
    stage: int | None = None
    market: MarketStatus | None = None
    claude: ClaudeStatus | None = None
    micro_live_enabled: bool | None = None


class AdvisorPosition(BaseModel):
    """An observed holding. A figure the venue would not give is None, never zero.

    The last four fields are written by the pricing pass that runs after the positions are
    read, not by the construction that creates them — which is how `quote_at` and `weight_pct`
    came to be on every stored position while no fixture in the suite had them. Everything
    inside the snapshot allows extras for the reason the snapshot itself does: see
    [AdvisorSnapshot].
    """

    model_config = ConfigDict(extra="allow")

    symbol: str
    quantity: float
    average_cost: float | None = None
    price: float | None = None
    market_value_usd: float | None = None
    unrealized_pct: float | None = None
    quote_at: str | None = None
    weight_pct: float | None = None


class AdvisorAccount(BaseModel):
    """One brokerage account as the assistant observed it, masked.

    ``complete`` is the account's own verdict on whether its totals could be read. Equity and
    cash stay None when they could not — the module that builds this says so in its docstring,
    and a zero there would be a claim about money rather than an admission of not knowing.
    """

    model_config = ConfigDict(extra="allow")

    id: str
    label: str
    agentic: bool = False
    positions: list[AdvisorPosition] = Field(default_factory=list)
    complete: bool = False
    equity_usd: float | None = None
    cash_usd: float | None = None


class AdvisorCandidate(BaseModel):
    """A symbol the assistant may consider, and where it came from."""

    model_config = ConfigDict(extra="allow")

    symbol: str
    sources: list[str] = Field(default_factory=list)


class AdvisorSnapshot(BaseModel):
    """The saved observation of the venue, read back from the state database.

    Extras are allowed, for the same two reasons as ``Fill``: this is a stored document that an
    older version of the code may have written, and the task runner splices keys into it in
    flight — a broker that will not refresh adds ``stale`` and an explanation to ``errors``
    rather than discarding what was last read. Every field is optional because the document is
    simply absent until the first refresh has run.

    The rule covers everything inside it, not only this top level: an account, a holding and a
    candidate are all parts of the same stored document, and a field one of them grew is no
    more this layer's to reject than a field the document grew.

    Note that ``stale`` here is the snapshot's own account of itself. The one on ``AdvisorState``
    is computed from ``fetched_at`` on every read, and is the one a screen should trust.
    """

    model_config = ConfigDict(extra="allow")

    fetched_at: str | None = None
    source: str | None = None
    accounts: list[AdvisorAccount] = Field(default_factory=list)
    candidates: list[AdvisorCandidate] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    coverage: str | None = None
    coverage_note: str | None = None
    stale: bool | None = None


class PersonalNotes(BaseModel):
    """What the owner told the assistant about their own situation, and its revision.

    ``revision`` is a concurrency token, not decoration: a save carrying a stale one is refused
    so that two devices cannot silently overwrite each other's notes.
    """

    model_config = ConfigDict(extra="forbid")

    text: str = ""
    revision: int = 0
    updated_at: str | None = None


class AdvisorJob(BaseModel):
    """An assistant task and where it got to. Every field is projected with .get()."""

    model_config = ConfigDict(extra="forbid")

    run_key: str | None = None
    ticker: str | None = None
    state: str | None = None
    error: str | None = None
    created_at: str | None = None
    task: dict[str, Any] | None = None


class AdvisorDecision(BaseModel):
    """A decision record as the assistant lists it.

    ``policy`` is the stored policy document passed through whole. It belongs to the research
    pipeline, and naming its fields here would be a second copy of a shape this layer does not
    own — the same call made for ``HardRiskView.ladder``.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str | None = None
    ticker: str | None = None
    bucket: str | None = None
    account_id: str | None = None
    created_at: str | None = None
    trade_date: str | None = None
    action: str | None = None
    policy: dict[str, Any] | None = None
    advisory: bool = False
    proposal_id: str | None = None


class AdvisorState(BaseModel):
    """Everything the assistant screen renders from one poll."""

    model_config = ConfigDict(extra="forbid")

    snapshot: AdvisorSnapshot = Field(default_factory=AdvisorSnapshot)
    stale: bool
    notes: PersonalNotes = Field(default_factory=PersonalNotes)
    jobs: list[AdvisorJob] = Field(default_factory=list)
    conversations: list[str] = Field(default_factory=list)
    decisions: list[AdvisorDecision] = Field(default_factory=list)
    coverage: str


class AdvisorMessage(BaseModel):
    """One turn. ``research_candidates`` is present on an answer and absent on a question."""

    model_config = ConfigDict(extra="forbid")

    role: str
    content: str
    run_key: str
    state: str
    research_candidates: list[dict[str, Any]] | None = None


class AdvisorConversation(BaseModel):
    """One conversation, most recent turns last."""

    model_config = ConfigDict(extra="forbid")

    conversation_id: str
    messages: list[AdvisorMessage] = Field(default_factory=list)
