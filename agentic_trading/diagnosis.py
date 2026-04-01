"""Why a research run did not end in an order, as a small closed vocabulary.

Every run that bought nothing used to read the same on every screen: "no trade". Five quite
different things were hiding behind those two words, and they call for five different responses:

* the strategy looked and chose not to act, which is the system working;
* the account did not have the cash;
* the order was sized into a corner by limits that contradict one another at this account size;
* the evidence a position may be opened on was missing or stale;
* the research never finished at all.

A person cannot tell which from "no trade", and neither can anything that is supposed to act on
it. So this module reads what a run already recorded -- the job row, the decision record, the
Hard Risk rules and the facts they were decided on -- and names the reason: a category from a
closed set, a code inside it, and one short sentence in each language the console speaks.

Nothing here decides anything. It reads a decision that was already made and says why; it never
changes a verdict, never reads the venue and never calls a model. A record written before a field
existed is described from what it does carry, and where that is not enough the answer is the
generic sentence for its category rather than a guess.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------- how a worker's pipeline ended
#
# The process exit code is the only thing a research pipeline hands back to the worker that
# started it, so each way of ending that a person would respond to differently has its own.
EXIT_OK = 0
EXIT_PIPELINE = 1          # the pipeline failed: a defect or an unclassified model error
EXIT_FAIL_CLOSED = 2       # stopped before it began: the book or its prices could not be read
EXIT_LIMIT = 3             # the subscription limit; recoverable, and requeued
EXIT_LOGIN = 4             # this Mac's Claude login lapsed; recoverable once a person signs in
EXIT_SUBJECT = 5           # what was to be studied is no longer there to study
EXIT_TIMEOUT = 124

# ---------------------------------------------------------------- categories
PROPOSAL = "proposal_created"
NOTHING_TO_DO = "no_action_needed"
STRATEGY = "strategy_no_trade"
FUNDS = "insufficient_funds"
ORDER_LIMIT = "order_limit_conflict"
DATA = "data_missing"
RESEARCH = "research_failed"
RISK_PAUSE = "risk_pause"
NOT_PERMITTED = "not_permitted"
SYSTEM = "system_error"
ADVICE = "advice_only"

#: The five the plan names, in its order, then the ones the record needed as well: a pause the
#: risk controls imposed and an instrument the account may not hold are neither "the strategy
#: declined" nor "a limit conflict", and filing them under either would repeat the old mistake.
CATEGORIES = (STRATEGY, FUNDS, ORDER_LIMIT, DATA, RESEARCH, RISK_PAUSE, NOT_PERMITTED, SYSTEM,
              PROPOSAL, NOTHING_TO_DO, ADVICE)

LABELS: dict[str, tuple[str, str]] = {
    STRATEGY: ("策略选择不交易", "Strategy chose not to trade"),
    FUNDS: ("资金不足", "Not enough cash"),
    ORDER_LIMIT: ("订单限制冲突", "Order limits conflict"),
    DATA: ("数据缺失", "Data missing"),
    RESEARCH: ("研究失败", "Research failed"),
    RISK_PAUSE: ("风控暂停", "Paused by risk controls"),
    NOT_PERMITTED: ("不在允许范围", "Not permitted"),
    SYSTEM: ("系统异常", "System fault"),
    PROPOSAL: ("已生成提案", "Proposal created"),
    NOTHING_TO_DO: ("无需操作", "Nothing to do"),
    ADVICE: ("只读建议", "Advice only"),
}

#: Which reason leads when a run was stopped by several. The one a person can do least about
#: comes first: a paused account or a forbidden instrument makes every sizing question moot, and
#: missing evidence outranks a limit, because a limit only matters once the idea itself stands.
_PRIORITY = (SYSTEM, RESEARCH, RISK_PAUSE, NOT_PERMITTED, DATA, STRATEGY, FUNDS, ORDER_LIMIT,
             NOTHING_TO_DO, PROPOSAL, ADVICE)

# ---------------------------------------------------------------- the sentences
#
# One entry per code: (category, Chinese, English). A sentence may name facts in braces; when a
# record does not carry one of them the plain form after the bar is used instead, so an older
# record reads as a shorter sentence and never as a sentence with a hole in it.
def _say(category: str, zh: str, en: str) -> tuple[str, str, str]:
    return category, zh, en


_TEXT: dict[str, tuple[str, str, str]] = {
    # -- the research did not finish
    "llm_login_expired": _say(RESEARCH,
                          "这台 Mac 上的 Claude 登录已过期，研究无法运行。重新登录后会自动重试。",
                          "The Claude login on this Mac has expired, so research cannot run. "
                          "It is retried once someone signs in again."),
    "llm_limit": _say(RESEARCH, "Claude 订阅额度已用尽，额度重置后自动重试。",
                  "The Claude subscription limit was reached; the run is retried after the reset."),
    "timeout": _say(RESEARCH, "研究超过 {minutes} 分钟仍未完成，已停止。|研究超时，已停止。",
                "Research ran past its {minutes}-minute limit and was stopped.|"
                "Research ran past its time limit and was stopped."),
    "cancelled": _say(RESEARCH, "研究已被取消。", "Research was cancelled."),
    "portfolio_unavailable": _say(RESEARCH, "账本或行情读不出来，研究在开始前就停止了。",
                              "The ledger or its prices could not be read, so research stopped "
                              "before it began."),
    "subject_unavailable": _say(RESEARCH, "要研究的持仓已不在账户里（或账户没有读全），本次跳过。",
                            "The holding is no longer in the account, or the account could not "
                            "be read in full; this run was skipped."),
    "worker_crashed": _say(RESEARCH, "研究进程异常退出，没有产生结论。",
                       "The research process exited unexpectedly without a conclusion."),
    "pipeline_error": _say(RESEARCH, "研究流程出错，没有产生结论。",
                       "The research pipeline failed before reaching a conclusion."),
    "no_record": _say(RESEARCH, "研究结束了，但没有留下决策记录。",
                  "Research ended without leaving a decision record."),
    "policy_review_failed": _say(RESEARCH, "最后一步审核调用失败，按不交易处理。",
                             "The final review step failed, so the run fell back to no trade."),
    # -- the strategy chose not to act
    "rating_hold_no_position": _say(STRATEGY, "研究评级为“持有”，目前没有持仓，所以不买入。",
                                "Research rated it Hold and nothing is held, so there is "
                                "nothing to buy."),
    "rating_bearish_no_position": _say(STRATEGY, "研究评级偏空（{rating}），目前没有持仓，所以不操作。|"
                                             "研究评级偏空，目前没有持仓，所以不操作。",
                                   "Research rated it {rating} and nothing is held, so there is "
                                   "nothing to do.|Research was bearish and nothing is held, so "
                                   "there is nothing to do."),
    "rating_not_tradeable": _say(STRATEGY, "研究没有给出可以执行的评级。",
                             "Research returned no rating that can be acted on."),
    "confidence_below_no_trade": _say(STRATEGY, "把握度 {confidence} 低于不交易线 {threshold}。|"
                                            "把握度低于不交易线。",
                                  "Confidence {confidence} is below the no-trade line of "
                                  "{threshold}.|Confidence is below the no-trade line."),
    "confidence_below_floor": _say(STRATEGY, "把握度 {confidence} 低于开仓门槛 {floor}。|把握度低于开仓门槛。",
                               "Confidence {confidence} is below the {floor} needed to open a "
                               "position.|Confidence is below what opening a position needs."),
    "reviewer_cautious": _say(STRATEGY, "最终审核建议{reviewer_zh}，取更谨慎的一方，不买入。|"
                                    "最终审核更谨慎，不买入。",
                          "The final reviewer recommended {reviewer}; the more cautious view "
                          "wins.|The final reviewer was more cautious, so nothing is bought."),
    "no_trade": _say(STRATEGY, "策略判断这次不交易。", "The strategy chose not to trade this time."),
    # -- nothing needed doing
    "hold": _say(NOTHING_TO_DO, "已持有，研究结论是继续持有。",
             "Already held; research says to keep holding."),
    "at_target": _say(NOTHING_TO_DO, "持仓已达到目标仓位，不需要再买。",
                  "The position is already at its target size."),
    # -- the account did not have the money
    "cash_short": _say(FUNDS, "可用现金 ${cash} 不够这笔 ${order} 的买入。|可用现金不够这笔买入。",
                   "Cash of ${cash} does not cover a ${order} buy.|"
                   "There is not enough cash for this buy."),
    "cash_floor": _say(FUNDS, "买入后现金会低于要求保留的 {min_cash_pct}%。|买入后现金会低于要求保留的比例。",
                   "The buy would leave less than the {min_cash_pct}% that must stay in cash.|"
                   "The buy would leave less cash than must be kept."),
    "account_fully_deployed": _say(FUNDS, "账户已投入 ${deployed}，再买 ${order} 会超过可投入上限 ${cap}。|"
                                      "账户可投入的资金已经用满。",
                               "${deployed} is already deployed; ${order} more would pass the "
                               "${cap} the account may deploy.|The account's deployable capital "
                               "is used up."),
    # -- the order could not be shaped to fit every limit at once
    "target_below_minimum": _say(ORDER_LIMIT,
                             "按当前仓位比例这笔只有 ${requested}，低于最低下单金额 ${minimum}。|"
                             "按当前仓位比例，这笔低于最低下单金额。",
                             "At the configured size this order is only ${requested}, below the "
                             "${minimum} minimum order.|At the configured size this order is "
                             "below the minimum order."),
    "limit_below_minimum": _say(ORDER_LIMIT,
                            "{limit_zh}只允许再买 ${legal_max}，低于最低下单金额 ${minimum}。|"
                            "各项上限留下的空间低于最低下单金额。",
                            "{limit} leaves room for only ${legal_max}, below the ${minimum} "
                            "minimum order.|The limits leave less room than the minimum order."),
    "limit_exhausted": _say(ORDER_LIMIT, "{limit_zh}已经用满，不能再买。|上限已经用满，不能再买。",
                        "{limit} is used up, so nothing more can be bought.|"
                        "A limit is used up, so nothing more can be bought."),
    "position_cap": _say(ORDER_LIMIT, "买入后该标的占 {post_pct}%，超过单一标的上限 {cap_pct}%。|"
                                  "买入后会超过单一标的上限。",
                     "The buy would make this holding {post_pct}% against a {cap_pct}% cap.|"
                     "The buy would pass the single-holding cap."),
    "single_order_cap": _say(ORDER_LIMIT, "这笔 ${order} 超过单笔上限 ${cap}。|这笔超过单笔上限。",
                         "This ${order} order is above the ${cap} single-order cap.|"
                         "This order is above the single-order cap."),
    "tier_share_cap": _say(ORDER_LIMIT, "买入后{tier_zh}档合计 {post_pct}%，超过上限 {cap_pct}%。|"
                                    "买入后会超过该风险档位的占比上限。",
                       "The buy would take the {tier} tier to {post_pct}% against a {cap_pct}% "
                       "cap.|The buy would pass this risk tier's share of the account."),
    "tier_order_cap": _say(ORDER_LIMIT, "这笔 ${order} 超过{tier_zh}档单笔上限 ${cap}。|"
                                    "这笔超过该风险档位的单笔上限。",
                       "This ${order} order is above the {tier} tier's ${cap} ticket.|"
                       "This order is above this risk tier's ticket size."),
    "exposure_group_overlap": _say(ORDER_LIMIT, "已持有同一类别的 {held}，同类只允许一只。|"
                                            "同一类别已持有一只，不再重复买入。",
                               "{held} is already held in the same exposure group, and a group "
                               "holds one fund.|A fund from the same exposure group is already "
                               "held."),
    "sleeve_max": _say(ORDER_LIMIT, "买入后这一资产类别会超过上限。",
                   "The buy would take this asset class past its maximum."),
    "stocks_combined_cap": _say(ORDER_LIMIT, "买入后个股合计 {post_pct}%，超过上限 {cap_pct}%。|"
                                         "买入后个股合计会超过上限。",
                            "The buy would take individual stocks to {post_pct}% against a "
                            "{cap_pct}% cap.|The buy would pass the cap on individual stocks."),
    "sector_cap": _say(ORDER_LIMIT, "买入后同一行业占 {post_pct}%，超过上限 {cap_pct}%。|"
                                "买入后同一行业会超过上限。",
                   "The buy would take one sector to {post_pct}% against a {cap_pct}% cap.|"
                   "The buy would pass the cap on one sector."),
    "max_positions": _say(ORDER_LIMIT, "持仓数量已到上限 {max} 只。|持仓数量已到上限。",
                      "The account already holds its maximum of {max} positions.|"
                      "The account already holds its maximum number of positions."),
    "new_positions_per_day": _say(ORDER_LIMIT, "今天新开仓已到上限 {max} 笔。|今天新开仓已到上限。",
                              "Today's limit of {max} new position(s) is used.|"
                              "Today's limit on new positions is used."),
    "new_positions_per_week": _say(ORDER_LIMIT, "本周新开仓已到上限 {max} 笔。|本周新开仓已到上限。",
                               "This week's limit of {max} new position(s) is used.|"
                               "This week's limit on new positions is used."),
    "orders_per_week": _say(ORDER_LIMIT, "近一周下单已到上限 {max} 笔。|近一周下单已到上限。",
                        "The limit of {max} orders in a week is used.|"
                        "The weekly limit on orders is used."),
    "same_day_round_trip": _say(ORDER_LIMIT, "今天刚卖出过这只，同一天不再买回。",
                            "It was sold today, and it is not bought back the same day."),
    "autonomous_allowance": _say(ORDER_LIMIT, "买入后自动交易占用 {post_pct}%，超过授权额度 {cap_pct}%。|"
                                          "自动交易的授权额度已经用满。",
                             "The buy would take what was opened automatically to {post_pct}% "
                             "against an allowance of {cap_pct}%.|The allowance for automatic "
                             "trading is used up."),
    "invalid_reduce_quantity": _say(ORDER_LIMIT, "减仓数量无效。", "The quantity to sell is not valid."),
    # -- the evidence was not there
    "market_evidence_missing": _say(DATA, "缺少开仓必需的行情或研究报告。",
                                "The market data or reports a position must be opened on are "
                                "missing."),
    "web_evidence_stale": _say(DATA, "引用的网页资料已过期，或来源不合规。",
                           "A web source it relied on is stale or was not properly retrieved."),
    "material_information_missing": _say(DATA, "研究指出还缺 {count} 项关键信息，不开仓。|"
                                           "研究指出还缺关键信息，不开仓。",
                                     "Research lists {count} material fact(s) still missing, so "
                                     "no position is opened.|Research lists material facts "
                                     "still missing, so no position is opened."),
    "evidence_quality_poor": _say(DATA, "研究判定这次的证据质量差。",
                              "Research rated its own evidence as poor."),
    "no_confidence": _say(DATA, "研究没有给出把握度。", "Research gave no confidence figure."),
    "prices_stale": _say(DATA, "持仓的价格数据已经 {age} 个交易日没更新。|持仓的价格数据过期。",
                     "A holding's price is {age} trading day(s) old.|A holding's price is stale."),
    "snapshot_warning": _say(DATA, "账本快照有异常，按拒绝处理。",
                         "The portfolio snapshot carries a warning, so nothing proceeds."),
    "regime_unknown": _say(DATA, "读不到大盘和波动率数据，暂不开仓。",
                       "The index and volatility data could not be read, so nothing is opened."),
    "earnings_date_unknown": _say(DATA, "查不到下一次财报日期，暂不买入个股。",
                              "The next earnings date could not be found, so the stock is not "
                              "bought."),
    "incomplete_proposal": _say(DATA, "提案信息不完整。", "The proposal is incomplete."),
    # -- a risk control said stop
    "kill_switch": _say(RISK_PAUSE, "交易已暂停，不提交任何订单。",
                    "Trading is paused; no order is submitted."),
    "drawdown_ladder": _say(RISK_PAUSE, "账户回撤触发风控（{level}），暂停开仓。|账户回撤触发风控，暂停开仓。",
                        "The drawdown ladder is at {level}, so nothing is opened.|"
                        "The drawdown ladder has stopped new positions."),
    "market_regime": _say(RISK_PAUSE, "市场处于{level_zh}状态，暂停开仓。|市场风险偏高，暂停开仓。",
                      "The market regime is {level}, so nothing is opened.|"
                      "The market regime has stopped new positions."),
    "earnings_blackout": _say(RISK_PAUSE, "临近财报，暂不买入。",
                          "It is too close to earnings to buy."),
    "macro_blackout": _say(RISK_PAUSE, "重要宏观数据发布前后，自动交易不开仓。",
                       "Around a major macro release, nothing is opened automatically."),
    "daily_loss_gate": _say(RISK_PAUSE, "今天已实现的亏损触到当日亏损线，今天不再开仓。",
                        "Today's realized loss reached the daily gate; nothing more is opened "
                        "today."),
    "real_money_off": _say(RISK_PAUSE, "实盘执行开关是关闭的。",
                       "Real-money execution is switched off."),
    # -- the account may not hold it
    "instrument_not_permitted": _say(NOT_PERMITTED, "这个品种不在允许交易的范围内。",
                                 "This instrument is outside what the account may trade."),
    "stock_screen_failed": _say(NOT_PERMITTED, "个股没有通过基本面准入筛选。",
                            "The stock did not pass the eligibility screen."),
    "strategy_not_allowed": _say(NOT_PERMITTED, "这类策略在这个账户里不被允许。",
                             "This kind of strategy is not allowed in this account."),
    "held_in_other_account": _say(NOT_PERMITTED, "这个标的已经在另一个账户里持有。",
                              "The symbol is already held in another account."),
    "no_position_to_reduce": _say(NOT_PERMITTED, "没有持仓可以卖。", "There is no position to sell."),
    "no_tier": _say(NOT_PERMITTED, "这个品种不属于任何已配置的风险档位。",
                "The instrument belongs to no configured risk tier."),
    "limits_missing": _say(NOT_PERMITTED, "账户缺少必需的风控配置，按拒绝处理。",
                       "The account lacks a required risk setting, so the order is refused."),
    "bucket_missing": _say(NOT_PERMITTED, "账本里没有这个账户。",
                       "The ledger does not hold this account."),
    "deferred_to_plan": _say(NOTHING_TO_DO, "研究结论已交给自动交易方案：由方案按自己的仓位和规则决定是否下单，"
                                           "这里不另外生成提案。",
                             "The conclusion is handed to the automatic-trading plan in force, "
                             "which decides the order under its own sizing and rules; no separate "
                             "proposal is written here."),
    # -- what the automatic plan itself holds a signal to (agentic_trading.auto.trader)
    "plan_paused": _say(RISK_PAUSE, "自动交易已暂停（{why}），暂不新开仓。|自动交易已暂停，暂不新开仓。",
                        "Automatic trading is paused ({why}); nothing new is opened.|"
                        "Automatic trading is paused; nothing new is opened."),
    "unresolved_order": _say(RISK_PAUSE, "账户里有一笔结果不明的订单，先对账，暂不提交新单。",
                             "An order in the account is unresolved; it is reconciled first, and "
                             "no new order is submitted until then."),
    "plan_result_unknown": _say(RISK_PAUSE, "有仓位估不出价（价格太旧，或和成交记录对不上），自动交易的盈亏算不出来，"
                                            "暂不新开仓。",
                                "A position cannot be valued -- its price is too old, or it does "
                                "not match the fills on record -- so what automatic trading has "
                                "made or lost cannot be worked out; nothing new is opened until it "
                                "can."),
    "autonomy_withdrawn": _say(RISK_PAUSE, "一笔自动订单的结果出了问题，自动下单已停止，等你检查后恢复。",
                               "An automatic order ended in a way nobody could account for, so "
                               "automatic orders have stopped until you look and restore them."),
    "plan_positions_full": _say(ORDER_LIMIT, "方案最多持有 {max} 个仓位，已经满了。|方案的仓位数已满。",
                                "The plan holds at most {max} position(s) and is full.|"
                                "The plan's positions are full."),
    "order_in_progress": _say(ORDER_LIMIT, "这只标的已有一笔订单在处理中。",
                              "An order for this symbol is already in progress."),
    "already_held": _say(NOTHING_TO_DO, "已经持有，方案只开仓不加仓。",
                         "Already held; the plan opens positions and does not add to them."),
    "not_in_plan": _say(NOT_PERMITTED, "不在方案允许买入的范围内。",
                        "It is outside what the plan may buy."),
    "not_managed": _say(NOT_PERMITTED, "这个仓位不是自动交易买入的，卖出需要你来确认。",
                        "This position was not opened by automatic trading; selling it is yours "
                        "to confirm."),
    "trend_not_confirmed": _say(STRATEGY, "收盘价 {close} 在自己的 {window} 日均线 {average} 下方，"
                                          "趋势未确认，暂不买入。|收盘价在均线下方，趋势未确认，暂不买入。",
                                "Its close of {close} is below its own {window}-session average "
                                "of {average}; the trend is not confirmed, so it is not bought "
                                "yet.|Its close is below its own average, so it is not bought yet."),
    "trend_unknown": _say(DATA, "读不到足够的日线来计算 {window} 日均线，暂不买入。|"
                                "读不到足够的日线来判断趋势，暂不买入。",
                          "There is not enough daily history to compute its {window}-session "
                          "average, so it is not bought yet.|There is not enough daily history "
                          "to judge its trend, so it is not bought yet."),
    "signal_expired": _say(DATA, "这份研究已经过了有效期，不再据此下单。",
                           "This research is past its lifetime and is no longer acted on."),
    "price_not_after_research": _say(DATA, "研究刚完成，要等行情更新到研究之后才下单（约 {minutes} 分钟）。|"
                                           "研究刚完成，要等行情更新到研究之后才下单。",
                                     "The research has only just finished; an order waits until "
                                     "prices are from after it (about {minutes} min).|The research "
                                     "has only just finished; an order waits until prices are from "
                                     "after it."),
    # -- a baseline plan's tranches (agentic_trading.auto.trader; contract 2.1 and 3.3)
    "baseline_research_not_a_buy": _say(NOTHING_TO_DO, "基准方案按你的基准比例分批买入，研究只用来否决，不单独下买单。",
                                        "Under the baseline plan purchases follow the owner's "
                                        "baseline weights in tranches; research can only veto "
                                        "them, and buys nothing by itself."),
    "tranche_written": _say(PROPOSAL, "写出了第 {index}/{of} 批买入（${usd}）。|写出了一批基准买入。",
                            "Tranche {index} of {of} (${usd}) was written.|A baseline tranche was "
                            "written."),
    "tranche_due": _say(PROPOSAL, "第 {index}/{of} 批（${usd}）已到期，下次运行写出提案。|这一批已到期，下次运行写出提案。",
                        "Tranche {index} of {of} (${usd}) is due; the next pass writes it.|This "
                        "tranche is due; the next pass writes it."),
    "tranche_not_due": _say(NOTHING_TO_DO, "还没到下一批的日期：{due} 起（两批之间至少隔 {spacing} 个交易日）。|"
                                           "还没到下一批的日期。",
                            "The next tranche is not due until {due} (tranches are at least "
                            "{spacing} sessions apart).|The next tranche is not due yet."),
    "tranche_vetoed": _say(STRATEGY, "{day} 的研究给出“{said_zh}”，这一批不买。|最近的研究给出减持或卖出，这一批不买。",
                           "Research on {day} said {said}; this tranche is not bought.|Recent "
                           "research said to hold less of it; this tranche is not bought."),
    "tranche_not_researched": _say(DATA, "最近 {days} 天没有研究过这只基金，不买。|最近没有研究过这只基金，不买。",
                                   "Nobody has researched this fund in the last {days} days, so "
                                   "it is not bought.|Nobody has researched this fund recently, "
                                   "so it is not bought."),
    "tranche_regime_gate": _say(RISK_PAUSE, "{gate_zh}，这一批不买。|市场状态或回撤阶梯现在不允许，这一批不买。",
                                "{gate_en}; this tranche is not bought.|The market regime or the "
                                "drawdown ladder does not allow it now; this tranche is not "
                                "bought."),
    "tranche_cooling_down": _say(RISK_PAUSE, "方案在这只基金上的仓位刚结束，{sessions} 个交易日内不买回，{due} 起再判断。|"
                                             "方案在这只基金上的仓位刚结束，冷却期内不买回。",
                                 "The plan's position in this fund has just ended; it is not "
                                 "bought back for {sessions} sessions, until {due}.|The plan's "
                                 "position in this fund has just ended; it is not bought back "
                                 "until its cooling-off period is over."),
    "tranche_below_minimum": _say(ORDER_LIMIT, "按比例只有 ${usd}，低于最小下单额 ${minimum}，不买。|"
                                               "按比例分到的金额低于最小下单额，不买。",
                                  "Its share is ${usd}, under the ${minimum} minimum order, so it "
                                  "is not bought.|Its share is under the minimum order, so it is "
                                  "not bought."),
    "tranche_position_limit": _say(ORDER_LIMIT, "账户最多持有 {max} 个仓位，按基准比例它排在后面，不买。|"
                                                "账户的仓位数有限，按基准比例它排在后面，不买。",
                                   "The account holds at most {max} positions, and its baseline "
                                   "weight ranks below theirs, so it is not bought.|The account's "
                                   "positions are limited, and its baseline weight ranks below "
                                   "theirs, so it is not bought."),
    "tranche_retry_later": _say(ORDER_LIMIT, "这一批今天已经试过 {tries} 次，稍后或下个交易日再试。|这一批稍后再试。",
                                "This tranche has been tried {tries} time(s) today; it is tried "
                                "again later.|This tranche is tried again later."),
    "tranche_complete": _say(NOTHING_TO_DO, "已经分 {of} 批买到目标。|已经买到目标。",
                             "Bought to its target in {of} tranche(s).|Bought to its target."),
    "first_real_order": _say(NOT_PERMITTED, "这是方案在实盘的第一笔买入，由你批准；成交后方案开始自动买入。",
                             "This is the plan's first real-money purchase and waits for you; "
                             "once it fills, the plan buys on its own."),
    "exit_price_stale": _say(DATA, "价格是 {day} 的，太旧，今天不判断止损。|价格太旧，今天不判断止损。",
                             "It is priced from {day}, too old to judge its stop today.|Its price "
                             "is too old to judge its stop today."),
    "exit_drill": _say(NOTHING_TO_DO, "模拟账户的卖出演练：证明代码能自己走完一次卖出，之后按批次规则买回。",
                       "A drill in the simulated account, to show code carries a sale through "
                       "itself; it is bought back by the tranche rules."),
    # -- a fault
    "risk_engine_error": _say(SYSTEM, "风控计算出错，按拒绝处理。",
                          "The risk engine failed, so the order is refused."),
    "unknown": _say(SYSTEM, "没能判断出原因，请查看这次研究的详细记录。",
                "The reason could not be determined; see the run's own record."),
    # -- something did come of it
    "proposal_waiting": _say(PROPOSAL, "已生成{action_zh}提案，等待批准。|已生成提案，等待批准。",
                         "A {action} proposal was created and is waiting for approval.|"
                         "A proposal was created and is waiting for approval."),
    "proposal_automatic": _say(PROPOSAL, "已生成{action_zh}提案，将按授权自动执行。|"
                                     "已生成提案，将按授权自动执行。",
                           "A {action} proposal was created and will be carried out under the "
                           "standing authorisation.|A proposal was created and will be carried "
                           "out under the standing authorisation."),
    "advice": _say(ADVICE, "只读建议：{action_zh}。这个账户不会下单。|只读建议。这个账户不会下单。",
               "Advice only: {action}. No order is placed for this account.|"
               "Advice only. No order is placed for this account."),
}

#: The limit that bound a sizing, by the code ``sizing.Limit`` gives it.
LIMIT_NAMES: dict[str, tuple[str, str]] = {
    "cash": ("可用现金", "Available cash"),
    "cash_floor": ("现金保留要求", "The cash that must be kept"),
    "deployed_cap": ("账户可投入上限", "The account's deployable capital"),
    "position_cap": ("单一标的上限", "The single-holding cap"),
    "order_cap": ("单笔上限", "The single-order cap"),
    "tier_share": ("风险档位占比上限", "The risk tier's share of the account"),
    "tier_order": ("风险档位单笔上限", "The risk tier's ticket size"),
    "sleeve_max": ("资产类别上限", "The asset-class maximum"),
    "stocks_combined": ("个股合计上限", "The cap on individual stocks"),
    "sector_cap": ("行业上限", "The cap on one sector"),
    "autonomous_allowance": ("自动交易额度", "The allowance for automatic trading"),
    "plan_capital": ("自动交易可用资金", "The capital set aside for automatic trading"),
}

_ACTION_ZH = {"BUY": "买入", "ADD": "加仓", "REDUCE": "减仓", "SELL": "卖出", "HOLD": "持有",
              "NO_TRADE": "不交易"}
_TIER_ZH = {"low": "低风险", "mid": "中风险", "high": "高风险"}
_REGIME_ZH = {"HIGH_RISK": "高风险", "ELEVATED_RISK": "风险偏高", "UNKNOWN": "未知"}
#: What research said, as a veto names it: a rating or a reviewer's action.
_SAID_ZH = {"underweight": "减持", "sell": "卖出", "reduce": "减仓", "overweight": "增持", "buy": "买入",
            "hold": "持有"}


# ---------------------------------------------------------------- what a Hard Risk rule means
#
# Rule ids are the engine's own and are stable; the wording of a rule's detail is not, and is
# never read here. A rule that needs a fact to be told apart says so through ``facts``.
_RULES: dict[str, str] = {
    "HR00": "incomplete_proposal", "HR00b": "snapshot_warning", "HR13": "prices_stale",
    "HR17": "real_money_off", "HR14": "no_position_to_reduce", "HR14b": "invalid_reduce_quantity",
    "HR01": "kill_switch", "HR02": "drawdown_ladder", "HR03": "market_regime",
    "HR18": "earnings_blackout", "HR19": "macro_blackout", "HR15": "strategy_not_allowed",
    "HR04": "instrument_not_permitted", "HR04b": "stock_screen_failed",
    "HR05c": "market_evidence_missing", "HR05e": "web_evidence_stale",
    "HR05d": "material_information_missing", "HR05": "confidence_below_floor",
    "HR05b": "evidence_quality_poor", "HR07": "cash_short", "HR07b": "held_in_other_account",
    "HR07c": "account_fully_deployed", "HR08": "position_cap", "HR08b": "single_order_cap",
    "HR21": "tier_share_cap", "HR21b": "tier_order_cap", "HR09": "exposure_group_overlap",
    "HR10": "sleeve_max", "HR10b": "cash_floor", "HR11": "stocks_combined_cap",
    "HR11b": "sector_cap", "HR11c": "max_positions", "HR12": "new_positions_per_day",
    "HR12b": "new_positions_per_week", "HR20": "orders_per_week", "HR20b": "same_day_round_trip",
    "HR22": "daily_loss_gate", "HR23": "autonomous_allowance",
}

#: What the engine could not rule on at all, by the code it records beside the sentence.
_FAIL_CLOSED: dict[str, str] = {
    "incomplete_proposal": "incomplete_proposal", "snapshot_warnings": "snapshot_warning",
    "real_money_disabled": "real_money_off", "bucket_missing": "bucket_missing",
    "regime_unavailable": "regime_unknown", "earnings_date_unknown": "earnings_date_unknown",
    "missing_confidence": "no_confidence", "evidence_poor": "evidence_quality_poor",
    "no_tier": "no_tier", "limits_missing": "limits_missing", "no_screen": "limits_missing",
    "engine_exception": "risk_engine_error",
}


@dataclass(frozen=True)
class Reason:
    """One thing that stood between a run and an order."""

    code: str
    facts: dict[str, Any] = field(default_factory=dict)
    rule_id: str | None = None

    @property
    def category(self) -> str:
        return _TEXT.get(self.code, _TEXT["unknown"])[0]

    def text(self) -> dict[str, str]:
        _category, zh, en = _TEXT.get(self.code, _TEXT["unknown"])
        facts = _presentable(self.facts)
        return {"zh": _render(zh, facts), "en": _render(en, facts)}

    def as_dict(self) -> dict[str, Any]:
        return {"category": self.category, "code": self.code, "rule_id": self.rule_id,
                "text": self.text()}


@dataclass(frozen=True)
class Outcome:
    """Why a run ended as it did: the reason that leads, and every other one behind it."""

    reasons: tuple[Reason, ...]

    @property
    def lead(self) -> Reason:
        return self.reasons[0]

    @property
    def category(self) -> str:
        return self.lead.category

    @property
    def code(self) -> str:
        return self.lead.code

    def as_dict(self) -> dict[str, Any]:
        zh, en = LABELS[self.category]
        return {"category": self.category, "code": self.code,
                "label": {"zh": zh, "en": en}, "summary": self.lead.text(),
                "reasons": [reason.as_dict() for reason in self.reasons]}


def outcome_of(*reasons: Reason) -> Outcome:
    """The reasons, the one a person can do least about first; the engine's order otherwise."""
    if not reasons:
        reasons = (Reason("unknown"),)
    rank = {category: index for index, category in enumerate(_PRIORITY)}
    ordered = sorted(enumerate(reasons), key=lambda pair: (rank.get(pair[1].category, 99), pair[0]))
    seen: set[tuple[str, str | None]] = set()
    unique = []
    for _index, reason in ordered:
        key = (reason.code, reason.rule_id)
        if key not in seen:
            seen.add(key)
            unique.append(reason)
    return Outcome(tuple(unique))


