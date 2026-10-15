"""Steps 7 and 8: the algorithm tool and the MCP server.

    python tests/test_mcp_and_algorithm.py      # needs FalkorDB on localhost:6379

Covers the one graph algorithm (betweenness), the MCP server's tool
discovery and dispatch, and the isolation guarantee at the tenant boundary.

Nothing here talks to a model. The MCP tests drive the server object
in-process rather than over stdio, so they exercise schema generation,
argument validation and dispatch -- but **not** the stdio transport or a
real MCP client handshake.
"""

import asyncio
import contextlib
import importlib.util
import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from falkordb import FalkorDB  # noqa: E402

from agents.toolbox import PLANNER_TOOLS, REVIEWER_TOOLS  # noqa: E402
from memory import MemoryStore  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "mcp_server", ROOT / "scripts" / "mcp_server.py")
mcp_server = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mcp_server)

TENANT = "test"
PASSED, FAILED = [], []


def test(fn):
    name = fn.__name__.replace("test_", "").replace("_", " ")
    try:
        fn()
    except AssertionError as e:
        FAILED.append((name, str(e) or "assertion failed"))
        print(f"[FAIL] {name}\n       {e}")
    except Exception as e:
        FAILED.append((name, f"{type(e).__name__}: {e}"))
        print(f"[ERROR] {name}\n       {type(e).__name__}: {e}")
    else:
        PASSED.append(name)
        print(f"[PASS] {name}")
    return fn


def fresh_store(tenant=TENANT, world_file="world_acme.json"):
    world = json.loads((ROOT / "data" / world_file).read_text(encoding="utf-8"))
    world["graph"] = f"tenant_{tenant}"
    spec = importlib.util.spec_from_file_location("seed", ROOT / "scripts" / "seed.py")
    seed = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(seed)
    with contextlib.redirect_stdout(io.StringIO()):
        seed.seed_tenant(FalkorDB(host="localhost", port=6379), world,
                         ROOT / "data" / world_file)
    return MemoryStore(tenant)


# ------------------------------------------------------- the algorithm (step 8)
@test
def test_betweenness_names_the_single_point_of_failure():
    store = fresh_store()
    result = store.critical_projects()
    assert result["ranking"], "betweenness returned nothing"
    assert result["ranking"][0]["project"] == "Spring Launch", result
    assert result["ranking"][0]["score"] > 0, result
    assert result["caveat"] is None, result


@test
def test_only_projects_on_a_path_score_at_all():
    # Spring Launch is the only project with something on each side of it.
    store = fresh_store()
    scored = {c["project"] for c in store.critical_projects(limit=20)["ranking"]}
    assert scored == {"Spring Launch"}, scored


@test
def test_the_algorithm_agrees_with_the_plain_traversal():
    # If betweenness disagreed with dependents_of/depends_on_chain, one of
    # them would be wrong. The algorithm should confirm, not contradict.
    store = fresh_store()
    top = store.critical_projects()["ranking"][0]["project"]
    upstream = store.depends_on_chain(top)
    downstream = store.dependents_of(top)
    assert downstream and upstream, (upstream, downstream)
    assert {d["project"] for d in downstream} == {"Billing v2", "Mobile App"}
    assert {u["project"] for u in upstream} == {"Ledger Migration"}


@test
def test_people_never_score_because_they_have_no_incoming_edges():
    # Documented limitation, asserted so it cannot silently change: the
    # question betweenness answers here is "what", not "who".
    store = fresh_store()
    rows = store._read("MATCH (p:Person)<-[r]-() RETURN count(r)")
    assert rows[0][0] == 0, "a Person gained an incoming edge; recheck moment 8"


@test
def test_the_algorithm_cannot_see_validity_and_says_so():
    """Characterises a real limitation rather than asserting a wish.

    Retiring a fact sets valid_to; it does not delete the edge. The
    algorithm walks structure, so the retired edge still counts. Every other
    read in MemoryStore filters on validity and this one cannot, so the
    result reports how many retired edges it may have included.
    """
    store = fresh_store()
    s = store.start_session(user="t", session_id="s1")
    assert store.critical_projects()["ranking"][0]["project"] == "Spring Launch"

    store.retire_fact(s, "Reviewer", "DEPENDS_ON", "Billing v2", "Spring Launch",
                      "decoupled")
    store.retire_fact(s, "Reviewer", "DEPENDS_ON", "Mobile App", "Spring Launch",
                      "decoupled")

    # The plain traversal, which does filter, now sees nothing downstream.
    assert store.dependents_of("Spring Launch") == []

    # The algorithm still scores it -- and flags that it is reading history.
    after = store.critical_projects()
    assert after["ranking"][0]["project"] == "Spring Launch", after
    assert after["retired_dependency_edges"] == 2, after
    assert after["caveat"] and "retired" in after["caveat"], after


# ------------------------------------------------------- the MCP server (step 8)
def build(agent="Reviewer", allowed=REVIEWER_TOOLS, store=None):
    store = store or fresh_store()
    session = store.start_session(user="mcp-test", session_id="sess-mcp")
    return store, mcp_server.build_server(store, agent, session, allowed=allowed)


