"""Build the conversational demo workspace, deterministically.

    python scripts/seed_demo.py              # wipe and rebuild `demo`
    python scripts/seed_demo.py --keep       # add to it instead

`scripts/seed.py` builds the company world — Acme and Globex, projects,
decisions, the launch conflict. This builds the other half: a workspace
that looks like somebody has been *talking* to the agent for two days,
with the memory that conversation would have left behind.

Why seed it at all, rather than filming the conversation from scratch: the
interesting parts of this system are what happens on the *second* day --
recalling across a session boundary, handing work to another agent,
consolidating a subject that has come up five times. Reaching them live
takes twenty minutes of typing, and a demo that begins mid-story can show
them in two.

Everything here is written through `MemoryStore`, the same methods the
curator calls, so the graph is identical to one built by talking. No model
is used: this is deterministic and takes about a second, which is what you
want when resetting between takes.

What it deliberately does NOT do is create tasks or run agents. The
handoffs are the demo. Leaving them undone means `Let the Reviewer work`
is a live moment rather than a replay.
"""

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from memory.store import MemoryStore, delete_tenant  # noqa: E402

TENANT = "demo"
USER = "you"

# A clock that advances, so the history looks like two days rather than
# one busy second. Second-precision timestamps tie otherwise, and the
# whole point of the recall moments is that yesterday is behind today.
START = datetime(2026, 10, 17, 9, 0, tzinfo=timezone.utc)


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


def say(store, session, who, text, topics=(), settles=None):
    """One utterance, with the topics the curator would have attached."""
    eid = store.record_event(session, "Planner", "message", text,
                             speaker=who)
    if topics:
        store.link_event_topics(eid, list(topics))
    if settles:
        store.mark_settlement(eid, settles, list(topics) or ["general"])
    return eid