def _render(template: str, facts: dict[str, Any]) -> str:
    """The sentence with its facts, or its plain form when one of them is not on record."""
    full, _, plain = template.partition("|")
    try:
        return full.format(**facts)
    except (KeyError, IndexError, ValueError):
        return plain or full


def _presentable(facts: dict[str, Any]) -> dict[str, Any]:
    """Facts as a sentence prints them: money to the cent, shares of an account to one place."""
    out: dict[str, Any] = {}
    for key, value in (facts or {}).items():
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, (int, float)):
            if key.endswith("_pct"):
                out[key] = f"{float(value):.1f}".rstrip("0").rstrip(".")
            elif key in ("cash", "order", "cap", "deployed", "requested", "minimum", "legal_max",
                         "notional"):
                out[key] = f"{float(value):,.2f}"
            else:
                out[key] = f"{value:g}" if isinstance(value, float) else str(value)
        else:
            out[key] = str(value)
    action = str((facts or {}).get("action") or "")
    if action:
        out["action_zh"] = _ACTION_ZH.get(action, action)
        out["action"] = action.replace("_", " ").lower()
    reviewer = str((facts or {}).get("reviewer") or "")
    if reviewer:
        out["reviewer_zh"] = _ACTION_ZH.get(reviewer, reviewer)
        out["reviewer"] = reviewer.replace("_", " ").lower()
    tier = str((facts or {}).get("tier") or "")
    if tier:
        out["tier_zh"] = _TIER_ZH.get(tier, tier)
    level = str((facts or {}).get("level") or "")
    if level:
        out["level_zh"] = _REGIME_ZH.get(level, level)
    said = str((facts or {}).get("said") or "")
    if said:
        out["said_zh"] = _SAID_ZH.get(said.lower(), said)
        out["said"] = said.replace("_", " ")
    gate = str((facts or {}).get("gate") or "")
    if gate:
        out["gate_zh"], out["gate_en"] = {
            "regime": (f"市场状态是{_REGIME_ZH.get(level, level)}",
                       f"The market regime is {level.replace('_', ' ').lower()}"),
            "ladder": (f"回撤阶梯在 {level} 档，不在正常档", f"The drawdown ladder is at {level}, not NORMAL"),
            "macro": ("今天在宏观数据发布的窗口内", "Today is inside a macro release window"),
        }.get(gate, ("市场状态或回撤阶梯现在不允许", "The market regime or the ladder does not allow it now"))
    limit = str((facts or {}).get("limit") or "")
    if limit:
        zh, en = LIMIT_NAMES.get(limit, (limit, limit))
        out["limit_zh"], out["limit"] = zh, en
    return out


