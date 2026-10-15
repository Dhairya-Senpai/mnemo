"""Graph-backed agent memory on FalkorDB.

Everything the agents are allowed to remember or recall goes through
MemoryStore. See docs/SCHEMA.md for the graph model.
"""

from .diff import Changes, diff_snapshots
from .store import MemoryStore, utc_now

__all__ = ["MemoryStore", "utc_now", "diff_snapshots", "Changes"]
