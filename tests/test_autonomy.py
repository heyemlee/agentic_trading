"""The gate that lets deterministic code approve a reduction. Fully offline.

Read the refusals first. A refusal here means the proposal waits for a person, which is what
happens to everything today — so every one of these is a delay, and the single grant is the
only case that becomes an order without anyone looking.
"""

from datetime import UTC, datetime, timedelta

import pytest

from agentic_trading import autonomy
from agentic_trading.execution_scenarios import _profiles

MICRO = "micro_live"
NOW = datetime(2026, 9, 10, 15, 0, tzinfo=UTC)


@pytest.fixture
def armed(tmp_path):
    profiles = _profiles(tmp_path)
    profiles.risk["buckets"][MICRO]["autonomy"] = {
        "reduce": {"enabled": True, "min_confidence": 80, "max_positions_reduced_per_day": 1}}
    return profiles


def ask(profiles, **over):
    kw = {"bucket": MICRO, "action": "REDUCE", "confidence": 85, "held_quantity": 2.0,
          "requested_quantity": 1.0, "reduced_today": 0, "now": NOW}
    kw.update(over)
    return autonomy.permits(profiles, **kw)


def test_a_half_reduction_with_conviction_is_the_one_thing_it_may_do(armed):
    decision = ask(armed)
    assert decision.permitted, decision.reasons


def test_closing_a_position_outright_always_belongs_to_a_person(armed):
    decision = ask(armed, action="SELL", requested_quantity=2.0)
    assert not decision.permitted
    assert "closing a position outright" in decision.reasons[-1]


@pytest.mark.parametrize("action", ["BUY", "ADD", "HOLD", "NO_TRADE"])
def test_arming_reductions_arms_nothing_that_takes_on_risk(armed, action):
    # The two blocks are switched separately, which is the whole reason they are two blocks.
    assert not ask(armed, action=action).permitted


def test_it_is_off_until_switched_on(tmp_path):
    profiles = _profiles(tmp_path)
    decision = ask(profiles)
    assert not decision.permitted and "enabled is not true" in decision.reasons[-1]


def test_low_conviction_waits_for_a_person(armed):
    decision = ask(armed, confidence=79)
    assert not decision.permitted and "a person decides this one" in decision.reasons[-1]
    assert ask(armed, confidence=80).permitted, "the floor is inclusive"


@pytest.mark.parametrize("confidence", [None, True, "85", 85.5])
def test_an_unusable_confidence_is_a_refusal(armed, confidence):
    # bool is a subclass of int, so `min_confidence: true` would otherwise read as 1.
    assert not ask(armed, confidence=confidence).permitted


def test_one_position_a_day_and_no_more(armed):
    assert ask(armed, reduced_today=0).permitted
    spent = ask(armed, reduced_today=1)
    assert not spent.permitted and "at the limit of 1" in spent.reasons[-1]


def test_a_caller_cannot_hand_over_more_than_half_a_holding(armed):
    # Belt and braces over the engine's own REDUCE semantics.
    decision = ask(armed, held_quantity=2.0, requested_quantity=1.5)
    assert not decision.permitted and "more than half the holding" in decision.reasons[-1]


@pytest.mark.parametrize("missing", ["min_confidence", "max_positions_reduced_per_day"])
def test_a_limit_that_is_not_declared_refuses_rather_than_defaulting(armed, missing):
    armed.risk["buckets"][MICRO]["autonomy"]["reduce"].pop(missing)
    decision = ask(armed)
    assert not decision.permitted and missing in decision.reasons[-1]


# -- the sticky withdrawal ----------------------------------------------------------

def test_a_trip_withholds_autonomy_until_a_person_clears_it(armed):
    assert autonomy.is_armed(armed, MICRO)
    autonomy.trip(armed, "the venue returned an order we cannot account for")
    assert not autonomy.is_armed(armed, MICRO)
    decision = ask(armed)
    assert not decision.permitted and "needs a manual reset" in decision.reasons[-1]
    autonomy.reset(armed, "checked the venue by hand, the order was never placed")
    assert autonomy.is_armed(armed, MICRO) and ask(armed).permitted


def test_the_first_reason_is_the_one_kept_and_the_history_survives_a_reset(armed):
    autonomy.trip(armed, "first")
    autonomy.trip(armed, "second")
    state = autonomy.load_downgrade(armed)
    assert state.reason == "first", "the first explanation is the one that matters"
    autonomy.reset(armed, "looked into it")
    events = [h["event"] for h in autonomy.load_downgrade(armed).history]
    assert events == ["tripped", "tripped", "reset"], "a reset must never be invisible"


@pytest.mark.parametrize("outcome", sorted(autonomy.TRIPPING_OUTCOMES))
def test_an_attempt_that_ends_in_an_unaccountable_state_withdraws_autonomy(armed, outcome):
    assert autonomy.trip_for_outcome(armed, MICRO, outcome, "detail")
    assert autonomy.load_downgrade(armed).tripped


