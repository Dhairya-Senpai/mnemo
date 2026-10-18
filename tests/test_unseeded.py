"""Memory on a graph that was never seeded, and facts that recur.

    python tests/test_unseeded.py         # needs FalkorDB on localhost:6379

Every other suite loads `world_acme.json` first, so every other suite has
Agent nodes, Person nodes and indexes before it starts. That shared fixture
hid three bugs, all of which a user found in minutes by talking to the agent
about a doctor's appointment:

1. `record_event` matched on `(a:Agent {name:...})`. With no seeded Agent the
   match yielded no rows, the CREATE never ran, and the method still returned
   an event id -- so the entire episodic tier was a silent no-op while facts
   kept being written.
2. `retire_open_fact` closed every open edge between two nodes, so recording
   a rescheduled appointment and then retiring the old one destroyed both.
3. `recall_entity` filtered only on `valid_to`, so the reasoning trace's
   RETURNED edges came back as facts the agent believed about you.

These tests therefore start from `tenant_unseeded`, an empty graph, and the
personal-assistant shape rather than the company one.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from agents import build_planner  # noqa: E402
from llm.fake import FakeAdapter, calls, says  # noqa: E402
from memory import MemoryStore  # noqa: E402

from falkordb import FalkorDB  # noqa: E402

TENANT = "unseeded"
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
    """A store on a graph with nothing in it at all. No seed, no Agents."""
    try:
        FalkorDB(host="localhost", port=6379).select_graph(f"tenant_{TENANT}").delete()
    except Exception:
        pass
    return MemoryStore(TENANT)


print("using empty graph tenant_unseeded (no seed, no Agent nodes)\n")


# --------------------------------------- a graph that does not exist at all
@test
def test_reads_work_before_the_graph_exists():
    """A workspace has to be readable before anyone has written to it.

    FalkorDB creates a graph's Redis key on first write; until then
    GRAPH.RO_QUERY fails with "Invalid graph operation on empty key" rather
    than returning no rows. The UI lists conversations before the first
    message, so without this a brand-new workspace crashed on open.
    """
    store = empty_store()          # deletes the graph; nothing has written yet
    assert store.list_sessions(limit=5) == []
    assert store.recall_recent(limit=5) == []
    assert store.explain_answer() is None
    assert store.what_is_on("2026-10-20") == []
    assert store.recall_entity("nobody") is None


@test
def test_the_first_write_brings_the_graph_into_being():
    store = empty_store()
    assert store.list_sessions() == [], "fixture should start with no graph"
    sid = store.start_session(user="Siddhi", session_id="s-first")
    store.record_event(sid, "Planner", "message", "hello", speaker="user")
    assert len(store.list_sessions()) == 1
    assert len(store.recall_recent()) == 1


# ------------------------------------------------- bug 1: silent no-op
@test
def test_an_event_is_written_without_a_seeded_agent():
    store = empty_store()
    sid = store.start_session(user="Siddhi", session_id="s-ev")
    assert store._read("MATCH (a:Agent) RETURN count(a)")[0][0] == 0, \
        "fixture must start with no Agent nodes"

    eid = store.record_event(sid, "Planner", "message", "hello", speaker="user")

    found = store._read("MATCH (e:Event {id:$id}) RETURN count(e)", id=eid)[0][0]
    assert found == 1, "record_event returned an id for an event it never wrote"
    assert store._read("MATCH (:Agent {name:'Planner'})-[:PERFORMED]->(:Event) "
                       "RETURN count(*)")[0][0] == 1, "no PERFORMED edge"


@test
def test_the_agent_node_is_created_once_not_per_event():
    store = empty_store()
    sid = store.start_session(user="Siddhi", session_id="s-dup")
    for i in range(4):
        store.record_event(sid, "Planner", "message", f"msg {i}")
    assert store._read("MATCH (a:Agent {name:'Planner'}) RETURN count(a)")[0][0] == 1, \
        "MERGE should reuse the agent, not create a duplicate each time"


@test
def test_episodic_recall_works_on_an_unseeded_graph():
    store = empty_store()
    sid = store.start_session(user="Siddhi", session_id="s-recall")
    store.record_event(sid, "Planner", "message", "first", speaker="user")
    store.record_event(sid, "Planner", "message", "second", speaker="Planner")

    # Recall returns the person's half; an agent's own reply is output, not
    # evidence, and is covered by test_an_agent_does_not_recall_its_own_prose.
    recent = store.recall_recent(limit=10)
    assert [r["summary"] for r in recent] == ["first"], recent
    assert len(store.session_transcript(sid)) == 2, "transcript should round-trip"


@test
def test_a_missing_session_fails_loudly():
    """The failure that used to be silent must now be an exception."""
    store = empty_store()
    try:
        store.record_event("no-such-session", "Planner", "message", "orphan")
    except LookupError:
        return
    raise AssertionError("recording into a missing session silently succeeded")


# ------------------------------------- bug 2: updating a recurring fact
@test
def test_retiring_one_date_leaves_the_other_open():
    store = empty_store()
    sid = store.start_session(user="Siddhi", session_id="s-resched")
    store.remember_fact(sid, "Planner", "Siddhi", "HAS_APPOINTMENT", "doctor",
                        when="2026-10-20")
    store.remember_fact(sid, "Planner", "Siddhi", "HAS_APPOINTMENT", "doctor",
                        when="2026-10-22")

    closed = store.retire_open_fact(sid, "Planner", "Siddhi", "HAS_APPOINTMENT",
                                    "doctor", "moved", when="2026-10-20")
    assert closed == 1, f"expected to close exactly one edge, closed {closed}"

    still_on = [f["when"] for f in store.recall_entity("Siddhi")["facts_out"]]
    assert still_on == ["2026-10-22"], \
        f"the replacement should survive; open dates are {still_on}"


@test
def test_the_rescheduling_order_that_used_to_lose_the_fact():
    """Record the new date first, then retire the old one by date."""
    store = empty_store()
    sid = store.start_session(user="Siddhi", session_id="s-order")
    store.remember_fact(sid, "Planner", "Siddhi", "HAS_APPOINTMENT", "doctor",
                        when="2026-10-20")
    # The order a model actually chose: assert, then retire.
    store.remember_fact(sid, "Planner", "Siddhi", "HAS_APPOINTMENT", "doctor",
                        when="2026-10-22")
    store.retire_open_fact(sid, "Planner", "Siddhi", "HAS_APPOINTMENT", "doctor",
                           "moved", when="2026-10-20")
    assert store.what_is_on("2026-10-22"), "the appointment was lost entirely"
    assert not store.what_is_on("2026-10-20"), "the old date should be closed"


@test
def test_without_a_date_every_matching_fact_still_closes():
    """Documented behaviour, kept on purpose for facts that hold only once."""
    store = empty_store()
    sid = store.start_session(user="Siddhi", session_id="s-bulk")
    for day in ("2026-10-20", "2026-10-22"):
        store.remember_fact(sid, "Planner", "Siddhi", "HAS_APPOINTMENT",
                            "doctor", when=day)
    closed = store.retire_open_fact(sid, "Planner", "Siddhi", "HAS_APPOINTMENT",
                                    "doctor", "cancelled all")
    assert closed == 2, f"expected both closed, got {closed}"


# --------------------------------------- bug 3: the trace leaking into recall
@test
def test_recall_entity_does_not_report_the_reasoning_trace():
    store = empty_store()
    sid = store.start_session(user="Siddhi", session_id="s-trace")
    store.remember_fact(sid, "Planner", "Siddhi", "HAS_APPOINTMENT", "doctor",
                        when="2026-10-20")
    # A lookup that cites Siddhi, exactly as dispatch would record one.
    store.record_lookup(sid, "Planner", "recall_entity",
                        json.dumps({"name": "Siddhi"}), keys=["Siddhi"])

    entity = store.recall_entity("Siddhi")
    leaked = [f for f in entity["facts_in"] + entity["facts_out"]
              if f["predicate"] in ("READ", "RETURNED")]
    assert not leaked, f"the trace came back as belief: {leaked}"
    assert [f["predicate"] for f in entity["facts_out"]] == ["HAS_APPOINTMENT"], \
        entity["facts_out"]


@test
def test_structural_edges_are_not_facts_either():
    """ABOUT and PERFORMED are plumbing; they must not show up as knowledge."""
    store = empty_store()
    sid = store.start_session(user="Siddhi", session_id="s-struct")
    store.remember_fact(sid, "Planner", "Siddhi", "LIKES", "coffee")
    entity = store.recall_entity("Siddhi")
    kinds = {f["predicate"] for f in entity["facts_in"] + entity["facts_out"]}
    assert kinds == {"LIKES"}, f"plumbing leaked into facts: {kinds}"


# ------------------------------------- the whole scenario, end to end
@test
def test_appointment_then_flight_across_two_sessions():
    """The user's own scenario, scripted so it runs without a model."""
    store = empty_store()

    s1 = store.start_session(user="Siddhi", session_id="s-day1")
    # Two scripted calls: the Planner's reply, then the curation of that
    # turn. The Planner has no fact-writing tool any more -- extraction is
    # the curator's job, so that the model answering the person is not also
    # the model deciding what to write down.
    llm1 = FakeAdapter([
        says("Noted - doctor on the 20th."),
        says(json.dumps({
            "topics": ["doctor appointment"],
            "entities": [],
            "facts": [{"subject": "Siddhi", "predicate": "HAS_APPOINTMENT",
                       "object": "doctor", "subject_type": "Person",
                       "object_type": "Thing", "when": "2026-10-20"}],
            "rules": [],
        })),
    ])
    first = build_planner(store, s1, llm1).run(
        "I have a doctor appointment on the 20th.")
    assert first.curation.facts == ["Siddhi HAS_APPOINTMENT doctor"], \
        first.curation

    # A different session. Nothing is carried over but the graph.
    s2 = store.start_session(user="Siddhi", session_id="s-day2")
    llm2 = FakeAdapter([
        calls("what_is_on", {"date": "2026-10-20"}),
        says("You already have a doctor appointment that day."),
        says('{"topics":[],"entities":[],"facts":[],"rules":[]}'),
    ])
    turn = build_planner(store, s2, llm2).run("Can I fly on the 20th?")

    clash = json.loads(turn.steps[0].result)
    assert clash and clash[0]["object"] == "doctor", \
        f"the second session could not see the first: {clash}"
    assert clash[0]["subject"] == "Siddhi", "stored against the wrong subject"


