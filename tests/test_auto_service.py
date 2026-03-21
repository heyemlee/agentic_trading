"""The automatic-trading plan as the service, the API and the CLI give it (docs/todo.md, P1.3-P1.4).

What only these can check: that a preview writes nothing, that each change is described by the
console's own rules for what loosens a limit, that the minimum order cannot be lowered past the
venue's, and that a model's suggestion is a suggestion. The account is the sandbox's own $100
ledger; the model, where one is asked, is the suite's fake.
"""

import json
import shutil

import pytest

from agentic_trading import config_changes as cc
from agentic_trading.auto import advice, cli, modes, plan
from agentic_trading.llm.claude_code import ClaudeCodeError
from agentic_trading.service import ServiceError
from tests.test_api import sandbox_service  # noqa: F401 - shared isolated fixture

OPEN = "buckets.micro_live.autonomy.open"


def _config_bytes(service):
    directory = service.profiles().config_dir
    return {path.name: path.read_bytes() for path in sorted(directory.glob("*.yaml"))}


# ---------------------------------------------------------------- the preview
def test_the_plan_is_drawn_at_the_size_the_account_already_is(sandbox_service):  # noqa: F811
    document = sandbox_service.auto_plan()
    assert document["envelope"]["capital_usd"] == 100.0 and document["envelope"]["mode"] == "steady"
    assert document["account"]["equity_usd"] == 100.0 and document["account"]["positions"] == 0
    assert document["today"]["any_order"] is False
    assert document["under_plan"]["deployable_usd"] == 60.0
    assert document["strategy"]["suggested_by"] == "template"
    assert [mode["key"] for mode in document["modes"]] == list(modes.MODES)
    assert json.loads(json.dumps(document, ensure_ascii=False))["plan_id"] == document["plan_id"]


def test_previewing_a_plan_writes_nothing(sandbox_service):  # noqa: F811
    profiles = sandbox_service.profiles()
    before = _config_bytes(sandbox_service)
    db = sandbox_service.state_db(profiles)
    records = cc.records(db)
    sandbox_service.auto_plan(capital_usd=80.0, loss_pause_pct=5.0)
    assert _config_bytes(sandbox_service) == before, "a preview may not touch config/"
    assert cc.records(db) == records, "and it records no change, pending or applied"


def test_each_change_is_classified_by_the_consoles_own_rules(sandbox_service):  # noqa: F811
    landing = sandbox_service.auto_plan()["landing"]
    effect = {row["path"]: row["effect"] for row in landing["changes"]}
    # Everything that lets an order through that was being stopped is a loosening and waits.
    assert effect[f"{OPEN}.enabled"] == cc.LOOSENING
    assert effect[f"{OPEN}.max_pct_of_account"] == cc.LOOSENING          # 20% -> 100%
    assert effect["sizing.fit_to_limits"] == cc.UNKNOWN                   # not declared yet
    assert effect["sizing.min_order_usd"] == cc.LOOSENING                 # 25 -> 16
    # What gets researched is not a limit on the money.
    assert effect["research_schedule.accounts.micro_live.candidates.enabled"] == cc.NEUTRAL
    assert landing["classification"] == cc.COOLING and landing["needs_password"] is True
    assert landing["cooling_hours"] == cc.COOLING_HOURS and landing["errors"] == []
    assert "密码" in landing["summary"]["zh"] and "24" in landing["summary"]["zh"]
    assert all(row["waits"] for row in landing["changes"]
               if row["effect"] in (cc.LOOSENING, cc.UNKNOWN))


def test_a_key_the_plan_did_not_name_is_never_created(sandbox_service):  # noqa: F811
    profiles = sandbox_service.profiles()
    landing = sandbox_service._auto_landing(profiles, [
        {"file": "risk_profile.yaml", "path": "sizing.made_up_setting", "to": 1}])
    assert landing["changes"] == [] and "no such setting" in landing["errors"][0]


EXIT = "buckets.micro_live.autonomy.managed_exit"