@pytest.mark.parametrize("outcome", ["STALE", "REJECTED", "INVALIDATED", "REANALYZE",
                                     "ALREADY_EXECUTED", "DRY_RUN_VALID"])
def test_an_ordinary_not_now_leaves_autonomy_armed(armed, outcome):
    # A gate that withdraws itself every Saturday, when revalidate returns STALE because the
    # market is closed, is a gate nobody can rely on.
    assert not autonomy.trip_for_outcome(armed, MICRO, outcome)
    assert autonomy.is_armed(armed, MICRO)


def test_the_history_stays_bounded(armed):
    for i in range(40):
        autonomy.trip(armed, f"trip {i}")
    history = autonomy.load_downgrade(armed).history
    assert len(history) <= 20
    assert history[0]["reason"] == "trip 0", "the first trip explains the state and is kept"


# -- counting what has already been done today --------------------------------------

class FakeProposal:
    def __init__(self, ticker, bucket=MICRO, approved_by="autonomy:reduce", minutes_ago=30,
                 action="REDUCE"):
        self.ticker = ticker
        self.bucket = bucket
        self.action = action
        self.approved_by = approved_by
        self.approved_at = (NOW - timedelta(minutes=minutes_ago)).isoformat()


def test_the_daily_count_comes_from_the_approvals_themselves(armed):
    # Not from a counter: a restart, a crash or a second process cannot lose an approval record.
    proposals = [FakeProposal("VTI"), FakeProposal("BND")]
    assert autonomy.reduced_today(proposals, MICRO, now=NOW) == 2


def test_two_reductions_of_one_position_count_once(armed):
    proposals = [FakeProposal("VTI"), FakeProposal("VTI", minutes_ago=90)]
    assert autonomy.reduced_today(proposals, MICRO, now=NOW) == 1


def test_a_persons_approval_is_not_counted_against_the_autonomous_budget(armed):
    proposals = [FakeProposal("VTI", approved_by="owner"), FakeProposal("BND", approved_by=None)]
    assert autonomy.reduced_today(proposals, MICRO, now=NOW) == 0


def test_yesterdays_reduction_does_not_count(armed):
    assert autonomy.reduced_today([FakeProposal("VTI", minutes_ago=25 * 60)], MICRO, now=NOW) == 0


def test_another_buckets_reduction_does_not_count(armed):
    assert autonomy.reduced_today([FakeProposal("VTI", bucket="core")], MICRO, now=NOW) == 0


# -- the command ---------------------------------------------------------------------

@pytest.fixture
def config_copy(tmp_path):
    """A config the command can load without reaching the real one, or the real state/."""
    import shutil

    from tests.conftest import ROOT

    cfg = tmp_path / "config"
    shutil.copytree(ROOT / "config", cfg)
    (tmp_path / "state").mkdir()
    return cfg


def run(cfg, *args):
    from agentic_trading import autonomy as module

    assert module.main([*args, "--config-dir", str(cfg)]) == 0


def test_status_reports_what_autonomy_may_do_and_changes_nothing(config_copy, capsys):
    import json

    run(config_copy, "status")
    out = json.loads(capsys.readouterr().out)
    assert out["action_permitted"] == "REDUCE"
    assert out["enabled"] is False and out["armed"] is False
    assert out["limits"]["max_positions_reduced_per_day"] == 1
    assert not (config_copy.parent / "state" / "autonomy.json").exists(), "status must not write"


def test_stop_halts_in_one_command_without_touching_the_config(config_copy, capsys):
    import json

    before = (config_copy / "risk_profile.yaml").read_bytes()
    run(config_copy, "stop", "--note", "the tape looks wrong to me")
    out = json.loads(capsys.readouterr().out)
    assert out["withdrawn"]["tripped"] is True
    assert "the tape looks wrong to me" in out["withdrawn"]["reason"]
    # The point of the command: stopping never means editing a live trading config in a hurry.
    assert (config_copy / "risk_profile.yaml").read_bytes() == before


def test_resume_is_separate_deliberate_and_recorded(config_copy, capsys):
    import json

    run(config_copy, "stop", "--note", "looked wrong")
    capsys.readouterr()
    run(config_copy, "resume", "--note", "checked the venue, nothing was placed")
    out = json.loads(capsys.readouterr().out)
    assert out["withdrawn"]["tripped"] is False
    events = [h["event"] for h in out["withdrawn"]["history"]]
    assert events == ["tripped", "reset"], "restoring autonomy is never invisible"


@pytest.mark.parametrize("action", ["stop", "resume"])
def test_changing_the_state_requires_saying_why(config_copy, action):
    from agentic_trading import autonomy as module

    with pytest.raises(SystemExit):
        module.main([action, "--config-dir", str(config_copy)])


