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

## 3c. Resuming a long conversation in a new chat

**The scenario judges actually want for Agent Memory and Coordination:** work
through a problem across many turns, start a fresh chat, say "look at what I
was working on and let's continue", and have the agent pick it up.

**Found by:** three turns designing a Java token-bucket rate limiter, then a
new session asking to continue.

It failed completely, and returned an **empty answer**. The agent had no tool
that could list or read a past conversation. `recall_recent` gave it a flat
stream of fact summaries, so it probed blindly with `recall_entity` and
`recall_related` until the budget ran out, then produced nothing at all.

Worth noting what caused half of this: excluding an agent's own replies from
`recall_recent` (section 3, attempt 3) is right for ambient recall and wrong
for resuming work, where the replies *are* the thing being resumed.

**Changed, three things:**

- `recent_conversations` — the conversations in this workspace, newest
  first, with how each one opened.
- `read_conversation` — one past conversation in full, both sides. This is
  the resolution of the tension above: ambient recall still leaves an
  agent's prose out, because it is unreliable as fact, but a conversation
  asked for by name comes back whole.
- A turn that exhausts its budget now says so instead of returning an empty
  bubble, which read as a crash.

With those, the agent recovered the language, the data structure, the design
decision and the open question. It called `recent_conversations`, then
`read_conversation`, which is the intended path.

**And one more structural guard.** Even with the right tools it then called
`what_is_on` on eight different dates and spent the whole budget after it
already had its answer. The repeat cache only catches *identical* calls, so
a per-tool cap was added: four calls to one tool in a turn, then it is told
to answer with what it has.

**Two tests caught a real design question here.** Both suites assert that a
model cannot name an `agent` or a `session_id`, because provenance is a
property of the wiring. `read_conversation` names a session by design. The
invariant was narrowed rather than dropped: it cannot write, and `tenant`
and `agent` stay bound server-side, so the furthest it reaches is another
conversation in a workspace the caller already had full write access to.

## 3d. Every message stored, and none of it connected

**Found by:** looking at the graph in the UI and saying "I only see
preferences and appointments, not the conversation."

That turned out to be half right in a way worth recording, because the first
instinct — "we are not storing the conversation" — was wrong, and the second
— "something is missing" — was right.

**The conversation was always stored.** `Event.summary` holds the full
verbatim text of every message either side. It is how the UI rebuilds a
transcript from FalkorDB rather than from browser state. What hid it was the
graph *view*: the default canvas filter excludes `Event`, `Session`, `Lookup`
and `Agent` as plumbing, so the one layer being asked about was the one layer
not drawn.

**What was actually missing was edges.** Counted on the three workspaces in
real use:

```
personal   utterances= 28   connected to any entity=  0   islands= 28
work       utterances= 11   connected to any entity=  0   islands= 11
siddhi     utterances=  8   connected to any entity=  0   islands=  8
```

**47 of 47.** Not one message had an outgoing edge. The cause was a single
omitted argument: `record_event` accepts `about`, which wires an episode to
the entities it touched, and `Agent._remember` never passed one. A fact
written by `remember_fact` got its `ABOUT` edges; the message that taught it
got none. So `recall_entity("Java")` returned the one-line extracted fact and
not the ten turns where the problem was actually worked out.

**Changed:** a `Topic` node, an `ON_TOPIC` edge, and a curator pass that
writes both. See SCHEMA.md and QUERIES.md sections 17–18.

### Why a separate pass, and not better prompting

Because that had already failed three times (section 3b), and because the
failure here is structural rather than a matter of wording. One model call
was doing two jobs — hold a conversation *and* curate the graph — and the
second job lost whenever the first was interesting. The quiet version is the
dangerous one: a standing rule the person clearly stated simply never gets
written, the agent truthfully says "noted", and nothing in the system
notices.

So the jobs are split. **The Planner converses and reads; the curator
writes.** `remember_fact` and `learn_procedure` were removed from the
Planner's tools. One writer means no duplicate-suppression race.

