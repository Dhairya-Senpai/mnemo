"""The curator: a second pass whose only job is to write to the graph.

Why this exists at all. Until now one model call did two jobs at once --
hold a conversation *and* curate the memory -- and the second job lost
whenever the first was interesting. docs/TESTING.md section 5 is the
evidence: "dr appointment" stored as `dentist`, the same allergy sentence
producing `IS_ALLERGIC_TO` on one run and `HAS_ALLERGY` on the next. And the
quieter failure, which is worse: a standing rule the person clearly stated
was simply never written, because the model answered the question in front
of it and moved on. Nothing in the system noticed.

So the jobs are split. The Planner converses and reads. The curator writes
facts, rules, topics and mentions. One writer, one schema, one retry -- and
no dedup problem, because there is only one thing writing.

Two properties it must have, both learned the hard way:

* **It cannot break a turn.** Curation runs after the answer is already in
  the graph. Every failure here is swallowed and reported, never raised. A
  turn that fails to curate loses edges, not the reply.
* **It is told what the graph already holds.** The existing topics and
  entity names go into its prompt, so "lift control logic" is reused rather
  than re-coined as "elevator logic" next turn. Consistency is driven by the
  graph rather than by hoping the model is consistent with itself -- which
  section 5 of TESTING.md records that it is not.
"""

from dataclasses import dataclass, field
from pathlib import Path

from llm.structured import ask_json
from memory.schema import clean_predicate, not_entity_clause

PROMPT_PATH = Path(__file__).resolve().parent.parent.parent / "prompts" / "curator.txt"

REQUIRED_KEYS = ("topics", "entities", "facts", "rules")

# Bounds on what one turn may write. A model that decides every noun is a
# fact should cost us a messy turn, not a graph full of scenery.
MAX_TOPICS = 3
MAX_ENTITIES = 8
MAX_FACTS = 5
MAX_RULES = 2

# How much of the graph to show the curator. Enough to drive reuse, small
# enough that the prompt stays cheap on a local model.
MAX_KNOWN_TOPICS = 20
MAX_KNOWN_ENTITIES = 40


@dataclass
class Curation:
    """What one curation pass actually wrote. Reported, never asserted."""
    topics: list[str] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)
    facts: list[str] = field(default_factory=list)
    rules: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def wrote_anything(self) -> bool:
        return bool(self.topics or self.entities or self.facts or self.rules)


def load_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8").strip()


def known_context(store) -> str:
    """The topics and entity names the graph already holds, for reuse.

    Entities exclude the plumbing labels for the same reason the graph view
    does: an Event or a Lookup is not a thing anyone mentions.
    """
    topics = [t["topic"] for t in store.topic_index(limit=MAX_KNOWN_TOPICS)]
    rows = store._read(
        f"MATCH (n) WHERE n.name IS NOT NULL AND {not_entity_clause('n')} "
        f"RETURN DISTINCT n.name LIMIT $limit",
        limit=MAX_KNOWN_ENTITIES,
    )
    entities = [r[0] for r in rows]
    return (
        f"Topics already in this graph: "
        f"{', '.join(topics) if topics else '(none yet)'}\n"
        f"Known entities: "
        f"{', '.join(entities) if entities else '(none yet)'}\n"
    )


def build_prompt(store, user_text, reply_text) -> str:
    return (
        f"{known_context(store)}\n"
        f"--- the turn ---\n"
        f"Person: {user_text}\n\n"
        f"Assistant: {reply_text}\n"
        f"--- end of turn ---\n\n"
        f"Return the JSON object now."
    )


def _as_list(value, limit):
    """Tolerate a model returning a bare string where a list was asked for."""
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return value[:limit]


