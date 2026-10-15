"""Compare two graph snapshots: what was added, retired, and linked.

This is how the UI's memory-changes panel knows what happened. It diffs the
graph itself rather than trusting an agent's account of what it did, which
matters because the interesting failure is an agent that says it recorded
something and did not.

"Retired" is the category worth watching. Nothing in this system is ever
deleted, so a fact leaving the current view always shows up here as a
validity window closing, never as a disappearance.
"""

from dataclasses import dataclass, field


@dataclass
class Changes:
    added_nodes: list = field(default_factory=list)
    added_edges: list = field(default_factory=list)
    retired_nodes: list = field(default_factory=list)
    retired_edges: list = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.added_nodes or self.added_edges
                    or self.retired_nodes or self.retired_edges)

    @property
    def counts(self) -> dict:
        return {
            "added nodes": len(self.added_nodes),
            "added links": len(self.added_edges),
            "retired facts": len(self.retired_edges) + len(self.retired_nodes),
        }

    def lines(self) -> list[str]:
        """Human-readable summary, newest category first."""
        out = []
        for n in self.added_nodes:
            out.append(f"+ {n['label']}: {n['name']}")
        for e in self.added_edges:
            out.append(f"+ {e['from']} -[{e['type']}]-> {e['to']}")
        for n in self.retired_nodes:
            out.append(f"~ retired {n['label']}: {n['name']} "
                       f"(valid_to {n['valid_to']})")
        for e in self.retired_edges:
            out.append(f"~ retired {e['from']} -[{e['type']}]-> {e['to']} "
                       f"(valid_to {e['valid_to']})")
        return out


def diff_snapshots(before, after) -> Changes:
    """What changed between two MemoryStore.snapshot() results."""
    changes = Changes()

    for key, node in after["nodes"].items():
        if key not in before["nodes"]:
            changes.added_nodes.append(node)
        elif before["nodes"][key]["valid_to"] is None and node["valid_to"] is not None:
            changes.retired_nodes.append(node)

    for key, edge in after["edges"].items():
        if key not in before["edges"]:
            changes.added_edges.append(edge)
        elif before["edges"][key]["valid_to"] is None and edge["valid_to"] is not None:
            changes.retired_edges.append(edge)

    return changes