def test_the_way_out_is_part_of_the_plan_and_waits_like_any_other_new_authority(sandbox_service):  # noqa: F811
    """Code may not be let in without being let out: the plan asks for both together."""
    document = sandbox_service.auto_plan()
    change = next(c for c in document["changes"] if c["path"] == f"{EXIT}.enabled")
    assert change["to"] is True and change["kind"] == plan.BOUNDARY
    assert "自己" in change["why"]["zh"] and "你自己买入的仓位" in change["why"]["zh"]
    row = next(r for r in document["landing"]["changes"] if r["path"] == f"{EXIT}.enabled")
    # The real configuration has never declared it, so it is a new authority and it waits.
    assert row["effect"] in (cc.UNKNOWN, cc.LOOSENING) and row["waits"] is True
    assert sandbox_service.profiles().risk_cfg(*EXIT.split("."), default=None) is None


def test_a_block_the_file_never_declared_is_created_only_for_a_path_the_code_names(sandbox_service):  # noqa: F811
    profiles = sandbox_service.profiles()
    text = (profiles.config_dir / "risk_profile.yaml").read_text(encoding="utf-8")
    named = frozenset({f"{EXIT}.enabled"})
    made = cc.set_values(text, {f"{EXIT}.enabled": True}, create=named)
    cc.validate(profiles.config_dir, "risk_profile.yaml", made)
    assert cc.get_value(cc._parse(made), f"{EXIT}.enabled") is True
    # Nothing else moved: the only lines that differ are the new block's own.
    added = [line for line in made.splitlines() if line not in text.splitlines()]
    assert [line.strip() for line in added] == ["managed_exit:", "enabled: true"]
    # The same path without being named, a sibling that was not named, and a new top-level
    # section even when it is named: each is refused, as a misspelt key always was.
    for updates, create in (
            ({f"{EXIT}.enabled": True}, frozenset()),
            ({"buckets.micro_live.autonomy.managed_exits.enabled": True}, named),
            ({"brand_new_section.enabled": True}, frozenset({"brand_new_section.enabled"}))):
        with pytest.raises(cc.ConfigChangeError, match="no such setting"):
            cc.set_values(text, updates, create=create)


def test_an_envelope_past_a_ceiling_is_a_refusal_a_person_can_read(sandbox_service):  # noqa: F811
    with pytest.raises(ServiceError, match="ceiling"):
        sandbox_service.auto_plan(capital_usd=5000.0)
    with pytest.raises(ServiceError, match="loss line"):
        sandbox_service.auto_plan(loss_pause_pct=60.0)


def test_a_ledger_that_cannot_be_read_stops_the_plan_and_says_so(sandbox_service):  # noqa: F811
    profiles = sandbox_service.profiles()
    (profiles.project_dir / "state" / "micro_live_portfolio.yaml").unlink()
    with pytest.raises(ServiceError, match="could not be read"):
        sandbox_service.auto_plan()


def test_cash_an_unresolved_order_holds_is_not_planned_twice(sandbox_service):  # noqa: F811
    profiles = sandbox_service.profiles()
    db = sandbox_service.state_db(profiles)
    clean = sandbox_service.auto_plan()
    assert clean["account"]["open_orders"] == 0 and clean["account"]["committed_usd"] == 0.0
    with db.transaction() as conn:
        conn.execute("INSERT INTO reservations VALUES(?,?,?,?,?,'ACTIVE',?)",
                     ("ct-working-1", "p-working", "micro_live", 35.0, 0.0, "2026-10-06T00:00:00+00:00"))
        # Another account's order is not this account's cash.
        conn.execute("INSERT INTO reservations VALUES(?,?,?,?,?,'ACTIVE',?)",
                     ("ct-demo-1", "p-demo", "conceptual", 500.0, 0.0, "2026-10-06T00:00:00+00:00"))
    document = sandbox_service.auto_plan()
    assert document["account"]["open_orders"] == 1 and document["account"]["committed_usd"] == 35.0
    assert document["account"]["cash_usd"] == 65.0
    assert document["under_plan"]["deployable_usd"] <= 65.0
    assert "未了结" in document["summary"]["zh"] and "$35.00" in document["summary"]["zh"]


# ---------------------------------------------------------------- the minimum order and the venue
def _lower_minimum(service, value):
    profiles = service.profiles()
    current = (profiles.config_dir / "risk_profile.yaml").read_text(encoding="utf-8")
    new_text = cc.set_values(current, {"sizing.min_order_usd": value})
    cc.validate(profiles.config_dir, "risk_profile.yaml", new_text)
    return cc.classify("risk_profile.yaml", current, new_text)


