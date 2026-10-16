"""The reasoning trace: what an agent read, and why it answered as it did.

    python tests/test_trace.py            # needs FalkorDB on localhost:6379

Mutating tools have always left an Event behind, so what an agent *changed*
was traceable. Reads were not -- and reads are what decide an answer. These
tests cover the layer that closes that: a Lookup per read, RETURNED edges to
the nodes it surfaced, and READ edges from the answer back to all of it.

Runs against the throwaway graph `tenant_trace`, seeded from world_acme.json.
No model is called: the agent turns are driven by the scripted adapter.
"""

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from agents import build_reviewer  # noqa: E402
from agents.toolbox import citation_keys  # noqa: E402
from llm.fake import FakeAdapter, calls, says  # noqa: E402
from memory import MemoryStore, diff_snapshots  # noqa: E402

_spec = importlib.util.spec_from_file_location("seed", ROOT / "scripts" / "seed.py")
seed = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(seed)

from falkordb import FalkorDB  # noqa: E402

TENANT = "trace"
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


def fresh_store():
    world = json.loads((ROOT / "data" / "world_acme.json").read_text(encoding="utf-8"))
    world["graph"] = f"tenant_{TENANT}"
    db = FalkorDB(host="localhost", port=6379)
    seed.validate(world, ROOT / "data" / "world_acme.json")
    seed.seed_tenant(db, world, ROOT / "data" / "world_acme.json")
    return MemoryStore(TENANT)


def blocked_turn(store, session_id):
    """A Reviewer turn that reads twice and then answers. The common shape."""
    llm = FakeAdapter([
        calls("depends_on_chain", {"project": "Billing v2"}),
        calls("current_decisions", {"project": "Spring Launch"}),
        says("No. Billing v2 sits behind Spring Launch."),
    ])
    return build_reviewer(store, session_id, llm).run("Can we ship billing?")


print("seeding fixture graph tenant_trace ...")
S = fresh_store()
SESS = S.start_session(user="demo-user", session_id="sess-trace-1")
print()


# ---------------------------------------------------------------- the walker
@test
def test_citation_keys_finds_identifiers_at_any_depth():
    found = citation_keys({
        "entity": {"label": "Project", "name": "Billing v2"},
        "events": [{"id": "evt-1", "kind": "message"}],
        "chain": ["Spring Launch", "Ledger Migration"],
        "limit": 5,
    })
    assert found == ["Billing v2", "evt-1", "Spring Launch", "Ledger Migration"], found


@test
def test_citation_keys_ignores_values_that_are_not_identifiers():
    # 'kind', 'label' and 'summary' are not identities and must not become
    # citations -- otherwise a node happening to be called "message" is cited.
    assert citation_keys({"kind": "message", "label": "Person", "hops": 2,
                          "summary": "Spring Launch"}) == []


# ---------------------------------------------------------- writing a trace
@test
def test_a_read_records_a_lookup_citing_real_nodes():
    lid = S.record_lookup(SESS, "Reviewer", "depends_on_chain",
                          json.dumps({"project": "Billing v2"}),
                          keys=["Billing v2", "Spring Launch"])
    rows = S._read("MATCH (l:Lookup {id:$id})-[:RETURNED]->(n) "
                   "RETURN n.name ORDER BY n.name", id=lid)
    assert [r[0] for r in rows] == ["Billing v2", "Spring Launch"], rows


@test
def test_a_citation_is_only_made_for_a_node_that_exists():
    """The trace cannot claim a fact the graph does not hold."""
    lid = S.record_lookup(SESS, "Reviewer", "recall_entity",
                          json.dumps({"name": "Invented Thing"}),
                          keys=["Invented Thing", "Spring Launch"])
    rows = S._read("MATCH (l:Lookup {id:$id})-[:RETURNED]->(n) RETURN n.name",
                   id=lid)
    assert [r[0] for r in rows] == ["Spring Launch"], \
        f"a fabricated key was cited: {rows}"