# ---------------------------------------------------------------- a research job
def failure_for_exit(code: int, *, has_record: bool) -> tuple[str, str]:
    """(failure code, sentence for the job row) for a pipeline that did not complete.

    The sentence is what ``job.error`` has always held and what a notification quotes, so the
    three that existed keep their words; the others name what a person can do about it.
    """
    if code == EXIT_OK and not has_record:
        return "no_record", "research exited without a matching decision record"
    if code == EXIT_LIMIT:
        return "llm_limit", "Claude subscription limit reached; retry after reset"
    if code == EXIT_LOGIN:
        return "llm_login_expired", ("the Claude login on this Mac has expired; sign in again "
                                     "(run `claude`, then /login) and the run is retried")
    if code == EXIT_FAIL_CLOSED:
        return "portfolio_unavailable", ("research stopped before it began: the ledger or its "
                                         "prices could not be read")
    if code == EXIT_SUBJECT:
        return "subject_unavailable", "the holding to review is no longer in the account"
    if code == EXIT_TIMEOUT:
        return "timeout", "research worker deadline reached"
    return "pipeline_error", f"research pipeline exited with code {code}"


#: What our own code has written into ``job.error``, for a row from before ``failure_code``.
_LEGACY_ERRORS = (
    ("research pipeline exited with code 2", "portfolio_unavailable"),
    ("without a matching decision record", "no_record"),
    ("no longer present", "subject_unavailable"),
    ("unavailable or incomplete", "subject_unavailable"),
    ("worker failed", "worker_crashed"),
    ("worker exited without a result", "worker_crashed"),
    ("worker launch failed", "worker_crashed"),
    ("research pipeline exited", "pipeline_error"),
)