def test_lowering_the_minimum_order_waits_and_cannot_pass_the_venues_own(sandbox_service):  # noqa: F811
    verdict = _lower_minimum(sandbox_service, 16)
    assert verdict["classification"] == cc.COOLING            # it used to read as a tightening
    assert verdict["changes"][0]["effect"] == cc.LOOSENING
    assert _lower_minimum(sandbox_service, 1)["classification"] == cc.COOLING      # the venue's floor
    with pytest.raises(cc.ConfigChangeError, match=r"cannot be set to \$0\.50"):
        _lower_minimum(sandbox_service, 0.5)
    # Raising it, or leaving it, is never refused.
    assert _lower_minimum(sandbox_service, 40)["classification"] == cc.IMMEDIATE


def test_the_minimum_cannot_be_lowered_while_the_venues_is_not_on_record(sandbox_service, monkeypatch):  # noqa: F811
    from agentic_trading.broker import requirements

    monkeypatch.setattr(requirements, "for_venue", lambda _venue: None)
    with pytest.raises(cc.ConfigChangeError, match="cannot be lowered"):
        _lower_minimum(sandbox_service, 16)
    assert _lower_minimum(sandbox_service, 30)["classification"] == cc.IMMEDIATE


def test_turning_cut_to_fit_on_is_a_loosening_and_off_is_not():
    assert cc._effect(cc._rule("sizing.fit_to_limits"), False, True) == cc.LOOSENING
    assert cc._effect(cc._rule("sizing.fit_to_limits"), True, False) == cc.TIGHTENING
    assert cc._effect(cc._rule("sizing.micro_live.fit_to_limits"), False, True) == cc.LOOSENING
    assert cc._effect(cc._rule(f"{OPEN}.max_usd"), 100, 300) == cc.LOOSENING
    assert cc._effect(cc._rule(f"{OPEN}.max_usd"), 300, 100) == cc.TIGHTENING


# ---------------------------------------------------------------- a model's suggestion
class _Suggests:
    def __init__(self, strategy=None, error=None):
        self.strategy, self.error, self.seen = strategy, error, None

    def with_structured_output(self, schema):
        assert schema is plan.Strategy
        return self

    def invoke(self, messages):
        self.seen = messages
        if self.error is not None:
            raise self.error
        return self.strategy


def test_a_models_suggestion_is_used_only_after_it_is_clamped(sandbox_service, monkeypatch):  # noqa: F811
    model = _Suggests(plan.Strategy(max_positions=8, position_pct=40.0, min_confidence=55,
                                    stop_loss_pct=8.0, max_holding_days=60, universe=["VTI", "NVDA"],
                                    rationale_zh="资金小，集中一些。", rationale_en="Small capital."))
    real = advice.suggest
    monkeypatch.setattr(advice, "suggest", lambda *a, **kw: real(*a, llm=model, **kw))
    document = sandbox_service.auto_plan(ask_model=True)
    used = document["strategy"]
    assert used["suggested_by"] == "model" and used["suggestion_error"] is None
    assert used["min_confidence"] == 80, "a model cannot lower the bar for buying on its own"
    assert used["max_positions"] == 3 and used["universe"] == ["VTI"]
    assert used["stop_loss_pct"] == 8.0 and used["rationale"]["zh"] == "资金小，集中一些。"
    assert {note["field"] for note in document["clamps"]} == {"max_positions", "min_confidence", "universe"}
    # And the model was told the envelope and the bounds, with no tool and no order surface.
    system, user = model.seen
    assert "cannot change the envelope" in system["content"]
    brief = json.loads(user["content"])
    assert brief["envelope"]["capital_usd"] == 100.0 and brief["bounds"]["min_confidence"] == [80, 100]


@pytest.mark.parametrize("error,expected", [
    (ClaudeCodeError("claude failed", login_expired=True), "login on this Mac has expired"),
    (ClaudeCodeError("limit", rate_limited=True), "subscription limit"),
    (ClaudeCodeError("boom"), "model call failed"),
    (ValueError("not a strategy"), "could not be used"),
])
def test_a_model_that_cannot_answer_leaves_the_template_and_the_reason(sandbox_service, monkeypatch,  # noqa: F811
                                                                      error, expected):
    model = _Suggests(error=error)
    real = advice.suggest
    monkeypatch.setattr(advice, "suggest", lambda *a, **kw: real(*a, llm=model, **kw))
    document = sandbox_service.auto_plan(ask_model=True)
    assert document["strategy"]["suggested_by"] == "template"
    assert expected in document["strategy"]["suggestion_error"]
    assert document["under_plan"]["deployable_usd"] == 60.0, "the plan stands without the model"


