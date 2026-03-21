"""Automatic trading: a plan a person confirms once, carried out inside it by deterministic code.

The plan is docs/todo.md. What is here, and the line it does not cross:

* ``modes``     the three modes and, for each, why it is or is not open yet;
* ``plan``      what a person confirms (capital, a loss line, a mode) and what code may then adjust
                (strategy parameters), with every figure held to the risk profile's own limits;
* ``paper``     the simulated account: the same rules and the same code path as the real one,
                with its own state and a venue that cannot be the real one.

Nothing in this package places an order. An automatic order is an ordinary proposal, approved
through ``service/approvals.py`` like any other, so the Hard Risk Layer, revalidation, the
capital reservation and reconciliation all stand in front of it exactly as they stand in front
of a person's. Nothing here can widen a limit either: a plan reads the limits, and where they
leave no room it says so.
"""
