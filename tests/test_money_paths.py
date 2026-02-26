"""What can place an order lives in one module of service/, and these tests say so.

Splitting service/ by domain was worth doing partly so that auditing "what can move money" is
auditing one file. Nothing in the language holds that: a later feature is free to import
``execution.execute`` into whichever domain module it happens to be working in, and nobody would
notice. These tests notice.
"""
from __future__ import annotations

import ast
from pathlib import Path

SERVICE = Path(__file__).resolve().parent.parent / "agentic_trading" / "service"

#: The one function in the package that reaches ``BrokerAdapter.submit_order``. Everything else
#: in execution.py either reads the venue or records what a submission did; ``reconcile_proposal``
#: is deliberately not here, because it never submits, reviews or cancels (execution.py).
ORDER_PLACING = {"execute"}

#: The adapter methods that change something at the venue rather than read it.
VENUE_WRITES = {"submit_order", "cancel_order", "review_order", "pre_submit_check"}

#: The module that may hold both.
ALLOWED = "approvals.py"


def _modules():
    return sorted(p for p in SERVICE.glob("*.py"))


def _from_execution(tree):
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[-1] == "execution":
            out |= {alias.name for alias in node.names}
    return out


def test_only_the_approvals_module_imports_what_places_an_order():
    carriers = sorted(p.name for p in _modules()
                      if _from_execution(ast.parse(p.read_text())) & ORDER_PLACING)
    assert carriers == [ALLOWED], (
        f"{sorted(ORDER_PLACING)} places real orders, so service/{ALLOWED} is the only module "
        f"that may import it; found it in {carriers}. Call it through the approvals module "
        "instead of importing it here, so that what can move money stays in one file.")


def test_no_other_domain_module_asks_a_venue_to_do_something():
    offenders = []
    for path in _modules():
        if path.name == ALLOWED:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Attribute) and node.attr in VENUE_WRITES:
                offenders.append(f"{path.name}:{node.lineno} .{node.attr}()")
    assert not offenders, (
        f"only service/{ALLOWED} may ask a venue to place, change or cancel an order; "
        f"found {offenders}")


def test_the_convention_is_still_about_the_function_that_places_the_order():
    """Without this, renaming or moving ``execution.execute`` would leave the tests above
    passing while guarding nothing."""
    from agentic_trading import execution
    from agentic_trading.service import approvals

    assert approvals.execute_proposal is execution.execute
    assert "submit_order" in Path(execution.__file__).read_text()