# -- opening: the one place code may create risk -------------------------------------

@pytest.fixture
def armed_to_open(armed):
    armed.risk["buckets"][MICRO]["autonomy"]["open"] = {
        "enabled": True, "min_confidence": 80,
        "classes": ["broad_diversified_etf", "bond_or_cash_like_etf"],
        "max_new_positions_per_day": 1}
    return armed


def ask_open(profiles, **over):
    kw = {"bucket": MICRO, "action": "BUY", "confidence": 85,
          "limit_class": "broad_diversified_etf", "opened_today": 0, "now": NOW}
    kw.update(over)
    return autonomy.permits(profiles, **kw)


def test_an_allowed_class_with_conviction_may_be_opened(armed_to_open):
    assert ask_open(armed_to_open).permitted


def test_opening_is_off_even_while_reducing_is_on(armed):
    decision = ask_open(armed)
    assert not decision.permitted and "autonomy.open.enabled is not true" in decision.reasons[-1]


@pytest.mark.parametrize("limit_class", ["individual_stock", "sector_factor_thematic_etf",
                                         "crypto", None])
def test_only_the_named_classes_may_be_opened(armed_to_open, limit_class):
    decision = ask_open(armed_to_open, limit_class=limit_class)
    assert not decision.permitted
    assert "not one of the classes code may open" in decision.reasons[-1]


def test_an_empty_class_list_is_not_permission_to_buy_anything(armed_to_open):
    armed_to_open.risk["buckets"][MICRO]["autonomy"]["open"]["classes"] = []
    decision = ask_open(armed_to_open)
    assert not decision.permitted and "not permission to buy anything" in decision.reasons[-1]


def test_one_new_position_a_day_and_no_more(armed_to_open):
    assert ask_open(armed_to_open, opened_today=0).permitted
    spent = ask_open(armed_to_open, opened_today=1)
    assert not spent.permitted and "at the limit of 1" in spent.reasons[-1]


def test_the_two_budgets_are_separate(armed_to_open):
    # Reducing something does not spend the allowance to open something, and the reverse.
    assert ask_open(armed_to_open, reduced_today=5).permitted
    assert ask(armed_to_open, opened_today=5).permitted


def test_a_withdrawal_stops_opening_too(armed_to_open):
    autonomy.trip(armed_to_open, "an order we cannot account for")
    assert not ask_open(armed_to_open).permitted
    assert not autonomy.is_armed(armed_to_open, MICRO)


def test_the_open_budget_counts_buys_and_the_reduce_budget_counts_reductions(armed_to_open):
    bought = [FakeProposal("VTI", action="BUY"), FakeProposal("BND", action="ADD")]
    reduced = [FakeProposal("SGOV", action="REDUCE")]
    everything = bought + reduced
    assert autonomy.opened_today(everything, MICRO, now=NOW) == 2
    assert autonomy.reduced_today(everything, MICRO, now=NOW) == 1


# ------------------------------------------------------------------ who approved it
def _approved(via=None, by=None, action="REDUCE", ticker="VTI", bucket="micro_live"):
    from types import SimpleNamespace

    return SimpleNamespace(bucket=bucket, action=action, ticker=ticker, approved_by=by,
                           approved_via=via, approved_at=datetime.now(UTC).isoformat())


def test_who_approved_it_is_read_from_the_record_not_from_a_username():
    """`approved_by` carries a session's username, which a person chooses. A limit decided by
    looking for a prefix in it would be a limit decided on something a person can pick."""
    assert autonomy.was_autonomous(_approved(via="autonomy")) is True
    assert autonomy.was_autonomous(_approved(via="console")) is False
    assert autonomy.was_human(_approved(via="console")) is True
    assert autonomy.was_human(_approved(via="cli")) is True
    assert autonomy.was_human(_approved(via="autonomy")) is False
    # A username that looks autonomous does not make it so once the record says otherwise.
    assert autonomy.was_autonomous(_approved(via="console", by="autonomy:reduce")) is False
    assert autonomy.was_human(_approved(via="console", by="autonomy:reduce")) is True


def test_a_record_from_before_the_field_falls_back_in_the_safe_direction():
    """The two questions face opposite ways, so the fallback differs and each errs toward less
    autonomy: an unattributable record is counted against code's daily allowance, and is never
    evidence of the human purchase a position floor is measured from."""
    legacy_auto = _approved(by="autonomy:reduce")
    legacy_human = _approved(by="owner")
    # Counted as autonomous: under-counting would hand code an extra reduction.
    assert autonomy.was_autonomous(legacy_auto) is True
    assert autonomy.reduced_today([legacy_auto], "micro_live") == 1
    # Never read as a person: a baseline raised by a record we cannot attribute is no baseline.
    assert autonomy.was_human(legacy_auto) is False
    assert autonomy.was_human(legacy_human) is False
    assert autonomy.was_autonomous(legacy_human) is False
