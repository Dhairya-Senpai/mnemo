"""Mnemo - a chat window whose memory is a FalkorDB graph.

    streamlit run app.py

Needs FalkorDB running (`docker start falkordb`), the graphs seeded
(`python scripts/seed.py`), and a model configured:

    LLM_PROVIDER=ollama  LLM_MODEL=ornith:9b     # local, free
    LLM_PROVIDER=claude  LLM_MODEL=claude-sonnet-5-5

It looks like any other chat app on purpose: conversations down the left,
the exchange in the middle, the model picker under the composer. What is not
ordinary sits one click away, under each answer, where a chat app usually
puts copy and retry -- what the turn changed in memory, which reads produced
the answer, the graph itself, and the second agent.

Nothing here is held in browser state. The conversation list, the transcript
and the reasoning behind every answer are read back out of FalkorDB on each
render, so reloading the page loses nothing: none of it was ever in the page.
"""

import os
import re
import sys
import time
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from agents import (  # noqa: E402
    REVIEWER_TRIGGER,
    build_planner,
    build_reviewer,
)
from demo import BY_KEY, MOMENTS, reset_world  # noqa: E402
from demo.falkor_canvas import (  # noqa: E402
    canvas_html,
    graph_payload,
    legend as graph_legend,
)
from demo.graphview import VIEWS, WORLD  # noqa: E402
from llm import get_adapter  # noqa: E402
from memory import (MemoryStore, delete_tenant, diff_snapshots,
                    list_tenants, rename_tenant)  # noqa: E402

st.set_page_config(page_title="Mnemo", layout="wide",
                   page_icon="//", initial_sidebar_state="expanded")

