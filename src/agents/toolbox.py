"""The memory tools, described for a model and dispatched to MemoryStore.

Two things worth knowing:

* **`agent` and `session_id` are bound here, not passed by the model.** The
  schemas below do not expose them, so a model cannot write an event as a
  different agent or into someone else's session. Provenance is therefore a
  property of the wiring rather than of the model's good behaviour.
* **Results are rendered small.** Tool results are resent on every subsequent
  turn, so a chatty tool costs tokens on every later call in the loop. Each
  renderer returns compact JSON, and list tools take a `limit`.

Roles get different tool sets (PLANNER_TOOLS / REVIEWER_TOOLS). A smaller
surface means fewer tokens and fewer wrong turns.
"""

import json
from dataclasses import dataclass
from typing import Any, Callable

from llm.types import ToolCall, ToolResult, ToolSpec
from memory.schema import FACT_ENDPOINTS


def _schema(properties: dict[str, Any], required: list[str] | None = None) -> dict:
    """JSON Schema in the shape strict mode requires."""
    return {
        "type": "object",
        "properties": properties,
        "required": required if required is not None else sorted(properties),
        "additionalProperties": False,
    }


_STR = {"type": "string"}
_INT = {"type": "integer"}


@dataclass(frozen=True)
class Tool:
    spec: ToolSpec
    handler: Callable[..., Any]


def _tool(name, description, properties, required, handler) -> Tool:
    return Tool(
        spec=ToolSpec(name=name, description=description,
                      parameters=_schema(properties, required)),
        handler=handler,
    )


# --------------------------------------------------------------- the catalogue
# Each handler takes (store, agent, session_id, **arguments) and returns a
# JSON-serialisable value.

