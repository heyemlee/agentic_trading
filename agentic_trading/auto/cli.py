"""``ct-auto``: the automatic-trading plan, the modes and the simulated account, from a terminal.

The same service the API serves (``service/auto.py``), so what this prints is what the console
shows. ``plan``, ``status`` and ``live`` read; the rest act on the simulated account. On the real
account this can do exactly two things besides read, and both are stopping: ``pause --live`` and
``stop --live``. Starting a plan there, resuming one and replacing its strategy take the console
password, which a terminal does not have, so they are done in the console and nowhere else. No
command here can change ``config/`` or place a real order.
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from ..env import load_env
from . import modes


def _say(pair: dict[str, str] | None, language: str) -> str:
    return str((pair or {}).get(language) or (pair or {}).get("en") or "")


def render_plan(plan: dict[str, Any], language: str = "zh") -> str:
    """The plan as a person reads it: what it comes to, then the detail behind each figure."""
    zh = language == "zh"
    envelope, strategy, account = plan["envelope"], plan["strategy"], plan["account"]
    mode = _say(plan["mode"]["label"], language)
    lines = [
        (f"【{mode}模式方案】" if zh else f"[{mode} plan]") + f"  plan {plan['plan_id']}",
        _say(plan["summary"], language),
        "",
        (f"账户：权益 ${account['equity_usd']:,.2f}，现金 ${account['cash_usd']:,.2f}，"
         f"持仓 {account['positions']} 个（账本日期 {account['as_of']}）" if zh else
         f"Account: equity ${account['equity_usd']:,.2f}, cash ${account['cash_usd']:,.2f}, "
         f"{account['positions']} position(s), ledger dated {account['as_of']}"),
        (f"你确认的边界：可用资金 ${envelope['capital_usd']:,.2f}；亏损 "
         f"${envelope['loss_pause_usd']:,.2f}（{envelope['loss_pause_pct']:g}%）时暂停新开仓" if zh else
         f"Your envelope: ${envelope['capital_usd']:,.2f} of capital; new positions stop at a loss "
         f"of ${envelope['loss_pause_usd']:,.2f} ({envelope['loss_pause_pct']:g}%)"),
        (f"策略参数（{'模型建议' if strategy['suggested_by'] == 'model' else '默认模板'}）："
         f"最多 {strategy['max_positions']} 个仓位，每个占可用资金 {strategy['position_pct']:g}%，"
         f"把握度 ≥ {strategy['min_confidence']} 才买入，跌 {strategy['stop_loss_pct']:g}% 或持有满 "
         f"{strategy['max_holding_days']} 天退出" if zh else
         f"Strategy ({'model' if strategy['suggested_by'] == 'model' else 'template'}): up to "
         f"{strategy['max_positions']} position(s) of {strategy['position_pct']:g}% each, buy at "
         f"confidence >= {strategy['min_confidence']}, exit at -{strategy['stop_loss_pct']:g}% or "
         f"after {strategy['max_holding_days']} days"),
        ("可买范围：" if zh else "May buy: ") + ", ".join(strategy["universe"]),
    ]
    if strategy.get("suggestion_error"):
        lines.append(("模型建议不可用：" if zh else "No model suggestion: ") + strategy["suggestion_error"])
    if _say(strategy.get("rationale"), language):
        lines.append(("理由：" if zh else "Why: ") + _say(strategy["rationale"], language))
    for note in plan["clamps"]:
        lines.append(("  已调整：" if zh else "  adjusted: ") + _say(note["why"], language))

    for title, key in ((("按当前设置" if zh else "As configured today"), "today"),
                       (("按本方案" if zh else "Under this plan"), "under_plan")):
        view = plan[key]
        minimum = view["minimum_order"]
        source = ({"product": "本产品设置", "broker": "券商要求"} if zh else
                  {"product": "product setting", "broker": "venue requirement"})[minimum["source"]]
        lines += ["", f"— {title} —",
                  (f"最低下单金额 ${minimum['usd']:,.2f}（{source}）；"
                   f"超上限时{'先缩小再下单' if view['fits_to_limits'] else '整笔拒绝'}" if zh else
                   f"Minimum order ${minimum['usd']:,.2f} ({source}); an order over a limit is "
                   f"{'cut to fit' if view['fits_to_limits'] else 'refused'}")]
        for step in view["path"]:
            if step["feasible"]:
                cut = ((f"（由 ${step['requested_usd']:,.2f} 缩小）" if zh else
                        f" (cut from ${step['requested_usd']:,.2f})")
                       if step.get("requested_usd", 0) > step["order_usd"] + 0.005 else "")
                lines.append(f"  ✓ {step['symbol']:5s} ${step['order_usd']:,.2f}{cut}")
            else:
                lines.append(f"  ✗ {step['symbol']:5s} " + _say(step["why_not"], language))
        lines.append(f"  实际可投入：${view['deployable_usd']:,.2f}（{view['positions']} 个仓位）" if zh else
                      f"  Can actually be invested: ${view['deployable_usd']:,.2f} "
                      f"({view['positions']} position(s))")
    pace = plan["under_plan"]["pace"]
    lines.append(f"  建仓节奏：每天最多新开 {pace['new_positions_per_day']} 个、每周最多新开 "
                  f"{pace['new_positions_per_week']} 个仓位（账户限额）" if zh else
                  f"  Pace: at most {pace['new_positions_per_day']} new position(s) a day and "
                  f"{pace['new_positions_per_week']} a week (the account's limits)")

    if plan["changes"]:
        kinds = ({"boundary": "边界·需你确认", "product": "产品参数", "schedule": "研究安排"} if zh else
                 {"boundary": "boundary, yours to confirm", "product": "product setting",
                  "schedule": "research schedule"})
        waits = {row["path"]: row["waits"] for row in plan.get("landing", {}).get("changes", [])}
        lines += ["", "— 需要调整的设置 —" if zh else "— Settings that would change —"]
        unset = "未设置" if zh else "not set"
        for change in plan["changes"]:
            mark = (" ⏳" if waits.get(change["path"]) else "")
            lines.append(f"  [{kinds[change['kind']]}]{mark} {change['path']}: "
                         f"{unset if change['from'] is None else change['from']} → {change['to']}")
            lines.append("      " + _say(change["why"], language))
        if plan.get("landing"):
            lines.append("  " + _say(plan["landing"]["summary"], language))
            for error in plan["landing"]["errors"]:
                lines.append(("  无法应用：" if zh else "  cannot be applied: ") + error)

    venue = plan.get("venue")
    if venue:
        lines += ["", "— 券商要求 —" if zh else "— Venue requirements —",
                  (f"  {venue['name']}：最低下单 ${venue['min_order_usd']:,.2f}，"
                   f"{'支持' if venue['fractional'] else '不支持'}碎股；"
                   f"{'已由券商对本账户确认' if venue['confirmed_for_account'] else '来自券商公开文档，尚未在本账户上确认'}"
                   if zh else
                   f"  {venue['name']}: minimum order ${venue['min_order_usd']:,.2f}, fractional "
                   f"{'supported' if venue['fractional'] else 'not supported'}; "
                   f"{'confirmed for this account' if venue['confirmed_for_account'] else 'from published documentation, not yet confirmed for this account'}")]
        for question in venue["open_questions"]:
            lines.append(("  待核实：" if zh else "  open: ") + _say(question, language))
    return "\n".join(lines)


def render_status(status: dict[str, Any], language: str = "zh") -> str:
    zh = language == "zh"
    lines = ["【自动交易状态】" if zh else "[Automatic trading]"]
    for mode in status["modes"]:
        state = (("已对实盘开放" if zh else "open for real money") if mode["open_for_real_money"]
                 else ("未开放" if zh else "not open"))
        lines.append(f"{mode['order']}. {_say(mode['label'], language)}：{state}"
                     + (f"（已模拟 {mode['simulated_days']} 个交易日）" if zh else
                        f" ({mode['simulated_days']} simulated session(s))"))
        for blocker in mode["blockers"]:
            lines.append("    - " + _say(blocker["text"], language))
    # What is not a mode, and does not open because one did.
    for extension in status.get("extensions") or []:
        lines.append(f"·  {_say(extension['label'], language)}："
                     + ("未开放" if zh else "not open"))
        for blocker in extension["blockers"]:
            lines.append("    - " + _say(blocker["text"], language))
    real = status.get("real_account") or {}
    if real.get("request"):
        lines.append("实盘：方案已确认，等待设置生效（详见 ct-auto live）。" if zh else
                      "Real account: a plan is confirmed and waiting for its settings (ct-auto live).")
    elif not real.get("open"):
        lines.append("实盘：没有方案生效。" if zh else "Real account: no plan is in force.")
    return "\n".join(lines)


#: Why a session was not counted, as a person reads it (the codes are ``auto.runner.PROBLEMS``).
PROBLEM_TEXT = {
    "pass_failed": ("运行失败（账本或行情读不出来）", "a pass failed: the book or its prices could not be read"),
    "paused": ("当时处于暂停", "the plan was paused"),
    "autonomy_withdrawn": ("自动下单权限被收回", "automatic orders had been withdrawn"),
    "unknown_order": ("有订单结果不明", "an order's outcome was unknown"),
    "result_unknown": ("仓位与成交记录对不上", "a position did not match the fills on record"),
    "regime_unknown": ("大盘数据读不到", "the index data could not be read"),
    "book_unconfirmed": ("模拟账本未能确认到当日", "the simulated book could not be dated to the session"),
}


def render_simulation(sim: dict[str, Any], language: str = "zh") -> str:
    """The simulated account as a person reads it: is it running, what has it done, why not more."""
    zh = language == "zh"
    lines = ["【模拟账户】" if zh else "[Simulated account]"]
    if not sim.get("exists"):
        return "\n".join([*lines, "  " + _say(sim.get("note"), language)])
    plan, state, version = sim.get("plan"), sim.get("state") or {}, sim.get("version")
    if plan is None:
        lines.append("  没有生效的方案；已有仓位仍按原策略管理。" if zh else
                     "  No plan is in force; positions it opened are still looked after.")
    else:
        envelope, strategy = plan["envelope"], plan["strategy"]
        label = _say(modes.TABLE[envelope["mode"]].as_dict()["label"], language)
        lines.append(f"  方案 {plan['plan_id']}（{label}）：可用资金 ${envelope['capital_usd']:,.2f}，"
                      f"亏损暂停线 ${envelope['loss_pause_usd']:,.2f}" if zh else
                      f"  Plan {plan['plan_id']} ({label}): ${envelope['capital_usd']:,.2f} of capital, "
                      f"loss line ${envelope['loss_pause_usd']:,.2f}")
        lines.append(f"  策略版本 {plan['opening_version']}：最多 {strategy['max_positions']} 个仓位，"
                      f"每个 {strategy['position_pct']:g}%，把握度 ≥ {strategy['min_confidence']}，"
                      f"止损 {strategy['stop_loss_pct']:g}%，最长持有 {strategy['max_holding_days']} 天" if zh else
                      f"  Strategy version {plan['opening_version']}: up to {strategy['max_positions']} "
                      f"position(s) of {strategy['position_pct']:g}%, confidence >= "
                      f"{strategy['min_confidence']}, stop {strategy['stop_loss_pct']:g}%, held at most "
                      f"{strategy['max_holding_days']} days")
        if plan.get("managing"):
            lines.append(("  仍在管理旧仓位的版本：" if zh else "  Versions still managing positions: ")
                         + ", ".join(plan["managing"]))
    if state.get("paused"):
        lines.append(f"  已暂停新开仓：{state.get('reason')}" if zh else
                      f"  Paused, opening nothing: {state.get('reason')}")
    sessions = sim.get("sessions") or {}
    if version:
        lines.append(f"  有效模拟交易日：{sessions.get('counted', 0)} / {sessions.get('needed_to_open')}"
                      f"（等待样本）；完整买卖流程："
                      f"{'已验证' if version['validation'].get('exits_verified') else '尚未发生'}" if zh else
                      f"  Sessions counted: {sessions.get('counted', 0)} of "
                      f"{sessions.get('needed_to_open')} needed; a purchase carried to a sale: "
                      f"{'yes' if version['validation'].get('exits_verified') else 'not yet'}")
    for row in sessions.get("not_counted") or []:
        why = "、".join(PROBLEM_TEXT.get(code, (code, code))[0 if zh else 1] for code in row["problems"]) \
            or ("开盘时段的运行不足" if zh else "too few passes while the market was open")
        lines.append(f"    - {row['session']}: {why}")
    account, funding = sim.get("account"), sim.get("funding")
    if account:
        result = account.get("result_usd")
        # Not known where no funding is on record: an amount that cannot be said is not zero.
        made = f"${result:+,.2f}" if result is not None else ("无法确定" if zh else "not known")
        lines.append(f"  账户：权益 ${account['equity_usd']:,.2f}（现金 ${account['cash_usd']:,.2f}），"
                      f"累计入金 ${account['funded_usd']:,.2f}，盈亏 {made}"
                      f"（记录于 {account['at'][:16]}Z）" if zh else
                      f"  Account: equity ${account['equity_usd']:,.2f} (cash ${account['cash_usd']:,.2f}), "
                      f"funded ${account['funded_usd']:,.2f}, result {made} "
                      f"(recorded {account['at'][:16]}Z)")
    elif funding:
        lines.append(f"  账户：现金 ${funding['cash_usd']:,.2f}，累计入金 ${funding['net_usd']:,.2f}；"
                      "还没有运行记录，暂无估值。" if zh else
                      f"  Account: cash ${funding['cash_usd']:,.2f}, funded ${funding['net_usd']:,.2f}; "
                      "no pass has run yet, so there is no valuation.")
    for held in sim.get("positions") or []:
        quantity = held["managed_quantity"]
        lines.append(f"  持仓 {held['symbol']}：" + ("数量无法确认" if quantity is None else f"{quantity:g} 股")
                      + f"，策略版本 {held['strategy_version']}" if zh else
                      f"  Holding {held['symbol']}: " + ("quantity cannot be confirmed" if quantity is None
                                                        else f"{quantity:g} share(s)")
                      + f", version {held['strategy_version']}")
    for trade in (sim.get("trades") or [])[-5:]:
        lines.append(f"  已平仓 {trade['symbol']}：${trade['realized_usd']:+,.2f}"
                      f"（{'代码' if trade['closed_by'] == 'code' else '人工'}平仓）" if zh else
                      f"  Closed {trade['symbol']}: ${trade['realized_usd']:+,.2f} "
                      f"(closed by {'code' if trade['closed_by'] == 'code' else 'a person'})")
    for signal in (sim.get("signals") or [])[:5]:
        found = signal.get("diagnosis") or {}
        lines.append(f"  {'研究' if zh else 'Research'} {signal.get('ticker')}: "
                     + _say(found.get("summary"), language)
                     + ("" if signal.get("final", True) else ("（等待中）" if zh else " (waiting)")))
    coverage = (sim.get("research") or {}).get("text")
    if coverage:
        lines.append("  ! " + _say(coverage, language))
    return "\n".join(lines)


ROW_NAMES = {"strategy": ("策略", "Strategy"), "passive": ("被动持有", "Passive"),
             "simple_rule": ("简单规则", "Simple rule"), "benchmark": ("60/40 基准", "60/40 benchmark"),
             "cash": ("现金", "Cash")}


def _figure(value: Any, suffix: str = "", signed: bool = False) -> str:
    if value is None:
        return "—"
    return (f"{value:+.2f}" if signed else f"{value:.2f}") + suffix


def render_rows(rows: dict[str, dict[str, Any]], language: str = "zh") -> list[str]:
    """One line a comparator: what it returned, what it risked, what it traded and what that cost.

    A figure that could not be computed is a dash. It is never printed as a zero.
    """
    zh = language == "zh"
    head = (f"  {'':10s} {'净收益%':>9s} {'最大回撤%':>9s} {'换手%':>8s} {'成本$':>8s} {'平仓':>5s}" if zh else
            f"  {'':16s} {'return%':>9s} {'drawdown%':>10s} {'turnover%':>10s} {'costs$':>8s} {'closed':>7s}")
    lines = [head]
    for name in ("strategy", "passive", "simple_rule", "benchmark", "cash"):
        row = rows.get(name)
        if row is None:
            continue
        label = ROW_NAMES[name][0 if zh else 1]
        figures = row.get("figures")
        if figures is None:
            why = row.get("why_not")
            why = _say(why, language) if isinstance(why, dict) else str(why or "")
            lines.append(f"  {label}: " + (why or ("没有结果" if zh else "no result")))
            continue
        width = 10 if zh else 16
        lines.append(f"  {label:{width}s} {_figure(figures['net_return_pct'], signed=True):>9s} "
                     f"{_figure(figures['max_drawdown_pct']):>{9 if zh else 10}s} "
                     f"{_figure(figures['turnover_pct']):>{8 if zh else 10}s} "
                     f"{_figure(figures['costs_usd']):>8s} {figures['trades_closed']:>{5 if zh else 7}d}")
    return lines


def render_performance(report: dict[str, Any], language: str = "zh") -> str:
    zh = language == "zh"
    lines = ["【模拟表现】" if zh else "[Simulated performance]"]
    if not report.get("exists"):
        return "\n".join([*lines, "  " + ("模拟账户还没有启动。" if zh else
                                          "The simulated account has not been started.")])
    sim = report["simulation"]
    window = sim["window"]
    lines.append(f"— 实时模拟：{window['start'] or '—'} 至 {window['end'] or '—'}，记录 {window['sessions']} 个交易日，"
                  f"其中计入验证的 {sim['sessions_counted']} 个 —" if zh else
                  f"— Live simulation: {window['start'] or '—'} to {window['end'] or '—'}, "
                  f"{window['sessions']} session(s) on record, {sim['sessions_counted']} counted —")
    lines += render_rows(sim["rows"], language) if sim["rows"] else [
        "  还没有运行记录。" if zh else "  No pass has run yet."]
    if sim.get("comparators_unavailable"):
        lines.append(("  对照组暂时算不出来：" if zh else "  The comparators could not be computed: ")
                     + sim["comparators_unavailable"])
    research = sim["research"]
    outcomes = "、".join(f"{key} {value}" for key, value in sorted(research["run_outcomes"].items())) or "—"
    lines.append(f"  研究 {research['research_runs']} 次（{outcomes}），模型调用 {research['llm_calls']} 次，"
                  f"耗时 {research['research_seconds']:.0f} 秒" if zh else
                  f"  {research['research_runs']} research run(s) ({outcomes}), {research['llm_calls']} "
                  f"model call(s), {research['research_seconds']:.0f}s")
    if sim["conclusions_by_outcome"]:
        lines.append(("  研究结论的去向：" if zh else "  What came of each conclusion: ")
                     + ", ".join(f"{key} {value}" for key, value in sorted(sim["conclusions_by_outcome"].items())))
    lines.append("  " + _say(sim["confidence"]["note"], language))
    lines.append("  " + _say(sim["caveat"], language))
    lines.append("— 历史回测 —" if zh else "— Historical backtests —")
    if not report["backtests"]:
        lines.append("  还没有回测记录。" if zh else "  None on record.")
    for card in report["backtests"]:
        standing = (("可作为独立验证" if zh else "an independent test") if card["independent"] else
                    ("不是独立验证" if zh else "not an independent test"))
        lines.append(f"  {card['card_id']}  {card['window']['start']} → {card['window']['end']}  "
                     f"[{card['sample']}; {standing}]")
        for name, figures in card["figures"].items():
            if figures.get("net_return_pct") is not None:
                lines.append(f"      {ROW_NAMES.get(name, (name, name))[0 if zh else 1]}: "
                             f"{figures['net_return_pct']:+.2f}%, "
                             + ("回撤 " if zh else "drawdown ") + f"{figures['max_drawdown_pct']:.2f}%")
    lines.append("  " + _say(report["note"], language))
    return "\n".join(lines)


def render_card(answer: dict[str, Any], language: str = "zh") -> str:
    zh = language == "zh"
    card = answer["card"]
    lines = [("【回测记录】" if zh else "[Backtest record]") + f"  {card['card_id']}",
             f"  {card['window']['start']} → {card['window']['end']}  "
             + (f"策略版本 {card['version_id']}" if zh else f"strategy version {card['version_id']}"),
             "  " + _say(card["design"]["says"], language)]
    if card["design"].get("selection"):
        lines.append("  " + _say(card["design"]["selection"], language))
    lines.append("  " + (("独立验证：是" if card["design"]["independent"] else "独立验证：否") if zh else
                         f"Independent test: {'yes' if card['design']['independent'] else 'no'}"))
    lines += render_rows(card["rows"], language)
    for limit in card["limits"]:
        lines.append("  · " + _say(limit, language))
    lines.append("  · " + _say(card["model"]["caveat"], language))
    lines.append(("  记录与原始账目核对：" if zh else "  Checked against its own ledgers: ")
                 + (("一致" if zh else "reproduced") if answer["grounded"] else
                    "; ".join(answer["problems"])))
    lines.append(f"  {answer['path']}")
    return "\n".join(lines)


def render_live(live: dict[str, Any], language: str = "zh") -> str:
    """The real account under automatic trading: what is in force, what waits, and every condition."""
    zh = language == "zh"
    lines = ["【实盘账户 · 自动交易】" if zh else "[Real account: automatic trading]"]
    plan, waiting, ended = live.get("plan"), live.get("request"), live.get("ended")
    if plan:
        envelope = plan["envelope"]
        state = live.get("state") or {}
        lines.append(
            f"  方案生效中：{envelope['mode']}，可用资金 ${float(envelope['capital_usd']):,.2f}，"
             f"策略版本 {plan['opening_version']}（{plan['confirmed_by']} 于 {str(plan['confirmed_at'])[:16]} 确认）"
             if zh else
             f"  In force: {envelope['mode']}, ${float(envelope['capital_usd']):,.2f} of capital, "
             f"strategy version {plan['opening_version']} (confirmed by {plan['confirmed_by']} at "
             f"{str(plan['confirmed_at'])[:16]})")
        if state.get("paused"):
            lines.append(("  已暂停：" if zh else "  Paused: ") + str(state.get("reason")))
        if plan.get("managing"):
            lines.append(("  仍在管理旧仓位的策略版本：" if zh else "  Still managing what they bought: ")
                         + ", ".join(plan["managing"]))
    elif waiting:
        lines.append(
            f"  已确认、等待设置生效：方案 {waiting['plan_id']}，有效期至 {str(waiting['expires_at'])[:16]}"
             if zh else
             f"  Confirmed, waiting for its settings: plan {waiting['plan_id']}, stands until "
             f"{str(waiting['expires_at'])[:16]}")
        if waiting.get("last_reason"):
            lines.append(("    还在等：" if zh else "    still waiting on: ") + str(waiting["last_reason"]))
    else:
        lines.append("  没有方案生效。" if zh else "  No plan is in force.")
        if ended:
            lines.append(("  上一个方案已停止：" if zh else "  The last plan ended: ")
                         + str(ended.get("withdrawn_reason")))
    seen = live.get("last_pass")
    if seen:
        lines.append(("  最近一次运行：" if zh else "  Last pass: ") + str(seen.get("at"))[:19]
                     + (f"（失败：{seen['error']}）" if seen.get("error") and zh else
                        f" (failed: {seen['error']})" if seen.get("error") else ""))
    for held in live.get("positions") or []:
        lines.append(f"  {held['symbol']:5s} {held.get('managed_quantity')} "
                     + (f"（策略版本 {held.get('strategy_version')}）" if zh else
                        f"(strategy version {held.get('strategy_version')})"))
    candidate = live.get("candidate") or {}
    lines += ["", "— 在实盘启动模拟中的方案，需要满足 —" if zh else
              "— What starting the simulated plan here would take —"]
    if not candidate.get("available"):
        lines.append("  " + _say(candidate.get("why"), language))
    else:
        for gate in candidate["gates"]:
            lines.append(f"  {'✓' if gate['ok'] else '✗'} " + _say(gate["text"], language))
        for note in candidate.get("advisories") or []:
            lines.append("  ! " + _say(note["text"], language))
        lines.append("  启动、恢复和更换策略需要控制台密码，请在控制台里操作。" if zh else
                      "  Starting, resuming and replacing take the console password: do them "
                      "in the console.")
    looked = (live.get("replacement") or {}).get("last")
    if looked:
        lines.append(("  最近一次候选策略评估：" if zh else "  Last look at a candidate: ")
                     + f"{looked.get('outcome')} ({str(looked.get('at'))[:16]})")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    load_env()
    parser = argparse.ArgumentParser(
        prog="ct-auto",
        description="The automatic-trading plan, the modes, and the simulated account. Nothing "
                    "here can place a real order or change config/. On the real account it can "
                    "read, pause and stop; starting, resuming and replacing take the console "
                    "password and are done in the console.")
    parser.add_argument("--config-dir", default="config")
    parser.add_argument("--lang", choices=("zh", "en"), default="zh")
    parser.add_argument("--json", action="store_true", help="print the full document as JSON")
    commands = parser.add_subparsers(dest="command", required=True)

    def envelope(command):
        command.add_argument("--mode", choices=modes.MODES, default=modes.STEADY)
        command.add_argument("--capital", type=float, default=None,
                             help="dollars automatic trading may deploy (default: the account's equity)")
        command.add_argument("--loss-pause-pct", type=float, default=None,
                             help="loss, as a percent of the capital, at which new positions stop")

    plan_cmd = commands.add_parser("plan", help="what the plan would be for an envelope")
    envelope(plan_cmd)
    plan_cmd.add_argument("--ask-model", action="store_true",
                          help="ask the model for the strategy parameters (spends the subscription)")
    commands.add_parser("status", help="every mode, open or not, and why not")
    commands.add_parser("sim", help="the simulated account: its plan, its record, what it holds")
    start = commands.add_parser("start", help="start the simulated account under a previewed plan")
    envelope(start)
    start.add_argument("--plan-id", required=True,
                       help="the id `ct-auto plan` printed for this envelope: what you read is "
                            "what is started, or nothing is")
    commands.add_parser("live", help="the real account: what is in force, what waits, every condition")
    pause = commands.add_parser("pause", help="stop a plan opening positions (the simulated one, "
                                              "or with --live the real one)")
    pause.add_argument("--reason", required=True)
    pause.add_argument("--live", action="store_true", help="the real account's plan")
    resume = commands.add_parser("resume", help="let the simulated plan open positions again")
    resume.add_argument("--note", required=True)
    stop = commands.add_parser("stop", help="take a plan out of force (the simulated one, or "
                                            "with --live the real one and any start waiting)")
    stop.add_argument("--reason", required=True)
    stop.add_argument("--live", action="store_true", help="the real account's plan")
    fund = commands.add_parser("fund", help="deposit into, or withdraw from, the simulated account")
    fund.add_argument("kind", choices=("deposit", "withdrawal"))
    fund.add_argument("amount", type=float)
    fund.add_argument("--note", default="")
    adjust = commands.add_parser(
        "adjust", help="change the simulated plan's strategy inside its confirmed envelope")
    for flag, kind in (("--max-positions", int), ("--position-pct", float), ("--min-confidence", int),
                       ("--stop-loss-pct", float), ("--take-profit-pct", float),
                       ("--max-holding-days", int), ("--trend-window", int)):
        adjust.add_argument(flag, type=kind, default=None)
    adjust.add_argument("--ask-model", action="store_true")
    adjust.add_argument("--reason", default="")
    tick = commands.add_parser("tick", help="run one pass over the simulated account now")
    tick.add_argument("--new-version", action="store_true",
                      help="if the code running here is not the plan's strategy version, move the "
                           "plan to it (a new version with an empty record) instead of making no pass")
    commands.add_parser("performance", help="the simulated record beside passive, a simple rule, "
                                            "60/40 and cash; backtests listed apart")
    backtest = commands.add_parser(
        "backtest", help="run the plan's comparators and recorded research over history; keeps the record")
    backtest.add_argument("--start", default=None, help="YYYY-MM-DD (default: two years before --end)")
    backtest.add_argument("--end", default=None, help="YYYY-MM-DD (default: the last finished session)")
    commands.add_parser("intraday-check", help="what an intraday mode would need that is missing, "
                                               "measured now (reads today's minute bars)")
    args = parser.parse_args(argv)

    from ..service import Service, ServiceError

    service = Service(args.config_dir)
    who = "cli"
    try:
        if args.command == "plan":
            document = service.auto_plan(mode=args.mode, capital_usd=args.capital,
                                         loss_pause_pct=args.loss_pause_pct, ask_model=args.ask_model)
            text = render_plan(document, args.lang)
        elif args.command == "status":
            document = service.auto_status()
            text = render_status(document, args.lang) + "\n\n" + render_simulation(
                document["simulation"], args.lang)
        elif args.command == "sim":
            document = service.auto_simulation()
            text = render_simulation(document, args.lang)
        elif args.command == "start":
            document = service.auto_start(plan_id=args.plan_id, mode=args.mode, capital_usd=args.capital,
                                          loss_pause_pct=args.loss_pause_pct, identity=who)
            text = render_simulation(document["simulation"], args.lang)
        elif args.command == "live":
            document = service.auto_live()
            text = render_live(document, args.lang)
        elif args.command == "pause" and args.live:
            document = service.auto_pause(reason=args.reason, identity=who, scope="live")
            text = render_live(service.auto_live(), args.lang)
        elif args.command == "stop" and args.live:
            document = service.auto_stop(reason=args.reason, identity=who, scope="live")
            text = render_live(service.auto_live(), args.lang)
        elif args.command == "pause":
            document = service.auto_pause(reason=args.reason, identity=who)
            text = render_simulation(service.auto_simulation(), args.lang)
        elif args.command == "resume":
            document = service.auto_resume(note=args.note, identity=who)
            moved = document.get("loss_line_moved")
            text = render_simulation(service.auto_simulation(), args.lang)
            if moved:
                text += ("\n  亏损暂停线改从当前盈亏起算：" if args.lang == "zh" else
                         "\n  The loss line is now measured from the result as it stands: ") \
                    + f"${moved['from']:+,.2f} → ${moved['to']:+,.2f}"
        elif args.command == "stop":
            document = service.auto_stop(reason=args.reason, identity=who)
            text = render_simulation(service.auto_simulation(), args.lang)
        elif args.command == "fund":
            document = service.auto_fund(amount_usd=args.amount, kind=args.kind, note=args.note,
                                         identity=who)
            text = render_simulation(service.auto_simulation(), args.lang)
        elif args.command == "adjust":
            wanted = {"max_positions": args.max_positions, "position_pct": args.position_pct,
                      "min_confidence": args.min_confidence, "stop_loss_pct": args.stop_loss_pct,
                      "take_profit_pct": args.take_profit_pct, "max_holding_days": args.max_holding_days,
                      "trend_window": args.trend_window}
            document = service.auto_adjust(
                strategy={key: value for key, value in wanted.items() if value is not None},
                ask_model=args.ask_model, reason=args.reason, identity=who)
            text = render_simulation(service.auto_simulation(), args.lang)
        elif args.command == "performance":
            document = service.auto_performance()
            text = render_performance(document, args.lang)
        elif args.command == "backtest":
            document = service.auto_backtest(start=args.start, end=args.end, identity=who)
            text = render_card(document, args.lang)
        elif args.command == "intraday-check":
            document = service.auto_intraday_checks()
            marks = {True: "✓", False: "✗", None: "?"}
            text = "\n".join([("【短线模式检查】" if args.lang == "zh" else "[Intraday checks]"),
                              "  " + _say(document["note"], args.lang),
                              *(f"  {marks[check['ok']]} " + _say(check["text"], args.lang)
                                for check in document["checks"])])
        else:
            # The simulated account's pass only; the real account's is the API's to run. And a
            # pass from a terminal does not move the plan to this terminal's code unless asked:
            # the service may be running a release, and this a checkout that has moved on.
            document = service.auto_tick(real_account=False, rebind=args.new_version)
            text = json.dumps({key: document.get(key) for key in (
                "session", "ran", "skipped", "error", "recorded")}, indent=2, ensure_ascii=False,
                default=str)
    except ServiceError as exc:
        print(f"ct-auto: {exc}")
        return 2
    print(json.dumps(document, indent=2, ensure_ascii=False, default=str) if args.json else text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
