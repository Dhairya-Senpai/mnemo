"""The open fact layer: remember anything, structured, versioned, sourced.

    python tests/test_open_facts.py     # needs FalkorDB on localhost:6379

The closed schema could only remember shapes chosen in advance, which meant
the agent refused facts rather than storing them. These tests cover the layer
that fixes that, and - importantly - the boundary that keeps it from becoming
a junk drawer or an injection hole.

No model involved. Everything here is the memory layer on its own.
"""

import contextlib
import importlib.util
import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from falkordb import FalkorDB  # noqa: E402

from memory import MemoryStore  # noqa: E402
from memory.schema import clean_label, clean_predicate  # noqa: E402

TENANT = "test"
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
    spec = importlib.util.spec_from_file_location("seed", ROOT / "scripts" / "seed.py")
    seed = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(seed)
    with contextlib.redirect_stdout(io.StringIO()):
        seed.seed_tenant(FalkorDB(host="localhost", port=6379), world,
                         ROOT / "data" / "world_acme.json")
    return MemoryStore(TENANT)


STORE = fresh_store()
SESSION = STORE.start_session(user="demo-user", session_id="sess-open")


# ------------------------------------------------------------- the boundary
@test
def test_labels_and_predicates_are_normalised_not_repaired():
    assert clean_label("Appointment") == "Appointment"
    assert clean_predicate("has appointment") == "HAS_APPOINTMENT"
    assert clean_predicate("reports-to") == "REPORTS_TO"
    # Same fact phrased two ways must land on one edge type, not two.
    assert clean_predicate("HAS_APPOINTMENT") == clean_predicate("has appointment")


@test
def test_cypher_cannot_be_smuggled_through_a_type():
    # Labels and relationship types are interpolated into queries because
    # Cypher cannot parameterise them. Validation is the entire defence.
    for bad in ("Appointment {x:1})-[:OWNS]->(:Person", "Foo`Bar", "has-'quote",
                "", "   ", "9Lives", "a" * 40):
        try:
            clean_label(bad)
        except ValueError:
            continue
        raise AssertionError(f"label {bad!r} should have been refused")

    for bad in ("A`B", "]->(:X)<-[:Y", "has'quote", "x" * 50, "", "  "):
        try:
            clean_predicate(bad)
        except ValueError:
            continue
        raise AssertionError(f"predicate {bad!r} should have been refused")

    # Note what is NOT refused, and why: "DROP ALL" normalises to the
    # identifier DROP_ALL, which is a silly edge name but cannot escape the
    # relationship-type position it is interpolated into. Blocking words that
    # merely look dangerous would be cargo-cult; blocking the characters that
    # break out of the position is the actual defence.
    assert clean_predicate("DROP ALL") == "DROP_ALL"


@test
def test_open_facts_cannot_impersonate_a_behavioural_type():
    # A node labelled Decision would be picked up by current_decisions and by
    # supersession logic expecting properties it does not have.
    for reserved in ("Decision", "Task", "Procedure", "Event", "Agent"):
        try:
            clean_label(reserved)
        except ValueError as e:
            assert "built-in" in str(e), str(e)
            continue
        raise AssertionError(f"{reserved} should be reserved")

    for reserved in ("SUPERSEDES", "ASSIGNED_TO", "OWNS", "DEPENDS_ON"):
        try:
            clean_predicate(reserved)
        except ValueError:
            continue
        raise AssertionError(f"{reserved} should be reserved")


# --------------------------------------------------------------- the feature
@test
def test_an_arbitrary_fact_becomes_a_real_typed_edge():
    out = STORE.remember_fact(
        SESSION, "Planner", "Robin Vale", "has appointment", "dentist",
        subject_type="Person", object_type="Appointment", when="2026-10-20")
    assert out["predicate"] == "HAS_APPOINTMENT", out
    assert out["object"]["type"] == "Appointment", out
    assert out["when"] == "2026-10-20"

    rows = STORE._read(
        "MATCH (s)-[r:HAS_APPOINTMENT]->(o) WHERE ID(r) >= 0 "
        "RETURN labels(s)[0], s.name, labels(o)[0], o.name, r.when, "
        "       r.valid_to, r.source_event")
    assert rows, "the edge should exist"
    label, subject, olabel, obj, when, valid_to, src = rows[0]
    assert (subject, obj, when) == ("Robin Vale", "dentist", "2026-10-20")
    assert olabel == "Appointment"
    assert valid_to is None, "a new fact is current"
    assert src and src.startswith("evt-"), "every fact carries its source event"


