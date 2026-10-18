# The Cypher this project depends on

Every query below is live in the code, with the file it comes from. They are
grouped by the job they do, and each one says *why* it is written that way —
several are shaped by FalkorDB behaviour that is not obvious, and the wrong
shape fails silently rather than erroring.

Companion documents: [`SCHEMA.md`](SCHEMA.md) for the graph model and the
verification behind it, [`AI-DISCLOSURE.md`](AI-DISCLOSURE.md) for how this
was built.

Running convention: node labels and relationship types are interpolated into
query strings from fixed tables in [`src/memory/schema.py`](../src/memory/schema.py),
never from caller input, because Cypher cannot parameterise either. Every
*value* travels as a query parameter.

---

## 1. Reading what is currently true

The single most-used pattern in the system. A fact is current when it has no
end date, and FalkorDB cannot store `null` as a property value, so "no end
date" means the key is absent.

```cypher
MATCH (p:Person)-[o:OWNS]->(j:Project {name: $project})
WHERE o.valid_to IS NULL
RETURN p.name, o.valid_from
```

Two things are load-bearing here:

- `WHERE o.valid_to IS NULL` is the whole versioning model. Drop it and you
  get every owner the project has ever had.
- The edge alias `o` is **referenced** in the `WHERE`. This matters: FalkorDB
  matches an unreferenced relationship existentially, so a pattern whose edge
  alias appears nowhere else reports only that *at least one* edge exists.
  Versioning creates parallel edges between the same pair, so an unreferenced
  alias silently under-reports. Referencing it in the `WHERE` fixes that as a
  side effect; `WHERE ID(o) >= 0` is the documented workaround when there is
  no other filter.

*Source: `MemoryStore.current_facts`, `src/memory/store.py`.*

## 2. Retiring a fact without deleting it

```cypher
MATCH (a:Person {name: $from_name})-[r:OWNS]->(b:Project {name: $to_name})
WHERE r.valid_to IS NULL
SET r.valid_to = $now, r.retired_by = $eid
RETURN count(r)
```

The `WHERE` restricts the write to the currently-open edge, so re-running it
is harmless and a retired edge is never re-stamped. Scoping comes from the
`MATCH` pattern and never from `LIMIT`: in FalkorDB, `CREATE`, `SET`,
`DELETE` and `MERGE` all run to completion **before** a trailing `LIMIT`
applies, so `... SET ... RETURN r LIMIT 1` would update every match and
return one.

The old edge stays in the graph. That is what makes history queryable.

*Source: `MemoryStore.retire_fact`.*

## 3. Claiming a task atomically

The coordination primitive. FalkorDB serialises writes per graph and each
write query is atomic, but it has **no multi-query transactions** — so the
claim has to be one query that both tests and sets the status.

```cypher
MATCH (t:Task)-[:ASSIGNED_TO]->(:Agent {name: $agent})
WHERE t.status = 'open'
WITH t ORDER BY t.created_at, t.id LIMIT 1
SET t.status = 'claimed', t.claimed_by = $worker, t.claimed_at = $now
RETURN t.id AS id
```

The caller won if and only if the result set is non-empty; a loser's `MATCH`
finds nothing because the status is no longer `open` by the time its write is
dequeued.

`WITH t ... LIMIT 1` **before** the `SET` is essential and is the exception to
the rule in section 2: a `LIMIT` inside a `WITH` projection does constrain the
following write, while a trailing one does not. Both behaviours were tested
directly. Get this wrong and one call claims every open task at once.

Measured: 16 concurrent clients draining 100 tasks produced 100 claims and
zero double-claims. The same logic written as read-then-write across two
queries had **16 of 16** clients believe they had won.

*Source: `MemoryStore.claim_task`; measured by `scripts/demo_concurrency.py`.*

## 4. Multi-hop traversal over current edges only

"What does Billing v2 sit behind?" — the query behind the blocked-release
moment.

```cypher
MATCH path = (:Project {name: $project})-[:DEPENDS_ON*1..4]->(t:Project)
WHERE all(r IN relationships(path) WHERE r.valid_to IS NULL)
RETURN t.name, length(path) AS hops
ORDER BY hops, t.name
```