def classify_job(job: dict[str, Any], *, log_tail: str = "") -> Outcome | None:
    """Why a research job that is over did not produce a decision, or None if it did or might yet.

    ``log_tail`` is the end of the job's own log, for a row written before the reason was stored
    with it: on 2026-10-02 every failed run was recorded as "exited with code 1", and only the
    log says the login had expired.
    """
    state = str(job.get("state") or "")
    if state in ("QUEUED", "RUNNING", "COMPLETE", ""):
        return None
    if state == "CLAUDE_LIMIT_REACHED":
        return outcome_of(Reason("llm_limit"))
    if state == "TIMED_OUT":
        seconds = job.get("timeout_seconds")
        facts = {"minutes": round(float(seconds) / 60)} if seconds else {}
        return outcome_of(Reason("timeout", facts))
    if state == "CANCELLED":
        return outcome_of(Reason("cancelled"))
    code = job.get("failure_code")
    if code not in _TEXT:
        code = legacy_failure_code(str(job.get("error") or ""), log_tail)
    return outcome_of(Reason(str(code)))


def legacy_failure_code(error: str, log_tail: str = "") -> str:
    """The failure code of a row that stored only a sentence, from that sentence and its log."""
    from .llm.claude_code import _looks_limited, _looks_logged_out

    for line in reversed((log_tail or "").splitlines()[-40:]):
        if "FAILED" in line or "STOPPED" in line or "Error" in line:
            _head, _, message = line.partition(": ")
            if _looks_logged_out("", message):
                return "llm_login_expired"
            if _looks_limited("", message):
                return "llm_limit"
    for marker, code in _LEGACY_ERRORS:
        if marker in error:
            return code
    return "pipeline_error"