# The palette and type scale live in .streamlit/config.toml. These three rules
# are the ones Streamlit gives no setting for.
st.markdown("""
<style>
/* The name sits in the gap at the head of the sidebar, in the heading
   face, quiet enough not to compete with the conversation list. */
.mnemo-mark {
    font-family: Georgia, 'Iowan Old Style', 'Times New Roman', serif;
    font-size: 1.3rem;
    line-height: 1.6rem;
    color: #E8E6DC;
    opacity: .92;
    padding: 0 0 .9rem 0;
    /* The name's box spans the sidebar and so sits over the collapse
       control. It is text, not a target, so let clicks through to it. */
    width: fit-content;
    pointer-events: none;
}

/* Streamlit reserves a 56px strip at the head of the sidebar for the
   collapse control and begins the content below it, which left the name
   sitting in a gap. Pulling the content up into that strip puts the name on
   the same line as the control -- it is left aligned, the control is right
   aligned, so they share the row rather than collide. */
section[data-testid="stSidebar"] [data-testid="stSidebarUserContent"] {
    padding-top: 0 !important;
    margin-top: -3.9rem !important;
}
/* Pulling the content up puts its box over the collapse control, which then
   swallows the click. Lifting the header above it hands those clicks back. */
section[data-testid="stSidebar"] [data-testid="stSidebarHeader"] {
    position: relative !important;
    z-index: 10 !important;
}

/* A conversation row is a row, not a button: no outline, no fill, left
   aligned. The open one is marked by sitting in a well -- darker than the
   sidebar rather than brighter -- because selection is a state, not an
   action. */
[class*="st-key-crow"] button {
    border: none !important;
    background: transparent !important;
    box-shadow: none !important;
    color: #E8E6DC !important;
    justify-content: flex-start !important;
    text-align: left !important;
    font-size: .85rem !important;
    padding: .32rem .5rem !important;
    min-height: 0 !important;
    border-radius: 6px !important;
}
[class*="st-key-crow"] button > div {
    justify-content: flex-start !important;
    width: 100% !important;
}
[class*="st-key-crow"] button * { text-align: left !important; }
[class*="st-key-crow"] button:hover { background: rgba(232,230,220,.07) !important; }
[class*="st-key-crow"] button[kind="primary"] {
    background: rgba(0, 0, 0, .38) !important;
}
[class*="st-key-crow"] button[kind="primary"]:hover {
    background: rgba(0, 0, 0, .46) !important;
}

/* The per-conversation menu, same treatment as the workspace one. */
[class*="st-key-cmenu"] { display: flex; justify-content: flex-end; }
[class*="st-key-cmenu"] button {
    border: none !important;
    background: transparent !important;
    box-shadow: none !important;
    padding: 0 !important;
    min-height: 0 !important;
    color: rgba(232, 230, 220, .35) !important;
}
[class*="st-key-cmenu"] button:hover { color: #E8E6DC !important; }
[class*="st-key-cmitems"] button, [class*="st-key-cmdel"] button {
    display: flex !important;
    justify-content: flex-start !important;
    text-align: left !important;
    width: 100% !important;
    padding: .34rem .5rem !important;
    min-height: 0 !important;
    font-size: .84rem !important;
    border: none !important;
    background: transparent !important;
    border-radius: 6px !important;
}
[class*="st-key-cmitems"] button > div,
[class*="st-key-cmdel"] button > div {
    justify-content: flex-start !important; width: 100% !important;
}
[class*="st-key-cmitems"] button:hover, [class*="st-key-cmdel"] button:hover {
    background: rgba(232, 230, 220, .09) !important;
}
[class*="st-key-cmitems"] button { color: #E8E6DC !important; }
[class*="st-key-cmdel"] button { color: #f87171 !important; }

/* The inline rename sits exactly where the row was. */
[class*="st-key-crename"] input {
    font-size: .85rem !important;
    padding: .3rem .5rem !important;
}

/* The model picker belongs with the composer, bottom right, the way a chat
   app shows what it is running. st.chat_input is pinned by Streamlit and
   nothing can be placed inside it, so this lifts the picker out of the page
   flow and parks it against the composer instead of leaving it adrift. */
.st-key-modelbar {
    position: fixed;
    right: 2.2rem;
    bottom: 0.1rem;
    z-index: 100;
    width: 26rem;
}
/* The visible box is on an emotion-hashed div with no stable attribute of
   its own, so this clears every descendant inside the bar rather than
   chasing a class name that changes between Streamlit releases. */
.st-key-modelbar div {
    background: transparent !important;
    border: none !important;
    box-shadow: none !important;
}
.st-key-modelbar [data-baseweb="select"] {
    min-height: 1.4rem;
    font-size: .75rem;
    opacity: .55;
}
.st-key-modelbar:hover [data-baseweb="select"] { opacity: 1; }
.st-key-modelbar p { font-size: .75rem; opacity: .55; }

/* The + that adds a workspace is a piece of the heading, not a control
   sitting next to it: no border, no fill, no button padding, and the same
   weight as the label it follows. Streamlit's tertiary button is close but
   still reserves a button's worth of height and colour. */
.st-key-wsadd, [class*="st-key-wskebab"] { display: flex; justify-content: flex-end; }
.st-key-wsadd button, [class*="st-key-wskebab"] button {
    border: none !important;
    background: transparent !important;
    box-shadow: none !important;
    padding: 0 !important;
    min-height: 0 !important;
    height: auto !important;
    color: rgba(232, 230, 220, .45) !important;
}
.st-key-wsadd button:hover,
[class*="st-key-wskebab"] button:hover { color: #E8E6DC !important; }
.st-key-wsadd span[data-testid="stIconMaterial"],
[class*="st-key-wskebab"] span[data-testid="stIconMaterial"] { font-size: 1.05rem; }

/* The workspace menu is a menu, not a stack of form buttons. Three things
   make Streamlit's defaults too airy for one, and none has a setting:
   the popover body pads itself 21.5px, every vertical block puts a 15px gap
   between children, and a button centres its own label. `st-key-wsmenu`
   prefix-matches both the normal rows and the delete row. */
/* Streamlit gives a popover a fixed minimum width, which left the menu
   three short rows wide and a lot of empty space to their right. Sizing to
   content makes it as wide as its longest row and no wider. */
[data-testid="stPopoverBody"] {
    padding: .4rem !important;
    min-width: 0 !important;
    width: max-content !important;
}
[data-testid="stPopoverBody"] [data-testid="stVerticalBlock"] {
    gap: 0 !important;
}
[class*="st-key-wsmenu"] button {
    display: flex !important;
    justify-content: flex-start !important;
    text-align: left !important;
    width: 100% !important;
    padding: .34rem .5rem !important;
    min-height: 0 !important;
    font-size: .84rem !important;
    border: none !important;
    background: transparent !important;
    border-radius: 6px !important;
}
/* The label wrapper inside the button is itself a flex container, so
   text-align does nothing to it -- it needs justify-content too. */
[class*="st-key-wsmenu"] button * { text-align: left !important; }
[class*="st-key-wsmenu"] button > div {
    justify-content: flex-start !important;
    width: 100% !important;
}
[class*="st-key-wsmenu"] button:hover {
    background: rgba(232, 230, 220, .09) !important;
}
.st-key-wsmenu button { color: #E8E6DC !important; }
.st-key-wsmenudel button { color: #f87171 !important; }
[class*="st-key-wsmenu"] span[data-testid="stIconMaterial"] {
    font-size: .95rem; margin-right: .5rem;
}
</style>
""", unsafe_allow_html=True)

