"""The demo script, as runnable moments.

Each moment is one beat from PROJECT_CONTEXT.md section 7, wired to the real
agents, the real tools and the real graph. Only the model is scripted: the
FakeAdapter replays a fixed sequence of tool choices, so a recording is
identical every take and costs nothing.

What is genuinely real in every moment: the Cypher, the graph writes, the
versioning, the task claim, the consolidation selection, and the before/after
diff shown in the changes panel. What is scripted: which tool the model
decides to call. That distinction is worth stating out loud during a demo
rather than glossing over.
"""

import importlib.util
import json
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from agents import (  # noqa: E402
    REVIEWER_TRIGGER,
    build_planner,
    build_reviewer,
    consolidate_entity,
)
from llm import FakeAdapter, calls, says  # noqa: E402
from llm.fake import last_tool_result  # noqa: E402
from memory import MemoryStore  # noqa: E402
from memory.store import DEFAULT_HOST, DEFAULT_PORT  # noqa: E402

DELAY = "Delay Spring Launch to May 15, 2027"


# --------------------------------------------------------------------- types
@dataclass
class MomentResult:
    narration: str
    steps: list = field(default_factory=list)
    path: list | None = None            # node names to highlight in the graph
    facts: dict = field(default_factory=dict)
    note: str = ""                      # anything the viewer should know


@dataclass
class Moment:
    key: str
    number: str
    title: str
    shows: str
    track: str
    run: Callable[[MemoryStore], MomentResult]
    chained: bool = False               # depends on earlier moments


# ------------------------------------------------------------------- helpers
def reset_world():
    """Reseed both tenants. Every demo run should start here."""
    spec = importlib.util.spec_from_file_location(
        "seed", ROOT / "scripts" / "seed.py")
    seed = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(seed)
    from falkordb import FalkorDB
    db = FalkorDB(host=DEFAULT_HOST, port=DEFAULT_PORT)
    for name in ("world_acme.json", "world_globex.json"):
        path = ROOT / "data" / name
        world = json.loads(path.read_text(encoding="utf-8"))
        seed.validate(world, path)
        seed.seed_tenant(db, world, path)


def _session(store, sid):
    try:
        return store.start_session(user="demo-user", session_id=sid)
    except Exception:
        return sid                      # already open from an earlier run


def _steps(turn):
    return [{"tool": s.name, "args": s.arguments, "result": s.result,
             "error": s.is_error} for s in turn.steps]


def _live_decision(store, project="Spring Launch"):
    live = store.current_decisions(project=project)
    return live[0]["id"] if live else None


# ------------------------------------------------------------------- moments
def m1_record_delay(store):
    session = _session(store, "sess-day1")
    script = [
        calls("current_decisions", {"project": "Spring Launch"}),
        lambda m, t, s: calls("record_decision", {
            "title": DELAY,
            "reason": "Ledger Migration is blocked",
            "affects": ["Spring Launch"],
            "made_by": "Sam Okafor",
            "supersedes": json.loads(last_tool_result(m))[0]["id"],
        }),
        lambda m, t, s: calls("create_task", {
            "assigned_to": "Reviewer",
            "title": "Work out what the launch delay breaks downstream",
            "concerns_decision": json.loads(last_tool_result(m))["decision_id"],
            "concerns_project": "Spring Launch",
        }),
        says("I've recorded the delay with its reason, retired the April date it "
             "replaces, and left the Reviewer a task to work out the fallout."),
    ]
    turn = build_planner(store, session, FakeAdapter(script)).run(
        "We need to delay the Spring Launch to May 15, 2027 - Ledger Migration is "
        "blocked. Someone should check what that breaks.")
    return MomentResult(turn.text, _steps(turn), facts={"session": session})


def m2_reviewer_claims(store):
    session = _session(store, "sess-day1-reviewer")
    script = [
        calls("claim_task", {}),
        lambda m, t, s: (
            calls("get_task_context",
                  {"task_id": json.loads(last_tool_result(m))["task_id"]})
            if json.loads(last_tool_result(m) or "{}").get("task_id")
            else says("Nothing is assigned to me. Run moment 1 first.")),
        lambda m, t, s: says(
            f"Picked up work on {json.loads(last_tool_result(m))['project']}, "
            f"concerning: {json.loads(last_tool_result(m))['decision']['title']}."),
    ]
    turn = build_reviewer(store, session, FakeAdapter(script)).run(REVIEWER_TRIGGER)
    return MomentResult(
        turn.text, _steps(turn),
        note="The Reviewer was handed only this sentence, which names nothing: "
             f"“{REVIEWER_TRIGGER}”")


