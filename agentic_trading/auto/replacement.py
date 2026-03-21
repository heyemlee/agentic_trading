"""Whether a strategy that has been simulated may take over from the one opening positions.

The plan a person confirmed lets the strategy be improved inside its boundaries without asking
them each time (docs/todo.md, P3.4). That is the one place in this system where something a
model suggested could come to decide real orders with nobody approving it, so what stands in
front of it is not the model's opinion of its own suggestion and not a backtest somebody liked.
It is a list, held here, every line of which has to hold:

* the candidate is a different strategy version from the one in force, in the same mode, and
  needs nothing the confirmed plan did not already carry -- it may be a different strategy, it
  may not be a wider authority;
* it has been run live in the simulated account for 180 valid sessions of its own, and has
  carried a position from its own purchase to its own sale there;
* one experiment, over the sessions since it was fixed, is on record and can be recomputed from
  its own ledgers; all of that window is after the day the candidate was fixed; and no other
  variant of the same strategy was tried on an overlapping window -- so it is a test, not the
  winner of a comparison;
* in that experiment, after costs, it returned more than the strategy in force *given the same
  research, the same money, the same prices and the same costs*, and more than simply buying
  and holding the same funds; and its worst drawdown was no deeper than either's;
* the account is in order at the moment of the change.

**The decisive experiment is run once.** A candidate is put to the performance lines when it
reaches its 180 sessions, on the window up to that day, and what that shows is kept on the
candidate's own record. It is not run again next week on a longer window in the hope of a better
answer: looking until it passes is how a strategy that does nothing comes to look as if it does
something. A candidate that did not pass stays on record as one that did not, with the
experiment that says so, and a different strategy starts its own 180 sessions from nothing.

**Nothing is discarded and nothing is carried.** The version that is replaced goes on managing
what it bought until those positions are gone. The one that replaces it is the candidate exactly
as simulated; if the code under it has changed since, it is not that candidate any more.

Arithmetic over facts it is handed, like ``auto.gates``: no venue, no ledger, no model.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from . import modes
from .gates import ACCOUNT, OWNER, RECORD, Gate

LOG = "auto.replacements"
MAX_LOG = 200

#: How often a candidate is looked at. Weekly, by the plan; the look is cheap until it has its
#: sessions, and decisive exactly once.
EVALUATE_EVERY_DAYS = 7

#: What came of looking at a candidate:
#:
#: ``nothing``     there is no candidate; the simulated account runs the strategy in force.
#: ``ineligible``  there is one, but it is another mode's or needs more than was confirmed.
#: ``waiting``     it has not had its sessions, or its exit, yet.
#: ``rejected``    it had its comparison and did not pass every line. Kept, as that.
#: ``unproven``    it had its comparison and the record of it can no longer be read.
#: ``passed``      every line about the candidate holds; it has not taken over -- nobody chose
#:                 that for this plan, or the account is not in order at the moment.
#: ``replaced``    it opens positions on the real account now.
NOTHING, INELIGIBLE, WAITING, REJECTED, UNPROVEN, PASSED, REPLACED = (
    "nothing", "ineligible", "waiting", "rejected", "unproven", "passed", "replaced")

#: The rows of the decisive experiment a candidate is held against.
CANDIDATE, INCUMBENT, PASSIVE = "strategy", "incumbent", "passive"


def _both(zh: str, en: str) -> tuple[str, str]:
    return zh, en


def _figure(card: dict[str, Any] | None, row: str, key: str) -> float | None:
    found = (((card or {}).get("rows") or {}).get(row) or {}).get("figures") or {}
    value = found.get(key)
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def standing_lines(*, candidate: dict[str, Any] | None, incumbent: dict[str, Any] | None,
                   settings_needed: tuple[str, ...] = ()) -> list[Gate]:
    """Whether there is a candidate at all, and whether it stays inside what was confirmed."""
    if candidate is None or incumbent is None or candidate["version_id"] == incumbent["version_id"]:
        return [Gate("candidate_exists", False, *_both(
            "模拟账户里运行的就是实盘正在用的策略版本，没有候选策略需要评估。",
            "The simulated account is running the strategy version the real account uses; there "
            "is no candidate to evaluate."), RECORD)]
    out = [Gate("candidate_exists", True, *_both(
        f"候选策略 {candidate['version_id']}（模拟中），现用策略 {incumbent['version_id']}（实盘）。",
        f"Candidate {candidate['version_id']} (simulated); in force {incumbent['version_id']} "
        "(real account)."), RECORD)]
    same_mode = candidate.get("mode") == incumbent.get("mode")
    out.append(Gate("same_mode", same_mode, *(
        _both("候选策略与现用策略是同一个模式。", "The candidate is in the same mode as the strategy in force.")
        if same_mode else
        _both(f"候选策略是{modes.TABLE[candidate['mode']].label[0]}模式，现用的是"
              f"{modes.TABLE[incumbent['mode']].label[0]}模式。换模式是你的决定，不属于自动替换。",
              f"The candidate is a {candidate['mode']} strategy and the one in force is "
              f"{incumbent['mode']}. Changing mode is your decision, not a replacement.")), OWNER))
    inside = not settings_needed
    out.append(Gate("inside_confirmed_plan", inside, *(
        _both("候选策略不需要改动任何你确认过的设置。",
              "The candidate needs no setting the plan you confirmed does not already carry.")
        if inside else
        _both(f"候选策略需要改动你确认过的方案之外的设置：{'、'.join(settings_needed)}。"
              "放宽授权只能由你通过新的方案确认，代码不会自己做。",
              "The candidate would need settings outside the plan you confirmed: "
              f"{', '.join(settings_needed)}. Widening authority is yours, through a new plan; "
              "code does not do it.")), OWNER))
    return out


def record_lines(candidate_id: str, record: dict[str, Any]) -> list[Gate]:
    """The candidate's own live-simulated record: enough sessions, and a way out that has worked."""
    need = modes.SIMULATION_DAYS_TO_REPLACE
    counted = int(record.get("counted") or 0)
    verified = bool(record.get("exits_verified"))
    return [
        Gate("candidate_record", counted >= need, *(
            _both(f"候选策略 {candidate_id} 已有 {counted} 个有效模拟交易日（需要 {need} 个）。"
                  "这是运行天数，不是盈利证明。",
                  f"Candidate {candidate_id} has {counted} valid simulated sessions ({need} "
                  "needed). That is days run, not proof of profit.") if counted >= need else
            _both(f"候选策略 {candidate_id} 只有 {counted} 个有效模拟交易日，需要至少 {need} 个（等待样本）。"
                  "在此之前现用策略保持不变。",
                  f"Candidate {candidate_id} has {counted} valid simulated session(s) and needs "
                  f"{need}. Until then the strategy in force stays.")), RECORD),
        Gate("candidate_exit_exercised", verified, *(
            _both("候选策略在模拟账户里完成过“代码买入→代码卖出”的完整流程。",
                  "The candidate has carried a position from its own purchase to its own sale in "
                  "the simulated account.") if verified else
            _both("候选策略在模拟账户里还没有完成过一次“代码买入→代码卖出”的完整流程。",
                  "The candidate has not yet carried one position from its own purchase to its "
                  "own sale in the simulated account.")), RECORD),
    ]


