"""A strategy takes over from the one in force only after it has been proved (docs/todo.md, P3.4).

The real account runs strategy A. The simulated account is moved to a candidate, B. These tests
hold what stands between B and real orders, and that none of it is B's own opinion of itself:

* nothing is compared until B has 180 valid simulated sessions of its own and has exited once;
* the comparison is one experiment, B and A over the same recorded research, the same money,
  the same prices and the same costs, with buy-and-hold beside them -- kept with its ledgers,
  recomputable, and made exactly once;
* B replaces A only if that experiment is an independent out-of-sample result in which B, after
  costs, did better than A and than buy-and-hold and fell no further than either;
* a B that does not pass is kept on record as one that did not, and A stays;
* passing does not replace by itself unless the person chose that for the plan; and A goes on
  managing what it bought either way.

The market is the test's own straight lines (tests/auto_market.py): VTI rises, VXUS falls. No
figure here says anything about a real strategy.
"""

from datetime import UTC, date, datetime, timedelta

import pytest

from agentic_trading import events
from agentic_trading.auto import bars as bar_source
from agentic_trading.auto import experiments, modes, replacement, runner, store
from agentic_trading.auto.gates import Gate
from agentic_trading.service import Service, ServiceError
from agentic_trading.service.shapes import MarketStatus
from tests.auto_market import scripted_history, today
from tests.test_auto_live import PASSWORD, Real, real  # noqa: F401 - the real account's desk

MICRO = "micro_live"
FIXED_DAYS_AGO = 300


def _days_ago(days):
    """A date that many days before the market's own today."""
    return (date.fromisoformat(today()) - timedelta(days=days)).isoformat()


def _fixed_on():
    return _days_ago(FIXED_DAYS_AGO)


def _candidate(real, monkeypatch, *, sessions=modes.SIMULATION_DAYS_TO_REPLACE, exits=True,  # noqa: F811
               **changes):
    """Move the simulated account to a candidate fixed 300 days ago, and give it its record.

    The date it was fixed is put in the past the only honest way a test can: by saying so when
    it is fixed. Its sessions are then all after that day, as a real candidate's would be.
    """
    fixed = _fixed_on()
    with monkeypatch.context() as patch:
        patch.setattr(store, "_today", lambda: fixed)
        adjusted = real.service.auto_adjust(strategy=changes, reason="a candidate")
    version = adjusted["opening_version"]
    assert adjusted["changed"] is True
    assert store.version(real.sim().state_db(), version)["data_as_of"] == fixed
    real.record(version, sessions)
    if exits:
        # One position from the candidate's own purchase to its own sale, in the simulated
        # account only: the real account's pass is not run for this. A different fund from the
        # one the first strategy was exercised on, which was bought and sold earlier "today".
        sim = real.sim()
        real.research("BND")
        runner.run(sim, research_from=real.runs)
        real.move("BND", 0.88)
        runner.run(sim, research_from=real.runs)
        real.move("BND", 1 / 0.88)
        real.forget_research()
        assert runner.exits_verified(sim, sim.profiles(), version)
    return version


def _research_in_the_window(real, *, days_ago=150, ticker="VTI"):  # noqa: F811
    """A conclusion recorded live while the candidate was being simulated.

    A hundred and fifty days back by default: far enough that a strategy holding for ninety days
    has sold by the end of the window, and one holding for a hundred and eighty has not.
    """
    return real.research(ticker, confidence=90, age_minutes=days_ago * 1440)


@pytest.fixture
def running(tmp_path, monkeypatch, offline_market_reads):
    """Strategy A in force on the real account, with history a comparison can be run over.

    The account is let trade several times a day: these tests put a strategy's validation, its
    time in force and a second strategy's validation into one afternoon.
    """
    real = Real(tmp_path, monkeypatch, openings_per_day=6)  # noqa: F811
    active = real.in_force()
    scripted_history(monkeypatch)
    return real, active["opening_version"]


def _lines(answer):
    return {line["code"]: line for line in answer["lines"]}


# ---------------------------------------------------------------- the lines, on their own
def _card(*, candidate=(5.0, -2.0), incumbent=(3.0, -4.0), passive=(1.0, -6.0), independent=True):
    def row(pair):
        return {"figures": None if pair is None else {"net_return_pct": pair[0], "max_drawdown_pct": pair[1]}}

    return {"card_id": "xp-test", "window": {"start": "2026-01-02", "end": "2026-09-30"},
            "design": {"independent": independent, "sample": "out_of_sample" if independent else "mixed",
                       "says": {"zh": "区间跨过了策略定下来的日期", "en": "the window straddles the day"}},
            "rows": {"strategy": row(candidate), "incumbent": row(incumbent), "passive": row(passive)}}