def m3_block_the_release(store):
    session = _session(store, "sess-day2")
    did = _live_decision(store)
    script = [
        calls("depends_on_chain", {"project": "Billing v2"}),
        calls("current_decisions", {"project": "Spring Launch"}),
        calls("explain_path", {"from_label": "Project", "from_name": "Billing v2",
                               "to_label": "Project", "to_name": "Ledger Migration"}),
        calls("create_ticket", {
            "title": "Billing v2 blocked by the Spring Launch delay",
            "blocks_project": "Billing v2",
            "cites_decision": did}),
        says("No. Billing v2 sits behind Spring Launch, which is delayed to May 15, 2027 "
             "because Ledger Migration is blocked. I've filed a ticket citing that "
             "decision."),
    ]
    turn = build_reviewer(store, session, FakeAdapter(script)).run(
        "Can we ship Billing v2 this sprint?")
    path = store.explain_path("Project", "Billing v2", "Project", "Ledger Migration")
    return MomentResult(
        turn.text, _steps(turn),
        path=path["chain"] if path else None,
        note="A new session, the next day. Nothing was carried over in code - "
             "the Reviewer read all of this from the graph.")


def m4_teach_a_rule(store):
    s1 = _session(store, "sess-rule-taught")
    # The rule is written by the curator, not by the Planner. Splitting the
    # jobs is the point: the model answering the person is no longer also the
    # model deciding what to write down, which is what made rule capture
    # unreliable in the first place (docs/TESTING.md section 3b). So the
    # scripted adapter serves the reply first, then the curation JSON.
    planner = build_planner(store, s1, FakeAdapter([
        says("Noted. I'll apply that whenever a finance system is involved."),
        says(json.dumps({
            "topics": ["finance notifications"],
            "entities": ["Ledger Service"],
            "facts": [],
            "rules": [{
                "name": "notify-finance-lead",
                "trigger": "a finance system is affected",
                "action": "notify Priya Nair",
                "applies_to_systems": ["Ledger Service"],
                "applies_to_projects": []}],
            "settles": "",
        })),
    ]))
    taught = planner.run("Whenever a finance system is affected, notify Priya.")

    s2 = _session(store, "sess-rule-fires")
    reviewer = build_reviewer(store, s2, FakeAdapter([
        calls("get_applicable_procedures", {"label": "Project", "name": "Billing v2"}),
        lambda m, t, s: says(
            "Before I touch Billing v2: a standing rule applies - "
            f"{json.loads(last_tool_result(m))[0]['action']} "
            f"({json.loads(last_tool_result(m))[0]['matched']})."
            if json.loads(last_tool_result(m) or "[]")
            else "No standing rules apply here."),
    ]))
    fired = reviewer.run("I'm about to make a change to Billing v2.")
    return MomentResult(
        f"**Taught (session 1):** {taught.text}\n\n**Fired (session 2):** {fired.text}",
        _steps(taught) + _steps(fired),
        note="The rule was taught about Ledger Service. It fired for Billing v2, "
             "which the rule never mentions, because Billing v2 USES that system.")


def m5a_supersede(store):
    session = _session(store, "sess-supersede")
    old = _live_decision(store)
    script = [
        calls("current_decisions", {"project": "Spring Launch"}),
        calls("record_decision", {
            "title": "Delay Spring Launch to June 1, 2027",
            "reason": "Ledger Migration slipped again",
            "affects": ["Spring Launch"],
            "supersedes": old}),
        lambda m, t, s: calls("decision_history", {
            "decision_id": json.loads(last_tool_result(m))["decision_id"]}),
        says("June 1, 2027 is now the current date. The May 15, 2027 decision is retired but "
             "still readable, and the chain back through it is intact."),
    ]
    turn = build_planner(store, session, FakeAdapter(script)).run(
        "Ledger Migration slipped again. Push the launch to June 1, 2027.")
    return MomentResult(turn.text, _steps(turn),
                        note="Nothing was deleted. The old decision has an end "
                             "date and a SUPERSEDES link pointing at it.")


