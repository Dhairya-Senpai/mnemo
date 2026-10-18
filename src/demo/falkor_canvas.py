"""Render the memory graph with FalkorDB's own canvas web component.

`@falkordb/canvas` is the force-directed visualiser from the FalkorDB UI
repository, published as a framework-free custom element. Using it rather
than drawing our own means the graph in this app looks like the graph in
FalkorDB Browser, and the force layout, hit-testing, curved parallel edges
and viewport culling are theirs rather than ours.

Three of its configuration hooks happen to line up exactly with conventions
this project already had, which is why it replaced the Graphviz rendering:

* `captionsKeys` -- nodes are captioned by `name`, falling back through
  `title` and `text`, which is the same coalesce every query here uses.
* `linkLineDash` -- a retired edge is drawn dashed instead of vanishing.
  Nothing in this system is deleted, and the picture has to say so.
* `isNodeSelected` -- highlights what the last answer actually read, so the
  reasoning trace and the picture agree.

The component is loaded from jsDelivr at a pinned version. Streamlit renders
it inside a sandboxed iframe, so nothing here can reach the rest of the page.

`graphview.py` still holds the Graphviz renderer. It is no longer wired into
the app but needs no network, so it stays as the offline fallback.
"""

import json

CANVAS_VERSION = "0.3.3"
CDN = f"https://cdn.jsdelivr.net/npm/@falkordb/canvas@{CANVAS_VERSION}/+esm"

# Same palette as the Graphviz view, so the two renderers agree.
NODE_COLOR = {
    "Person": "#60a5fa",
    "Project": "#4ade80",
    "System": "#fb923c",
    "Decision": "#facc15",
    "Procedure": "#a78bfa",
    "Insight": "#f472b6",
    "Task": "#94a3b8",
    "Ticket": "#f87171",
    "Agent": "#38bdf8",
    "Session": "#64748b",
    "Event": "#64748b",
    # The hub of the conversation layer, and the thing a viewer should find
    # first. It was falling through to DEFAULT_COLOR, which put the most
    # important node in the picture in the same grey as everything unknown.
    "Topic": "#e879f9",
    "Lookup": "#334155",
}
DEFAULT_COLOR = "#cbd5e1"

# Retired nodes and edges are drawn, but muted. They are history, not news.
RETIRED_COLOR = "#475569"