`retire_fact` deliberately stayed with the Planner. Retirement is triggered
by a person correcting something mid-conversation, where the Planner has the
context and the curator — which runs afterwards — would be too late. It also
preserves the ordering bug 2.2 required: retire during the turn, assert
after it.

### Verified against a live model

`ornith:9b`, fresh workspace, 18 Oct:

```
session 1: "I am designing lift control logic in Java. For any code you
            give me, put a comment on every single line."
  curated -> topics ['lift control logic']
             rules  ['comment every line of Java code']

session 1: "what data structure should hold the pending floor requests?"
  curated -> topics ['lift control logic']        (reused, not re-coined)

session 2: "lets pick up the lift logic from where we stopped"
  -> recovered the PriorityQueue recommendation

session 3: "write code to print fibonacci series"
  TOOLS: []            <- no tools called at all
  -> Java, with a comment on every line
```

The rule was captured although the Planner has no tool to capture one, and
applied in session 3 although the model called nothing. Both halves come
from the graph: the curator wrote the rule, and `_standing_rules` read it
back into the prompt.

Note what session 2 shows honestly: it resumed via `recent_conversations`
and `read_conversation`, **not** `read_topic`. With one conversation in the
workspace the old path still works. `read_topic` earns its place when there
are many and their opening lines are unhelpful, which is the case it was
built for and the case a single live run does not prove.

### Three defects the browser found that the tests did not

All three were found by driving the real UI with `ornith:9b`, in a fresh
workspace, immediately after the suite went green. None of them raised an
error; all three produced valid JSON and a sensible-looking answer.

**1. A follow-up turn lost its topic.** Turn 1 "I'm working on lift control
logic in Java" was topiced correctly. Turn 2 "what data structure should
hold the pending floor requests?" came back with `"topics": []` — the
curator, seeing one turn in isolation, judged it had no subject of its own.
A ten-turn design discussion would have left two turns findable and eight
lost, which is the whole feature failing quietly for the case it was built
for.

Fixed structurally rather than by prompting, for the fourth time in this
file: **a turn inherits the topics of the conversation it is in** when the
curator names none. A conversation is about something, and a follow-up is
about the same thing until it changes. `session_topics` reads that from the
graph.

**2. The agent's own restatement became a second rule.** Resuming the work,
the Planner said "the standing rule is: any code I give you will have a
comment on every line". The curator read that as a new teaching, and the
graph ended up holding both:

```
comment every line of code
always comment every line of code
```

`procedure_exists` matched on exact name and missed it. This is section 3's
problem in a new place — an agent's prose treated as evidence — and it is
worse for rules than for facts, because every duplicate is injected into
every later prompt and the bloat compounds. `similar_procedure` now compares
the content words of the *action*, and rules already in force are shown to
the curator so it is less likely to re-emit one.

**3. A node literally named `Person`.** The curator prompt said to attribute
a fact about the person to them by name, and never said what the name was.
`Agent.system_prompt` had already learned this exact lesson — "without a
referent for 'I' the agent invents a subject" — and the curator was written
without it. Now passed the session user.

Worth noting the root cause behind that one, unfixed: `app.py` sets
`USER = "you"`, so the curator is told it is talking to someone called
"you". Changing it would orphan facts already stored against that name in
existing workspaces, so it stands.

### 3e. "It said noted, and then forgot"

**Found by:** teaching it "from now on i want any code i ask first
preference in java", getting "Noted — from now on, any code you ask for
will first be in Java", and then getting Python for BFS in the next chat.

Two separate defects, and the investigation is the useful part.

**The rule was never written.** Only `comment every line of code` was in
force, so Python was the correct answer to an empty rule set. The capture
failed, which is the one thing the curator exists to prevent.

Re-running that exact turn through the curator afterwards extracted the
rule correctly, four times out of four. So it was not the model, not the
prompt and not the duplicate guard — the Java action shares no content
words with the comment rule. What actually happened is that `curate.py` had
been edited while the Streamlit process was up, the page was showing
Streamlit's "File change — Rerun" banner, and the turn ran against a
half-reloaded module.