def _failing(card, grounded=True):
    return [line.code for line in replacement.performance_lines(card, grounded=grounded) if not line.ok]


def test_every_performance_line_has_to_hold():
    assert _failing(_card()) == []
    assert _failing(_card(candidate=(3.0, -2.0))) == ["beats_incumbent_after_costs"]       # a tie is not better
    assert _failing(_card(candidate=(2.0, -2.0), incumbent=(3.0, -4.0), passive=(1.0, -6.0))) == [
        "beats_incumbent_after_costs"]
    assert _failing(_card(candidate=(5.0, -2.0), passive=(6.0, -6.0))) == ["beats_passive_after_costs"]
    # More return bought with a deeper fall is not an improvement: than either of them.
    assert _failing(_card(candidate=(9.0, -5.0))) == ["drawdown_not_worse"]
    assert _failing(_card(candidate=(9.0, -5.0), incumbent=(3.0, -7.0), passive=(1.0, -4.0))) == [
        "drawdown_not_worse"]
    assert _failing(_card(candidate=(5.0, -4.0))) == [], "as deep as the shallower is not deeper"
    assert _failing(_card(independent=False)) == ["independent_out_of_sample"]
    assert _failing(_card(), grounded=False) == ["experiment_reproducible"]


def test_a_side_with_no_result_is_not_a_tie_and_not_a_win():
    failing = _failing(_card(candidate=None))
    assert failing == ["beats_incumbent_after_costs", "beats_passive_after_costs", "drawdown_not_worse"]
    lines = replacement.performance_lines(_card(incumbent=None), grounded=True)
    said = next(line for line in lines if line.code == "beats_incumbent_after_costs")
    assert said.ok is False and "没有结果不等于持平" in said.zh and "No result is not a tie" in said.en


def test_with_no_comparison_there_is_one_line_and_it_is_a_no():
    lines = replacement.performance_lines(None, grounded=None)
    assert [(line.code, line.ok) for line in lines] == [("decisive_experiment", False)]
    assert "180" in lines[0].zh


def test_what_a_look_comes_to_follows_from_the_lines_alone():
    yes = [Gate("x", True, "", "", "record")]
    no = [Gate("x", False, "", "", "record")]
    common = {"standing": yes, "record": yes, "performance": yes, "account": yes,
              "compared": True, "unreadable": False, "wanted": True}
    assert replacement.outcome_of(**common) == replacement.REPLACED
    assert replacement.outcome_of(**{**common, "wanted": False}) == replacement.PASSED
    assert replacement.outcome_of(**{**common, "account": no}) == replacement.PASSED
    assert replacement.outcome_of(**{**common, "performance": no}) == replacement.REJECTED
    assert replacement.outcome_of(**{**common, "record": no}) == replacement.WAITING
    assert replacement.outcome_of(**{**common, "compared": False}) == replacement.WAITING
    assert replacement.outcome_of(**{**common, "unreadable": True}) == replacement.UNPROVEN
    assert replacement.outcome_of(**{**common, "standing": no}) == replacement.INELIGIBLE
    # Wanting it is never enough by itself.
    for missing in ("standing", "record", "performance", "account"):
        assert replacement.outcome_of(**{**common, missing: no}) != replacement.REPLACED


# ---------------------------------------------------------------- before there is anything to prove
def test_with_no_plan_in_force_there_is_nothing_to_replace(real):  # noqa: F811
    real.simulate()
    answer = real.service.auto_evaluate_candidate()
    assert answer["outcome"] == replacement.NOTHING and "no plan is in force" in answer["reason"]
    with pytest.raises(ServiceError, match="no plan is in force"):
        real.service.auto_replacement_setting(on=True, confirmed=True)


def test_the_strategy_in_force_is_not_a_candidate_against_itself(running):
    real, incumbent = running  # noqa: F811
    answer = real.service.auto_evaluate_candidate()
    assert answer["outcome"] == replacement.NOTHING
    assert _lines(answer)["candidate_exists"]["ok"] is False
    assert replacement.history(real.db) == [], "a look that found no candidate is not an evaluation"
    assert store.active_plan(real.db)["opening_version"] == incumbent