def graph_payload(store, labels, cited=(), changed=(), max_nodes=120):
    """Read the graph as the shape `<falkordb-canvas>.setData()` expects.

    Node ids are FalkorDB's internal ids rather than names, because the
    component matches links to nodes by id and names are not unique across
    labels. `snapshot()` cannot be reused for this: it reports an edge's
    endpoints by name, and the canvas needs them by id.
    """
    cited, changed = set(cited or ()), set(changed or ())

    # labels=None means "whatever is in here", which is the only view that
    # works for a workspace built by conversation rather than seeded from the
    # company world: its nodes carry labels the agent invented. The plumbing
    # is still excluded -- an Event or a Lookup is not a thing you know.
    if labels == "*":
        # Exclude nothing. The only view that shows the graph as FalkorDB
        # Browser shows it, which is the honest comparison to be able to
        # make: same component, same data, no filter.
        node_where = "true"
        edge_where = "true"
    elif labels is None:
        node_where = ("NOT n:Event AND NOT n:Session AND NOT n:Lookup "
                      "AND NOT n:Agent")
        edge_where = ("NOT a:Event AND NOT a:Session AND NOT a:Lookup "
                      "AND NOT a:Agent AND NOT b:Event AND NOT b:Session "
                      "AND NOT b:Lookup AND NOT b:Agent")
    else:
        label_list = ", ".join(f"'{l}'" for l in labels)
        node_where = f"labels(n)[0] IN [{label_list}]"
        edge_where = (f"labels(a)[0] IN [{label_list}] "
                      f"AND labels(b)[0] IN [{label_list}]")

    rows = store._read(
        f"MATCH (n) WHERE {node_where} "
        f"RETURN ID(n), labels(n)[0], "
        f"       coalesce(n.name, n.title, n.text, n.id), n.valid_to "
        f"LIMIT {int(max_nodes)}"
    )
    # An Event has no name, so it was captioned by its id --
    # `evt-065e1752121669-e791b6`, which is unreadable and identical-looking
    # across fifty nodes. Numbering them in the order they happened says the
    # one thing about an episode that a picture can usefully carry: when it
    # was, relative to everything else. The order is (ts, id) for the reason
    # given in SCHEMA.md -- second-precision timestamps tie within a turn,
    # and ids carry a microsecond prefix that breaks the tie correctly.
    ordinals = {
        row[0]: (i + 1, row[1])
        for i, row in enumerate(store._read(
            "MATCH (e:Event) RETURN ID(e), e.speaker ORDER BY e.ts, e.id"))
    }

    nodes, known = [], set()
    for node_id, label, name, valid_to in rows:
        if label == "Event" and node_id in ordinals:
            seq, speaker = ordinals[node_id]
            name = f"#{seq} {speaker}" if speaker else f"#{seq}"
        known.add(node_id)
        retired = valid_to is not None
        is_cited, is_changed = name in cited, name in changed
        nodes.append({
            "id": node_id,
            "labels": [label],
            "color": RETIRED_COLOR if retired else NODE_COLOR.get(label, DEFAULT_COLOR),
            "visible": True,
            # Node captions are sized as a fraction of the radius, so the
            # default 9 renders names too small to read at a zoom that fits
            # the whole graph. What the answer touched is drawn larger again.
            "size": 26 if (is_cited or is_changed) else 20,
            # Without this a node is captioned by its id, because `caption`
            # defaults to 'id' per node regardless of the config's
            # captionsKeys. Every node here carries `name`.
            "caption": "name",
            "data": {
                "name": name,
                "label": label,
                # Consumed by isNodeSelected / the tooltip, not by the layout.
                "retired": retired,
                "cited": is_cited,
                "changed": is_changed,
            },
        })

    # `ID(r) >= 0` is not decoration: an unreferenced edge alias is matched
    # existentially in FalkorDB, which under-reports the parallel edges that
    # versioning creates -- and parallel edges are exactly what this view
    # exists to show.
    edges = store._read(
        f"MATCH (a)-[r]->(b) WHERE ID(r) >= 0 AND {edge_where} "
        f"RETURN ID(r), type(r), ID(a), ID(b), r.valid_to"
    )
    links = []
    for edge_id, rel, src, dst, valid_to in edges:
        if src not in known or dst not in known:
            continue                      # endpoint fell outside max_nodes
        retired = valid_to is not None
        links.append({
            "id": edge_id,
            "relationship": f"{rel} (retired)" if retired else rel,
            "color": RETIRED_COLOR if retired else "#94a3b8",
            "source": src,
            "target": dst,
            "visible": True,
            "data": {"retired": retired},
        })

    return {"nodes": nodes, "links": links}


def legend(payload):
    """The labels and relationship types actually present, with their colours.

    FalkorDB Browser shows these down the side and it is most of why its
    view reads so much faster than a bare force layout -- a hairball with a
    key is a diagram, a hairball without one is a hairball.

    Counted from the payload rather than from the schema, so it describes
    what is on screen. A legend listing types the graph does not hold would
    be the same kind of lie as a reasoning trace that cites a tool it never
    called.
    """
    labels, rels = {}, {}
    for node in payload["nodes"]:
        name = node["labels"][0]
        entry = labels.setdefault(
            name, {"label": name, "count": 0,
                   "color": NODE_COLOR.get(name, DEFAULT_COLOR)})
        entry["count"] += 1
    for link in payload["links"]:
        name = link["relationship"]
        entry = rels.setdefault(
            name, {"label": name, "count": 0, "color": link["color"]})
        entry["count"] += 1
    by_count = lambda d: sorted(d.values(), key=lambda e: (-e["count"],
                                                          e["label"]))
    return {"labels": by_count(labels), "relationships": by_count(rels)}