# ---------------------------------------------------------------- a decision record
def classify_record(record: dict[str, Any], *, no_trade_below: int | None = None,
                    floor: int | None = None) -> Outcome:
    """Why a finished run ended as it did, from its own decision record.

    ``no_trade_below`` and ``floor`` are the confidence lines the profile holds for this run's
    account and instrument. They are only needed for a record from before the policy step wrote
    its own reason codes, and without them such a record is described a little less exactly.
    """
    policy = record.get("policy") or {}
    hard = record.get("hard_risk") or {}
    final = record.get("final_recommendation") or {}
    action = str(final.get("action") or policy.get("final_action") or "")

    if record.get("advisory") or final.get("scope") == "read_only" or hard.get("verdict") == "READ_ONLY":
        if policy.get("error"):
            return outcome_of(Reason("policy_review_failed"))
        return outcome_of(Reason("advice", {"action": action or "NO_TRADE"}))
    if record.get("proposal_id"):
        proposal = record.get("proposal") or {}
        waits = proposal.get("requires_human_approval", hard.get("requires_human_approval", True))
        return outcome_of(Reason("proposal_waiting" if waits else "proposal_automatic",
                                 {"action": proposal.get("action") or action}))
    if record.get("error"):
        return outcome_of(Reason("pipeline_error"))
    if (record.get("auto_plan") or {}).get("deferred"):
        return outcome_of(Reason("deferred_to_plan"))

    verdict = str(hard.get("verdict") or "")
    if verdict == "REJECT":
        return outcome_of(*_hard_risk_reasons(hard))
    if policy.get("error"):
        return outcome_of(Reason("policy_review_failed"))
    wanted = str(policy.get("final_action") or "")
    if wanted in ("BUY", "ADD") and verdict == "NO_ACTION":
        return outcome_of(_sizing_reason(hard))
    if wanted == "HOLD":
        return outcome_of(Reason("hold"))
    return outcome_of(*_policy_reasons(record, no_trade_below=no_trade_below, floor=floor))


