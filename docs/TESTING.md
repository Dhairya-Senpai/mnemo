# What was tested, and what it broke

The automated suite is in `tests/`. This file is the other half: the
scenarios run by hand against a live model, the bugs they found, and the
ones still open. It exists because almost every real defect in this project
was found by talking to the agent rather than by running the tests — and
the reason why is itself the most useful finding here.

Everything below was run against FalkorDB graph module v6.0.2 with
`ornith:9b` through Ollama, between 15 and 18 October 2026.

---

## 1. The blind spot in the test suite

158 tests passed while five real bugs sat in the code. They passed because
**every fixture seeded `world_acme.json` before its first assertion**, so
every test began from a graph that was already correctly populated. Nothing
ever exercised:

- a graph with no `Agent` nodes
- a graph with no Redis key at all
- a fact asserted twice before being retired
- an agent reading its own output back

`tests/test_unseeded.py` exists to close that gap. It starts from an empty
graph and uses the personal-assistant shape rather than the company one.

The lesson generalises: a fixture that sets the world up correctly cannot
test what happens when the world is not set up.

---

## 2. Bugs found by conversation

### 2.1 The episodic tier silently did nothing

**Found by:** opening a brand-new workspace and talking to it.

`record_event` matched on `(a:Agent {name: $agent})`. With no seeded Agent
the match yielded no rows, so the `CREATE` never ran — and the method still
returned an event id. Six sessions of conversation left **zero Event
nodes**, the agent answered "I don't know anything about you", and nothing
errored. Facts were still written, because writing a fact does not depend on
the Agent; only the history vanished.

**Fix:** `MERGE` the agent, and raise when the Session is missing. Returning
an id for an event that does not exist is how a caller ends up with
`source_event` pointing at nothing.

### 2.2 Rescheduling an appointment destroyed it

**Found by:** "actually that doctor appointment moved to the 22nd".

The model recorded the new date and then retired the old one. But
`retire_open_fact` closed **every** open edge of that predicate between the
two nodes, so it closed the 22nd as well as the 20th. The appointment was
gone entirely.

**Fix:** an optional `when` so a single instance can be named, plus prompt
guidance to retire before asserting. Closing every match stays the default,
because a fact that holds only once should still retire in one call.

### 2.3 The reasoning trace leaked into recall

**Found by:** reading `recall_entity` output during an unrelated check.

`recall_entity` filtered only on `valid_to`, so the `RETURNED` edges of the
reasoning trace came back as facts the agent believed about you.

**Fix:** also require `valid_from`, which is what `recall_related` had always
done. Only versioned edges carry it, so structural ones drop out.

### 2.4 The trace lied about the one tool the demo turns on

**Found by:** reading the "Why it answered that" panel in the browser.

The panel said `what_is_on` **returned nothing**, directly above an answer
plainly drawn from what it returned. The citation walker harvested
`name`/`id`/`project`/`from`/`to`, and `what_is_on` names its endpoints
`subject` and `object` — so it cited nothing at all.

A trace that lies is worse than no trace.

**Fix:** harvest those keys too, and skip ISO timestamps, because `from` and
`to` mean entity names in `current_facts` and validity instants in
`decision_history`.

### 2.5 Reads crashed on a workspace that had never been written to

**Found by:** deleting the personal workspace for real.

FalkorDB creates a graph's Redis key on the first write. Until then
`GRAPH.RO_QUERY` **errors** rather than returning no rows — and the UI reads
before it writes, listing conversations before anything has been said. A
genuinely empty workspace crashed on open. It had only ever worked because
earlier testing had already created the graph.

**Fix:** `_read` treats "Invalid graph operation on empty key" as empty,
which is what it honestly is. Only that one message is swallowed.

---

## 3. The one that took three attempts: retired facts that would not stay quiet

This is the most instructive sequence in the project, because each fix was
correct and still insufficient.

**The scenario.** Tell it about a doctor's appointment on the 20th. Remove
that conversation. Then ask about booking something else on the 20th.

### Attempt 1 — hide the conversation

Removing a chat marked the session hidden. The chat left the sidebar and the
appointment stayed current, so the agent kept refusing to book a flight on
account of something the person believed they had just deleted.

**Changed:** removing a conversation now also retires every fact whose
`source_event` belongs to it.

### Attempt 2 — label the stale episode

Retiring the fact silenced the fact queries. But the episodic tier is
immutable by design — an event never stops having happened — so the episode
`"Learned: you HAS_APPOINTMENT doctor (on 2026-10-20)"` still asserted it,
and a model reading episodes reported a clash fifteen minutes after the
retirement.

Episodes were labelled `[superseded - no longer true]` rather than hidden,
on the reasoning that history should not be rewritten.

**That made it worse.** The model read the label as something interesting to
report and narrated the cancellation instead of ignoring it.

**Changed:** the recall tools leave superseded episodes out and return a
`superseded_episodes` count. Recall answers "what is true now"; the history
tools answer "what happened". Nothing leaves the graph.

