"""The HTTP layer.

``app`` is a thin FastAPI wrapper over :mod:`agentic_trading.service`, which holds every
product operation as plain Python. The CLI, a scheduler and this API all share that one
implementation, so nothing here reimplements policy, risk or execution: every write path goes
through the same functions the CLI uses, and a frontend cannot bypass Hard Risk, human
approval, revalidation or the Micro-Live capital boundary.
"""
