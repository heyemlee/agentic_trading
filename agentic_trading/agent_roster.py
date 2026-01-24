"""The research team as one table: upstream node, report file, and console identity.

Three consumers read this and would otherwise each keep their own copy:

* the progress events written while a run is in flight, which only know the upstream
  LangGraph node name and need the role it belongs to,
* the roster the console draws its desks from, including which analysts a given
  instrument type actually starts, and
* the finished-run trace, whose report files are named per stage.

Upstream owns the node names, in ``tradingagents/graph/setup.py`` and
``analyst_execution.py``. A test asserts this table still covers every agent node the
pinned commit builds, so an upstream rename is a red test rather than a desk that
quietly stops lighting up. ``analyst`` is the upstream selection key for the four
analysts and ``None`` for the eight roles that always run.
"""

from __future__ import annotations

from typing import Any

#: group -> the order stages actually execute in, and the report directory that holds them.
GROUPS: tuple[tuple[str, str], ...] = (
    ("analysts", "1_analysts"),
    ("research", "2_research"),
    ("trading", "3_trading"),
    ("risk", "4_risk"),
    ("portfolio", "5_portfolio"),
)

#: key       console identity, stable across upstream renames
#: node      upstream LangGraph node name, the only thing a progress event carries
#: report    report file stem under runs/<id>/reports/<group dir>/
#: analyst   upstream analyst selection key, or None for a role that always runs
ROLES: tuple[dict[str, Any], ...] = (
    {"key": "market", "group": "analysts", "node": "Market Analyst",
     "report": "market", "analyst": "market"},
    {"key": "social", "group": "analysts", "node": "Sentiment Analyst",
     "report": "social", "analyst": "social"},
    {"key": "news", "group": "analysts", "node": "News Analyst",
     "report": "news", "analyst": "news"},
    {"key": "fundamentals", "group": "analysts", "node": "Fundamentals Analyst",
     "report": "fundamentals", "analyst": "fundamentals"},
    {"key": "bull", "group": "research", "node": "Bull Researcher",
     "report": "bull", "analyst": None},
    {"key": "bear", "group": "research", "node": "Bear Researcher",
     "report": "bear", "analyst": None},
    {"key": "manager", "group": "research", "node": "Research Manager",
     "report": "manager", "analyst": None},
    {"key": "trader", "group": "trading", "node": "Trader",
     "report": "trader", "analyst": None},
    {"key": "aggressive", "group": "risk", "node": "Aggressive Analyst",
     "report": "aggressive", "analyst": None},
    {"key": "neutral", "group": "risk", "node": "Neutral Analyst",
     "report": "neutral", "analyst": None},
    {"key": "conservative", "group": "risk", "node": "Conservative Analyst",
     "report": "conservative", "analyst": None},
    {"key": "portfolio", "group": "portfolio", "node": "Portfolio Manager",
     "report": "decision", "analyst": None},
)

#: upstream node name -> console role key. Built once; a node this run does not know
#: yields None, which the caller records verbatim rather than guessing at.
BY_NODE: dict[str, str] = {role["node"]: role["key"] for role in ROLES}

#: report file stem -> console role key, for reading a finished run's report tree.
BY_REPORT: dict[str, str] = {role["report"]: role["key"] for role in ROLES}


def role_for_node(node: str | None) -> str | None:
    """The console role a LangGraph node belongs to, or None if upstream added one."""
    return BY_NODE.get(node) if node else None
