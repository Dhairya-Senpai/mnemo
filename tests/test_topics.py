"""Conversation connected to the graph: topics, mentions, and the curator.

    python tests/test_topics.py           # needs FalkorDB on localhost:6379

Why this suite exists. Every message either side of a conversation was
already stored in full -- `Event.summary` holds the verbatim text, which is
how the UI rebuilds a transcript from FalkorDB rather than from browser
state. What was missing is that none of it was *connected*. Measured on the
three workspaces in real use, **47 of 47 utterances had no outgoing edge**:
every word stored, and not one message reachable from anything it was about.

The cause was a single omitted argument. `record_event` accepts `about`,
which wires an episode to the entities it touched, and the agent loop never
passed one. A fact written by `remember_fact` got its ABOUT edges; the
message that taught it got none.

So two things are tested here:

* **The connection itself** -- topics, mentions, and the queries that make a
  past conversation findable by subject rather than by its opening line.
* **The curator** -- the second pass that writes them. The Planner converses
  and reads; the curator writes facts, rules and topics. One writer, one
  schema, and no dedup race.

The curator's write path is tested with plain dicts and no adapter at all,
which is how every other memory behaviour in this project is tested. Where a
model is involved it is scripted, so this proves the mechanism and says
nothing about whether a real model extracts well.
"""

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from agents import build_planner  # noqa: E402
from agents.curate import apply_curation, build_prompt, known_context  # noqa: E402
from agents.toolbox import PLANNER_TOOLS  # noqa: E402
from llm.fake import FakeAdapter, calls, says  # noqa: E402
from memory import MemoryStore  # noqa: E402

from falkordb import FalkorDB  # noqa: E402

TENANT = "topics"
PASSED, FAILED = [], []


def test(fn):
    name = fn.__name__.replace("test_", "").replace("_", " ")
    try:
        fn()
    except AssertionError as e:
        FAILED.append((name, str(e) or "assertion failed"))
        print(f"[FAIL] {name}\n       {e}")
    except Exception as e:
        FAILED.append((name, f"{type(e).__name__}: {e}"))
        print(f"[ERROR] {name}\n       {type(e).__name__}: {e}")
    else:
        PASSED.append(name)
        print(f"[PASS] {name}")
    return fn


def empty_store():
    try:
        FalkorDB(host="localhost", port=6379).select_graph(
            f"tenant_{TENANT}").delete()
    except Exception:
        pass
    return MemoryStore(TENANT)


def curation(topics=(), entities=(), facts=(), rules=(), settles=""):
    """A curator result as JSON text, for scripting a fake adapter.

    Every key the curator requires must be here. Omitting one does not
    fail loudly -- `ask_json` validates, retries, and silently eats the
    next scripted reply, so the symptom is a confusing "script exhausted"
    three tests later.
    """
    return says(json.dumps({
        "topics": list(topics), "entities": list(entities),
        "facts": list(facts), "rules": list(rules), "settles": settles,
    }))


print("using tenant_topics\n")


# ------------------------------------------------- indexes on every workspace
@test
def test_a_conversation_workspace_gets_indexes_too():
    """Indexes used to be created only by the seeder.

    So the two demo graphs had all of them and every workspace made by
    talking had none -- the graphs actually in use were the unindexed ones.
    Same shape as the fixture blind spot in docs/TESTING.md section 1.
    """
    store = empty_store()
    assert store._read("CALL db.indexes() YIELD label RETURN label") == [], \
        "fixture should start with no graph at all"
    store.start_session(user="Siddhi", session_id="s-idx")
    labels = {r[0] for r in
              store._read("CALL db.indexes() YIELD label RETURN label")}
    assert "Topic" in labels, f"Topic.name must be indexed: {labels}"
    assert "Event" in labels and "Person" in labels, labels


@test
def test_indexing_twice_is_not_an_error():
    """FalkorDB has no CREATE INDEX IF NOT EXISTS -- verified, it is a parse
    error. A repeat raises "already indexed" and must be swallowed."""
    store = empty_store()
    store.start_session(user="Siddhi", session_id="s-a")
    second = MemoryStore(TENANT)          # fresh instance, same graph
    second.start_session(user="Siddhi", session_id="s-b")   # must not raise


@test
def test_reads_still_work_before_the_graph_exists():
    """ensure_indexes must not run in __init__.

    CREATE INDEX is a write, and a write brings the Redis key into being.
    Indexing at construction would mean a new workspace was never again read
    before it existed, quietly retiring the empty-key path that
    test_unseeded.py guards.
    """
    store = empty_store()
    assert store.list_sessions() == []
    assert store.topic_index() == []
    assert store.read_topic("anything")["turns"] == []


# ------------------------------------------------------------------- topics
@test
def test_a_turn_is_no_longer_an_island():
    store = empty_store()
    sid = store.start_session(user="Siddhi", session_id="s1")
    store.remember_fact(sid, "Planner", "Siddhi", "PREFERS", "Java",
                        object_type="Language")
    eid = store.record_event(sid, "Planner", "message",
                             "how do i design lift control logic",
                             speaker="user")
    before = store._read(
        "MATCH (e:Event {id:$e})-[:ABOUT|ON_TOPIC]->() RETURN count(*)",
        e=eid)[0][0]
    assert before == 0, "a bare message starts unconnected"

    store.link_event_topics(eid, ["lift control logic"])
    store.link_event_entities(eid, ["Java"])
    after = store._read(
        "MATCH (e:Event {id:$e})-[:ABOUT|ON_TOPIC]->() RETURN count(*)",
        e=eid)[0][0]
    assert after == 2, f"expected a topic and a mention, got {after}"


