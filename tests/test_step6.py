"""Step 6: consolidation, procedural memory, and relationship versioning.

    python tests/test_step6.py          # needs FalkorDB on localhost:6379

Three groups:

* **Structured JSON** -- parsing, validation, the single retry, and the cache.
  No database, no network.
* **Consolidation** -- the graph chooses the work and refuses to do it twice;
  the model only writes prose, and may decline.
* **Behaviour through agents** -- a rule taught in one session firing in
  another, and an ownership transfer that leaves both versions readable.

Models are scripted throughout, so this proves the mechanism, not that a real
model would summarise well. Consolidation quality in particular is exactly
the thing a fake adapter cannot tell you about.
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

from agents import (  # noqa: E402
    REVIEWER_TRIGGER,
    build_planner,
    build_reviewer,
    consolidate_entity,
    find_prompt_leaks,
    run_consolidation,
)
from llm import FakeAdapter, calls, says  # noqa: E402
from llm.base import LLMError  # noqa: E402
from llm.structured import JsonCache, ask_json, extract_json  # noqa: E402
from memory import MemoryStore  # noqa: E402

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


def kept(text, reason="worth keeping"):
    return says(json.dumps({"keep": True, "text": text, "reason": reason}))


def declined(reason="nothing durable here"):
    return says(json.dumps({"keep": False, "text": "", "reason": reason}))


def seed_episodes(store, session, n, entity="Ledger Migration", agent="Planner"):
    """Add n episodes about one entity, so there is something to consolidate."""
    for i in range(n):
        store.record_event(session, agent, "message",
                           f"Ledger Migration slipped again, week {i + 1}",
                           about=[("Project", entity)])


# ------------------------------------------------------------- structured JSON
@test
def test_plain_json_parses():
    assert extract_json('{"keep": true, "text": "x"}') == {"keep": True, "text": "x"}


@test
def test_fenced_json_parses():
    # Models wrap JSON in fences often enough that failing here would burn a
    # retry on an otherwise correct answer.
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}


@test
def test_json_embedded_in_prose_parses():
    assert extract_json('Sure! {"a": 1} Hope that helps.') == {"a": 1}


@test
def test_non_object_json_is_rejected():
    for bad in ("[1,2,3]", "not json at all"):
        try:
            extract_json(bad)
        except ValueError:
            continue
        raise AssertionError(f"{bad!r} should not parse to an object")


@test
def test_ask_json_retries_once_and_recovers():
    llm = FakeAdapter([says("sorry, no idea"), kept("a durable fact")])
    value = ask_json(llm, "summarise", required_keys=("keep", "text", "reason"))
    assert value["text"] == "a durable fact"
    assert llm.call_count == 2, llm.call_count
    # The retry must tell the model what was wrong, not just ask again.
    retry_text = llm.exchanges[1].messages[-1].text
    assert "not valid" in retry_text, retry_text


@test
def test_ask_json_gives_up_after_the_retry():
    llm = FakeAdapter([says("nope"), says("still nope")])
    try:
        ask_json(llm, "summarise", required_keys=("keep",))
    except LLMError as e:
        assert "2 attempt" in str(e), str(e)
        return
    raise AssertionError("expected LLMError once retries are exhausted")


@test
def test_missing_required_key_counts_as_invalid():
    llm = FakeAdapter([says('{"text": "x"}'), kept("recovered")])
    value = ask_json(llm, "p", required_keys=("keep", "text", "reason"))
    assert value["text"] == "recovered"
    assert "missing required key" in llm.exchanges[1].messages[-1].text


@test
def test_cache_prevents_a_second_model_call():
    cache = JsonCache()
    llm = FakeAdapter([kept("cached fact")])
    first = ask_json(llm, "same prompt", required_keys=("keep",), cache=cache)
    second = ask_json(llm, "same prompt", required_keys=("keep",), cache=cache)
    assert first == second
    assert llm.call_count == 1, f"cache miss: model called {llm.call_count} times"
    assert len(cache) == 1


@test
def test_cache_survives_a_restart_when_file_backed():
    path = Path(ROOT) / ".cache" / "test-cache.json"
    if path.exists():
        path.unlink()
    cache = JsonCache(path)
    ask_json(FakeAdapter([kept("persisted")]), "p", required_keys=("keep",),
             cache=cache)
    # A fresh cache object reading the same file must hit, with no model at all.
    reloaded = JsonCache(path)
    empty_llm = FakeAdapter([])
    value = ask_json(empty_llm, "p", required_keys=("keep",), cache=reloaded)
    assert value["text"] == "persisted", value
    assert empty_llm.call_count == 0
    path.unlink()


# ---------------------------------------------------------------- consolidation
@test
def test_below_threshold_nothing_is_a_candidate():
    store = fresh_store()
    s = store.start_session(user="demo-user", session_id="s1")
    # Checkout Redesign deliberately: the fixture seeds no events about it,
    # so the count here is exactly what this test adds.
    seed_episodes(store, s, 2, entity="Checkout Redesign")
    names = [c["name"] for c in store.consolidation_candidates(min_events=3)]
    assert "Checkout Redesign" not in names, names


@test
def test_graph_selects_the_entity_with_most_unabsorbed_episodes():
    store = fresh_store()
    s = store.start_session(user="demo-user", session_id="s1")
    seed_episodes(store, s, 4)
    top = store.consolidation_candidates(min_events=3)[0]
    assert top["name"] == "Ledger Migration", top
    assert top["episodes"] >= 4, top
    assert len(top["event_ids"]) == top["episodes"]


@test
def test_consolidation_writes_an_insight_linked_to_every_source():
    store = fresh_store()
    s = store.start_session(user="demo-user", session_id="s1")
    seed_episodes(store, s, 4)
    before = store.events_about("Project", "Ledger Migration")

    result = consolidate_entity(store, FakeAdapter([kept("Ledger Migration has "
                                "slipped repeatedly and keeps blocking the launch.")]),
                                s, "Planner", "Project", "Ledger Migration")
    assert result.kept, result.summary()

    insights = store.current_insights("Project", "Ledger Migration")
    assert len(insights) == 1, insights
    assert insights[0]["sources"] == len(before), \
        f"expected {len(before)} source links, got {insights[0]['sources']}"

    sources = store.insight_sources(result.insight_id)
    assert {s_["id"] for s_ in sources} == {e["id"] for e in before}


@test
def test_consolidated_episodes_are_not_consolidated_again():
    store = fresh_store()
    s = store.start_session(user="demo-user", session_id="s1")
    seed_episodes(store, s, 4)
    consolidate_entity(store, FakeAdapter([kept("a fact")]), s, "Planner",
                       "Project", "Ledger Migration")

    left = store.events_about("Project", "Ledger Migration", only_unconsolidated=True)
    assert left == [], f"absorbed episodes should drop out, got {left}"
    names = [c["name"] for c in store.consolidation_candidates(min_events=3)]
    assert "Ledger Migration" not in names, names


@test
def test_the_consolidation_event_does_not_become_raw_material():
    # Otherwise each consolidation feeds the next and the loop never settles.
    store = fresh_store()
    s = store.start_session(user="demo-user", session_id="s1")
    seed_episodes(store, s, 3)
    consolidate_entity(store, FakeAdapter([kept("a fact")]), s, "Planner",
                       "Project", "Ledger Migration")

    rows = store._read(
        "MATCH (e:Event {kind:'consolidation'})-[:ABOUT]->(:Project "
        "{name:'Ledger Migration'}) RETURN count(e)")
    assert rows[0][0] == 1, "the consolidation event should still be on the timeline"
    assert store.events_about("Project", "Ledger Migration") == [], \
        "but it must not be offered as raw material"


@test
def test_a_declined_consolidation_writes_nothing_and_keeps_the_episodes():
    store = fresh_store()
    s = store.start_session(user="demo-user", session_id="s1")
    seed_episodes(store, s, 3)
    before = len(store.events_about("Project", "Ledger Migration"))

    result = consolidate_entity(store, FakeAdapter([declined("just scheduling chatter")]),
                                s, "Planner", "Project", "Ledger Migration")
    assert not result.kept, result.summary()
    assert "chatter" in result.reason
    assert store.current_insights("Project", "Ledger Migration") == []
    # Still available, so a later pass with more context can try again.
    assert len(store.events_about("Project", "Ledger Migration")) == before


@test
def test_recall_related_leads_with_consolidated_knowledge():
    store = fresh_store()
    s = store.start_session(user="demo-user", session_id="s1")
    seed_episodes(store, s, 3)
    consolidate_entity(store, FakeAdapter([kept("Ledger Migration keeps slipping.")]),
                       s, "Planner", "Project", "Ledger Migration")

    rel = store.recall_related("Project", "Ledger Migration")
    assert rel["insights"], rel
    assert rel["insights"][0]["text"] == "Ledger Migration keeps slipping."


@test
def test_retiring_an_insight_removes_it_from_current():
    store = fresh_store()
    s = store.start_session(user="demo-user", session_id="s1")
    seed_episodes(store, s, 3)
    absorbed = len(store.events_about("Project", "Ledger Migration"))
    result = consolidate_entity(store, FakeAdapter([kept("stale fact")]), s,
                                "Planner", "Project", "Ledger Migration")
    assert store.retire_insight(s, "Planner", result.insight_id, "no longer true")
    assert store.current_insights("Project", "Ledger Migration") == []
    # The sources survive -- retiring is not deleting.
    assert len(store.insight_sources(result.insight_id)) == absorbed


@test
def test_run_consolidation_handles_several_entities():
    store = fresh_store()
    s = store.start_session(user="demo-user", session_id="s1")
    seed_episodes(store, s, 3, entity="Ledger Migration")
    seed_episodes(store, s, 3, entity="Mobile App")

    results = run_consolidation(
        store, FakeAdapter([kept("ledger fact"), kept("mobile fact")]),
        s, "Planner", max_entities=2)
    assert len(results) == 2, [r.summary() for r in results]
    assert all(r.kept for r in results), [r.summary() for r in results]
    assert len(store.current_insights()) == 2


@test
def test_insight_with_an_unknown_source_event_is_refused():
    store = fresh_store()
    s = store.start_session(user="demo-user", session_id="s1")
    try:
        store.record_insight(s, "Planner", "Project", "Mobile App",
                             "something", ["evt-does-not-exist"])
    except LookupError as e:
        assert "linked 0" in str(e), str(e)
        return
    raise AssertionError("an insight must not be written with dangling sources")


# ----------------------------------------------- procedural memory via an agent
@test
def test_a_rule_taught_in_one_session_fires_in_another():
    store = fresh_store()
    s1 = store.start_session(user="demo-user", session_id="s1")
    planner = build_planner(store, s1, FakeAdapter([
        calls("learn_procedure", {
            "name": "notify-finance-lead",
            "trigger": "a finance system is affected",
            "action": "notify Priya Nair",
            "applies_to_systems": ["Ledger Service"],
        }),
        says("Noted, I'll apply that from now on."),
    ]))
    planner.run("whenever a finance system is affected, notify Priya")

    # A new session. The Reviewer is told nothing about the rule.
    s2 = store.start_session(user="demo-user", session_id="s2")
    reviewer = build_reviewer(store, s2, FakeAdapter([
        calls("get_applicable_procedures", {"label": "Project", "name": "Billing v2"}),
        says("Rule applies: notify Priya Nair."),
    ]))
    turn = reviewer.run(REVIEWER_TRIGGER)

    hit = json.loads(turn.steps[0].result)
    assert hit, "the rule should have been found"
    assert hit[0]["name"] == "notify-finance-lead", hit
    # Billing v2 was never named in the rule; it matched through USES.
    assert "via system" in hit[0]["matched"], hit

    leaks = find_prompt_leaks(reviewer, turn,
                              ["notify-finance-lead", "Priya", "Ledger Service"])
    assert leaks == [], f"the Reviewer was told the rule outright: {leaks}"


@test
def test_a_retired_rule_stops_firing_for_the_agent():
    store = fresh_store()
    s1 = store.start_session(user="demo-user", session_id="s1")
    pid = store.learn_procedure(s1, "Planner", "temporary", "x", "y",
                                applies_to=[("System", "Ledger Service")])
    store.retire_procedure(s1, "Planner", pid, "superseded")

    s2 = store.start_session(user="demo-user", session_id="s2")
    reviewer = build_reviewer(store, s2, FakeAdapter([
        calls("get_applicable_procedures", {"label": "Project", "name": "Billing v2"}),
        says("No rules apply."),
    ]))
    turn = reviewer.run(REVIEWER_TRIGGER)
    assert json.loads(turn.steps[0].result) == [], turn.steps[0].result


# ------------------------------------- relationship versioning via an agent (5b)
@test
def test_agent_transfers_ownership_and_both_versions_survive():
    store = fresh_store()
    s = store.start_session(user="demo-user", session_id="s1")
    assert store.current_facts("OWNS", to_name="Ledger Migration")[0]["from"] == "Lee Park"

    reviewer = build_reviewer(store, s, FakeAdapter([
        calls("retire_fact", {"rel_type": "OWNS", "from_name": "Lee Park",
                              "to_name": "Ledger Migration",
                              "reason": "Dana has been doing the work"}),
        calls("assert_fact", {"rel_type": "OWNS", "from_name": "Dana Ruiz",
                              "to_name": "Ledger Migration"}),
        says("Ownership moved from Lee Park to Dana Ruiz."),
    ]))
    turn = reviewer.run("Dana has been running Ledger Migration for months now")

    assert turn.tools_used == ["retire_fact", "assert_fact"], turn.tools_used
    assert json.loads(turn.steps[0].result)["retired"] == 1, turn.steps[0].result

    current = store.current_facts("OWNS", to_name="Ledger Migration")
    assert [c["from"] for c in current] == ["Dana Ruiz"], current

    history = store.fact_history("OWNS", to_name="Ledger Migration")
    assert len(history) == 2, history
    assert history[0]["from"] == "Lee Park" and history[0]["valid_to"] is not None
    assert history[1]["from"] == "Dana Ruiz" and history[1]["current"]


@test
def test_the_informal_expert_becomes_the_owner():
    # The payoff of moment 5b: Dana already worked on it, and now the graph
    # says so officially, with the history still readable.
    store = fresh_store()
    s = store.start_session(user="demo-user", session_id="s1")
    worked = [w["from"] for w in store.current_facts("WORKED_ON",
                                                     to_name="Ledger Migration")]
    assert "Dana Ruiz" in worked, worked

    store.retire_fact(s, "Reviewer", "OWNS", "Lee Park", "Ledger Migration", "handover")
    store.assert_fact(s, "Reviewer", "OWNS", "Dana Ruiz", "Ledger Migration")

    owner = store.current_facts("OWNS", to_name="Ledger Migration")[0]["from"]
    assert owner == "Dana Ruiz"
    past = [h for h in store.fact_history("OWNS", to_name="Ledger Migration")
            if not h["current"]]
    assert past and past[0]["from"] == "Lee Park", past


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
