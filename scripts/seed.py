"""Seed the tenant memory graphs from the structured world files in data/.

Usage:
    python scripts/seed.py                      # seed every tenant
    python scripts/seed.py --tenant acme        # just one
    python scripts/seed.py --host <host> --port <port>

Each run DROPS the tenant's graph and rebuilds it, so seeding is repeatable
and the demo always starts from a known state.

What this does NOT seed, on purpose: the delay Decision (demo moment 1), the
Task handoff (moment 2), the Ticket (moment 3), the taught Procedure
(moment 4), and the ownership transfer (moment 5b). Those are created live by
the agents. Seeding them would hollow out the demo.

Schema reference: docs/SCHEMA.md (that file is the source of truth).
"""

import argparse
import json
import sys
from pathlib import Path

from falkordb import FalkorDB

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
sys.path.insert(0, str(ROOT / "src"))

# FACT_ENDPOINTS and INDEXES have one definition, shared with the memory
# tools, so the seeder and the runtime cannot drift apart. Relationship types
# and labels are interpolated into query strings (Cypher cannot parameterise
# either), so they must come from that table and never from the data file.
# Values always go through parameters.
from memory.schema import FACT_ENDPOINTS, INDEXES  # noqa: E402
from memory.store import DEFAULT_HOST, DEFAULT_PORT  # noqa: E402

# Properties carried by every versioned fact. `valid_to` is deliberately
# absent while a fact is current -- FalkorDB cannot store null, so an absent
# key is how "still true" is represented. See docs/SCHEMA.md section 2.
VERSIONED_PROPS = ("valid_from", "valid_to", "source_event")