@test
def test_case_and_spacing_do_not_fragment_a_topic():
    """The cheap half of the problem docs/TESTING.md section 5 describes."""
    store = empty_store()
    sid = store.start_session(user="Siddhi", session_id="s1")
    e1 = store.record_event(sid, "Planner", "message", "a", speaker="user")
    e2 = store.record_event(sid, "Planner", "message", "b", speaker="user")
    store.link_event_topics(e1, ["Lift Control Logic"])
    store.link_event_topics(e2, ["  lift   control logic  "])
    assert store._read("MATCH (t:Topic) RETURN count(t)")[0][0] == 1, \
        "the same subject must land on one node"


@test
def test_mentions_only_link_to_things_that_already_exist():
    """Mentions connect a turn to what is known; facts create what is new.

    Without the split every noun a model emitted would mint a node and the
    graph would fill with scenery.
    """
    store = empty_store()
    sid = store.start_session(user="Siddhi", session_id="s1")
    store.remember_fact(sid, "Planner", "Siddhi", "PREFERS", "Java",
                        object_type="Language")
    eid = store.record_event(sid, "Planner", "message", "java and tuesday",
                             speaker="user")
    linked = store.link_event_entities(eid, ["Java", "Tuesday"])
    assert linked == ["Java"], f"only existing entities link: {linked}"
    assert store._read("MATCH (n {name:'Tuesday'}) RETURN count(n)")[0][0] == 0


@test
def test_a_standing_rule_is_not_offered_as_an_entity():
    """A Procedure's `name` is an internal handle, not a thing anyone
    mentions. It leaked into the curator's known-entity list until
    NON_ENTITY_LABELS covered it."""
    store = empty_store()
    sid = store.start_session(user="Siddhi", session_id="s1")
    store.learn_procedure(sid, "Planner", "comment every line",
                          "writing code", "comment each line")
    entity_line = [ln for ln in known_context(store).splitlines()
                   if ln.startswith("Known entities:")][0]
    assert "comment every line" not in entity_line, entity_line
    assert entity_line.endswith("(none yet)"), entity_line


@test
def test_adding_a_topic_to_a_missing_event_is_loud():
    """Bug 2.1's lesson: a MATCH that finds nothing makes the rest of a
    query a silent no-op, and a method that returns anyway is how a caller
    ends up with provenance pointing at nothing."""
    store = empty_store()
    store.start_session(user="Siddhi", session_id="s1")
    try:
        store.link_event_topics("evt-does-not-exist", ["x"])
    except LookupError:
        return
    raise AssertionError("expected LookupError for an unknown event")


# -------------------------------------------------------- finding it again
@test
def test_the_index_counts_turns_and_orders_by_recency():
    store = empty_store()
    sid = store.start_session(user="Siddhi", session_id="s1")
    for text in ("one", "two", "three"):
        store.link_event_topics(
            store.record_event(sid, "Planner", "message", text,
                               speaker="user"),
            ["lift control logic"])
    store.link_event_topics(
        store.record_event(sid, "Planner", "message", "later",
                           speaker="user"),
        ["rate limiter design"])

    index = store.topic_index()
    assert [t["topic"] for t in index][0] == "rate limiter design", index
    lift = [t for t in index if t["topic"] == "lift control logic"][0]
    assert lift["turns"] == 3, lift


@test
def test_the_index_ignores_the_agents_own_working_record():
    """Only utterances are turns of conversation."""
    store = empty_store()
    sid = store.start_session(user="Siddhi", session_id="s1")
    said = store.record_event(sid, "Planner", "message", "hi", speaker="user")
    internal = store.record_event(sid, "Planner", "action", "did a thing")
    store.link_event_topics(said, ["lift control logic"])
    store.link_event_topics(internal, ["lift control logic"])
    assert store.topic_index()[0]["turns"] == 1


@test
def test_a_paraphrased_topic_still_resolves():
    """FalkorDB's CONTAINS is a plain substring test, so "lift logic" does
    not match "lift control logic" -- verified. Paraphrasing the topic back
    is exactly what a model does, hence the word-overlap fallback."""
    store = empty_store()
    sid = store.start_session(user="Siddhi", session_id="s1")
    store.link_event_topics(
        store.record_event(sid, "Planner", "message", "x", speaker="user"),
        ["lift control logic"])
    assert store.resolve_topic("lift control logic") == "lift control logic"
    assert store.resolve_topic("lift") == "lift control logic"
    assert store.resolve_topic("Lift Logic") == "lift control logic"
    assert store.resolve_topic("tax return") is None


@test
def test_reading_a_topic_spans_sessions_and_returns_both_sides():
    """The point of indexing by subject rather than by session: work that
    ran across three chats comes back as one thread."""
    store = empty_store()
    s1 = store.start_session(user="Siddhi", session_id="s1")
    a = store.record_event(s1, "Planner", "message", "design a lift",
                           speaker="user")
    b = store.record_event(s1, "Planner", "message", "use a state machine",
                           speaker="Planner")
    s2 = store.start_session(user="Siddhi", session_id="s2")
    c = store.record_event(s2, "Planner", "message", "what about door timing",
                           speaker="user")
    for eid in (a, b, c):
        store.link_event_topics(eid, ["lift control logic"])

    result = store.read_topic("lift logic")
    assert result["topic"] == "lift control logic", result
    assert len(result["turns"]) == 3, result["turns"]
    assert {t["session"] for t in result["turns"]} == {"s1", "s2"}
    assert [t["speaker"] for t in result["turns"]] == \
        ["user", "Planner", "user"], "both sides, in order"


