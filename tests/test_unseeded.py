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

    recent = store.recall_recent(limit=10)
    assert len(recent) == 2, f"recall_recent lost the history: {recent}"
    assert [r["summary"] for r in recent] == ["second", "first"], recent
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
    llm1 = FakeAdapter([
        calls("remember_fact", {"subject": "Siddhi", "predicate": "HAS_APPOINTMENT",
                                "object": "doctor", "when": "2026-10-20"}),
        says("Noted - doctor on the 20th."),
    ])
    build_planner(store, s1, llm1).run("I have a doctor appointment on the 20th.")

    # A different session. Nothing is carried over but the graph.
    s2 = store.start_session(user="Siddhi", session_id="s-day2")
    llm2 = FakeAdapter([
        calls("what_is_on", {"date": "2026-10-20"}),
        says("You already have a doctor appointment that day."),
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