def _build_catalogue() -> dict[str, Tool]:
    t = {}

    def add(name, desc, props, required, fn):
        t[name] = _tool(name, desc, props, required, fn)

    add("recall_recent",
        "Recent episodic events across all sessions, newest first. Use to see "
        "what has happened lately before answering.",
        {"limit": _INT}, [],
        lambda s, a, sid, limit=5: s.recall_recent(limit=limit))

    add("recall_related",
        "Everything currently known about one entity: the episodes that "
        "mentioned it and the facts it participates in.",
        {"label": {"type": "string",
                   "enum": ["Person", "Project", "System", "Decision"]},
         "name": _STR},
        ["label", "name"],
        lambda s, a, sid, label, name: s.recall_related(label, name))

    add("current_decisions",
        "Decisions currently in force, optionally for one project. A project "
        "has no status field: its state IS the set of live decisions affecting it.",
        {"project": _STR}, [],
        lambda s, a, sid, project=None: s.current_decisions(project=project))

    add("decision_history",
        "Walk the supersession chain back from a decision to show what it replaced.",
        {"decision_id": _STR}, ["decision_id"],
        lambda s, a, sid, decision_id: s.decision_history(decision_id))

    add("depends_on_chain",
        "Everything a project transitively depends on, following only "
        "currently-valid dependency edges.",
        {"project": _STR}, ["project"],
        lambda s, a, sid, project: s.depends_on_chain(project))

    add("critical_projects",
        "Run betweenness centrality over the dependency graph to find the "
        "projects most others sit behind -- the single points of failure. "
        "Answers 'what', not 'who': follow up with current_facts WORKED_ON "
        "to name the people exposed. If the result carries a caveat, say so "
        "in your answer: the algorithm cannot filter out retired edges.",
        {"limit": _INT}, [],
        lambda s, a, sid, limit=5: s.critical_projects(limit=limit))

    add("insight_sources",
        "The episodes a consolidated insight was built from. Use it to show "
        "your work when an insight is what you are relying on.",
        {"insight_id": _STR}, ["insight_id"],
        lambda s, a, sid, insight_id: s.insight_sources(insight_id))

    add("dependents_of",
        "Projects that depend ON this one. Use when something slips, to find "
        "what it breaks downstream. The reverse of depends_on_chain.",
        {"project": _STR}, ["project"],
        lambda s, a, sid, project: s.dependents_of(project))

    add("explain_path",
        "Shortest connection between two entities, as a readable chain. Use "
        "this to show WHY something is blocked rather than just asserting it.",
        {"from_label": {"type": "string",
                        "enum": ["Person", "Project", "System", "Decision"]},
         "from_name": _STR,
         "to_label": {"type": "string",
                      "enum": ["Person", "Project", "System", "Decision"]},
         "to_name": _STR},
        ["from_label", "from_name", "to_label", "to_name"],
        lambda s, a, sid, from_label, from_name, to_label, to_name:
            s.explain_path(from_label, from_name, to_label, to_name))

    add("current_facts",
        "Current edges of one relationship type, optionally pinned at either end.",
        {"rel_type": {"type": "string",
                      "enum": ["OWNS", "WORKED_ON", "USES", "DEPENDS_ON", "AFFECTS"]},
         "from_name": _STR, "to_name": _STR},
        ["rel_type"],
        lambda s, a, sid, rel_type, from_name=None, to_name=None:
            s.current_facts(rel_type, from_name=from_name, to_name=to_name))

    add("record_decision",
        "Record a decision with its reason. Pass supersedes when it replaces "
        "an earlier decision; the old one is retired automatically.",
        {"title": _STR, "reason": _STR,
         "affects": {"type": "array", "items": _STR},
         "made_by": _STR, "supersedes": _STR},
        ["title", "reason"],
        lambda s, a, sid, title, reason, affects=None, made_by=None, supersedes=None:
            {"decision_id": s.record_decision(
                sid, a, title, reason, affects=affects or (),
                made_by=made_by, supersedes=supersedes)})

    add("remember_fact",
        "Record anything you learn as a structured fact: a subject, a "
        "relationship, and an object, each with a type you choose. Use this "
        "for everything that is not a decision, a rule, or a task - people, "
        "places, appointments, preferences, constraints, anything. Give "
        "`when` (YYYY-MM-DD) whenever the fact is about a date, so later "
        "questions about that date can find it.",
        {"subject": _STR, "subject_type": _STR, "predicate": _STR,
         "object": _STR, "object_type": _STR, "when": _STR},
        ["subject", "predicate", "object"],
        lambda s, a, sid, subject, predicate, object, subject_type="Thing",
               object_type="Thing", when=None:
            s.remember_fact(sid, a, subject, predicate, object,
                            subject_type=subject_type, object_type=object_type,
                            when=when))

    add("recall_entity",
        "Everything known about something by name, whatever type it is. Use "
        "this when you are not sure how something was classified.",
        {"name": _STR}, ["name"],
        lambda s, a, sid, name: s.recall_entity(name))

    add("what_is_on",
        "Every fact dated on a given day (YYYY-MM-DD). Check this before "
        "committing to a date, so you notice clashes.",
        {"date": _STR}, ["date"],
        lambda s, a, sid, date: s.what_is_on(date))

    # One retire tool, not two: the model should not have to know whether a
    # relationship belongs to the built-in set or was invented at write time.
    def _retire(s, a, sid, rel_type, from_name, to_name, reason):
        if rel_type.upper() in FACT_ENDPOINTS:
            n = s.retire_fact(sid, a, rel_type.upper(), from_name, to_name, reason)
        else:
            n = s.retire_open_fact(sid, a, from_name, rel_type, to_name, reason)
        return {"retired": n}

    add("retire_fact",
        "Close a fact that is no longer true. Works for any relationship, "
        "built-in or one you invented. Nothing is deleted; the old version "
        "stays readable with an end date.",
        {"rel_type": _STR, "from_name": _STR, "to_name": _STR, "reason": _STR},
        ["rel_type", "from_name", "to_name", "reason"],
        _retire)

    add("assert_fact",
        "Open a new fact, e.g. the new owner after a handover.",
        {"rel_type": {"type": "string",
                      "enum": ["OWNS", "WORKED_ON", "USES", "DEPENDS_ON", "AFFECTS"]},
         "from_name": _STR, "to_name": _STR},
        ["rel_type", "from_name", "to_name"],
        lambda s, a, sid, rel_type, from_name, to_name:
            {"event_id": s.assert_fact(sid, a, rel_type, from_name, to_name)})

    add("create_task",
        "Hand work to another agent. The task carries its own context, so the "
        "other agent reads it from the graph instead of being told.",
        {"assigned_to": _STR, "title": _STR,
         "concerns_decision": _STR, "concerns_project": _STR},
        ["assigned_to", "title"],
        lambda s, a, sid, assigned_to, title, concerns_decision=None,
               concerns_project=None:
            {"task_id": s.create_task(sid, a, assigned_to, title,
                                      concerns_decision=concerns_decision,
                                      concerns_project=concerns_project)})

    add("claim_task",
        "Atomically claim the oldest open task assigned to you. Returns null "
        "when there is nothing to claim.",
        {}, [],
        lambda s, a, sid: {"task_id": s.claim_task(a)})

    add("get_task_context",
        "What a task is about, read from the graph.",
        {"task_id": _STR}, ["task_id"],
        lambda s, a, sid, task_id: s.get_task_context(task_id))

    add("complete_task",
        "Close a claimed task and record the outcome.",
        {"task_id": _STR, "result": _STR}, ["task_id", "result"],
        lambda s, a, sid, task_id, result:
            {"completed": s.complete_task(sid, a, task_id, result)})

    add("learn_procedure",
        "Store a standing rule the user stated, so later runs apply it "
        "without being told again.",
        {"name": _STR, "trigger": _STR, "action": _STR,
         "applies_to_systems": {"type": "array", "items": _STR},
         "applies_to_projects": {"type": "array", "items": _STR}},
        ["name", "trigger", "action"],
        lambda s, a, sid, name, trigger, action,
               applies_to_systems=None, applies_to_projects=None:
            {"procedure_id": s.learn_procedure(
                sid, a, name, trigger, action,
                applies_to=[("System", x) for x in (applies_to_systems or [])]
                           + [("Project", x) for x in (applies_to_projects or [])])})

    add("get_applicable_procedures",
        "Rules in force for an entity, including rules reached through the "
        "systems a project uses. Check this before acting.",
        {"label": {"type": "string", "enum": ["Project", "System"]}, "name": _STR},
        ["label", "name"],
        lambda s, a, sid, label, name: s.get_applicable_procedures(label, name))

    add("create_ticket",
        "File a ticket recording that something is blocked. Always cite the "
        "decision that justifies it.",
        {"title": _STR, "blocks_project": _STR, "cites_decision": _STR},
        ["title", "blocks_project"],
        lambda s, a, sid, title, blocks_project, cites_decision=None:
            {"ticket_id": s.create_ticket(sid, a, title, blocks_project,
                                          cites_decision=cites_decision)})

    add("explain_answer",
        "Why an agent answered as it did earlier. Returns the reads behind a "
        "past answer, in order, and which nodes each one returned. Pass "
        "'about' with the name of a project, person, system or decision to "
        "find the answer that was based on it; omit it for the most recent. "
        "Use this when asked why you said something, or on what basis.",
        {"about": _STR}, [],
        lambda s, a, sid, about=None: s.explain_answer(about=about))

    return t