@test
def test_a_removed_conversation_does_not_come_back_through_a_topic():
    """Removing a chat has to close every way back in, or topics become a
    hole in the removal."""
    store = empty_store()
    sid = store.start_session(user="Siddhi", session_id="s-gone")
    store.link_event_topics(
        store.record_event(sid, "Planner", "message", "secret plan",
                           speaker="user"),
        ["lift control logic"])
    assert len(store.read_topic("lift control logic")["turns"]) == 1
    store.hide_session(sid)
    assert store.read_topic("lift control logic")["turns"] == [], \
        "a hidden conversation must not be readable through its topic"


@test
def test_an_unknown_topic_reports_what_was_asked():
    store = empty_store()
    store.start_session(user="Siddhi", session_id="s1")
    result = store.read_topic("nothing like this")
    assert result["topic"] is None and result["asked"] == "nothing like this"


# ------------------------------------------------------------- the curator
@test
def test_the_curator_writes_topics_mentions_facts_and_rules():
    store = empty_store()
    sid = store.start_session(user="Siddhi", session_id="s1")
    store.remember_fact(sid, "Planner", "Siddhi", "WRITES_IN", "Java",
                        object_type="Language")
    eid = store.record_event(sid, "Planner", "message",
                             "comment every line. fizzbuzz in java",
                             speaker="user")
    out = apply_curation(store, sid, "Planner", [eid], {
        "topics": ["FizzBuzz in Java"],
        "entities": ["Java"],
        "facts": [{"subject": "Siddhi", "predicate": "prefers",
                   "object": "Java", "object_type": "Language"}],
        "rules": [{"name": "comment every line", "trigger": "writing code",
                   "action": "add a comment to each line"}],
    })
    assert out.topics == ["fizzbuzz in java"], out
    assert out.entities == ["Java"], out
    assert out.facts == ["Siddhi PREFERS Java"], out.facts
    assert out.rules == ["comment every line"], out
    assert store.procedure_exists("comment every line")


@test
def test_a_follow_up_turn_inherits_the_conversations_topic():
    """Found on a live run, not in a test.

    Turn 1 "I'm working on lift control logic" was topiced correctly. Turn 2
    "what data structure should hold the pending floor requests?" came back
    with an empty topic list -- valid JSON, no error. A ten-turn design
    discussion would have left two turns findable and eight lost, which is
    the whole feature failing quietly for the case it was built for.
    """
    store = empty_store()
    sid = store.start_session(user="Siddhi", session_id="s1")
    first = store.record_event(sid, "Planner", "message",
                               "lift control logic in java", speaker="user")
    apply_curation(store, sid, "Planner", [first],
                   {"topics": ["lift control logic"], "entities": [],
                    "facts": [], "rules": []})

    follow = store.record_event(sid, "Planner", "message",
                                "what holds the pending floor requests?",
                                speaker="user")
    out = apply_curation(store, sid, "Planner", [follow],
                         {"topics": [], "entities": [], "facts": [],
                          "rules": []})
    assert out.topics == ["lift control logic"], out
    assert any("carried from the conversation" in s for s in out.skipped),         out.skipped
    assert store.topic_index()[0]["turns"] == 2


@test
def test_a_new_topic_is_not_overridden_by_the_old_one():
    """Carry-forward is a fallback, not a rule. A turn that names its own
    subject keeps it."""
    store = empty_store()
    sid = store.start_session(user="Siddhi", session_id="s1")
    a = store.record_event(sid, "Planner", "message", "lifts", speaker="user")
    apply_curation(store, sid, "Planner", [a],
                   {"topics": ["lift control logic"], "entities": [],
                    "facts": [], "rules": []})
    b = store.record_event(sid, "Planner", "message", "taxes", speaker="user")
    out = apply_curation(store, sid, "Planner", [b],
                         {"topics": ["tax return"], "entities": [],
                          "facts": [], "rules": []})
    assert out.topics == ["tax return"], out


@test
def test_carry_forward_does_nothing_in_a_brand_new_conversation():
    store = empty_store()
    sid = store.start_session(user="Siddhi", session_id="s1")
    eid = store.record_event(sid, "Planner", "message", "hello",
                             speaker="user")
    out = apply_curation(store, sid, "Planner", [eid],
                         {"topics": [], "entities": [], "facts": [],
                          "rules": []})
    assert out.topics == [], out
    assert store._read("MATCH (t:Topic) RETURN count(t)")[0][0] == 0,         "chit-chat must not invent a topic"


@test
def test_the_same_fact_twice_is_written_once():
    """A person restating something is ordinary, and remember_fact always
    CREATEs. Without the guard, "I prefer Java" said twice is two identical
    open edges and recall reports the preference twice."""
    store = empty_store()
    sid = store.start_session(user="Siddhi", session_id="s1")
    eid = store.record_event(sid, "Planner", "message", "java please",
                             speaker="user")
    data = {"topics": [], "entities": [], "rules": [],
            "facts": [{"subject": "Siddhi", "predicate": "PREFERS",
                       "object": "Java"}]}
    first = apply_curation(store, sid, "Planner", [eid], data)
    second = apply_curation(store, sid, "Planner", [eid], data)
    assert first.facts == ["Siddhi PREFERS Java"], first
    assert second.facts == [], second
    assert any("already known" in s for s in second.skipped), second.skipped
    assert store._read(
        "MATCH ()-[r:PREFERS]->() WHERE r.valid_to IS NULL "
        "RETURN count(r)")[0][0] == 1