def _hard_risk_reasons(hard: dict[str, Any]) -> list[Reason]:
    """Every rule that failed, and everything the engine closed on without a rule to name."""
    reasons: list[Reason] = []
    regime = str((hard.get("regime") or {}).get("level") or "")
    closed = [str(code) for code in hard.get("fail_closed_codes") or []]
    for rule in hard.get("rules") or []:
        if rule.get("passed") or rule.get("blocking") is False:
            continue
        rule_id = str(rule.get("rule_id") or "")
        facts = dict(rule.get("facts") or {})
        if rule_id == "HR06":
            reasons.append(_sizing_reason(hard, rule=rule))
            continue
        code = _RULES.get(rule_id, "unknown")
        if rule_id == "HR00" and "engine_exception" in closed:
            code = "risk_engine_error"
        elif rule_id == "HR03":
            facts.setdefault("level", regime)
            if regime == "UNKNOWN":
                code = "regime_unknown"
        elif rule_id == "HR05" and "missing_confidence" in closed:
            code = "no_confidence"
        elif rule_id == "HR18" and "earnings_date_unknown" in closed:
            code = "earnings_date_unknown"
        elif rule_id == "HR02":
            facts.setdefault("level", (hard.get("ladder") or {}).get("effective_level_for_bucket"))
        reasons.append(Reason(code, facts, rule_id))
    named = {reason.code for reason in reasons}
    for code in closed:
        mapped = _FAIL_CLOSED.get(code)
        if mapped and mapped not in named:
            reasons.append(Reason(mapped))
            named.add(mapped)
    if not reasons:
        # A record from before the engine wrote codes beside its fail-closed sentences. The
        # sentences are still there and are still shown by the run's own page; here the honest
        # answer is that something closed, not a guess at which.
        reasons.append(Reason("risk_engine_error" if hard.get("fail_closed_reasons") else "unknown"))
    return reasons


