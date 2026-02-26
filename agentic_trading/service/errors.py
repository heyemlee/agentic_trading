"""The two errors every product operation raises.

They live apart from the package that assembles the domains so that each domain module can
import them without importing its own package.
"""
from __future__ import annotations


class ServiceError(RuntimeError):
    """A product-level failure with a message meant for a person."""



class ServiceConflict(ServiceError):
    """A stale version or conflicting retry; fetch current state before acting."""