USER = "you"
SESSION_PREFIX = "sess-you-"

# Local models explore more than they need to: ornith:9b typically spends
# 11-12 calls on a question Sonnet would answer in 4. The loop caches repeat
# calls and forces a final answer when the budget runs out, so a low cap is
# survivable - but this leaves enough room to finish the thought.
MAX_TOOL_CALLS = 14

# The scenario replayer and the graph reset still work and are still wired up
# below; they are simply not shown. They belong to a demo rig, and a demo rig
# in the sidebar is why this did not read as a chat app. Flip to True.
SHOW_DEMO_CONTROLS = False


@st.cache_resource
def get_store(tenant):
    return MemoryStore(tenant)


# A workspace is one FalkorDB graph. There is no tenant property on any node
# and no tenant filter in any query -- MemoryStore binds the graph at
# construction -- so isolation cannot be lost by forgetting a WHERE clause.
#
# The list is not kept here. It is read from FalkorDB with list_tenants(),
# so a workspace exists because a graph exists, and creating one is simply
# writing to a name nothing has written to yet. These are only the display
# names for the three that ship with the project:
#
# * acme     the company world the demo runs on: the launch delay, the
#            two-agent handoff, the dependency chain, the centrality result.
# * globex   a second company carrying the SAME entity names as acme with
#            different facts. That collision is what makes isolation
#            demonstrable rather than merely asserted.
# * personal unseeded, and stays that way until someone talks to it.
WORKSPACE_LABELS = {
    "acme": "Acme — company demo",
    "globex": "Globex — isolation proof",
    "personal": "Personal — my memory",
}
WORKSPACE_ORDER = ("personal", "acme", "globex")
NAME_OK = re.compile(r"^[a-z][a-z0-9_]{0,30}$")


def workspace_label(tenant):
    return WORKSPACE_LABELS.get(tenant, tenant.replace("_", " ").title())


def workspaces():
    """What exists, newest first, plus any of the three built-ins missing."""
    found = list_tenants()
    return found + [n for n in WORKSPACE_ORDER if n not in found]


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

    Resolved from the graph rather than from st.session_state, so a browser
    reload rejoins the same conversation instead of starting a new one.
    Persistence is a property of the database, not the page.
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
    st.session_state.curation_error = (
        turn.curation.error if turn.curation else None)
    return turn


def new_chat(store):
    st.session_state.planner_session = store.start_session(
        user=USER, session_id=f"{SESSION_PREFIX}{int(time.time())}")
    st.session_state.changes = None
    st.session_state.planner_steps = None


# --------------------------------------------------------------------- state
for key, default in (("planner_session", None), ("reviewer_session", None),
                     ("changes", None), ("reviewer_turn", None),
                     ("last_path", None), ("planner_steps", None),
                     ("cited_nodes", [])):
    st.session_state.setdefault(key, default)

# The tenant widget lives at the foot of the sidebar, but the store has to
# exist before the sidebar is drawn. Read last run's choice; the widget below
# writes this key, and changing it reruns the script.
# No explicit choice yet: open the workspace created most recently,
# which is the one someone just made.
tenant = st.session_state.get("tenant_name") or (workspaces() or ["personal"])[0]
store = get_store(tenant)

# Session ids belong to one graph. Carrying them across a workspace switch
# points the app at a Session that does not exist in the graph it is now
# talking to -- which used to write nothing at all, silently, and now raises.
# Everything derived from the old graph goes with them.
if st.session_state.get("_tenant_now") != tenant:
    st.session_state["_tenant_now"] = tenant
    for key in ("planner_session", "reviewer_session", "changes",
                "reviewer_turn", "planner_steps", "last_path"):
        st.session_state[key] = None
    st.session_state["cited_nodes"] = []

planner_session = current_session(store, "planner_session", USER, SESSION_PREFIX)
reviewer_session = current_session(store, "reviewer_session", "reviewer",
                                   "sess-reviewer-")


# ------------------------------------------------------------------- windows
# Each of these opens as a modal from the row under the last answer, which is
# where a chat app puts copy and retry. They are the graph showing its
# working, so they belong beside the answer rather than in a panel that is
# always on screen competing with the conversation.