**That diagnosis took half an hour and should have taken five seconds.**
`curate_turn` swallows every exception on purpose, so a failure costs edges
rather than the reply. But the error was returned on the `Turn` and then
dropped. Nothing was written, nothing was shown. A turn where the agent
says "noted" and stores nothing left **no trace anywhere** — which is
precisely the failure section 3b calls the dangerous one, reintroduced by
the mechanism built to fix it.

**Changed:** every curated turn now records the outcome on the answer event
(`curated`, `curation_note`), `failed_curations()` lists the ones that did
not run, and the Memory-changes panel says so outright. The next time this
happens it is one query, or one glance.

**And the agent volunteered an unrelated conversation.** The same turn
asked only for Java Fibonacci, and the reply appended "We left off after
discussing the `Map<Floor, Queue<Request>>` structure for pending floor
requests. What would you like to work on next?". Tools called:
`what_is_on`, `read_topic`, `read_topic`, `record_decision` — four, none
asked for.

Two causes, both mine:

- **The injected index was an invitation.** It read "Subjects already
  discussed in this workspace. To continue one, call read_topic with the
  name exactly as written here". The model took the suggestion. It now
  reads as reference, and says not to open one unless the person asks.
- **The Planner's prompt described a system that no longer existed.** It
  still told the agent to call `remember_fact` and `learn_procedure`, both
  removed when the curator took over writing, and `get_applicable_procedures`,
  which was never in its tool list at all. Rewritten to match its actual
  tools and to say that standing rules arrive in its instructions.

There is a feedback loop worth naming, which the first fix also closes: the
agent's unasked-for tangent was then curated as the turn's topic, so the
Fibonacci session became tagged `lift control logic`, and carry-forward
would have propagated that to every later turn in it. An agent's own
digression becoming recorded truth about what the conversation is for is
the same family as section 3 — prose treated as evidence — and it
compounds.

**Verified after the fix**, fresh workspace, `ornith:9b`:

```
session 1  "provide in java, also from now on i want any code i ask
            first preference in java"
           tools: []            <- no tangent, nothing unasked-for
           rule captured: 'Java-first code rule'

session 2  (NEW CHAT) "can you provide me code for bfs ?"
           tools: []
           -> "Here's a BFS implementation in Java:"
```

## 3f. The agent could not see the conversation it was having

**Found by:** working through a LeetCode problem over five turns, then
asking "can you add my uid at the top of this code?" and getting
*"I don't see any code attached to your message."* It had written three
thousand characters of Java two messages earlier.

It was telling the truth. `Agent.run` began every turn with:

```python
messages: list[Message] = [Message(role="user", text=user_message)]
```

**One message. No history, ever.** The UI shows a continuous thread and the
model saw each message alone. Every "fix this", "that one", "the code you
just gave me" was unanswerable, and had been since the agent loop was
written — missed because every test and every demo moment is a single turn
or a deliberate cross-session recall, and because the prompt told the agent
its memory was the graph, which read as an explanation rather than a bug.

**Changed:** the loop now replays the conversation at the start of each
turn, **read back out of the graph** by `session_transcript` — the same
Cypher the UI uses to rebuild its transcript on reload. That matters for
more than tidiness: an agent holding its own history in process memory
would be the one place in this system where recall did not go through
FalkorDB. Kill the process, reload the page, and the conversation the model
sees is identical, because it was never anywhere else.

Bounded by characters rather than turns, because a turn here can be four
thousand characters of Java. The visibility added in 3e earned itself
immediately by recording the reason a curation had failed on exactly such a
turn:

```
LLMError: reply hit the context limit (num_ctx=16384) and is truncated
```

The prompt was also wrong and is now corrected. It said "do not rely on
what appears earlier in this conversation as if it were established fact",
which is right about *facts* and wrong about *working context*: what "this
code" refers to is in the conversation, and asking the person to repeat
themselves is a failure. It now distinguishes the two.

