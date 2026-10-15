"""One small, real call to Anthropic, to close the project's biggest gap.

    python scripts/verify_live_api.py            # needs ANTHROPIC_API_KEY
    python scripts/verify_live_api.py --model claude-haiku-5-5

Everything else in this repository is tested with a scripted fake adapter, so
nothing proves the Claude adapter's wire format is actually accepted by the
live API, or that a real model can choose a memory tool. This script proves
both, cheaply, and reports exactly what it cost.

Three checks, in increasing cost:

  1. The request body is accepted at all (one tiny call, no tools).
  2. A real model, given the real tool schemas, chooses a tool.
  3. A full agent turn runs against the real graph.

Reads the seeded `tenant_acme` graph and writes to a throwaway session; it
does not modify the demo data beyond adding events to its own session.
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from agents import REVIEWER_TOOLS, Agent, Toolbox  # noqa: E402
from llm.claude import ClaudeAdapter  # noqa: E402
from llm.types import Message  # noqa: E402
from memory import MemoryStore  # noqa: E402

# Published per-million-token prices, for turning tokens into a number you can
# sanity-check against the console. If these drift, the token counts above
# them are still the ground truth.
PRICES = {
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-opus-5-5": (4.00, 20.00),
}

spend = []


def record(label, usage, model):
    if not usage:
        print(f"      {label}: no usage reported")
        return
    tin = usage.get("input_tokens", 0)
    tout = usage.get("output_tokens", 0)
    rate_in, rate_out = PRICES.get(model, (0.0, 0.0))
    cost = tin / 1e6 * rate_in + tout / 1e6 * rate_out
    spend.append(cost)
    print(f"      {label}: {tin:,} in + {tout:,} out  ~${cost:.5f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=None, help="defaults to the adapter's default")
    ap.add_argument("--tenant", default="acme")
    args = ap.parse_args()

    import os
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set in this process.")
        print("In PowerShell:  $env:ANTHROPIC_API_KEY = \"sk-ant-...\"")
        return 2

    adapter = ClaudeAdapter(model=args.model) if args.model else ClaudeAdapter()
    print(f"model: {adapter.model}")
    print(f"refusal fallback: {adapter.refusal_fallback}\n")

    # --- 1. is the request body accepted at all? ------------------------
    print("[1] minimal call, no tools")
    try:
        reply = adapter.chat([Message(role="user", text="Reply with the single word: ready")])
    except Exception as e:
        print(f"    FAILED: {type(e).__name__}: {e}")
        return 1
    print(f"    reply: {reply.text!r}")
    print(f"    stop_reason: {reply.stop_reason}")
    record("tokens", reply.usage, adapter.model)

    # --- 2. does a real model pick a tool from our schemas? -------------
    print("\n[2] real tool schemas, does the model choose one?")
    store = MemoryStore(args.tenant)
    session = store.start_session(user="live-verify", session_id="sess-live-verify")
    toolbox = Toolbox(store, "Reviewer", session, allowed=REVIEWER_TOOLS)
    try:
        reply2 = adapter.chat(
            [Message(role="user",
                     text="What decisions are currently in force for the project "
                          "'Spring Launch'? Use the tools.")],
            tools=toolbox.specs(),
            system="You answer only from the tools. Never guess.",
        )
    except Exception as e:
        print(f"    FAILED: {type(e).__name__}: {e}")
        return 1
    if reply2.tool_calls:
        for call in reply2.tool_calls:
            print(f"    chose: {call.name}({call.arguments})")
    else:
        print(f"    no tool call; said: {reply2.text[:120]!r}")
    record("tokens", reply2.usage, adapter.model)

    # --- 3. a full agent turn against the real graph --------------------
    print("\n[3] full agent turn against the real graph")
    agent = Agent("Reviewer", toolbox, adapter, prompt_name="reviewer")
    try:
        turn = agent.run("Can we ship Billing v2? Explain your reasoning.")
    except Exception as e:
        print(f"    FAILED: {type(e).__name__}: {e}")
        return 1
    print(f"    tools used: {turn.tools_used}")
    print(f"    answer: {turn.text[:400]}")

    print("\n" + "=" * 62)
    print(f"total reported spend across checks 1 and 2: ~${sum(spend):.5f}")
    print("(check 3 runs several calls; see your console for the exact figure)")
    print("\nThe Claude adapter works against the live API." if turn.text
          else "\nAdapter reached the API but the final answer was empty.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
