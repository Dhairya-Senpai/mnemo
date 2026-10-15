"""MCP server exposing the memory tools to any MCP client.

    python scripts/mcp_server.py --tenant acme --agent Reviewer

Speaks MCP over stdio, so an MCP-capable client can discover the memory
tools and choose between them. That tool selection is the Track 01 piece;
the tools themselves are the same Track 02 memory layer, exposed over a
second transport rather than reimplemented.

One implementation, two transports: every call goes through the same
`Toolbox.dispatch` the in-process agent uses, so an MCP client and the
Reviewer cannot drift apart or disagree about what a tool does.

Tenant, agent and session are bound here from the command line. They are not
tool parameters, so a client cannot write into another tenant's graph, act as
a different agent, or forge a session -- the same guarantee the in-process
agent has.

Built against the MCP Python SDK 2.x, where `FastMCP` was renamed to
`MCPServer`. On 1.x this import fails; the SDK's own error says so.
"""

import argparse
import inspect
import sys
from pathlib import Path
from typing import Literal

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from mcp.server.mcpserver import MCPServer  # noqa: E402

from agents.toolbox import CATALOGUE, PLANNER_TOOLS, REVIEWER_TOOLS, Toolbox  # noqa: E402
from llm.types import ToolCall  # noqa: E402
from memory import MemoryStore  # noqa: E402

ROLE_TOOLS = {
    "Planner": PLANNER_TOOLS,
    "Reviewer": REVIEWER_TOOLS,
}

_JSON_TO_PY = {"string": str, "integer": int, "boolean": bool, "number": float}


def _annotation(spec):
    """JSON Schema property -> Python type hint.

    The SDK builds each tool's schema from the function signature, so the
    hints are how constraints survive. Enums become Literal, which is what
    makes the server reject a bad label instead of passing it to Cypher.
    """
    if "enum" in spec:
        return Literal[tuple(spec["enum"])]
    if spec.get("type") == "array":
        return list[str]
    return _JSON_TO_PY.get(spec.get("type"), str)


def _as_mcp_function(name, tool, dispatch):
    """Give a catalogue entry a real signature so the SDK can describe it."""
    schema = tool.spec.parameters
    properties = schema.get("properties", {})
    required = set(schema.get("required", []))

    params, annotations = [], {}
    for pname, spec in properties.items():
        ann = _annotation(spec)
        if pname in required:
            params.append(inspect.Parameter(
                pname, inspect.Parameter.KEYWORD_ONLY, annotation=ann))
        else:
            ann = ann | None
            params.append(inspect.Parameter(
                pname, inspect.Parameter.KEYWORD_ONLY, annotation=ann, default=None))
        annotations[pname] = ann

    def fn(**kwargs):
        # Optional arguments arrive as None rather than being absent, which
        # would shadow the handlers' own defaults. Drop them.
        return dispatch(name, {k: v for k, v in kwargs.items() if v is not None})

    fn.__name__ = name
    fn.__doc__ = tool.spec.description
    fn.__signature__ = inspect.Signature(params, return_annotation=str)
    fn.__annotations__ = {**annotations, "return": str}
    return fn


def build_server(store, agent, session_id, allowed=None, name="agent-memory"):
    """An MCPServer exposing one agent's memory tools against one tenant."""
    toolbox = Toolbox(store, agent, session_id, allowed=allowed)

    def dispatch(tool_name, arguments):
        # Same path the in-process agent takes, including error handling:
        # a failure comes back as text the client can read, not a crash.
        result = toolbox.dispatch(ToolCall(id=f"mcp-{tool_name}",
                                           name=tool_name, arguments=arguments))
        return result.content

    server = MCPServer(
        name=name,
        instructions=(
            f"Graph-backed memory for tenant {store.tenant!r}, acting as "
            f"{agent!r}. Every call reads or writes a FalkorDB graph. Facts "
            f"are versioned: a fact is current when it has no end date, and "
            f"retiring one never deletes it."
        ),
    )
    for tool_name in toolbox.names:
        tool = CATALOGUE[tool_name]
        server.add_tool(
            _as_mcp_function(tool_name, tool, dispatch),
            name=tool_name,
            description=tool.spec.description,
        )
    return server


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tenant", default="acme")
    ap.add_argument("--agent", default="Reviewer", choices=sorted(ROLE_TOOLS))
    ap.add_argument("--session", default=None,
                    help="existing session id; a new one is opened if omitted")
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=6379)
    ap.add_argument("--all-tools", action="store_true",
                    help="expose the whole catalogue instead of the role's subset")
    args = ap.parse_args()

    store = MemoryStore(args.tenant, host=args.host, port=args.port)
    session = args.session or store.start_session(user="mcp-client")
    allowed = None if args.all_tools else ROLE_TOOLS[args.agent]

    server = build_server(store, args.agent, session, allowed=allowed)
    exposed = len(allowed) if allowed else len(CATALOGUE)
    # stderr: stdout is the MCP transport and must carry nothing else.
    print(f"agent-memory MCP server: tenant={args.tenant} agent={args.agent} "
          f"session={session} tools={exposed}", file=sys.stderr)
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
