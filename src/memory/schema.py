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
    "Topic": "name",
}

# What an Event may be ABOUT *by label*. Still closed, because the label is
# interpolated into the query. The curator links a turn to entities it
# mentions without naming a label at all -- see link_event_entities, which
# resolves by name and joins on internal id, so nothing is interpolated.
ENTITY_LABELS = ("Person", "Project", "System", "Decision")

# Labels that are never "a thing someone mentions", so they are excluded
# wherever a turn is linked to what it talked about. Three groups: plumbing
# (Event, Session, Lookup, Agent), the topic index itself, and behavioural
# nodes whose `name` is an internal handle rather than something a person
# refers to -- a Procedure called "comment every line" would otherwise be
# offered to the curator as an entity to mention.
NON_ENTITY_LABELS = ("Event", "Session", "Lookup", "Agent", "Topic",
                     "Procedure", "Insight", "Task", "Ticket")


def not_entity_clause(var="n"):
    """Cypher excluding the labels above for `var`. Built from the tuple
    rather than written out, so the three call sites cannot drift apart."""
    return " AND ".join(f"NOT {var}:{label}" for label in NON_ENTITY_LABELS)


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
    # Every turn resolves its topics by name, so this one is read on the
    # hot path rather than only by the demo.
    ("Topic", "name"),
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
    "Task", "Ticket", "Lookup", "Topic",
})

# Likewise for edges: these carry behaviour elsewhere in the system.
RESERVED_PREDICATES = frozenset({
    "HAS_EVENT", "PERFORMED", "ABOUT", "RECORDED_IN", "LEARNED_FROM",
    "DERIVED_FROM", "MADE", "SUPERSEDES", "CREATED", "ASSIGNED_TO",
    "CONCERNS", "FILED", "BLOCKS", "CITES", "APPLIES_TO",
    "READ", "RETURNED", "ON_TOPIC",
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
