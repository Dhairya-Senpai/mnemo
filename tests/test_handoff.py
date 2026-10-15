"""Two agents, one graph, no prompt passing. Zero tokens.

    python tests/test_handoff.py        # needs FalkorDB on localhost:6379

The claim under test is narrow and checkable: the Reviewer is handed one
fixed trigger sentence and nothing else, and everything it goes on to do
comes from the graph. The leak tests are the heart of this file -- without
them "agents communicate only through the graph" is just an assertion in a
README.

The models are scripted, so this proves the plumbing, not that a real model
would choose these tool calls.
"""

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
    find_prompt_leaks,
    run_handoff,
)
from llm import FakeAdapter, calls, says  # noqa: E402
from llm.fake import last_tool_result  # noqa: E402
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
    """Reload the Acme fixture into tenant_test and return a store.

    Every test reseeds, because these tests mutate the graph and a shared
    fixture would make them order-dependent.
    """
    import contextlib
    import importlib.util
    import io
    world = json.loads((ROOT / "data" / "world_acme.json").read_text(encoding="utf-8"))
    world["graph"] = f"tenant_{TENANT}"
    spec = importlib.util.spec_from_file_location("seed", ROOT / "scripts" / "seed.py")
    seed = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(seed)
    with contextlib.redirect_stdout(io.StringIO()):     # the seeder is chatty
        seed.seed_tenant(FalkorDB(host="localhost", port=6379), world,
                         ROOT / "data" / "world_acme.json")
    return MemoryStore(TENANT)


def _decision_id(store):
    """The decision currently in force for Spring Launch."""
    live = store.current_decisions(project="Spring Launch")
    return live[0]["id"] if live else None


# --------------------------------------------------------- scripted responders
def claim_then(next_step):
    """Reviewer script: claim a task, then react to the id it got back."""
    def responder(messages, tools, system):
        result = json.loads(last_tool_result(messages) or "{}")
        task_id = result.get("task_id")
        if not task_id:
            return says("Nothing is assigned to me.")
        return next_step(task_id)
    return responder


def from_task_context(build):
    """Reviewer script: react to whatever get_task_context returned."""
    def responder(messages, tools, system):
        ctx = json.loads(last_tool_result(messages) or "null")
        return build(ctx)
    return responder


PLANNER_SCRIPT = [
    calls("current_decisions", {"project": "Spring Launch"}),
    lambda m, t, s: calls("record_decision", {
        "title": "Delay Spring Launch to May 15",
        "reason": "Ledger Migration is blocked",
        "affects": ["Spring Launch"],
        "supersedes": json.loads(last_tool_result(m))[0]["id"],
    }),
    lambda m, t, s: calls("create_task", {
        "assigned_to": "Reviewer",
        "title": "Work out what the launch delay breaks downstream",
        "concerns_decision": json.loads(last_tool_result(m))["decision_id"],
        "concerns_project": "Spring Launch",
    }),
    says("Recorded the delay and asked the Reviewer to check the impact."),
]


print("seeding fixture graph tenant_test ...")
STORE = fresh_store()
print()


# ------------------------------------------------------------------- the tests
@test
def test_trigger_sentence_names_nothing_specific():
    # If this ever grows a project name, the handoff stops being a handoff.
    for word in ("spring", "launch", "billing", "ledger", "delay",
                 "decision", "lee", "dana"):
        assert word not in REVIEWER_TRIGGER.lower(), \
            f"the trigger leaks {word!r}: {REVIEWER_TRIGGER}"


@test
def test_planner_leaves_an_open_task_in_the_graph():
    store = fresh_store()
    sess = store.start_session(user="demo-user", session_id="s1")
    planner = build_planner(store, sess, FakeAdapter(PLANNER_SCRIPT))
    turn = planner.run("delay the spring launch to May 15 and check the fallout")

    assert turn.tools_used == ["current_decisions", "record_decision", "create_task"], \
        turn.tools_used
    open_tasks = store.open_tasks("Reviewer")
    assert len(open_tasks) == 1, open_tasks
    assert "downstream" in open_tasks[0]["title"]


