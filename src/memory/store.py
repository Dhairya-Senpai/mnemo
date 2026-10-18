"""The memory tools: every read and write the agents are allowed to make.

One class, one method per tool. Each method is plain code with its Cypher
visible beside it, and each is testable with no LLM involved -- that is the
point of this layer. The agents (and later the MCP server) call these methods
and nothing else, so every memory operation in the system goes through the
graph.

Design notes worth knowing before reading the methods:

* **Every mutating tool writes an Event first**, then points the resulting
  fact's `source_event` at it. Provenance is an invariant enforced here rather
  than left to discipline.
* **Reads use ro_query, writes use query.** FalkorDB executes read-only
  queries in parallel and serializes writes per graph, so this split is what
  makes concurrent reads actually concurrent.
* **Timestamps are second-precision ISO-8601 UTC strings** (docs/SCHEMA.md
  section 2). Ordering uses `(ts, id)` because two events can share a second.
  Do not add fractional seconds: "...00.123Z" sorts BEFORE "...00Z"
  lexicographically, which would silently break ordering against seeded data.
* **A fact is current when `valid_to` is absent.** Nothing is ever deleted or
  edited in place; retiring sets `valid_to` and leaves the old edge readable.
"""

import json
import time
import uuid
from datetime import datetime, timezone

from falkordb import FalkorDB
from redis.exceptions import ResponseError

from .schema import (
    CAPABILITIES,
    check_any_entity_label,
    DEFAULT_AGENTS,
    ENTITY_LABELS,
    EVENT_KINDS,
    FACT_ENDPOINTS,
    FULLTEXT_INDEXES,
    INDEXES,
    PROCEDURE_SCOPES,
    not_entity_clause,
    check_capability,
    check_fact_type,
    check_label,
    clean_label as check_open_label,
    clean_predicate as check_open_predicate,
)