@test
def test_a_fact_about_someone_else_keeps_its_own_types():
    store = empty_store()
    sid = store.start_session(user="Siddhi", session_id="s-third")
    store.remember_fact(sid, "Planner", "Priya", "IS_ALLERGIC_TO", "peanuts",
                        subject_type="Person", object_type="Food")
    priya = store.recall_entity("Priya")
    assert priya["type"] == "Person", priya
    assert priya["facts_out"][0]["name"] == "peanuts"
    assert priya["facts_out"][0]["type"] == "Food", priya["facts_out"]


# ----------------------------------- removing a conversation
@test
def test_removing_a_conversation_retires_what_it_taught():
    """Hiding the chat alone leaves the agent acting on it.

    The gap is easy to walk into: the conversation leaves the sidebar while
    the appointment stays current, so the agent keeps refusing to book a
    flight on account of something the person believes they deleted.
    """
    store = empty_store()
    sid = store.start_session(user="Siddhi", session_id="s-remove")
    store.remember_fact(sid, "Planner", "Siddhi", "HAS_APPOINTMENT", "doctor",
                        when="2026-10-20")
    assert store.what_is_on("2026-10-20"), "fixture should start booked"

    retired = store.hide_session(sid)
    assert retired == 1, f"expected one fact retired, got {retired}"
    assert store.what_is_on("2026-10-20") == [], \
        "the day is still blocked by a conversation that was removed"
    assert sid not in [x["id"] for x in store.list_sessions()], \
        "the conversation should be out of the list"