@st.dialog("Memory changes", width="large")
def window_changes():
    changes = st.session_state.changes
    # A curation that failed is the one thing worth saying even when the
    # graph is otherwise unchanged. The agent will have answered normally
    # and may well have said "noted" -- without this the person has no way
    # to know nothing was kept.
    failed = st.session_state.get("curation_error")
    if failed:
        st.error(f"The curator did not run on the last turn, so nothing it "
                 f"would have recorded was kept: {failed}")
    if changes is None or changes.empty:
        if not failed:
            st.caption("Nothing has changed yet.")
        return
    cols = st.columns(3)
    for col, (label, count) in zip(cols, changes.counts.items()):
        col.metric(label, count)
    for line in changes.lines():
        (st.success if line.startswith("+") else st.warning)(line)
    st.caption("Diffed from the graph before and after the turn, not from "
               "what the agent said it did.")


@st.dialog("Why it answered that", width="large")
def window_why():
    # Read from the graph every time, never from st.session_state. That is
    # the whole claim: the reasoning behind an answer outlives the page, the
    # process, and the session it was given in.
    about = st.text_input(
        "about", placeholder="about a project, person or system (optional)",
        label_visibility="collapsed").strip()

    trace = store.explain_answer(about=about or None)
    if not trace:
        st.caption("No traced answer yet." if not about
                   else f"No answer here was based on {about!r}.")
        st.session_state.cited_nodes = []
        return

    answer = trace["answer"]
    st.markdown(f"> {answer['text']}")
    st.caption(f"{answer['speaker']} · {answer['ts']} · reconstructed from "
               f"{len(trace['steps'])} recorded read(s)")
    for step in trace["steps"]:
        args = ", ".join(f"{k}={v!r}" for k, v in step["arguments"].items())
        with st.expander(f"READ  {step['tool']}({args})",
                         expanded=len(trace["steps"]) <= 3):
            if step["returned"]:
                for cited in step["returned"]:
                    label, _, name = cited.partition(":")
                    st.markdown(f"- `{label}` **{name}**")
            else:
                st.caption("returned nothing — which is itself the reason "
                           "for an answer like \"nothing clashes\"")
    st.session_state.cited_nodes = [
        c.partition(":")[2] for step in trace["steps"] for c in step["returned"]]


def _draw_legend(key):
    """Colour key for what is on screen, the way FalkorDB Browser shows one.

    A force layout without a key is a hairball; with one it is a diagram.
    Built from the payload, so it can only name labels and relationships
    the picture actually contains.
    """
    def chips(entries):
        return " ".join(
            f"<span style='display:inline-block;margin:0 .35rem .35rem 0;"
            f"padding:.1rem .5rem;border-radius:999px;font-size:.75rem;"
            f"background:{e['color']}22;border:1px solid {e['color']};"
            f"color:inherit'>"
            f"<span style='display:inline-block;width:.5rem;height:.5rem;"
            f"border-radius:50%;background:{e['color']};"
            f"margin-right:.35rem'></span>{e['label']} {e['count']}</span>"
            for e in entries)

    left, right = st.columns(2)
    with left:
        st.caption("Labels")
        st.markdown(chips(key["labels"]) or "—", unsafe_allow_html=True)
    with right:
        st.caption("Relationships")
        st.markdown(chips(key["relationships"]) or "—",
                    unsafe_allow_html=True)


@st.dialog("The graph", width="large")
def window_graph():
    view = st.selectbox("View", list(VIEWS), index=0,
                        label_visibility="collapsed",
                        help="\"Conversation and subjects\" is the one that "
                             "shows what was said joined to what it was "
                             "about. \"Everything\" filters nothing at all, "
                             "which is the same picture FalkorDB Browser "
                             "draws from the same component.")
    changes = st.session_state.changes
    changed = [n["name"] for n in changes.added_nodes] if changes else []
    try:
        payload = graph_payload(store, labels=VIEWS[view],
                                cited=st.session_state.get("cited_nodes") or [],
                                changed=changed)
        components.html(canvas_html(payload, height=520, zoom_floor=1.15),
                        height=530, scrolling=False)
        _draw_legend(graph_legend(payload))
        st.caption(f"{len(payload['nodes'])} nodes · {len(payload['links'])} "
                   f"relationships · rendered by FalkorDB's own "
                   f"`@falkordb/canvas`. Outlined: what the last answer read. "
                   f"Dashed: retired, still readable — nothing is deleted.")
    except Exception as exc:
        st.error(f"Could not draw the graph: {type(exc).__name__}: {exc}")