def test_a_candidate_short_of_its_sessions_waits_and_nothing_is_compared(running, monkeypatch):
    real, incumbent = running  # noqa: F811
    candidate = _candidate(real, monkeypatch, sessions=179, max_holding_days=180)

    def must_not_be_read(*_a, **_k):
        raise AssertionError("no history is read for a candidate that has not had its sessions")

    monkeypatch.setattr(bar_source, "read", must_not_be_read)
    answer = real.service.auto_evaluate_candidate(identity="owner")
    assert answer["outcome"] == replacement.WAITING
    lines = _lines(answer)
    assert lines["candidate_record"]["ok"] is False and "179" in lines["candidate_record"]["text"]["zh"]
    assert "等待样本" in lines["candidate_record"]["text"]["zh"]
    assert lines["decisive_experiment"]["ok"] is False and answer["card_id"] is None
    assert experiments.registered(real.sim().state_db()) == []
    assert store.active_plan(real.db)["opening_version"] == incumbent
    # Asking for it with the password changes none of that.
    forced = real.service.auto_evaluate_candidate(identity="owner", apply=True, confirmed=True)
    assert forced["outcome"] == replacement.WAITING
    assert store.active_plan(real.db)["opening_version"] == incumbent
    assert store.version(real.sim().state_db(), candidate)["validation"]["status"] != store.VALIDATED


def test_sessions_without_an_exit_are_not_enough(running, monkeypatch):
    real, incumbent = running  # noqa: F811
    _candidate(real, monkeypatch, exits=False, max_holding_days=180)
    answer = real.service.auto_evaluate_candidate()
    assert answer["outcome"] == replacement.WAITING
    assert _lines(answer)["candidate_record"]["ok"] is True
    assert _lines(answer)["candidate_exit_exercised"]["ok"] is False
    assert store.active_plan(real.db)["opening_version"] == incumbent


# ---------------------------------------------------------------- proved, and what happens then
def test_a_candidate_that_proves_better_is_ready_and_waits_for_the_person(running, monkeypatch):
    real, incumbent = running  # noqa: F811
    # Holds twice as long. On a fund that only rises that is more of the rise, for the same costs.
    candidate = _candidate(real, monkeypatch, max_holding_days=180)
    _research_in_the_window(real)
    answer = real.service.auto_evaluate_candidate(identity="owner")
    lines = _lines(answer)
    assert [(code, line["text"]["en"]) for code, line in lines.items() if not line["ok"]] == []
    assert answer["outcome"] == replacement.PASSED and answer["automatic"] is False
    assert (answer["candidate"], answer["incumbent"]) == (candidate, incumbent)
    assert "更高" in lines["beats_incumbent_after_costs"]["text"]["zh"]
    assert "这是运行天数，不是盈利证明" in lines["candidate_record"]["text"]["zh"]
    # Nothing changed on the real account: passing is not replacing.
    assert store.active_plan(real.db)["opening_version"] == incumbent

    # The experiment is one card, the candidate's, with the strategy in force as a row on it.
    sim_db = real.sim().state_db()
    registered = experiments.registered(sim_db)
    assert [(row["kind"], row["version_id"]) for row in registered] == [("replacement", candidate)]
    kept = store.version(sim_db, candidate)["validation"]["replacement"]
    assert kept["card_id"] == answer["card_id"] and kept["against"] == incumbent
    assert experiments.verify(kept["path"]) == [], "every figure is reproduced from its ledgers"
    import json
    from pathlib import Path

    card = json.loads((Path(kept["path"]) / "card.json").read_text())
    assert card["design"]["independent"] is True and card["design"]["sample"] == "out_of_sample"
    assert card["design"]["designed_on"] == _fixed_on() and card["window"]["start"] > _fixed_on()
    assert set(card["rows"]) >= {"strategy", "incumbent", "passive", "simple_rule", "benchmark", "cash"}
    assert card["conditions"]["incumbent"] == incumbent and card["conditions"]["replayed_conclusions"] == 1
    mine, theirs = card["rows"]["strategy"]["figures"], card["rows"]["incumbent"]["figures"]
    assert mine["net_return_pct"] > theirs["net_return_pct"] > card["rows"]["passive"]["figures"]["net_return_pct"]
    assert mine["costs_usd"] > 0, "after costs means the costs are in it"
    assert store.version(sim_db, candidate)["validation"]["status"] == store.VALIDATED

    # It is something to decide, where a person will see it, and not an alarm.
    items = [item for item in real.service.attention()["items"] if item["kind"] == "auto_candidate_ready"]
    assert len(items) == 1 and items[0]["severity"] == "info" and items[0]["requires_person"] is False
    assert "是否更换由你确认" in items[0]["detail_i18n"]["zh"]
    assert real.service.auto_live()["replacement"]["last"]["outcome"] == replacement.PASSED