@test
def test_a_read_that_found_nothing_is_still_recorded():
    """Evidence of absence. "Nothing clashes" needs a lookup behind it too."""
    lid = S.record_lookup(SESS, "Reviewer", "recall_entity",
                          json.dumps({"name": "Nobody"}), keys=[])
    assert S._read("MATCH (l:Lookup {id:$id}) RETURN count(l)", id=lid)[0][0] == 1
    assert S._read("MATCH (:Lookup {id:$id})-[r:RETURNED]->() RETURN count(r)",
                   id=lid)[0][0] == 0


@test
def test_citations_are_capped():
    many = [f"ghost-{i}" for i in range(40)] + ["Spring Launch"]
    assert len(many) > MemoryStore.MAX_CITATIONS, "test needs more keys than the cap"
    lid = S.record_lookup(SESS, "Reviewer", "recall_recent", "{}", keys=many)
    cited = S._read("MATCH (:Lookup {id:$id})-[r:RETURNED]->() RETURN count(r)",
                    id=lid)[0][0]
    assert cited <= MemoryStore.MAX_CITATIONS, cited


# -------------------------------------------------------- the loop wires it
@test
def test_an_agent_turn_links_its_answer_to_its_reads():
    store = fresh_store()
    sid = store.start_session(user="you", session_id="sess-link")
    turn = blocked_turn(store, sid)
    assert len(turn.lookups) == 2, turn.lookups
    linked = store._read(
        "MATCH (e:Event)-[:READ]->(l:Lookup) WHERE l.id IN $ids "
        "RETURN count(DISTINCT e), count(l)", ids=turn.lookups)
    assert linked[0][0] == 1, "all reads should hang off one answer"
    assert linked[0][1] == 2, linked


@test
def test_write_tools_are_not_traced():
    """They already leave an Event. Tracing both would double-count."""
    store = fresh_store()
    sid = store.start_session(user="you", session_id="sess-write")
    llm = FakeAdapter([
        calls("create_ticket", {"title": "Hold billing",
                                "blocks_project": "Billing v2"}),
        says("Filed."),
    ])
    turn = build_reviewer(store, sid, llm).run("Hold the billing release.")
    assert turn.lookups == [], f"a write was traced as a read: {turn.lookups}"


@test
def test_reading_the_trace_is_not_itself_traced():
    """Otherwise each explanation would cite the one before it."""
    store = fresh_store()
    sid = store.start_session(user="you", session_id="sess-recurse")
    blocked_turn(store, sid)
    llm = FakeAdapter([calls("explain_answer", {"about": "Billing v2"}),
                       says("Because of the dependency.")])
    turn = build_reviewer(store, sid, llm).run("Why?")
    assert turn.lookups == [], f"explain_answer traced itself: {turn.lookups}"


# ---------------------------------------------------------- reading it back
@test
def test_explain_answer_returns_the_reads_in_order():
    store = fresh_store()
    sid = store.start_session(user="you", session_id="sess-order")
    blocked_turn(store, sid)
    trace = store.explain_answer(about="Billing v2")
    assert trace, "no trace found"
    assert [s["tool"] for s in trace["steps"]] == \
        ["depends_on_chain", "current_decisions"], trace["steps"]
    assert trace["steps"][0]["arguments"] == {"project": "Billing v2"}
    assert "Billing v2" in trace["answer"]["text"]
    assert trace["answer"]["speaker"] == "Reviewer"


@test
def test_the_subject_is_found_through_citations_not_text():
    """'Ledger Migration' is never said in the answer, only read."""
    store = fresh_store()
    sid = store.start_session(user="you", session_id="sess-subject")
    turn = blocked_turn(store, sid)
    assert "Ledger Migration" not in turn.text, "fixture no longer proves the point"
    assert store.explain_answer(about="Ledger Migration"), \
        "an entity the answer only *read* should still find it"


@test
def test_an_unread_entity_explains_nothing():
    store = fresh_store()
    sid = store.start_session(user="you", session_id="sess-unread")
    blocked_turn(store, sid)
    assert store.explain_answer(about="Auth Service") is None