One invariant had to be narrowed rather than dropped.
`test_provider_state_is_carried_on_assistant_turns` asserts the loop hands
the provider's original turn back untouched so reasoning blocks replay
byte-identically. Replayed history carries no `provider_state` — it is
prior turns, and the provider only requires reasoning blocks preserved
within the turn that produced them. `Turn.replayed` now counts the leading
messages that are history, and the invariant applies to the rest. The
Claude adapter already rebuilds a `provider_state`-less assistant turn from
text, so this is handled — **but no request has ever been sent to the
Anthropic API, so that path remains unverified against the real thing.**

### The same bug twice: trimming emptied the history

The first fix did not work, and the second browser test found it in one
message. A 6,918-character Java answer, then "can you add my uid in this
code?", and the same reply as before.

Walking the transcript newest-first, the budget check is skipped while
nothing has been kept yet -- so the big assistant turn went in, then "yes"
broke the loop on the next iteration. That left a history of one assistant
message, and the rule that a history may not begin on an assistant message
popped it. **Two individually correct guards, and between them an empty
history in exactly the case the feature exists for.**

The budget was also simply too small: 5,000 characters, against a single
answer of 6,918.

**Changed:** trimming works in whole exchanges -- a user message and
everything said in reply -- so a reply is never orphaned from its question
and the "must start on a user turn" rule can never empty the list. The most
recent exchange is kept whatever it costs, because it is what "this code"
refers to and dropping it defeats the point. Budget raised to 12,000
characters, against `ornith:9b`'s 16,384-token window.

A second, milder problem showed up once the code was visible. The agent
could now see it -- "the code is a LeetCode solution" -- but asked which
code was meant and offered three interpretations instead of editing it.
Prompt-level fix, and flagged as such: asking someone to paste back
something you wrote two messages ago is the worst available answer.

Verified, `ornith:9b`:

```
turn 1   "give me a full java solution for leetcode 1569 ... with comments"
         -> 5,936 characters
         history available to the next turn: 2 messages, 6,045 chars
                                             (previously: 0)

turn 2   "can you add my uid 1222987805 in this code?"
         -> returns the whole solution with "User: 1222987805" in the header
```

## 3g. Three copies of the same preference

**Found by:** the same session. Four `Decision` nodes, three of them the
same two preferences:

```
Decision  'Code comments rule'
Decision  'Code language preference: Java first'
Decision  'First preference language for code'
```

The curator had already stored both correctly as `Procedure` nodes. The
Planner still has `record_decision` — it needs it, demo moments 1 and 5a
turn on it — and was using it for preferences as well, so every preference
landed twice, and again each time it was mentioned.

**Changed, in both places:** the prompt now says a preference is not a
decision, and `record_decision` refuses a near-duplicate of a currently
valid decision, returning the existing id. The guard is skipped when
`supersedes` is passed, because superseding is the sanctioned way to
restate a decision and it keeps the history connected. On the evidence in
this file the prompt half will not hold alone; the refusal is the half that
will.

## 3h. Why every node looked single

**Found by:** looking at the rendered graph and asking why nothing connects.

Measured on a real workspace:

```
Decision  'Code comments rule'              degree 1
Decision  'Code language preference...'     degree 1
Procedure 'comment every line of code'      degree 1
Procedure 'Java first preference for code'  degree 1
Topic     'array reordering'                degree 12
Topic     'lift control logic'              degree 10
```

A `Procedure` or `Decision` is created with exactly one edge, to the
`Event` that made it. Nothing tied it to the subject it was agreed under,
so in the canvas it is a dot on its own — correct, and useless.

**Changed:** after a curation writes its facts and rules, whatever this
conversation created is linked `ABOUT` the turn's topics. A rule learned
while discussing lift control logic is about lift control logic, and that
is a traversal worth having. `ABOUT` is reused here for the same reason as
everywhere else: "what is this node concerned with" is one question.

The ordering caught a bug of its own in review — the link ran before the
rules were written, so it linked nothing. It now runs last.

### What a live run confirmed

Fresh workspace, three sessions, nothing carried between them but the graph:

```
session 1  "I am working on lift control logic in Java. For any code you
            give me, put a comment on every single line."
           -> topic 'lift control logic'; rule 'comment every line of code'

session 1  "what data structure should hold the pending floor requests?"
           -> Map<Floor, Queue<Request>>   (topic carried forward)

session 2  (NEW CHAT) "lets pick up the lift logic from where we stopped"
           -> called read_topic('lift control logic')
           -> "We left off right after I offered to sketch a small class
               that wraps the Map<Floor, Queue<Request>> structure"

session 3  (NEW CHAT) "write code to print fibonacci series"
           tools called: []
           -> every line commented
```

Session 2 is the resume working through the new path: the topic index named
the subject before anything was asked, and `read_topic` was the tool chosen.
Session 3 is the rule applying with **no tools called at all** — it came
from injected context, which is the whole reason the rules are injected.

Session 3 answered in **Python, not Java**, and that is correct. The only
standing instruction given was about comments; "in Java" described the work
in hand, not a preference. The curator was right not to invent a Java rule
from it.

### What the tests caught that a run would not

- **Ordering by timestamp alone.** Two subjects touched in the same second
  tie on `max(e.ts)` and fell back to alphabetical — in an index whose whole
  claim is "most recently discussed". Fixed by tie-breaking on `max(e.id)`,
  which is the `ORDER BY (ts, id)` convention the rest of the store already
  follows and which this query had quietly skipped.
- **A removed conversation leaking back.** `read_topic` needed
  `s.hidden IS NULL`, or removing a chat closed the sidebar route and left
  the topic route wide open.
- **A `Procedure` offered as an entity to mention.** Its `name` is an
  internal handle, not something a person refers to.

### And one silent failure in the demo runner

Removing `learn_procedure` from the Planner broke demo moment 4: the rule
was never written, the Reviewer correctly reported "no standing rules apply
here", and `run_demo.py` printed **"all 10 moments ran"**. The tool error was
right there in its own output and nothing was checking it.

The runner now fails a moment whose agent could not call a tool. Same shape
as every other silent no-op in this file: the failure was visible and nothing
looked.

## 3i. No view could draw the thing we had just built

**Found by:** "why don't we show this graph on our site? it's directly from
FalkorDB", with a screenshot of FalkorDB Browser showing 105 nodes and 231
edges of `tenant_lifttest`.

Two parts to the answer, and the second was a real defect.

**We already use their renderer.** `@falkordb/canvas@0.3.3` is the same
force-directed component FalkorDB Browser draws with. The Browser *page*
cannot be embedded -- `X-Frame-Options: DENY` and
`frame-ancestors 'none'`, two independent blocks -- but the component is
published standalone and we have been using it since 18 Oct.

**But no view could show the conversation.** The view allow-lists are
tuples of closed-schema labels:

```python
WORLD = ("Person", "Project", "System")
SEMANTIC = ("Decision", "Procedure", "Insight")
OPERATIONAL = ("Task", "Ticket", "Agent")
EPISODIC = ("Session", "Event")
```

`Topic` is in none of them, so every allow-list view silently excluded it.
The one view that *would* show a Topic uses an exclude-list -- and that one
excludes `Event`. **So there was no view in the app that could draw a
conversation joined to its subjects**, which is the entire point of the
layer added the same day. The Browser looked richer because it was showing
something our app could not draw at all.

The same trap applies to every open-layer label: an `Appointment` or a
`Language` is in no tuple either, so an allow-list view cannot show those
either. Worth knowing before adding another view.

**Changed:** a `Conversation and subjects` view (`Topic`, `Event`,
`Session`), an `Everything` view that filters nothing at all, `Topic` given
its own colour instead of falling through to the default grey, and a legend
of labels and relationship types counted from the payload -- a force layout
without a key is a hairball, with one it is a diagram. The legend is built
from what is on screen rather than from the schema, so it cannot name a
type the picture does not contain.

**And a rendering bug underneath it.** With the conversation layer visible
the canvas went black. The host page fitted the graph and then clamped the
zoom up to a floor of 1.15, which exists so captions stay legible on the
thirty-node seeded world. On a seventy-node graph the fit lands far below
that, so clamping back up pushed every node outside the viewport and the
component's culling drew nothing. `chargeStrength: -900`, tuned on the same
small world, made the spread worse.