@test
def test_reviewer_claims_and_reads_the_task_from_a_different_session():
    store = fresh_store()
    s1 = store.start_session(user="demo-user", session_id="s1")
    build_planner(store, s1, FakeAdapter(PLANNER_SCRIPT)).run("delay the launch")

    # A separate session, as if the next day. Nothing is carried over in code.
    s2 = store.start_session(user="demo-user", session_id="s2")
    reviewer = build_reviewer(store, s2, FakeAdapter([
        calls("claim_task", {}),
        claim_then(lambda tid: calls("get_task_context", {"task_id": tid})),
        from_task_context(lambda ctx: says(
            f"Picked up work on {ctx['project']}: {ctx['decision']['title']}")),
    ]))
    turn = reviewer.run(REVIEWER_TRIGGER)

    assert turn.tools_used == ["claim_task", "get_task_context"], turn.tools_used
    assert "Spring Launch" in turn.text, turn.text
    assert "Delay Spring Launch to May 15" in turn.text, turn.text
    assert store.open_tasks("Reviewer") == [], "task should no longer be open"


@test
def test_nothing_about_the_task_was_passed_to_the_reviewer_in_prose():
    store = fresh_store()
    s1 = store.start_session(user="demo-user", session_id="s1")
    build_planner(store, s1, FakeAdapter(PLANNER_SCRIPT)).run("delay the launch")

    s2 = store.start_session(user="demo-user", session_id="s2")
    reviewer = build_reviewer(store, s2, FakeAdapter([
        calls("claim_task", {}),
        claim_then(lambda tid: calls("get_task_context", {"task_id": tid})),
        from_task_context(lambda ctx: says(f"Working on {ctx['project']}.")),
    ]))
    turn = reviewer.run(REVIEWER_TRIGGER)

    needles = ["Delay Spring Launch to May 15", "Ledger Migration is blocked",
               "Spring Launch", "downstream"]
    leaks = find_prompt_leaks(reviewer, turn, needles)
    assert leaks == [], f"the Reviewer was told these outright: {leaks}"


@test
def test_but_the_reviewer_did_learn_it_from_tool_results():
    # The mirror of the previous test. Without this, a Reviewer that simply
    # knew nothing would also pass the leak check.
    store = fresh_store()
    s1 = store.start_session(user="demo-user", session_id="s1")
    build_planner(store, s1, FakeAdapter(PLANNER_SCRIPT)).run("delay the launch")

    s2 = store.start_session(user="demo-user", session_id="s2")
    reviewer = build_reviewer(store, s2, FakeAdapter([
        calls("claim_task", {}),
        claim_then(lambda tid: calls("get_task_context", {"task_id": tid})),
        from_task_context(lambda ctx: says(f"Working on {ctx['project']}.")),
    ]))
    turn = reviewer.run(REVIEWER_TRIGGER)

    from_graph = "\n".join(s.result for s in turn.steps)
    assert "Spring Launch" in from_graph, from_graph
    assert "Delay Spring Launch to May 15" in from_graph, from_graph


@test
def test_the_leak_check_actually_catches_a_leak():
    """Guard the guard: a leak check that cannot fire proves nothing.

    Same setup as the previous two tests, but the Reviewer is briefed the way
    a message-passing design would brief it. The check must catch that.
    """
    store = fresh_store()
    s1 = store.start_session(user="demo-user", session_id="s1")
    build_planner(store, s1, FakeAdapter(PLANNER_SCRIPT)).run("delay the launch")

    s2 = store.start_session(user="demo-user", session_id="s2")
    reviewer = build_reviewer(store, s2, FakeAdapter([says("Understood.")]))
    leaky = ("The Planner delayed Spring Launch to May 15 because Ledger "
             "Migration is blocked. Please check the impact.")
    turn = reviewer.run(leaky)

    leaks = find_prompt_leaks(reviewer, turn,
                              ["Spring Launch", "Ledger Migration is blocked"])
    assert sorted(leaks) == ["Ledger Migration is blocked", "Spring Launch"], leaks


@test
def test_reviewer_says_so_when_no_work_is_waiting():
    store = fresh_store()
    s2 = store.start_session(user="demo-user", session_id="s2")
    reviewer = build_reviewer(store, s2, FakeAdapter([
        calls("claim_task", {}),
        claim_then(lambda tid: calls("get_task_context", {"task_id": tid})),
    ]))
    turn = reviewer.run(REVIEWER_TRIGGER)
    assert turn.steps[0].result == '{"task_id":null}', turn.steps[0].result
    assert "Nothing is assigned" in turn.text, turn.text


@test
def test_only_one_of_two_reviewers_gets_the_task():
    store = fresh_store()
    s1 = store.start_session(user="demo-user", session_id="s1")
    build_planner(store, s1, FakeAdapter(PLANNER_SCRIPT)).run("delay the launch")

    claimed = []
    for label in ("a", "b"):
        sess = store.start_session(user="demo-user", session_id=f"s2{label}")
        r = build_reviewer(store, sess, FakeAdapter([
            calls("claim_task", {}), says("done"),
        ]))
        turn = r.run(REVIEWER_TRIGGER)
        got = json.loads(turn.steps[0].result)["task_id"]
        if got:
            claimed.append(got)
    assert len(claimed) == 1, f"the task was claimed {len(claimed)} times: {claimed}"


