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
    ("Lookup", "id"),
]


# --------------------------------------------------------------- open layer
#
# The tables above are the *behavioural* core: types the system does something
# with (supersession, rule matching, atomic claiming, consolidation). They stay
# closed, because code depends on their shape.
#
# Everything else a user might say is an ordinary fact, and the agent gets to
# invent the type at write time. That is the difference between a memory and a
# form with fixed fields.
#
# Cypher cannot parameterise a label or a relationship type, so model output
# that becomes one must be validated rather than escaped. These patterns are
# the whole of that defence: anything not matching is refused outright, so a
# label can never carry a brace, a quote, or a Cypher keyword into a query.

import re

_LABEL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,30}$")
_PREDICATE_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,40}$")

# Open facts may not impersonate a behavioural type. A node labelled Decision
# would be picked up by current_decisions() and by supersession logic that
# expects properties an open fact does not have.
RESERVED_LABELS = frozenset({
    "Agent", "Session", "Event", "Decision", "Procedure", "Insight",
    "Task", "Ticket", "Lookup",
})

# Likewise for edges: these carry behaviour elsewhere in the system.
RESERVED_PREDICATES = frozenset({
    "HAS_EVENT", "PERFORMED", "ABOUT", "RECORDED_IN", "LEARNED_FROM",
    "DERIVED_FROM", "MADE", "SUPERSEDES", "CREATED", "ASSIGNED_TO",
    "CONCERNS", "FILED", "BLOCKS", "CITES", "APPLIES_TO",
    "READ", "RETURNED",
}) | set(FACT_ENDPOINTS)


def clean_label(label: str) -> str:
    """Validate a model-supplied node label, or refuse it.

    Returns the label unchanged on success. Never attempts to repair input:
    a label that needs repairing is one we do not understand, and guessing
    would put unvalidated text into a query.
    """
    text = (label or "").strip()
    if not _LABEL_RE.match(text):
        raise ValueError(
            f"invalid type {label!r}: use a short name starting with a letter, "
            f"letters digits and underscores only (e.g. Appointment, Vendor)")
    if text in RESERVED_LABELS:
        raise ValueError(
            f"{text!r} is a built-in type with its own behaviour and cannot be "
            f"used for a plain fact; use the dedicated tool for it")
    return text


def clean_predicate(predicate: str) -> str:
    """Normalise a model-supplied relationship name, or refuse it.

    Spaces and hyphens become underscores and the result is upper-cased, so
    "has appointment" and "HAS_APPOINTMENT" land on the same edge type rather
    than fragmenting the graph into near-duplicates.
    """
    text = re.sub(r"[\s\-]+", "_", (predicate or "").strip()).upper()
    if not _PREDICATE_RE.match(text):
        raise ValueError(
            f"invalid relationship {predicate!r}: use words and spaces or "
            f"underscores only (e.g. 'has appointment', 'reports to')")
    if text in RESERVED_PREDICATES:
        raise ValueError(
            f"{text!r} is a built-in relationship with its own behaviour; "
            f"use the dedicated tool for it")
    return text


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
