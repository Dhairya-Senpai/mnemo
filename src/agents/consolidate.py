"""Consolidation: many episodes in, one lasting fact out.

The division of labour matters more than the code here. The graph decides
what to consolidate, refuses to consolidate the same episodes twice, and
stores the result with a traversable link to every source. The model writes
one sentence of prose. Take FalkorDB away and what is left is a summariser
with no idea what to summarise, no memory of what it already did, and no way
to justify its output.

The model is also allowed to decline. An `Insight` that says nothing is worse
than no insight, so the prompt can answer `keep: false` and nothing is
written -- "memory got smaller" only counts if the thing kept is worth more
than the things it replaced.
"""

from dataclasses import dataclass

from llm.structured import ask_json

from .loop import load_prompt

MIN_EPISODES = 3


@dataclass
class Consolidation:
    """What one consolidation attempt did."""
    label: str
    name: str
    episodes: int
    kept: bool
    reason: str
    text: str = ""
    insight_id: str | None = None

    def summary(self) -> str:
        verb = f"kept as {self.insight_id}" if self.kept else "declined"
        return (f"{self.name}: {self.episodes} episodes -> {verb} ({self.reason})")


def _render_episodes(episodes) -> str:
    return "\n".join(f"- [{e['ts']}] ({e['kind']}) {e['summary']}" for e in episodes)


def consolidate_entity(store, llm, session_id, agent, label, name,
                       cache=None, min_episodes=MIN_EPISODES) -> Consolidation:
    """Fold one entity's un-absorbed episodes into a single Insight."""
    episodes = store.events_about(label, name, only_unconsolidated=True)
    if len(episodes) < min_episodes:
        return Consolidation(label, name, len(episodes), False,
                             f"only {len(episodes)} un-consolidated episodes")

    prompt = (
        f"These episodes all concern the {label.lower()} {name!r}.\n\n"
        f"{_render_episodes(episodes)}\n\n"
        f"Write the one lasting fact worth keeping."
    )
    answer = ask_json(
        llm, prompt,
        required_keys=("keep", "text", "reason"),
        system=load_prompt("consolidate"),
        cache=cache,
    )

    reason = str(answer.get("reason", "")).strip()
    text = str(answer.get("text", "")).strip()
    if not answer.get("keep") or not text:
        # Nothing written, so these episodes stay available to a later pass.
        return Consolidation(label, name, len(episodes), False,
                             reason or "model declined")

    insight_id = store.record_insight(
        session_id, agent, label, name, text,
        source_event_ids=[e["id"] for e in episodes],
    )
    return Consolidation(label, name, len(episodes), True, reason or "consolidated",
                         text=text, insight_id=insight_id)


def run_consolidation(store, llm, session_id, agent, min_episodes=MIN_EPISODES,
                      max_entities=3, cache=None) -> list[Consolidation]:
    """Consolidate the entities the graph says are worth consolidating.

    Candidate selection is a graph query, so this never scans and never
    re-reads episodes an Insight already absorbed.
    """
    candidates = store.consolidation_candidates(min_events=min_episodes,
                                                limit=max_entities)
    return [
        consolidate_entity(store, llm, session_id, agent,
                           c["label"], c["name"], cache=cache,
                           min_episodes=min_episodes)
        for c in candidates
    ]