def m5b_transfer_ownership(store):
    session = _session(store, "sess-ownership")
    script = [
        calls("current_facts", {"rel_type": "OWNS", "to_name": "Ledger Migration"}),
        calls("retire_fact", {"rel_type": "OWNS", "from_name": "Lee Park",
                              "to_name": "Ledger Migration",
                              "reason": "Dana has been running it for months"}),
        calls("assert_fact", {"rel_type": "OWNS", "from_name": "Dana Ruiz",
                              "to_name": "Ledger Migration"}),
        says("Ownership of Ledger Migration has moved from Lee Park to Dana Ruiz. "
             "Dana had been the one working on it all along."),
    ]
    turn = build_reviewer(store, session, FakeAdapter(script)).run(
        "Dana has effectively been running Ledger Migration. Make it official.")
    history = store.fact_history("OWNS", to_name="Ledger Migration")
    return MomentResult(
        turn.text, _steps(turn),
        facts={"ownership history": history},
        note="This is the only moment that retires a relationship rather than a "
             "node - most of the schema's machinery exists for this.")


def _summarise_ledger(messages, tools, system):
    """Scripted consolidation that actually reads the episodes it is given.

    A fixed summary would be wrong whenever a different set of moments had
    run first -- it would describe slips while quietly absorbing an ownership
    change. The episodes arrive in the prompt, so this reads them and covers
    what is genuinely there, which is what a real model would do. Still
    deterministic, so a recording is repeatable.
    """
    prompt = messages[0].text.lower()
    slips = prompt.count("slipped")
    themes = []
    if slips:
        themes.append(f"has slipped repeatedly ({slips} separate reports)")
    if "owns" in prompt:
        themes.append("changed owner over the same period")
    if "depend" in prompt:
        themes.append("sits upstream of the Spring Launch train")
    body = "; ".join(themes) if themes else "has been mentioned repeatedly"
    return says(json.dumps({
        "keep": True,
        "text": f"Ledger Migration {body}. It is the recurring cause of Spring "
                f"Launch delays.",
        "reason": "a pattern across several sessions rather than a one-off",
    }))


def m5c_consolidate(store):
    session = _session(store, "sess-consolidate")
    # Three separate sessions each noticing the same thing. Written here so the
    # moment is self-contained; in a longer-running system they accumulate.
    for week in (1, 2, 3):
        store.record_event(session, "Reviewer", "message",
                           f"Standup note: Ledger Migration slipped again, week {week}",
                           about=[("Project", "Ledger Migration")])
    before = store.events_about("Project", "Ledger Migration")
    result = consolidate_entity(
        store, FakeAdapter([_summarise_ledger]),
        session, "Reviewer", "Project", "Ledger Migration")
    sources = store.insight_sources(result.insight_id) if result.insight_id else []
    return MomentResult(
        f"Folded {result.episodes} separate episodes into one lasting fact:\n\n"
        f"**{result.text}**",
        facts={"episodes absorbed": [e["summary"] for e in sources],
               "left unconsolidated": len(store.events_about(
                   "Project", "Ledger Migration"))},
        note=f"The graph chose this entity: it had {len(before)} episodes with no "
             f"DERIVED_FROM edge. Those episodes are now absorbed and will not be "
             f"consolidated again. The model only wrote the sentence.")


def m6_tenant_isolation(store):
    acme, globex = MemoryStore("acme"), MemoryStore("globex")
    out = {}
    for name, s in (("acme", acme), ("globex", globex)):
        chain = s.depends_on_chain("Billing v2")
        blockers = [d for hop in chain
                    for d in s.current_decisions(project=hop["project"])]
        own = s.current_decisions(project="Billing v2")
        out[name] = {
            "depends on": [c["project"] for c in chain] or "nothing",
            "blocked by": [b["title"] for b in blockers] or "nothing",
            "own decisions": [d["title"] for d in own] or "none",
            "verdict": "NO - blocked upstream" if blockers else "YES - clear",
        }
    return MomentResult(
        f"**acme:** {out['acme']['verdict']}\n\n**globex:** {out['globex']['verdict']}",
        facts=out,
        note="Identical code, identical question. No tenant column and no tenant "
             "filter anywhere - each tenant is its own graph, so a leak cannot be "
             "caused by a forgotten WHERE clause.")


