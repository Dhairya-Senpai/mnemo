"""Agent Memory Graph - a two-agent workspace.

    streamlit run app.py

Needs FalkorDB running (`docker start falkordb`), the graphs seeded
(`python scripts/seed.py`), and a model configured:

    LLM_PROVIDER=ollama  LLM_MODEL=ornith:9b     # local, free
    LLM_PROVIDER=claude  LLM_MODEL=claude-sonnet-5-5

You talk to the Planner on the left. It decides for itself which memory tools
to call, and anything it learns goes into the graph. When it leaves work for
the Reviewer, that work shows up on the right - passed through the graph, not
through any code path between them.

The transcript is read back out of FalkorDB on every render rather than kept
in browser state. Reload the page and the conversation is still there, because
it was never in the page. That is the whole thesis, and it is checkable by
pressing F5.
"""

import os
import sys
import time
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from agents import (  # noqa: E402
    REVIEWER_TRIGGER,
    build_planner,
    build_reviewer,
)
from demo import BY_KEY, MOMENTS, reset_world  # noqa: E402
from demo.graphview import VIEWS, WORLD, build_dot  # noqa: E402
from llm import get_adapter  # noqa: E402
from memory import MemoryStore, diff_snapshots  # noqa: E402

st.set_page_config(page_title="Agent Memory Graph", layout="wide", page_icon="//")

USER = "you"
# Local models explore more than they need to: ornith:9b typically spends
# 11-12 calls on a question Sonnet would answer in 4. The loop caches repeat
# calls and forces a final answer when the budget runs out, so a low cap is
# survivable - but this leaves enough room to finish the thought.
MAX_TOOL_CALLS = 14


@st.cache_resource
def get_store(tenant):
    return MemoryStore(tenant)


# Offered in the sidebar so the provider can be changed while the app runs.
# Switching between these mid-conversation is the swappable-adapter claim made
# visible: the agents, tools, prompts and graph are identical either way.
CHOICES = {
    "ollama / ornith:9b": ("ollama", {"model": "ornith:9b"}),
    "claude / sonnet 5.5": ("claude", {"model": "claude-sonnet-5-5"}),
    "claude / haiku 5.5": ("claude", {"model": "claude-haiku-5-5"}),
}


def adapter():
    """Build the selected model adapter, surfacing config errors clearly."""
    label = st.session_state.get("model_choice") or next(iter(CHOICES))
    provider, kwargs = CHOICES[label]
    return get_adapter(provider, **kwargs)


def current_session(store, key, user, prefix):
    """Continue the newest session for this user, or open one.

    Deliberately resolved from the graph rather than from st.session_state,
    so a browser reload rejoins the same conversation instead of starting a
    new one. Persistence is a property of the database, not the page.
    """
    if st.session_state.get(key):
        return st.session_state[key]
    existing = [s for s in store.list_sessions(limit=50)
                if s["user"] == user and s["id"].startswith(prefix)]
    sid = existing[0]["id"] if existing else store.start_session(
        user=user, session_id=f"{prefix}{int(time.time())}")
    st.session_state[key] = sid
    return sid


def run_agent(build, store, session, text, speaker="user"):
    """Run one agent turn and capture what it changed in the graph."""
    before = store.snapshot()
    agent = build(store, session, adapter(), max_tool_calls=MAX_TOOL_CALLS)
    turn = agent.run(text, speaker=speaker)
    st.session_state.changes = diff_snapshots(before, store.snapshot())
    return turn


# --------------------------------------------------------------------- state
for key, default in (("planner_session", None), ("reviewer_session", None),
                     ("changes", None), ("reviewer_turn", None),
                     ("last_path", None), ("busy", False)):
    st.session_state.setdefault(key, default)