def test_the_model_is_not_asked_unless_somebody_asks(sandbox_service, monkeypatch):  # noqa: F811
    def never(*_args, **_kwargs):
        raise AssertionError("a preview spent the subscription without being asked to")

    monkeypatch.setattr(advice, "suggest", never)
    assert sandbox_service.auto_plan()["strategy"]["suggested_by"] == "template"


def test_the_real_adapter_returns_a_strategy_through_the_fake_cli(fake_claude, sandbox_service):  # noqa: F811
    """The structured call itself, end to end against the suite's stand-in for the CLI."""
    document = sandbox_service.auto_plan(ask_model=True)
    # The fake fills the schema with its minimums: every one is outside the bounds and is clamped.
    assert document["strategy"]["suggested_by"] == "model"
    assert document["strategy"]["min_confidence"] == 80 and document["strategy"]["max_positions"] == 1
    assert document["clamps"], "the fake's zeros did not become the plan"


# ---------------------------------------------------------------- over HTTP and from a terminal
def test_the_plan_and_the_status_over_http(auth_client, sandbox_service, monkeypatch):  # noqa: F811
    monkeypatch.setattr("agentic_trading.api.app._service", sandbox_service)
    status = auth_client.get("/auto-trading")
    assert status.status_code == 200
    body = status.json()
    assert [mode["key"] for mode in body["modes"]] == ["steady", "swing", "intraday"]
    assert all(mode["open_for_real_money"] is False and mode["blockers"] for mode in body["modes"])

    answer = auth_client.post("/auto-trading/plan", json={"mode": "steady", "capital_usd": 100})
    assert answer.status_code == 200, answer.text
    document = answer.json()
    assert document["under_plan"]["deployable_usd"] == 60.0 and document["landing"]["needs_password"]

    refused = auth_client.post("/auto-trading/plan", json={"capital_usd": 999999})
    assert refused.status_code == 400 and "ceiling" in refused.json()["detail"]
    assert auth_client.post("/auto-trading/plan", json={"mode": "scalp"}).status_code == 422
    assert auth_client.post("/auto-trading/plan", json={"password": "x"}).status_code == 422


def test_a_signed_out_caller_gets_neither(sandbox_service, monkeypatch, console_auth):  # noqa: F811
    from fastapi.testclient import TestClient

    from agentic_trading.api.app import app

    monkeypatch.setattr("agentic_trading.api.app._service", sandbox_service)
    with TestClient(app, base_url="https://testserver") as anonymous:
        assert anonymous.get("/auto-trading").status_code == 401
        assert anonymous.post("/auto-trading/plan", json={},
                              headers={"Origin": "https://testserver", "X-CT-Request": "1"}).status_code == 401


def test_the_cli_prints_the_plan_in_plain_chinese(sandbox_service, capsys, monkeypatch):  # noqa: F811
    config_dir = sandbox_service.profiles().config_dir
    monkeypatch.setattr("agentic_trading.service.Service", lambda _dir: sandbox_service)
    assert cli.main(["--config-dir", str(config_dir), "plan", "--capital", "100"]) == 0
    out = capsys.readouterr().out
    for expected in ("稳健模式方案", "实际可投入：$60.00（2 个仓位）", "按当前设置", "按本方案",
                     "✓ VTI   $33.00", "✓ VXUS  $27.00（由 $33.00 缩小）", "整笔拒绝",
                     "[边界·需你确认] ⏳", "不是券商的要求", "尚未在本账户上确认", "待核实："):
        assert expected in out, expected
    assert cli.main(["--config-dir", str(config_dir), "--lang", "en", "plan"]) == 0
    assert "Can actually be invested: $60.00 (2 position(s))" in capsys.readouterr().out
    assert cli.main(["--config-dir", str(config_dir), "status"]) == 0
    status = capsys.readouterr().out
    assert "1. 稳健：未开放" in status and "等待样本" in status and "3. 短线：未开放" in status
    assert cli.main(["--config-dir", str(config_dir), "plan", "--capital", "999999"]) == 2
    assert "ceiling" in capsys.readouterr().out


def test_the_cli_is_installed_as_a_console_script():
    from tests.conftest import ROOT

    assert 'ct-auto = "agentic_trading.auto.cli:main"' in (ROOT / "pyproject.toml").read_text()