@test
def test_server_exposes_exactly_the_roles_tools():
    _, server = build()
    names = {t.name for t in asyncio.run(server.list_tools())}
    assert names == set(REVIEWER_TOOLS), names - set(REVIEWER_TOOLS)

    _, planner = build(agent="Planner", allowed=PLANNER_TOOLS)
    pnames = {t.name for t in asyncio.run(planner.list_tools())}
    assert pnames == set(PLANNER_TOOLS), pnames
    assert "record_decision" in pnames and "record_decision" not in names


@test
def test_schemas_carry_enums_and_mark_optionals():
    _, server = build()
    tools = {t.name: t for t in asyncio.run(server.list_tools())}
    schema = tools["recall_related"].input_schema
    assert schema["properties"]["label"]["enum"] == \
        ["Person", "Project", "System", "Decision"], schema["properties"]["label"]
    assert set(schema["required"]) == {"label", "name"}, schema["required"]

    # A tool with no required arguments must still be callable.
    assert tools["claim_task"].input_schema.get("required", []) == []


@test
def test_agent_and_session_are_not_client_settable():
    # The whole point of binding them server-side.
    _, server = build()
    for tool in asyncio.run(server.list_tools()):
        props = set(tool.input_schema.get("properties", {}))
        assert "agent" not in props, tool.name
        assert "session_id" not in props, tool.name
        assert "tenant" not in props, tool.name


@test
def test_calling_a_tool_reads_the_real_graph():
    _, server = build()
    result = asyncio.run(server.call_tool(
        "current_decisions", {"project": "Spring Launch"}))
    assert not result.is_error, result
    payload = json.loads(result.content[0].text)
    assert payload and payload[0]["title"].startswith("Ship Spring Launch"), payload


@test
def test_calling_a_tool_writes_to_the_real_graph():
    store, server = build()
    before = store.current_facts("OWNS", to_name="Ledger Migration")[0]["from"]
    assert before == "Lee Park"

    result = asyncio.run(server.call_tool("retire_fact", {
        "rel_type": "OWNS", "from_name": "Lee Park",
        "to_name": "Ledger Migration", "reason": "handover"}))
    assert not result.is_error, result
    assert json.loads(result.content[0].text)["retired"] == 1

    assert store.current_facts("OWNS", to_name="Ledger Migration") == []
    history = store.fact_history("OWNS", to_name="Ledger Migration")
    assert len(history) == 1 and history[0]["valid_to"] is not None, history


@test
def test_optional_arguments_fall_back_to_handler_defaults():
    # MCP sends omitted optionals as null; passing those through would
    # shadow the handlers' own defaults.
    _, server = build()
    result = asyncio.run(server.call_tool("critical_projects", {}))
    assert not result.is_error, result
    payload = json.loads(result.content[0].text)
    assert payload["ranking"][0]["project"] == "Spring Launch", payload


@test
def test_an_invalid_enum_is_rejected_before_it_reaches_cypher():
    # The SDK validates against the generated schema and raises; it never
    # reaches our dispatch, so a bad label cannot be interpolated into Cypher.
    _, server = build()
    try:
        asyncio.run(server.call_tool("recall_related",
                                     {"label": "Dinosaur", "name": "Rex"}))
    except Exception as e:
        assert "literal_error" in str(e) or "Input should be" in str(e), str(e)
        return
    raise AssertionError("an invalid label must not be accepted")


@test
def test_a_tool_outside_the_role_is_not_exposed():
    _, server = build()
    names = {t.name for t in asyncio.run(server.list_tools())}
    assert "record_decision" not in names
    try:
        asyncio.run(server.call_tool("record_decision",
                                     {"title": "x", "reason": "y"}))
    except Exception as e:
        assert "Unknown tool" in str(e), str(e)
        return
    raise AssertionError("an unexposed tool must not run")


@test
def test_a_failing_tool_returns_text_not_a_crash():
    _, server = build()
    result = asyncio.run(server.call_tool("explain_path", {
        "from_label": "Person", "from_name": "Nobody",
        "to_label": "Project", "to_name": "Nowhere"}))
    # No path exists; the tool should answer null rather than raise.
    assert not result.is_error, result
    assert result.content[0].text == "null", result.content[0].text


@test
def test_two_servers_on_two_tenants_stay_separate():
    acme = fresh_store(tenant="test", world_file="world_acme.json")
    globex = fresh_store(tenant="test2", world_file="world_globex.json")
    _, a_server = build(store=acme)
    _, g_server = build(store=globex)

    q = {"project": "Billing v2"}
    a = json.loads(asyncio.run(
        a_server.call_tool("current_decisions", q)).content[0].text)
    g = json.loads(asyncio.run(
        g_server.call_tool("current_decisions", q)).content[0].text)

    assert a == [], f"acme should have no decision on Billing v2 yet: {a}"
    assert g and "independently" in g[0]["title"], g
    try:
        FalkorDB(host="localhost", port=6379).select_graph("tenant_test2").delete()
    except Exception:
        pass


# ------------------------------------------------------------------- report
if __name__ == "__main__":
    for graph in ("tenant_test", "tenant_test2"):
        try:
            FalkorDB(host="localhost", port=6379).select_graph(graph).delete()
        except Exception:
            pass
    print("\n" + "=" * 62)
    print(f"{len(PASSED)}/{len(PASSED) + len(FAILED)} tests passed")
    for name, why in FAILED:
        print(f"  FAILED: {name} -- {why}")
    sys.exit(1 if FAILED else 0)
