"""Run the demo moments from the terminal.

    python scripts/run_demo.py              # reset, then run every moment
    python scripts/run_demo.py m1 m2 m3     # run specific moments
    python scripts/run_demo.py --no-reset   # continue from current graph state

Same moments the Streamlit app runs, without the UI. Useful for checking the
demo still works after a change, and as a fallback if the UI misbehaves while
recording.

Models are scripted (zero tokens). The graph, the Cypher and the memory
changes are real.
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from demo import BY_KEY, MOMENTS, reset_world  # noqa: E402
from memory import MemoryStore, diff_snapshots  # noqa: E402

RULE = "=" * 72


def show(moment, store):
    print(f"\n{RULE}\n  MOMENT {moment.number}: {moment.title}")
    print(f"  shows: {moment.shows}   (track {moment.track})\n{RULE}")

    before = store.snapshot()
    try:
        result = moment.run(store)
    except Exception as e:
        print(f"  FAILED: {type(e).__name__}: {e}")
        return False
    changes = diff_snapshots(before, store.snapshot())

    if result.note:
        print(f"\n  [{result.note}]")
    print(f"\n  {result.narration}\n")

    if result.steps:
        print("  tools the agent called:")
        for s in result.steps:
            flag = " (error)" if s["error"] else ""
            print(f"    - {s['tool']}({json.dumps(s['args'])}){flag}")
            print(f"        -> {s['result'][:160]}")

    if result.path:
        print(f"\n  path: {' -> '.join(result.path)}")

    for key, value in result.facts.items():
        rendered = json.dumps(value, default=str) if not isinstance(value, str) else value
        print(f"\n  {key}: {rendered[:400]}")

    print("\n  memory changes:")
    if changes.empty:
        print("    (nothing changed)")
    for line in changes.lines():
        print(f"    {line}")

    # A moment whose agent could not call a tool has not run, whatever it
    # printed. Removing a tool from the Planner left moment 4 narrating "no
    # standing rules apply" while the runner reported all ten moments fine,
    # because nothing here looked at whether the calls succeeded. Same shape
    # as every silent no-op in docs/TESTING.md: the failure was visible in
    # the output and nothing was checking.
    broken = [s["tool"] for s in result.steps if s.get("error")]
    if broken:
        print(f"\n  FAILED: tool call(s) errored: {', '.join(broken)}")
        return False
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("keys", nargs="*", help="moment keys, e.g. m1 m3")
    ap.add_argument("--tenant", default="acme")
    ap.add_argument("--no-reset", action="store_true")
    args = ap.parse_args()

    if not args.no_reset:
        print("resetting both tenant graphs ...")
        reset_world()

    store = MemoryStore(args.tenant)
    chosen = [BY_KEY[k] for k in args.keys] if args.keys else MOMENTS
    failures = [m.number for m in chosen if not show(m, store)]

    print(f"\n{RULE}")
    if failures:
        print(f"  FAILED moments: {', '.join(failures)}")
        return 1
    print(f"  all {len(chosen)} moments ran")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