@st.dialog("The Reviewer", width="large")
def window_reviewer():
    st.caption("A second agent. It is told only this, and nothing about your "
               f"conversation:\n\n> {REVIEWER_TRIGGER}")
    open_tasks = store.open_tasks("Reviewer")
    if open_tasks:
        st.warning(f"{len(open_tasks)} task(s) waiting")
        for task in open_tasks:
            st.markdown(f"- {task['title']}")
    else:
        st.caption("No open tasks. The Planner opens one when it leaves work "
                   "behind.")

    if st.button("Let the Reviewer work", width="stretch",
                 disabled=not open_tasks):
        with st.spinner("the Reviewer is reading the graph..."):
            try:
                turn = run_agent(build_reviewer, store, reviewer_session,
                                 REVIEWER_TRIGGER, speaker="system")
                st.session_state.reviewer_turn = {"text": turn.text,
                                                  "tools": turn.tools_used}
            except Exception as exc:
                st.session_state.reviewer_turn = {
                    "text": f"failed: {type(exc).__name__}: {exc}", "tools": []}
        st.rerun()

    if st.session_state.reviewer_turn:
        st.markdown(st.session_state.reviewer_turn["text"])
        if st.session_state.reviewer_turn["tools"]:
            st.caption("read from the graph: "
                       + ", ".join(st.session_state.reviewer_turn["tools"]))


@st.dialog("Create a workspace")
def window_new_workspace():
    """Name it and go.

    A popover was the wrong shape for this: Streamlit hangs a chevron off
    every popover label, so a one-icon trigger reads as a dropdown rather
    than as an action, and the form inside it had nowhere to put a Cancel.
    """
    st.caption("A workspace is a graph of its own. Nothing is shared with the "
               "others, and it exists from the first thing you say in it.")
    # A fresh widget key each time, so the field is empty on reopening.
    # Clearing it directly is not possible: it is a widget key, and Streamlit
    # refuses writes to those once the widget has rendered.
    seq = st.session_state.get("ws_seq", 0)
    name = st.text_input("Name", key=f"ws_new_{seq}",
                         placeholder="Name your workspace",
                         label_visibility="collapsed").strip().lower()

    cancel, create = st.columns(2)
    if cancel.button("Cancel", width="stretch"):
        st.rerun()
    if create.button("Create workspace", width="stretch", type="primary",
                     disabled=not name):
        if not NAME_OK.match(name):
            st.error("Letters, digits and underscores, starting with a letter.")
        elif name in workspaces():
            st.error(f"{workspace_label(name)} already exists.")
        else:
            st.session_state.tenant_name = name
            st.session_state.ws_seq = seq + 1
            st.rerun()


@st.dialog("Rename workspace")
def window_rename(current):
    st.caption(f"Renaming {workspace_label(current)}. Everything in it comes "
               f"with it \u2014 the graph is moved, not copied.")
    seq = st.session_state.get("ws_seq", 0)
    name = st.text_input("Name", key=f"ws_ren_{seq}", value=current,
                         label_visibility="collapsed").strip().lower()
    cancel, go = st.columns(2)
    if cancel.button("Cancel", width="stretch"):
        st.rerun()
    if go.button("Rename", width="stretch", type="primary",
                 disabled=not name or name == current):
        if not NAME_OK.match(name):
            st.error("Letters, digits and underscores, starting with a letter.")
        else:
            try:
                rename_tenant(current, name)
            except Exception as exc:
                st.error(str(exc))
            else:
                # The store is cached per tenant name, so the old entry would
                # still point at a Redis key that no longer exists.
                st.cache_resource.clear()
                st.session_state.tenant_name = name
                st.session_state.ws_seq = seq + 1
                st.rerun()


@st.dialog("Delete workspace")
def window_delete(current):
    doomed = get_store(current)
    convos = len([x for x in doomed.list_sessions(limit=99) if x["turns"]])
    nodes = doomed._read("MATCH (n) RETURN count(n)")[0][0]
    st.markdown(f"Delete **{workspace_label(current)}**?")
    st.caption(f"{convos} conversation(s) and {nodes} node(s) go with it. This "
               f"one really does delete \u2014 it is the only thing in this "
               f"project that does.")
    cancel, go = st.columns(2)
    if cancel.button("Cancel", width="stretch"):
        st.rerun()
    if go.button("Delete", width="stretch", type="primary"):
        delete_tenant(current)
        st.cache_resource.clear()
        left = [t for t in workspaces() if t != current]
        st.session_state.tenant_name = left[0] if left else "personal"
        st.session_state.ws_seq = st.session_state.get("ws_seq", 0) + 1
        st.rerun()