CATALOGUE = _build_catalogue()

PLANNER_TOOLS = (
    "recall_recent", "recall_entity", "recall_related", "what_is_on",
    "current_decisions", "decision_history", "explain_path", "insight_sources",
    "remember_fact", "record_decision", "create_task", "learn_procedure",
    "retire_fact", "explain_answer",
)

REVIEWER_TOOLS = (
    "claim_task", "get_task_context", "recall_entity", "recall_related",
    "what_is_on", "current_decisions", "decision_history", "depends_on_chain",
    "dependents_of", "explain_path", "critical_projects", "insight_sources",
    "current_facts", "get_applicable_procedures", "remember_fact",
    "retire_fact", "assert_fact", "create_ticket", "complete_task",
    "explain_answer",
)


# Keys whose string values identify something in the graph. Harvested from a
# tool's arguments and its result, then resolved against the graph by
# MemoryStore.record_lookup -- a citation survives only if a node really bears
# that key, so this list being generous costs nothing but a failed match.
_ID_KEYS = frozenset({"name", "id", "project", "from", "to"})

# Keys holding a bare list of names rather than dicts (explain_path's chain).
_LIST_KEYS = frozenset({"chain"})

# Reads that are themselves about the trace. Recording them would make every
# question about past reasoning add to the reasoning being questioned, and
# each explanation would cite the last one.
_UNTRACED = frozenset({"explain_answer"})


