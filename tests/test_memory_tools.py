"""Direct tests for the memory tools. No LLM, no agents, no network beyond FalkorDB.

    python tests/test_memory_tools.py            # needs FalkorDB on localhost:6379

Runs against the throwaway graph `tenant_test`, loaded from data/world_acme.json
so the fixture is the same shape as the demo world. Drops the graph at the end.

Plain asserts and a tiny runner rather than pytest: zero dependencies, and the
output doubles as a readable demonstration that the memory layer works without
a model in the loop.
"""

import importlib.util
import json
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from memory import MemoryStore  # noqa: E402

# Reuse the real seeder so tests exercise it too.
_spec = importlib.util.spec_from_file_location("seed", ROOT / "scripts" / "seed.py")
seed = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(seed)

from falkordb import FalkorDB  # noqa: E402

TENANT = "test"
PASSED, FAILED = [], []


def test(fn):
    """Register and run one test, capturing the failure rather than aborting."""
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


def fresh_store(clock=None):
    """Reload the fixture graph and hand back a store pointed at it."""
    world = json.loads((ROOT / "data" / "world_acme.json").read_text(encoding="utf-8"))
    world["graph"] = f"tenant_{TENANT}"
    db = FalkorDB(host="localhost", port=6379)
    seed.validate(world, ROOT / "data" / "world_acme.json")
    seed.seed_tenant(db, world, ROOT / "data" / "world_acme.json")
    return MemoryStore(TENANT, clock=clock) if clock else MemoryStore(TENANT)


# A fixed clock keeps assertions about timestamps deterministic.
def fixed_clock(seq=iter([f"2026-10-15T12:{m:02d}:00Z" for m in range(60)])):
    return next(seq)


print("seeding fixture graph tenant_test ...")
S = fresh_store()
SESS = S.start_session(user="demo-user", session_id="sess-test-1")
print()


# --------------------------------------------------------------- episodic
@test
def test_record_event_and_recall_recent():
    eid = S.record_event(SESS, "Planner", "message", "User asked about the launch",
                         about=[("Project", "Spring Launch")])
    assert eid.startswith("evt-"), eid
    recent = S.recall_recent(limit=5, session_id=SESS)
    assert len(recent) == 1, f"expected 1 event in this session, got {len(recent)}"
    assert recent[0]["summary"] == "User asked about the launch"
    assert recent[0]["agent"] == "Planner"
    assert recent[0]["session"] == SESS


@test
def test_record_event_rejects_unknown_kind():
    try:
        S.record_event(SESS, "Planner", "gossip", "nope")
    except ValueError:
        return
    raise AssertionError("expected ValueError for an unknown event kind")


@test
def test_recall_recent_spans_sessions():
    # Seeded events live in sess-seed-acme; ours live in sess-test-1.
    across = S.recall_recent(limit=50)
    sessions = {e["session"] for e in across}
    assert len(sessions) >= 2, f"expected events from several sessions, saw {sessions}"


@test
def test_recall_related_returns_events_and_current_facts():
    rel = S.recall_related("Project", "Ledger Migration")
    owners = [f for f in rel["facts_in"] if f["type"] == "OWNS"]
    assert len(owners) == 1, f"expected exactly one current owner, got {owners}"
    assert owners[0]["name"] == "Lee Park", owners[0]
    uses = [f for f in rel["facts_out"] if f["type"] == "USES"]
    assert [u["name"] for u in uses] == ["Ledger Service"], uses


# --------------------------------------------------------------- semantic
@test
def test_record_decision_writes_provenance_and_scope():
    did = S.record_decision(
        SESS, "Planner",
        title="Delay Spring Launch to May 15",
        reason="Ledger Migration is blocked",
        affects=["Spring Launch"],
        made_by="Sam Okafor",
    )
    live = S.current_decisions(project="Spring Launch")
    titles = {d["title"] for d in live}
    assert "Delay Spring Launch to May 15" in titles, titles
    # Provenance must be traversable, not just recorded.
    chain = S.explain_path("Decision", did, "Project", "Spring Launch")
    assert chain and chain["steps"] == ["AFFECTS"], chain