The obvious way to write this does not work. Binding the variable-length
relationship — `-[d:DEPENDS_ON*1..4]->` — gives a **Path**, not a List, so
`all(r IN d ...)` and `size(d)` both fail with a type error. You must bind the
path and go through `relationships(path)`. `length(path)` and `nodes(path)`
are fine.

This is the core versioned-graph query: filtering a multi-hop walk to
currently-valid edges. It is worth knowing that the natural phrasing is the
broken one.

The reverse direction answers a different question — "what breaks if this
slips?" — and is the same query with the arrow turned around
(`MemoryStore.dependents_of`).

*Source: `MemoryStore.depends_on_chain`.*

## 5. Project state, derived rather than stored

A `Project` deliberately has **no `status` property**. Its state is the set of
decisions currently in force against it.

```cypher
MATCH (d:Decision)-[a:AFFECTS]->(p:Project {name: $project})
WHERE d.valid_to IS NULL AND a.valid_to IS NULL
RETURN d.id, d.title, d.reason, d.valid_from, p.name
ORDER BY d.valid_from DESC
```

Both the decision *and* the edge carry validity, and both are checked: a
decision can stay in force while its scope narrows.

The reason this is better than a status column is that state can never drift
from its justification. A stored status can be stale or unexplained; a derived
one arrives with its reason attached, and retiring the decision updates the
project's state for free.

*Source: `MemoryStore.current_decisions`.*

## 6. Supersession: what replaced what

`valid_to` says a decision stopped being current. `SUPERSEDES` says what
replaced it. Both are needed — the first makes "what is true now" a filter,
the second makes "walk me back" a traversal.

```cypher
MATCH (old:Decision {id: $old}), (new:Decision {id: $new})
WHERE old.valid_to IS NULL
SET old.valid_to = $now
CREATE (new)-[:SUPERSEDES]->(old)
```

```cypher
MATCH path = (d:Decision {id: $did})-[:SUPERSEDES*0..10]->(older:Decision)
RETURN older.id, older.title, older.valid_from, older.valid_to, length(path)
ORDER BY length(path)
```

`*0..10` includes the decision itself at distance 0, so one query returns the
current decision and its whole ancestry in order.

*Source: `MemoryStore.record_decision`, `MemoryStore.decision_history`.*

## 7. Provenance: why do we believe this?

The explainability spine. One query walks from a decision to the agent that
recorded it.

```cypher
MATCH (dec:Decision {id: $did})-[:RECORDED_IN]->(e:Event)<-[:HAS_EVENT]-(s:Session)
MATCH (ag:Agent)-[:PERFORMED]->(e)
MATCH (who:Person)-[:MADE]->(dec)
RETURN dec.title, who.name, e.id, e.ts, s.id, ag.name
```

Versioned **nodes** get a traversable provenance edge like this. Versioned
**relationships** cannot: Cypher has no edge-to-edge relationship, so an edge
carries `source_event` as a scalar id and the event is recovered by a second
`MATCH` on `Event.id`. That asymmetry is deliberate — see `SCHEMA.md` §2.

Why an agent *answered* as it did is a separate trail, through what it read:
section 16.

*Source: tests and `MemoryStore.explain_path`.*

## 8. The informal expert — version-aware negation

"Who works on Ledger Migration but does not currently own it?"

```cypher
MATCH (p:Person)-[w:WORKED_ON]->(j:Project {name: $project})
WHERE w.valid_to IS NULL
OPTIONAL MATCH (p)-[o:OWNS]->(j) WHERE o.valid_to IS NULL
WITH p, o WHERE o IS NULL
RETURN p.name, p.role
ORDER BY p.name
```

Existential subqueries do not exist in FalkorDB: `WHERE NOT EXISTS { MATCH
... }` is a parse error, and `exists((p)-[:OWNS]->(j))` is rejected because
traversal patterns are not allowed as arguments.

The pattern predicate `WHERE NOT (p)-[:OWNS]->(j)` *is* accepted — and is
**wrong here**, because it cannot see `valid_to` and would exclude someone
whose ownership was *retired*. That is exactly the state the ownership-transfer
moment creates. `OPTIONAL MATCH` plus a null check is version-aware and is the
form to use. `CALL { ... }` subqueries also work when neither fits.

