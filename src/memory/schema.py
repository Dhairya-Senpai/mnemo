"""Schema constants shared by the seeder and the memory tools.

Single definition on purpose. Relationship types and node labels get
interpolated into Cypher query strings -- Cypher cannot parameterise either --
so they must come from these tables and never from caller input or data files.
Values always travel as query parameters.

Reference: docs/SCHEMA.md (source of truth for the model itself).
"""

# Versioned world facts: relationship type -> (from label, to label).
# Every one of these carries valid_from / valid_to / source_event.
FACT_ENDPOINTS = {
    "OWNS":       ("Person", "Project"),
    "WORKED_ON":  ("Person", "Project"),
    "USES":       ("Project", "System"),
    "DEPENDS_ON": ("Project", "Project"),
    "AFFECTS":    ("Decision", "Project"),
}

# Facts keyed by `name`; Decision is keyed by `id` instead.
KEY_PROPERTY = {
    "Person": "name",
    "Project": "name",
    "System": "name",
    "Decision": "id",
    "Procedure": "id",
    "Insight": "id",
    "Event": "id",
    "Session": "id",
    "Task": "id",
    "Ticket": "id",
    "Agent": "name",
}

# What an Event may be ABOUT.
ENTITY_LABELS = ("Person", "Project", "System", "Decision")

# Closed set of Event.kind values.
EVENT_KINDS = ("message", "decision", "action", "handoff", "correction", "consolidation")

# What a Procedure may apply to.
PROCEDURE_SCOPES = ("Project", "System")

# (label, property) pairs indexed at seed time. db.indexes() reports one row
# per label, so this list produces fewer rows than entries.
INDEXES = [
    ("Event", "id"), ("Event", "ts"),
    ("Session", "id"),
    ("Task", "id"), ("Task", "status"),
    ("Ticket", "id"),
    ("Person", "name"), ("Project", "name"), ("System", "name"),
    ("Decision", "id"), ("Procedure", "id"), ("Insight", "id"),
]


def check_label(label, allowed, what="label"):
    """Reject any label that is not in a known-good tuple.

    Called before interpolating a label into a query string.
    """
    if label not in allowed:
        raise ValueError(f"unsupported {what} {label!r}; expected one of {allowed}")
    return label


def check_fact_type(rel_type):
    if rel_type not in FACT_ENDPOINTS:
        raise ValueError(
            f"unsupported fact type {rel_type!r}; expected one of {tuple(FACT_ENDPOINTS)}"
        )
    return rel_type