def _sizing_reason(hard: dict[str, Any], *, rule: dict[str, Any] | None = None) -> Reason:
    """Why sizing produced no order: at its target, or boxed in below the minimum order."""
    sizing = hard.get("sizing") or {}
    if not sizing and rule is None:
        rule = next((r for r in hard.get("rules") or [] if r.get("rule_id") == "HR06"), None)
    facts = dict(sizing or (rule or {}).get("facts") or {})
    code = str(facts.get("code") or "")
    if code in ("at_target", "target_below_minimum", "limit_below_minimum", "limit_exhausted"):
        if code in ("limit_below_minimum", "limit_exhausted"):
            binding = facts.get("binding") or []
            if binding:
                facts["limit"] = binding[0]
        return Reason(code, {k: v for k, v in facts.items() if k != "binding"}, "HR06")
    # A record from before sizing said which it was. Both figures it does carry are shares of the
    # allocation: the target the hint asked for and what the account already holds.
    try:
        held = float(hard.get("post_trade_weight_pct") or 0.0)
        target = float(hard.get("target_weight_pct_of_allocation") or 0.0)
    except (TypeError, ValueError):
        return Reason("target_below_minimum", {}, "HR06")
    return Reason("at_target" if target > 0 and held >= target - 1e-9 else "target_below_minimum",
                  {}, "HR06")


