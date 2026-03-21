"""What stands between a plan and real money, as a list a person can read (docs/todo.md, P3.2).

Starting a plan on the real account is the one thing in automatic trading that nothing is allowed
to make easier. So the conditions are not scattered through the code that starts it: they are
here, each one a line with a yes or a no and the words for why, and the start is refused unless
every line says yes. There is no argument to this module that skips a line, and no caller that
is handed one -- "not yet" is answered by going on simulating, which costs nothing.

**The record is the exact version's own.** The first two lines ask about the strategy version
that would open positions on the real account: these parameters, on this code, with these models
and this mandate (``auto.store.version_id``). Days simulated by any other version -- an earlier
set of parameters, the same parameters before a release changed the trading logic -- are that
version's and are not counted here.

**Enough days is not a good strategy.** A session counts when the account was run through it
with nothing wrong (``auto.store.session_valid``); a day on which nothing was bought counts as
much as one on which something was. So thirty of them say the system ran as built for thirty
sessions, and say nothing about whether the strategy makes money. The line that reports them
says so in its own words, every time, because the number will be read by someone deciding
whether to put money behind it.

Nothing here reads a venue, a ledger or a database. It is arithmetic over facts it is handed, so
the same facts always give the same list and a test can hand it any world.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import modes

#: Whose it is to clear a line: time, the person, the account being in order, the plan's own
#: settings coming into force, work that has not been built, or a fact nobody has established.
RECORD, OWNER, ACCOUNT, SETTINGS, BUILD, DATA = "record", "owner", "account", "settings", "build", "data"

#: The one line that does not stop a person confirming a plan: it is what confirming starts.
#: Everything else has to hold before anything is submitted, and all of it, this included, has to
#: hold at the moment a plan is put in force.
SETTINGS_PENDING = "settings_pending"

#: Questions that are the owner's to answer and that a plan puts to them as one of its own
#: settings, named for the setting that carries the answer. While that setting is among the
#: plan's changes still to land, the question is being asked, not ignored: it is counted with
#: the settings, and stands again as its own line if they do not land.
ANSWERED_BY_SETTING = {"swing_policy_undecided": ".autonomy.open.modes"}


@dataclass(frozen=True)
class Gate:
    """One condition, whether it holds, and why -- in the two languages the console speaks."""

    code: str
    ok: bool
    zh: str
    en: str
    waits_on: str

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "ok": self.ok, "waits_on": self.waits_on,
                "text": {"zh": self.zh, "en": self.en}}


def _both(zh: str, en: str) -> tuple[str, str]:
    return zh, en


def _record(version_id: str, record: dict[str, Any]) -> list[Gate]:
    need = modes.SIMULATION_DAYS_TO_OPEN
    counted = int(record.get("counted") or 0)
    other, other_days = record.get("simulated_version"), int(record.get("simulated_version_days") or 0)
    not_proof = _both("这只说明系统按规则连续运行了这么多个交易日，不代表策略已被证明能盈利。",
                      "That says the system ran as built for that many sessions. It is not "
                      "evidence that the strategy makes money.")
    if not record.get("exists"):
        zh, en = _both(f"还没有启动模拟账户。实盘之前，这个策略版本需要先有至少 {need} 个有效模拟交易日。",
                       "The simulated account has not been started. This strategy version needs "
                       f"at least {need} valid simulated sessions before real money.")
    elif counted >= need:
        zh, en = _both(f"策略版本 {version_id} 已有 {counted} 个有效模拟交易日（需要 {need} 个）。" + not_proof[0],
                       f"Strategy version {version_id} has {counted} valid simulated sessions "
                       f"({need} needed). " + not_proof[1])
    elif other and other != version_id:
        zh, en = _both(
            f"模拟账户里运行的是另一个策略版本（{other}，{other_days} 个有效交易日）。要在实盘启动的版本 "
            f"{version_id} 自己只有 {counted} 个，需要 {need} 个；别的版本的记录不能算在它名下。",
            f"The simulated account is running a different strategy version ({other}, "
            f"{other_days} valid sessions). The version that would start here, {version_id}, has "
            f"{counted} of its own and needs {need}; another version's record is not its record.")
    else:
        zh, en = _both(
            f"策略版本 {version_id} 只有 {counted} 个有效模拟交易日，需要至少 {need} 个（等待样本）。"
            "没有交易的日子同样计入，所以不需要、也不会为了凑天数而交易。",
            f"Strategy version {version_id} has {counted} valid simulated session(s) and needs "
            f"{need}. A day with no trade counts the same, so nothing is traded to be counted.")
    verified = bool(record.get("exits_verified"))
    return [
        Gate("simulation_record", counted >= need, zh, en, RECORD),
        Gate("exit_exercised", verified,
             *(_both("这个策略版本在模拟账户里完成过“代码买入→代码卖出”的完整流程。",
                     "This strategy version has carried a position from its own purchase to its "
                     "own sale in the simulated account.") if verified else
               _both("这个策略版本在模拟账户里还没有完成过一次“代码买入→代码卖出”的完整流程。"
                     "不会为了验证而强行交易：等它自己的止损、到期或研究卖出信号出现。",
                     "This strategy version has not yet carried one position from its own "
                     "purchase to its own sale in the simulated account. Nothing is traded to "
                     "prove it: it waits for a stop, an expiry or research saying sell.")), RECORD),
    ]


def _account(account: dict[str, Any]) -> list[Gate]:
    out: list[Gate] = []
    on = bool(account.get("real_money_on"))
    out.append(Gate("real_money_on", on, *(
        _both("这个账户的实盘执行开关是打开的。", "Real-money execution is switched on for this account.")
        if on else
        _both("这个账户的实盘执行开关（micro_live_execution.enabled）是关闭的。这里不会替你打开它。",
              "Real-money execution (micro_live_execution.enabled) is switched off for this "
              "account. Nothing here turns it on.")), OWNER))
    halted = account.get("halted")
    out.append(Gate("not_halted", not halted, *(
        _both("交易总开关没有暂停。", "Trading is not halted.") if not halted else
        _both(f"交易总开关处于暂停状态：{halted}", f"Trading is halted: {halted}")), OWNER))
    withdrawn = account.get("autonomy_withdrawn")
    out.append(Gate("autonomy_intact", not withdrawn, *(
        _both("自动权限没有被收回。", "Autonomous action has not been withdrawn.") if not withdrawn else
        _both(f"自动权限已被收回，需要你检查后恢复：{withdrawn}",
              f"Autonomous action was withdrawn and needs you to look and restore it: {withdrawn}")),
        OWNER))
    orders = list(account.get("unresolved_orders") or [])
    out.append(Gate("orders_settled", not orders, *(
        _both("没有结果不明或尚未对账的订单。", "No order is unresolved or waiting to be reconciled.")
        if not orders else
        _both(f"有 {len(orders)} 笔订单结果不明或尚未对账完成，需要先了结。",
              f"{len(orders)} order(s) are unresolved or not yet reconciled; they are settled first.")),
        ACCOUNT))
    book = account.get("book") or {}
    out.append(Gate("book_confirmed", bool(book.get("confirmed")), *(
        _both(f"Agentic 账本已与券商核对一致（账本日期 {book.get('as_of')}）。",
              f"The Agentic ledger agrees with the venue (dated {book.get('as_of')}).")
        if book.get("confirmed") else
        _both(f"Agentic 账本与券商的核对没有通过或没有记录：{book.get('error') or '没有核对记录'}",
              "The Agentic ledger has not been confirmed against the venue: "
              f"{book.get('error') or 'no check is on record'}")), ACCOUNT))
    unexplained = list(account.get("unexplained") or [])
    out.append(Gate("positions_explained", not unexplained, *(
        _both("账户里的每个仓位都能由成交记录解释。", "Every position in the account is explained by the fills on record.")
        if not unexplained else
        _both(f"这些仓位与成交记录对不上：{'、'.join(unexplained)}。对不上时代码无法确认哪些是它自己买的。",
              f"These positions do not match the fills on record: {', '.join(unexplained)}. "
              "Until they do, code cannot tell which shares are its own.")), ACCOUNT))
    possible = bool(account.get("any_order"))
    why = account.get("why_no_order") or {}
    out.append(Gate("order_possible", possible, *(
        _both("按这个方案，账户里至少有一笔订单能同时满足全部限制。",
              "Under this plan at least one order satisfies every limit at once.") if possible else
        _both("按这个方案，没有一笔订单能同时满足全部限制。" + str(why.get("zh") or ""),
              "Under this plan no order satisfies every limit at once. " + str(why.get("en") or ""))),
        ACCOUNT))
    return out


def evaluate(profiles: Any, *, mode: str, version_id: str, record: dict[str, Any],
             account: dict[str, Any], opened: tuple[str, ...] = (),
             settings_waiting: tuple[str, ...] = (), cooling_hours: int = 24) -> list[Gate]:
    """Every condition for this mode and this strategy version on the real account, in order.

    ``record`` is the simulated account's record *of this version*; ``account`` is the real
    account as it stands; ``opened`` the modes a person has already started there;
    ``settings_waiting`` the plan's settings that are not in force yet.
    """
    spec = modes.TABLE[mode]
    out = _record(version_id, record)

    waiting_on = [earlier for earlier in modes.earlier_modes(mode) if earlier not in opened]
    if waiting_on:
        names_zh = "、".join(modes.TABLE[m].label[0] for m in waiting_on)
        names_en = ", ".join(modes.TABLE[m].label[1] for m in waiting_on)
        out.append(Gate("earlier_mode_opened", False,
                        f"模式按顺序开放：需要先在实盘开放过{names_zh}模式，才能开放{spec.label[0]}。",
                        f"Modes open in order: {names_en} has to have been started on the real "
                        f"account before {spec.label[1]} may be.", RECORD))
    # What the risk profile and the state of the build say about this mode. The two record lines
    # are this module's own, about the exact version; everything else is taken as it is given.
    for blocker in modes.blockers(profiles, mode, simulated_days=modes.SIMULATION_DAYS_TO_OPEN,
                                  exits_verified=True):
        answer = ANSWERED_BY_SETTING.get(blocker.code)
        if answer and any(str(path).endswith(answer) for path in settings_waiting):
            # The plan itself carries the owner's answer to this one, as a setting they confirm
            # with it. Until that setting is in force it is the settings line that says no.
            continue
        out.append(Gate(blocker.code, False, blocker.zh, blocker.en, blocker.waits_on))

    out.extend(_account(account))

    waiting = list(settings_waiting)
    out.append(Gate(SETTINGS_PENDING, not waiting, *(
        _both("方案需要的设置都已经生效。", "Every setting the plan needs is in force.") if not waiting else
        _both(f"方案需要的 {len(waiting)} 项设置还没有生效：{'、'.join(waiting)}。它们走控制台的确认流程："
              f"放宽限制的需要控制台密码，并等待 {cooling_hours} 小时后生效，期间可以撤回。",
              f"{len(waiting)} setting(s) the plan needs are not in force: {', '.join(waiting)}. "
              "They go through the console's confirmation: one that loosens a limit takes the "
              f"console password and {cooling_hours} hours, and can be withdrawn until then.")),
        SETTINGS))
    return out


def failing(gates: list[Gate], *, ignore: tuple[str, ...] = ()) -> list[Gate]:
    return [gate for gate in gates if not gate.ok and gate.code not in ignore]


def is_open(gates: list[Gate]) -> bool:
    """Whether a plan may be put in force on the real account at this moment: every line holds."""
    return not failing(gates)


def may_confirm(gates: list[Gate]) -> bool:
    """Whether a person may confirm the plan now: everything holds except settings still to land."""
    return not failing(gates, ignore=(SETTINGS_PENDING,))


def refusal(gates: list[Gate], *, ignore: tuple[str, ...] = ()) -> str:
    """Why not, in one sentence a log and an error can carry."""
    return "; ".join(gate.en for gate in failing(gates, ignore=ignore))