@test
def test_supersede_retires_the_old_decision_and_links_the_chain():
    before = {d["title"] for d in S.current_decisions(project="Spring Launch")}
    assert "Ship Spring Launch on April 1, 2027" in before, before

    old = [d for d in S.current_decisions(project="Spring Launch")
           if d["title"].startswith("Ship Spring Launch")][0]
    new_id = S.record_decision(
        SESS, "Planner",
        title="Delay Spring Launch to June 1",
        reason="Ledger Migration slipped again",
        affects=["Spring Launch"],
        supersedes=old["id"],
    )
    after = {d["title"] for d in S.current_decisions(project="Spring Launch")}
    assert "Ship Spring Launch on April 1, 2027" not in after, \
        f"superseded decision is still current: {after}"
    assert "Delay Spring Launch to June 1" in after, after

    history = S.decision_history(new_id)
    assert len(history) >= 2, history
    assert history[0]["id"] == new_id and history[0]["to"] is None
    assert history[1]["id"] == old["id"] and history[1]["to"] is not None, \
        "the superseded decision should carry a valid_to"


@test
def test_derived_project_state_has_no_status_property():
    rows = S._read("MATCH (p:Project {name:'Spring Launch'}) RETURN p.status")
    assert rows[0][0] is None, "Project.status should not exist; state is derived"
    assert S.current_decisions(project="Spring Launch"), \
        "state must still be answerable from live decisions"


# ------------------------------------------- relationship versioning (moment 5b)
@test
def test_retire_and_assert_fact_moves_ownership():
    assert S.current_facts("OWNS", to_name="Ledger Migration")[0]["from"] == "Lee Park"

    n = S.retire_fact(SESS, "Reviewer", "OWNS", "Lee Park", "Ledger Migration",
                      reason="Dana has been doing the work")
    assert n == 1, f"expected to retire exactly 1 edge, retired {n}"

    S.assert_fact(SESS, "Reviewer", "OWNS", "Dana Ruiz", "Ledger Migration")

    current = S.current_facts("OWNS", to_name="Ledger Migration")
    assert len(current) == 1, f"expected one current owner, got {current}"
    assert current[0]["from"] == "Dana Ruiz", current

    history = S.fact_history("OWNS", to_name="Ledger Migration")
    assert len(history) == 2, f"both versions should survive, got {history}"
    assert [h["current"] for h in history] == [False, True], history
    assert history[0]["from"] == "Lee Park" and history[0]["valid_to"] is not None


@test
def test_retiring_a_missing_fact_reports_zero_rather_than_failing():
    n = S.retire_fact(SESS, "Reviewer", "OWNS", "Marco Silva", "Mobile App",
                      reason="never held it")
    assert n == 0, f"expected 0, got {n}"


@test
def test_assert_fact_rejects_unknown_endpoints():
    try:
        S.assert_fact(SESS, "Reviewer", "OWNS", "Nobody At All", "Mobile App")
    except LookupError:
        return
    raise AssertionError("expected LookupError for an unknown Person")


@test
def test_unknown_fact_type_is_refused():
    try:
        S.current_facts("TOTALLY_MADE_UP")
    except ValueError:
        return
    raise AssertionError("expected ValueError for an unknown relationship type")


# -------------------------------------------------------------- procedural
@test
def test_learn_procedure_and_match_it_directly():
    pid = S.learn_procedure(
        SESS, "Planner",
        name="notify-finance-lead",
        trigger="a finance system is affected",
        action="notify Priya Nair",
        applies_to=[("System", "Ledger Service")],
    )
    direct = S.get_applicable_procedures("System", "Ledger Service")
    assert [p["id"] for p in direct] == [pid], direct


@test
def test_procedure_reaches_projects_through_the_system_hop():
    # The rule is scoped to Ledger Service; Billing v2 USES Ledger Service,
    # so the rule must fire for Billing v2 without being taught again.
    hits = S.get_applicable_procedures("Project", "Billing v2")
    assert any(p["name"] == "notify-finance-lead" for p in hits), hits
    assert any("via system" in p["matched"] for p in hits), hits


@test
def test_procedure_does_not_fire_outside_its_scope():
    hits = S.get_applicable_procedures("Project", "Mobile App")
    assert not any(p["name"] == "notify-finance-lead" for p in hits), hits


@test
def test_retired_procedure_stops_matching():
    pid = S.learn_procedure(SESS, "Planner", name="temporary-rule",
                            trigger="x", action="y",
                            applies_to=[("System", "Auth Service")])
    assert S.get_applicable_procedures("System", "Auth Service"), "rule should match first"
    assert S.retire_procedure(SESS, "Planner", pid, reason="no longer needed")
    assert not S.get_applicable_procedures("System", "Auth Service"), \
        "a retired rule must stop matching"