Both now scale with the graph: repulsion eases towards -250 as nodes grow,
and the zoom floor is only applied when the fit is already near it. Below
that the graph is too large for legible captions anyway, and seeing its
shape is worth more than reading it.

A note on how long that took to find, because it is a reusable lesson:
much of the diagnosis was done against a component instance that had
already been poked with `zoomToFit`, `applyLayout` and `setConfig` from the
console. Measurements taken from it -- including "even twenty nodes paint
nothing" -- were worthless. The answer came from one clean reload.

## 3j. "What did we decide?" — the idea, and why the obvious version fails

**Proposed by the builder:** since the conversation is stored in increasing
order, walk it backwards — optionally filtered by keyword — to find a
decision.

The instinct is right and the ordering half was already true: ids carry a
microsecond prefix precisely so `(ts, id)` sorts chronologically. And there
is a real gap to fill. Formal decisions become `Decision` nodes, but the
moments that settle a piece of work never did: the Planner only records
work decisions, and section 3g made a preference explicitly not one.

**The keyword half fails, quietly.** After one turn chooses a PriorityQueue
and a later turn switches to an ArrayDeque, searching "queue" matches both
and nothing marks which holds. A settlement stated in prose has no
`valid_to` — that is section 3 again, in a fourth place.

**Built instead:** the curator marks the turn. `(Event)-[:SETTLES]->(Topic)`
with the decision on `Event.settled`, and "what did we decide" becomes an
ordering rather than a guess. Not versioned: a later settlement supersedes
an earlier one by being later.

Keyword search was still worth having, and it turns out FalkorDB has a real
full-text index — so it is a database query, not a Python scan. The DDL in
the docs (`db.idx.fulltext.createNodeIndex`) is rejected on v6.0.2;
`CREATE FULLTEXT INDEX FOR (e:Event) ON (e.summary)` works. It matches
whole words only — `queue` does not find `PriorityQueue` — which is the
second reason marking carries the feature rather than search.

**Verified live**, `ornith:9b`, fresh workspace:

```
"what should hold the pending floor requests?"
  settled: ''                           <- a question settles nothing

"ok lets go with a PriorityQueue ordered by floor number"
  settled: 'Pending requests are handled using a PriorityQueue ordered
            by floor number.'

"actually scrap that, an ArrayDeque is simpler"
  settled: 'Floor request handling switched from PriorityQueue to
            ArrayDeque'

(NEW SESSION) "what are we using to hold pending floor requests?"
  tools: ['what_was_decided']
  -> "Pending floor requests are held in an ArrayDeque — we switched from
      a PriorityQueue to that earlier today."
```

**One thing a single live run caught.** On the first attempt the agent
received a correctly newest-first list and narrated it *oldest*-first. It
still reached the right conclusion, which is exactly the kind of result
that would have shipped a latent bug. The fix was the usual one: the tool
now flags the newest settlement per topic as `current` and the rest as
`superseded`, rather than relying on the reader noticing the order.

**And a self-inflicted one worth recording**, because it will recur: adding
`settles` to the curator's required keys broke three unrelated tests with
"FakeAdapter script exhausted". A scripted reply missing a required key
does not fail loudly — `ask_json` validates, retries, and silently
consumes the next queued reply, so the error surfaces in a different test
than the one at fault. The test helper now builds every required key in one
place.

## 3k. One agent, counted

**Found by:** looking at the rendered graph and saying "planner is our 1
agent" — the brief asks for state shared *between* agents, and the picture
showed one.

Counted across every workspace:

```
lifttest   agents=['Planner']              tasks=0  tickets=0  reviewer_utterances=0
siddhi     agents=['Planner']              tasks=0  tickets=0  reviewer_utterances=0
work       agents=['Planner']              tasks=0  tickets=0  reviewer_utterances=0
personal   agents=['Planner']              tasks=0  tickets=0  reviewer_utterances=0
acme       agents=['Planner','Reviewer']   tasks=1  tickets=1  reviewer_utterances=4
```

