# Memory Graph Schema

**Status: locked.** Every FalkorDB behaviour this schema relies on was verified
by running probes against FalkorDB graph module **v6.0.2** on 2026-10-15, not by
reading documentation alone. See [Verified behaviour](#6-verified-behaviour).

This file is the source of truth for the graph model. Section 5 of
`PROJECT_CONTEXT.md` summarises it and points here; if the two ever disagree,
this file wins.

---

## 1. The organising idea

### A behavioural core and an open layer

The catalogues below are the **behavioural core**: types the system *does
something with* — supersession, rule matching, atomic claiming,
consolidation. Code depends on their shape, so they are closed.

Everything else a person says is an ordinary fact, and the agent invents the
type when it writes it. `remember_fact` creates real labels and real
relationship types at run time, so an `Appointment`, a `Vendor` or a `Risk`
is a first-class node that `recall_entity`, `explain_path` and centrality all
work on, without a line of code knowing those types exist.

This was not the original design, and the original design was wrong. A closed
schema meant the agent could only remember shapes chosen in advance; told
"I have a doctor appointment on the 20th" it replied that it would not record
a personal matter, and the fact was lost. A memory that refuses facts is a
form with fixed fields.

The open layer is not a junk drawer, because an open fact gets the same
discipline as a built-in one:

| | |
|---|---|
| **Structured on arrival** | subject, predicate, object — the model must decide what relates to what, not emit prose |
| **Entities reused** | a name that already exists is linked to, never duplicated; reuse wins over the proposed type |
| **Versioned** | `valid_from` / `valid_to`, retired and superseded like anything else |
| **Sourced** | `source_event` points at the episode that established it |
| **Dated** | `when` carries the date the fact is *about*, which is what makes `what_is_on()` possible |

**The security tradeoff, stated plainly.** Cypher cannot parameterise a label
or a relationship type, so model output that becomes one must be *validated*
rather than escaped. The closed schema used a fixed whitelist; the open layer
uses pattern validation (`^[A-Za-z][A-Za-z0-9_]{0,30}$` for labels,
`^[A-Z][A-Z0-9_]{0,40}$` for predicates) plus a reserved list so an open fact
cannot impersonate a behavioural type. That is weaker than a whitelist and
worth knowing. What it defends against is the characters that would break out
of the identifier position; a silly-but-harmless name like `DROP_ALL` is
allowed through deliberately, because blocking words that merely *look*
dangerous is cargo-cult while blocking the characters that escape the
position is the actual defence. Tested in `tests/test_open_facts.py`.

### Three tiers

Memory is split into three tiers governed by *different* rules. Most of the
design follows from this one distinction.

| Tier | What it holds | Mutability |
|---|---|---|
| **Episodic** | What happened: sessions, events, and the reads behind an answer | **Immutable.** An event never stops having happened, and neither does a read. No validity window. |
| **Semantic** | What is true: facts about the world | **Versioned.** Facts are retired, never deleted or edited in place. |
| **Operational** | Coordination plumbing: tasks, tickets | **Mutable in place.** Not a belief about the world, so versioning it would be ceremony. Its history already lives in the episodic tier. |

A consequence worth stating plainly: **the graph never forgets by overwriting.**
The only destructive operation in the system is closing a validity window, and
that leaves the old fact fully readable.

## 2. Conventions

These apply everywhere and are not repeated in the catalogues below.

### Timestamps are ISO-8601 UTC strings

Every timestamp is a string of the exact form `2026-10-15T14:30:00Z` — fixed
width, always UTC, always `Z`.

FalkorDB does support temporal property types (`date()`, `localdatetime()`), but
we deliberately do not use them:

- The docs name them inconsistently across pages (`DateTime` vs `LocalDateTime`).
- At least one official client cannot bind temporal values back as query
  parameters, so they must be constructed inside the query string.
- Fixed-width UTC strings sort lexicographically in chronological order
  (verified, T5a) and support range comparison (T5b).
- Strings are range-indexable; whether temporal types are is not documented.

Cost of this choice: no date arithmetic in Cypher. We do date arithmetic in
Python. For this project that costs nothing.

**Ids are time-sortable, and this is load-bearing.** Second precision means
several events written during one agent turn tie on `ts`. Every ordering
therefore falls through to `id`, so ids are minted as
`evt-<fixed-width microsecond hex>-<random suffix>`: the prefix sorts
lexicographically in time order, the suffix keeps them unique across
concurrent writers. With random uuids instead, `recall_recent` returned a
turn's events in arbitrary order — caught by a test, not by inspection.
Order by `(ts, id)`, never by `ts` alone.

### Validity windows mark what is current

Every **versioned** fact — whether node or relationship — carries:

| Property | Meaning |
|---|---|
| `valid_from` | ISO-8601 UTC. Always present. When the fact started being true. |
| `valid_to` | ISO-8601 UTC. **Absent while the fact is current.** Set to retire it. |
| `source_event` | The `id` of the `Event` that established the fact. |

`valid_to` is *absent*, not null. This is forced by FalkorDB rather than chosen:
null cannot be stored as a property value, so `SET x.valid_to = NULL` removes the
key entirely (verified, T1c). The test for a current fact is therefore
`WHERE x.valid_to IS NULL`.

**Retiring a fact never deletes it.** It sets `valid_to`. The retired edge stays
in the graph alongside its replacement, which is why history is queryable at all.

### Provenance is traversable for nodes, a join for relationships

Cypher cannot attach an edge to an edge. So provenance works in two ways:

- **Versioned nodes** (`Decision`, `Procedure`, `Insight`) get a real provenance
  *edge* to the `Event` that created them. These are traversable in one hop and
  carry the explainability demo.
- **Versioned relationships** can only carry `source_event` as a scalar id. The
  `Event` is recovered by a second `MATCH` on `Event.id` in the same query.

The alternative — reifying every fact into its own node so provenance is always
an edge — would make provenance uniform at the cost of roughly doubling the node
count and making every query and every rendered graph harder to read. Rejected as
not worth it. The asymmetry is a known, deliberate limitation.

### Tenants are separate graphs

One FalkorDB graph per tenant (`tenant_acme`, `tenant_globex`). There is no tenant
property on any node and no tenant filter in any query — isolation is structural,
so a cross-tenant leak is not possible by forgetting a `WHERE` clause.
*(Unverified: the sponsor's claim that one instance holds thousands of graphs. Two
is all we need; test before claiming more.)*

---

## 3. Node catalogue

| Node | Properties | Tier |
|---|---|---|
| `Agent` | `name`, `role` | — |
| `Session` | `id`, `started_at`, `user` | Episodic |
| `Event` | `id`, `ts`, `kind`, `summary`, `speaker` | Episodic |
| `Person` | `name`, `role`, `aliases[]` | Semantic (identity) |
| `Project` | `name` | Semantic (identity) |
| `System` | `name` | Semantic (identity) |
| `Decision` | `id`, `title`, `reason`, `valid_from`, `valid_to`, `source_event` | Semantic (**versioned**) |
| `Procedure` | `id`, `name`, `trigger`, `action`, `valid_from`, `valid_to`, `source_event` | Semantic (**versioned**) |
| `Insight` | `id`, `text`, `valid_from`, `valid_to` | Semantic (**versioned**) |
| `Task` | `id`, `status`, `created_at`, `claimed_by`, `claimed_at` | Operational |
| `Ticket` | `id`, `title`, `created_at` | Operational |
| `Lookup` | `id`, `tool`, `arguments`, `ts`, `agent`, `session` | Episodic |
| `Topic` | `name`, `first_seen` | Semantic (identity) |

### Why each node exists

**`Agent`** — the subject of provenance. Without it, "which agent learned this?"
is unanswerable, and that question is half of what separates graph memory from a
transcript. Two instances: Planner and Reviewer.

**`Session`** — the episodic container and the unit that proves persistence. Demo
moment 3 only lands because session 2 reads what session 1 wrote without being
handed it.

**`Event`** — the atom of episodic memory and the anchor of all provenance. Every
write to the graph produces one. `kind` is a closed set: `message`, `decision`,
`action`, `handoff`, `correction`, `consolidation`. Immutable by design: when the
world changes we write a *new* event and never edit an old one. That immutability
is what makes the audit trail worth trusting.

`speaker` is set only on utterances, and names who said it — `"user"` when a
person typed it, or an agent's name when an agent said it. `PERFORMED` cannot
carry this, because an agent records the user's message and therefore performs
an event it did not author. The distinction is what lets a conversation be read
back out of the graph: the UI rebuilds its transcript from
`session_transcript()` rather than from browser state, so reloading the page
loses nothing. Events without a `speaker` are the agent's working record rather
than anything that was said.

**`Person`, `System`** — stable semantic identities. Not versioned, because the
*entity* does not expire; its *relationships* do. Lee Park does not stop existing
when he stops owning Ledger Service. `aliases[]` is a string array, which FalkorDB
both stores and range-indexes, and is what lets "Dana" resolve to "Dana Ruiz".

**`Project`** — the hub of the demo: what decisions affect, what depends on what,
what a ticket blocks. **It deliberately has no `status` property** — see
[Derived project state](#derived-project-state).

**`Decision`** — the headline semantic node. It is a node rather than an edge
property for three reasons, each impossible for an edge: it needs its own
provenance edge to an `Event`, it needs to be the target of a `Task`, and it needs
`SUPERSEDES` to chain to another `Decision`.

**`Procedure`** — procedural memory; the standing rule the user teaches mid-demo.
Versioned because a rule can be revoked, and "the agent correctly stopped applying
a retired rule" is a stronger demonstration than "the agent applied a rule".

**`Insight`** — the target of consolidation. When several episodes say the same
thing, they are summarised into one lasting semantic fact that keeps edges back to
every source event. Without this node, consolidation has nowhere to land:
`Decision` is wrong because nobody decided it, and `Procedure` is wrong because it
is not a rule.

Two rules keep consolidation from eating itself. Episodes with an incoming
`DERIVED_FROM` are already absorbed and drop out of candidate selection. And
events of kind `consolidation` are never offered as raw material — writing an
insight produces an event `ABOUT` the same entity, so without that exclusion
each consolidation would feed the next and the loop would never settle.

**`Task`** — the handoff. Its `status` (`open` → `claimed` → `done`) *is* the
coordination mechanism: the Reviewer finds work by querying the graph, not by
receiving a prompt. See [The claim operation](#5-the-claim-operation).

**`Ticket`** — the single Track 01 action output. One action, per the scope
guardrail.

**`Lookup`** — one read an agent made: the tool, its arguments, and the moment.
Mutating tools already write an `Event`, so what an agent *changed* was always
traceable; what it *read* was not, and reads are what decide an answer. Without
this node, "why did you say that?" can only be answered from whatever the UI
still has in memory, which is gone on reload. With it, the question is a
traversal.

It is deliberately **not** an `Event`. An agent consulting the graph is not an
episode in the organisation's history: making it one would flood
`recall_recent`, pull lookups into `consolidation_candidates` as raw material,
and put the agent's own reading into the transcript the UI replays. Separate
label, same tier, different purpose.

**`Topic`** — what a turn of conversation was *about*. Added 18 Oct, and the
measurement that forced it: across the three workspaces in real use, **47 of
47 utterances had no outgoing edge at all.** Every message was stored in full
— `Event.summary` holds the verbatim text — and not one of them was reachable
from anything it concerned. `record_event` has always accepted an `about`
argument that wires an episode to its entities; the agent loop never passed
one, so only facts written by `remember_fact` got edges and the conversation
that taught them got none.

A topic is in the **closed behavioural core** rather than the open layer,
by this file's own test for which side of the line a type sits on: code does
something with it. `topic_index` renders it into the Planner's prompt and
`read_topic` traverses it. So it is also in `RESERVED_LABELS`, and `ON_TOPIC`
in `RESERVED_PREDICATES`, so an open fact cannot impersonate either.

Not versioned, for the same reason as `Person` and `System`: the identity
does not expire. An event never stops having been about something.

Names are lower-cased and whitespace-collapsed on write. That is the cheap
half of the fragmentation problem in docs/TESTING.md section 5 — it stops
`Lift Control Logic` and `lift control logic` becoming two nodes, and cannot
do anything about genuine synonyms. The other half is handled by *showing the
curator the topics the graph already holds*, so reuse is driven by the graph
rather than by hoping the model is consistent with itself.

### Derived project state

A `Project` has no `status`. "Is Spring Launch blocked?" is answered by traversing
to the currently-valid decisions that affect it:

```cypher
MATCH (d:Decision)-[a:AFFECTS]->(p:Project {name: $project})
WHERE d.valid_to IS NULL AND a.valid_to IS NULL
RETURN d.title, d.reason
```

This is stronger than storing a status property, for a reason worth saying out
loud in the demo: **state can never drift from its justification.** A stored
status can be stale or unexplained. A derived one arrives with its reason
attached, and retiring the decision updates the project's state for free.

---

## 4. Relationship catalogue

### Episodic and provenance — never versioned

| Relationship | Why it exists |
|---|---|
| `(Session)-[:HAS_EVENT]->(Event)` | The timeline. Ordering comes from `Event.ts`, not edge order. |
| `(Agent)-[:PERFORMED]->(Event)` | Attribution. Makes "who learned this" answerable. |
| `(Event)-[:ABOUT]->(Person\|Project\|System\|Decision)` | Links an episode to the entities it touched. This is what `recall_related(entity)` traverses. |
| `(Person)-[:MADE]->(Decision)` | Which *human* decided, as distinct from which agent recorded it. Not versioned: who made a decision is a historical fact that never stops being true. |
| `(Decision)-[:RECORDED_IN]->(Event)` | Traversable provenance for decisions; the spine of the explainability demo. |
| `(Procedure)-[:LEARNED_FROM]->(Event)` | Same for rules: "you told me this on Oct 15, in session 1." |
| `(Insight)-[:DERIVED_FROM]->(Event)` | Many-to-one: the episodes a consolidation summarised. It does double duty — it is both the provenance trail and the marker that those episodes have been absorbed, so candidate selection excludes them automatically rather than needing a flag. |
| `(Event)-[:READ]->(Lookup)` | The reads behind one answer. The `Event` is the agent's own reply, so this edge is what makes "why did you say that" a traversal rather than a screenshot. Attached when the answer is written, so a turn that crashed before answering leaves its lookups unlinked — findable by `Lookup.session`, which is the honest outcome: the reads did happen. |
| `(Event)-[:ON_TOPIC]->(Topic)` | What a turn was about, so a past conversation is findable by **subject** rather than by its opening line. `recent_conversations` titles a session with its first message, which for a long design discussion is the least representative sentence in it — the decisions are in turns 7 to 9. Not versioned: the episodic tier is immutable. |
| `(Lookup)-[:RETURNED]->(any)` | Which nodes a read surfaced. Resolved against the graph at write time rather than taken on trust, so a citation exists only if a node really bears that key — the trace cannot claim a fact the graph does not hold. |
| `(Insight)-[:ABOUT]->(Person\|Project\|System)` | What a consolidation is about, so `recall_related` can lead with it. Shares the `ABOUT` type with events deliberately: "what is this node concerned with" is the same question either way. |

### Semantic world facts — every one carries `valid_from`, `valid_to`, `source_event`

| Relationship | Why it exists |
|---|---|
| `(Person)-[:OWNS]->(Project)` | Official ownership. Versioned because ownership transfers. |
| `(Person)-[:WORKED_ON]->(Project)` | Actual involvement. **The gap between this and `OWNS` is the informal-expert moment**: the graph knows who really knows the system; an org chart would not. |
| `(Project)-[:USES]->(System)` | Technical footprint. The hop that resolves "finance systems are affected", which fires the Priya rule. |
| `(Project)-[:DEPENDS_ON]->(Project)` | The multi-hop edge. Billing v2 → Spring Launch is the hidden dependency the Reviewer finds. |
| `(Decision)-[:AFFECTS]->(Project)` | What a decision touches. Versioned separately from the decision, so scope can narrow without the decision being retired. |
| `(Procedure)-[:APPLIES_TO]->(Project\|System)` | Rule scope, versioned so a rule's reach can change. |

### The update chain

| Relationship | Why it exists |
|---|---|
| `(Decision)-[:SUPERSEDES]->(Decision)` | `valid_to` says a decision *stopped being current*; `SUPERSEDES` says *what replaced it*. Both are needed: the first makes "what is true now" a filter, the second makes "walk me back through the history" a traversal. |

`SUPERSEDES` exists **only** between `Decision` nodes. For a plain versioned edge
such as `OWNS`, "who replaced Lee?" is already answerable by matching the same
pattern with an open `valid_to`, so a supersession edge would be redundant.

### Operational — not versioned

| Relationship | Why it exists |
|---|---|
| `(Agent)-[:CREATED]->(Task)` | Who asked. |
| `(Task)-[:ASSIGNED_TO]->(Agent)` | Who must act. Split from `CREATED` so the two are separately queryable. |
| `(Task)-[:CONCERNS]->(Decision\|Project)` | **The literal replacement for message-passing.** The claiming agent reads what the task is about from the graph, not from a prompt. |
| `(Agent)-[:FILED]->(Ticket)` | Attribution for the action. |
| `(Ticket)-[:BLOCKS]->(Project)` | The action's effect on the world. |
| `(Ticket)-[:CITES]->(Decision)` | Without it the ticket is an orphan assertion. With it the demo renders the full chain: ticket → decision → event → session → agent. |

---

## 5. The claim operation

FalkorDB's [concurrency model](https://docs.falkordb.com/design/concurrency)
guarantees that writes to one graph are serialized and that each write query is
individually atomic, but it explicitly does **not** support multi-query
transactions. That single fact determines the design: **the claim must be one
query that both tests and sets the status.** A read followed by a separate write
is a lost-update bug.

Claim a specific task — the `WHERE` is the compare, the `SET` is the swap:

```cypher
MATCH (t:Task {id: $tid})
WHERE t.status = 'open'
SET t.status = 'claimed', t.claimed_by = $agent, t.claimed_at = $now
RETURN t.id AS id
```

The caller won if and only if the result set is non-empty. The loser's `MATCH`
finds nothing, because by the time its write is dequeued the status is no longer
`open`.

Claim the next open task assigned to me:

```cypher
MATCH (t:Task)-[:ASSIGNED_TO]->(:Agent {name: $agent})
WHERE t.status = 'open'
WITH t ORDER BY t.created_at, t.id LIMIT 1
SET t.status = 'claimed', t.claimed_by = $worker, t.claimed_at = $now
RETURN t.id AS id
```

This is only correct because `WITH ... LIMIT 1` constrains the following `SET`
(verified, T2c) whereas a *trailing* `LIMIT` would not (T2b). Getting this wrong
would claim every open task at once.

---

## 6. Verified behaviour

Probed on 2026-10-15 against `falkordb/falkordb:latest`, graph module v6.0.2, in
Docker on Windows 11 Home. Server config as shipped: `MAX_QUEUED_QUERIES 25`,
`TIMEOUT 1000`, `THREAD_COUNT` default. **14/14 schema checks and 5/5 race checks
passed.**

| # | Claim | Result |
|---|---|---|
| T1a | Relationship properties store and read back | PASS |
| T1b | After `SET = NULL`, `IS NULL` is true | PASS |
| T1c | `SET = NULL` **removes the key**, does not store a null | PASS — `properties(r)` no longer lists `valid_to` |
| T2a | Trailing `LIMIT` does **not** constrain `CREATE` | PASS — created 3, returned 1 |
| T2b | Trailing `LIMIT` does **not** constrain `SET` | PASS — set 3, returned 1 |
| T2c | `WITH ... LIMIT 1` **does** constrain `SET` | PASS — set exactly 1 |
| T3a | Unreferenced edge alias is matched existentially | PASS — `count(b)` returned 1 for 2 parallel edges |
| T3b | `count(e)` sees both parallel edges | PASS — returned 2 |
| T3c | `WHERE ID(e) >= 0` restores correct counting | PASS — returned 2 |
| T3d | Current-only filter picks exactly one of two versions | PASS |
| T3e | Current owner resolves to a single row | PASS |
| T4a | Relationship range index can be created | PASS — `CREATE INDEX FOR ()-[r:OWNS]-() ON (r.valid_to)` |
| T4b | `IS NULL` **cannot** use the index | PASS — plan was `All Node Scan` + `Filter` |
| T4c | A range comparison **can** use it | PASS — plan was `Edge By Index Scan` |
| T5a | ISO-8601 strings sort chronologically | PASS |
| T5b | ISO-8601 string range comparison works | PASS |

### Consequences for query writing

1. **Always reference the edge alias** when counting over a versioned
   relationship. Versioning creates parallel edges between the same pair, and an
   unreferenced alias silently under-reports (T3a). `WHERE ID(e) >= 0` is the
   documented workaround; in practice `WHERE e.valid_to IS NULL` also references
   the alias and fixes it as a side effect.
2. **Never scope a write with a trailing `LIMIT`.** Use `WITH ... LIMIT n` before
   the write, or scope it with the `MATCH` pattern (T2a/T2b/T2c).
3. **`WHERE valid_to IS NULL` will never use an index** (T4b). At 30–50 nodes this
   is irrelevant. It is the one real argument for a sentinel `valid_to` of
   `9999-12-31T23:59:59Z`, which would make "current" a range filter and therefore
   indexable. **Deliberately rejected**: absent-means-current is much easier to
   explain, and we are two orders of magnitude away from the graph size where it
   would matter.
4. **A query with no `MATCH` or `CREATE` fails on a graph key that does not exist**
   with `Invalid graph operation on empty key`. Seeding must create something
   before running any bare `UNWIND` or `RETURN`.

### Cypher subset limits found while writing the seed queries

Discovered on 2026-10-15 against v6.0.2 while building `scripts/seed.py`. Each
of these is a thing that looks like valid Cypher and is not.

5. **A variable-length relationship variable is a `Path`, not a `List`.** In
   `MATCH (a)-[d:DEPENDS_ON*1..3]->(b)`, `d` binds to a Path, so `size(d)` and
   `all(r IN d WHERE ...)` both fail with `Type mismatch: expected List but was
   Path`. **Bind the path and use `relationships(path)` instead:**

   ```cypher
   MATCH path = (:Project {name:$from})-[:DEPENDS_ON*1..3]->(t:Project)
   WHERE all(r IN relationships(path) WHERE r.valid_to IS NULL)
   RETURN t.name, length(path) AS hops
   ```

   This matters more than it looks: filtering a multi-hop traversal to
   *currently-valid* edges is the core versioned-graph query, and the obvious
   way to write it does not work. `length(path)` and `nodes(path)` are fine.

6. **`shortestPath()` is only allowed in `WITH` or `RETURN`, never in `MATCH`.**
   `MATCH p = shortestPath(...)` is rejected outright. Match the endpoints
   first, then compute the path:

   ```cypher
   MATCH (a:Project {name:$a}), (b:Project {name:$b})
   WITH shortestPath((a)-[:DEPENDS_ON*]->(b)) AS p
   RETURN [n IN nodes(p) | n.name] AS chain, length(p) AS hops
   ```

   The `explain_path` tool must be written this way.

7. **Existential subqueries do not exist.** `WHERE NOT EXISTS { MATCH ... }` is
   a parse error, and `exists((a)-[:R]->(b))` is rejected with "traversal
   patterns are not allowed as arguments to exists()". Two things do work:
   - `WHERE NOT (p)-[:OWNS]->(j)` — a pattern predicate. **It is version-blind.**
     It cannot see `valid_to`, so it would wrongly exclude a person whose
     ownership was *retired* — exactly the state demo moment 5b creates.
   - `OPTIONAL MATCH` plus a null check — version-aware, and the form to use:

   ```cypher
   MATCH (p:Person)-[w:WORKED_ON]->(j:Project {name:$project})
   WHERE w.valid_to IS NULL
   OPTIONAL MATCH (p)-[o:OWNS]->(j) WHERE o.valid_to IS NULL
   WITH p, o WHERE o IS NULL
   RETURN p.name, p.role
   ```

   `CALL { ... }` subqueries *are* supported, and are the fallback for anything
   these two cannot express.

### Measured concurrency

Not a benchmark — a correctness test with timings attached. Single local Docker
container, 16 Python threads each on its own connection, released simultaneously
from a barrier.

| Scenario | Result |
|---|---|
| 16 clients race for **1** task | Exactly **1** winner. 23.7 ms wall clock. |
| 16 clients drain **50** tasks | **50** claims, **0** duplicates, **0** left open. 57.6 ms wall clock, about 868 claims/sec including each client's final empty poll. |
| 16 clients, read-then-write in **two** queries (control) | **16 of 16** believed they won — lost updates, exactly as predicted. |

The third row is the point: it is the evidence that the single-query
compare-and-set is necessary rather than merely tidy.

Caveat worth carrying: recent FalkorDB GitHub issues describe rework of the write
path, including a report that schema-mutating commands can bypass write
serialization. Those are issues and PRs, not documentation, and we have not
confirmed which builds they affect. Our measured result on v6.0.2 stands on its
own; do not generalise it to other versions.

---

## 7. Demo coverage audit

Mapping all 11 node types and 20 relationship types against the eight demo moments
in `PROJECT_CONTEXT.md` section 7 turned up schema that nothing shows. Recorded
here so the gaps are decided, not forgotten.

| Gap | Status |
|---|---|
| **Relationship versioning is never demonstrated.** Moment 5 retired a `Decision` — a node. Nothing retired `OWNS`, `DEPENDS_ON`, `WORKED_ON`, `USES`, `AFFECTS` or `APPLIES_TO`, so the most interesting part of the schema had no screen time. | **Closed Oct 15, 2026** by new demo moment **5b**: ownership of the Ledger Migration project transfers from Lee Park to Dana Ruiz, closing the old `OWNS` edge and opening a new one. Note the transfer targets a `Project`, not the Ledger Service `System` — `OWNS` targets `Project` only. |
| **`Insight` and `DERIVED_FROM` have no moment at all.** Consolidation is a required behaviour and a build step, but section 7 never showed it. | **Closed Oct 15, 2026** by new demo moment **5c**: several separate Ledger Migration slips consolidate into one standing insight, with the source episodes shown. |
| **The informal-expert fact is stranded.** The `OWNS` vs `WORKED_ON` gap is planted in section 6 but no moment asked the question that reveals it. | **Closed Oct 15, 2026** by moment 5b: the transfer makes Dana the answer to "who owns Ledger Migration?", and she is already the answer to "who worked on it?". |
| **`WORKED_ON`'s main consumer is the first planned cut** (the centrality algorithm in moment 8). | **Closed Oct 15, 2026.** Moment 5b gives it a second consumer that survives cutting moment 8. |
| **`(Person)-[:MADE]->(Decision)` has no moment.** In every demo beat the decision reaches the graph through the user talking to the Planner, so the agent's `PERFORMED` edge carries the attribution and no moment asks "which person decided this?". | **Open.** Kept because a company graph that cannot say who decided something is odd, and because it costs one edge at seed time. Cut it if moment 5 does not grow a human decision-maker. |
| **Decorative properties**: `Person.role` is displayed but never queried — it would become load-bearing if the taught rule read "notify the finance lead" rather than "notify Priya", which is a better rule anyway. `Session.user` has one value per tenant in the demo. | **Accepted.** Both cost nothing to keep. |
| `Ticket.status` | **Cut.** Tickets are created and never updated in the demo. Removed from the catalogue above. |
| **An agent's reads left no trace.** Every mutating tool wrote an `Event`, so what an agent changed was explainable. What it *read* — the traversals that actually decided the answer — existed only in the UI's session state and vanished on reload. The one line of the brief the build did not honour through the graph. | **Closed Oct 16, 2026** by the `Lookup` node and the `READ` / `RETURNED` edges, plus the `explain_answer` tool, which lets an agent recover its own reasoning in a later session. |

## 8. Known weak spots

- Entity matching is by name and alias. This works only because the data is
  synthetic and the names are controlled. A real deployment needs entity
  resolution.
- Consolidation quality depends on LLM summarisation and needs inspection.
- No confidence scores on facts. Add a property if a demo moment needs one.
- Edge-level provenance is an id join, not a traversal (section 2).
- The "thousands of graphs per instance" claim is unverified; we use two.
- **Graph algorithms cannot see validity windows.** `algo.betweenness` walks
  structure, and retiring a fact sets `valid_to` rather than deleting the
  edge, so a retired `DEPENDS_ON` still contributes to the score. Every other
  read filters on `valid_to IS NULL`; this one structurally cannot. Verified
  on Oct 15, 2026: after retiring both dependencies into Spring Launch,
  `dependents_of` correctly returned nothing while betweenness still ranked
  it first. `critical_projects` therefore returns the count of retired
  dependency edges alongside the ranking — zero means the scores are exactly
  current. Nothing in the demo retires a `DEPENDS_ON`, so it stays zero in
  practice. The alternatives considered and rejected: maintaining a parallel
  "current only" edge type (denormalisation that can drift), and computing
  centrality in Python (which would stop it being a graph algorithm).
- **Citations are resolved by an unlabelled scan.** `record_lookup` matches
  every harvested key against `n.name` or `n.id` with no label to narrow on,
  so it cannot use an index and scans once per key. At demo scale (tens to
  hundreds of nodes) this is free; on a large graph it would need the
  candidate labels passed in. The match also inherits the same name-collision
  caveat as the rest of the system.
- Betweenness answers *what*, never *who*: `Person` has no incoming edges in
  this schema, so a person is never an intermediate node and always scores 0.

## 9. Indexes

Created on a graph's **first write** (`MemoryStore.ensure_indexes`, called
from `start_session`), and by the seeder when it rebuilds a demo graph.

Until 18 Oct they were created *only* by the seeder, which meant the two demo
graphs had all of them and **every workspace created by conversation had
none** — so the graphs actually in use were the unindexed ones. That is the
same blind spot as the test fixtures in docs/TESTING.md section 1: the seeded
path was healthy and the unseeded path was never looked at.

Two details, both verified rather than assumed:

- **FalkorDB has no `CREATE INDEX IF NOT EXISTS`.** It is a parse error, not
  a no-op. A repeat raises `Attribute 'x' is already indexed`, and only that
  one message is swallowed.
- **Indexing must not happen in `__init__`.** `CREATE INDEX` is a write, and
  a write brings the graph's Redis key into being. Indexing at construction
  would mean a new workspace was never again read before it existed, quietly
  retiring the empty-key path that `test_unseeded.py` exists to guard.

- **Nodes**: `Event.id`, `Event.ts`, `Session.id`, `Task.id`, `Task.status`,
  `Ticket.id`, `Person.name`, `Project.name`, `System.name`, `Decision.id`,
  `Procedure.id`, `Insight.id`, `Lookup.id`, `Topic.name`
- **Relationships**: none yet. The syntax is verified to work (T4a), but with
  `IS NULL` unable to use an index (T4b) and a graph this small, there is nothing
  to gain. Add one only when a query measurably needs it.