# ------------------------------------------------------------ coordination
@test
def test_task_handoff_carries_context_through_the_graph():
    did = S.current_decisions(project="Spring Launch")[0]["id"]
    tid = S.create_task(SESS, "Planner", "Reviewer",
                        title="Check downstream impact of the delay",
                        concerns_decision=did, concerns_project="Spring Launch")

    claimed = S.claim_task("Reviewer")
    assert claimed == tid, f"expected to claim {tid}, got {claimed}"

    # The Reviewer was handed no prompt: everything comes from the graph.
    ctx = S.get_task_context(claimed)
    assert ctx["status"] == "claimed"
    assert ctx["created_by"] == "Planner"
    assert ctx["project"] == "Spring Launch"
    assert ctx["decision"]["id"] == did, ctx

    assert S.complete_task(SESS, "Reviewer", tid, result="Billing v2 and Mobile App affected")
    assert S.get_task_context(tid)["status"] == "done"


@test
def test_claim_returns_none_when_nothing_is_open():
    assert S.claim_task("Reviewer") is None


@test
def test_claim_is_atomic_under_concurrent_claimers():
    tid = S.create_task(SESS, "Planner", "Reviewer", title="Contended task")
    winners, n = [], 8
    barrier = threading.Barrier(n)
    lock = threading.Lock()

    def worker():
        store = MemoryStore(TENANT)          # each client gets its own connection
        barrier.wait()
        got = store.claim_task("Reviewer")
        if got:
            with lock:
                winners.append(got)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert winners == [tid], f"expected exactly one winner, got {winners}"


@test
def test_open_tasks_excludes_claimed_and_done():
    a = S.create_task(SESS, "Planner", "Reviewer", title="Task A")
    S.create_task(SESS, "Planner", "Reviewer", title="Task B")
    open_before = {t["id"] for t in S.open_tasks("Reviewer")}
    assert a in open_before and len(open_before) == 2, open_before
    S.claim_task("Reviewer")
    assert len(S.open_tasks("Reviewer")) == 1


# ----------------------------------------------------------------- action
@test
def test_ticket_cites_the_decision_that_justified_it():
    did = S.current_decisions(project="Spring Launch")[0]["id"]
    tid = S.create_ticket(SESS, "Reviewer",
                          title="Billing v2 blocked by launch delay",
                          blocks_project="Billing v2", cites_decision=did)
    rows = S._read(
        "MATCH (t:Ticket {id:$tid})-[:CITES]->(d:Decision) "
        "MATCH (t)-[:BLOCKS]->(p:Project) "
        "MATCH (a:Agent)-[:FILED]->(t) "
        "RETURN d.id, p.name, a.name", tid=tid)
    assert rows and rows[0][0] == did, rows
    assert rows[0][1] == "Billing v2" and rows[0][2] == "Reviewer", rows


# ------------------------------------------------------------ explanation
@test
def test_explain_path_finds_the_hidden_dependency():
    path = S.explain_path("Project", "Billing v2", "Project", "Ledger Migration")
    assert path is not None, "expected a path"
    assert path["chain"] == ["Billing v2", "Spring Launch", "Ledger Migration"], path
    assert path["steps"] == ["DEPENDS_ON", "DEPENDS_ON"], path
    assert path["hops"] == 2, path


@test
def test_explain_path_returns_none_when_unconnected():
    assert S.explain_path("Person", "Marco Silva", "System", "Reporting API",
                          max_hops=1) is None


@test
def test_depends_on_chain_is_multi_hop_and_current_only():
    chain = S.depends_on_chain("Billing v2")
    assert [c["project"] for c in chain] == ["Spring Launch", "Ledger Migration"], chain
    assert [c["hops"] for c in chain] == [1, 2], chain


# -------------------------------------------------------------- isolation
@test
def test_tenant_isolation_is_structural():
    globex = MemoryStore("globex")
    # Written only into tenant_test above.
    assert not globex.current_facts("OWNS", from_name="Dana Ruiz"), \
        "a write to one tenant leaked into another"
    # Same question, different answer, from two separate graphs.
    assert S.depends_on_chain("Billing v2"), "Acme fixture should show a dependency"
    assert globex.depends_on_chain("Billing v2") == [], \
        "Globex Billing v2 is deliberately decoupled"


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