def performance_lines(card: dict[str, Any] | None, *, grounded: bool | None) -> list[Gate]:
    """What the decisive experiment shows, line by line. With no card, every line is a no."""
    if card is None:
        return [Gate("decisive_experiment", False, *_both(
            "候选策略还没有做过决定性的同条件对比（要等它满 180 个有效模拟交易日）。",
            "The candidate has not had its decisive comparison yet; that waits for its 180 valid "
            "simulated sessions."), RECORD)]
    design = card.get("design") or {}
    out: list[Gate] = []
    out.append(Gate("experiment_reproducible", bool(grounded), *(
        _both(f"实验 {card['card_id']} 的每个数字都能从它自己的账目重新算出来。",
              f"Every figure on experiment {card['card_id']} is reproduced from its own ledgers.")
        if grounded else
        _both(f"实验 {card['card_id']} 的数字无法从它自己的账目重新算出来，不能作为证据。",
              f"The figures on experiment {card['card_id']} are not reproduced from its own "
              "ledgers, so it is not evidence.")), RECORD))
    independent = bool(design.get("independent"))
    said = design.get("selection") or design.get("says") or {}
    out.append(Gate("independent_out_of_sample", independent, *(
        _both(f"对比区间（{card['window']['start']} 至 {card['window']['end']}）全部在候选策略定下来之后，"
              "并且没有同一策略的其他版本在重叠区间上试过：这是独立的样本外结果。",
              f"The window ({card['window']['start']} to {card['window']['end']}) is all after the "
              "candidate was fixed, and no other variant of the strategy was tried on an "
              "overlapping one: an independent out-of-sample result.") if independent else
        _both("这不是独立的样本外结果。" + str(said.get("zh") or ""),
              "This is not an independent out-of-sample result. " + str(said.get("en") or ""))), RECORD))

    mine = _figure(card, CANDIDATE, "net_return_pct")
    for row, code, zh, en in (
            (INCUMBENT, "beats_incumbent_after_costs", "同条件下的现用策略", "the strategy in force under the same conditions"),
            (PASSIVE, "beats_passive_after_costs", "同条件的被动持有", "buying and holding the same funds")):
        theirs = _figure(card, row, "net_return_pct")
        if mine is None or theirs is None:
            out.append(Gate(code, False, *_both(
                f"候选策略或{zh}在这段区间里没有可比的结果（例如区间内没有实时记录的研究结论）。没有结果不等于持平。",
                f"The candidate or {en} has no comparable result over this window -- no research "
                "recorded live inside it, for one. No result is not a tie."), RECORD))
            continue
        better = mine > theirs
        out.append(Gate(code, better, *_both(
            f"扣成本后，候选策略 {mine:+.2f}%，{zh} {theirs:+.2f}%："
            + ("候选更高。" if better else "候选没有更高，保留现用策略。"),
            f"After costs the candidate returned {mine:+.2f}% and {en} {theirs:+.2f}%: "
            + ("the candidate is higher." if better else "the candidate is not higher, so the "
               "strategy in force stays.")), RECORD))
    depth = _figure(card, CANDIDATE, "max_drawdown_pct")
    others = {row: _figure(card, row, "max_drawdown_pct") for row in (INCUMBENT, PASSIVE)}
    if depth is None or any(value is None for value in others.values()):
        out.append(Gate("drawdown_not_worse", False, *_both(
            "最大回撤无法比较：至少有一方在这段区间里没有结果。",
            "The drawdowns cannot be compared: at least one side has no result over this window."),
            RECORD))
    else:
        # Drawdowns are negative numbers; "no deeper" is "not below".
        shallow = all(depth >= float(value) for value in others.values())
        out.append(Gate("drawdown_not_worse", shallow, *_both(
            f"最大回撤：候选 {depth:.2f}%，现用策略 {others[INCUMBENT]:.2f}%，被动持有 {others[PASSIVE]:.2f}%："
            + ("候选不比两者更深。" if shallow else "候选更深，保留现用策略。"),
            f"Worst drawdown: candidate {depth:.2f}%, in force {others[INCUMBENT]:.2f}%, passive "
            f"{others[PASSIVE]:.2f}%: " + ("the candidate's is no deeper than either."
                                           if shallow else "the candidate's is deeper, so the "
                                           "strategy in force stays.")), RECORD))
    return out