@test
def test_the_same_fact_on_a_different_date_is_a_different_fact():
    """`when` is part of a fact's identity. Two appointments with the same
    doctor on different dates are both true -- the distinction
    retire_open_fact had to learn in bug 2.2."""
    store = empty_store()
    sid = store.start_session(user="Siddhi", session_id="s1")
    eid = store.record_event(sid, "Planner", "message", "two visits",
                             speaker="user")
    for day in ("2026-10-20", "2026-10-22"):
        apply_curation(store, sid, "Planner", [eid], {
            "topics": [], "entities": [], "rules": [],
            "facts": [{"subject": "Siddhi", "predicate": "HAS_APPOINTMENT",
                       "object": "doctor", "when": day}]})
    assert store._read(
        "MATCH ()-[r:HAS_APPOINTMENT]->() WHERE r.valid_to IS NULL "
        "RETURN count(r)")[0][0] == 2


@test
def test_a_rule_already_in_force_is_not_relearned():
    store = empty_store()
    sid = store.start_session(user="Siddhi", session_id="s1")
    eid = store.record_event(sid, "Planner", "message", "always java",
                             speaker="user")
    data = {"topics": [], "entities": [], "facts": [],
            "rules": [{"name": "use java", "trigger": "code",
                       "action": "write Java"}]}
    apply_curation(store, sid, "Planner", [eid], data)
    again = apply_curation(store, sid, "Planner", [eid], data)
    assert again.rules == [], again
    assert store._read(
        "MATCH (p:Procedure) WHERE p.valid_to IS NULL "
        "RETURN count(p)")[0][0] == 1


@test
def test_the_same_rule_worded_differently_is_not_relearned():
    """Found by use, in the browser, not by a test.

    The Planner restated a standing rule in its own reply -- "the standing
    rule is: any code I give you will have a comment on every line" -- and
    the curator read its own restatement as a new teaching. The graph ended
    up holding both "comment every line of code" and "always comment every
    line of code".

    That is TESTING.md section 3's problem in a new place: an agent's prose
    treated as evidence. It is worse for rules than for facts, because every
    duplicate is injected into every later prompt and the bloat compounds.
    """
    store = empty_store()
    sid = store.start_session(user="Siddhi", session_id="s1")
    eid = store.record_event(sid, "Planner", "message", "comments please",
                             speaker="user")
    apply_curation(store, sid, "Planner", [eid], {
        "topics": [], "entities": [], "facts": [],
        "rules": [{"name": "comment every line of code",
                   "trigger": "when providing code",
                   "action": "put a comment on every single line "
                             "explaining what it does"}]})
    out = apply_curation(store, sid, "Planner", [eid], {
        "topics": [], "entities": [], "facts": [],
        "rules": [{"name": "always comment every line of code",
                   "trigger": "when providing code",
                   "action": "include a comment on every single line "
                             "explaining what it does"}]})
    assert out.rules == [], out
    assert any("already in force as" in s for s in out.skipped), out.skipped
    assert store._read("MATCH (p:Procedure) WHERE p.valid_to IS NULL "
                       "RETURN count(p)")[0][0] == 1


@test
def test_a_genuinely_different_rule_is_still_learned():
    """The duplicate guard must not swallow a second, unrelated rule."""
    store = empty_store()
    sid = store.start_session(user="Siddhi", session_id="s1")
    eid = store.record_event(sid, "Planner", "message", "rules", speaker="user")
    apply_curation(store, sid, "Planner", [eid], {
        "topics": [], "entities": [], "facts": [],
        "rules": [{"name": "comment every line", "trigger": "code",
                   "action": "put a comment on every single line"}]})
    out = apply_curation(store, sid, "Planner", [eid], {
        "topics": [], "entities": [], "facts": [],
        "rules": [{"name": "metric units", "trigger": "measurements",
                   "action": "always answer in metric units never imperial"}]})
    assert out.rules == ["metric units"], out


@test
def test_the_curator_is_told_who_it_is_curating_for():
    """Found by use: a live run produced a Person node literally named
    "Person". The prompt said to attribute a fact to the person by name and
    never said what the name was. Agent.system_prompt had already learned
    this lesson; the curator had not."""
    store = empty_store()
    sid = store.start_session(user="Siddhi", session_id="s1")
    prompt = build_prompt(store, "I work on lifts", "Noted.", user="Siddhi")
    assert "Siddhi" in prompt, prompt[:200]
    assert "Person:" not in prompt, "the literal word is what it latched onto"


@test
def test_rules_in_force_are_shown_to_the_curator():
    store = empty_store()
    sid = store.start_session(user="Siddhi", session_id="s1")
    store.learn_procedure(sid, "Planner", "comment every line",
                          "writing code", "comment each line")
    assert "comment every line" in build_prompt(store, "hi", "hello")


@test
def test_a_scoped_rule_still_reaches_through_the_uses_edge():
    """Demo moment 4. A rule taught about a system must fire for a project
    that merely USES it -- a project the rule never names."""
    store = empty_store()
    sid = store.start_session(user="Siddhi", session_id="s1")
    store._write("CREATE (:Project {name:'Billing v2'}), "
                 "(:System {name:'Ledger Service'})")
    store.assert_fact(sid, "Planner", "USES", "Billing v2", "Ledger Service")
    eid = store.record_event(sid, "Planner", "message", "tell priya",
                             speaker="user")
    apply_curation(store, sid, "Planner", [eid], {
        "topics": [], "entities": [], "facts": [],
        "rules": [{"name": "notify-finance-lead",
                   "trigger": "a finance system is affected",
                   "action": "notify Priya Nair",
                   "applies_to_systems": ["Ledger Service"]}]})
    hits = store.get_applicable_procedures("Project", "Billing v2")
    assert hits and hits[0]["name"] == "notify-finance-lead", hits
    assert "via system" in hits[0]["matched"], hits


