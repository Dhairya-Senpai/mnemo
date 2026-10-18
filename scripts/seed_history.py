"""Build Acme's working history, deterministically.

    python scripts/seed_history.py           # add the history to `acme`
    python scripts/seed_history.py --tenant globex

Run after `scripts/seed.py`. That builds the world — people, projects,
systems, the April ship date. This builds what happened *to* it: the launch
slipping twice, a task handed between agents, a release blocked, a standing
rule taught, ownership moving, and six episodes folded into one insight.

Without this the Acme workspace is a static org chart. With it, every claim
the project makes is visible as data: two SUPERSEDES chains, a retired OWNS
edge next to its replacement, a Ticket that CITES the decision behind it, a
Procedure scoped to a system, and an Insight with DERIVED_FROM edges back to
every episode it absorbed.

**This used to be `scripts/run_demo.py`**, which replayed ten scripted
moments through the agent loop with a fake adapter. The data was a
by-product of the replay. That machinery was removed on 18 October — the
repository now ships only what the app needs to run — so the writes were
rewritten as direct `MemoryStore` calls. Same graph, no scripted model, and
about a second instead of thirty.

Everything goes through `MemoryStore`, the same methods the agents call, so
the result is indistinguishable from a graph built by talking to them.
"""

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from memory.store import MemoryStore  # noqa: E402

TENANT = "acme"
USER = "demo-user"

# Two days of working history. Second-precision timestamps tie when written
# in one burst, and several reads here depend on "most recent" being
# well-defined -- current_decisions, the supersede chain, recall ordering.
START = datetime(2026, 10, 16, 9, 0, tzinfo=timezone.utc)


class Clock:
    """Monotonic fake clock. `step` is seconds between writes."""

    def __init__(self, start=START, step=45):
        self.now = start
        self.step = step

    def __call__(self):
        self.now += timedelta(seconds=self.step)
        return self.now.strftime("%Y-%m-%dT%H:%M:%SZ")

    def jump(self, **kw):
        self.now += timedelta(**kw)


# --------------------------------------------------------------------- beats
def day_one_delay(store):
    """The launch slips, and the Planner leaves the Reviewer a task.

    The task carries CONCERNS edges rather than a message. That is the whole
    coordination claim: the Reviewer is told nothing, and reads what the work
    is about from the graph.
    """
    s = store.start_session(user=USER, session_id="sess-day1")
    store.record_event(
        s, "Planner", "message",
        "We need to delay the Spring Launch to May 15, 2027 - Ledger Migration "
        "is blocked. Someone should check what that breaks.",
        about=[("Project", "Spring Launch")], speaker="user")

    live = store.current_decisions(project="Spring Launch")
    delay_id = store.record_decision(
        s, "Planner",
        title="Delay Spring Launch to May 15, 2027",
        reason="Ledger Migration is blocked",
        affects=["Spring Launch"],
        made_by="Sam Okafor",
        supersedes=live[0]["id"] if live else None)

    store.create_task(
        s, "Planner", assigned_to="Reviewer",
        title="Work out what the launch delay breaks downstream",
        concerns_decision=delay_id,
        concerns_project="Spring Launch")

    store.record_event(
        s, "Planner", "message",
        "I've recorded the delay with its reason, retired the April date it "
        "replaces, and left the Reviewer a task to work out the fallout.")
    return delay_id


def reviewer_claims(store):
    """The Reviewer picks the task up. Claim, then close it with its finding."""
    s = store.start_session(user=USER, session_id="sess-day1-reviewer")
    task_id = store.claim_task("Reviewer")
    if task_id is None:
        return None
    store.complete_task(
        s, "Reviewer", task_id,
        "Billing v2 and Mobile App both sit behind Spring Launch; Billing v2 "
        "is the one with a release this sprint.")
    return task_id


def day_two_block_the_release(store, clock, decision_id):
    """The next day, a different session files the ticket. Track 01's action.

    CITES is what stops the ticket being an orphan assertion -- it makes
    ticket -> decision -> event -> session -> agent traversable, which is
    where the explainability claim actually lives.
    """
    clock.jump(days=1)
    s = store.start_session(user=USER, session_id="sess-day2")
    store.record_event(s, "Reviewer", "message", "Can we ship Billing v2 this sprint?",
                       about=[("Project", "Billing v2")], speaker="user")
    store.create_ticket(
        s, "Reviewer",
        title="Billing v2 blocked by the Spring Launch delay",
        blocks_project="Billing v2",
        cites_decision=decision_id)
    store.record_event(
        s, "Reviewer", "message",
        "No. Billing v2 sits behind Spring Launch, which is delayed to May 15, "
        "2027 because Ledger Migration is blocked. I've filed a ticket citing "
        "that decision.")