def test_the_person_can_make_the_change_with_the_password_and_the_old_strategy_keeps_its_positions(
        running, monkeypatch):
    real, incumbent = running  # noqa: F811
    # The strategy in force holds something it bought.
    real.research("VXUS", confidence=90)
    real.service.auto_tick()
    real.service.autonomous_reductions()
    real.forget_research()
    assert real.held("VXUS") > 0
    candidate = _candidate(real, monkeypatch, max_holding_days=180)
    _research_in_the_window(real)
    with pytest.raises(ServiceError, match="needs the console password"):
        real.service.auto_evaluate_candidate(identity="owner", apply=True)
    answer = real.service.auto_evaluate_candidate(identity="owner", apply=True, confirmed=True)
    assert answer["outcome"] == replacement.REPLACED and answer["managing"] == [incumbent]
    active = store.active_plan(real.db)
    assert active["opening_version"] == candidate and active["managing"] == [incumbent]
    assert active["strategy"]["max_holding_days"] == 180
    # The same plan, the same envelope, the same confirmation: only who opens has changed.
    assert active["plan_id"] and active["confirmed_by"] == "owner" and active["scope"] == "live"
    record = store.version(real.db, candidate)
    assert record["validation"]["status"] == store.VALIDATED and record["validation"]["replaced"] == incumbent
    assert record["validation"]["simulated_days"] == 180 and record["data_as_of"] == _fixed_on()
    assert record["parent"] == incumbent
    entry = events.query(real.db, kinds=("auto_strategy_replaced",))["events"][0]
    assert entry["actor"] == "owner" and entry["detail"]["automatic"] is False
    told = [e for e in real.service.collect_notifications() if e.key.startswith("auto_replaced:")]
    assert len(told) == 1 and told[0].urgent is True

    # What the old strategy bought is still its own, on its own terms.
    view = real.service.auto_live()["positions"][0]
    assert view["symbol"] == "VXUS" and view["strategy_version"] == incumbent
    real.move("VXUS", 0.85)
    stopped = real.service.auto_tick()["live"]["pass"]["exits"][0]
    assert stopped["strategy_version"] == incumbent and stopped["exit"]["reason"] == "stop_loss"
    # And there is no candidate left: the simulated account runs what the real one now does.
    assert real.service.auto_evaluate_candidate()["outcome"] == replacement.NOTHING


def test_it_takes_over_by_itself_only_under_a_plan_set_to_and_never_mid_session(running, monkeypatch):
    real, incumbent = running  # noqa: F811
    with pytest.raises(ServiceError, match="needs the console password"):
        real.service.auto_replacement_setting(on=True, identity="owner")
    assert real.service.auto_replacement_setting(on=True, identity="owner", confirmed=True) == {
        "auto_replace": True, "plan_id": store.active_plan(real.db)["plan_id"]}
    assert real.service.auto_live()["replacement"]["automatic"] is True
    candidate = _candidate(real, monkeypatch, max_holding_days=180)
    _research_in_the_window(real)

    # During the session the weekly look is not made at all.
    assert "evaluation" not in real.service.auto_tick()["live"]
    assert store.active_plan(real.db)["opening_version"] == incumbent
    # After the close it is, once, and the candidate takes over.
    monkeypatch.setattr(Service, "market_status", lambda self: MarketStatus(is_open=False))
    looked = real.service.auto_tick()["live"]["evaluation"]
    assert looked["outcome"] == replacement.REPLACED and looked["automatic"] is True
    assert store.active_plan(real.db)["opening_version"] == candidate
    entry = events.query(real.db, kinds=("auto_strategy_replaced",))["events"][0]
    assert entry["actor"] == "auto:replacement" and entry["detail"]["automatic"] is True
    # The next pass does not look again: a week has not passed.
    assert "evaluation" not in real.service.auto_tick()["live"]
    # Turning it off takes no password.
    assert real.service.auto_replacement_setting(on=False)["auto_replace"] is False