def build(store, clock):
    # ---------------------------------------------------- day one, morning
    s1 = store.start_session(user=USER, session_id="demo-day1-lift")
    store.rename_session(s1, "Lift controller design")
    lift = ["lift controller"]

    say(store, s1, "user",
        "I'm building a lift controller for a six-floor office block. "
        "From now on any code you give me should be in Java, with a "
        "comment on every line explaining what it does.", lift)
    say(store, s1, "Planner",
        "Noted - Java from now on, with a comment on every line. "
        "What part of the controller do you want to start with?", lift)
    store.learn_procedure(
        s1, "Planner", "Java with comments", "writing code",
        "write code in Java, with a comment on every line explaining it")

    say(store, s1, "user",
        "What should hold the pending floor requests?", lift)
    say(store, s1, "Planner",
        "Three reasonable options: a PriorityQueue ordered by floor, an "
        "ArrayDeque for strict arrival order, or a TreeSet if you need to "
        "deduplicate. Which matters more, fairness or travel time?", lift)

    say(store, s1, "user",
        "Fairness matters more - nobody should be skipped. Let's go with "
        "an ArrayDeque in arrival order.", lift,
        settles="Pending floor requests are held in an ArrayDeque in "
                "arrival order, chosen for fairness over travel time")
    say(store, s1, "Planner",
        "Agreed. An ArrayDeque in arrival order it is - first requested, "
        "first served, so nobody is skipped.", lift)

    # ----------------------------------------------------- day one, later
    clock.jump(hours=4)
    say(store, s1, "user",
        "What about the doors? How long should they stay open?", lift)
    say(store, s1, "Planner",
        "A separate timer from the motion controller, so a door hold "
        "never blocks the car from being dispatched.", lift,
        settles="Door timing runs on its own timer, separate from the "
                "motion controller")

    say(store, s1, "user", "Good. And the emergency stop?", lift)
    say(store, s1, "Planner",
        "It has to pre-empt the queue entirely rather than being another "
        "request in it - otherwise it waits its turn behind six floors.",
        lift)

    # --------------------------------------- day two, a different chat
    clock.jump(days=1)
    s2 = store.start_session(user=USER, session_id="demo-day2-review")
    store.rename_session(s2, "Picking the lift work back up")

    say(store, s2, "user",
        "Let's pick up the lift logic from where we stopped.", lift)
    say(store, s2, "Planner",
        "We settled on an ArrayDeque in arrival order for pending floor "
        "requests, and door timing on its own timer. The emergency stop "
        "pre-empting the queue was the open question.", lift)

    say(store, s2, "user",
        "Right. Actually I've changed my mind on the queue - throughput "
        "matters more than I thought. Switch it to a PriorityQueue "
        "ordered by floor.", lift,
        settles="Switched to a PriorityQueue ordered by floor; throughput "
                "now matters more than strict fairness")
    say(store, s2, "Planner",
        "Changed - a PriorityQueue ordered by floor. The earlier "
        "ArrayDeque decision stays readable as what it replaced.", lift)

    # ---------------------------------------- an unrelated second subject
    clock.jump(hours=2)
    s3 = store.start_session(user=USER, session_id="demo-day2-invoice")
    store.rename_session(s3, "Invoice export")
    inv = ["invoice export"]

    say(store, s3, "user",
        "Different thing - I need to export invoices to CSV monthly.", inv)
    say(store, s3, "Planner",
        "Monthly CSV export. Does it need to run unattended, or will you "
        "trigger it?", inv)
    say(store, s3, "user",
        "Unattended, first of the month.", inv,
        settles="Invoice export runs unattended on the first of each "
                "month, producing CSV")

    # A fact with a date, so what_is_on has something to find.
    store.remember_fact(s3, "Planner", USER, "HAS_APPOINTMENT",
                        "dentist", object_type="Thing", when="2026-10-20")

    # Material for the Archivist. Consolidation needs an entity with
    # several unabsorbed episodes about it, and topics alone do not
    # provide that -- `consolidation_candidates` counts ABOUT edges, which
    # `remember_fact` creates at both ends. Six small facts learned across
    # the design conversation is exactly the shape that should collapse
    # into one standing statement.
    for predicate, obj, kind in [
        ("SERVES", "six floors", "Detail"),
        ("QUEUES_WITH", "PriorityQueue ordered by floor", "DataStructure"),
        ("HAS_COMPONENT", "door timer", "Component"),
        ("HAS_COMPONENT", "emergency stop", "Component"),
        ("WRITTEN_IN", "Java", "Language"),
        ("HAS_COMPONENT", "motion controller", "Component"),
    ]:
        store.remember_fact(s1, "Planner", "lift controller firmware", predicate,
                            obj,
                            subject_type="System", object_type=kind)

    return {"sessions": [s1, s2, s3]}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--keep", action="store_true",
                    help="add to the existing workspace instead of wiping it")
    args = ap.parse_args()

    if not args.keep:
        try:
            delete_tenant(TENANT)
        except Exception:
            pass

    clock = Clock()
    store = MemoryStore(TENANT, clock=clock)
    build(store, clock)

    print(f"workspace '{TENANT}' built\n")
    print("  conversations")
    for s in sorted(store.list_sessions(limit=20), key=lambda x: x["id"]):
        if s["turns"]:
            print(f"    {s['title'] or s['preview'][:40]:34} {s['turns']} turns")
    print("\n  subjects")
    for t in store.topic_index():
        print(f"    {t['topic']:22} {t['turns']} turns, last {t['last'][:10]}")
    print("\n  standing rules")
    for p in store.get_applicable_procedures():
        print(f"    {p['name']}: {p['action'][:58]}")
    print("\n  settled, newest first")
    for x in store.settlements(limit=9):
        print(f"    [{x['status']:10}] {x['settled'][:62]}")
    print("\n  agents on the roster")
    for a in store.agent_roster():
        print(f"    {a['agent']:10} {', '.join(a['handles'])}")
    print("\n  ready for consolidation")
    for c in store.consolidation_candidates(min_events=3):
        print(f"    {c['label']} '{c['name']}' - {c['episodes']} episodes")
    print("\nNo tasks created and no agents run: the handoffs are the demo.")


if __name__ == "__main__":
    main()