*Source: `tests/test_memory_tools.py`, `MemoryStore` procedural queries.*

## 9. A rule firing without being named

Procedural memory. A rule is taught about *Ledger Service*; it must fire for
*Billing v2*, a project the rule never mentions, because Billing v2 uses that
system.

```cypher
MATCH (p:Procedure)-[s:APPLIES_TO]->(sys:System)<-[u:USES]-(proj:Project {name: $name})
WHERE p.valid_to IS NULL AND s.valid_to IS NULL AND u.valid_to IS NULL
RETURN p.id, p.name, p.trigger, p.action, 'via system ' + sys.name
```

Three validity checks, because all three edges can be retired independently:
the rule, the rule's scope, and the project's use of the system. Retire any one
and the rule correctly stops firing.

*Source: `MemoryStore.get_applicable_procedures`.*

## 10. Choosing what to consolidate

Consolidation is graph-driven. The graph picks the work; the model only writes
prose.

```cypher
MATCH (e:Event)-[:ABOUT]->(n)
WHERE NOT (e)<-[:DERIVED_FROM]-(:Insight) AND e.kind <> 'consolidation'
WITH n, count(e) AS episodes, collect(e.id) AS event_ids
WHERE episodes >= $min_events
RETURN labels(n)[0], coalesce(n.name, n.title, n.id), episodes, event_ids
ORDER BY episodes DESC LIMIT $limit
```

Three things are doing real work:

- Grouping is a traversal, not a scan.
- `NOT (e)<-[:DERIVED_FROM]-(:Insight)` is how "already absorbed" is
  represented. There is no `consolidated` flag anywhere, so the marker and the
  audit trail cannot disagree.
- `e.kind <> 'consolidation'` stops the feedback loop: writing an insight
  produces an event `ABOUT` the same entity, and without this exclusion each
  consolidation would feed the next one forever.

Writing the insight links it to every source in one query:

```cypher
MATCH (i:Insight {id: $iid})
MATCH (e:Event) WHERE e.id IN $ids
CREATE (i)-[:DERIVED_FROM]->(e)
```

The caller checks `relationships_created` against the number of ids, so an
insight is never written with dangling provenance.

*Source: `MemoryStore.consolidation_candidates`, `MemoryStore.record_insight`.*

## 11. Explaining how two things connect

```cypher
MATCH (a:Project {name: $from_name}), (b:Project {name: $to_name})
WITH shortestPath((a)-[*..6]->(b)) AS p
WHERE p IS NOT NULL
RETURN [n IN nodes(p) | coalesce(n.name, n.title, n.id)] AS chain,
       [r IN relationships(p) | type(r)] AS steps,
       length(p) AS hops
```

`shortestPath()` is **only accepted in `WITH` or `RETURN`** in FalkorDB —
`MATCH p = shortestPath(...)` is rejected outright. Match the endpoints first,
then compute the path.

Returns `["Billing v2", "Spring Launch", "Ledger Migration"]` with
`["DEPENDS_ON", "DEPENDS_ON"]`, which is what turns "this is blocked" into
"this is blocked, and here is the chain".

*Source: `MemoryStore.explain_path`.*

## 12. The graph algorithm

One algorithm, per the project's scope rule. Betweenness centrality over the
dependency graph answers "what is the single point of failure?"

```cypher
CALL algo.betweenness({nodeLabels: ['Project'],
                       relationshipTypes: ['DEPENDS_ON']})
YIELD node, score
WHERE score > 0
RETURN node.name, score
ORDER BY score DESC, node.name
LIMIT $limit
```

Scoping matters: run unscoped it also ranks Events and Sessions, which is
noise. On the seeded graph it returns **Spring Launch, 2.0** — the project
everything else sits behind.

Two limitations, both reported rather than hidden:

- **It answers *what*, never *who*.** `OWNS` and `WORKED_ON` point from a
  person into a project, so a `Person` has no incoming edges, is never an
  intermediate node, and scores exactly 0.
- **It cannot see validity windows.** The algorithm walks structure, and
  retiring a fact sets `valid_to` rather than deleting the edge, so a retired
  `DEPENDS_ON` still contributes. Every other read filters on validity; this
  one structurally cannot. `critical_projects` therefore returns the count of
  retired dependency edges alongside the ranking — zero means the ranking is
  exactly current.

