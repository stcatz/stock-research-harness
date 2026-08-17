"""Explicit, auditable data-ingestion entry points.

Ingestion is intentionally separate from the research engine.  The engine only
consumes immutable normalized snapshots.
"""

from .sec import HttpResponse, SecClient, collect_sec_snapshot

__all__ = ["HttpResponse", "SecClient", "collect_sec_snapshot"]