@test
def test_full_chain_delay_to_blocked_ticket_citing_the_decision():
    """Demo moments 1 to 3, end to end, with the graph as the only channel."""
    store = fresh_store()
    s1 = store.start_session(user="demo-user", session_id="s1")
    s2 = store.start_session(user="demo-user", session_id="s2")

    reviewer_script = [
        calls("claim_task", {}),
        claim_then(lambda tid: calls("get_task_context", {"task_id": tid})),
        from_task_context(lambda ctx: calls("dependents_of",
                                            {"project": ctx["project"]})),
        lambda m, t, s: calls("create_ticket", {
            "title": "Billing v2 blocked by the Spring Launch delay",
            "blocks_project": json.loads(last_tool_result(m))[0]["project"],
            "cites_decision": _decision_id(store),
        }),
        says("Billing v2 and Mobile App sit behind Spring Launch. Filed a ticket."),
    ]

    handoff = run_handoff(
        store,
        user_message="delay the spring launch to May 15 and check the fallout",
        planner_llm=FakeAdapter(PLANNER_SCRIPT),
        reviewer_llm=FakeAdapter(reviewer_script),
        planner_session=s1,
        reviewer_session=s2,
        leak_needles=["Delay Spring Launch to May 15", "Spring Launch",
                      "Billing v2", "Ledger Migration is blocked"],
    )

    assert handoff.task_id, "the Planner should have opened a task"
    assert handoff.clean, f"the handoff leaked: {handoff.leaks}"
    assert handoff.reviewer.tools_used == [
        "claim_task", "get_task_context", "dependents_of", "create_ticket"
    ], handoff.reviewer.tools_used

    # The ticket must be connected to the decision that justified it.
    rows = store._read(
        "MATCH (t:Ticket)-[:CITES]->(d:Decision) "
        "MATCH (t)-[:BLOCKS]->(p:Project) "
        "MATCH (a:Agent)-[:FILED]->(t) "
        "RETURN t.title, d.title, p.name, a.name")
    assert rows, "no ticket with a citation was written"
    _, decision_title, project, agent = rows[0]
    assert decision_title == "Delay Spring Launch to May 15", decision_title
    assert project == "Billing v2", project
    assert agent == "Reviewer", agent

    # And the old decision really was retired along the way.
    live = {d["title"] for d in store.current_decisions(project="Spring Launch")}
    assert "Ship Spring Launch on April 1, 2027" not in live, live


@test
def test_completing_the_task_closes_it_in_the_graph():
    store = fresh_store()
    s1 = store.start_session(user="demo-user", session_id="s1")
    build_planner(store, s1, FakeAdapter(PLANNER_SCRIPT)).run("delay the launch")

    s2 = store.start_session(user="demo-user", session_id="s2")
    holder = {}
    reviewer = build_reviewer(store, s2, FakeAdapter([
        calls("claim_task", {}),
        claim_then(lambda tid: (holder.setdefault("tid", tid),
                                calls("complete_task", {
                                    "task_id": tid,
                                    "result": "Billing v2 and Mobile App affected",
                                }))[1]),
        says("Done and recorded."),
    ]))
    reviewer.run(REVIEWER_TRIGGER)

    ctx = store.get_task_context(holder["tid"])
    assert ctx["status"] == "done", ctx
    assert store.open_tasks("Reviewer") == []


@test
def test_dependents_of_is_the_reverse_of_depends_on_chain():
    store = fresh_store()
    downstream = [d["project"] for d in store.dependents_of("Spring Launch")]
    assert downstream == ["Billing v2", "Mobile App"], downstream
    upstream = [d["project"] for d in store.depends_on_chain("Billing v2")]
    assert upstream == ["Spring Launch", "Ledger Migration"], upstream


@test
def test_handoff_summary_reports_the_channel():
    store = fresh_store()
    s1 = store.start_session(user="demo-user", session_id="s1")
    s2 = store.start_session(user="demo-user", session_id="s2")
    handoff = run_handoff(
        store, "delay the launch",
        planner_llm=FakeAdapter(PLANNER_SCRIPT),
        reviewer_llm=FakeAdapter([calls("claim_task", {}), says("got it")]),
        planner_session=s1, reviewer_session=s2,
        leak_needles=["Spring Launch"],
    )
    assert "graph only" in handoff.summary(), handoff.summary()
    assert "claim_task" in handoff.summary()


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