*Source: `MemoryStore.critical_projects`.*

## 13. Remembering something nobody modelled

The open layer. A label and a relationship type chosen by the model at write
time become a real label and a real relationship type:

```cypher
CREATE (n:Appointment {name: $name, valid_from: $now})
```

```cypher
MATCH (s) WHERE ID(s) = $sid
MATCH (o) WHERE ID(o) = $oid
CREATE (s)-[:HAS_APPOINTMENT {valid_from: $valid_from,
                              source_event: $source_event,
                              when: $when}]->(o)
```

Both the label and the relationship type are interpolated, because Cypher
cannot parameterise either. They are therefore **validated, not escaped** —
see `SCHEMA.md` §1 for the patterns and the reserved list. Values still travel
as parameters.

Entity reuse happens first, and deliberately ignores the type the model
proposed:

```cypher
MATCH (n) WHERE n.name = $name RETURN ID(n), labels(n)[0] LIMIT 1
```

If "Lee Park" already exists as a `Person`, a fact about `Contact` "Lee Park"
attaches to the existing Person. One node with many facts beats two nodes that
never meet, and the type the model guessed is the least reliable part of its
output.

*Source: `MemoryStore.remember_fact`, `MemoryStore._resolve_entity`.*

## 14. "What else is on that day?"

The query that makes a memory able to notice a double-booking:

```cypher
MATCH (s)-[r]->(o)
WHERE ID(r) >= 0 AND r.valid_to IS NULL AND r.when STARTS WITH $date
RETURN coalesce(s.name, s.title, s.id), type(r),
       coalesce(o.name, o.title, o.id), r.when
ORDER BY r.when
```

Untyped on both ends and untyped on the edge: it does not care what kind of
thing is on the calendar, which is the point. `r.valid_to IS NULL` means a
cancelled commitment stops colliding without being deleted. `ID(r) >= 0`
is the usual guard against existential edge matching.

`coalesce(...)` across `name` / `title` / `id` is how one query reads nodes
from the open layer and the behavioural core together — an `Appointment` has
a name, a `Decision` has a title, an `Event` has only an id.

*Source: `MemoryStore.what_is_on`.*

## 15. Everything about one thing, whatever it is

```cypher
MATCH (n {name:$name})-[r]->(m) WHERE r.valid_to IS NULL
RETURN type(r), labels(m)[0], coalesce(m.name, m.title, m.id),
       r.when, r.valid_from
```

Takes no label. The caller asks about a name and finds out what it turned out
to be, which is what an open schema requires: by the time you want to recall
something, you should not have to remember how it was classified.

*Source: `MemoryStore.recall_entity`.*

## 16. Why did you say that? The reasoning trace

Section 7 explains why we *believe* a fact. This explains why an agent
*answered* as it did, which is a different question: the answer turned on
what the agent went and read.

Each read becomes a `Lookup`, and the nodes it surfaced become `RETURNED`
edges. Both halves are one query:

```cypher
CREATE (l:Lookup {id: $lid, tool: $tool, arguments: $args,
                  ts: $ts, agent: $agent, session: $sid})
WITH l UNWIND $keys AS k
MATCH (n) WHERE (n.name = k OR n.id = k) AND NOT n:Lookup
CREATE (l)-[:RETURNED]->(n)
```

Three things about this shape:

**The `CREATE` survives an empty `$keys`.** `UNWIND []` yields no rows, so
nothing after it runs — but the `Lookup` was already created by the clause
before the `WITH`. Verified against FalkorDB v6.0.2, and load-bearing: a read
that found nothing is the evidence for an answer like *"no, nothing clashes"*,
so it has to be recorded with zero citations rather than not recorded at all.

**`$keys` are resolved, not trusted.** They are names and ids harvested from
the tool's arguments and result. The `MATCH` is what decides whether a
citation exists, so the trace can never claim a node the graph does not hold.
The cost is an unlabelled scan per key — free at demo scale, wrong on a large
graph.

**`NOT n:Lookup`** stops a trace citing another trace.