@test
def test_one_bad_fact_does_not_cost_the_rest_of_the_turn():
    """Each clause is guarded separately on purpose."""
    store = empty_store()
    sid = store.start_session(user="Siddhi", session_id="s1")
    eid = store.record_event(sid, "Planner", "message", "mixed", speaker="user")
    out = apply_curation(store, sid, "Planner", [eid], {
        "topics": ["something real"],
        "entities": [],
        "facts": [{"subject": "", "predicate": "BROKEN", "object": ""},
                  {"subject": "Siddhi", "predicate": "LIKES",
                   "object": "tea"}],
        "rules": [],
    })
    assert out.topics == ["something real"], out
    assert out.facts == ["Siddhi LIKES tea"], out.facts
    assert out.skipped, "the malformed fact should be reported, not silent"


@test
def test_a_curator_cannot_impersonate_a_behavioural_type():
    """ON_TOPIC and Topic are reserved, like every other type the system
    attaches behaviour to."""
    store = empty_store()
    sid = store.start_session(user="Siddhi", session_id="s1")
    eid = store.record_event(sid, "Planner", "message", "x", speaker="user")
    out = apply_curation(store, sid, "Planner", [eid], {
        "topics": [], "entities": [], "rules": [],
        "facts": [{"subject": "a", "predicate": "ON_TOPIC", "object": "b"}]})
    assert out.facts == [], out
    assert any("ON_TOPIC" in s for s in out.skipped), out.skipped


@test
def test_a_model_returning_a_string_instead_of_a_list_is_tolerated():
    store = empty_store()
    sid = store.start_session(user="Siddhi", session_id="s1")
    eid = store.record_event(sid, "Planner", "message", "x", speaker="user")
    out = apply_curation(store, sid, "Planner", [eid],
                         {"topics": "lift control logic", "entities": [],
                          "facts": [], "rules": []})
    assert out.topics == ["lift control logic"], out


@test
def test_the_curator_is_told_what_the_graph_already_holds():
    """Reuse is driven by the graph, not by hoping the model is consistent
    with itself -- which docs/TESTING.md section 5 records that it is not."""
    store = empty_store()
    sid = store.start_session(user="Siddhi", session_id="s1")
    store.remember_fact(sid, "Planner", "Siddhi", "WRITES_IN", "Java",
                        object_type="Language")
    store.link_event_topics(
        store.record_event(sid, "Planner", "message", "x", speaker="user"),
        ["lift control logic"])
    prompt = build_prompt(store, "and the doors?", "Door timing next.")
    assert "lift control logic" in prompt
    assert "Java" in prompt
    assert "and the doors?" in prompt and "Door timing next." in prompt


# ------------------------------------------------- the agent, end to end
@test
def test_the_planner_can_no_longer_write_facts_itself():
    """Decision (b): one writer. The Planner converses and reads."""
    assert "remember_fact" not in PLANNER_TOOLS
    assert "learn_procedure" not in PLANNER_TOOLS
    # Retirement stays: a person correcting something mid-conversation needs
    # it then, and the curator runs afterwards. It also preserves bug 2.2's
    # ordering -- retire during the turn, assert after it.
    assert "retire_fact" in PLANNER_TOOLS
    assert "read_topic" in PLANNER_TOOLS


@test
def test_a_turn_curates_itself_and_links_both_sides():
    """The question and the answer are about the same thing. Linking only
    one would leave half the conversation unreachable from its subject."""
    store = empty_store()
    sid = store.start_session(user="Siddhi", session_id="s1")
    turn = build_planner(store, sid, FakeAdapter([
        says("Use a state machine per car."),
        curation(topics=["lift control logic"]),
    ])).run("how do i design lift control logic")

    assert turn.curation.topics == ["lift control logic"], turn.curation
    linked = store._read(
        "MATCH (e:Event)-[:ON_TOPIC]->(:Topic) RETURN count(e)")[0][0]
    assert linked == 2, f"both the question and the answer: {linked}"


@test
def test_a_broken_curator_does_not_break_the_turn():
    """Curation runs after the answer is already in the graph. A curator
    that could take the reply down with it would be a worse trade than
    losing some edges."""
    store = empty_store()
    sid = store.start_session(user="Siddhi", session_id="s1")
    turn = build_planner(store, sid, FakeAdapter([
        says("Here is your answer."),
        says("I am afraid I cannot produce JSON today."),
        says("Still not JSON."),
    ])).run("anything")

    assert turn.text == "Here is your answer.", "the reply must survive"
    assert turn.curation.error, "the failure must be reported, not swallowed"
    assert turn.curation.wrote_anything is False
    assert len(store.session_transcript(sid)) == 2, \
        "both sides of the exchange are still recorded"