# ---------------------------------------------------------------- not proved
def test_a_candidate_that_does_worse_is_rejected_kept_and_never_tried_again(running, monkeypatch):
    real, incumbent = running  # noqa: F811
    real.service.auto_replacement_setting(on=True, identity="owner", confirmed=True)
    # Sells after a month. On a fund that only rises that is less of the rise.
    candidate = _candidate(real, monkeypatch, max_holding_days=30)
    _research_in_the_window(real)
    answer = real.service.auto_evaluate_candidate(identity="owner")
    lines = _lines(answer)
    assert answer["outcome"] == replacement.REJECTED
    assert lines["beats_incumbent_after_costs"]["ok"] is False
    assert "保留现用策略" in lines["beats_incumbent_after_costs"]["text"]["zh"]
    assert store.active_plan(real.db)["opening_version"] == incumbent, "the strategy in force stays"

    sim_db = real.sim().state_db()
    record = store.version(sim_db, candidate)
    assert record["validation"]["status"] == store.REJECTED
    assert [row["card_id"] for row in record["evidence"]] == [answer["card_id"]], "the evidence is kept"
    assert experiments.verify(record["validation"]["replacement"]["path"]) == []

    # A week later, with more research on record that would flatter it: not compared again.
    _research_in_the_window(real, days_ago=120)
    monkeypatch.setattr(bar_source, "read", lambda *_a, **_k: pytest.fail("the comparison is made once"))
    again = real.service.auto_evaluate_candidate(identity="owner", apply=True, confirmed=True)
    assert again["outcome"] == replacement.REJECTED and again["card_id"] == answer["card_id"]
    assert len(experiments.registered(sim_db)) == 1
    assert store.active_plan(real.db)["opening_version"] == incumbent
    # Both looks are on record, and so is the candidate, as one that did not pass.
    assert [row["outcome"] for row in replacement.history(real.db)] == ["rejected", "rejected"]
    assert candidate in {row["version_id"] for row in store.versions(sim_db)}
    assert [e["outcome"] for e in events.query(real.db, kinds=("auto_candidate_evaluated",))["events"]] == [
        "rejected", "rejected"]


def test_with_no_research_recorded_in_the_window_there_is_nothing_to_prove_it_with(running, monkeypatch):
    real, incumbent = running  # noqa: F811
    candidate = _candidate(real, monkeypatch, max_holding_days=180)
    answer = real.service.auto_evaluate_candidate(identity="owner", apply=True, confirmed=True)
    lines = _lines(answer)
    assert answer["outcome"] == replacement.REJECTED
    assert lines["beats_incumbent_after_costs"]["ok"] is False
    assert "没有结果不等于持平" in lines["beats_incumbent_after_costs"]["text"]["zh"]
    assert store.active_plan(real.db)["opening_version"] == incumbent
    assert store.version(real.sim().state_db(), candidate)["validation"]["status"] == store.REJECTED


def test_a_window_another_variant_was_tried_on_is_a_selection_not_a_test(running, monkeypatch):
    real, incumbent = running  # noqa: F811
    candidate = _candidate(real, monkeypatch, max_holding_days=180)
    _research_in_the_window(real)
    sim_db = real.sim().state_db()
    # Some other variant of the same strategy was already run over part of the same months.
    experiments.register(sim_db, {
        "card_id": "xp-earlier", "kind": "backtest", "version_id": "sv-another-variant",
        "lineage": experiments.lineage_of(sim_db, candidate),
        "window": {"start": _days_ago(200),
                   "end": _days_ago(100)},
        "design": {"sample": "out_of_sample"}, "recorded_at": datetime.now(UTC).isoformat()})
    answer = real.service.auto_evaluate_candidate(identity="owner", apply=True, confirmed=True)
    line = _lines(answer)["independent_out_of_sample"]
    assert line["ok"] is False and "不能当作独立验证" in line["text"]["zh"]
    assert answer["outcome"] == replacement.REJECTED
    assert store.active_plan(real.db)["opening_version"] == incumbent


def test_a_comparison_whose_record_is_gone_proves_nothing_and_is_not_made_again(running, monkeypatch):
    real, incumbent = running  # noqa: F811
    candidate = _candidate(real, monkeypatch, max_holding_days=180)
    _research_in_the_window(real)
    first = real.service.auto_evaluate_candidate(identity="owner")
    assert first["outcome"] == replacement.PASSED
    import shutil

    shutil.rmtree(store.version(real.sim().state_db(), candidate)["validation"]["replacement"]["path"])
    answer = real.service.auto_evaluate_candidate(identity="owner", apply=True, confirmed=True)
    assert answer["outcome"] == replacement.UNPROVEN
    assert _lines(answer)["experiment_reproducible"]["ok"] is False
    assert store.active_plan(real.db)["opening_version"] == incumbent
    assert len(experiments.registered(real.sim().state_db())) == 1