@test
def test_removing_a_conversation_is_not_a_delete():
    store = empty_store()
    sid = store.start_session(user="Siddhi", session_id="s-keep")
    store.remember_fact(sid, "Planner", "Siddhi", "HAS_APPOINTMENT", "doctor",
                        when="2026-10-20")
    store.hide_session(sid)

    rows = store._read("MATCH ()-[r:HAS_APPOINTMENT]->() "
                       "RETURN r.valid_from, r.valid_to, r.retired_with_session")
    assert len(rows) == 1, "the edge should still be there"
    assert rows[0][1] is not None, "it should carry an end date"
    assert rows[0][2] == sid, "and say which removal ended it"
    # The episode itself survives, so nothing points at a missing event.
    assert store._read("MATCH (:Session {id:$s})-[:HAS_EVENT]->(e:Event) "
                       "RETURN count(e)", s=sid)[0][0] > 0


@test
def test_other_conversations_are_untouched_by_a_removal():
    store = empty_store()
    keep = store.start_session(user="Siddhi", session_id="s-other")
    store.remember_fact(keep, "Planner", "Siddhi", "HAS_APPOINTMENT",
                        "dentist", when="2026-10-21")
    drop = store.start_session(user="Siddhi", session_id="s-drop")
    store.remember_fact(drop, "Planner", "Siddhi", "HAS_APPOINTMENT",
                        "doctor", when="2026-10-20")

    store.hide_session(drop)
    assert store.what_is_on("2026-10-20") == [], "removed day should be clear"
    assert store.what_is_on("2026-10-21"), "the other conversation's fact went too"