The answer is then pointed at the reads behind it:

```cypher
MATCH (e:Event {id: $eid}) MATCH (l:Lookup) WHERE l.id IN $lids
CREATE (e)-[:READ]->(l)
```

Finding which answer to explain is itself a traversal, and this is the part
worth noticing — the subject is matched through the **citation edges**, not
by searching the text of what was said:

```cypher
MATCH (e:Event)-[:READ]->(:Lookup)-[:RETURNED]->(x)
WHERE x.name = $about OR x.id = $about
RETURN DISTINCT e.id, e.ts, e.summary, e.speaker
ORDER BY e.ts DESC, e.id DESC LIMIT 1
```

So "why did you block the billing release?" finds the right answer even when
the answer never used the words that were asked about — it qualifies because
one of its reads returned that node.

Then the trace itself:

```cypher
MATCH (e:Event {id: $eid})-[:READ]->(l:Lookup)
OPTIONAL MATCH (l)-[:RETURNED]->(n)
RETURN l.tool, l.arguments,
       collect(labels(n)[0] + ':' + coalesce(n.name, n.title, n.id))
ORDER BY l.ts, l.id
```

`OPTIONAL MATCH` plus `collect` is what makes the empty case readable:
FalkorDB drops the nulls, so a read that returned nothing comes back as `[]`
rather than as a missing row.

**What the trace deliberately stays out of.** Three existing queries would
otherwise fill up with citations:

- `recall_related` needs no change at all. Its fact queries require
  `r.valid_from IS NOT NULL`, and a citation is episodic so it carries no
  validity window — it is invisible to them for free.
- `consolidation_candidates` matches `(e:Event)-[:ABOUT]->(n)`, and a
  `Lookup` is not an `Event`, so lookups are never summarised as episodes.
- `snapshot` had to be told, and now excludes them explicitly:

```cypher
MATCH (n) WHERE NOT n:Lookup RETURN ...
MATCH (a)-[r]->(b)
WHERE ID(r) >= 0 AND NOT type(r) IN ['READ', 'RETURNED'] RETURN ...
```

That panel answers "what does the agent now believe that it did not before",
and a lookup is not a belief. Left in, one turn would bury a single real
change under a dozen citations.

*Source: `MemoryStore.record_lookup`, `link_reasoning`, `find_answer`,
`explain_answer`, and the capture point in `Toolbox.dispatch`.*

## 17. Finding a past conversation by subject

**What it answers:** "let's pick up the lift logic from where we stopped."

Until 18 Oct the only route back into past work was `recent_conversations`,
which titles each session with its **opening line**. For a long design
discussion that is the least representative sentence in it — the first
message is the naive version of the problem and the decisions are in turns
seven to nine. If the chat opened with "hey quick question", nothing matched
"lift logic" at all.

Worse, a turn had no edges to anything. Measured across the three workspaces
in real use: **47 of 47 utterances had no outgoing relationship.** Every word
stored, none of it reachable.

### Writing the link

```cypher
MATCH (e:Event {id: $eid})
MERGE (t:Topic {name: $name})
ON CREATE SET t.first_seen = $now
MERGE (e)-[:ON_TOPIC]->(t)
```

`MERGE ... ON CREATE SET` is verified on v6.0.2: `first_seen` keeps the value
from the first write and is not overwritten on later ones. Both `MERGE`s
matter — the second stops a turn being linked to the same topic twice when a
curation is retried.

### The index injected into every prompt

```cypher
MATCH (t:Topic)<-[:ON_TOPIC]-(e:Event)
WHERE e.speaker IS NOT NULL
WITH t, count(e) AS turns, max(e.ts) AS last, max(e.id) AS seq
RETURN t.name, turns, last
ORDER BY last DESC, seq DESC LIMIT $limit
```

Three things are load-bearing here:

- **`e.speaker IS NOT NULL`** keeps the count to actual utterances. An
  agent's internal working record is not a turn of conversation.
- **`max(e.id)` as the tie-break.** Timestamps are second-precision, so two
  subjects touched in one turn tie on `last` and would otherwise order
  alphabetically — in an index whose whole claim is "most recently
  discussed". Ids carry a microsecond prefix and sort in time order. This is
  the same `ORDER BY (ts, id)` convention used everywhere else in the store,
  and a test caught it being missing here.