def m7_concurrency(store):
    from falkordb import FalkorDB
    graph = "tenant_demo_load"
    db = FalkorDB(host=DEFAULT_HOST, port=DEFAULT_PORT)
    try:
        db.select_graph(graph).delete()
    except Exception:
        pass
    load = MemoryStore("demo_load")
    load._write("CREATE (:Agent {name:'Reviewer'})")
    session = load.start_session(user="demo-user", session_id="sess-load")
    tasks = 40
    load._write(
        "UNWIND range(1,$n) AS i MATCH (a:Agent {name:'Reviewer'}) "
        "CREATE (t:Task {id:'task-'+toString(i), title:'unit', status:'open', "
        "created_at:$now}) CREATE (t)-[:ASSIGNED_TO]->(a)",
        n=tasks, now=load.now())

    claimed, workers = [], 8
    lock, gate = threading.Lock(), threading.Barrier(workers)

    def work(i):
        own = MemoryStore("demo_load")
        mine = []
        gate.wait()
        while True:
            tid = own.claim_task("Reviewer")
            if tid is None:
                break
            own.complete_task(session, "Reviewer", tid, f"done by {i}")
            mine.append(tid)
        with lock:
            claimed.extend(mine)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(workers)]
    t0 = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.perf_counter() - t0
    duplicates = len(claimed) - len(set(claimed))
    try:
        db.select_graph(graph).delete()
    except Exception:
        pass

    return MomentResult(
        f"{workers} agents drained {tasks} tasks in {elapsed * 1000:.0f} ms "
        f"with **{duplicates} double-claims**.",
        facts={"workers": workers, "tasks": tasks,
               "claimed": len(claimed), "double-claimed": duplicates,
               "wall clock ms": round(elapsed * 1000)},
        note="The claim is one Cypher query that both tests and sets the status. "
             "Written as read-then-write across two queries, every worker "
             "believes it won - see scripts/demo_concurrency.py for that control "
             "run. Runs on a throwaway graph, not the demo data.")


def m8_single_point_of_failure(store):
    ranking = store.critical_projects()
    top = ranking["ranking"][0]["project"] if ranking["ranking"] else None
    downstream = store.dependents_of(top) if top else []
    exposed = store.current_facts("WORKED_ON", to_name=top) if top else []
    path = store.explain_path("Project", "Billing v2", "Project", "Ledger Migration")
    return MomentResult(
        f"**{top}** - betweenness centrality over the dependency graph.",
        facts={"ranking": ranking["ranking"],
               "retired edges included": ranking["retired_dependency_edges"],
               "what sits behind it": [d["project"] for d in downstream],
               "people exposed": [p["from"] for p in exposed]},
        path=path["chain"] if path else None,
        note="Answers what, not who: OWNS and WORKED_ON point from a person into "
             "a project, so a person is never an intermediate node and always "
             "scores zero. The algorithm also cannot filter on validity, so the "
             "count of retired edges is reported alongside the ranking.")


MOMENTS = [
    Moment("m1", "1", "Record the launch delay",
           "Episodic + semantic writes, provenance", "02", m1_record_delay),
    Moment("m2", "2", "Reviewer picks up the task",
           "Handoff through the graph only", "02", m2_reviewer_claims, chained=True),
    Moment("m3", "3", "Block the billing release",
           "Persistence, multi-hop, action, explainability", "02 + 01",
           m3_block_the_release, chained=True),
    Moment("m4", "4", "Teach a rule, watch it fire later",
           "Procedural memory changing behaviour", "02", m4_teach_a_rule),
    Moment("m5a", "5a", "Supersede a decision",
           "Memory that updates (nodes)", "02", m5a_supersede, chained=True),
    Moment("m5b", "5b", "Transfer ownership",
           "Memory that updates (relationships)", "02", m5b_transfer_ownership),
    Moment("m5c", "5c", "Consolidate repeated episodes",
           "Memory that gets smaller, with provenance", "02", m5c_consolidate),
    Moment("m6", "6", "Same question, other tenant",
           "Per-tenant isolation", "02", m6_tenant_isolation),
    Moment("m7", "7", "Many agents at once",
           "High concurrency, measured", "02", m7_concurrency),
    Moment("m8", "8", "What is the single point of failure?",
           "Graph algorithm as a tool", "01", m8_single_point_of_failure),
]

BY_KEY = {m.key: m for m in MOMENTS}
