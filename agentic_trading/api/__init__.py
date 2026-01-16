"""Frontend-facing application interface.

Two layers, deliberately separated:

* ``service`` is plain Python over the existing engine. It has no HTTP in it, so the CLI, a
  scheduler and the API all share one implementation of every product operation.
* ``app`` is a thin FastAPI wrapper that exposes ``service`` over HTTP and publishes an
  OpenAPI schema a frontend can generate against.

Nothing here reimplements policy, risk or execution. Every write path goes through the same
functions the CLI uses, so a frontend cannot bypass Hard Risk, human approval, revalidation or
the Micro-Live capital boundary.
"""

from .status import (
    MicroLiveStatus,
    ProposalStatus,
    SystemStatus,
    micro_live_status,
    proposal_status,
)

__all__ = ["MicroLiveStatus", "ProposalStatus", "SystemStatus", "micro_live_status", "proposal_status"]