def _policy_reasons(record: dict[str, Any], *, no_trade_below: int | None,
                    floor: int | None) -> list[Reason]:
    """What the policy step decided, from the codes it wrote or from the fields it always kept."""
    policy = record.get("policy") or {}
    written = [Reason(str(item.get("code")), {k: v for k, v in item.items() if k != "code"})
               for item in policy.get("reason_codes") or []
               if isinstance(item, dict) and item.get("code") in _TEXT]
    if written:
        return written
    rating = str((record.get("upstream_decision") or {}).get("portfolio_manager_signal") or "")
    confidence = policy.get("confidence")
    reviewer = policy.get("reviewer_action")
    if rating == "Hold":
        return [Reason("rating_hold_no_position")]
    if rating in ("Underweight", "Sell"):
        return [Reason("rating_bearish_no_position", {"rating": rating})]
    if rating not in ("Buy", "Overweight"):
        return [Reason("rating_not_tradeable")]
    if confidence is None:
        return [Reason("no_confidence")]
    if no_trade_below is not None and confidence < no_trade_below:
        return [Reason("confidence_below_no_trade",
                       {"confidence": confidence, "threshold": no_trade_below})]
    if floor is not None and confidence < floor:
        return [Reason("confidence_below_floor", {"confidence": confidence, "floor": floor})]
    if reviewer in ("NO_TRADE", "HOLD", "REDUCE", "SELL"):
        return [Reason("reviewer_cautious", {"reviewer": reviewer})]
    return [Reason("no_trade")]
