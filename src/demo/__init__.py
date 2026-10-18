"""Rendering for the graph panel.

This package used to hold the scripted demo moments too -- ten beats replayed
through a fake adapter. They were removed on 18 October along with the fake
adapter itself: the app had outgrown a demo rig in its sidebar, and shipping a
second, scripted way to drive the agents meant shipping code that only ever
ran for a recording. The graph data those moments produced is now written
directly by `scripts/seed_history.py`.

What is left renders the graph: `graphview` builds the two views, and
`falkor_canvas` wraps FalkorDB's own canvas web component.
"""