# ------------------------------------------------------------------- sidebar
with st.sidebar:
    st.title("Agent Memory Graph")

    default = os.environ.get("LLM_CHOICE") or next(iter(CHOICES))
    st.selectbox("Model", list(CHOICES),
                 index=list(CHOICES).index(default) if default in CHOICES else 0,
                 key="model_choice")
    try:
        probe = adapter()
        st.caption(f"ready: `{probe.name}` / `{probe.model}`")
    except Exception as exc:
        st.error(f"{type(exc).__name__}: {exc}")
    st.caption("Switching provider changes nothing else - same agents, same "
               "tools, same graph.")

    tenant = st.selectbox("Tenant", ["acme", "globex"], index=0)
    store = get_store(tenant)

    planner_session = current_session(store, "planner_session", USER, "sess-you-")
    reviewer_session = current_session(store, "reviewer_session", "reviewer",
                                       "sess-reviewer-")

    st.divider()
    st.subheader("Session")
    st.caption(f"`{planner_session}`")
    st.caption("Reload the page - the conversation comes back from the graph.")

    if st.button("Start a new session", width="stretch"):
        st.session_state.planner_session = store.start_session(
            user=USER, session_id=f"sess-you-{int(time.time())}")
        st.session_state.changes = None
        st.rerun()

    past = [s for s in store.list_sessions(limit=20)
            if s["user"] == USER and s["turns"]]
    if past:
        labels = {f"{s['id'][-10:]} - {s['turns']} turns": s["id"] for s in past}
        picked = st.selectbox("Earlier sessions", list(labels), index=None,
                              placeholder="jump to a past session")
        if picked and labels[picked] != planner_session:
            st.session_state.planner_session = labels[picked]
            st.rerun()

    st.divider()
    st.subheader("World")
    if st.button("Reset both graphs", width="stretch"):
        with st.spinner("reseeding..."):
            reset_world()
        for key in ("planner_session", "reviewer_session", "changes",
                    "reviewer_turn", "last_path"):
            st.session_state[key] = None
        st.cache_resource.clear()
        st.rerun()

    scenario = st.selectbox(
        "Load an example scenario", [m.key for m in MOMENTS], index=None,
        format_func=lambda k: f"{BY_KEY[k].number}. {BY_KEY[k].title}",
        placeholder="pre-populate memory...")
    if scenario and st.button("Run scenario", width="stretch"):
        moment = BY_KEY[scenario]
        before = store.snapshot()
        with st.spinner(f"running {moment.title}..."):
            result = moment.run(get_store("acme"))
        st.session_state.changes = diff_snapshots(before, store.snapshot())
        st.session_state.last_path = result.path
        st.rerun()
    st.caption("Scenarios use scripted models to seed memory quickly. "
               "The conversation above uses the real one.")

    st.divider()
    view = st.selectbox("Graph view", list(VIEWS), index=0)

# ---------------------------------------------------------------------- main
left, right = st.columns([3, 2], gap="large")

with left:
    st.subheader("You and the Planner")
    transcript = store.session_transcript(planner_session)
    if not transcript:
        st.caption("Say something. Try: *we need to delay the Spring Launch to "
                   "May 15, 2027 because Ledger Migration is blocked*")
    for entry in transcript:
        role = "user" if entry["speaker"] == USER or entry["speaker"] == "user" \
            else "assistant"
        with st.chat_message(role):
            st.markdown(entry["text"])

    if st.session_state.get("planner_steps"):
        with st.expander(f"tools the Planner just used "
                         f"({len(st.session_state.planner_steps)})"):
            for step in st.session_state.planner_steps:
                st.markdown(f"**{step['tool']}**")
                st.code(step["result"][:800], language="json")

with right:
    st.subheader("The Reviewer")
    open_tasks = store.open_tasks("Reviewer")
    if open_tasks:
        st.warning(f"{len(open_tasks)} task(s) waiting")
        for task in open_tasks:
            st.markdown(f"- {task['title']}")
    else:
        st.caption("No open tasks.")

    st.caption("The Reviewer is told only this, and nothing about your "
               f"conversation:\n\n> {REVIEWER_TRIGGER}")

    if st.button("Let the Reviewer work", width="stretch",
                 disabled=not open_tasks):
        with st.spinner("the Reviewer is reading the graph..."):
            try:
                turn = run_agent(build_reviewer, store, reviewer_session,
                                 REVIEWER_TRIGGER, speaker="system")
                st.session_state.reviewer_turn = {
                    "text": turn.text,
                    "tools": turn.tools_used,
                }
            except Exception as exc:
                st.session_state.reviewer_turn = {
                    "text": f"failed: {type(exc).__name__}: {exc}", "tools": []}
        st.rerun()

    if st.session_state.reviewer_turn:
        st.markdown(st.session_state.reviewer_turn["text"])
        if st.session_state.reviewer_turn["tools"]:
            st.caption("read from the graph: "
                       + ", ".join(st.session_state.reviewer_turn["tools"]))

    st.divider()
    st.subheader("Memory changes")
    changes = st.session_state.changes
    if changes is None or changes.empty:
        st.caption("Nothing has changed yet.")
    else:
        cols = st.columns(3)
        for col, (label, count) in zip(cols, changes.counts.items()):
            col.metric(label, count)
        for line in changes.lines():
            (st.success if line.startswith("+") else st.warning)(line)

# chat input lives outside the columns; Streamlit pins it to the bottom
prompt = st.chat_input("Tell the Planner something, or ask it a question")
if prompt:
    with st.spinner("the Planner is thinking and writing to the graph..."):
        try:
            turn = run_agent(build_planner, store, planner_session, prompt)
            st.session_state.planner_steps = [
                {"tool": s.name, "result": s.result} for s in turn.steps]
        except Exception as exc:
            store.record_event(planner_session, "Planner", "message",
                               f"(failed: {type(exc).__name__}: {exc})",
                               speaker="Planner")
    st.rerun()

st.divider()
st.subheader(f"The graph - tenant {tenant}")
added = [n["name"] for n in changes.added_nodes] if changes else []
try:
    st.graphviz_chart(
        build_dot(store, labels=VIEWS.get(view, WORLD),
                  path=st.session_state.last_path, changed=added),
        width="stretch", height=520)
    st.caption("Green: added by the last action. Red: an explanation chain. "
               "Dashed grey: retired, still readable - nothing is deleted.")
except Exception as exc:
    st.error(f"Could not draw the graph: {type(exc).__name__}: {exc}")
