"""Demo moment 7: many agents reading and writing the same graph at once.

    python scripts/demo_concurrency.py [workers] [readers] [tasks]
    defaults: 16 workers, 8 readers, 100 tasks

Uses its own graph (`tenant_concurrency`) so it never disturbs the demo data.

What this measures, and what it does not: these are real client threads
against a real FalkorDB on one machine, so the numbers are a correctness
result with timings attached, not a benchmark. Every number printed is
counted during the run. Nothing here is estimated.

The correctness claim is the interesting half. FalkorDB serializes writes per
graph and each write query is atomic, but there are no transactions across
queries -- so the claim has to be ONE query that both tests and sets the
status. The control run at the end does it the wrong way on purpose, and
usually shows several workers all believing they won the same task.
"""

import sys
import threading
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from falkordb import FalkorDB  # noqa: E402

from memory import MemoryStore  # noqa: E402
from memory.store import DEFAULT_HOST, DEFAULT_PORT  # noqa: E402

TENANT = "concurrency"
GRAPH = f"tenant_{TENANT}"


def build(tasks):
    """A graph with one agent, one project, and `tasks` open tasks."""
    db = FalkorDB(host=DEFAULT_HOST, port=DEFAULT_PORT)
    try:
        db.select_graph(GRAPH).delete()
    except Exception:
        pass
    store = MemoryStore(TENANT)
    store._write("CREATE (:Agent {name:'Reviewer'})")
    store._write("CREATE (:Agent {name:'Planner'})")
    store._write("CREATE (:Project {name:'Spring Launch'})")
    session = store.start_session(user="demo", session_id="sess-load")
    store._write(
        "UNWIND range(1,$n) AS i "
        "MATCH (a:Agent {name:'Reviewer'}) "
        "CREATE (t:Task {id:'task-'+toString(i), title:'unit '+toString(i), "
        "                status:'open', created_at:$now}) "
        "CREATE (t)-[:ASSIGNED_TO]->(a)",
        n=tasks, now=store.now(),
    )
    return store, session


def run(workers, readers, tasks):
    store, session = build(tasks)
    claimed, errors = [], []
    per_worker = Counter()
    totals = Counter()                   # real query counts, not estimates
    lock = threading.Lock()
    gate = threading.Barrier(workers + readers)
    stop = threading.Event()

    def worker(i):
        """Claim a task, write an event, complete it. Repeat until drained."""
        own = MemoryStore(TENANT)
        mine = []
        gate.wait()
        try:
            while True:
                task_id = own.claim_task("Reviewer")
                if task_id is None:
                    break
                own.complete_task(session, "Reviewer", task_id,
                                  result=f"handled by worker-{i}")
                mine.append(task_id)
        except Exception as e:
            with lock:
                errors.append(f"worker-{i}: {type(e).__name__}: {e}")
        finally:
            with lock:
                claimed.extend(mine)
                per_worker[f"worker-{i}"] = len(mine)
                totals["writes"] += own.writes
                totals["reads"] += own.reads
            stop.set()

    def reader(i):
        """Hammer read-only queries while the writers work."""
        own = MemoryStore(TENANT)
        gate.wait()
        try:
            while not stop.is_set():
                own.recall_recent(limit=5)
                own.open_tasks("Reviewer")
        except Exception as e:
            with lock:
                errors.append(f"reader-{i}: {type(e).__name__}: {e}")
        finally:
            with lock:
                totals["reads"] += own.reads
                totals["writes"] += own.writes

    threads = ([threading.Thread(target=worker, args=(i,)) for i in range(workers)]
               + [threading.Thread(target=reader, args=(i,)) for i in range(readers)])
    t0 = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.perf_counter() - t0

    duplicates = [t for t, c in Counter(claimed).items() if c > 1]
    left_open = store._read(
        "MATCH (t:Task) WHERE t.status='open' RETURN count(t)")[0][0]
    done = store._read(
        "MATCH (t:Task) WHERE t.status='done' RETURN count(t)")[0][0]
    total_q = totals["reads"] + totals["writes"]

    print(f"\n{'=' * 64}")
    print(f"  {workers} writer threads, {readers} reader threads, {tasks} tasks")
    print(f"  one FalkorDB container, one graph, separate connection per thread")
    print(f"{'=' * 64}")
    print(f"  wall clock            {elapsed * 1000:,.0f} ms")
    print(f"  tasks claimed         {len(claimed)}")
    print(f"  claimed twice         {len(duplicates)}")
    print(f"  left open             {left_open}")
    print(f"  marked done           {done}")
    print(f"  errors                {len(errors)}")
    for e in errors[:5]:
        print(f"      {e}")
    # Counted inside MemoryStore during the run, not estimated.
    print(f"  graph write queries   {totals['writes']:,}")
    print(f"  graph read queries    {totals['reads']:,} "
          f"(served in parallel while the writes were serialised)")
    print(f"  total                 {total_q:,} queries "
          f"= {total_q / elapsed:,.0f}/sec")
    busiest = per_worker.most_common(1)[0] if per_worker else ("none", 0)
    print(f"  work split            {min(per_worker.values(), default=0)}"
          f"-{busiest[1]} tasks per worker across {len(per_worker)} workers")

    ok = (len(claimed) == tasks and not duplicates and left_open == 0
          and done == tasks and not errors)
    print(f"\n  correctness: {'PASS' if ok else 'FAIL'} "
          f"- every task claimed exactly once and completed")
    return ok, store, session


def control(store, session, workers):
    """The anti-pattern, on purpose: read the status, then write it."""
    store._write("MATCH (t:Task {id:'task-1'}) "
                 "SET t.status='open', t.claimed_by=NULL")
    winners = []
    lock = threading.Lock()
    gate = threading.Barrier(workers)

    def naive(i):
        own = MemoryStore(TENANT)
        gate.wait()
        row = own._read("MATCH (t:Task {id:'task-1'}) RETURN t.status")
        if row and row[0][0] == "open":
            time.sleep(0.02)                 # widen the window deliberately
            own._write("MATCH (t:Task {id:'task-1'}) "
                       "SET t.status='claimed', t.claimed_by=$w", w=f"worker-{i}")
            with lock:
                winners.append(i)

    threads = [threading.Thread(target=naive, args=(i,)) for i in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    print(f"\n{'=' * 64}")
    print("  CONTROL: the same claim written as read-then-write (two queries)")
    print(f"{'=' * 64}")
    print(f"  workers that believed they won: {len(winners)} of {workers}")
    print("  -> " + ("lost updates, as expected. This is why the real claim is "
                     "a single compare-and-set query."
                     if len(winners) > 1 else
                     "no race this run; rerun, the window is timing-dependent."))


def main():
    workers = int(sys.argv[1]) if len(sys.argv) > 1 else 16
    readers = int(sys.argv[2]) if len(sys.argv) > 2 else 8
    tasks = int(sys.argv[3]) if len(sys.argv) > 3 else 100

    ok, store, session = run(workers, readers, tasks)
    control(store, session, workers)

    try:
        FalkorDB(host=DEFAULT_HOST, port=DEFAULT_PORT).select_graph(GRAPH).delete()
    except Exception:
        pass
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