@test
def test_the_agent_can_see_earlier_turns_of_the_same_chat():
    """The worst defect found so far, and the simplest.

    `run` began every turn with `[Message(user_message)]` and nothing else,
    so the model had never seen what was said earlier in the same
    conversation. The UI shows a continuous thread; the model saw each
    message alone. Asked "can you add my uid to this code?" two messages
    after writing three thousand characters of Java, it replied "I don't
    see any code attached to your message" -- truthfully.
    """
    store = empty_store()
    sid = store.start_session(user="Siddhi", session_id="s1")
    llm = FakeAdapter([says("Here is the Java: class Solution {}"),
                       curation(topics=["array reordering"]),
                       says("Added."),
                       curation(topics=["array reordering"])])
    planner = build_planner(store, sid, llm)
    planner.run("give me the solution")
    turn = planner.run("add my uid at the top of this code")

    sent = [m for m in turn.messages if m.role in ("user", "assistant")]
    said = " ".join(m.text or "" for m in sent)
    assert "class Solution {}" in said,         f"the earlier answer must be replayed into the turn: {said[:300]}"
    assert "give me the solution" in said, "and the earlier question"


@test
def test_the_replayed_history_comes_from_the_graph():
    """Not from anything this process is holding. A fresh Agent object on
    the same session sees the same conversation, which is what makes this
    graph memory rather than a chat buffer."""
    store = empty_store()
    sid = store.start_session(user="Siddhi", session_id="s1")
    build_planner(store, sid, FakeAdapter([
        says("First answer."), curation()])).run("first question")

    fresh = build_planner(store, sid, FakeAdapter([
        says("Second answer."), curation()]))
    turn = fresh.run("second question")
    said = " ".join(m.text or "" for m in turn.messages if m.text)
    assert "First answer." in said and "first question" in said, said[:300]


@test
def test_a_long_code_answer_still_reaches_the_next_turn():
    """The exact shape that failed in the browser after the first fix.

    A 6,918-character Java answer, then "can you add my uid in this code?".
    Trimming by individual message kept the big assistant turn (the budget
    check is skipped while nothing is kept), then "yes" broke the loop, and
    the rule that a history may not begin on an assistant message popped
    the only thing left. Two correct guards, an empty history, and the
    agent answering "I don't see any code in your message" for the second
    time.
    """
    store = empty_store()
    sid = store.start_session(user="Siddhi", session_id="s1")
    store.record_event(sid, "Planner", "message", "what was problem 1569?",
                       speaker="user")
    store.record_event(sid, "Planner", "message", "It is a BST problem.",
                       speaker="Planner")
    store.record_event(sid, "Planner", "message", "yes", speaker="user")
    big = "\n".join(["class Solution {"] + ["    // line"] * 600 + ["}"])
    assert len(big) > 6000, len(big)
    store.record_event(sid, "Planner", "message", big, speaker="Planner")

    history = build_planner(store, sid,
                            FakeAdapter([says("ok"), curation()]))._history()
    said = " ".join(m.text for m in history)
    assert big in said, "the code the person is pointing at must survive"
    assert history[0].role == "user"
    assert history[-1].role == "assistant"


