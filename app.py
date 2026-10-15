"""Agent Memory Graph - the demo UI.

    streamlit run app.py

Needs FalkorDB running and the graphs seeded (python scripts/seed.py).

This is a demo runner rather than a free-form chat, on purpose. The models
are scripted, so a chat box would only be able to answer what it had been
told to answer -- pretending otherwise would be dishonest. What is real here
is everything else: the Cypher, the graph writes, the versioning, the task
claim, and the before/after diff in the memory-changes panel.
"""

import json
import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from demo import MOMENTS, reset_world  # noqa: E402
from demo.graphview import VIEWS, WORLD, build_dot  # noqa: E402
from memory import MemoryStore, diff_snapshots  # noqa: E402

st.set_page_config(page_title="Agent Memory Graph", layout="wide",
                   page_icon="//")


@st.cache_resource
def get_store(tenant):
    return MemoryStore(tenant)


def init():
    st.session_state.setdefault("result", None)
    st.session_state.setdefault("changes", None)
    st.session_state.setdefault("moment", None)
    st.session_state.setdefault("ran", [])


init()

# ------------------------------------------------------------------- sidebar
with st.sidebar:
    st.title("Agent Memory Graph")
    st.caption("Memory that lives in a FalkorDB graph. "
               "Models are scripted; the graph is real.")

    tenant = st.selectbox("Tenant", ["acme", "globex"], index=0)
    store = get_store(tenant)

    if st.button("Reset both graphs", width="stretch"):
        with st.spinner("reseeding..."):
            reset_world()
        st.session_state.update(result=None, changes=None, moment=None, ran=[])
        st.cache_resource.clear()
        st.rerun()

    st.divider()
    st.subheader("Demo moments")
    st.caption("1 to 3 are a chain. Run them in order after a reset.")

    for moment in MOMENTS:
        done = "- done" if moment.key in st.session_state.ran else ""
        if st.button(f"{moment.number}. {moment.title} {done}",
                     key=f"btn-{moment.key}", width="stretch"):
            target = get_store("acme")        # moments act on the acme world
            before = target.snapshot()
            try:
                result = moment.run(target)
                st.session_state.changes = diff_snapshots(before,
                                                          target.snapshot())
                st.session_state.result = result
                st.session_state.moment = moment
                if moment.key not in st.session_state.ran:
                    st.session_state.ran.append(moment.key)
            except Exception as exc:          # a failed moment must not hide
                st.session_state.result = None
                st.session_state.moment = moment
                st.session_state.changes = None
                st.session_state.error = f"{type(exc).__name__}: {exc}"
            st.rerun()

    st.divider()
    view = st.selectbox("Graph view", list(VIEWS), index=0)
    st.caption("Retired facts stay on the graph, drawn dashed and grey. "
               "Nothing is ever deleted.")

# ---------------------------------------------------------------------- main
moment = st.session_state.moment
result = st.session_state.result
changes = st.session_state.changes

if moment is None:
    st.header("Pick a moment from the left")
    st.markdown(
        "Each one runs the real agents against the real graph. After it runs "
        "you will see what the agent said, which tools it called, **what "
        "changed in the graph**, and the graph itself with the explanation "
        "chain highlighted.\n\n"
        "Start with **Reset both graphs**, then moments 1, 2 and 3 in order - "
        "they tell one story across two sessions."
    )
else:
    st.header(f"{moment.number}. {moment.title}")
    st.caption(f"Shows: {moment.shows}  ·  Track {moment.track}")

    if result is None:
        st.error(st.session_state.get("error", "the moment failed"))
    else:
        if result.note:
            st.info(result.note)

        left, right = st.columns([3, 2], gap="large")

        with left:
            st.subheader("What happened")
            st.markdown(result.narration)

            if result.steps:
                st.subheader("Tools the agent called")
                for step in result.steps:
                    icon = "!" if step["error"] else "+"
                    with st.expander(f"{icon} {step['tool']}"):
                        st.code(json.dumps(step["args"], indent=2), language="json")
                        st.caption("returned")
                        st.code(step["result"][:2000], language="json")

            if result.path:
                st.subheader("Explanation chain")
                st.markdown(" → ".join(f"**{p}**" for p in result.path))

            for key, value in result.facts.items():
                st.subheader(key)
                if isinstance(value, str):
                    st.write(value)
                else:
                    st.json(value, expanded=False)

        with right:
            st.subheader("Memory changes")
            if changes is None or changes.empty:
                st.caption("Nothing changed - this moment only read the graph.")
            else:
                cols = st.columns(3)
                for col, (label, count) in zip(cols, changes.counts.items()):
                    col.metric(label, count)
                # Green for added, amber for retired. The +/~ prefix is
                # already in the text; st.success/warning only take an emoji
                # or a Material shortcode as `icon`, so no icon is passed.
                for line in changes.lines():
                    if line.startswith("+"):
                        st.success(line)
                    else:
                        st.warning(line)

st.divider()
st.subheader(f"The graph - tenant {tenant}")

added_names = []
if changes:
    added_names = [n["name"] for n in changes.added_nodes]

try:
    dot = build_dot(
        get_store(tenant),
        labels=VIEWS.get(view, WORLD),
        path=result.path if result else None,
        changed=added_names,
    )
    # Explicit height: graphviz sizes to its content, which on a small graph
    # leaves it too cramped to read from across a room while recording.
    st.graphviz_chart(dot, width="stretch", height=560)
    st.caption("Red outline: the explanation chain. Green outline: added by "
               "the last moment. Dashed and grey: retired, still readable.")
except Exception as exc:
    st.error(f"Could not draw the graph: {type(exc).__name__}: {exc}")
    st.caption("Is FalkorDB running and seeded? python scripts/seed.py")