@test
def test_existing_entities_are_reused_not_duplicated():
    # Lee Park already exists as a Person in the seeded world. Saying
    # something new about him must attach to that node, not make a second.
    before = STORE._read("MATCH (n) WHERE n.name = 'Lee Park' RETURN count(n)")[0][0]
    out = STORE.remember_fact(SESSION, "Planner", "Lee Park", "prefers",
                              "morning standups", subject_type="Contact",
                              object_type="Preference")
    after = STORE._read("MATCH (n) WHERE n.name = 'Lee Park' RETURN count(n)")[0][0]
    assert before == after == 1, f"Lee Park duplicated: {before} -> {after}"
    # Reuse wins over the proposed type: he stays a Person, not a Contact.
    assert out["subject"]["type"] == "Person", out
    assert out["subject"]["created"] is False


@test
def test_a_fact_can_be_retired_like_any_other():
    STORE.remember_fact(SESSION, "Planner", "Dana Ruiz", "sits in", "Room 4",
                        object_type="Room")
    n = STORE.retire_open_fact(SESSION, "Planner", "Dana Ruiz", "sits in",
                               "Room 4", "moved desks")
    assert n == 1, f"expected to retire one edge, got {n}"

    rows = STORE._read(
        "MATCH (s {name:'Dana Ruiz'})-[r:SITS_IN]->(o) WHERE ID(r) >= 0 "
        "RETURN r.valid_to")
    assert rows and rows[0][0] is not None, "retiring sets an end date"
    assert len(rows) == 1, "retiring must not delete the edge"


@test
def test_recall_entity_works_without_knowing_the_type():
    found = STORE.recall_entity("dentist")
    assert found is not None, "should find the invented Appointment node"
    assert found["type"] == "Appointment"
    incoming = [f for f in found["facts_in"] if f["predicate"] == "HAS_APPOINTMENT"]
    assert incoming and incoming[0]["name"] == "Robin Vale", found
    assert incoming[0]["when"] == "2026-10-20"


@test
def test_recall_entity_returns_none_for_something_unknown():
    assert STORE.recall_entity("a thing nobody mentioned") is None


@test
def test_the_date_lookup_finds_the_clash():
    """The question that started this: would it catch a double-booking?"""
    STORE.remember_fact(SESSION, "Planner", "Robin Vale", "flies to", "Lisbon",
                        object_type="Trip", when="2026-10-20")
    on_that_day = STORE.what_is_on("2026-10-20")
    predicates = {f["predicate"] for f in on_that_day}
    assert {"HAS_APPOINTMENT", "FLIES_TO"} <= predicates, on_that_day
    assert len(on_that_day) >= 2, "both commitments should surface"


@test
def test_a_retired_fact_drops_out_of_the_date_lookup():
    STORE.remember_fact(SESSION, "Planner", "Robin Vale", "attends", "a workshop",
                        object_type="Event_", when="2026-11-02")
    assert STORE.what_is_on("2026-11-02"), "should be there first"
    STORE.retire_open_fact(SESSION, "Planner", "Robin Vale", "attends",
                           "a workshop", "cancelled")
    assert STORE.what_is_on("2026-11-02") == [], "a cancelled commitment is not on"


@test
def test_open_facts_join_the_rest_of_the_graph():
    # The payoff of real labels rather than a blob: machinery built for the
    # closed schema works on invented types without changing.
    STORE.remember_fact(SESSION, "Planner", "Acme Travel", "supplies",
                        "Spring Launch", subject_type="Vendor")
    path = STORE.explain_path("Project", "Billing v2", "Project", "Ledger Migration")
    assert path, "existing traversals still work"

    rows = STORE._read(
        "MATCH (v)-[r:SUPPLIES]->(p:Project {name:'Spring Launch'}) "
        "WHERE r.valid_to IS NULL RETURN labels(v)[0], v.name")
    assert rows and rows[0] == ["Vendor", "Acme Travel"], rows


@test
def test_an_episode_links_to_both_ends_of_the_fact():
    # So recall finds the conversation from either side of what was said.
    STORE.remember_fact(SESSION, "Planner", "Marco Silva", "carries",
                        "the pager", object_type="Duty")
    for name in ("Marco Silva", "the pager"):
        found = STORE.recall_entity(name)
        assert found["episodes"], f"no episode linked to {name}"


@test
def test_a_bad_type_is_refused_without_writing_anything():
    before = STORE._read("MATCH (n) RETURN count(n)")[0][0]
    try:
        STORE.remember_fact(SESSION, "Planner", "x", "is", "y",
                            object_type="Not A Valid Label!")
    except ValueError:
        after = STORE._read("MATCH (n) RETURN count(n)")[0][0]
        assert after == before, "a refused fact must not leave debris"
        return
    raise AssertionError("an invalid type should raise")


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
