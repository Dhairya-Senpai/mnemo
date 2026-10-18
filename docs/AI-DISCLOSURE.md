# AI assistance disclosure

Required by the Graph Hacks rules, which permit AI coding assistants provided
their use is disclosed and the participant understands and can explain the
submission.

**Short version:** this is a solo entry. Claude (chat) was used for planning
and design before the event; Claude Code (Opus 5) wrote essentially all of
the code during the event, working one build step at a time. Every design
decision was made by the participant at a checkpoint between steps, and the
build stopped for a decision whenever one was open. Nothing in the repository
is unreviewed output: each step ended with a written explanation of what was
built and why, and several steps changed direction because the participant
rejected the first proposal.

---

## Tools used

| Tool | Used for |
|---|---|
| Claude (chat) | Planning before the event: track choice, the memory-model sketch, the demo script, the build order. The result is `PROJECT_CONTEXT.md`. |
| Claude Code (Opus 5) | Implementation during the event: all Python, the Cypher, the tests, the seed data, and the documentation in `docs/`. |

No other AI tools were used. No code was copied from another project.

## What Claude Code did

- Wrote every Python module in `src/`, `scripts/` and `tests/`.
- Wrote the synthetic world files in `data/`, the prompts in `prompts/`, and
  the documents in `docs/`.
- Verified FalkorDB behaviour by running probe scripts against a local
  instance rather than relying on recalled or documented behaviour, and
  recorded the results in `SCHEMA.md` and `QUERIES.md`.
- Reported its own mistakes. Several are recorded in the repository history
  and in the documents, including: ordering that was silently wrong because
  random ids were used as a tiebreaker; a demo script that overstated its
  conclusion by treating a ship-date decision as a blocker; a `depends_on`
  helper that only walked one direction; and a proposed demo beat that did
  not typecheck against the schema it had just written.

## What the participant did

The participant did not write the code. Their contribution was direction,
decisions and rejection — which is where this project's shape actually comes
from. The decisions below were each made at a checkpoint, in response to a
proposal, and each one changed what was built next:

| Decision | Effect |
|---|---|
| Keep the fictional company domain rather than a personal-assistant domain | Set the whole world model |
| Generalise validity times to **every** changeable fact, not just decisions | The core of the schema; most of the query complexity follows from this |
| Keep `SUPERSEDES` for `Decision` nodes only | Avoided redundant edges on every versioned relationship |
| **Drop `Project.status` entirely** and derive project state from live decisions — rejecting the proposed reified status node | Arguably the best idea in the schema: state can never drift from its justification |
| Keep `Task`/`Ticket` status unversioned | Kept the coordination layer simple |
| Add the ownership-transfer beat to the demo | Gave relationship versioning the only moment that exercises it |
| Build consolidation **and** give it a demo moment | Stopped `Insight` becoming machinery nobody sees |
| Fold build step 7 into step 8 | Pacing call against the clock |
| Require the schema to be locked only after the behaviour was probed, not before | Caught three FalkorDB behaviours that would otherwise have been assumed |
| Require a throwaway script to test the query pitfalls against a real server | Produced the verification table in `SCHEMA.md` |

The participant also set the working rules the build followed: explain before
coding, stop at checkpoints, never claim code works without running it, verify
FalkorDB syntax against the docs rather than assuming Neo4j semantics, and keep
the graph load-bearing.

## Understanding the code

The rules require the participant to be able to explain the graph model, the
queries, the architecture and the technical decisions. The two documents
written for that purpose are:

- [`SCHEMA.md`](SCHEMA.md) — every node and relationship, why each exists, and
  the measured FalkorDB behaviour the design rests on.
- [`QUERIES.md`](QUERIES.md) — every query the product depends on, with the
  reason it is written that way, including the several cases where the obvious
  phrasing fails silently.

> **Participant note — complete before submitting.** Add a sentence here in
> your own words about which parts you worked through in most detail and
> anything you would change. Leaving this section as written by Claude would
> undercut the point of the disclosure.

## What is verified, and what is not

Being specific about this matters more than looking finished.

**Verified by running it:**

- All 203 tests pass against FalkorDB graph module v6.0.2.
- Every FalkorDB behaviour claimed in `SCHEMA.md` and `QUERIES.md` was probed
  directly; the probes are reproducible.
- The concurrency numbers were measured during an actual run, with query
  counters inside `MemoryStore`. No number in this repository is estimated.
- The MCP server was driven over real stdio by the MCP SDK's own client.
- **A live model has driven the agent loop.** `ornith:9b` through Ollama chose
  its own tools, traversed the dependency chain and answered from the graph,
  then recovered its own reasoning in a separate session via `explain_answer`
  (16 October 2026).

**Not verified:**

- **No request has ever been sent to the Anthropic API.** There is no API key
  on the development machine. The Claude adapter's translation layer is
  unit-tested against a stub client, but its live wire format is unconfirmed.
  The Ollama adapter is the one proven against a real model; the Claude one is
  not.
- **Every automated test uses a scripted fake adapter.** The live-model runs
  above were done by hand, not in the test suite, because a test that needs a
  model running locally is not a test that passes anywhere.
- Consolidation **quality** is unknown. The mechanism is tested; whether a
  real model writes a useful standing fact has not been checked.
- Asked to explain itself, the live model **paraphrased the stored answer**
  rather than naming the queries behind it, as its role prompt asks. The trace
  it read is exact; a 9B model's use of it is imperfect.
- The MCP server has not been tested against a third-party MCP host.
- The concurrency figures are from one machine and one container. They are a
  correctness result with timings attached, not a benchmark.

## Reproducing the checks

```bash
docker run -d --name falkordb -p 6379:6379 -p 3000:3000 falkordb/falkordb:latest
pip install falkordb anthropic mcp
python scripts/seed.py
python tests/test_memory_tools.py
python tests/test_llm_and_agent.py
python tests/test_handoff.py
python tests/test_step6.py
python tests/test_mcp_and_algorithm.py
python tests/test_open_facts.py
python tests/test_trace.py
python tests/test_unseeded.py
python scripts/demo_isolation.py
python scripts/demo_concurrency.py
```

Last updated: 16 October 2026.