def apply_curation(store, session_id, agent, event_ids, data) -> Curation:
    """Write one curation result into the graph.

    Split out from the model call so the whole write path is testable with a
    plain dict and no adapter at all -- which is how every other memory
    behaviour in this project is tested.

    Each clause is guarded separately. One malformed fact should cost its own
    edge, not the topics and rules either side of it.
    """
    out = Curation()
    event_ids = [e for e in event_ids if e]

    topics = [t for t in _as_list(data.get("topics"), MAX_TOPICS)
              if isinstance(t, str) and t.strip()]
    if topics:
        for eid in event_ids:
            try:
                out.topics = store.link_event_topics(eid, topics) or out.topics
            except Exception as exc:
                out.skipped.append(f"topics on {eid}: {type(exc).__name__}")

    entities = [e for e in _as_list(data.get("entities"), MAX_ENTITIES)
                if isinstance(e, str) and e.strip()]
    if entities:
        for eid in event_ids:
            try:
                linked = store.link_event_entities(eid, entities)
                out.entities = sorted(set(out.entities) | set(linked))
            except Exception as exc:
                out.skipped.append(f"entities on {eid}: {type(exc).__name__}")

    for fact in _as_list(data.get("facts"), MAX_FACTS):
        if not isinstance(fact, dict):
            continue
        subject = str(fact.get("subject") or "").strip()
        predicate = str(fact.get("predicate") or "").strip()
        obj = str(fact.get("object") or "").strip()
        if not (subject and predicate and obj):
            out.skipped.append(f"fact missing a part: {fact}")
            continue
        try:
            # Normalise before the duplicate check, or "has appointment" and
            # HAS_APPOINTMENT read as two different facts and both get written.
            rel = clean_predicate(predicate)
            when = (str(fact.get("when")).strip()
                    if fact.get("when") else None)
            if store.open_fact_exists(subject, rel, obj, when=when):
                out.skipped.append(f"already known: {subject} {rel} {obj}")
                continue
            store.remember_fact(
                session_id, agent, subject, rel, obj,
                subject_type=str(fact.get("subject_type") or "Thing").strip()
                             or "Thing",
                object_type=str(fact.get("object_type") or "Thing").strip()
                            or "Thing",
                when=when)
            out.facts.append(f"{subject} {rel} {obj}")
        except Exception as exc:
            out.skipped.append(f"fact {subject} {predicate} {obj}: "
                               f"{type(exc).__name__}: {exc}")

    for rule in _as_list(data.get("rules"), MAX_RULES):
        if not isinstance(rule, dict):
            continue
        name = str(rule.get("name") or "").strip()
        action = str(rule.get("action") or "").strip()
        trigger = str(rule.get("trigger") or "").strip()
        if not (name and action):
            out.skipped.append(f"rule missing a part: {rule}")
            continue
        try:
            if store.procedure_exists(name):
                out.skipped.append(f"rule already in force: {name}")
                continue
            # Scope is what lets a rule taught about Ledger Service fire for
            # Billing v2, a project the rule never names, through the USES
            # edge -- demo moment 4. An unscoped rule is not broken: it is
            # one that holds everywhere, which is the normal case for a
            # preference about how to work. See TESTING.md section 3b.
            scopes = (
                [("System", x) for x in _as_list(
                    rule.get("applies_to_systems"), MAX_ENTITIES)
                 if isinstance(x, str) and x.strip()]
                + [("Project", x) for x in _as_list(
                    rule.get("applies_to_projects"), MAX_ENTITIES)
                   if isinstance(x, str) and x.strip()]
            )
            store.learn_procedure(session_id, agent, name, trigger, action,
                                  applies_to=scopes)
            out.rules.append(name)
        except Exception as exc:
            out.skipped.append(f"rule {name}: {type(exc).__name__}: {exc}")

    return out


def curate_turn(store, adapter, session_id, agent, user_text, reply_text,
                event_ids, cache=None) -> Curation:
    """Read one finished turn and write what it taught into the graph.

    Never raises. The turn it is curating has already been answered and
    recorded; a curator that could take the reply down with it would be a
    worse trade than losing some edges.
    """
    try:
        data = ask_json(
            adapter,
            build_prompt(store, user_text, reply_text),
            required_keys=REQUIRED_KEYS,
            system=load_prompt(),
            cache=cache,
        )
    except Exception as exc:
        return Curation(error=f"{type(exc).__name__}: {exc}")

    try:
        return apply_curation(store, session_id, agent, event_ids, data)
    except Exception as exc:                      # belt and braces
        return Curation(error=f"{type(exc).__name__}: {exc}")