def teach_a_rule(store):
    """A standing rule, scoped to a system.

    Taught about Ledger Service, and it later fires for Billing v2 -- which
    the rule never mentions -- because Billing v2 USES that system. The match
    is a traversal, not a string comparison, which is the point.
    """
    s = store.start_session(user=USER, session_id="sess-rule-taught")
    eid = store.record_event(
        s, "Planner", "message",
        "Whenever a finance system is affected, notify Priya.",
        about=[("System", "Ledger Service")], speaker="user")
    store.link_event_topics(eid, ["finance notifications"])
    reply = store.record_event(
        s, "Planner", "message",
        "Noted. I'll apply that whenever a finance system is involved.",
        about=[("System", "Ledger Service")])
    store.link_event_topics(reply, ["finance notifications"])
    store.learn_procedure(
        s, "Planner",
        name="notify-finance-lead",
        trigger="a finance system is affected",
        action="notify Priya Nair",
        applies_to=[("System", "Ledger Service")])

    # A second session, where the rule fires. Kept separate on purpose: the
    # rule was taught about Ledger Service and fires here for Billing v2,
    # which it never mentions, and the two conversations being distinct is
    # what shows the rule survived the session boundary.
    s2 = store.start_session(user=USER, session_id="sess-rule-fires")
    store.record_event(s2, "Reviewer", "message",
                       "I'm about to make a change to Billing v2.",
                       about=[("Project", "Billing v2")], speaker="user")
    applicable = store.get_applicable_procedures("Project", "Billing v2")
    store.record_event(
        s2, "Reviewer", "message",
        f"Before I touch Billing v2: a standing rule applies - "
        f"{applicable[0]['action']} ({applicable[0]['matched']})."
        if applicable else "No standing rules apply here.",
        about=[("Project", "Billing v2")])


def supersede_again(store):
    """A second slip. The chain has to survive being extended."""
    s = store.start_session(user=USER, session_id="sess-supersede")
    store.record_event(
        s, "Planner", "message",
        "Ledger Migration slipped again. Push the launch to June 1, 2027.",
        about=[("Project", "Spring Launch")], speaker="user")
    live = store.current_decisions(project="Spring Launch")
    store.record_decision(
        s, "Planner",
        title="Delay Spring Launch to June 1, 2027",
        reason="Ledger Migration slipped again",
        affects=["Spring Launch"],
        supersedes=live[0]["id"] if live else None)
    store.record_event(
        s, "Planner", "message",
        "June 1, 2027 is now the current date. The May 15, 2027 decision is "
        "retired but still readable, and the chain back through it is intact.")


def transfer_ownership(store):
    """The only beat that retires a *relationship* rather than a node.

    Most of the schema's versioning machinery exists for this case, and it is
    the one a demo usually skips.
    """
    s = store.start_session(user=USER, session_id="sess-ownership")
    store.record_event(
        s, "Reviewer", "message",
        "Dana has effectively been running Ledger Migration. Make it official.",
        about=[("Person", "Dana Ruiz"), ("Project", "Ledger Migration")],
        speaker="user")
    store.retire_fact(s, "Reviewer", "OWNS", "Lee Park", "Ledger Migration",
                      reason="Dana has been running it for months")
    store.assert_fact(s, "Reviewer", "OWNS", "Dana Ruiz", "Ledger Migration")
    store.record_event(
        s, "Reviewer", "message",
        "Ownership of Ledger Migration has moved from Lee Park to Dana Ruiz. "
        "Dana had been the one working on it all along.")


def consolidate_ledger(store):
    """Fold the repeated Ledger Migration episodes into one lasting fact.

    The graph picks the work: `events_about(..., only_unconsolidated=True)`
    returns the episodes with no DERIVED_FROM edge. In the running system a
    model writes the sentence; here it is fixed, because a seeder that needs
    a 5.6 GB download to produce demo data is not a seeder.

    The DERIVED_FROM edges are the part that matters. They are what makes the
    insight checkable, and what stops those episodes being absorbed twice.
    """
    s = store.start_session(user=USER, session_id="sess-consolidate")
    for week in (1, 2, 3):
        store.record_event(
            s, "Reviewer", "message",
            f"Standup note: Ledger Migration slipped again, week {week}",
            about=[("Project", "Ledger Migration")])

    episodes = store.events_about("Project", "Ledger Migration",
                                  only_unconsolidated=True)
    if len(episodes) < 3:
        return 0
    store.record_insight(
        s, "Reviewer", "Project", "Ledger Migration",
        text="Ledger Migration has slipped repeatedly (3 separate reports); "
             "changed owner over the same period; sits upstream of the Spring "
             "Launch train. It is the recurring cause of Spring Launch delays.",
        source_event_ids=[e["id"] for e in episodes])
    return len(episodes)


def build(store, clock):
    decision_id = day_one_delay(store)
    reviewer_claims(store)
    day_two_block_the_release(store, clock, decision_id)
    teach_a_rule(store)
    supersede_again(store)
    transfer_ownership(store)
    return consolidate_ledger(store)


# ---------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tenant", default=TENANT,
                    help=f"workspace to add the history to (default {TENANT})")
    args = ap.parse_args()

    clock = Clock()
    store = MemoryStore(args.tenant, clock=clock)
    absorbed = build(store, clock)

    snap = store.snapshot()
    print(f"history written into '{args.tenant}': "
          f"{len(snap['nodes'])} nodes, {len(snap['edges'])} edges\n")

    print("  Spring Launch, back down the SUPERSEDES chain")
    live = store.current_decisions(project="Spring Launch")
    for d in (store.decision_history(live[0]["id"]) if live else []):
        state = "current" if d["to"] is None else "retired"
        print(f"    [{state:7}] {d['title']}")

    print("\n  standing rules")
    for p in store.get_applicable_procedures():
        print(f"    {p['name']}: {p['action']}")

    print("\n  ownership of Ledger Migration, over time")
    for f in store.fact_history("OWNS", to_name="Ledger Migration"):
        until = f.get("valid_to") or "now"
        print(f"    {f['from']:12} {f['valid_from'][:10]} -> {until[:10]}")

    print(f"\n  consolidation: {absorbed} episodes absorbed into one insight")
    print("\nAcme now has a history: two slips, a blocked release, a rule, "
          "an ownership change.")


if __name__ == "__main__":
    main()