@test
def test_the_trace_outlives_the_session_and_the_connection():
    """The whole claim: provenance is in the database, not the process."""
    store = fresh_store()
    sid = store.start_session(user="you", session_id="sess-day-one")
    blocked_turn(store, sid)

    later = MemoryStore(TENANT)          # new connection, new day, new session
    later.start_session(user="you", session_id="sess-day-two")
    trace = later.explain_answer(about="Billing v2")
    assert trace and len(trace["steps"]) == 2, trace
    assert trace["steps"][0]["returned"], "citations should come back too"


@test
def test_an_agent_can_explain_itself_through_the_tool():
    store = fresh_store()
    sid1 = store.start_session(user="you", session_id="sess-mon")
    blocked_turn(store, sid1)

    sid2 = store.start_session(user="you", session_id="sess-tue")
    llm = FakeAdapter([calls("explain_answer", {"about": "Billing v2"}),
                       says("Because Billing v2 depends on Spring Launch.")])
    turn = build_reviewer(store, sid2, llm).run("Why did you block that?")
    step = [s for s in turn.steps if s.name == "explain_answer"][0]
    assert not step.is_error, step.result
    payload = json.loads(step.result)
    assert [s["tool"] for s in payload["steps"]] == \
        ["depends_on_chain", "current_decisions"], payload


# --------------------------------------------------- it stays out of the way
@test
def test_citations_do_not_leak_into_fact_recall():
    """RETURNED carries no valid_from, so recall_related's filter skips it."""
    store = fresh_store()
    sid = store.start_session(user="you", session_id="sess-leak")
    blocked_turn(store, sid)
    rel = store.recall_related("Project", "Spring Launch")
    polluted = [f for f in rel["facts_in"] + rel["facts_out"]
                if f["type"] in ("READ", "RETURNED")]
    assert not polluted, polluted


@test
def test_lookups_are_never_consolidation_candidates():
    store = fresh_store()
    sid = store.start_session(user="you", session_id="sess-consol")
    blocked_turn(store, sid)
    cands = store.consolidation_candidates(min_events=1, limit=25)
    assert not [c for c in cands if c["label"] == "Lookup"], cands


@test
def test_the_changes_panel_ignores_the_trace():
    """A lookup is the agent reading, not the agent believing something new."""
    store = fresh_store()
    sid = store.start_session(user="you", session_id="sess-diff")
    before = store.snapshot()
    llm = FakeAdapter([calls("depends_on_chain", {"project": "Billing v2"}),
                       says("It sits behind Spring Launch.")])
    build_reviewer(store, sid, llm).run("What is billing waiting on?")
    changes = diff_snapshots(before, store.snapshot())
    assert not [n for n in changes.added_nodes if n["label"] == "Lookup"], \
        changes.added_nodes
    assert not [e for e in changes.added_edges
                if e["type"] in ("READ", "RETURNED")], changes.added_edges


@test
def test_the_trace_cannot_be_forged_through_the_open_layer():
    """Lookup/READ/RETURNED are reserved, so a model cannot fabricate evidence."""
    store = fresh_store()
    sid = store.start_session(user="you", session_id="sess-forge")
    for kwargs in (
        {"subject": "x", "predicate": "READ", "obj": "y"},
        {"subject": "x", "predicate": "RETURNED", "obj": "y"},
        {"subject": "x", "predicate": "mentions", "obj": "y",
         "subject_type": "Lookup"},
    ):
        try:
            store.remember_fact(sid, "Planner", **kwargs)
        except ValueError:
            continue
        raise AssertionError(f"open layer accepted {kwargs}")


@test
def test_traces_respect_the_tenant_boundary():
    store = fresh_store()
    sid = store.start_session(user="you", session_id="sess-tenant")
    blocked_turn(store, sid)
    assert MemoryStore("globex").explain_answer(about="Billing v2") is None, \
        "a trace leaked across tenants"


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
