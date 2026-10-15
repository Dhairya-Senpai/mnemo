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

import time
import uuid
from datetime import datetime, timezone

from falkordb import FalkorDB

from .schema import (
    ENTITY_LABELS,
    EVENT_KINDS,
    FACT_ENDPOINTS,
    PROCEDURE_SCOPES,
    check_fact_type,
    check_label,
)


def utc_now():
    """Second-precision ISO-8601 UTC, e.g. 2026-10-15T14:30:00Z."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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

    # ----------------------------------------------------------------- plumbing

    def _read(self, q, **params):
        self.reads += 1
        return self._g.ro_query(q, params=params).result_set

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

    # ------------------------------------------------------------------ episodic

    def start_session(self, user, session_id=None, started_at=None):
        """Open an episodic container. Everything else hangs off a session."""
        sid = session_id or self._new_id("sess")
        self._write(
            "CREATE (:Session {id:$id, started_at:$started_at, user:$user})",
            id=sid, started_at=started_at or self.now(), user=user,
        )
        return sid

    def record_event(self, session_id, agent, kind, summary, about=None, ts=None):
        """Write one episodic event and wire it to its session, agent and subjects.

        `about` is a list of (label, name) pairs naming the entities the event
        concerned; those ABOUT edges are what recall_related traverses later.
        """
        if kind not in EVENT_KINDS:
            raise ValueError(f"unknown event kind {kind!r}; expected one of {EVENT_KINDS}")
        eid = self._new_id("evt")
        self._write(
            "MATCH (s:Session {id:$sid}), (a:Agent {name:$agent}) "
            "CREATE (e:Event {id:$eid, ts:$ts, kind:$kind, summary:$summary}) "
            "CREATE (s)-[:HAS_EVENT]->(e) "
            "CREATE (a)-[:PERFORMED]->(e)",
            sid=session_id, agent=agent, eid=eid,
            ts=ts or self.now(), kind=kind, summary=summary,
        )
        for label, name in about or []:
            check_label(label, ENTITY_LABELS, "ABOUT target")
            key = "id" if label == "Decision" else "name"
            self._write(
                f"MATCH (e:Event {{id:$eid}}), (n:{label} {{{key}:$name}}) "
                f"CREATE (e)-[:ABOUT]->(n)",
                eid=eid, name=name,
            )
        return eid

    def recall_recent(self, limit=10, session_id=None):
        """Most recent events, newest first. Episodic recall across sessions.

        Ordering is (ts, id) descending because two events can share a second.
        """
        if session_id:
            rows = self._read(
                "MATCH (s:Session {id:$sid})-[:HAS_EVENT]->(e:Event) "
                "MATCH (a:Agent)-[:PERFORMED]->(e) "
                "RETURN e.id, e.ts, e.kind, e.summary, a.name, s.id "
                "ORDER BY e.ts DESC, e.id DESC LIMIT $limit",
                sid=session_id, limit=limit,
            )
        else:
            rows = self._read(
                "MATCH (s:Session)-[:HAS_EVENT]->(e:Event) "
                "MATCH (a:Agent)-[:PERFORMED]->(e) "
                "RETURN e.id, e.ts, e.kind, e.summary, a.name, s.id "
                "ORDER BY e.ts DESC, e.id DESC LIMIT $limit",
                limit=limit,
            )
        return [
            {"id": r[0], "ts": r[1], "kind": r[2], "summary": r[3],
             "agent": r[4], "session": r[5]}
            for r in rows
        ]

    def recall_related(self, label, name, limit=20):
        """Everything the graph currently knows about one entity.

        Returns episodes that mentioned it plus the world facts it currently
        participates in. This is the main multi-hop read and the Track 01
        "graph query as agent tool".
        """
        check_label(label, ENTITY_LABELS, "entity label")
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

        return {
            "entity": {"label": label, "name": name},
            "insights": [
                {"id": r[0], "text": r[1], "since": r[2]} for r in insights
            ],
            "events": [
                {"id": r[0], "ts": r[1], "kind": r[2], "summary": r[3]} for r in events
            ],
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
        """
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

    def get_applicable_procedures(self, label, name):
        """Rules in force for an entity, directly or via the project's systems.

        The second hop is what makes the taught rule fire without being
        repeated: a rule scoped to a finance System also applies to any Project
        that currently USES it.
        """
        check_label(label, PROCEDURE_SCOPES, "procedure scope")
        direct = self._read(
            f"MATCH (p:Procedure)-[s:APPLIES_TO]->(n:{label} {{name:$name}}) "
            f"WHERE p.valid_to IS NULL AND s.valid_to IS NULL "
            f"RETURN p.id, p.name, p.trigger, p.action, 'direct'",
            name=name,
        )
        indirect = []
        if label == "Project":
            indirect = self._read(
                "MATCH (p:Procedure)-[s:APPLIES_TO]->(sys:System)<-[u:USES]-"
                "(proj:Project {name:$name}) "
                "WHERE p.valid_to IS NULL AND s.valid_to IS NULL AND u.valid_to IS NULL "
                "RETURN p.id, p.name, p.trigger, p.action, 'via system ' + sys.name",
                name=name,
            )
        seen, out = set(), []
        for r in list(direct) + list(indirect):
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
        """
        nodes = self._read(
            "MATCH (n) RETURN ID(n), labels(n)[0], "
            "coalesce(n.name, n.title, n.text, n.id), n.valid_to"
        )
        edges = self._read(
            "MATCH (a)-[r]->(b) WHERE ID(r) >= 0 "
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
        """The episodes that mention an entity, oldest first (reading order)."""
        check_label(label, ENTITY_LABELS, "entity label")
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
        check_label(label, ENTITY_LABELS, "entity label")
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
        self._write(
            f"MATCH (i:Insight {{id:$iid}}), (n:{label} {{{key}:$name}}) "
            f"CREATE (i)-[:ABOUT]->(n)",
            iid=iid, name=name,
        )
        return iid

    def current_insights(self, label=None, name=None):
        """Insights in force, optionally about one entity, with source counts."""
        if label:
            check_label(label, ENTITY_LABELS, "entity label")
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
                    concerns_decision=None, concerns_project=None):
        """Hand work to another agent. The task IS the message.

        CONCERNS carries the context, so the claiming agent reads what the task
        is about from the graph rather than receiving a prompt.
        """
        eid = self.record_event(session_id, agent, "handoff", f"Created task: {title}")
        tid = self._new_id("task")
        self._write(
            "MATCH (creator:Agent {name:$agent}), (assignee:Agent {name:$assignee}) "
            "CREATE (t:Task {id:$tid, title:$title, status:'open', "
            "                created_at:$now, source_event:$eid}) "
            "CREATE (creator)-[:CREATED]->(t) "
            "CREATE (t)-[:ASSIGNED_TO]->(assignee)",
            agent=agent, assignee=assigned_to, tid=tid, title=title,
            now=self.now(), eid=eid,
        )
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

        Returns the claimed task's id, or None if there was nothing to claim.
        """
        res = self._write(
            "MATCH (t:Task)-[:ASSIGNED_TO]->(:Agent {name:$agent}) "
            "WHERE t.status = 'open' "
            "WITH t ORDER BY t.created_at, t.id LIMIT 1 "
            "SET t.status = 'claimed', t.claimed_by = $agent, t.claimed_at = $now "
            "RETURN t.id AS id",
            agent=agent, now=self.now(),
        )
        return res.result_set[0][0] if res.result_set else None

    def get_task_context(self, task_id):
        """What a claimed task is about, read entirely from the graph."""
        rows = self._read(
            "MATCH (t:Task {id:$tid}) "
            "OPTIONAL MATCH (t)-[:CONCERNS]->(d:Decision) "
            "OPTIONAL MATCH (t)-[:CONCERNS]->(p:Project) "
            "OPTIONAL MATCH (creator:Agent)-[:CREATED]->(t) "
            "RETURN t.id, t.title, t.status, t.created_at, "
            "       d.id, d.title, d.reason, p.name, creator.name",
            tid=task_id,
        )
        if not rows:
            return None
        r = rows[0]
        return {
            "id": r[0], "title": r[1], "status": r[2], "created_at": r[3],
            "decision": {"id": r[4], "title": r[5], "reason": r[6]} if r[4] else None,
            "project": r[7],
            "created_by": r[8],
        }

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