# ---------------------------------------------------------------- the modes
def test_every_mode_says_exactly_what_it_is_still_missing(profiles):
    rows = {row["key"]: row for row in modes.status(profiles)}
    steady = {b["code"] for b in rows["steady"]["blockers"]}
    assert steady == {"simulation_record_short", "exit_not_exercised"}
    swing = {b["code"] for b in rows["swing"]["blockers"]}
    # Not open because steady is not; because whether it counts as the momentum the risk profile
    # gates is the owner's to say; because nothing studies its funds every session; and because
    # four orders a week is tight for a daily rhythm.
    assert swing == {"earlier_mode_not_open", "simulation_record_short", "exit_not_exercised",
                     "swing_policy_undecided", "daily_research_not_scheduled",
                     "activity_limits_tight_for_daily"}
    waits = {b["code"]: b["waits_on"] for b in rows["swing"]["blockers"]}
    assert waits["swing_policy_undecided"] == waits["daily_research_not_scheduled"] == "owner"
    research = next(b for b in rows["swing"]["blockers"] if b["code"] == "daily_research_not_scheduled")
    assert "未开启" in research["text"]["zh"] and "每周约 15 次" in research["text"]["zh"]
    intraday = {b["code"] for b in rows["intraday"]["blockers"]}
    # Intraday is a momentum strategy by its own tag, which the risk profile gates outright.
    assert {"intraday_not_built", "broker_intraday_unverified", "activity_limits_too_tight",
            "strategy_always_needs_a_person"} <= intraday
    assert rows["intraday"]["can_simulate"] is False and rows["steady"]["can_simulate"] is True
    for row in rows.values():
        assert row["open_for_real_money"] is False
        assert all(b["text"]["zh"] and b["text"]["en"] and b["waits_on"] for b in row["blockers"])


def test_the_three_modes_are_three_ways_of_running_not_three_names():
    """docs/todo.md, P2.5: what differs is what decides a trade, how often, and how it ends."""
    steady, swing, intraday = (modes.TABLE[key] for key in modes.MODES)
    assert (steady.cadence, swing.cadence, intraday.cadence) == ("weekly", "daily", "intraday")
    assert (steady.signal, swing.signal, intraday.signal) == (
        modes.RESEARCH, modes.RESEARCH_AND_TREND, modes.PLAN_AND_RULES)
    assert steady.trend_window == (0, 0) and swing.trend_window == (10, 60)
    assert steady.holding_days == (7, 180) and swing.holding_days == (2, 30) and intraday.holding_days == (0, 1)
    said = [mode.as_dict()["runs_as"] for mode in (steady, swing, intraday)]
    assert len({text["zh"] for text in said}) == 3 and all(text["zh"] and text["en"] for text in said)
    assert "均线" in said[1]["zh"] and "均线" not in said[0]["zh"]
    assert "不为每一笔交易再问模型" in said[2]["zh"] and "尚未实现" in said[2]["zh"]


def test_a_mode_opens_only_on_its_record_and_in_order(profiles):
    ready = {"steady": 30, "swing": 30}
    exits = {"steady": True, "swing": True}
    rows = {row["key"]: row for row in modes.status(
        profiles, simulated_days=ready, exits_verified=exits, opened=("steady",))}
    assert rows["steady"]["open_for_real_money"] is True and rows["steady"]["blockers"] == []
    # Swing has its days too, and is still held by the profile's own rule and by not being opened.
    assert rows["swing"]["open_for_real_money"] is False
    short = modes.status(profiles, simulated_days={"steady": 29}, exits_verified=exits,
                         opened=("steady",))[0]
    assert short["open_for_real_money"] is False
    assert "29" in short["blockers"][0]["text"]["zh"] and "等待样本" in short["blockers"][0]["text"]["zh"]


def test_the_sandbox_config_is_a_copy_and_is_left_as_it_was(sandbox_service, tmp_path):  # noqa: F811
    live = (sandbox_service.profiles().config_dir / "risk_profile.yaml").read_text(encoding="utf-8")
    shutil.copy(sandbox_service.profiles().config_dir / "risk_profile.yaml", tmp_path / "before.yaml")
    sandbox_service.auto_plan(ask_model=False)
    assert (tmp_path / "before.yaml").read_text(encoding="utf-8") == live