@test
def test_a_renamed_conversation_keeps_its_messages():
    store = empty_store()
    sid = store.start_session(user="Siddhi", session_id="s-rename")
    store.record_event(sid, "Planner", "message", "I have a doctor appointment",
                       speaker="user")
    store.rename_session(sid, "Doctor appointment")
    row = [x for x in store.list_sessions() if x["id"] == sid][0]
    assert row["title"] == "Doctor appointment"
    assert row["preview"] == "I have a doctor appointment", \
        "renaming must not rewrite what was said"


# -------------------------------- episodes that outlived their facts
@test
def test_recall_leaves_out_an_episode_whose_fact_was_retired():
    """The bug this exists for: the clash that would not go away.

    Retiring a fact updates the semantic tier and leaves the episodic one
    alone -- correctly, because the conversation did happen. But the
    episode's own words still assert the fact, so a model reading episodes
    reported a double-booking fifteen minutes after the appointment had been
    retired, while the fact queries beside it returned nothing.

    Labelling the episode in place was tried first and was not enough: the
    model read the label as something worth reporting and narrated the
    cancellation instead of ignoring it.
    """
    store = empty_store()
    sid = store.start_session(user="Siddhi", session_id="s-stale")
    store.remember_fact(sid, "Planner", "Siddhi", "HAS_APPOINTMENT", "doctor",
                        when="2026-10-20")
    store.retire_open_fact(sid, "Planner", "Siddhi", "HAS_APPOINTMENT",
                           "doctor", "cancelled", when="2026-10-20")

    entity = store.recall_entity("Siddhi")
    assert entity["facts_out"] == [], "the fact itself should be gone"
    assert not [e for e in entity["episodes"] if "HAS_APPOINTMENT" in e["summary"]], \
        "a retired fact came back as an episode and will be read as current"
    assert entity["superseded_episodes"] == 1, "history should still be counted"


@test
def test_an_ordinary_message_survives_a_retirement():
    """An episode that established nothing has nothing to lose."""
    store = empty_store()
    sid = store.start_session(user="Siddhi", session_id="s-plain")
    store.record_event(sid, "Planner", "message", "hello there", speaker="user")
    store.remember_fact(sid, "Planner", "Siddhi", "LIKES", "coffee")
    store.retire_open_fact(sid, "Planner", "Siddhi", "LIKES", "coffee", "changed")

    said = [e["summary"] for e in store.recall_recent(limit=20)]
    assert "hello there" in said, "a plain message was dropped as stale"
    assert "Learned: Siddhi LIKES coffee" not in said, \
        "the retired fact is still being recalled"


@test
def test_nothing_is_dropped_while_it_is_still_true():
    store = empty_store()
    sid = store.start_session(user="Siddhi", session_id="s-live")
    store.remember_fact(sid, "Planner", "Siddhi", "HAS_APPOINTMENT", "dentist",
                        when="2026-10-21")
    entity = store.recall_entity("Siddhi")
    assert entity["superseded_episodes"] == 0
    assert [e for e in entity["episodes"] if "HAS_APPOINTMENT" in e["summary"]], \
        "a current fact's episode should still be recalled"


