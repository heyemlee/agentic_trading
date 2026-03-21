"""The three modes, and for each one the reasons it is or is not open.

A mode is a rhythm: how often a decision is made, what makes it, and how long a position is
meant to be held. They open in a fixed order -- steady, then swing, then intraday -- and a later
one does not open because an earlier one did. "Not open" is never a bare label: every mode
carries the specific conditions it is still missing, read from the risk profile and from the
record of what has actually been simulated, so a screen can say why and a person can see what
would have to change.

Nothing here opens a mode. This module reads; opening one is a person's confirmation
(``plan``) on top of a record that only time can produce.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

STEADY, SWING, INTRADAY = "steady", "swing", "intraday"
MODES = (STEADY, SWING, INTRADAY)

#: Sessions of live simulation a mode needs before it may be offered for real money, and before
#: a strategy may replace the one in force (docs/todo.md, P3.2 and P3.4). Counted from the
#: record, never asserted: see ``auto.paper``.
SIMULATION_DAYS_TO_OPEN = 30
SIMULATION_DAYS_TO_REPLACE = 180


@dataclass(frozen=True)
class Mode:
    key: str
    order: int
    label: tuple[str, str]              # zh, en
    #: How often a decision is made.
    cadence: str                        # weekly | daily | intraday
    #: What produces the signal: the multi-agent research run, or rules over prices alone.
    signal: str                         # research | rules
    holding: tuple[str, str]            # zh, en
    #: The strategy tag its proposals carry. The risk profile's strategy policy and the
    #: freshness class both key on it, so a mode cannot choose a looser treatment by renaming.
    strategy_tag: str
    #: Bounds on how long one position may be held, in calendar days.
    holding_days: tuple[int, int]
    #: Bounds on the trend average a strategy of this mode may use, in sessions; (0, 0) where
    #: the mode has no trend rule.
    trend_window: tuple[int, int] = (0, 0)
    #: What running it consists of, said once, so that a mode is its behaviour and not its name.
    runs_as: tuple[str, str] = ("", "")

    def as_dict(self) -> dict[str, Any]:
        return {"key": self.key, "order": self.order,
                "label": {"zh": self.label[0], "en": self.label[1]},
                "cadence": self.cadence, "signal": self.signal,
                "holding": {"zh": self.holding[0], "en": self.holding[1]},
                "strategy_tag": self.strategy_tag,
                "holding_days": list(self.holding_days),
                "trend_window": list(self.trend_window),
                "runs_as": {"zh": self.runs_as[0], "en": self.runs_as[1]}}


#: What decides a trade in each mode (``Mode.signal``):
#:
#: ``research``              a research run concludes, and nothing else decides.
#: ``research+trend``        research still has to say buy; a rule over the fund's own daily
#:                           closes then times the purchase and can end the position. The rule
#:                           is never a reason to buy on its own.
#: ``premarket_plan+rules``  a model sets the day's plan once, before the open; local rules act
#:                           on it through the session at the speed of the data. No model is
#:                           asked anything per trade, which is the only way that could work.
RESEARCH, RESEARCH_AND_TREND, PLAN_AND_RULES = "research", "research+trend", "premarket_plan+rules"

def _both(zh: str, en: str) -> tuple[str, str]:
    return zh, en


TABLE: dict[str, Mode] = {
    STEADY: Mode(STEADY, 1, ("稳健", "Steady"), "weekly", RESEARCH,
                 ("以周到月持有", "held for weeks to months"), "long_only_diversified", (7, 180),
                 runs_as=_both(
                     "每周研究一次；研究说买且把握度够才买；跌到止损线或持有到期卖出。",
                     "Researched weekly; bought when research says so with enough confidence; "
                     "sold at its stop or at the end of its holding period.")),
    SWING: Mode(SWING, 2, ("波段", "Swing"), "daily", RESEARCH_AND_TREND,
                ("以几天到几周持有", "held for days to weeks"), "long_only_diversified", (2, 30),
                trend_window=(10, 60),
                runs_as=_both(
                    "每个交易日研究一次；研究说买、并且收盘价在自己的均线上方才买；"
                    "跌到止损线、跌破均线或持有到期卖出。持有期更短，止损更紧。",
                    "Researched every session; bought when research says so and the close is "
                    "above its own average; sold at its stop, on a close below that average, "
                    "or at the end of a shorter holding period.")),
    INTRADAY: Mode(INTRADAY, 3, ("短线", "Intraday"), "intraday", PLAN_AND_RULES,
                   ("盘中按分钟级规则进出", "in and out within the session"), "momentum", (0, 1),
                   runs_as=_both(
                       "开盘前由模型定一次当天的计划；盘中由本地规则按分钟行情执行，"
                       "不为每一笔交易再问模型；收盘前全部平仓。尚未实现。",
                       "A model sets the day's plan once before the open; local rules carry "
                       "it out on minute data without asking a model per trade; flat by the "
                       "close. Not built.")),
}


@dataclass(frozen=True)
class Blocker:
    """One condition a mode is still missing."""

    code: str
    zh: str
    en: str
    #: Whose it is to clear: time (a record that accumulates), the owner (a decision), or work
    #: that has not been built.
    waits_on: str                       # record | owner | build | data

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "waits_on": self.waits_on, "text": {"zh": self.zh, "en": self.en}}


#: The modes code may open positions under on its own where the risk profile does not say.
#: Steady alone: it is the first to open, and the only one whose every part -- research says
#: buy, a stop, a term -- was something the account already allowed before modes existed.
DEFAULT_OPEN_MODES = (STEADY,)
#: Where the risk profile says which modes those are. A list, like the classes beside it.
OPEN_MODES_PATH = "buckets.{account}.autonomy.open.modes"


def allowed_modes(profiles: Any, account: str = "micro_live") -> tuple[str, ...]:
    """The modes code may open positions under on its own in this account.

    Read from ``buckets.<account>.autonomy.open.modes``; ``DEFAULT_OPEN_MODES`` where the
    profile has never declared it. This is the owner's answer to a question code may not answer
    for them -- above all whether a swing strategy, which times purchases with a trend rule,
    falls under their rule that every momentum order needs a person -- and it is read where an
    order is approved (``Service.autonomous_reductions``), not only where a plan is started.
    """
    opening = (((profiles.risk.get("buckets") or {}).get(account) or {}).get("autonomy") or {}).get("open") or {}
    declared = opening.get("modes")
    if not isinstance(declared, (list, tuple)):
        return DEFAULT_OPEN_MODES
    return tuple(str(item) for item in declared if str(item) in MODES)


def blockers(profiles: Any, mode: str, *, simulated_days: int = 0, exits_verified: bool = False,
             account: str = "micro_live") -> list[Blocker]:
    """Why this mode may not yet trade real money, each reason on its own.

    ``simulated_days`` and ``exits_verified`` are facts about the simulated account's record
    and are passed in rather than read here, so this stays arithmetic over what it is given.
    """
    spec = TABLE[mode]
    out: list[Blocker] = []
    risk = profiles.risk

    if simulated_days < SIMULATION_DAYS_TO_OPEN:
        out.append(Blocker(
            "simulation_record_short",
            f"实时模拟只有 {simulated_days} 个有效交易日，需要至少 {SIMULATION_DAYS_TO_OPEN} 个（等待样本）。",
            f"Only {simulated_days} session(s) of live simulation are on record; "
            f"{SIMULATION_DAYS_TO_OPEN} are needed.", "record"))
    if not exits_verified:
        out.append(Blocker(
            "exit_not_exercised",
            "模拟账户里还没有完成过一次“买入→卖出”的完整流程。",
            "The simulated account has not yet carried one position from purchase to sale.",
            "record"))

    # What the risk profile itself says about this kind of strategy in this account. These are
    # the owner's boundaries; a mode waits behind them and never around them.
    policy = (risk.get("strategy_policy") or {}).get("sandbox") or {}
    gated = {str(tag).lower() for tag in policy.get("strategies_requiring_human_approval_always") or []}
    if spec.strategy_tag in gated:
        out.append(Blocker(
            "strategy_always_needs_a_person",
            "风控配置规定动量类策略每一笔都必须人工确认，不能自动执行。",
            "The risk profile requires a person to confirm every momentum order; it may not be "
            "automatic.", "owner"))
    if mode == SWING and SWING not in allowed_modes(profiles, account):
        # Swing buys on research and uses a trend rule only to time the purchase and to leave.
        # Whether that is "momentum" in the sense the risk profile gates is not something code
        # gets to settle in its own favour: it stays shut for real money until the owner says,
        # and the way they say it is a setting of its own (``allowed_modes``). The list of
        # strategies that always need a person is not touched: the real-money preflight holds
        # momentum to it whatever is decided here.
        out.append(Blocker(
            "swing_policy_undecided",
            "风控配置规定动量类策略每一笔都必须人工确认。波段模式按“研究结论买入，趋势只用于择时和退出”实现；"
            "它是否属于这条规定的范围需要你来定，定下来之前不对实盘开放。你的决定是一项单独的设置"
            "（允许代码自行开仓的模式里加入“波段”），列在波段方案里，确认方案时用控制台密码一并确认。",
            "The risk profile requires a person to confirm every momentum order. Swing buys on "
            "research and uses a trend rule only for timing and exit; whether that falls under "
            "that rule is yours to decide, and it is not opened for real money until you have. "
            "Your decision is a setting of its own -- adding swing to the modes code may open "
            "under -- listed in a swing plan and confirmed with it, on the console password.",
            "owner"))

    limits = ((risk.get("buckets") or {}).get(account) or {}).get("activity_limits") or {}
    per_week = profiles.effective(limits.get("max_orders_per_week"), "activity.max_orders_per_week")
    per_day = profiles.effective(limits.get("max_new_positions_per_day"), "activity.per_day")
    if mode == SWING:
        schedule = ((risk.get("research_schedule") or {}).get("accounts") or {}).get(account) or {}
        study = schedule.get("candidates") or {}
        off = not study or study.get("enabled") is False
        if off or str(study.get("period", "weekly")) != "daily":
            now_zh, now_en = ("未开启", "switched off") if off else ("每周一次", "weekly")
            cap = int(study.get("max_tickers_per_run", 3) or 3)
            out.append(Blocker(
                "daily_research_not_scheduled",
                f"方案里的基金目前的研究安排是：{now_zh}。波段模式按每个交易日研究设计；改成每日后，"
                f"每个交易日最多多用 {cap} 次研究额度（每周约 {cap * 5} 次，每周一次时最多 {cap} 次）。",
                f"The plan's funds are not studied every session (the study is {now_en}). Swing is "
                f"designed on daily research; switched to daily it costs up to {cap} analyses a "
                f"session, about {cap * 5} a week against {cap} when weekly.", "owner"))
    if mode == INTRADAY:
        out.append(Blocker(
            "intraday_not_built",
            "分钟级行情接入、行情时效检查和盘中规则执行尚未实现。",
            "Minute data, its freshness checks and the in-session rule runner are not built.",
            "build"))
        out.append(Blocker(
            "broker_intraday_unverified",
            "券商对日内交易的限制（现金账户的结算与日内交易次数规则）尚未核实。",
            "The venue's rules for same-day trading in a cash account (settlement, day-trade "
            "counts) have not been verified.", "data"))
        if per_week is not None:
            out.append(Blocker(
                "activity_limits_too_tight",
                f"当前每周最多下 {int(per_week)} 笔单、每天最多新开 {int(per_day or 1)} 个仓位，"
                "日内模式在这个限额内无法运作。",
                f"At most {int(per_week)} orders a week and {int(per_day or 1)} new position(s) "
                "a day are allowed, which an intraday mode cannot work within.", "owner"))
    if mode == SWING and per_week is not None and int(per_week) < 5:
        out.append(Blocker(
            "activity_limits_tight_for_daily",
            f"当前每周最多下 {int(per_week)} 笔单，每日节奏的进出会经常被这条限额挡住。",
            f"At most {int(per_week)} orders a week are allowed, which a daily rhythm will "
            "often run into.", "owner"))
    return out


def earlier_modes(mode: str) -> list[str]:
    """The modes that must have opened before this one may."""
    return [key for key in MODES if TABLE[key].order < TABLE[mode].order]


def stock_extension(profiles: Any, account: str = "micro_live") -> dict[str, Any]:
    """Where trading single stocks automatically stands: not open, and what it would take.

    Every mode here trades funds. That a mode has been opened says a strategy over *funds* ran
    as built and was put in force; it says nothing about a company's shares, which fail in ways
    a fund does not -- an earnings report overnight, a thin order book, one name being most of
    an account. So stocks are not a setting on a mode that has opened. They are their own
    extension with their own conditions, none of which a fund's record meets, and this is the
    list. It is read by the same screen that shows why a mode is closed.
    """
    classes = [str(c) for c in (((profiles.risk.get("buckets") or {}).get(account) or {})
                                .get("autonomy") or {}).get("open", {}).get("classes") or []]
    stock_classes = [c for c in classes if "stock" in c or "equity" in c]
    blockers = [
        Blocker("stocks_not_in_any_plan",
                "方案的可买范围只由配置里的 ETF 敞口分组生成，个股不在其中；任何模式的方案都不会买入个股。",
                "A plan's universe is built from the configured ETF exposure groups alone. No "
                "mode's plan can buy a single stock.", "build"),
        Blocker("company_events_unvalidated",
                "公司事件（财报、分红除权、停牌、并购）对自动买卖的影响没有单独验证过：现有的财报静默期规则是为人工确认的流程写的。",
                "What company events do to automatic orders -- earnings, ex-dates, halts, "
                "takeovers -- has not been validated on its own. The earnings blackout that "
                "exists was written for orders a person confirms.", "build"),
        Blocker("liquidity_unvalidated",
                "个股的流动性没有为自动交易单独验证过：模拟成交里个股的价差和滑点是没有用真实成交校准过的假设值，"
                "订单大小也没有按该股的成交量来限制。",
                "Liquidity has not been validated for automatic orders in stocks: the simulated "
                "venue's spread and slippage for a stock are assumptions never calibrated "
                "against real fills, and an order is not sized against the stock's own volume.",
                "build"),
        Blocker("concentration_unvalidated",
                "单一公司集中度没有单独验证过：一个仓位占账户三分之一，对一只分散的基金和对一家公司不是同一种风险。",
                "Concentration in one company has not been validated: a third of an account in "
                "a diversified fund and in one company are not the same risk.", "build"),
        Blocker("stocks_need_their_own_record",
                "ETF 模式的模拟记录和实盘开放不能算作个股的记录。个股需要自己的模拟交易日、自己的退出验证和你的单独确认。",
                "A fund mode's simulated record, and its being open, do not count for stocks. "
                "Stocks need simulated sessions of their own, an exit exercised of their own, "
                "and your separate confirmation.", "record"),
    ]
    if stock_classes:
        blockers.insert(0, Blocker(
            "stock_classes_configured",
            f"配置里允许代码自行开仓的品种包含个股类别（{'、'.join(stock_classes)}）。自动交易的方案仍然不会使用它们。",
            "The configuration lets code open stock classes on its own "
            f"({', '.join(stock_classes)}). An automatic-trading plan still does not use them.",
            "owner"))
    return {"key": "stocks", "label": {"zh": "美股个股", "en": "Single stocks"},
            "open_for_real_money": False, "can_simulate": False,
            "blockers": [item.as_dict() for item in blockers]}


def status(profiles: Any, *, simulated_days: dict[str, int] | None = None,
           exits_verified: dict[str, bool] | None = None, opened: tuple[str, ...] = (),
           account: str = "micro_live") -> list[dict[str, Any]]:
    """Every mode, with whether it is open for real money and what it still lacks if not."""
    days = simulated_days or {}
    verified = exits_verified or {}
    rows = []
    for key in MODES:
        missing = blockers(profiles, key, simulated_days=int(days.get(key, 0)),
                           exits_verified=bool(verified.get(key, False)), account=account)
        waiting_on = [earlier for earlier in earlier_modes(key) if earlier not in opened]
        if waiting_on:
            names_zh = "、".join(TABLE[m].label[0] for m in waiting_on)
            names_en = ", ".join(TABLE[m].label[1] for m in waiting_on)
            missing.insert(0, Blocker(
                "earlier_mode_not_open", f"按顺序开放：需要先开放{names_zh}模式。",
                f"Modes open in order: {names_en} must open first.", "record"))
        rows.append({**TABLE[key].as_dict(),
                     "simulated_days": int(days.get(key, 0)),
                     "open_for_real_money": key in opened and not missing,
                     "can_simulate": key != INTRADAY,
                     "blockers": [item.as_dict() for item in missing]})
    return rows