@st.dialog("Remove conversation")
def window_remove_chat(session_id):
    msgs = len(store.session_transcript(session_id))
    facts = store._read(
        "MATCH (:Session {id:$sid})-[:HAS_EVENT]->(e:Event) "
        "WITH collect(e.id) AS ids MATCH ()-[r]->() "
        "WHERE r.source_event IN ids AND r.valid_to IS NULL "
        "RETURN count(r)", sid=session_id)[0][0]
    st.markdown("Remove this conversation?")
    st.caption(
        f"The {msgs} message(s) go out of the sidebar, and the {facts} fact(s) "
        f"this conversation taught stop being current — so the agent will no "
        f"longer act on them. Nothing is destroyed: the facts keep an end "
        f"date and stay readable as history, which is why the reasoning "
        f"behind older answers still resolves.")
    cancel, go = st.columns(2)
    if cancel.button("Cancel", width="stretch"):
        st.rerun()
    if go.button("Remove", width="stretch", type="primary"):
        store.hide_session(session_id)
        if st.session_state.get("planner_session") == session_id:
            st.session_state.planner_session = None
        st.session_state.changes = None
        st.session_state.planner_steps = None
        st.rerun()


@st.dialog("Delete permanently")
def window_destroy_chat(session_id):
    g = store.deletion_preview(session_id)
    st.markdown("**Delete this conversation and everything it taught?**")
    st.caption(
        f"{g['messages']} message(s), {g['events']} event(s), "
        f"{g['facts']} fact(s), {g['nodes']} recorded node(s) and "
        f"{g['lookups']} reasoning trace(s) go from the graph, along with any "
        f"entity left with nothing attached to it. This cannot be undone.")
    if g["orphaned_insights"]:
        st.warning(f"{g['orphaned_insights']} insight(s) were consolidated "
                   f"from these episodes and will keep existing with fewer "
                   f"sources.")
    st.caption("**Remove** is the gentler option: it takes the conversation "
               "out of the sidebar and retires what it taught, leaving the "
               "history readable.")
    cancel, go = st.columns(2)
    if cancel.button("Cancel", width="stretch"):
        st.rerun()
    if go.button("Delete permanently", width="stretch", type="primary"):
        store.delete_session(session_id)
        if st.session_state.get("planner_session") == session_id:
            st.session_state.planner_session = None
        st.session_state.changes = None
        st.session_state.planner_steps = None
        st.rerun()


@st.dialog("Tool calls on the last turn", width="large")
def window_tools():
    for step in st.session_state.planner_steps or []:
        st.markdown(f"`{step['tool']}`")
        st.code(step["result"][:1200], language="json")