def account_lines(account: dict[str, Any], *, paused: str | None) -> list[Gate]:
    """The real account at the moment of the change: it has to be in order, and running."""
    out = [Gate("plan_running", not paused, *(
        _both("实盘方案正在运行。", "The real plan is running.") if not paused else
        _both(f"实盘方案处于暂停状态（{paused}），暂停期间不更换策略。",
              f"The real plan is paused ({paused}); a strategy is not changed while it is.")), OWNER)]
    for code, ok, zh, en in (
            ("real_money_on", bool(account.get("real_money_on")),
             "实盘执行开关已关闭。", "Real-money execution is switched off."),
            ("not_halted", not account.get("halted"), "交易总开关处于暂停状态。", "Trading is halted."),
            ("autonomy_intact", not account.get("autonomy_withdrawn"),
             "自动权限已被收回，需要你检查后恢复。", "Autonomous action was withdrawn and waits for you."),
            ("orders_settled", not account.get("unresolved_orders"),
             "有订单结果不明或尚未对账完成。", "An order is unresolved or not yet reconciled.")):
        out.append(Gate(code, ok, *(_both("正常。", "In order.") if ok else _both(zh, en)), ACCOUNT))
    return out


def unreadable_lines(card_id: str | None) -> list[Gate]:
    """The line for a comparison that was made and whose record cannot be read back."""
    return [Gate("experiment_reproducible", False, *_both(
        f"候选策略的对比实验（{card_id or '未知'}）的记录读不出来了。没有可以核对的记录，就不能据此更换策略；"
        "也不会重做一次来代替它。",
        f"The record of the candidate's comparison ({card_id or 'unknown'}) can no longer be read. "
        "With nothing to check, nothing is changed on the strength of it, and it is not run "
        "again in its place."), RECORD)]