- **`max()` over ISO strings works**, verified — the fixed-width UTC format
  sorts lexicographically in chronological order.

What goes into the prompt is the *index*, never the content. Injecting past
turns themselves would re-open the bug in docs/TESTING.md section 3 attempt
3: an agent reading back its own prose and repeating a fact retired minutes
earlier, because a sentence has no `valid_to`. A one-line index asserts
nothing.

### Reading one subject back

```cypher
MATCH (t:Topic {name: $name})<-[:ON_TOPIC]-(e:Event)
MATCH (s:Session)-[:HAS_EVENT]->(e)
WHERE e.speaker IS NOT NULL AND s.hidden IS NULL
RETURN s.id, e.ts, e.speaker, e.summary
ORDER BY e.ts, e.id LIMIT $limit
```

`s.hidden IS NULL` is not optional. Removing a conversation has to close
every way back into it, or topics become a hole in the removal.

Both sides come back whole, deliberately. Ambient recall leaves an agent's
prose out because it is unreliable as fact; a conversation asked for **by
name** is being resumed, and there the replies are the thing being resumed.
Same resolution as `read_conversation` in TESTING.md section 3c.

### Resolving a name the model paraphrased

`CONTAINS` in FalkorDB is a plain substring test, so `"lift control logic"
CONTAINS "lift logic"` is **false** — verified. Since paraphrasing the topic
back is exactly what a model does, resolution is exact match, then substring
either direction, then best word overlap, scored in Python. Topic counts are
in the tens, so this costs nothing and forgives far more than Cypher can.

## 18. Linking a turn to what it mentioned

```cypher
MATCH (e:Event {id: $eid})
MATCH (n) WHERE n.name = $name
  AND NOT n:Event AND NOT n:Session AND NOT n:Lookup
  AND NOT n:Agent AND NOT n:Topic AND NOT n:Procedure
  AND NOT n:Insight AND NOT n:Task AND NOT n:Ticket
MERGE (e)-[:ABOUT]->(n)
```

Three decisions worth defending:

- **`ABOUT` is reused rather than a new `MENTIONS` type invented.** The same
  argument this schema already makes for `Insight` sharing it: "what is this
  node concerned with" is the same question either way. A second edge type
  would be drift.
- **No label is named anywhere**, so nothing is interpolated into the query.
  The name travels as a parameter and the join is on internal id — which is
  how this reaches open-layer entities such as `Appointment` or `Language`
  that `ENTITY_LABELS` deliberately does not list.
- **It only links to nodes that already exist.** Mentions connect a turn to
  what is known; facts create what is new. Without that split every noun a
  model emitted would mint a node and the graph would fill with scenery.

The exclusion list is built from `NON_ENTITY_LABELS` rather than written out,
so its three call sites cannot drift apart. It covers plumbing, the topic
index itself, and behavioural nodes whose `name` is an internal handle — a
`Procedure` called "comment every line" was being offered to the curator as
an entity to mention until that was fixed.

## 19. "What did we decide?"

**What it answers:** the question a transcript is worst at.

Formal decisions become `Decision` nodes. But the moments that actually
settle a piece of work — "go with a PriorityQueue", "scrap that, use an
ArrayDeque" — never did: the Planner only records work decisions, and a
preference is explicitly not one. Those settlements lived in prose, and
prose cannot be retired (TESTING.md section 3).

The obvious design is a keyword search over what was said. It does not
work, and the reason is worth stating because the failure is quiet: after
one turn chooses a PriorityQueue and a later turn switches to an
ArrayDeque, searching "queue" matches both and nothing says which holds.

So the curator **marks** the turn instead:

```cypher
MATCH (e:Event {id: $eid}) SET e.settled = $text
MERGE (t:Topic {name: $name})
MERGE (e)-[:SETTLES]->(t)
```

and the question becomes an ordering, not a guess:

```cypher
MATCH (e:Event)-[:SETTLES]->(t:Topic)
MATCH (s:Session)-[:HAS_EVENT]->(e)
WHERE e.settled IS NOT NULL AND s.hidden IS NULL AND t.name = $topic
RETURN e.id, e.ts, t.name, e.settled, e.speaker, s.id
ORDER BY e.ts DESC, e.id DESC LIMIT $limit
```