# ------------------------------------------------------------------- sidebar
with st.sidebar:
    st.markdown("<div class='mnemo-mark'>Mnemo</div>", unsafe_allow_html=True)

    # Workspace first: it decides which graph everything below is read from,
    # so it belongs above the things it governs rather than at the foot.
    options = workspaces()
    seq = st.session_state.get("ws_seq", 0)

    head, plus = st.columns([4, 1], vertical_alignment="center")
    head.caption("Workspace")
    with plus.container(key="wsadd"):
        if st.button(":material/add:", help="New workspace", type="tertiary"):
            window_new_workspace()

    pick, kebab = st.columns([5, 1], vertical_alignment="center")
    picked = pick.selectbox(
        "Workspace", options, format_func=workspace_label,
        label_visibility="collapsed",
        index=options.index(tenant) if tenant in options else 0)
    if picked != tenant:
        st.session_state.tenant_name = picked
        st.rerun()

    # The key carries a counter. A popover's open/closed state lives in the
    # browser and survives a rerun, so a menu item that opens a dialog leaves
    # the menu sitting on top of it -- covering the dialog's own Cancel.
    # Changing the key remounts the subtree, which closes the popover.
    kebab_box = kebab.container(key=f"wskebab{st.session_state.get('menu_seq', 0)}")
    with kebab_box.popover(":material/more_vert:",
                           help=f"Manage {workspace_label(tenant)}"):
        # Three rows and nothing else. Anything needing a field or a
        # confirmation opens its own dialog instead, which is also the only
        # place a Cancel can sensibly live.
        with st.container(key="wsmenu"):
            if st.button(":material/hub: View graph", type="tertiary",
                         width="stretch"):
                st.session_state.open_graph = True
                st.session_state.menu_seq = st.session_state.get("menu_seq", 0) + 1
                st.rerun()
            if st.button(":material/edit: Rename", type="tertiary",
                         width="stretch"):
                st.session_state.open_rename = True
                st.session_state.menu_seq = st.session_state.get("menu_seq", 0) + 1
                st.rerun()
        with st.container(key="wsmenudel"):
            if st.button(":material/delete: Delete", type="tertiary",
                         width="stretch"):
                st.session_state.open_delete = True
                st.session_state.menu_seq = st.session_state.get("menu_seq", 0) + 1
                st.rerun()

    st.divider()
    if st.button("✚  New chat", width="stretch"):
        new_chat(store)
        st.rerun()

    st.caption("Conversations")

    # A session nobody has spoken in has no title and nothing to return to,
    # and listing the one just opened put a second "New chat" directly under
    # the button that makes them. The button is the only way in.
    sessions = [s for s in store.list_sessions(limit=40)
                if s["user"] == USER and s["id"].startswith(SESSION_PREFIX)
                and s["turns"]]

    for sess in sessions:
        sid = sess["id"]
        active = sid == planner_session
        title = (sess["title"] or sess["preview"] or "New chat").strip()
        title = title.replace("\n", " ")

        # Renaming happens in place: the row becomes the title, editable.
        if st.session_state.get("renaming") == sid:
            with st.container(key=f"crename{sid}"):
                edited = st.text_input("rename", value=title,
                                       key=f"rn{sid}",
                                       label_visibility="collapsed")
            if edited.strip() and edited.strip() != title:
                store.rename_session(sid, edited)
                st.session_state.renaming = None
                st.rerun()
            continue

        row, menu = st.columns([5, 1], vertical_alignment="center")
        shown = title if len(title) <= 28 else title[:27] + "…"
        with row.container(key=f"crow{sid}"):
            if st.button(shown, key=f"go{sid}", width="stretch",
                         type="primary" if active else "tertiary",
                         help=f"{sess['turns']} messages · {sess['started_at']}"):
                if not active:
                    st.session_state.planner_session = sid
                    st.session_state.changes = None
                    st.session_state.planner_steps = None
                    st.rerun()

        with menu.container(key=f"cmenu{sid}{st.session_state.get('menu_seq', 0)}"):
            with st.popover(":material/more_horiz:", help="Manage"):
                with st.container(key=f"cmitems{sid}"):
                    if st.button(":material/edit: Rename", key=f"ren{sid}",
                                 type="tertiary", width="stretch"):
                        st.session_state.renaming = sid
                        st.session_state.menu_seq = \
                            st.session_state.get("menu_seq", 0) + 1
                        st.rerun()
                    if st.button(":material/visibility_off: Remove",
                                 key=f"del{sid}", type="tertiary",
                                 width="stretch",
                                 help="Out of the sidebar; what it taught is "
                                      "retired but stays readable"):
                        st.session_state.removing = sid
                        st.session_state.menu_seq = \
                            st.session_state.get("menu_seq", 0) + 1
                        st.rerun()
                with st.container(key=f"cmdel{sid}"):
                    if st.button(":material/delete_forever: Delete",
                                 key=f"des{sid}", type="tertiary",
                                 width="stretch",
                                 help="Destroy the conversation and the facts "
                                      "it taught. No undo."):
                        st.session_state.destroying = sid
                        st.session_state.menu_seq = \
                            st.session_state.get("menu_seq", 0) + 1
                        st.rerun()

    if SHOW_DEMO_CONTROLS:
        st.divider()
        if st.button("Reset both graphs", width="stretch"):
            with st.spinner("reseeding..."):
                reset_world()
            for key in ("planner_session", "reviewer_session", "changes",
                        "reviewer_turn", "last_path", "planner_steps"):
                st.session_state[key] = None
            st.cache_resource.clear()
            st.rerun()
        scenario = st.selectbox(
            "Example scenario", [m.key for m in MOMENTS], index=None,
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

    st.divider()
    st.caption("One graph per workspace. No tenant column, no tenant filter — "
               "nothing can cross between them.")

if st.session_state.pop("open_graph", False):
    window_graph()
if st.session_state.pop("open_rename", False):
    window_rename(tenant)
if st.session_state.pop("open_delete", False):
    window_delete(tenant)
if st.session_state.get("removing"):
    window_remove_chat(st.session_state.pop("removing"))
if st.session_state.get("destroying"):
    window_destroy_chat(st.session_state.pop("destroying"))

# ---------------------------------------------------------------------- chat
# The composer is read FIRST, even though Streamlit pins it to the foot of the
# window wherever it is called. Reading it last meant the message the person
# had just sent did not exist yet while the transcript was being drawn, so
# they watched a spinner on an unchanged page for the ten or twenty seconds
# the model took, with no sign their message had landed. Reading it here lets
# the turn be echoed the moment it is sent, which is what every chat app does.
prompt = st.chat_input("Message the Planner")

transcript = store.session_transcript(planner_session)

if not transcript and not prompt:
    # The opening screen is the composer and one line, as in any chat app.
    # Full width rather than a centre column, so the greeting stays on one
    # line, and dropped down the page so it sits above the composer rather
    # than clinging to the top of the window.
    st.markdown(
        "<div style='margin-top:16vh'>"
        "<h1 style='text-align:center;margin-bottom:.5rem'>"
        "What should I remember?</h1>"
        "<p style='text-align:center;opacity:.45;font-size:.85rem;"
        "line-height:1.6;max-width:34rem;margin:0 auto'>"
        "Anything still true tomorrow goes into the graph. Try "
        "<em>I have a doctor appointment on the 20th</em>, then open a new "
        "chat and ask <em>can I fly out on the 20th?</em></p></div>",
        unsafe_allow_html=True)
else:
    # A fixed-height scrolling area, so the transcript grows inside its own
    # box instead of pushing the composer and the action row down the page.
    with st.container(height=470, border=False):
        for entry in transcript:
            role = "user" if entry["speaker"] in (USER, "user") else "assistant"
            with st.chat_message(role):
                st.markdown(entry["text"])

        if prompt:
            # The new turn, drawn before the model is called. Streamlit paints
            # as the script runs, so both of these are on screen while the
            # agent works below -- the message sent, and an answer coming.
            with st.chat_message("user"):
                st.markdown(prompt)
            with st.chat_message("assistant"):
                with st.spinner("reading the graph…"):
                    try:
                        turn = run_agent(build_planner, store,
                                         planner_session, prompt)
                        st.session_state.planner_steps = [
                            {"tool": s.name, "result": s.result}
                            for s in turn.steps]
                    except Exception as exc:
                        # Record the failure in the conversation where
                        # possible, but never let logging an error raise a
                        # second one on top of it: that buries the cause.
                        try:
                            store.record_event(
                                planner_session, "Planner", "message",
                                f"(failed: {type(exc).__name__}: {exc})",
                                speaker="Planner")
                        except Exception:
                            st.error(f"{type(exc).__name__}: {exc}")
            # Reread from the graph, so what is on screen is what was stored.
            st.rerun()

        # The action row, under the last answer, where copy and retry live in
        # every other chat app. This is the whole difference between this and
        # every other chat app, so it sits exactly there.
        acts = st.columns([1, 1, 1, 1.15, 1, 4])
        if acts[0].button("Changes", key="a-chg", type="tertiary",
                          help="What this turn added, linked or retired"):
            window_changes()
        if acts[1].button("Why", key="a-why", type="tertiary",
                          help="The reads behind this answer, from the graph"):
            window_why()
        if acts[2].button("Graph", key="a-gph", type="tertiary",
                          help="The memory graph, drawn by FalkorDB's canvas"):
            window_graph()
        if acts[3].button("Reviewer", key="a-rev", type="tertiary",
                          help="The second agent, and what it was left"):
            window_reviewer()
        if st.session_state.planner_steps and acts[4].button(
                "Tools", key="a-tls", type="tertiary",
                help=f"{len(st.session_state.planner_steps)} tool call(s) and "
                     f"what each returned"):
            window_tools()

# ---------------------------------------------------- underneath the composer
with st.container(key="modelbar"):
    status, picker = st.columns([5, 5])
    default = os.environ.get("LLM_CHOICE") or next(iter(CHOICES))
    picker.selectbox(
        "Model", list(CHOICES), label_visibility="collapsed",
        index=list(CHOICES).index(default) if default in CHOICES else 0,
        key="model_choice",
        help="Switching provider changes nothing else: same agents, same "
             "tools, same prompts, same graph.")
    try:
        adapter()
    except Exception as exc:
        status.caption(f":red[{type(exc).__name__}: {exc}]")