def test_a_proved_candidate_does_not_take_over_an_account_that_is_not_in_order(running, monkeypatch):
    real, incumbent = running  # noqa: F811
    candidate = _candidate(real, monkeypatch, max_holding_days=180)
    _research_in_the_window(real)
    real.service.auto_pause(scope="live", reason="looking at something", identity="owner")
    answer = real.service.auto_evaluate_candidate(identity="owner", apply=True, confirmed=True)
    assert answer["outcome"] == replacement.PASSED
    assert _lines(answer)["plan_running"]["ok"] is False
    assert store.active_plan(real.db)["opening_version"] == incumbent
    # Resumed, the same proof is enough: nothing is compared a second time.
    real.service.auto_resume(scope="live", note="done", identity="owner", confirmed=True)
    monkeypatch.setattr(bar_source, "read", lambda *_a, **_k: pytest.fail("the comparison is made once"))
    done = real.service.auto_evaluate_candidate(identity="owner", apply=True, confirmed=True)
    assert done["outcome"] == replacement.REPLACED
    assert store.active_plan(real.db)["opening_version"] == candidate


def test_a_strategy_of_another_mode_is_not_a_replacement(running):
    real, incumbent = running  # noqa: F811
    # The simulated account is started again on a swing plan: a different mode's strategy.
    preview = real.service.auto_plan(mode="swing")
    real.service.auto_start(plan_id=preview["plan_id"], mode="swing", identity="owner")
    answer = real.service.auto_evaluate_candidate(identity="owner", apply=True, confirmed=True)
    assert answer["outcome"] == replacement.INELIGIBLE
    line = _lines(answer)["same_mode"]
    assert line["ok"] is False and "换模式是你的决定" in line["text"]["zh"]
    assert answer["card_id"] is None and store.active_plan(real.db)["opening_version"] == incumbent


def test_a_change_of_code_after_the_proof_is_a_different_candidate(running, monkeypatch):
    real, _incumbent = running  # noqa: F811
    _candidate(real, monkeypatch, max_holding_days=180)
    _research_in_the_window(real)
    assert real.service.auto_evaluate_candidate(identity="owner")["outcome"] == replacement.PASSED
    # A release changes the trading logic before anyone made the change.
    monkeypatch.setattr(store, "logic_digest", lambda: "a-later-release")
    real.service.auto_tick()
    assert store.active_plan(real.db) is None, "the real plan is out of force (tests/test_auto_live.py)"
    answer = real.service.auto_evaluate_candidate(identity="owner", apply=True, confirmed=True)
    assert answer["outcome"] == replacement.NOTHING and "no plan is in force" in answer["reason"]


# ---------------------------------------------------------------- over HTTP
def test_replacement_over_http(auth_client, running, monkeypatch):
    real, incumbent = running  # noqa: F811
    monkeypatch.setattr("agentic_trading.api.app._service", real.service)
    candidate = _candidate(real, monkeypatch, max_holding_days=180)
    _research_in_the_window(real)
    looked = auth_client.post("/auto-trading/candidate/evaluate", json={})
    assert looked.status_code == 200 and looked.json()["outcome"] == replacement.PASSED
    bare = auth_client.post("/auto-trading/candidate/replace", json={})
    assert bare.status_code == 400 and "console password" in bare.json()["detail"]
    wrong = auth_client.post("/auto-trading/candidate/replace", json={"password": "not-it"})
    assert wrong.status_code == 400 and "did not match" in wrong.json()["detail"]
    on = auth_client.post("/auto-trading/replacement", json={"on": True})
    assert on.status_code == 400 and "console password" in on.json()["detail"]
    assert auth_client.post("/auto-trading/replacement", json={"on": "yes"}).status_code == 422
    assert auth_client.post("/auto-trading/candidate/replace", json={"confirmed": True}).status_code == 422
    assert store.active_plan(real.db)["opening_version"] == incumbent
    done = auth_client.post("/auto-trading/candidate/replace", json={"password": PASSWORD})
    assert done.status_code == 200 and done.json()["outcome"] == replacement.REPLACED
    assert auth_client.get("/auto-trading/live").json()["plan"]["opening_version"] == candidate