@test
def test_retired_facts_are_still_in_the_graph_after_recall_hides_them():
    """Not hidden from the database, only from "what is true now"."""
    store = empty_store()
    sid = store.start_session(user="Siddhi", session_id="s-hist")
    store.remember_fact(sid, "Planner", "Siddhi", "HAS_APPOINTMENT", "doctor",
                        when="2026-10-20")
    store.retire_open_fact(sid, "Planner", "Siddhi", "HAS_APPOINTMENT",
                           "doctor", "cancelled", when="2026-10-20")
    rows = store._read("MATCH ()-[r:HAS_APPOINTMENT]->() "
                       "RETURN r.when, r.valid_to")
    assert len(rows) == 1 and rows[0][1] is not None, \
        "the edge should still exist, carrying an end date"


@test
def test_an_agent_does_not_recall_its_own_prose():
    """The sentence that outlived the fact it was about.

    Retiring a fact correctly silenced every tool that reads facts. Then the
    agent read its own earlier answer out of the episode log -- "you already
    have a dentist appointment on the 20th" -- and said it again. A reply
    asserts things in sentences rather than in edges, so nothing can retire
    it.
    """
    store = empty_store()
    sid = store.start_session(user="Siddhi", session_id="s-prose")
    store.record_event(sid, "Planner", "message", "can i fly on the 20th?",
                       speaker="user")
    store.record_event(sid, "Planner", "message",
                       "You already have a dentist appointment on the 20th.",
                       speaker="Planner")

    said = [e["summary"] for e in store.recall_recent(limit=20)]
    assert "can i fly on the 20th?" in said, "the person's own words are evidence"
    assert not [s for s in said if "dentist appointment" in s], \
        "the agent is being fed its own prose back as if it were a fact"

    # It is left out of recall, not removed: the UI still rebuilds both sides.
    spoken = [m["speaker"] for m in store.session_transcript(sid)]
    assert spoken == ["user", "Planner"], spoken


@test
def test_recall_recent_still_fills_its_limit():
    """Dropping replies must not quietly halve what recall returns."""
    store = empty_store()
    sid = store.start_session(user="Siddhi", session_id="s-fill")
    for i in range(8):
        store.record_event(sid, "Planner", "message", f"user says {i}",
                           speaker="user")
        store.record_event(sid, "Planner", "message", f"agent replies {i}",
                           speaker="Planner")
    got = store.recall_recent(limit=5)
    assert len(got) == 5, f"asked for 5, got {len(got)}"
    assert all("agent replies" not in e["summary"] for e in got), got


# ------------------------------------------- deleting, as opposed to removing
@test
def test_deleting_a_conversation_takes_its_facts_with_it():
    store = empty_store()
    sid = store.start_session(user="Siddhi", session_id="s-destroy")
    store.remember_fact(sid, "Planner", "Siddhi", "HAS_APPOINTMENT", "doctor",
                        when="2026-10-20")
    before = store.deletion_preview(sid)
    assert before["facts"] >= 1 and before["events"] >= 1, before

    store.delete_session(sid)
    assert store.what_is_on("2026-10-20") == []
    assert store._read("MATCH ()-[r:HAS_APPOINTMENT]->() RETURN count(r)")[0][0] == 0, \
        "a delete should leave no edge behind, retired or otherwise"
    assert store._read("MATCH (s:Session {id:$i}) RETURN count(s)",
                       i=sid)[0][0] == 0


@test
def test_a_delete_leaves_nothing_dangling():
    """The property that makes the order of operations matter.

    Facts cite their originating event by id. Remove the events first and
    every one of those ids points at nothing, which is exactly the failure
    that made retiring the safer default in the first place.
    """
    store = empty_store()
    sid = store.start_session(user="Siddhi", session_id="s-dangle")
    store.remember_fact(sid, "Planner", "Siddhi", "LIKES", "coffee")
    store.record_lookup(sid, "Planner", "recall_entity", "{}", keys=["Siddhi"])
    store.delete_session(sid)

    live = {r[0] for r in store._read("MATCH (e:Event) RETURN e.id")}
    cited = {r[0] for r in store._read(
        "MATCH ()-[r]->() WHERE r.source_event IS NOT NULL "
        "RETURN r.source_event")}
    cited |= {r[0] for r in store._read(
        "MATCH (n) WHERE n.source_event IS NOT NULL RETURN n.source_event")}
    assert cited <= live, f"these cite events that no longer exist: {cited - live}"
    assert store._read("MATCH (l:Lookup) RETURN count(l)")[0][0] == 0, \
        "the reasoning trace outlived the answers it belonged to"