**In every workspace actually used, there was one agent.** The two-agent
story existed only in the seeded company graph — a scripted terminal demo,
not the product.

It was not a plumbing gap. The Reviewer has a modal in the app with an
open-task list and a button; the Planner has `create_task`; the Reviewer
has every tool it needs. What was missing was a **job**: both prompts said
"in a small engineering organisation", and the Reviewer's role was to block
a release and file a ticket. Nothing in a coding or personal conversation
ever triggers that, so no task was ever filed, so the button stayed
disabled.

### And underneath it, bug 2.1 for the third time

Giving the Reviewer a job exposed a deadlock that would have stopped it
anyway. `create_task` matched on both `Agent` nodes:

```cypher
MATCH (creator:Agent {name:$agent}), (assignee:Agent {name:$assignee})
CREATE (t:Task ...)
```

In a workspace built by conversation there is no `Agent {name:'Reviewer'}`
until the Reviewer runs. The MATCH found nothing, the CREATE silently did
not run, and the method **returned an id for a task that did not exist**.
No task was waiting because the Reviewer had never run; the Reviewer never
ran because no task was waiting.

Same silent no-op as section 2.1, in a third place, and the same fix:
`MERGE` the agents, and raise when nothing was written.

`CONCERNS` had the matching flaw — it accepted only `Decision` and
`Project`, both absent from a personal workspace, so a task could carry
nothing but its own title. That is the `PROCEDURE_SCOPES` trap from 3b
again: a closed list written for the seeded world. It now accepts a
`Topic`, and `get_task_context` returns what has already been settled on
that subject, so the Reviewer's first read already holds the thing it was
asked to check against.

**Verified live**, `ornith:9b`, fresh workspace, two sessions:

```
Planner  "can you get the reviewer to check the lift design
          against my standing rules?"
         tools: ['read_topic', 'create_task']
         -> task filed, concerning Topic 'lift controller design'

Reviewer (separate session, told only the fixed trigger sentence)
         tools: ['claim_task', 'get_task_context',
                 'get_applicable_procedures', 'what_was_decided',
                 'read_topic', 'complete_task']
         -> "The lift controller design is consistent with the standing
             rules - no conflict found."

find_prompt_leaks(['lift control logic', 'ArrayDeque',
                   'Java code preference', 'Siddhi', 'lift controller'])
         -> []
```

Six tools, every one a graph read, and nothing about the work reached it in
prose. The graph afterwards holds two `Agent` nodes, one `Task` marked
`done`, and a `CONCERNS` edge to the subject.

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

- **The curator costs a second model call per turn.** On a local 9B model
  that latency is noticeable. It buys reliable capture, which prompting
  demonstrably did not, but it is a real trade and not a free one.

- **A settlement is only as good as the curator's judgement.** It decides
  what counts as deciding. Observed to discriminate correctly between a
  question, an option list and a choice on a live run, but that is one run
  on one model, not a measurement.

- **Topic synonyms still fragment.** Normalising case and spacing stops
  `Lift Control Logic` and `lift control logic` splitting, and showing the
  curator the existing topics drives reuse, but nothing merges "lift logic"
  and "elevator control" if the model coins both. Same shape as the
  predicate fragmentation in section 5, and unsolved for the same reason.

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

- Concurrency: 16 writer threads, 8 readers, 100 tasks. **100 claims, zero
  double-claims, zero left open, zero errors — on every run.** Throughput
  varies, because the readers loop until the writers finish: 460-580
  queries in 100-136 ms across the runs recorded, around 4,300-4,700
  queries/sec. An earlier version of this line quoted a single run's totals
  as if they were fixed, which is the same mistake as estimating.
  The control run, with the claim written as read-then-write across two
  queries, had **16 of 16 workers believe they had won**.
- Tenant isolation: the same question answered "no, blocked upstream" in
  `acme` and "yes, cleared" in `globex`, with no tenant named in any query.
- A live local model has driven the full agent loop, chosen its own tools,
  and recovered its own reasoning in a later session through
  `explain_answer`.