def decision(lines: list[Gate]) -> bool:
    """Whether the candidate may take over: every line holds."""
    return all(line.ok for line in lines)


def outcome_of(*, standing: list[Gate], record: list[Gate], performance: list[Gate],
               account: list[Gate], compared: bool, unreadable: bool, wanted: bool) -> str:
    """What a look comes to, from the lines. ``wanted`` is whether anyone asked for the change:
    the plan's own choice, or a person with the password now."""
    if not decision(standing):
        return INELIGIBLE
    if not decision(record):
        return WAITING
    if unreadable:
        return UNPROVEN
    if not compared:
        return WAITING
    if not decision(performance):
        return REJECTED
    return REPLACED if wanted and decision(account) else PASSED


# ---------------------------------------------------------------- the record of having looked
def history(db) -> list[dict[str, Any]]:
    return list((db.read_meta(LOG, {}) or {}).get("evaluations") or [])


#: How long after a look that could not be finished -- the history unreadable, the ledger stale
#: -- before the next attempt. Without it a failing look would be made again every minute.
RETRY_AFTER_HOURS = 6


def _since(stamp: Any, now: datetime) -> timedelta | None:
    try:
        return now - datetime.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        return None


def due(db, *, now: datetime | None = None) -> bool:
    """Whether a week has passed since a candidate was last looked at, and a look may be tried."""
    now = now or datetime.now(UTC)
    record = db.read_meta(LOG, {}) or {}
    looked = _since(record.get("last_evaluated_at"), now) if record.get("last_evaluated_at") else None
    tried = _since(record.get("last_attempt_at"), now) if record.get("last_attempt_at") else None
    if looked is not None and looked < timedelta(days=EVALUATE_EVERY_DAYS):
        return False
    return tried is None or tried >= timedelta(hours=RETRY_AFTER_HOURS)


def attempting(db, *, now: datetime | None = None) -> None:
    """Note that a look is being started, so one that fails is not started again at once."""
    stamp = (now or datetime.now(UTC)).isoformat()
    with db.transaction() as conn:
        db.write_meta(LOG, {**(db.read_meta(LOG, {}) or {}), "last_attempt_at": stamp}, conn)


def note(db, entry: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """Keep what a look at a candidate found. Every look is kept, the ones that came to nothing too."""
    stamp = (now or datetime.now(UTC)).isoformat()
    with db.transaction() as conn:
        record = db.read_meta(LOG, {}) or {}
        kept = [*(record.get("evaluations") or []), {**entry, "at": stamp}][-MAX_LOG:]
        db.write_meta(LOG, {**record, "evaluations": kept, "last_evaluated_at": stamp}, conn)
    return {**entry, "at": stamp}