@test
def test_a_delete_sweeps_entities_nothing_points_at():
    store = empty_store()
    sid = store.start_session(user="Siddhi", session_id="s-sweep")
    store.remember_fact(sid, "Planner", "Siddhi", "HAS_APPOINTMENT", "doctor",
                        when="2026-10-20")
    store.delete_session(sid)
    left = [r[0] for r in store._read(
        "MATCH (n) WHERE NOT n:Agent RETURN coalesce(n.name, n.id)")]
    assert left == [], f"orphaned entities were left behind: {left}"


@test
def test_a_delete_leaves_other_conversations_alone():
    store = empty_store()
    keep = store.start_session(user="Siddhi", session_id="s-keep2")
    store.remember_fact(keep, "Planner", "Siddhi", "HAS_APPOINTMENT",
                        "dentist", when="2026-10-21")
    drop = store.start_session(user="Siddhi", session_id="s-drop2")
    store.remember_fact(drop, "Planner", "Siddhi", "HAS_APPOINTMENT",
                        "doctor", when="2026-10-20")

    store.delete_session(drop)
    assert store.what_is_on("2026-10-21"), "the other conversation's fact went too"
    assert store.what_is_on("2026-10-20") == []
    assert keep in [x["id"] for x in store.list_sessions()]


# ------------------------------------------- rules that belong to no project
@test
def test_a_rule_scoped_to_nothing_is_still_reachable():
    """The orphaned standing rule.

    PROCEDURE_SCOPES allows only Project and System, so a rule about a topic
    -- "prefer Java for coding" -- is stored with no APPLIES_TO edge and used
    to be unreachable by the only tool that reads rules, while
    learn_procedure returned a perfectly good id and the agent reported that
    it had saved a standing rule.
    """
    store = empty_store()
    sid = store.start_session(user="Siddhi", session_id="s-rule")
    store.learn_procedure(sid, "Planner", "prefer Java for coding",
                          "coding, programming", "use Java")

    assert [r["name"] for r in store.get_applicable_procedures()] == \
        ["prefer Java for coding"], "a rule with no scope vanished"
    scoped = store.get_applicable_procedures("Project", "anything")
    assert [r["matched"] for r in scoped] == ["applies everywhere"], scoped


@test
def test_standing_rules_reach_the_agent_without_a_tool_call():
    """A rule has to apply on the turns the model sees no reason to look.

    Asked to write hello world, a model taught "prefer Java" called no tools
    at all and answered in Python: it did not think the question needed
    remembering. So the rules are read from the graph into every system
    prompt rather than waiting to be fetched.
    """
    store = empty_store()
    sid = store.start_session(user="Siddhi", session_id="s-inject")
    store.learn_procedure(sid, "Planner", "prefer Java for coding",
                          "coding, programming", "use Java")

    agent = build_planner(store, sid, FakeAdapter([says("ok")]))
    assert "prefer Java for coding" in agent.system_prompt()
    assert "use Java" in agent.system_prompt()


@test
def test_retiring_a_rule_takes_it_out_of_the_prompt():
    """Which is what keeps this memory rather than a hardcoded instruction."""
    store = empty_store()
    sid = store.start_session(user="Siddhi", session_id="s-retire-rule")
    pid = store.learn_procedure(sid, "Planner", "prefer Java for coding",
                                "coding", "use Java")
    agent = build_planner(store, sid, FakeAdapter([says("ok")]))
    assert "prefer Java" in agent.system_prompt()

    store.retire_procedure(sid, "Planner", pid, "changed my mind")
    assert "prefer Java" not in agent.system_prompt(), \
        "a retired rule is still being pushed at the model"


# ------------------------------------------------------------------- report
if __name__ == "__main__":
    try:
        FalkorDB(host="localhost", port=6379).select_graph(f"tenant_{TENANT}").delete()
    except Exception:
        pass
    print("\n" + "=" * 62)
    print(f"{len(PASSED)}/{len(PASSED) + len(FAILED)} tests passed")
    for name, why in FAILED:
        print(f"  FAILED: {name} -- {why}")
    sys.exit(1 if FAILED else 0)