@test
def test_an_exchange_is_kept_whole_or_not_at_all():
    """Keeping half an exchange orphans a reply from its question."""
    store = empty_store()
    sid = store.start_session(user="Siddhi", session_id="s1")
    for i in range(8):
        store.record_event(sid, "Planner", "message", f"q{i} " + "x" * 2000,
                           speaker="user")
        store.record_event(sid, "Planner", "message", f"a{i} " + "y" * 2000,
                           speaker="Planner")
    history = build_planner(store, sid,
                            FakeAdapter([says("ok"), curation()]))._history()
    roles = [m.role for m in history]
    assert roles == ["user", "assistant"] * (len(roles) // 2), roles
    assert history[0].role == "user"


@test
def test_history_is_bounded_and_starts_on_a_user_turn():
    """A turn here can be four thousand characters of Java, and a real run
    recorded "reply hit the context limit (num_ctx=16384)"."""
    store = empty_store()
    sid = store.start_session(user="Siddhi", session_id="s1")
    for i in range(12):
        store.record_event(sid, "Planner", "message", f"q{i} " + "x" * 600,
                           speaker="user")
        store.record_event(sid, "Planner", "message", f"a{i} " + "y" * 600,
                           speaker="Planner")
    planner = build_planner(store, sid, FakeAdapter([says("ok"), curation()]))
    history = planner._history()
    assert history, "some history must survive"
    assert sum(len(m.text) for m in history) <= planner.HISTORY_CHARS + 1200
    assert history[0].role == "user",         "an assistant message with no question before it reads as the model "        "talking to itself"


@test
def test_a_preference_is_not_recorded_as_a_second_decision():
    """Found by use: a coding preference stated across a conversation
    produced three Decision nodes -- "Code language preference: Java
    first", "First preference language for code", "Code comments rule" --
    each a near-duplicate of a standing rule the curator had already
    stored."""
    store = empty_store()
    sid = store.start_session(user="Siddhi", session_id="s1")
    first = store.record_decision(sid, "Planner",
                                  "Code language preference: Java first",
                                  "the person asked for it")
    again = store.record_decision(sid, "Planner",
                                  "First preference language for code",
                                  "the person asked again")
    assert again == first, "the duplicate must return the existing decision"
    assert store._read("MATCH (d:Decision) WHERE d.valid_to IS NULL "
                       "RETURN count(d)")[0][0] == 1


@test
def test_superseding_a_decision_is_never_treated_as_a_duplicate():
    """Superseding is the sanctioned way to restate a decision and it keeps
    the history connected, so the guard must not block it."""
    store = empty_store()
    sid = store.start_session(user="Siddhi", session_id="s1")
    old = store.record_decision(sid, "Planner", "Delay launch to May",
                                "ledger blocked")
    new = store.record_decision(sid, "Planner", "Delay launch to May 15",
                                "ledger still blocked", supersedes=old)
    assert new != old
    assert store._read("MATCH (:Decision)-[:SUPERSEDES]->(:Decision) "
                       "RETURN count(*)")[0][0] == 1


@test
def test_a_rule_is_tied_to_the_subject_it_was_learned_under():
    """Every Procedure and Decision used to have degree 1 -- one edge, to
    the Event that made it -- while Topic nodes had ten or twelve. That is
    what "why is every node single?" was describing."""
    store = empty_store()
    sid = store.start_session(user="Siddhi", session_id="s1")
    eid = store.record_event(sid, "Planner", "message", "java please",
                             speaker="user")
    out = apply_curation(store, sid, "Planner", [eid], {
        "topics": ["array reordering"], "entities": [], "facts": [],
        "rules": [{"name": "java first", "trigger": "code",
                   "action": "write Java first"}]})
    assert out.rules == ["java first"], out
    assert out.linked_topics >= 1, out
    reached = store._read(
        "MATCH (t:Topic {name:'array reordering'})<-[:ABOUT]-(p:Procedure) "
        "RETURN p.name")
    assert reached and reached[0][0] == "java first", reached


# ------------------------------------------------------- what was decided
@test
def test_the_newest_settlement_is_the_one_that_holds():
    """The point of marking rather than searching.

    Two turns both mention a queue. A keyword search matches both equally
    and cannot say which is current, because a sentence carries no
    valid_to -- docs/TESTING.md section 3. Ordering marked settlements by
    (ts, id) descending answers it exactly.
    """
    store = empty_store()
    sid = store.start_session(user="Siddhi", session_id="s1")
    first = store.record_event(sid, "Planner", "message",
                               "Use a PriorityQueue ordered by floor.",
                               speaker="Planner")
    store.mark_settlement(first, "Pending requests use a PriorityQueue",
                          ["lift control logic"])
    time.sleep(1.05)                    # second-precision timestamps
    second = store.record_event(sid, "Planner", "message",
                                "Agreed, an ArrayDeque is simpler.",
                                speaker="Planner")
    store.mark_settlement(second, "Switched to an ArrayDeque",
                          ["lift control logic"])

    settled = store.settlements("lift logic")
    assert len(settled) == 2, settled
    assert settled[0]["settled"] == "Switched to an ArrayDeque", settled
    assert settled[1]["settled"].endswith("PriorityQueue"), settled
    # Flagged as well as ordered. Asked what was decided, a model read a
    # correctly newest-first list and narrated it oldest-first; it reached
    # the right conclusion that time, which is not the same as reliable.
    assert settled[0]["status"] == "current", settled
    assert settled[1]["status"] == "superseded", settled


@test
def test_a_settlement_needs_a_statement_and_a_real_event():
    store = empty_store()
    sid = store.start_session(user="Siddhi", session_id="s1")
    eid = store.record_event(sid, "Planner", "message", "hi", speaker="user")
    for bad, exc in (("", ValueError), ("   ", ValueError)):
        try:
            store.mark_settlement(eid, bad, ["x"])
        except ValueError:
            pass
        else:
            raise AssertionError("an empty settlement must be refused")
    try:
        store.mark_settlement("evt-nope", "something", ["x"])
    except LookupError:
        return
    raise AssertionError("a missing event must be loud, not a silent no-op")


@test
def test_a_removed_conversation_hides_its_settlements():
    """Same hole as read_topic had: removing a chat must close every route
    back into it, decisions included."""
    store = empty_store()
    sid = store.start_session(user="Siddhi", session_id="s-gone")
    eid = store.record_event(sid, "Planner", "message", "we will use Java",
                             speaker="Planner")
    store.mark_settlement(eid, "The project is written in Java", ["stack"])
    assert len(store.settlements("stack")) == 1
    store.hide_session(sid)
    assert store.settlements("stack") == []


@test
def test_full_text_search_finds_a_turn_by_its_words():
    """A real FalkorDB full-text index, not a Python scan. The DDL the
    docs show (`db.idx.fulltext.createNodeIndex`) is rejected on v6.0.2;
    `CREATE FULLTEXT INDEX` is what works."""
    store = empty_store()
    sid = store.start_session(user="Siddhi", session_id="s1")
    store.record_event(sid, "Planner", "message",
                       "Use a PriorityQueue ordered by floor.",
                       speaker="Planner")
    store.record_event(sid, "Planner", "message",
                       "Unrelated chatter about lunch.", speaker="user")
    hits = store.search_turns("PriorityQueue")
    assert len(hits) == 1, hits
    assert "PriorityQueue" in hits[0]["text"]
    # Whole words, not substrings -- verified, and the reason settlements
    # exist rather than this being the whole answer.
    assert store.search_turns("queue") == []


@test
def test_search_skips_a_removed_conversation():
    store = empty_store()
    sid = store.start_session(user="Siddhi", session_id="s-gone")
    store.record_event(sid, "Planner", "message", "the passphrase is hunter",
                       speaker="user")
    assert len(store.search_turns("hunter")) == 1
    store.hide_session(sid)
    assert store.search_turns("hunter") == []


@test
def test_the_curator_marks_what_a_turn_settled():
    store = empty_store()
    sid = store.start_session(user="Siddhi", session_id="s1")
    asked = store.record_event(sid, "Planner", "message",
                               "which structure?", speaker="user")
    answered = store.record_event(sid, "Planner", "message",
                                  "A PriorityQueue.", speaker="Planner")
    out = apply_curation(store, sid, "Planner", [asked, answered], {
        "topics": ["lift control logic"], "entities": [], "facts": [],
        "rules": [],
        "settles": "Pending floor requests use a PriorityQueue"})
    assert out.settled == "Pending floor requests use a PriorityQueue", out
    # Marked on the answer, not the question: that is the turn in which the
    # question stopped being open.
    settled = store.settlements("lift control logic")
    assert len(settled) == 1 and settled[0]["event"] == answered, settled


@test
def test_most_turns_settle_nothing():
    store = empty_store()
    sid = store.start_session(user="Siddhi", session_id="s1")
    eid = store.record_event(sid, "Planner", "message", "hello",
                             speaker="user")
    out = apply_curation(store, sid, "Planner", [eid], {
        "topics": [], "entities": [], "facts": [], "rules": [],
        "settles": ""})
    assert out.settled == "", out
    assert store.settlements() == []
    assert store._read("MATCH ()-[r:SETTLES]->() RETURN count(r)")[0][0] == 0


@test
def test_a_settlement_cannot_be_forged_as_an_open_fact():
    store = empty_store()
    sid = store.start_session(user="Siddhi", session_id="s1")
    eid = store.record_event(sid, "Planner", "message", "x", speaker="user")
    out = apply_curation(store, sid, "Planner", [eid], {
        "topics": [], "entities": [], "rules": [], "settles": "",
        "facts": [{"subject": "a", "predicate": "SETTLES", "object": "b"}]})
    assert out.facts == [], out
    assert any("SETTLES" in s for s in out.skipped), out.skipped


@test
def test_a_failed_curation_is_recorded_on_the_answer():
    """The curator swallows every exception by design, so that a failure
    costs edges rather than the reply. That made it silent: the person is
    told "noted", nothing is kept, and nothing anywhere says so -- the
    failure docs/TESTING.md section 3b calls the dangerous one, brought
    back by the thing built to fix it."""
    store = empty_store()
    sid = store.start_session(user="Siddhi", session_id="s1")
    turn = build_planner(store, sid, FakeAdapter([
        says("Noted - Java from now on."),
        says("not json"),
        says("still not json"),
    ])).run("from now on i want any code in java")

    assert turn.text == "Noted - Java from now on."
    failed = store.failed_curations()
    assert len(failed) == 1, failed
    assert failed[0]["why"], "the reason must be recorded, not just the fact"
    assert "Noted" in failed[0]["answer"], failed[0]


@test
def test_a_successful_curation_is_recorded_too():
    store = empty_store()
    sid = store.start_session(user="Siddhi", session_id="s1")
    build_planner(store, sid, FakeAdapter([
        says("Noted."),
        curation(topics=["java preferences"]),
    ])).run("java please")
    assert store.failed_curations() == []
    assert store._read(
        "MATCH (e:Event) WHERE e.curated = true RETURN count(e)")[0][0] == 1


@test
def test_the_index_does_not_invite_the_model_to_open_a_subject():
    """The first wording said "To continue one, call read_topic...". Asked
    for Java Fibonacci, the model read an unrelated lift conversation and
    appended where it had left off on it."""
    store = empty_store()
    sid = store.start_session(user="Siddhi", session_id="s1")
    store.link_event_topics(
        store.record_event(sid, "Planner", "message", "x", speaker="user"),
        ["lift control logic"])
    prompt = build_planner(store, sid,
                           FakeAdapter([says("hi")])).system_prompt()
    assert "unless the person asks" in prompt, prompt[-400:]
    assert "To continue one" not in prompt


@test
def test_the_topic_index_is_injected_into_the_planners_prompt():
    """A model cannot ask for a conversation whose existence nothing has
    told it about. recent_conversations lists sessions by their *opening
    line*, which for a long design discussion is the least representative
    sentence in it."""
    store = empty_store()
    sid = store.start_session(user="Siddhi", session_id="s1")
    store.link_event_topics(
        store.record_event(sid, "Planner", "message", "x", speaker="user"),
        ["lift control logic"])
    planner = build_planner(store, sid, FakeAdapter([says("hi")]))
    prompt = planner.system_prompt()
    assert "lift control logic" in prompt, prompt[-400:]
    assert "read_topic" in prompt


@test
def test_resuming_work_in_a_new_session_through_the_topic():
    """The scenario this whole layer exists for: a long back-and-forth,
    then a fresh chat that picks it up by subject."""
    store = empty_store()
    s1 = store.start_session(user="Siddhi", session_id="s1")
    planner = build_planner(store, s1, FakeAdapter([
        says("Use one state machine per car."),
        curation(topics=["lift control logic"]),
        says("Door timing should be a separate timer."),
        curation(topics=["lift control logic"]),
    ]))
    planner.run("how do i design lift control logic")
    planner.run("what about the doors")

    # A new session. Nothing carries over but the graph.
    s2 = store.start_session(user="Siddhi", session_id="s2")
    resumed = build_planner(store, s2, FakeAdapter([
        calls("read_topic", {"topic": "lift logic"}),
        says("We settled on one state machine per car; doors were next."),
        curation(topics=["lift control logic"]),
    ]))
    assert "lift control logic" in resumed.system_prompt(), \
        "the index must name the topic before anything is asked"

    turn = resumed.run("lets pick up the lift logic")
    read = json.loads(turn.steps[0].result)
    assert read["topic"] == "lift control logic", read
    said = " ".join(t["text"] for t in read["turns"])
    assert "state machine per car" in said, read["turns"]
    assert "what about the doors" in said, read["turns"]


print()
print("=" * 62)
print(f"{len(PASSED)}/{len(PASSED) + len(FAILED)} tests passed")
for name, why in FAILED:
    print(f"  FAILED: {name} -- {why}")
sys.exit(1 if FAILED else 0)