def validate(world, path):
    """Catch typos in the data file before touching the database.

    A misspelled name would otherwise MATCH nothing, and CREATE would
    silently produce no edge -- a seeded graph that is quietly wrong is far
    worse than one that refuses to load.
    """
    errors = []
    names = {
        "Person":  {p["name"] for p in world.get("people", [])},
        "Project": {p["name"] for p in world.get("projects", [])},
        "System":  {s["name"] for s in world.get("systems", [])},
    }
    event_ids = {e["id"] for e in world.get("events", [])}
    session_ids = {s["id"] for s in world.get("sessions", [])}
    agent_names = {a["name"] for a in world.get("agents", [])}

    for e in world.get("events", []):
        if e["session"] not in session_ids:
            errors.append(f"event {e['id']}: unknown session {e['session']!r}")
        if e["agent"] not in agent_names:
            errors.append(f"event {e['id']}: unknown agent {e['agent']!r}")

    for d in world.get("decisions", []):
        if d["source_event"] not in event_ids:
            errors.append(f"decision {d['id']}: unknown source_event {d['source_event']!r}")
        if d["recorded_in"] not in event_ids:
            errors.append(f"decision {d['id']}: unknown recorded_in {d['recorded_in']!r}")
        if d["made_by"] not in names["Person"]:
            errors.append(f"decision {d['id']}: unknown person {d['made_by']!r}")
        for proj in d.get("affects", []):
            if proj not in names["Project"]:
                errors.append(f"decision {d['id']}: unknown project {proj!r}")

    for i, f in enumerate(world.get("facts", [])):
        if f["type"] not in FACT_ENDPOINTS:
            errors.append(f"fact[{i}]: unknown type {f['type']!r}")
            continue
        from_label, to_label = FACT_ENDPOINTS[f["type"]]
        if from_label not in names or to_label not in names:
            # AFFECTS lives in FACT_ENDPOINTS but is keyed by Decision.id, so
            # it is declared inside "decisions", not in the flat facts list.
            errors.append(
                f"fact[{i}] {f['type']}: endpoints {from_label}/{to_label} are not "
                f"name-keyed; declare this one under 'decisions' instead"
            )
            continue
        if f["from"] not in names[from_label]:
            errors.append(f"fact[{i}] {f['type']}: unknown {from_label} {f['from']!r}")
        if f["to"] not in names[to_label]:
            errors.append(f"fact[{i}] {f['type']}: unknown {to_label} {f['to']!r}")
        if f["source_event"] not in event_ids:
            errors.append(f"fact[{i}] {f['type']}: unknown source_event {f['source_event']!r}")

    for a in world.get("event_about", []):
        if a["event"] not in event_ids:
            errors.append(f"event_about: unknown event {a['event']!r}")
        if a["name"] not in names.get(a["label"], set()):
            errors.append(f"event_about: unknown {a['label']} {a['name']!r}")

    if errors:
        print(f"\n{path.name} failed validation:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        raise SystemExit(1)


def seed_tenant(db, world, path):
    name = world["graph"]
    print(f"\n=== {name} ===")

    # Drop and rebuild, so a reseed always lands on a known state.
    try:
        db.select_graph(name).delete()
        print("  dropped existing graph")
    except Exception:
        print("  no existing graph to drop")
    g = db.select_graph(name)

    # --- nodes ---------------------------------------------------------
    for a in world["agents"]:
        g.query("CREATE (:Agent {name:$name, role:$role})", params=a)

    for p in world["people"]:
        g.query(
            "CREATE (:Person {name:$name, role:$role, aliases:$aliases})",
            params={"name": p["name"], "role": p["role"], "aliases": p.get("aliases", [])},
        )

    for p in world["projects"]:
        # No `status` property by design -- project state is derived from the
        # currently-valid decisions that affect it (docs/SCHEMA.md section 3).
        g.query("CREATE (:Project {name:$name})", params={"name": p["name"]})

    for s in world["systems"]:
        g.query("CREATE (:System {name:$name})", params={"name": s["name"]})

    for s in world["sessions"]:
        g.query(
            "CREATE (:Session {id:$id, started_at:$started_at, user:$user})",
            params={"id": s["id"], "started_at": s["started_at"], "user": s["user"]},
        )

    for e in world["events"]:
        g.query(
            "CREATE (:Event {id:$id, ts:$ts, kind:$kind, summary:$summary})",
            params={"id": e["id"], "ts": e["ts"], "kind": e["kind"], "summary": e["summary"]},
        )
        # Episodic wiring: which session the event belongs to, and who acted.
        g.query(
            "MATCH (s:Session {id:$sid}), (e:Event {id:$eid}) CREATE (s)-[:HAS_EVENT]->(e)",
            params={"sid": e["session"], "eid": e["id"]},
        )
        g.query(
            "MATCH (a:Agent {name:$agent}), (e:Event {id:$eid}) CREATE (a)-[:PERFORMED]->(e)",
            params={"agent": e["agent"], "eid": e["id"]},
        )

    for d in world["decisions"]:
        props = {k: d[k] for k in ("id", "title", "reason", "valid_from", "source_event")}
        g.query(
            "CREATE (:Decision {id:$id, title:$title, reason:$reason, "
            "valid_from:$valid_from, source_event:$source_event})",
            params=props,
        )
        g.query(
            "MATCH (p:Person {name:$person}), (d:Decision {id:$did}) CREATE (p)-[:MADE]->(d)",
            params={"person": d["made_by"], "did": d["id"]},
        )
        g.query(
            "MATCH (d:Decision {id:$did}), (e:Event {id:$eid}) CREATE (d)-[:RECORDED_IN]->(e)",
            params={"did": d["id"], "eid": d["recorded_in"]},
        )
        for proj in d.get("affects", []):
            # AFFECTS is a versioned world fact, so it carries a validity window
            # of its own: a decision's scope can narrow without the decision
            # itself being retired.
            g.query(
                "MATCH (d:Decision {id:$did}), (p:Project {name:$proj}) "
                "CREATE (d)-[:AFFECTS {valid_from:$valid_from, source_event:$src}]->(p)",
                params={"did": d["id"], "proj": proj,
                        "valid_from": d["valid_from"], "src": d["source_event"]},
            )

    # --- versioned world facts ------------------------------------------
    for f in world["facts"]:
        from_label, to_label = FACT_ENDPOINTS[f["type"]]
        present = [k for k in VERSIONED_PROPS if f.get(k) is not None]
        # Only emit keys that are actually present. An absent `valid_to` is
        # what makes a fact current; writing a null would be a type error.
        prop_clause = ", ".join(f"{k}:${k}" for k in present)
        params = {k: f[k] for k in present}
        params["from_name"] = f["from"]
        params["to_name"] = f["to"]
        g.query(
            f"MATCH (a:{from_label} {{name:$from_name}}), (b:{to_label} {{name:$to_name}}) "
            f"CREATE (a)-[:{f['type']} {{{prop_clause}}}]->(b)",
            params=params,
        )

    # --- episodic links to entities --------------------------------------
    for a in world.get("event_about", []):
        label = a["label"]
        if label not in ("Person", "Project", "System"):
            raise SystemExit(f"event_about: unsupported label {label!r}")
        g.query(
            f"MATCH (e:Event {{id:$eid}}), (n:{label} {{name:$name}}) "
            f"CREATE (e)-[:ABOUT]->(n)",
            params={"eid": a["event"], "name": a["name"]},
        )

    # --- indexes ----------------------------------------------------------
    for label, prop in INDEXES:
        g.query(f"CREATE INDEX FOR (n:{label}) ON (n.{prop})")

    return g


def summarise(g):
    counts = {}
    for label in ("Agent", "Person", "Project", "System", "Session",
                  "Event", "Decision", "Task", "Ticket", "Procedure", "Insight"):
        n = g.ro_query(f"MATCH (n:{label}) RETURN count(n)").result_set[0][0]
        if n:
            counts[label] = n
    total_nodes = g.ro_query("MATCH (n) RETURN count(n)").result_set[0][0]

    # Every edge alias is referenced in the RETURN, because an unreferenced
    # alias is matched existentially and under-reports parallel edges.
    # See docs/SCHEMA.md section 6, consequence 1.
    rels = {}
    for rtype in ("HAS_EVENT", "PERFORMED", "ABOUT", "MADE", "RECORDED_IN",
                  "AFFECTS", "OWNS", "WORKED_ON", "USES", "DEPENDS_ON"):
        n = g.ro_query(f"MATCH ()-[r:{rtype}]->() RETURN count(r)").result_set[0][0]
        if n:
            rels[rtype] = n
    total_rels = sum(rels.values())

    print(f"  nodes ({total_nodes}): " + ", ".join(f"{k}={v}" for k, v in counts.items()))
    print(f"  edges ({total_rels}): " + ", ".join(f"{k}={v}" for k, v in rels.items()))
    idx = g.ro_query("CALL db.indexes()").result_set
    print(f"  indexes: {len(idx)}")
    return total_nodes, total_rels


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--tenant", help="seed only this tenant (e.g. acme)")
    args = ap.parse_args()

    files = sorted(DATA_DIR.glob("world_*.json"))
    if args.tenant:
        files = [f for f in files if f.stem == f"world_{args.tenant}"]
    if not files:
        raise SystemExit(f"no world files found in {DATA_DIR}")

    db = FalkorDB(host=args.host, port=args.port)
    grand_nodes = grand_rels = 0
    for path in files:
        world = json.loads(path.read_text(encoding="utf-8"))
        validate(world, path)
        g = seed_tenant(db, world, path)
        n, r = summarise(g)
        grand_nodes += n
        grand_rels += r

    print(f"\nseeded {len(files)} tenant(s): {grand_nodes} nodes, {grand_rels} edges total")


if __name__ == "__main__":
    main()