def citation_keys(value, _out=None) -> list[str]:
    """Pull every identifier out of a tool's arguments or result.

    Deliberately dumb and schema-free: one walker for all twenty-odd tools
    rather than a per-tool table that would drift out of date the first time
    a return shape changed.
    """
    out = [] if _out is None else _out
    if isinstance(value, dict):
        for key, inner in value.items():
            if key in _ID_KEYS and isinstance(inner, str):
                out.append(inner)
            elif key in _LIST_KEYS and isinstance(inner, list):
                out.extend(x for x in inner if isinstance(x, str))
            else:
                citation_keys(inner, out)
    elif isinstance(value, list):
        for item in value:
            citation_keys(item, out)
    return out


class Toolbox:
    """The tools one agent may use, bound to that agent and session."""

    def __init__(self, store, agent: str, session_id: str,
                 allowed: tuple[str, ...] | None = None):
        self.store = store
        self.agent = agent
        self.session_id = session_id
        names = allowed if allowed is not None else tuple(CATALOGUE)
        unknown = [n for n in names if n not in CATALOGUE]
        if unknown:
            raise ValueError(f"unknown tool(s): {unknown}")
        self.names = names
        # Lookup ids recorded during the current turn. The agent loop reads
        # this to point its answer at the reads behind it, then clears it.
        self.lookups: list[str] = []

    def specs(self) -> list[ToolSpec]:
        return [CATALOGUE[n].spec for n in self.names]

    def dispatch(self, call: ToolCall) -> ToolResult:
        """Run one tool call. Errors come back as results, not exceptions.

        A raised exception would end the turn; an error result lets the model
        see what went wrong and try something else, which is usually what we
        want from a wrong argument or a missing entity.
        """
        if call.name not in self.names:
            return ToolResult(
                call_id=call.id,
                content=f"Error: no tool named {call.name!r}. "
                        f"Available: {', '.join(self.names)}",
                is_error=True,
            )
        # A tool is a read if it did not write. Comparing MemoryStore's own
        # write counter is self-maintaining: a hand-kept list of write tools
        # would silently go stale the first time a tool changed, and the
        # trace would quietly stop recording something.
        writes_before = self.store.writes
        try:
            value = CATALOGUE[call.name].handler(
                self.store, self.agent, self.session_id, **call.arguments
            )
        except TypeError as e:
            return ToolResult(call_id=call.id,
                              content=f"Error: bad arguments for {call.name}: {e}",
                              is_error=True)
        except Exception as e:
            return ToolResult(call_id=call.id,
                              content=f"Error: {call.name} failed: "
                                      f"{type(e).__name__}: {e}",
                              is_error=True)
        if self.store.writes == writes_before and call.name not in _UNTRACED:
            # A read. Mutating tools are skipped because they already leave
            # an Event of their own -- recording both would double-count.
            self._trace(call, value)
        return ToolResult(call_id=call.id, content=_render(value))

    def _trace(self, call: ToolCall, value) -> None:
        """Write one Lookup for a read the agent just made.

        Failing to record a trace must never fail the turn: the answer is
        worth more than the audit of it, so this swallows its own errors.
        """
        try:
            keys = citation_keys(call.arguments) + citation_keys(value)
            self.lookups.append(self.store.record_lookup(
                self.session_id, self.agent, call.name,
                json.dumps(call.arguments, separators=(",", ":"), default=str),
                keys=keys,
            ))
        except Exception:
            pass


def _render(value) -> str:
    """Compact JSON. Tool results are resent every turn, so size matters."""
    if value is None:
        return "null"
    if isinstance(value, str):
        return value
    return json.dumps(value, separators=(",", ":"), default=str)
