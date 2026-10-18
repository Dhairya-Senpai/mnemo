"""Render the graph as DOT for Streamlit's graphviz_chart.

Built as a plain string rather than through the graphviz package, which keeps
the dependency list to Streamlit alone -- graphviz_chart accepts a dot string
and renders it client side.

Two things the view has to show, or the demo loses its point:

* **Retired facts are still there**, drawn greyed and dashed rather than
  removed. If retiring looked like deleting, the whole versioning argument
  would be invisible.
* **What just changed** is outlined, so a viewer can watch the graph gain
  nodes after a moment instead of being told it did.
"""

NODE_STYLE = {
    "Person":    ("#dbeafe", "#1e40af"),
    "Project":   ("#dcfce7", "#166534"),
    "System":    ("#ffedd5", "#9a3412"),
    "Decision":  ("#fef9c3", "#854d0e"),
    "Procedure": ("#ede9fe", "#5b21b6"),
    "Insight":   ("#fce7f3", "#9d174d"),
    "Task":      ("#f1f5f9", "#334155"),
    "Ticket":    ("#fee2e2", "#991b1b"),
    "Agent":     ("#e0f2fe", "#075985"),
    "Session":   ("#f8fafc", "#475569"),
    "Event":     ("#f8fafc", "#64748b"),
}

WORLD = ("Person", "Project", "System")
SEMANTIC = ("Decision", "Procedure", "Insight")
OPERATIONAL = ("Task", "Ticket", "Agent")
EPISODIC = ("Session", "Event")
# What a turn was about, and the turns themselves. Separate from EPISODIC
# because a Topic is not an episode -- it is the subject episodes hang off,
# and it is the only one of these worth looking at on its own.
CONVERSATION = ("Topic", "Event", "Session")

# `None` means no label filter: show whatever this workspace happens to hold,
# minus the plumbing. Every other entry is an allow-list built for the company
# world, which silently hides open-layer facts -- a personal workspace whose
# nodes are Place, Food or Thing rendered as a single node under those.
# `None` means no allow-list: show whatever this workspace holds, minus the
# plumbing. `ALL` means exclude nothing at all.
#
# Every allow-list here is a tuple of *closed-schema* labels, which is a trap
# worth naming: an open-layer node (Appointment, Language, Thing) and a Topic
# are in none of them, so an allow-list view silently cannot show them.
# `Topic` was missing from all five of these, which meant no view in the app
# could draw a conversation joined to its subjects -- the one thing the
# conversation layer exists to make visible.
ALL = "*"

# Two, deliberately. The picker once had five and most of them were
# allow-lists built for the seeded company world -- useful while that was
# the only demo, confusing once the product was a chat app. What is worth
# switching between is "the things I know" and "all of it".
VIEWS = {
    # Facts and the subjects they came up under, with the plumbing left
    # out: tell it you like Nike shoes and this is where
    # you -LIKE-> Nike shoes appears, uncluttered by the fifty Event
    # nodes that recorded the conversation around it.
    "What this workspace knows": None,
    # Nothing filtered at all -- the same picture FalkorDB Browser draws
    # from the same database, through the same @falkordb/canvas component.
    "Falkor": ALL,
}

# Kept for the offline Graphviz renderer in this module, which still
# accepts an explicit allow-list.
LEGACY_VIEWS = {
    "Conversation and subjects": CONVERSATION,
    "World and decisions": WORLD + SEMANTIC,
    "World only": WORLD,
}


def _esc(text):
    return str(text).replace('"', '\\"')


def _wrap(text, width=22):
    """Break long names so nodes stay readable."""
    words, lines, line = str(text).split(), [], ""
    for word in words:
        if len(line) + len(word) + 1 > width and line:
            lines.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    if line:
        lines.append(line)
    return "\\n".join(lines) or str(text)


def build_dot(store, labels=WORLD + SEMANTIC, path=None, changed=None,
              max_nodes=60):
    """DOT for the current graph, limited to `labels`.

    `path` is a list of node names to outline as the explanation chain.
    `changed` is a set of node names added by the last moment.
    """
    path = set(path or [])
    changed = set(changed or [])
    label_list = ", ".join(f"'{l}'" for l in labels)

    nodes = store._read(
        f"MATCH (n) WHERE labels(n)[0] IN [{label_list}] "
        f"RETURN labels(n)[0], coalesce(n.name, n.title, n.text, n.id), n.valid_to "
        f"LIMIT {int(max_nodes)}"
    )
    edges = store._read(
        f"MATCH (a)-[r]->(b) "
        f"WHERE ID(r) >= 0 AND labels(a)[0] IN [{label_list}] "
        f"  AND labels(b)[0] IN [{label_list}] "
        f"RETURN type(r), coalesce(a.name, a.title, a.text, a.id), "
        f"       coalesce(b.name, b.title, b.text, b.id), r.valid_to"
    )

    out = [
        "digraph memory {",
        '  rankdir=LR;',
        '  bgcolor="transparent";',
        '  node [shape=box style="rounded,filled" fontname="Helvetica" '
        'fontsize=10 margin="0.12,0.07"];',
        '  edge [fontname="Helvetica" fontsize=8 color="#94a3b8"];',
    ]

    def render(attrs):
        return " ".join(f"{k}={v}" for k, v in attrs.items())

    for label, name, valid_to in nodes:
        fill, border = NODE_STYLE.get(label, ("#ffffff", "#334155"))
        # A dict, not a list: highlighting overrides the base colour rather
        # than emitting the attribute twice and relying on precedence.
        attrs = {"label": f'"{_esc(_wrap(name))}"', "fillcolor": f'"{fill}"',
                 "color": f'"{border}"'}
        if valid_to is not None:                      # retired, not deleted
            attrs["style"] = '"rounded,filled,dashed"'
            attrs["fontcolor"] = '"#94a3b8"'
        if name in path:
            attrs.update(penwidth="3", color='"#dc2626"')
        elif name in changed:
            attrs.update(penwidth="3", color='"#16a34a"')
        out.append(f'  "{_esc(name)}" [{render(attrs)}];')

    for rel, src, dst, valid_to in edges:
        attrs = {"label": f'"{_esc(rel)}"'}
        if valid_to is not None:
            attrs.update(label=f'"{_esc(rel)} (retired)"', style="dashed",
                         color='"#cbd5e1"', fontcolor='"#cbd5e1"')
        elif src in path and dst in path:
            attrs.update(penwidth="2.5", color='"#dc2626"',
                         fontcolor='"#dc2626"')
        out.append(f'  "{_esc(src)}" -> "{_esc(dst)}" [{render(attrs)}];')

    out.append("}")
    return "\n".join(out)
