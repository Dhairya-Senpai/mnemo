"""Demo moment 6: the same question, two tenants, two correct answers.

    python scripts/demo_isolation.py

Run `python scripts/seed.py` first. This script WRITES to the acme graph
(it applies the launch delay, as demo moments 1-5 would have), so reseed
before recording.

The point to make while this runs: there is no tenant column, no tenant
parameter and no WHERE clause doing the separating. Each tenant is its own
FalkorDB graph, so isolation is structural -- it cannot be lost by forgetting
a filter, which is the usual way multi-tenant leaks happen.

On the verdict: a decision in force on something Billing v2 depends on is
reported as a blocker. That is only honest because the setup below records
an actual delay first -- on a freshly seeded graph the only decision on
Spring Launch is its original ship date, which blocks nothing.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from memory import MemoryStore  # noqa: E402

QUESTION = "Can we ship Billing v2?"

DELAY_TITLE = "Delay Spring Launch to May 15, 2027"


def apply_launch_delay(store):
    """Put acme into the state demo moments 1-5 leave behind.

    Without this, the only decision on Spring Launch is its original ship
    date, and calling that a blocker would be nonsense. Idempotent: if the
    delay is already recorded, nothing changes.
    """
    live = store.current_decisions(project="Spring Launch")
    if any(d["title"] == DELAY_TITLE for d in live):
        return None
    earlier = next((d["id"] for d in live
                    if d["title"].startswith("Ship Spring Launch")), None)
    session = store.start_session(user="demo", session_id="sess-isolation-setup")
    return store.record_decision(
        session, "Planner",
        title=DELAY_TITLE,
        reason="Ledger Migration is blocked",
        affects=["Spring Launch"],
        supersedes=earlier,
    )


def answer(store):
    """Work out whether Billing v2 is clear, using only this tenant's graph."""
    blockers = []
    chain = store.depends_on_chain("Billing v2")
    for hop in chain:
        for decision in store.current_decisions(project=hop["project"]):
            blockers.append((hop["project"], hop["hops"], decision))
    own = store.current_decisions(project="Billing v2")
    return chain, blockers, own


def show(tenant):
    store = MemoryStore(tenant)
    chain, blockers, own = answer(store)

    print(f"\n{'=' * 64}")
    print(f"  tenant: {tenant}   (graph: {store.graph_name})")
    print(f"{'=' * 64}")
    print(f"  Q: {QUESTION}")

    if chain:
        path = " -> ".join(["Billing v2"] + [h["project"] for h in chain])
        print(f"\n  Billing v2 sits behind: {path}")
    else:
        print("\n  Billing v2 depends on nothing.")

    for project, hops, decision in blockers:
        where = f"{project}, {hops} hop(s) upstream"
        print(f"\n  Decision in force on {where}:")
        print(f"    {decision['title']}")
        print(f"    reason: {decision['reason']}")

    for decision in own:
        print(f"\n  Decision on Billing v2 itself:")
        print(f"    {decision['title']}")
        print(f"    reason: {decision['reason']}")

    if blockers:
        verdict = "NO - blocked upstream"
    elif own:
        verdict = "YES - explicitly cleared"
    else:
        verdict = "YES - nothing blocks it"
    print(f"\n  A: {verdict}")
    return verdict


def main():
    print("Same question. Same code. Two graphs.")

    # Put acme where the earlier demo moments would have left it.
    recorded = apply_launch_delay(MemoryStore("acme"))
    print(f"  setup: acme launch delay {'recorded' if recorded else 'already present'}")

    verdicts = {t: show(t) for t in ("acme", "globex")}

    print(f"\n{'=' * 64}")
    print("  acme   ->", verdicts["acme"])
    print("  globex ->", verdicts["globex"])
    if verdicts["acme"] == verdicts["globex"]:
        print("\n  Both tenants answered the same way -- the contrast is gone.")
        print("  Check that seeding ran and that the worlds still differ.")
        return 1

    print("\n  Different answers, and neither query named a tenant:")
    print("  the separation is the graph, not a filter.")

    # Prove the obvious failure mode is impossible: a write in one tenant is
    # simply not visible in the other, with nothing in the query to get wrong.
    acme, globex = MemoryStore("acme"), MemoryStore("globex")
    sess = acme.start_session(user="demo", session_id="sess-isolation-probe")
    acme.record_event(sess, "Planner", "message",
                      "ISOLATION PROBE: written only into acme",
                      about=[("Project", "Billing v2")])
    leaked = [e for e in globex.recall_recent(limit=50)
              if "ISOLATION PROBE" in e["summary"]]
    print(f"\n  wrote a probe event into acme; globex sees {len(leaked)} of them.")
    return 0 if not leaked else 1


if __name__ == "__main__":
    raise SystemExit(main())