No `valid_to` is needed. A later settlement supersedes an earlier one by
being later, and `(ts, id)` is exact because ids carry a microsecond
prefix — the same property that makes `recall_recent` ordered.

**The first result is flagged `current`, not merely first.** Asked what had
been decided, a model read a correctly newest-first list and narrated it
oldest-first. It reached the right answer that time, which is not the same
as being reliable, and ordering is a convention a reader can miss.

## 20. Full-text search over what was said

```cypher
CALL db.idx.fulltext.queryNodes('Event', $term) YIELD node AS e
MATCH (s:Session)-[:HAS_EVENT]->(e)
WHERE s.hidden IS NULL AND e.speaker IS NOT NULL
RETURN e.id, e.ts, e.speaker, e.summary, e.settled, s.id
ORDER BY e.ts DESC, e.id DESC LIMIT $limit
```

Searching prose is the one job a transcript is genuinely better at than a
graph, and FalkorDB has a real full-text index for it — so it is a database
query rather than a Python scan over every row. The DDL is
`CREATE FULLTEXT INDEX FOR (e:Event) ON (e.summary)`; the
`db.idx.fulltext.createNodeIndex` form in the docs is rejected.

Two things to know. It matches **whole words**: `queue` does not find
`PriorityQueue`. And the parameter here is `$term`, not `$q`, because
`MemoryStore._read`'s own first argument is named `q` and passing `q=`
collides with it — caught by a `TypeError` on the first run.

## 21. Indexes

```cypher
CREATE INDEX FOR (n:Event) ON (n.id)
```

Created at seed time for `Event.id`, `Event.ts`, `Session.id`, `Task.id`,
`Task.status`, `Ticket.id`, `Person.name`, `Project.name`, `System.name`,
`Decision.id`, `Procedure.id`, `Insight.id`. `CALL db.indexes()` reports one
row per label, so twelve property indexes show as ten rows.

Relationship indexes work too — `CREATE INDEX FOR ()-[r:OWNS]-() ON (r.valid_to)` —
but none are created, deliberately: `WHERE valid_to IS NULL` **cannot use an
index**, because null is never stored and so never appears in one. Verified
with `GRAPH.EXPLAIN`: the `IS NULL` form plans as `All Node Scan` + `Filter`,
while a range comparison plans as `Edge By Index Scan`.

That is the one real argument for a sentinel end-date instead of an absent
one, and it was considered and rejected: absent-means-current is far easier to
explain, and this graph is two orders of magnitude away from the size where it
would matter.

*Source: `src/memory/schema.py`, `scripts/seed.py`.*

---

## FalkorDB behaviours worth knowing before writing more

All verified by running them against FalkorDB graph module v6.0.2, not read
from documentation.

| Behaviour | Consequence |
|---|---|
| `null` cannot be stored as a property value | `SET x.p = NULL` removes the key; "absent" is how we say "current" |
| Trailing `LIMIT` does not constrain `CREATE`/`SET`/`DELETE`/`MERGE` | Scope writes with the `MATCH` pattern, or `WITH ... LIMIT` before the write |
| `WITH ... LIMIT n` **does** constrain a following write | This is what makes the atomic claim correct |
| An unreferenced edge alias is matched existentially | Always reference the alias when counting over versioned edges |
| A variable-length relationship variable is a `Path`, not a `List` | Use `relationships(path)` for `all()` / `none()` / `size()` |
| `shortestPath()` is rejected in `MATCH` | Only valid in `WITH` or `RETURN` |
| `EXISTS { ... }` and `exists((a)-[:R]->(b))` are unsupported | Use a pattern predicate (version-blind) or `OPTIONAL MATCH` (version-aware) |
| Indexes do not serve `<>` or `IS NULL` filters | `valid_to IS NULL` always scans; fine at this size |
| A query with no `MATCH`/`CREATE` fails on a graph that does not exist | `Invalid graph operation on empty key` — materialise the graph first |
| Writes serialise per graph; each write query is atomic; no multi-query transactions | Compare-and-set must be a single query |