def canvas_html(payload, height=460, background="#0e1117",
                foreground="#e6e6e6", zoom_floor=1.0):
    """A self-contained page hosting the component, for st.components.v1.html.

    Returned as a string rather than written to disk because Streamlit serves
    component HTML from memory into a sandboxed iframe.
    """
    data = json.dumps(payload)
    # -900 is right for the thirty-node seeded world and far too much for a
    # conversation graph several times that size. Eased towards -200 as the
    # node count grows, with a floor so a large graph does not collapse into
    # an unreadable knot either.
    count = max(len(payload["nodes"]), 1)
    charge = -900 if count <= 30 else max(-900 * 30 // count, -250)
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><style>
  html, body {{ margin:0; padding:0; background:{background}; overflow:hidden; }}
  falkordb-canvas {{ display:block; width:100%; height:{height}px; }}
  #err {{ color:#f87171; font:12px/1.5 system-ui; padding:10px; }}
</style></head>
<body>
<falkordb-canvas id="g"></falkordb-canvas>
<div id="err"></div>
<script type="module">
  const DATA = {data};
  try {{
    await import({CDN!r});
    const el = document.getElementById('g');
    el.setConfig({{
      width: window.innerWidth,
      height: {height},
      backgroundColor: {background!r},
      foregroundColor: {foreground!r},
      // Caption by name, then the fields other node kinds use instead.
      captionsKeys: ['name', 'title', 'text'],
      // What the last answer read, outlined.
      isNodeSelected: (n) => !!(n.data && n.data.cited),
      // Retired edges stay visible, dashed. Nothing here is ever deleted.
      linkLineDash: (l) => (l.data && l.data.retired) ? [4, 3] : [],
      // Captions fill the node, and nodes are sized up in graph_payload, so
      // names stay legible at a zoom that fits the whole graph.
      nodeStyle: {{ textFillRatio: 0.8 }},
      // Repulsion is scaled down as the graph grows. -900 was tuned on the
      // seeded company world of about thirty nodes; left fixed, a seventy
      // node conversation spreads so far that the fit lands at a zoom the
      // clamp below then undoes, and the canvas renders empty.
      simulation: {{ chargeStrength: {charge} }},
    }});
    el.setData(DATA);
    // The force simulation needs to settle before a fit means anything.
    // Fitting alone is not enough: with twenty-odd nodes the fit lands near
    // zoom 0.4, where a 20-unit node is eight screen pixels and the
    // component skips drawing its caption entirely. So the fit is clamped to
    // a floor at which names and relationship types are legible, and the
    // viewer pans -- which is how FalkorDB Browser behaves too.
    setTimeout(() => {{
      try {{
        el.zoomToFit(1.0);
        setTimeout(() => {{
          try {{
            // Only nudge up to the floor when the fit is already near it.
            // Clamping unconditionally is what blanked the canvas: on a
            // large graph the fit is far below the floor, so zooming back
            // to the floor pushed every node outside the viewport. Below
            // that the graph is too big for legible captions anyway, and
            // seeing its shape is worth more than reading it.
            const z = el.getZoom();
            if (z < {zoom_floor} && z > {zoom_floor} * 0.45) {{
              el.zoom({zoom_floor});
            }}
            // Without this the zoom is stored but the canvas never
            // repaints, so the captions stay missing at any zoom level.
            if (el.refresh) el.refresh();
          }} catch (e) {{}}
        }}, 450);
      }} catch (e) {{}}
    }}, 900);
    addEventListener('resize', () => el.setConfig({{ width: window.innerWidth,
                                                     height: {height} }}));
  }} catch (e) {{
    document.getElementById('err').textContent =
      'FalkorDB canvas failed to load: ' + e + ' (needs network for the CDN)';
  }}
</script>
</body></html>"""