### Attempt 3 — the agent's own prose

**Found by:** a cricket scenario. Two chats established a preference; a third
asked whether to enter a tournament. The agent correctly recalled the
preference — and also mentioned a dentist appointment that had been retired
five minutes earlier.

The trace showed why:

```
what_is_on("2026-10-20")  ->  nothing             (correct)
recall_recent(10)         ->  10 message events   (its own earlier reply)
```

It read back a sentence *it had written* in another chat — "you already have
a dentist appointment on 2026-10-20" — and repeated it. **A reply asserts
things in sentences rather than in edges, so nothing can retire it.** The
sentence outlived the fact it was about.

**Changed:** `recall_recent` no longer returns an agent's own replies. A
reply is output, not evidence; the substance of a turn is the facts it wrote
and what the person actually said. Transcripts are untouched, so the UI
still shows both sides.

### What this sequence shows

Retirement is a property of edges. Anything that states a fact in prose —
an episode summary, an agent's reply, the person's own words — sits outside
that mechanism and has to be handled separately. Three different surfaces
had to be closed one at a time, each found only by use.

---

## 3b. The standing rule that was stored and then orphaned

**Found by:** "i like to code in java and prefer any coding in that
language", then in a new chat, "write a code to print hello world". The
answer came back in Python, with a tour of four other languages.

Two independent failures, stacked.

**The rule was unreachable.** `learn_procedure` stored a correct `Procedure`
-- trigger "coding, programming, implementation", action "use Java" -- with
**zero `APPLIES_TO` edges**, because `PROCEDURE_SCOPES` permits only
`Project` and `System` and a rule about a topic has neither.
`get_applicable_procedures` matches only through `APPLIES_TO`, so all four
plausible lookups returned nothing. Meanwhile `learn_procedure` returned a
perfectly good id, so the agent truthfully reported that it had saved a
standing rule it could never find again. The whole procedural mechanism was
built around the company world, where every rule attaches to a project.

**And it never looked.** The trace for the hello-world answer was empty: no
tools at all. The model read it as a generic coding question and answered
from training.

**Changed:** a rule with no scope is one that holds everywhere, not a broken
one, so `get_applicable_procedures` returns unscoped rules alongside scoped
ones and can be called with no entity at all.

That made the rule reachable and changed nothing, because the model still
did not look. Prompting it to look did not work either -- the third time
prompt guidance failed with this model. So the Planner now reads its live
rules from the graph into every system prompt. Retiring a rule removes it
from the next turn, which is what keeps this memory rather than a hardcoded
instruction:

```
rule live     -> JAVA
rule retired  -> not java
```

**The Reviewer deliberately does not get this.** `find_prompt_leaks` exists
to prove the Reviewer was told nothing, and an injected rule would be
something it was told -- demo moment 4 turns on a rule firing *without*
being restated. The existing test caught this immediately when injection was
first applied to both agents. The Planner answers free-form input from a
person and carries its rules; the Reviewer gets one fixed trigger and reads
everything, rules included.

## 4. Still open

- **A person's own words are still recalled after the fact is retired.**
  "i have dr appointment on 20 oct" remains in `recall_recent` after the
  appointment is gone, because what someone said is genuine history. A model
  could read a conflict out of it. Suppressing what the person actually said
  is a larger decision than suppressing what the agent generated, so it was
  left alone deliberately.

- **Hard deletion.** Removing a conversation retires what it taught but
  keeps it in the graph. See `SCHEMA.md` for the delete path added
  alongside it.

---

## 5. What the model gets wrong

Separate from system defects, and worth stating because the graph is only
ever as good as what is written into it.

| Observed | Example |
|---|---|
| Misreads input | "dr appointment" stored as `dentist` |
| Date arithmetic | "coming up in 2 days" (the 20th) described as "tomorrow" on the 18th |
| Predicate fragmentation | the same allergy sentence produced `IS_ALLERGIC_TO` on one run and `HAS_ALLERGY` on another; `clean_predicate` normalises case and spacing but cannot merge synonyms |
| Over-sharing | asked to book a cinema ticket, it volunteered the passport expiry date — fixed by prompt, which is weaker than a structural fix and may recur |
| Ignores instructions | told twice in the prompt not to mention retired items, it did anyway; only removing the data from the tool output worked |

The pattern: **the graph is more reliable than the sentence about it.** Where
behaviour mattered, the fix that held was always the one that changed what
the tools return, not the one that asked the model to behave.

---

## 6. Measured, not estimated

- Concurrency: 16 writer threads, 8 readers, 100 tasks — 484 queries in
  115 ms, zero double-claims, zero errors. The control run, with the claim
  written as read-then-write across two queries, had **16 of 16 workers
  believe they had won**.
- Tenant isolation: the same question answered "no, blocked upstream" in
  `acme` and "yes, cleared" in `globex`, with no tenant named in any query.
- A live local model has driven the full agent loop, chosen its own tools,
  and recovered its own reasoning in a later session through
  `explain_answer`.