def utc_now():
    """Second-precision ISO-8601 UTC, e.g. 2026-10-15T14:30:00Z."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def list_tenants(host="localhost", port=6379):
    """Every workspace that exists, most recently created first.

    There is no registry of workspaces anywhere in this project, and that is
    the point: a workspace *is* a graph. Isolation is structural, so the set
    of workspaces is simply whatever graphs the database is holding, and a
    new one needs no registration -- it exists as soon as something is
    written to it.

    "Created" is the earliest Session in the graph. Nothing records when a
    graph came into being -- Redis keys carry no creation time -- but the
    first session opened against a workspace is the moment it started
    existing, which is the same thing measured from the inside.
    """
    db = FalkorDB(host=host, port=port)
    names = [n[len("tenant_"):] for n in db.list_graphs()
             if n.startswith("tenant_")]

    def born(name):
        try:
            rows = db.select_graph(f"tenant_{name}").ro_query(
                "MATCH (s:Session) RETURN min(s.started_at)").result_set
            return (rows[0][0] or "") if rows else ""
        except Exception:
            return ""                      # graph with no key yet, or no sessions

    names.sort()                           # alphabetical within a tie
    names.sort(key=born, reverse=True)     # stable: newest first
    return names


def rename_tenant(old, new, host="localhost", port=6379):
    """Rename a workspace, moving the graph rather than copying it.

    FalkorDB has no GRAPH.RENAME. It does not need one: a graph lives under a
    single Redis key, so RENAME moves the whole thing -- nodes, edges and
    indexes -- in one operation. Verified against FalkorDB v6.0.2.
    """
    db = FalkorDB(host=host, port=port)
    if f"tenant_{new}" in db.list_graphs():
        raise ValueError(f"a workspace named {new!r} already exists")
    if f"tenant_{old}" not in db.list_graphs():
        raise LookupError(f"no workspace named {old!r}")
    db.connection.rename(f"tenant_{old}", f"tenant_{new}")


def delete_tenant(name, host="localhost", port=6379):
    """Drop a workspace entirely. This one really does delete."""
    FalkorDB(host=host, port=port).select_graph(f"tenant_{name}").delete()


class MemoryStore:
    """Graph-backed memory for one tenant.

    The tenant is fixed at construction and every query runs against that
    tenant's graph. There is no tenant property on any node and no tenant
    filter in any query, so cross-tenant leakage is not possible by forgetting
    a WHERE clause -- isolation is structural.
    """

    def __init__(self, tenant, host="localhost", port=6379, clock=utc_now):
        self.tenant = tenant
        self.graph_name = f"tenant_{tenant}"
        self._db = FalkorDB(host=host, port=port)
        self._g = self._db.select_graph(self.graph_name)
        self._clock = clock
        # Query counters, so the concurrency demo can report what actually
        # ran instead of estimating it. One store per thread, summed at the end.
        self.reads = 0
        self.writes = 0
        self._indexed = False

    # ----------------------------------------------------------------- plumbing

    def _read(self, q, **params):
        """Run a read-only query, treating a graph that does not exist as empty.

        FalkorDB stores a graph under a Redis key that is created by the
        first write. Until then the key does not exist, and GRAPH.RO_QUERY
        against it fails with "Invalid graph operation on empty key" rather
        than returning no rows.

        That matters because a workspace has to be readable before it is
        writable: the UI lists conversations before anyone has said anything,
        so a brand-new tenant would crash on first open instead of showing an
        empty chat. A graph with nothing in it genuinely answers "nothing",
        so that is what this returns. Only that one error is swallowed --
        anything else is a real failure and still raises.
        """
        self.reads += 1
        try:
            return self._g.ro_query(q, params=params).result_set
        except ResponseError as exc:
            if "empty key" in str(exc).lower():
                return []
            raise

    def _write(self, q, **params):
        self.writes += 1
        return self._g.query(q, params=params)

    @staticmethod
    def _new_id(prefix):
        """Time-ordered id: fixed-width microsecond stamp, then random suffix.

        Timestamps are second-precision, so several events written in the same
        second tie on `ts`. Ordering falls through to `id`, and a random uuid
        would make that order arbitrary -- which in a fast agent run means
        recall_recent returns the turn's events shuffled. A fixed-width hex
        microsecond prefix sorts lexicographically in time order, and the
        random suffix keeps ids unique across concurrent writers.
        """
        micros = int(time.time() * 1_000_000)
        return f"{prefix}-{micros:014x}-{uuid.uuid4().hex[:6]}"

    def now(self):
        return self._clock()

    def ensure_indexes(self):
        """Create this schema's indexes on this graph, if they are missing.

        Indexes used to be created only by `scripts/seed.py`, so the two
        demo graphs had all eleven and every workspace made by conversation
        had none -- the graphs actually being used were the unindexed ones.
        That is the same blind spot as the test fixtures described in
        docs/TESTING.md section 1: the seeded path was healthy and the
        unseeded path was never looked at.

        Deliberately not called from __init__. CREATE INDEX is a write, and
        a write brings the graph's Redis key into being; doing it at
        construction would mean a brand-new workspace was never again read
        before it existed, and the empty-key path that test_unseeded.py
        guards would quietly stop being exercised. First write is the right
        moment, and start_session is the first write any workspace sees.

        FalkorDB has no `CREATE INDEX IF NOT EXISTS` -- verified, it is a
        parse error rather than a no-op -- so a repeat raises "already
        indexed", and only that one message is swallowed.
        """
        if self._indexed:
            return
        for label, prop in INDEXES:
            try:
                self._write(f"CREATE INDEX FOR (n:{label}) ON (n.{prop})")
            except ResponseError as exc:
                if "already indexed" not in str(exc).lower():
                    raise
        for label, prop in FULLTEXT_INDEXES:
            try:
                self._write(
                    f"CREATE FULLTEXT INDEX FOR (n:{label}) ON (n.{prop})")
            except ResponseError as exc:
                if "already indexed" not in str(exc).lower():
                    raise
        self._indexed = True
        # The roster belongs to every workspace, not only seeded ones --
        # the same lesson as the indexes themselves.
        self.ensure_agents()

    # -------------------------------------------------------------- the roster

    def ensure_agents(self):
        """Put the agent roster in the graph, idempotently.

        Routing is a query, not a constant. Until now the Planner knew the
        word "Reviewer" because its prompt said so, which meant a new agent
        needed a prompt edit and a code change. With the roster in the
        graph, adding one is writing a node -- and an `Agent` written by
        hand, with a role and a capability, is routed to without anything
        being recompiled.

        `Agent.role` has existed since the seeder and was read by nothing.
        This is what it was for.
        """
        for name, (role, handles) in DEFAULT_AGENTS.items():
            self._write(
                "MERGE (a:Agent {name:$name}) SET a.role = $role",
                name=name, role=role)
            for cap in handles:
                self._write(
                    "MATCH (a:Agent {name:$name}) "
                    "MERGE (c:Capability {name:$cap}) "
                    "ON CREATE SET c.description = $desc "
                    "MERGE (a)-[:HANDLES]->(c)",
                    name=name, cap=check_capability(cap),
                    desc=CAPABILITIES[cap])

    def agent_roster(self, exclude=("Planner",)):
        """Who can be handed work, and what each one is for.

        Read from the graph so the list the Planner sees is the list that
        exists. Agents with no capability are left out: they can be
        addressed by name but nothing can be routed to them, and offering
        one as a destination would strand the work.
        """
        rows = self._read(
            "MATCH (a:Agent)-[:HANDLES]->(c:Capability) "
            "WITH a, collect(c.name) AS caps "
            "RETURN a.name, a.role, caps ORDER BY a.name")
        return [{"agent": r[0], "role": r[1], "handles": r[2]}
                for r in rows if r[0] not in (exclude or ())]

    # ------------------------------------------------------------------ episodic

    def start_session(self, user, session_id=None, started_at=None):
        """Open an episodic container. Everything else hangs off a session."""
        self.ensure_indexes()
        sid = session_id or self._new_id("sess")
        self._write(
            "CREATE (:Session {id:$id, started_at:$started_at, user:$user})",
            id=sid, started_at=started_at or self.now(), user=user,
        )
        return sid

    def record_event(self, session_id, agent, kind, summary, about=None, ts=None,
                     speaker=None):
        """Write one episodic event and wire it to its session, agent and subjects.

        `about` is a list of (label, name) pairs naming the entities the event
        concerned; those ABOUT edges are what recall_related traverses later.

        `speaker` says who uttered a message: "user" when a person typed it,
        or an agent's name when an agent said it. PERFORMED alone cannot
        carry this -- an agent records the user's message, so it performs an
        event it did not author. Without the distinction a conversation
        cannot be read back out of the graph, which is what lets the UI
        rebuild a transcript from memory rather than from browser state.
        """
        if kind not in EVENT_KINDS:
            raise ValueError(f"unknown event kind {kind!r}; expected one of {EVENT_KINDS}")
        eid = self._new_id("evt")
        # MERGE, not MATCH, on the Agent. With MATCH, a graph whose Agent
        # nodes were never seeded yields no rows, so the CREATE silently
        # does not run: no Event, no history, and no error -- the whole
        # episodic tier quietly becomes a no-op while facts keep being
        # written. An agent is identified by its name alone, so creating
        # one on first use is correct rather than merely convenient.
        res = self._write(
            "MATCH (s:Session {id:$sid}) "
            "MERGE (a:Agent {name:$agent}) "
            "CREATE (e:Event {id:$eid, ts:$ts, kind:$kind, summary:$summary}) "
            "CREATE (s)-[:HAS_EVENT]->(e) "
            "CREATE (a)-[:PERFORMED]->(e)",
            sid=session_id, agent=agent, eid=eid,
            ts=ts or self.now(), kind=kind, summary=summary,
        )
        # A missing Session still writes nothing, and that has to be loud:
        # handing back an id for an event that does not exist is how a
        # caller ends up with source_event pointing at nothing.
        if res.relationships_created != 2:
            raise LookupError(
                f"could not record event: session {session_id!r} not found")
        if speaker:
            self._write("MATCH (e:Event {id:$eid}) SET e.speaker = $speaker",
                        eid=eid, speaker=speaker)
        for label, name in about or []:
            # Open-layer labels too: an episode about an Appointment or a
            # DataStructure is as real as one about a Project, and the
            # closed list here was the fourth place that assumption bit.
            label = check_any_entity_label(label, "ABOUT target")
            key = "id" if label == "Decision" else "name"
            self._write(
                f"MATCH (e:Event {{id:$eid}}), (n:{label} {{{key}:$name}}) "
                f"CREATE (e)-[:ABOUT]->(n)",
                eid=eid, name=name,
            )
        return eid

    def _superseded_events(self, event_ids):
        """Which of these episodes described something no longer true.

        The episodic tier is immutable on purpose: an event never stops
        having happened, so retiring a fact does not touch the episode that
        established it. That is right as history and wrong as an answer --
        the episode's own words still assert the fact ("Learned: you
        HAS_APPOINTMENT doctor"), so a model reading recent episodes reports
        a clash that was retired an hour ago, even while the fact queries
        beside it correctly return nothing.

        An episode counts as superseded when everything it established has
        since been retired. One that established nothing -- an ordinary
        message -- is never superseded, because it asserted nothing to lose.

        Labelling these in place was tried first and was not enough: a small
        model reads "[superseded]" as something interesting to report, so it
        narrated the cancelled appointment instead of ignoring it. The recall
        tools now leave them out and return a count instead. Nothing is lost
        from the graph -- history is reached through the history tools, which
        is the same split as everywhere else here: recall answers "what is
        true now", history answers "what happened".
        """
        ids = [i for i in dict.fromkeys(event_ids) if i]
        if not ids:
            return set()
        rows = self._read(
            "MATCH ()-[r]->() WHERE r.source_event IN $ids "
            "RETURN r.source_event, r.valid_to", ids=ids)
        rows = list(rows) + list(self._read(
            "MATCH (n) WHERE n.source_event IN $ids "
            "RETURN n.source_event, n.valid_to", ids=ids))
        state = {}
        for eid, valid_to in rows:
            state.setdefault(eid, []).append(valid_to is not None)
        return {eid for eid, retired in state.items() if retired and all(retired)}

    def recall_recent(self, limit=10, session_id=None):
        """Most recent events, newest first. Episodic recall across sessions.

        Ordering is (ts, id) descending because two events can share a second.

        An agent's own replies are left out, and that is not tidiness. A
        reply is prose the model generated; it asserts things in sentences
        rather than in edges, so nothing can retire it. Retiring a fact
        correctly silenced every tool that reads facts, and then the agent
        read its own earlier answer -- "you already have a dentist
        appointment on the 20th" -- out of the episode log and said it
        again. The sentence outlived the fact it was about.

        What an agent said is still in the graph, with its `speaker`, and
        the UI rebuilds transcripts from it. It is simply not evidence: the
        substance of a turn is the facts it wrote and what the person
        actually said, and both of those remain here.
        """
        # Over-fetch: the agents' own replies are dropped below, and asking
        # for exactly `limit` rows would return short once they go.
        window = max(int(limit) * 4, 20)
        if session_id:
            rows = self._read(
                "MATCH (s:Session {id:$sid})-[:HAS_EVENT]->(e:Event) "
                "MATCH (a:Agent)-[:PERFORMED]->(e) "
                "RETURN e.id, e.ts, e.kind, e.summary, a.name, s.id, e.speaker "
                "ORDER BY e.ts DESC, e.id DESC LIMIT $limit",
                sid=session_id, limit=window,
            )
        else:
            rows = self._read(
                "MATCH (s:Session)-[:HAS_EVENT]->(e:Event) "
                "MATCH (a:Agent)-[:PERFORMED]->(e) "
                "RETURN e.id, e.ts, e.kind, e.summary, a.name, s.id, e.speaker "
                "ORDER BY e.ts DESC, e.id DESC LIMIT $limit",
                limit=window,
            )
        spoken_by_agent = {r[0] for r in self._read("MATCH (a:Agent) RETURN a.name")}
        rows = [r for r in rows if r[6] not in spoken_by_agent][:int(limit)]
        stale = self._superseded_events([r[0] for r in rows])
        return [
            {"id": r[0], "ts": r[1], "kind": r[2], "summary": r[3],
             "agent": r[4], "session": r[5]}
            for r in rows if r[0] not in stale
        ]

    def session_user(self, session_id):
        """Who the agent is talking to in this session.

        Without this the agent has no referent for "I" or "me" and has to
        guess a subject when recording a personal fact - which it does badly,
        sometimes filing the user's dentist appointment under its own name.
        """
        rows = self._read("MATCH (s:Session {id:$sid}) RETURN s.user",
                          sid=session_id)
        return rows[0][0] if rows else None

    def session_transcript(self, session_id, limit=200):
        """Read a conversation back out of the graph, oldest first.

        The UI renders chat history from this rather than keeping it in
        browser state, so reloading the page does not lose the conversation:
        it was never in the page. Only events with a `speaker` are utterances;
        everything else in the session is the agent's working record.
        """
        rows = self._read(
            "MATCH (s:Session {id:$sid})-[:HAS_EVENT]->(e:Event) "
            "WHERE e.speaker IS NOT NULL "
            "RETURN e.id, e.ts, e.speaker, e.summary "
            "ORDER BY e.ts, e.id LIMIT $limit",
            sid=session_id, limit=limit,
        )
        return [{"id": r[0], "ts": r[1], "speaker": r[2], "text": r[3]}
                for r in rows]

    # -------------------------------------------------------------------- topics
    #
    # A turn used to be an island: record_event accepts an `about` argument
    # that wires an episode to the entities it touched, and the agent loop
    # never passed one. Measured on the three workspaces in real use, 47 of
    # 47 utterances had no outgoing edge at all -- every message stored, and
    # none of it reachable from anything it was about. These four methods and
    # the curator that calls them are what close that.

    @staticmethod
    def _clean_topic(name):
        """Normalise a topic so the same subject lands on one node.

        Topics come from a model, and docs/TESTING.md section 5 records what
        that is worth: the same allergy sentence produced IS_ALLERGIC_TO on
        one run and HAS_ALLERGY on the next. Case and spacing are the cheap
        half of that problem and this fixes them; genuine synonyms it cannot.

        Unlike a label or a relationship type, a topic is a property *value*,
        so it travels as a query parameter and carries no injection risk.
        The normalising here is purely against fragmentation.
        """
        text = " ".join((name or "").split()).lower()
        if not text:
            raise ValueError("a topic needs a name")
        return text[:80]

    def link_event_topics(self, event_id, topics):
        """Attach one turn to the subjects it was about.

        Returns the topic names actually written, which is what lets the
        caller report what it did rather than assert it.
        """
        if not self._read("MATCH (e:Event {id:$eid}) RETURN e.id", eid=event_id):
            # The same failure as bug 2.1 in docs/TESTING.md: a MATCH that
            # finds nothing makes the rest of the query a silent no-op. An
            # id was handed to us, so a missing event is a real fault.
            raise LookupError(f"cannot add topics: no event {event_id!r}")
        written = []
        for raw in topics or []:
            try:
                name = self._clean_topic(raw)
            except ValueError:
                continue                   # skip junk, never fail the turn
            self._write(
                "MATCH (e:Event {id:$eid}) "
                "MERGE (t:Topic {name:$name}) "
                "ON CREATE SET t.first_seen = $now "
                "MERGE (e)-[:ON_TOPIC]->(t)",
                eid=event_id, name=name, now=self.now())
            written.append(name)
        return written

    def link_event_entities(self, event_id, names):
        """Link a turn to entities that **already exist**, by name.

        The split that keeps this from filling the graph with scenery:
        mentions connect a turn to things already known, while facts create
        what is new. A model naming "Tuesday" in passing should not mint a
        node; a model stating "the review is on Tuesday" should, and that
        goes through remember_fact.

        No label is named anywhere here, so nothing is interpolated into the
        query -- the name travels as a parameter and the join is on internal
        id. That is exactly why this reaches open-layer entities such as
        Appointment or Language, which ENTITY_LABELS deliberately does not
        list. ABOUT is reused rather than a new MENTIONS type being invented,
        per the argument in docs/SCHEMA.md for Insight sharing it: "what is
        this node concerned with" is the same question either way.
        """
        linked = []
        for raw in names or []:
            name = " ".join((raw or "").split())
            if not name:
                continue
            res = self._write(
                f"MATCH (e:Event {{id:$eid}}) "
                f"MATCH (n) WHERE n.name = $name AND {not_entity_clause('n')} "
                f"MERGE (e)-[:ABOUT]->(n)",
                eid=event_id, name=name)
            if res.relationships_created:
                linked.append(name)
        return linked

    def record_curation(self, event_id, ok, detail=""):
        """Mark on the answer whether the turn's curation succeeded.

        The curator exists to make capture reliable, and until this it
        failed *silently*: `curate_turn` swallows every exception by design,
        the error was returned on the Turn and then dropped, and nothing was
        written. So a turn where the agent said "noted, Java from now on"
        and nothing was stored left no trace anywhere -- which is exactly
        the failure docs/TESTING.md section 3b calls the dangerous one, and
        it was reintroduced by the thing built to fix it.

        Stored as properties on the answer Event rather than as a node: this
        is a fact about a turn, not an episode of its own, and a new node
        per turn would show up in recall and in the rendered graph.
        """
        self._write(
            "MATCH (e:Event {id:$eid}) "
            "SET e.curated = $ok, e.curation_note = $detail",
            eid=event_id, ok=bool(ok), detail=(detail or "")[:400])

    def failed_curations(self, limit=20):
        """Turns whose curation did not run. Answers "why wasn't that kept?"

        Without this the question is unanswerable after the fact -- the only
        evidence was a log line in a process that has since restarted.
        """
        rows = self._read(
            "MATCH (e:Event) WHERE e.curated = false "
            "RETURN e.id, e.ts, e.curation_note, substring(e.summary, 0, 120) "
            "ORDER BY e.ts DESC, e.id DESC LIMIT $limit",
            limit=limit,
        )
        return [{"event": r[0], "ts": r[1], "why": r[2], "answer": r[3]}
                for r in rows]

    def session_topics(self, session_id, limit=3):
        """Topics already attached to this conversation, most recent first.

        What this is for: a curator looking at one turn in isolation often
        decides a follow-up has no subject of its own. Observed immediately
        on a live run -- turn 1 "I'm working on lift control logic" was
        topiced correctly, and turn 2 "what data structure should hold the
        pending floor requests?" came back with an empty topic list. Valid
        JSON, no error, and a ten-turn design discussion would end up with
        two turns findable and eight lost.

        A conversation is about something, and a follow-up is about the same
        thing until it changes. So the fallback is read from the graph
        rather than argued for in the prompt -- the same reasoning as every
        other fix in docs/TESTING.md that actually held: change what the
        code does, not what the model is asked to do.
        """
        rows = self._read(
            "MATCH (s:Session {id:$sid})-[:HAS_EVENT]->(e:Event)-[:ON_TOPIC]->(t:Topic) "
            "WITH t, max(e.id) AS seq "
            "RETURN t.name ORDER BY seq DESC LIMIT $limit",
            sid=session_id, limit=limit,
        )
        return [r[0] for r in rows]

    def link_session_outputs_to_topics(self, session_id, topics):
        """Attach this conversation's rules and decisions to its subjects.

        A `Procedure` or `Decision` is created with exactly one edge, to the
        `Event` that made it. That makes it an island: it cannot be reached
        from the subject it was agreed under, and in the rendered graph it
        is a dot on its own. Counted on a real workspace, every Decision and
        every Procedure had degree 1 while the Topic nodes had 10 and 12.

        ABOUT is reused here for the same reason as everywhere else -- "what
        is this node concerned with" is one question. Returns how many edges
        were newly created, so the caller reports rather than asserts.
        """
        made = 0
        for raw in topics or []:
            try:
                name = self._clean_topic(raw)
            except ValueError:
                continue
            res = self._write(
                "MATCH (s:Session {id:$sid})-[:HAS_EVENT]->(e:Event) "
                "MATCH (n)-[:RECORDED_IN|LEARNED_FROM]->(e) "
                "MATCH (t:Topic {name:$name}) "
                "MERGE (n)-[:ABOUT]->(t)",
                sid=session_id, name=name)
            made += res.relationships_created
        return made

    def mark_settlement(self, event_id, statement, topics):
        """Record that this turn settled something, and on what subject.

        Why this is a marked edge rather than a keyword search over prose,
        which is the obvious cheaper design:

        A settlement stated in a sentence cannot be retired. That is the
        whole lesson of docs/TESTING.md section 3 -- three separate
        surfaces had to be closed one at a time because prose asserts
        things that `valid_to` cannot reach. Search the transcript for
        "queue" after one turn chose a PriorityQueue and a later one
        switched to a Deque, and both match equally.

        Marking makes the settlement a thing in the graph, so "what did we
        decide" is answered by *ordering* rather than by guessing which
        sentence is current. No `valid_to` is needed: a later settlement on
        the same topic supersedes an earlier one simply by being later, and
        ordering by (ts, id) is exact -- which is the same argument
        docs/SCHEMA.md makes for `SUPERSEDES` being needed only on
        `Decision`, where the replacement is not otherwise findable.
        """
        text = " ".join((statement or "").split())[:400]
        if not text:
            raise ValueError("a settlement needs a statement")
        if not self._read("MATCH (e:Event {id:$eid}) RETURN e.id", eid=event_id):
            raise LookupError(f"cannot mark a settlement: no event {event_id!r}")
        self._write("MATCH (e:Event {id:$eid}) SET e.settled = $text",
                    eid=event_id, text=text)
        marked = []
        for raw in topics or []:
            try:
                name = self._clean_topic(raw)
            except ValueError:
                continue
            self._write(
                "MATCH (e:Event {id:$eid}) MERGE (t:Topic {name:$name}) "
                "MERGE (e)-[:SETTLES]->(t)",
                eid=event_id, name=name)
            marked.append(name)
        return {"statement": text, "topics": marked}

    def settlements(self, topic=None, limit=10):
        """What has been settled, newest first. The current one is the first.

        Ordered by (ts, id) descending, which is only exact because ids
        carry a microsecond prefix -- several turns inside one exchange tie
        on a second-precision timestamp. Hidden sessions are excluded, or
        removing a conversation would leave its decisions reachable.
        """
        where = ["e.settled IS NOT NULL", "s.hidden IS NULL"]
        params = {"limit": limit}
        match = ("MATCH (e:Event)-[:SETTLES]->(t:Topic) "
                 "MATCH (sess:Session)-[:HAS_EVENT]->(e) ")
        if topic is not None:
            resolved = self.resolve_topic(topic)
            if resolved is None:
                return []
            where.append("t.name = $topic")
            params["topic"] = resolved
        rows = self._read(
            match.replace("(sess:Session)", "(s:Session)")
            + f"WHERE {' AND '.join(where)} "
            + "RETURN e.id, e.ts, t.name, e.settled, e.speaker, s.id "
              "ORDER BY e.ts DESC, e.id DESC LIMIT $limit",
            **params,
        )
        # Flagged, not merely ordered. Asked what was decided, a model read
        # a correctly newest-first list and narrated it oldest-first; it
        # reached the right conclusion that time, which is not the same as
        # being reliable. Order is a convention a reader can miss.
        #
        # `latest` and `earlier`, not `current` and `superseded`. The first
        # version said superseded, and that was a claim the graph cannot
        # support: several independent things get settled on one subject --
        # the queue type, the door timing, the emergency stop are three
        # decisions about one lift, and none replaces another. Only a
        # settlement that contradicts an earlier one supersedes it, and
        # nothing here knows which did. Saying "earlier" is true; saying
        # "superseded" was the tool asserting something it had not been
        # told, which is the failure this project keeps finding in models.
        out, seen = [], set()
        for r in rows:
            topic_name = r[2]
            out.append({"event": r[0], "ts": r[1], "topic": topic_name,
                        "settled": r[3], "said_by": r[4], "session": r[5],
                        "status": ("latest" if topic_name not in seen
                                   else "earlier")})
            seen.add(topic_name)
        return out

    def search_turns(self, text, limit=10):
        """Full-text search over what was actually said, newest first.

        A real FalkorDB full-text index, not a Python scan -- see
        FULLTEXT_INDEXES for the verified DDL. Searching prose is the one
        job a transcript is genuinely better at than a graph, and doing it
        through the database rather than around it is the difference
        between using FalkorDB and decorating with it.

        Both sides come back. Ambient recall leaves an agent's prose out
        because it is unreliable as fact, but a search is asked for by
        name, and the answer to "where did we land on this" is very often
        in something the agent said -- the same resolution reached for
        read_conversation and read_topic.

        It matches **whole words, not substrings**, which is worth knowing
        before relying on it: searching "queue" does not find
        "PriorityQueue", because the tokeniser treats that as one word.
        Verified, not assumed. This is why `settlements()` exists rather
        than this being the whole answer -- a marked settlement is found by
        traversal, which does not care how the thing was spelled.
        """
        query = " ".join((text or "").split())
        if not query:
            return []
        try:
            rows = self._read(
                # `term`, not `q`: _read's own first parameter is named q,
                # so passing q= as a query parameter collides with it.
                "CALL db.idx.fulltext.queryNodes('Event', $term) YIELD node AS e "
                "MATCH (s:Session)-[:HAS_EVENT]->(e) "
                "WHERE s.hidden IS NULL AND e.speaker IS NOT NULL "
                "RETURN e.id, e.ts, e.speaker, e.summary, e.settled, s.id "
                "ORDER BY e.ts DESC, e.id DESC LIMIT $limit",
                term=query, limit=limit)
        except ResponseError as exc:
            # A workspace written before the index existed, or a query the
            # tokeniser rejects. Saying so beats pretending nothing matched.
            raise LookupError(f"full-text search unavailable: {exc}") from exc
        return [{"event": r[0], "ts": r[1], "speaker": r[2], "text": r[3],
                 "settled": r[4], "session": r[5]} for r in rows]

    def topic_index(self, limit=12):
        """One line per subject this workspace has discussed, newest first.

        This is the index, not the content, and the distinction is the whole
        design. Injecting past turns into every prompt would re-open the bug
        in docs/TESTING.md section 3, attempt 3 -- an agent reading back its
        own prose and repeating a fact that had since been retired, because
        a sentence cannot carry valid_to. Injecting the *index* tells the
        model the conversation exists without putting stale prose in front
        of it, and it fetches the real thing by name only when it is wanted.

        Only utterances count. The agent's internal working record is not a
        turn of conversation.
        """
        rows = self._read(
            "MATCH (t:Topic)<-[:ON_TOPIC]-(e:Event) "
            "WHERE e.speaker IS NOT NULL "
            # Tie-broken on id, not on name. Timestamps are second-precision,
            # so two subjects touched in one turn tie on `last` and would
            # otherwise order alphabetically -- in an index whose whole claim
            # is "most recently discussed". Ids carry a microsecond prefix and
            # sort in time order, which is why the convention everywhere in
            # this store is ORDER BY (ts, id) and never ts alone.
            "WITH t, count(e) AS turns, max(e.ts) AS last, max(e.id) AS seq "
            "RETURN t.name, turns, last "
            "ORDER BY last DESC, seq DESC LIMIT $limit",
            limit=limit,
        )
        return [{"topic": r[0], "turns": r[1], "last": r[2]} for r in rows]

    def resolve_topic(self, name):
        """Find the stored topic a loosely-worded request means, or None.

        Exact match, then substring either way, then best overlap of words.
        The fallbacks are not decoration: FalkorDB's CONTAINS is a plain
        substring test, so "lift logic" does not match "lift control logic"
        -- verified -- and paraphrasing the topic back is exactly what a
        model does. Topic counts are in the tens, so scoring them in Python
        costs nothing and forgives far more than Cypher can.
        """
        wanted = self._clean_topic(name)
        rows = self._read("MATCH (t:Topic) RETURN t.name")
        names = [r[0] for r in rows]
        if wanted in names:
            return wanted
        for stored in names:
            if wanted in stored or stored in wanted:
                return stored
        words = set(wanted.split())
        best, score = None, 0
        for stored in names:
            shared = len(words & set(stored.split()))
            if shared > score:
                best, score = stored, shared
        return best

    def read_topic(self, name, limit=100):
        """Every turn on one subject, both sides, verbatim, across sessions.

        This is read_conversation organised by subject instead of by
        session, and it is what makes "let's pick up the lift logic" work
        when the lift was discussed across three different chats.

        Both sides come back whole, deliberately. Ambient recall leaves an
        agent's prose out because it is unreliable as fact (docs/TESTING.md
        section 3), but a conversation asked for by name is being resumed,
        and there the replies *are* the thing being resumed -- the same
        resolution reached for read_conversation in section 3c.

        Hidden sessions are excluded. Removing a conversation has to remove
        it from every way back in, or topics become a hole in the removal.
        """
        resolved = self.resolve_topic(name)
        if resolved is None:
            return {"topic": None, "asked": name, "turns": []}
        rows = self._read(
            "MATCH (t:Topic {name:$name})<-[:ON_TOPIC]-(e:Event) "
            "MATCH (s:Session)-[:HAS_EVENT]->(e) "
            "WHERE e.speaker IS NOT NULL AND s.hidden IS NULL "
            "RETURN s.id, e.ts, e.speaker, e.summary "
            "ORDER BY e.ts, e.id LIMIT $limit",
            name=resolved, limit=limit,
        )
        return {
            "topic": resolved,
            "asked": name,
            "turns": [{"session": r[0], "ts": r[1], "speaker": r[2],
                       "text": r[3]} for r in rows],
        }

    def list_sessions(self, limit=25):
        """Sessions newest first, with how much was said and how each opened.

        `preview` is the first thing actually said, which is what the UI
        titles a conversation with -- `sess-you-1792283610` is an id, not a
        name, and a sidebar full of ids is unreadable.

        Only events carrying a `speaker` count. An agent's internal working
        record is not part of the conversation, so it neither titles it nor
        counts towards its length.

        The ORDER BY before the collect is what makes `msgs[0]` the *first*
        message rather than an arbitrary one: aggregation preserves the order
        rows arrive in. Collecting every summary to use two of them is
        wasteful on a long conversation, but it keeps this to one round trip
        and the alternative is a second query per session.
        """
        rows = self._read(
            "MATCH (s:Session) WHERE s.hidden IS NULL "
            "OPTIONAL MATCH (s)-[:HAS_EVENT]->(e:Event) WHERE e.speaker IS NOT NULL "
            "WITH s, e ORDER BY e.ts, e.id "
            "WITH s, collect(e.summary) AS msgs "
            "RETURN s.id, s.started_at, s.user, size(msgs) AS turns, msgs[0], "
            "       s.title "
            "ORDER BY s.started_at DESC, s.id DESC LIMIT $limit",
            limit=limit,
        )
        return [{"id": r[0], "started_at": r[1], "user": r[2], "turns": r[3],
                 "preview": r[4], "title": r[5]}
                for r in rows]

    def deletion_preview(self, session_id):
        """What a permanent delete of this conversation would destroy.

        Shown before the fact because a delete here is not like deleting a
        chat elsewhere: the conversation is the provenance for what it
        taught, so the facts go with it.
        """
        ids = [r[0] for r in self._read(
            "MATCH (:Session {id:$sid})-[:HAS_EVENT]->(e:Event) RETURN e.id",
            sid=session_id)]
        if not ids:
            return {"events": 0, "messages": 0, "facts": 0, "nodes": 0,
                    "lookups": 0, "orphaned_insights": 0}
        count = lambda q: (self._read(q, ids=ids) or [[0]])[0][0]  # noqa: E731
        return {
            "events": len(ids),
            "messages": len(self.session_transcript(session_id)),
            "facts": count("MATCH ()-[r]->() WHERE r.source_event IN $ids "
                           "RETURN count(r)"),
            "nodes": count("MATCH (n) WHERE n.source_event IN $ids "
                           "RETURN count(n)"),
            "lookups": (self._read("MATCH (l:Lookup) WHERE l.session = $sid "
                                   "RETURN count(l)", sid=session_id)
                        or [[0]])[0][0],
            # Insights built from these episodes keep existing but lose the
            # sources that justified them, which is worth saying out loud.
            "orphaned_insights": count(
                "MATCH (i:Insight)-[:DERIVED_FROM]->(e:Event) "
                "WHERE e.id IN $ids RETURN count(DISTINCT i)"),
        }

    def delete_session(self, session_id):
        """Destroy a conversation and everything it created. No undo.

        This is the one operation in the project that really deletes, and it
        exists because retiring is not always what someone means. Retirement
        keeps the row and closes its validity window, which is right for
        memory that changed -- but anything still in the graph can still be
        read, and three separate surfaces had to be shut one at a time before
        a retired appointment stopped being mentioned (see docs/TESTING.md).
        When the intent is "this should not exist", only removing it is
        honest.

        Order matters. Facts are removed before the events they cite, so no
        edge is ever left with a `source_event` pointing at a deleted node;
        the trace goes before the events it hangs off, for the same reason.
        Orphaned entities are swept last: a node left with no relationships
        at all -- the `doctor` nobody has an appointment with any more --
        carries nothing, and leaving it would slowly fill the graph with
        names that mean nothing.

        What this cannot undo is a reference from somewhere else. An Insight
        consolidated from these episodes keeps existing with fewer sources.
        `deletion_preview` reports that before anything happens.
        """
        ids = [r[0] for r in self._read(
            "MATCH (:Session {id:$sid})-[:HAS_EVENT]->(e:Event) RETURN e.id",
            sid=session_id)]
        gone = {"facts": 0, "nodes": 0, "lookups": 0, "events": 0,
                "orphans": 0}

        if ids:
            res = self._write("MATCH ()-[r]->() WHERE r.source_event IN $ids "
                              "DELETE r RETURN count(r)", ids=ids)
            gone["facts"] = res.result_set[0][0] if res.result_set else 0
            gone["nodes"] = self._write(
                "MATCH (n) WHERE n.source_event IN $ids "
                "DETACH DELETE n", ids=ids).nodes_deleted
        # By session, not by READ edge. A turn that failed before answering
        # leaves its lookups unlinked -- recorded, with nowhere to hang --
        # and matching on the edge would walk straight past them.
        gone["lookups"] = self._write(
            "MATCH (l:Lookup) WHERE l.session = $sid "
            "DETACH DELETE l", sid=session_id).nodes_deleted

        gone["events"] = self._write(
            "MATCH (s:Session {id:$sid}) "
            "OPTIONAL MATCH (s)-[:HAS_EVENT]->(e:Event) "
            "DETACH DELETE e, s", sid=session_id).nodes_deleted

        # An Agent is kept even with nothing left to point at: it is an
        # identity, not a fact, and record_event MERGEs it back anyway.
        gone["orphans"] = self._write(
            "MATCH (n) WHERE NOT (n)--() AND NOT n:Agent "
            "DELETE n").nodes_deleted
        return gone

    def rename_session(self, session_id, title):
        """Give a conversation a name instead of its opening line.

        Until renamed, a conversation is titled by its first message, which is
        derived rather than stored. A title is the one thing about a session
        a person owns, so it is the one thing they can set.
        """
        res = self._write(
            "MATCH (s:Session {id:$sid}) SET s.title = $title",
            sid=session_id, title=(title or "").strip())
        return bool(res.properties_set)

    def hide_session(self, session_id):
        """Remove a conversation, and retire everything it taught.

        Hiding the conversation alone is not enough, and the gap is easy to
        walk into: the chat leaves the sidebar while the facts it established
        stay current, so the agent keeps refusing to book a flight on account
        of an appointment the user believes they just deleted. The memory and
        the conversation are different things, and the person removing one
        means both.

        So this closes the validity window on every fact whose `source_event`
        belongs to this session -- open-layer edges, versioned world
        relationships, and the versioned nodes (Decision, Procedure) alike.

        It is still not a delete. The facts keep their `valid_to` and remain
        readable through fact_history, the events stay, and the reasoning
        trace still resolves: nothing is left pointing at something that no
        longer exists. The rule is the one that holds everywhere else here --
        the only destructive operation is closing a validity window.

        Returns how many facts were retired.
        """
        now = self.now()
        edges = self._write(
            "MATCH (:Session {id:$sid})-[:HAS_EVENT]->(e:Event) "
            "WITH collect(e.id) AS ids "
            "MATCH ()-[r]->() "
            "WHERE r.source_event IN ids AND r.valid_to IS NULL "
            "SET r.valid_to = $now, r.retired_with_session = $sid "
            "RETURN count(r)",
            sid=session_id, now=now)
        nodes = self._write(
            "MATCH (:Session {id:$sid})-[:HAS_EVENT]->(e:Event) "
            "WITH collect(e.id) AS ids "
            "MATCH (n) "
            "WHERE n.source_event IN ids AND n.valid_to IS NULL "
            "SET n.valid_to = $now, n.retired_with_session = $sid "
            "RETURN count(n)",
            sid=session_id, now=now)
        self._write("MATCH (s:Session {id:$sid}) SET s.hidden = true",
                    sid=session_id)
        count = 0
        for res in (edges, nodes):
            if res.result_set:
                count += res.result_set[0][0]
        return count

    def recall_related(self, label, name, limit=20):
        """Everything the graph currently knows about one entity.

        Returns episodes that mentioned it plus the world facts it currently
        participates in. This is the main multi-hop read and the Track 01
        "graph query as agent tool".
        """
        label = check_any_entity_label(label)
        key = "id" if label == "Decision" else "name"

        events = self._read(
            f"MATCH (e:Event)-[:ABOUT]->(n:{label} {{{key}:$name}}) "
            f"RETURN e.id, e.ts, e.kind, e.summary "
            f"ORDER BY e.ts DESC, e.id DESC LIMIT $limit",
            name=name, limit=limit,
        )

        # Outgoing and incoming current facts. The edge alias is referenced in
        # the WHERE, which also avoids the existential-match quirk that makes
        # an unreferenced alias under-report parallel (versioned) edges.
        out = self._read(
            f"MATCH (n:{label} {{{key}:$name}})-[r]->(m) "
            f"WHERE r.valid_to IS NULL AND r.valid_from IS NOT NULL "
            f"RETURN type(r), labels(m)[0], coalesce(m.name, m.title, m.id), r.valid_from",
            name=name,
        )
        inc = self._read(
            f"MATCH (m)-[r]->(n:{label} {{{key}:$name}}) "
            f"WHERE r.valid_to IS NULL AND r.valid_from IS NOT NULL "
            f"RETURN type(r), labels(m)[0], coalesce(m.name, m.title, m.id), r.valid_from",
            name=name,
        )
        # Consolidated knowledge is usually the most useful thing to return
        # first: one insight can stand for a dozen episodes.
        insights = self._read(
            f"MATCH (i:Insight)-[:ABOUT]->(n:{label} {{{key}:$name}}) "
            f"WHERE i.valid_to IS NULL "
            f"RETURN i.id, i.text, i.valid_from ORDER BY i.valid_from DESC",
            name=name,
        )

        stale_events = self._superseded_events([r[0] for r in events])
        return {
            "entity": {"label": label, "name": name},
            "insights": [
                {"id": r[0], "text": r[1], "since": r[2]} for r in insights
            ],
            "events": [
                {"id": r[0], "ts": r[1], "kind": r[2], "summary": r[3]}
                for r in events if r[0] not in stale_events
            ],
            "superseded_episodes": sum(1 for r in events if r[0] in stale_events),
            "facts_out": [
                {"type": r[0], "label": r[1], "name": r[2], "since": r[3]} for r in out
            ],
            "facts_in": [
                {"type": r[0], "label": r[1], "name": r[2], "since": r[3]} for r in inc
            ],
        }

    # ------------------------------------------------------------------ semantic

    def record_decision(self, session_id, agent, title, reason,
                        affects=(), made_by=None, supersedes=None):
        """Record a decision, with provenance, scope, and an optional update chain.

        When `supersedes` is given the older decision is retired in the same
        call: its valid_to closes and a SUPERSEDES edge records what replaced
        it. valid_to says a fact stopped being current; SUPERSEDES says what
        replaced it, and the demo needs both.

        Re-recording a decision that is already current is refused. Found by
        use: a person stated a coding preference three times across a
        conversation and the Planner wrote three Decision nodes --
        "Code language preference: Java first", "First preference language
        for code", "Code comments rule" -- each a near-duplicate of a
        standing rule the curator had already stored properly. Telling the
        Planner in its prompt that a preference is not a decision is the
        first line of defence and, on the evidence in docs/TESTING.md, not
        one that holds by itself.
        """
        if supersedes is None:
            twin = self.similar_decision(title)
            if twin:
                return twin
        eid = self.record_event(
            session_id, agent, "decision", f"Recorded decision: {title}",
            about=[("Project", p) for p in affects],
        )
        did = self._new_id("dec")
        now = self.now()
        self._write(
            "MATCH (e:Event {id:$eid}) "
            "CREATE (d:Decision {id:$did, title:$title, reason:$reason, "
            "                    valid_from:$now, source_event:$eid}) "
            "CREATE (d)-[:RECORDED_IN]->(e)",
            eid=eid, did=did, title=title, reason=reason, now=now,
        )
        if made_by:
            self._write(
                "MATCH (p:Person {name:$person}), (d:Decision {id:$did}) "
                "CREATE (p)-[:MADE]->(d)",
                person=made_by, did=did,
            )
        for project in affects:
            self._write(
                "MATCH (d:Decision {id:$did}), (p:Project {name:$project}) "
                "CREATE (d)-[:AFFECTS {valid_from:$now, source_event:$eid}]->(p)",
                did=did, project=project, now=now, eid=eid,
            )
        if supersedes:
            # Close the old decision and link the chain. Scoped by the MATCH
            # pattern, never by LIMIT -- LIMIT does not constrain SET.
            self._write(
                "MATCH (old:Decision {id:$old}), (new:Decision {id:$new}) "
                "WHERE old.valid_to IS NULL "
                "SET old.valid_to = $now "
                "CREATE (new)-[:SUPERSEDES]->(old)",
                old=supersedes, new=did, now=now,
            )
        return did

    def current_decisions(self, project=None):
        """Decisions that are in force, optionally narrowed to one project.

        This is also how project state is derived: a Project has no `status`
        property, so "is it blocked?" is answered by the decisions affecting it
        -- state therefore always arrives with its justification attached.
        """
        if project:
            rows = self._read(
                "MATCH (d:Decision)-[a:AFFECTS]->(p:Project {name:$project}) "
                "WHERE d.valid_to IS NULL AND a.valid_to IS NULL "
                "RETURN d.id, d.title, d.reason, d.valid_from, p.name "
                "ORDER BY d.valid_from DESC",
                project=project,
            )
        else:
            rows = self._read(
                "MATCH (d:Decision)-[a:AFFECTS]->(p:Project) "
                "WHERE d.valid_to IS NULL AND a.valid_to IS NULL "
                "RETURN d.id, d.title, d.reason, d.valid_from, p.name "
                "ORDER BY d.valid_from DESC"
            )
        return [
            {"id": r[0], "title": r[1], "reason": r[2], "since": r[3], "project": r[4]}
            for r in rows
        ]

    def decision_history(self, decision_id):
        """Walk the SUPERSEDES chain backwards from a current decision."""
        rows = self._read(
            "MATCH path = (d:Decision {id:$did})-[:SUPERSEDES*0..10]->(older:Decision) "
            "RETURN older.id, older.title, older.valid_from, older.valid_to, length(path) "
            "ORDER BY length(path)",
            did=decision_id,
        )
        return [
            {"id": r[0], "title": r[1], "from": r[2], "to": r[3], "steps": r[4]}
            for r in rows
        ]

    def assert_fact(self, session_id, agent, rel_type, from_name, to_name, summary=None):
        """Open a new versioned world fact (e.g. a new OWNS edge after a transfer)."""
        check_fact_type(rel_type)
        from_label, to_label = FACT_ENDPOINTS[rel_type]
        eid = self.record_event(
            session_id, agent, "action",
            summary or f"Asserted {from_name} -{rel_type}-> {to_name}",
            about=[(from_label, from_name), (to_label, to_name)],
        )
        res = self._write(
            f"MATCH (a:{from_label} {{name:$from_name}}), (b:{to_label} {{name:$to_name}}) "
            f"CREATE (a)-[:{rel_type} {{valid_from:$now, source_event:$eid}}]->(b)",
            from_name=from_name, to_name=to_name, now=self.now(), eid=eid,
        )
        if res.relationships_created != 1:
            raise LookupError(
                f"could not assert {rel_type}: {from_label} {from_name!r} or "
                f"{to_label} {to_name!r} not found"
            )
        return eid

    def retire_fact(self, session_id, agent, rel_type, from_name, to_name, reason):
        """Close the validity window on a current fact. Nothing is deleted.

        Returns the number of edges retired, which is 0 if the fact was already
        retired or never existed -- the caller should check rather than assume.
        """
        check_fact_type(rel_type)
        from_label, to_label = FACT_ENDPOINTS[rel_type]
        eid = self.record_event(
            session_id, agent, "correction",
            f"Retired {from_name} -{rel_type}-> {to_name}: {reason}",
            about=[(from_label, from_name), (to_label, to_name)],
        )
        res = self._write(
            f"MATCH (a:{from_label} {{name:$from_name}})-[r:{rel_type}]->"
            f"(b:{to_label} {{name:$to_name}}) "
            f"WHERE r.valid_to IS NULL "
            f"SET r.valid_to = $now, r.retired_by = $eid "
            f"RETURN count(r)",
            from_name=from_name, to_name=to_name, now=self.now(), eid=eid,
        )
        return res.result_set[0][0] if res.result_set else 0

    def current_facts(self, rel_type, from_name=None, to_name=None):
        """Read current edges of one type, optionally pinned at either end."""
        check_fact_type(rel_type)
        from_label, to_label = FACT_ENDPOINTS[rel_type]
        where = ["r.valid_to IS NULL"]
        params = {}
        if from_name:
            where.append("a.name = $from_name")
            params["from_name"] = from_name
        if to_name:
            where.append("b.name = $to_name")
            params["to_name"] = to_name
        rows = self._read(
            f"MATCH (a:{from_label})-[r:{rel_type}]->(b:{to_label}) "
            f"WHERE {' AND '.join(where)} "
            f"RETURN a.name, b.name, r.valid_from, r.source_event",
            **params,
        )
        return [
            {"from": r[0], "to": r[1], "since": r[2], "source_event": r[3]} for r in rows
        ]

    def fact_history(self, rel_type, from_name=None, to_name=None):
        """Every version of a fact, current and retired, oldest first."""
        check_fact_type(rel_type)
        from_label, to_label = FACT_ENDPOINTS[rel_type]
        where = ["r.valid_from IS NOT NULL"]
        params = {}
        if from_name:
            where.append("a.name = $from_name")
            params["from_name"] = from_name
        if to_name:
            where.append("b.name = $to_name")
            params["to_name"] = to_name
        rows = self._read(
            f"MATCH (a:{from_label})-[r:{rel_type}]->(b:{to_label}) "
            f"WHERE {' AND '.join(where)} "
            f"RETURN a.name, b.name, r.valid_from, r.valid_to "
            f"ORDER BY r.valid_from",
            **params,
        )
        return [
            {"from": r[0], "to": r[1], "valid_from": r[2], "valid_to": r[3],
             "current": r[3] is None}
            for r in rows
        ]

    # ------------------------------------------------------------- open facts

    def _resolve_entity(self, name, label):
        """Find an existing node by name, or make one with the given type.

        Reuse comes first and ignores the proposed type on purpose. If the
        agent says "Priya Nair" is a Contact and a Person called Priya Nair
        already exists, the right answer is the existing Priya - one node with
        many facts, not two nodes that never meet. Entity resolution is by
        name, which works here because the data is synthetic and names are
        controlled; a real deployment needs more.

        Returns (internal id, label, created?).
        """
        existing = self._read(
            "MATCH (n) WHERE n.name = $name RETURN ID(n), labels(n)[0] LIMIT 1",
            name=name)
        if existing:
            return existing[0][0], existing[0][1], False

        safe = check_open_label(label)
        rows = self._write(
            f"CREATE (n:{safe} {{name:$name, valid_from:$now}}) RETURN ID(n)",
            name=name, now=self.now()).result_set
        return rows[0][0], safe, True

    def remember_fact(self, session_id, agent, subject, predicate, obj,
                      subject_type="Thing", object_type="Thing",
                      when=None, note=None):
        """Store an arbitrary fact as a typed, versioned, sourced edge.

        This is what makes the memory open rather than a fixed form. The agent
        chooses the types and the relationship name at write time; they become
        real labels and edge types, so everything built for the closed schema
        - recall, path explanation, centrality - works on them unchanged.

        It is not a junk drawer, because the fact arrives structured and gets
        the same discipline as everything else: entities are reused rather
        than duplicated, the edge carries valid_from / valid_to, and
        source_event points at the episode that established it. A fact learned
        this way can be retired and superseded like any other.

        `when` is the date the fact is *about* (not when it was recorded),
        which is what makes what_is_on() possible.
        """
        # Validate everything before writing anything. These raise on bad
        # model output, and a half-written fact - an event with no fact
        # attached - is worse than a refusal, because it looks like memory.
        rel = check_open_predicate(predicate)
        check_open_label(subject_type)
        check_open_label(object_type)
        if not (subject or "").strip() or not (obj or "").strip():
            raise ValueError("a fact needs both a subject and an object")

        eid = self.record_event(
            session_id, agent, "message",
            note or f"Learned: {subject} {predicate} {obj}"
                    + (f" (on {when})" if when else ""))

        sid, slabel, s_new = self._resolve_entity(subject, subject_type)
        oid, olabel, o_new = self._resolve_entity(obj, object_type)

        props = {"valid_from": self.now(), "source_event": eid}
        if when:
            props["when"] = when
        clause = ", ".join(f"{k}:${k}" for k in props)
        self._write(
            f"MATCH (s) WHERE ID(s) = $sid MATCH (o) WHERE ID(o) = $oid "
            f"CREATE (s)-[:{rel} {{{clause}}}]->(o)",
            sid=sid, oid=oid, **props)

        # Tie the episode to both ends so recall_related finds it from either.
        for node_id in (sid, oid):
            self._write(
                "MATCH (e:Event {id:$eid}) MATCH (n) WHERE ID(n) = $nid "
                "CREATE (e)-[:ABOUT]->(n)", eid=eid, nid=node_id)

        return {
            "subject": {"name": subject, "type": slabel, "created": s_new},
            "predicate": rel,
            "object": {"name": obj, "type": olabel, "created": o_new},
            "when": when,
            "source_event": eid,
        }

    def retire_open_fact(self, session_id, agent, subject, predicate, obj,
                         reason, when=None):
        """Close an open-layer fact. Nothing is deleted, as everywhere else.

        `when` picks out a single instance. Without it, every open edge of
        this predicate between these two nodes is closed -- right for a
        fact that holds once ("X reports to Y"), wrong for one that
        recurs. Two appointments with the same doctor on different dates
        are both legitimately true, and a rescheduling that records the
        new date before retiring the old one would otherwise close both
        and lose the fact entirely.

        Returns the number of edges closed. More than one, where one was
        meant, is the signature of exactly that bug.
        """
        rel = check_open_predicate(predicate)
        eid = self.record_event(
            session_id, agent, "correction",
            f"Retired: {subject} {predicate} {obj}"
            + (f" (on {when})" if when else "") + f" ({reason})")
        where = ["s.name = $subject", "o.name = $obj", "r.valid_to IS NULL"]
        params = {"subject": subject, "obj": obj}
        if when:
            where.append("r.when = $when")
            params["when"] = when
        res = self._write(
            f"MATCH (s)-[r:{rel}]->(o) WHERE {' AND '.join(where)} "
            f"SET r.valid_to = $now, r.retired_by = $eid "
            f"RETURN count(r)",
            now=self.now(), eid=eid, **params)
        return res.result_set[0][0] if res.result_set else 0

    def open_fact_exists(self, subject, predicate, obj, when=None):
        """Is this exact fact already current? The curator's duplicate guard.

        With one writer there is no race to lose, but a person restating
        something across turns is ordinary, and remember_fact always CREATEs.
        Without this, "I prefer Java" said twice is two identical open edges
        and `recall_entity` reports the preference twice.

        `when` is part of the identity, not a detail: two appointments with
        the same doctor on different dates are both legitimately true. That
        is the same distinction retire_open_fact had to learn -- see bug 2.2
        in docs/TESTING.md.
        """
        rel = check_open_predicate(predicate)
        where = ["s.name = $subject", "o.name = $obj", "r.valid_to IS NULL"]
        params = {"subject": subject, "obj": obj}
        if when:
            where.append("r.when = $when")
            params["when"] = when
        else:
            where.append("r.when IS NULL")
        rows = self._read(
            f"MATCH (s)-[r:{rel}]->(o) WHERE {' AND '.join(where)} "
            f"RETURN count(r)", **params)
        return bool(rows and rows[0][0])

    @staticmethod
    def _rule_words(text):
        """Content words of a rule, for comparing two statements of it."""
        stop = {"a", "an", "the", "to", "of", "on", "in", "for", "and", "it",
                "its", "that", "this", "is", "be", "with", "every", "each",
                "all", "any", "always", "please", "when", "you", "your",
                "i", "me", "my", "will", "should", "must", "do", "give",
                "giving", "provide", "providing", "what", "does"}
        cleaned = "".join(c if c.isalnum() or c.isspace() else " "
                          for c in (text or "").lower())
        return {w for w in cleaned.split() if w not in stop and len(w) > 2}

    def similar_procedure(self, action, threshold=0.7):
        """An in-force rule that already says this, or None.

        Exact-name matching is not enough, and the gap was found by use: the
        Planner restated a standing rule in its reply ("the standing rule is:
        any code I give you will have a comment on every line"), the curator
        read its own restatement as a new teaching, and the graph ended up
        holding both "comment every line of code" and "always comment every
        line of code".

        That is the TESTING.md section 3 problem in a new place -- an agent's
        own prose treated as evidence -- and it is worse for rules than for
        facts, because every duplicate is injected into every later prompt
        and the bloat compounds.

        Compared on the content words of the *action*, since that is what a
        rule does; two people wording the same instruction differently still
        overlap heavily. Deliberately blunt: it cannot merge genuinely
        different wordings of the same intent, and says so in TESTING.md.
        """
        wanted = self._rule_words(action)
        if not wanted:
            return None
        rows = self._read(
            "MATCH (p:Procedure) WHERE p.valid_to IS NULL "
            "RETURN p.name, p.action")
        for name, existing in rows:
            other = self._rule_words(existing)
            if not other:
                continue
            overlap = len(wanted & other) / max(len(wanted), len(other))
            if overlap >= threshold:
                return name
        return None

    def similar_decision(self, title, threshold=0.7):
        """The id of a currently-valid decision that already says this.

        Same comparison as `similar_procedure`, on the title, because a
        decision's title is what it decides. Deliberately only applied when
        no `supersedes` is passed: superseding is the sanctioned way to
        restate a decision, and it keeps the history connected.
        """
        wanted = self._rule_words(title)
        if not wanted:
            return None
        rows = self._read(
            "MATCH (d:Decision) WHERE d.valid_to IS NULL "
            "RETURN d.id, d.title")
        for did, existing in rows:
            other = self._rule_words(existing)
            if not other:
                continue
            if len(wanted & other) / max(len(wanted), len(other)) >= threshold:
                return did
        return None

    def procedure_exists(self, name):
        """Is a standing rule of this name already in force?

        Matched on name rather than on wording, because a person who
        restates a rule has not taught a second rule. Retired rules do not
        count: re-teaching something you revoked should take effect.
        """
        rows = self._read(
            "MATCH (p:Procedure) WHERE p.name = $name AND p.valid_to IS NULL "
            "RETURN count(p)", name=name)
        return bool(rows and rows[0][0])

    def recall_entity(self, name, limit=25):
        """Everything known about something, whatever type it turned out to be.

        Deliberately takes no label: the point of the open layer is that the
        caller need not know how a thing was typed in order to ask about it.
        """
        found = self._read(
            "MATCH (n) WHERE n.name = $name "
            "RETURN labels(n)[0], n.name, n.valid_to LIMIT 1", name=name)
        if not found:
            return None

        # `valid_from IS NOT NULL` is what makes these return facts rather
        # than plumbing. Only versioned edges carry it, so structural ones
        # -- ABOUT, PERFORMED, and the RETURNED edges of the reasoning
        # trace -- drop out. Without it, every lookup an agent has ever
        # made comes back as something it believes about the entity.
        out = self._read(
            "MATCH (n {name:$name})-[r]->(m) "
            "WHERE r.valid_to IS NULL AND r.valid_from IS NOT NULL "
            "RETURN type(r), labels(m)[0], coalesce(m.name, m.title, m.id), "
            "       r.when, r.valid_from LIMIT $limit",
            name=name, limit=limit)
        inc = self._read(
            "MATCH (m)-[r]->(n {name:$name}) "
            "WHERE r.valid_to IS NULL AND r.valid_from IS NOT NULL "
            "RETURN type(r), labels(m)[0], coalesce(m.name, m.title, m.id), "
            "       r.when, r.valid_from LIMIT $limit",
            name=name, limit=limit)
        events = self._read(
            "MATCH (e:Event)-[:ABOUT]->(n {name:$name}) "
            "RETURN e.ts, e.kind, e.summary, e.id ORDER BY e.ts DESC LIMIT $limit",
            name=name, limit=limit)
        stale = self._superseded_events([r[3] for r in events])

        def facts(rows):
            return [{"predicate": r[0], "type": r[1], "name": r[2],
                     "when": r[3], "since": r[4]} for r in rows]

        return {
            "name": found[0][1],
            "type": found[0][0],
            "retired": found[0][2] is not None,
            "facts_out": facts(out),
            "facts_in": facts(inc),
            "episodes": [{"ts": r[0], "kind": r[1], "summary": r[2]}
                         for r in events if r[3] not in stale],
            # Not hidden, just not recalled: the count says history exists.
            "superseded_episodes": sum(1 for r in events if r[3] in stale),
        }

    def what_is_on(self, date, limit=50):
        """Every current fact dated on a given day.

        The reason open facts carry `when`: without a date on the fact itself
        there is no way to ask "what else is happening then", and a memory
        that cannot answer that will cheerfully double-book you.
        """
        rows = self._read(
            "MATCH (s)-[r]->(o) "
            "WHERE ID(r) >= 0 AND r.valid_to IS NULL AND r.when STARTS WITH $date "
            "RETURN coalesce(s.name, s.title, s.id), type(r), "
            "       coalesce(o.name, o.title, o.id), r.when "
            "ORDER BY r.when LIMIT $limit",
            date=date, limit=limit)
        return [{"subject": r[0], "predicate": r[1], "object": r[2], "when": r[3]}
                for r in rows]

    # ---------------------------------------------------------------- procedural

    def learn_procedure(self, session_id, agent, name, trigger, action, applies_to=()):
        """Store a standing rule the user taught, with where it came from.

        `applies_to` is a list of (label, name) pairs; the scope edges are what
        get_applicable_procedures matches on later.
        """
        eid = self.record_event(
            session_id, agent, "correction", f"Learned rule: {name}",
        )
        pid = self._new_id("proc")
        now = self.now()
        self._write(
            "MATCH (e:Event {id:$eid}) "
            "CREATE (p:Procedure {id:$pid, name:$name, trigger:$trigger, action:$action, "
            "                     valid_from:$now, source_event:$eid}) "
            "CREATE (p)-[:LEARNED_FROM]->(e)",
            eid=eid, pid=pid, name=name, trigger=trigger, action=action, now=now,
        )
        for label, target in applies_to:
            check_label(label, PROCEDURE_SCOPES, "procedure scope")
            self._write(
                f"MATCH (p:Procedure {{id:$pid}}), (n:{label} {{name:$target}}) "
                f"CREATE (p)-[:APPLIES_TO {{valid_from:$now, source_event:$eid}}]->(n)",
                pid=pid, target=target, now=now, eid=eid,
            )
        return pid

    def get_applicable_procedures(self, label=None, name=None):
        """Rules in force: for one entity, plus the ones that hold everywhere.

        Three kinds come back.

        * Scoped directly to this entity.
        * Scoped to a System the Project currently USES. That second hop is
          what makes a rule taught about one system fire for a project that
          merely depends on it, without anyone repeating the rule.
        * Scoped to nothing at all.

        The third kind used to be unreachable, and silently so.
        PROCEDURE_SCOPES allows only Project and System, so a rule about a
        topic -- "prefer Java for coding" -- is stored with no APPLIES_TO
        edge and can never be matched by anything, while learn_procedure
        hands back a perfectly good id and the agent reports that it saved a
        standing rule. The promise was real and the rule was orphaned. A rule
        anchored to nothing is one that holds everywhere, not a broken one.

        Called with no entity, this returns every live rule, which is how an
        agent asks "what standing rules are there?" without first having to
        guess a project or system name to hang the question on.
        """
        rows = []
        if label and name:
            check_label(label, PROCEDURE_SCOPES, "procedure scope")
            rows += list(self._read(
                f"MATCH (p:Procedure)-[s:APPLIES_TO]->(n:{label} {{name:$name}}) "
                f"WHERE p.valid_to IS NULL AND s.valid_to IS NULL "
                f"RETURN p.id, p.name, p.trigger, p.action, 'direct'",
                name=name))
            if label == "Project":
                rows += list(self._read(
                    "MATCH (p:Procedure)-[s:APPLIES_TO]->(sys:System)<-[u:USES]-"
                    "(proj:Project {name:$name}) "
                    "WHERE p.valid_to IS NULL AND s.valid_to IS NULL "
                    "  AND u.valid_to IS NULL "
                    "RETURN p.id, p.name, p.trigger, p.action, "
                    "       'via system ' + sys.name",
                    name=name))
            # A rule with no live scope edge is not scoped to this entity --
            # it is scoped to nothing, so it applies here as well.
            rows += list(self._read(
                "MATCH (p:Procedure) WHERE p.valid_to IS NULL "
                "OPTIONAL MATCH (p)-[s:APPLIES_TO]->() WHERE s.valid_to IS NULL "
                "WITH p, count(s) AS scopes WHERE scopes = 0 "
                "RETURN p.id, p.name, p.trigger, p.action, 'applies everywhere'"))
        else:
            rows += list(self._read(
                "MATCH (p:Procedure) WHERE p.valid_to IS NULL "
                "RETURN p.id, p.name, p.trigger, p.action, 'standing rule'"))

        seen, out = set(), []
        for r in rows:
            if r[0] in seen:
                continue
            seen.add(r[0])
            out.append({"id": r[0], "name": r[1], "trigger": r[2],
                        "action": r[3], "matched": r[4]})
        return out

    def retire_procedure(self, session_id, agent, procedure_id, reason):
        """Revoke a rule. The agent must then visibly stop applying it."""
        eid = self.record_event(
            session_id, agent, "correction", f"Retired rule {procedure_id}: {reason}"
        )
        res = self._write(
            "MATCH (p:Procedure {id:$pid}) WHERE p.valid_to IS NULL "
            "SET p.valid_to = $now, p.retired_by = $eid",
            pid=procedure_id, now=self.now(), eid=eid,
        )
        return bool(res.properties_set)

    def snapshot(self):
        """A comparable picture of the whole graph, for showing what changed.

        Taken before and after a demo moment and diffed, so the "memory
        changes" panel reports what the graph actually did rather than what
        an agent said it did. Keyed on internal ids so a node or edge is
        tracked across the change.

        `WHERE ID(r) >= 0` is not decoration: an unreferenced edge alias is
        matched existentially in FalkorDB, which would under-report the
        parallel edges versioning creates.

        The reasoning trace is excluded. This panel answers "what does the
        agent now believe that it did not before", and a lookup is not a
        belief -- it is the agent reading. Left in, a single turn would bury
        one real change under a dozen citations. The trace has its own view.
        """
        nodes = self._read(
            "MATCH (n) WHERE NOT n:Lookup "
            "RETURN ID(n), labels(n)[0], "
            "coalesce(n.name, n.title, n.text, n.id), n.valid_to"
        )
        edges = self._read(
            "MATCH (a)-[r]->(b) "
            "WHERE ID(r) >= 0 AND NOT type(r) IN ['READ', 'RETURNED'] "
            "RETURN ID(r), type(r), coalesce(a.name, a.title, a.text, a.id), "
            "coalesce(b.name, b.title, b.text, b.id), r.valid_to"
        )
        return {
            "nodes": {r[0]: {"label": r[1], "name": r[2], "valid_to": r[3]}
                      for r in nodes},
            "edges": {r[0]: {"type": r[1], "from": r[2], "to": r[3],
                             "valid_to": r[4]} for r in edges},
        }

    # ------------------------------------------------------------ consolidation

    def consolidation_candidates(self, min_events=3, limit=5):
        """Entities with several episodes that have not been absorbed yet.

        The graph picks the work, which is what keeps consolidation from
        being "summarise some text and store it":

        * grouping is a traversal (`Event -[:ABOUT]-> entity`), not a scan;
        * "already absorbed" is an edge, not a flag -- an event with an
          incoming DERIVED_FROM has been consolidated, so it drops out;
        * consolidation events are excluded as raw material, otherwise each
          consolidation would feed the next one and the loop never settles.

        Remove FalkorDB and there is no candidate selection, no de-duplication
        and no provenance left. Ranking is by episode count for now; this is
        the hook where PageRank or betweenness replaces it in build step 8.
        """
        rows = self._read(
            "MATCH (e:Event)-[:ABOUT]->(n) "
            "WHERE NOT (e)<-[:DERIVED_FROM]-(:Insight) AND e.kind <> 'consolidation' "
            "WITH n, count(e) AS episodes, collect(e.id) AS event_ids "
            "WHERE episodes >= $min_events "
            "RETURN labels(n)[0], coalesce(n.name, n.title, n.id), episodes, event_ids "
            "ORDER BY episodes DESC LIMIT $limit",
            min_events=min_events, limit=limit,
        )
        return [
            {"label": r[0], "name": r[1], "episodes": r[2], "event_ids": r[3]}
            for r in rows
        ]

    def events_about(self, label, name, only_unconsolidated=True, limit=20):
        """The episodes that mention an entity, oldest first (reading order).

        Accepts an open-layer label. `consolidation_candidates` picks the
        work from whatever the graph holds, so refusing anything outside
        `ENTITY_LABELS` here meant the chooser and the reader disagreed --
        and the Archivist declined seven real episodes it was not allowed
        to see.
        """
        label = check_any_entity_label(label)
        key = "id" if label == "Decision" else "name"
        absorbed = ("AND NOT (e)<-[:DERIVED_FROM]-(:Insight) "
                    if only_unconsolidated else "")
        rows = self._read(
            f"MATCH (e:Event)-[:ABOUT]->(n:{label} {{{key}:$name}}) "
            f"WHERE e.kind <> 'consolidation' {absorbed}"
            f"RETURN e.id, e.ts, e.kind, e.summary "
            f"ORDER BY e.ts, e.id LIMIT $limit",
            name=name, limit=limit,
        )
        return [{"id": r[0], "ts": r[1], "kind": r[2], "summary": r[3]} for r in rows]

    def record_insight(self, session_id, agent, label, name, text, source_event_ids):
        """Store a consolidated fact, linked back to every episode behind it.

        The DERIVED_FROM edges are the whole point: a consolidation that
        cannot be traced to its sources is an unverifiable assertion, and it
        is also what stops those episodes being consolidated again.
        """
        label = check_any_entity_label(label)
        if not source_event_ids:
            raise ValueError("an insight needs at least one source event")
        key = "id" if label == "Decision" else "name"

        eid = self.record_event(
            session_id, agent, "consolidation",
            f"Consolidated {len(source_event_ids)} episodes about {name}: {text}",
            about=[(label, name)],
        )
        iid = self._new_id("ins")
        now = self.now()
        self._write(
            "CREATE (:Insight {id:$iid, text:$text, valid_from:$now, source_event:$eid})",
            iid=iid, text=text, now=now, eid=eid,
        )
        res = self._write(
            "MATCH (i:Insight {id:$iid}) "
            "MATCH (e:Event) WHERE e.id IN $ids "
            "CREATE (i)-[:DERIVED_FROM]->(e)",
            iid=iid, ids=list(source_event_ids),
        )
        if res.relationships_created != len(source_event_ids):
            raise LookupError(
                f"expected {len(source_event_ids)} source events, "
                f"linked {res.relationships_created}"
            )
        # Loud, not silent. This MATCH finding nothing leaves an Insight
        # with its sources but no subject -- it exists, it is correct, and
        # `current_insights` cannot see it. Exactly the shape of bug 2.1,
        # and it happened: a consolidation of seven episodes landed
        # unreachable because the entity label did not match.
        res = self._write(
            f"MATCH (i:Insight {{id:$iid}}), (n:{label} {{{key}:$name}}) "
            f"CREATE (i)-[:ABOUT]->(n)",
            iid=iid, name=name,
        )
        if not res.relationships_created:
            raise LookupError(
                f"insight written but not attached: no {label} named "
                f"{name!r} to be about")
        return iid

    def current_insights(self, label=None, name=None):
        """Insights in force, optionally about one entity, with source counts."""
        if label:
            label = check_any_entity_label(label)
            key = "id" if label == "Decision" else "name"
            rows = self._read(
                f"MATCH (i:Insight)-[:ABOUT]->(n:{label} {{{key}:$name}}) "
                f"WHERE i.valid_to IS NULL "
                f"MATCH (i)-[d:DERIVED_FROM]->(:Event) "
                f"RETURN i.id, i.text, i.valid_from, count(d), "
                f"       coalesce(n.name, n.title, n.id) "
                f"ORDER BY i.valid_from DESC",
                name=name,
            )
        else:
            rows = self._read(
                "MATCH (i:Insight)-[:ABOUT]->(n) WHERE i.valid_to IS NULL "
                "MATCH (i)-[d:DERIVED_FROM]->(:Event) "
                "RETURN i.id, i.text, i.valid_from, count(d), "
                "       coalesce(n.name, n.title, n.id) "
                "ORDER BY i.valid_from DESC"
            )
        return [
            {"id": r[0], "text": r[1], "since": r[2], "sources": r[3], "about": r[4]}
            for r in rows
        ]

    def insight_sources(self, insight_id):
        """The episodes an insight was built from -- the 'show your work' read."""
        rows = self._read(
            "MATCH (:Insight {id:$iid})-[:DERIVED_FROM]->(e:Event) "
            "RETURN e.id, e.ts, e.summary ORDER BY e.ts, e.id",
            iid=insight_id,
        )
        return [{"id": r[0], "ts": r[1], "summary": r[2]} for r in rows]

    def retire_insight(self, session_id, agent, insight_id, reason):
        """Retire a consolidation that no longer holds."""
        eid = self.record_event(
            session_id, agent, "correction", f"Retired insight {insight_id}: {reason}"
        )
        res = self._write(
            "MATCH (i:Insight {id:$iid}) WHERE i.valid_to IS NULL "
            "SET i.valid_to = $now, i.retired_by = $eid "
            "RETURN count(i)",
            iid=insight_id, now=self.now(), eid=eid,
        )
        return bool(res.result_set and res.result_set[0][0])

    # -------------------------------------------------------------- coordination

    def create_task(self, session_id, agent, assigned_to, title,
                    concerns_decision=None, concerns_project=None,
                    concerns_topic=None, needs=None):
        """Hand work to another agent. The task IS the message.

        CONCERNS carries the context, so the claiming agent reads what the task
        is about from the graph rather than receiving a prompt.

        `concerns_topic` exists because the first two only reach the company
        world. A workspace built by conversation has no `Project` and no
        `Decision` node, so a task could carry nothing but its own title and
        `get_task_context` returned `decision: None, project: None` -- the
        claiming agent would have work it could not find the subject of, and
        the handoff would quietly degrade into a bare sentence.

        That is the same trap as `PROCEDURE_SCOPES` permitting only Project
        and System (docs/TESTING.md 3b) and `ENTITY_LABELS` for ABOUT: a
        closed list written for the seeded world, silently excluding the one
        people actually use.
        """
        eid = self.record_event(session_id, agent, "handoff", f"Created task: {title}")
        tid = self._new_id("task")
        # MERGE both agents, not MATCH. This was bug 2.1 again, still here
        # and this time deadlocking the whole handoff: in a workspace built
        # by conversation there is no `Agent {name:'Reviewer'}` node until
        # the Reviewer runs, so the MATCH found nothing, the CREATE silently
        # did not run, and this method returned an id for a task that did
        # not exist. The Reviewer never ran because no task was waiting;
        # no task was waiting because the Reviewer had never run.
        #
        # An agent is identified by its name alone, so bringing one into
        # being when work is first assigned to it is correct rather than
        # merely convenient.
        res = self._write(
            "MERGE (creator:Agent {name:$agent}) "
            "MERGE (assignee:Agent {name:$assignee}) "
            "CREATE (t:Task {id:$tid, title:$title, status:'open', "
            "                created_at:$now, source_event:$eid}) "
            "CREATE (creator)-[:CREATED]->(t) "
            "CREATE (t)-[:ASSIGNED_TO]->(assignee)",
            agent=agent, assignee=assigned_to, tid=tid, title=title,
            now=self.now(), eid=eid,
        )
        if res.relationships_created != 2:
            raise LookupError(
                f"could not create task {title!r}: nothing was written")
        if concerns_decision:
            self._write(
                "MATCH (t:Task {id:$tid}), (d:Decision {id:$did}) "
                "CREATE (t)-[:CONCERNS]->(d)",
                tid=tid, did=concerns_decision,
            )
        if concerns_project:
            self._write(
                "MATCH (t:Task {id:$tid}), (p:Project {name:$project}) "
                "CREATE (t)-[:CONCERNS]->(p)",
                tid=tid, project=concerns_project,
            )
        # If the caller named an agent but not a capability, ask the graph
        # what that agent handles. Told to attach `needs`, a live model
        # simply did not -- producing a task that no capability match could
        # ever claim. Deriving it is the structural answer: the roster
        # already knows, so the handoff does not depend on the model
        # remembering an argument.
        if not needs and assigned_to:
            handled = self._read(
                "MATCH (:Agent {name:$name})-[:HANDLES]->(c:Capability) "
                "RETURN c.name", name=assigned_to)
            if len(handled) == 1:
                needs = handled[0][0]
        # Likewise the subject. Note this is decided on *outcome*, further
        # down, not on whether an argument was passed: a live model handed
        # `concerns_project="lift controller"` in a workspace that has no
        # Project nodes at all, the MATCH found nothing, and the task was
        # left about nothing while looking correctly filled in. Checking
        # the argument would have accepted that; checking whether an edge
        # was actually created does not.
        if needs:
            # What the work requires, rather than only who should do it.
            # An agent then claims what it can do -- see claim_task.
            self._write(
                "MATCH (t:Task {id:$tid}) MERGE (c:Capability {name:$cap}) "
                "ON CREATE SET c.description = $desc "
                "MERGE (t)-[:NEEDS]->(c)",
                tid=tid, cap=check_capability(needs),
                desc=CAPABILITIES[needs])
        if concerns_topic:
            # MERGE, not MATCH: the Planner may hand off a subject the
            # curator has not written yet, and a task pointing at nothing is
            # the failure this argument exists to prevent.
            self._write(
                "MATCH (t:Task {id:$tid}) MERGE (tp:Topic {name:$topic}) "
                "ON CREATE SET tp.first_seen = $now "
                "MERGE (t)-[:CONCERNS]->(tp)",
                tid=tid, topic=self._clean_topic(concerns_topic),
                now=self.now(),
            )

        # Did anything actually attach? The three clauses above all match on
        # something that may not exist, and a task about nothing cannot be
        # read by the agent that claims it. Fall back to what the
        # conversation is about, then to the subject named in the title --
        # resolve_topic already forgives loose wording, so this matches what
        # the workspace holds rather than coining a new subject.
        attached = self._read(
            "MATCH (:Task {id:$tid})-[:CONCERNS]->() RETURN count(*)",
            tid=tid)
        if not (attached and attached[0][0]):
            fallback = (self.session_topics(session_id, limit=1) or [None])[0]
            fallback = fallback or self.resolve_topic(title)
            if fallback:
                self._write(
                    "MATCH (t:Task {id:$tid}) MERGE (tp:Topic {name:$topic}) "
                    "ON CREATE SET tp.first_seen = $now "
                    "MERGE (t)-[:CONCERNS]->(tp)",
                    tid=tid, topic=self._clean_topic(fallback),
                    now=self.now())
        return tid

    def claim_task(self, agent):
        """Atomically claim the oldest open task assigned to this agent.

        ONE query that both tests and sets the status. FalkorDB serializes
        writes per graph and each write is atomic, but there are no multi-query
        transactions -- so a read followed by a separate write would lose
        updates. Measured: 16 concurrent clients using read-then-write all
        believed they had won.

        `WITH t ... LIMIT 1` before the SET is load-bearing. A trailing LIMIT
        would NOT constrain the write and would claim every open task at once.

        An agent claims work **addressed to it by name or matching something
        it can do**. The second half is what makes this coordination rather
        than messaging: a task says what it needs, and whichever agent
        handles that takes it. Adding an agent that handles `review` means
        review work can go to it without anything that files tasks being
        changed.

        The capability arm is a pattern predicate inside the same `WHERE`,
        verified on v6.0.2, so this is still **one** query that both tests
        and sets -- the property the whole handoff rests on.

        Returns the claimed task's id, or None if there was nothing to claim.
        """
        res = self._write(
            "MATCH (t:Task) WHERE t.status = 'open' AND ( "
            "  (t)-[:ASSIGNED_TO]->(:Agent {name:$agent}) OR "
            "  (t)-[:NEEDS]->(:Capability)<-[:HANDLES]-(:Agent {name:$agent}) ) "
            "WITH t ORDER BY t.created_at, t.id LIMIT 1 "
            "SET t.status = 'claimed', t.claimed_by = $agent, t.claimed_at = $now "
            "RETURN t.id AS id",
            agent=agent, now=self.now(),
        )
        return res.result_set[0][0] if res.result_set else None

    def get_task_context(self, task_id):
        """What a claimed task is about, read entirely from the graph.

        This is the whole handoff. The claiming agent is told one fixed
        sentence naming nothing, so everything it acts on comes from here
        and from what it reads next.

        A topic-scoped task also returns what has already been settled on
        that subject. Without it the Reviewer gets a subject and has to
        guess the route in; with it the first read already contains the
        thing it was asked to check against.
        """
        rows = self._read(
            "MATCH (t:Task {id:$tid}) "
            "OPTIONAL MATCH (t)-[:CONCERNS]->(d:Decision) "
            "OPTIONAL MATCH (t)-[:CONCERNS]->(p:Project) "
            "OPTIONAL MATCH (t)-[:CONCERNS]->(tp:Topic) "
            "OPTIONAL MATCH (creator:Agent)-[:CREATED]->(t) "
            "RETURN t.id, t.title, t.status, t.created_at, "
            "       d.id, d.title, d.reason, p.name, creator.name, tp.name",
            tid=task_id,
        )
        if not rows:
            return None
        r = rows[0]
        out = {
            "id": r[0], "title": r[1], "status": r[2], "created_at": r[3],
            "decision": {"id": r[4], "title": r[5], "reason": r[6]} if r[4] else None,
            "project": r[7],
            "created_by": r[8],
            "topic": r[9],
        }
        if r[9]:
            out["settled_so_far"] = [
                {"settled": x["settled"], "status": x["status"]}
                for x in self.settlements(topic=r[9], limit=5)
            ]
        return out

    def complete_task(self, session_id, agent, task_id, result):
        """Close a claimed task and leave the outcome in the episodic record."""
        eid = self.record_event(
            session_id, agent, "handoff", f"Completed task {task_id}: {result}"
        )
        res = self._write(
            "MATCH (t:Task {id:$tid}) WHERE t.status = 'claimed' "
            "SET t.status = 'done', t.completed_at = $now, "
            "    t.result = $result, t.completed_event = $eid",
            tid=task_id, now=self.now(), result=result, eid=eid,
        )
        return bool(res.properties_set)

    def open_tasks(self, agent=None):
        if agent:
            rows = self._read(
                "MATCH (t:Task)-[:ASSIGNED_TO]->(a:Agent {name:$agent}) "
                "WHERE t.status = 'open' "
                "RETURN t.id, t.title, t.created_at ORDER BY t.created_at, t.id",
                agent=agent,
            )
        else:
            rows = self._read(
                "MATCH (t:Task) WHERE t.status = 'open' "
                "RETURN t.id, t.title, t.created_at ORDER BY t.created_at, t.id"
            )
        return [{"id": r[0], "title": r[1], "created_at": r[2]} for r in rows]

    # -------------------------------------------------------------------- action

    def create_ticket(self, session_id, agent, title, blocks_project, cites_decision=None):
        """File a ticket: the one Track 01 action.

        CITES is what keeps the ticket from being an orphan assertion -- it
        makes the chain ticket -> decision -> event -> session -> agent
        traversable, which is the explainability claim.
        """
        eid = self.record_event(
            session_id, agent, "action", f"Filed ticket: {title}",
            about=[("Project", blocks_project)],
        )
        tid = self._new_id("tick")
        self._write(
            "MATCH (a:Agent {name:$agent}), (p:Project {name:$project}) "
            "CREATE (t:Ticket {id:$tid, title:$title, created_at:$now, source_event:$eid}) "
            "CREATE (a)-[:FILED]->(t) "
            "CREATE (t)-[:BLOCKS]->(p)",
            agent=agent, project=blocks_project, tid=tid, title=title,
            now=self.now(), eid=eid,
        )
        if cites_decision:
            self._write(
                "MATCH (t:Ticket {id:$tid}), (d:Decision {id:$did}) "
                "CREATE (t)-[:CITES]->(d)",
                tid=tid, did=cites_decision,
            )
        return tid

    # --------------------------------------------------------------- explanation

    def explain_path(self, from_label, from_name, to_label, to_name, max_hops=6):
        """Shortest connection between two entities, as a readable chain.

        shortestPath() is only accepted in WITH or RETURN in FalkorDB, never in
        MATCH -- so the endpoints are matched first and the path computed after.
        """
        check_label(from_label, ENTITY_LABELS, "entity label")
        check_label(to_label, ENTITY_LABELS, "entity label")
        from_key = "id" if from_label == "Decision" else "name"
        to_key = "id" if to_label == "Decision" else "name"
        rows = self._read(
            f"MATCH (a:{from_label} {{{from_key}:$from_name}}), "
            f"      (b:{to_label} {{{to_key}:$to_name}}) "
            f"WITH shortestPath((a)-[*..{int(max_hops)}]->(b)) AS p "
            f"WHERE p IS NOT NULL "
            f"RETURN [n IN nodes(p) | coalesce(n.name, n.title, n.id)] AS chain, "
            f"       [r IN relationships(p) | type(r)] AS steps, "
            f"       length(p) AS hops",
            from_name=from_name, to_name=to_name,
        )
        if not rows:
            return None
        return {"chain": rows[0][0], "steps": rows[0][1], "hops": rows[0][2]}

    def depends_on_chain(self, project, max_hops=4):
        """Everything a project transitively depends on, current edges only.

        relationships(path) is required here: a variable-length relationship
        variable binds to a Path in FalkorDB, not a List, so all(r IN d ...)
        would be a type error.
        """
        rows = self._read(
            f"MATCH path = (:Project {{name:$project}})-[:DEPENDS_ON*1..{int(max_hops)}]->"
            f"(t:Project) "
            f"WHERE all(r IN relationships(path) WHERE r.valid_to IS NULL) "
            f"RETURN t.name, length(path) AS hops ORDER BY hops, t.name",
            project=project,
        )
        return [{"project": r[0], "hops": r[1]} for r in rows]

    def critical_projects(self, limit=5):
        """Projects that most often sit between other projects, by betweenness.

        This is the one graph algorithm the project uses (Track 01). It
        answers "what is the single point of failure?" -- a node with high
        betweenness is on many shortest paths, so delaying it propagates
        furthest.

        Scoped to Project nodes and DEPENDS_ON edges on purpose. Run over the
        whole graph it also scores Events and Sessions, which is noise.

        Two limitations, both real, both reported rather than hidden:

        **It answers *what*, not *who*.** `Person` has no incoming edges in
        this schema -- OWNS and WORKED_ON point from a person into a project
        -- so no shortest path passes through a person and every person
        scores exactly 0. To name the people exposed, follow up with
        current_facts('WORKED_ON', to_name=...) on whatever this returns.

        **It cannot see validity windows.** The algorithm walks graph
        structure, and retiring a fact sets `valid_to` rather than deleting
        the edge, so a retired DEPENDS_ON still contributes to the score.
        Every other read in this class filters on `valid_to IS NULL`; this
        one structurally cannot. So the count of retired dependency edges is
        returned alongside the ranking: while it is zero the scores are
        exactly current, and when it is not the caller knows the ranking is
        reading history too. Nothing in the demo script retires a
        DEPENDS_ON edge, so in practice it stays zero -- but silently
        returning a stale ranking would be the wrong failure mode.
        """
        rows = self._read(
            "CALL algo.betweenness({nodeLabels:['Project'], "
            "                       relationshipTypes:['DEPENDS_ON']}) "
            "YIELD node, score WHERE score > 0 "
            "RETURN node.name, score ORDER BY score DESC, node.name LIMIT $limit",
            limit=limit,
        )
        retired = self._read(
            "MATCH ()-[r:DEPENDS_ON]->() WHERE r.valid_to IS NOT NULL "
            "RETURN count(r)"
        )[0][0]
        return {
            "ranking": [{"project": r[0], "score": r[1]} for r in rows],
            "retired_dependency_edges": retired,
            "caveat": ("scores include retired dependency edges; the algorithm "
                       "cannot filter on validity" if retired else None),
        }

    def dependents_of(self, project, max_hops=4):
        """Projects that transitively depend ON this one -- the reverse walk.

        Both directions are needed and they answer different questions.
        depends_on_chain answers "can we ship X?" (what is X waiting for).
        This answers "what breaks if X slips?", which is what the Reviewer
        needs when a decision lands on a project other things sit behind.
        """
        rows = self._read(
            f"MATCH path = (d:Project)-[:DEPENDS_ON*1..{int(max_hops)}]->"
            f"(:Project {{name:$project}}) "
            f"WHERE all(r IN relationships(path) WHERE r.valid_to IS NULL) "
            f"RETURN d.name, length(path) AS hops ORDER BY hops, d.name",
            project=project,
        )
        return [{"project": r[0], "hops": r[1]} for r in rows]

    # ------------------------------------------------------------ reasoning trace
    #
    # Mutating tools already leave an Event behind, so what an agent *changed*
    # has always been traceable. What it *read* was not, and reads are what
    # decide an answer. These three methods close that gap: each read becomes
    # a Lookup node, the nodes it returned become RETURNED edges, and the
    # answer the agent finally gave points at the lookups behind it.
    #
    # The trace is deliberately NOT versioned. A citation is episodic -- the
    # agent did read that fact at that moment, and that never stops having
    # happened. Leaving valid_from off is also what keeps citations out of
    # recall_related, whose fact queries require `r.valid_from IS NOT NULL`
    # (verified: a RETURNED edge is invisible to them).

    # A single lookup cites at most this many nodes. A wide recall can mention
    # dozens, and past a handful the trace stops being an explanation and
    # becomes a second copy of the result.
    MAX_CITATIONS = 12

    def record_lookup(self, session_id, agent, tool, arguments, keys=()):
        """Record one read an agent made, and which nodes came back.

        `keys` are names and ids lifted from the tool's result. They are
        resolved against the graph rather than trusted: the citation exists
        only if a node actually bears that key, so the trace cannot claim a
        fact the graph does not hold.

        One query does both halves. `CREATE ... WITH l UNWIND $keys ...`
        still creates the Lookup when `keys` is empty (verified against
        FalkorDB v6.0.2), which matters more than it looks -- a read that
        found nothing is evidence for an answer like "no, nothing clashes",
        and it has to be recorded to be cited.

        The match is unlabelled, so it cannot use a label index and scans
        once per key. At demo scale (tens to hundreds of nodes) that is
        nothing; on a large graph it would want narrowing.
        """
        lid = self._new_id("look")
        # dict.fromkeys dedupes while keeping the order the tool returned.
        cited = [k for k in dict.fromkeys(keys) if k][:self.MAX_CITATIONS]
        self._write(
            "CREATE (l:Lookup {id:$lid, tool:$tool, arguments:$args, "
            "                  ts:$ts, agent:$agent, session:$sid}) "
            "WITH l UNWIND $keys AS k "
            "MATCH (n) WHERE (n.name = k OR n.id = k) AND NOT n:Lookup "
            "CREATE (l)-[:RETURNED]->(n)",
            lid=lid, tool=tool, args=arguments, ts=self.now(),
            agent=agent, sid=session_id, keys=cited,
        )
        return lid

    def link_reasoning(self, event_id, lookup_ids):
        """Point an agent's answer at the lookups that produced it."""
        ids = [i for i in lookup_ids if i]
        if not ids:
            return 0
        res = self._write(
            "MATCH (e:Event {id:$eid}) MATCH (l:Lookup) WHERE l.id IN $lids "
            "CREATE (e)-[:READ]->(l) RETURN count(l)",
            eid=event_id, lids=ids,
        )
        return res.result_set[0][0] if res.result_set else 0

    def find_answer(self, about=None):
        """The most recent agent answer that has a trace, optionally about one thing.

        With `about`, the graph does the finding: an answer qualifies because
        one of its lookups returned a node with that name or id. So "why did
        you block the billing release?" resolves through the citation edges
        rather than by searching the text of what was said.
        """
        if about:
            rows = self._read(
                "MATCH (e:Event)-[:READ]->(:Lookup)-[:RETURNED]->(x) "
                "WHERE x.name = $about OR x.id = $about "
                "RETURN DISTINCT e.id, e.ts, e.summary, e.speaker "
                "ORDER BY e.ts DESC, e.id DESC LIMIT 1",
                about=about,
            )
        else:
            rows = self._read(
                "MATCH (e:Event)-[:READ]->(:Lookup) "
                "RETURN DISTINCT e.id, e.ts, e.summary, e.speaker "
                "ORDER BY e.ts DESC, e.id DESC LIMIT 1"
            )
        if not rows:
            return None
        r = rows[0]
        return {"id": r[0], "ts": r[1], "text": r[2], "speaker": r[3]}

    def explain_answer(self, about=None):
        """Why an agent answered as it did: every read, in order, and its results.

        This is the whole point of the trace. It is a traversal of the graph,
        not a replay of anything held in the UI or the process -- so it
        survives a reload, a restart, and a later session, and the agent can
        read it back about itself.
        """
        answer = self.find_answer(about)
        if not answer:
            return None
        rows = self._read(
            "MATCH (e:Event {id:$eid})-[:READ]->(l:Lookup) "
            "OPTIONAL MATCH (l)-[:RETURNED]->(n) "
            "RETURN l.tool, l.arguments, "
            "       collect(labels(n)[0] + ':' + coalesce(n.name, n.title, n.id)) "
            "ORDER BY l.ts, l.id",
            eid=answer["id"],
        )
        steps = []
        for tool, args, returned in rows:
            try:
                parsed = json.loads(args) if args else {}
            except (TypeError, ValueError):
                parsed = {"raw": args}
            steps.append({"tool": tool, "arguments": parsed,
                          "returned": returned})
        return {"answer": answer, "steps": steps}
