"""What can place an order, and where it is allowed to live.

Splitting service/ by domain was worth doing partly so that auditing "what can move money" is
auditing one file. Nothing in the language holds that: a later feature is free to import
``execution.execute`` into whichever module it happens to be working in, and nobody would notice.
These tests notice, across the whole package rather than service/ alone -- two modules outside
service/ already import it, and each is exempt here for a stated reason that is itself tested.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "agentic_trading"

#: The one function in the package that reaches ``BrokerAdapter.submit_order``. Everything else in
#: execution.py either reads the venue or records what a submission did; ``reconcile_proposal`` is
#: deliberately not here, because it never submits, reviews or cancels (see execution.py).
ORDER_PLACING = {"execute"}

#: The module that may hold it: the live path, reached from the console.
ALLOWED = "service/approvals.py"

#: Modules that import the same function but cannot reach a venue. Each entry is the reason, and
#: each reason is held by a test below -- an exemption nobody checks is not an exemption.
EXEMPT = {
    "cli.py":
        "ct-approve, which _load() turns away once execution_policy.stage is above 2, as it is "
        "in this deployment",
    "execution_scenarios.py":
        "builds SimulatedBroker itself -- no make_broker, no credentials, no network",
}

#: Adapter methods that change something at the venue rather than read it. Checked inside service/
#: only: execution.py and the adapters under broker/ are where they are implemented.
VENUE_WRITES = {"submit_order", "cancel_order", "review_order", "pre_submit_check"}


def _sources():
    return sorted(p for p in PACKAGE.rglob("*.py") if "__pycache__" not in p.parts)


def _from_execution(path):
    """The names ``path`` imports out of execution.py, under whatever alias."""
    out = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[-1] == "execution":
            out |= {alias.name for alias in node.names}
    return out


def test_only_the_approvals_module_may_place_an_order():
    carriers = {str(p.relative_to(PACKAGE)) for p in _sources()
                if _from_execution(p) & ORDER_PLACING}
    assert carriers == {ALLOWED} | set(EXEMPT), (
        f"{sorted(ORDER_PLACING)} places real orders. {ALLOWED} is the live path; "
        f"{sorted(EXEMPT)} import it for the reasons recorded in EXEMPT. Found: {sorted(carriers)}."
        " Call it through the approvals module instead of importing it somewhere new, so that what"
        " can move money stays where an audit will look for it.")


def test_the_cli_exemption_lasts_exactly_as_long_as_the_stage_gate():
    """cli.py imports the same function and calls it. It is exempt above only because ``_load()``
    refuses every entry point it guards once execution_policy.stage is above 2, which is this
    deployment. If that stops being true, ct-approve can place an order again and cli.py is a
    second live path, so this fails rather than the exemption quietly widening."""
    from agentic_trading.profile import load_profiles

    stage = load_profiles(ROOT / "config").stage
    assert stage > 2, (
        f"execution_policy.stage is {stage}, so ct-approve is a working order path again; "
        "cli.py must not be exempt in EXEMPT while that is true")


def test_ct_approve_refuses_before_it_ever_reaches_a_venue(monkeypatch):
    """The refusal has to come before the broker is built, or the exemption would still open a
    session against a real venue on the way to being turned away."""
    from agentic_trading import cli

    monkeypatch.setattr(cli, "make_broker", lambda *_a, **_k: pytest.fail(
        "ct-approve built a broker before refusing"))
    assert cli.approve_main(["any-proposal-id", "--config-dir", str(ROOT / "config")]) == 2


def test_no_module_in_service_asks_a_venue_to_do_something():
    offenders = []
    for path in sorted((PACKAGE / "service").glob("*.py")):
        if str(path.relative_to(PACKAGE)) == ALLOWED:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Attribute) and node.attr in VENUE_WRITES:
                offenders.append(f"{path.name}:{node.lineno} .{node.attr}()")
    assert not offenders, (
        f"only {ALLOWED} may ask a venue to place, change or cancel an order; found {offenders}")


def test_the_convention_is_still_about_the_function_that_places_the_order():
    """Without this, renaming or moving ``execution.execute`` would leave the tests above passing
    while guarding nothing."""
    from agentic_trading import execution
    from agentic_trading.service import approvals

    assert approvals.execute_proposal is execution.execute
    assert "submit_order" in Path(execution.__file__).read_text()
