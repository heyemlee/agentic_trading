"""Product operations, in plain Python.

Everything a frontend can do lives here. There is no HTTP in this module, so the CLI, a
scheduler and the API server all run the same code. Each function returns a plain dict shaped
for a screen, never an internal object, and every write path calls the same engine functions
the CLI uses so policy, Hard Risk, approval and revalidation cannot be bypassed.
"""

from __future__ import annotations

from .approvals import ApprovalsMixin
from .assistant import AssistantMixin
from .auto import AutoTradingMixin
from .base import ServiceBase
from .books import BooksMixin
from .configuration import ConfigurationMixin
from .errors import ServiceConflict, ServiceError
from .notifications import NotificationsMixin
from .research import ResearchMixin
from .system import SystemMixin

__all__ = ["Service", "ServiceConflict", "ServiceError"]


class Service(ApprovalsMixin, AssistantMixin, AutoTradingMixin, BooksMixin, ConfigurationMixin,
              NotificationsMixin, ResearchMixin, SystemMixin, ServiceBase):
    """One instance per process. Cheap to construct.

    Between calls it keeps at most one venue session, and only for reads that record nothing
    when the venue fails them (see :meth:`_read_venue`). An order, and the reconciliation of
    one, opens a session of its own every time.

    It also keeps the results of the three reads a console polls that are costly to repeat --
    the market regime, the venue's account, the priced portfolio -- each with the inputs it was
    computed from (see kept.py). The readings compute through methods looked up when they run.
    """
