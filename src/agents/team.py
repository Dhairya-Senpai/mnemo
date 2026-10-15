"""Two agents that share nothing but the graph.

This module exists to make one claim checkable rather than asserted: the
Reviewer never receives the Planner's output. It is handed a fixed trigger
sentence that names no project, no decision and no person, and everything it
then knows it read from the graph.

`find_prompt_leaks` is the check. It scans what the Reviewer was *told* --
the system prompt and every user turn -- for content that should only have
reached it through a tool result. Anything it finds is a handoff that
cheated. The check runs in the tests and is cheap enough to run in the demo,
which is the point: "no prompt was passed" is a measurement, not a promise.

Note what the check deliberately ignores: tool results, and the agent's own
output. A Reviewer that names "Spring Launch" in its answer is fine -- it
learned it from the graph. A Reviewer that was *given* "Spring Launch" is not.
"""

from dataclasses import dataclass, field

from .loop import Agent, Turn, load_prompt
from .toolbox import PLANNER_TOOLS, REVIEWER_TOOLS, Toolbox

PLANNER = "Planner"
REVIEWER = "Reviewer"

# The only thing the Reviewer is ever told. Static, and deliberately empty of
# specifics: if this sentence ever grows a project name, the handoff stops
# being a graph handoff and becomes message passing.
REVIEWER_TRIGGER = (
    "Check whether any work has been assigned to you. If there is, claim it, "
    "read what it concerns, and act on it. If nothing is assigned to you, say so."
)


def build_planner(store, session_id, llm, prompt=None, **kwargs) -> Agent:
    return Agent(
        name=PLANNER,
        toolbox=Toolbox(store, PLANNER, session_id, allowed=PLANNER_TOOLS),
        llm=llm,
        prompt=prompt if prompt is not None else load_prompt("planner"),
        **kwargs,
    )


def build_reviewer(store, session_id, llm, prompt=None, **kwargs) -> Agent:
    return Agent(
        name=REVIEWER,
        toolbox=Toolbox(store, REVIEWER, session_id, allowed=REVIEWER_TOOLS),
        llm=llm,
        prompt=prompt if prompt is not None else load_prompt("reviewer"),
        **kwargs,
    )


def find_prompt_leaks(agent: Agent, turn: Turn, needles) -> list[str]:
    """Which of `needles` reached the model as prose rather than tool output.

    Inputs only: the system prompt and the text of user turns. Tool results
    are excluded because that is the legitimate channel, and assistant text
    is excluded because it is the model's own output, derived from what the
    tools returned.
    """
    told = [agent.system_prompt()]
    for msg in turn.messages:
        if msg.role == "user" and msg.text:
            told.append(msg.text)
    blob = "\n".join(told).lower()
    return [n for n in needles if n.lower() in blob]


@dataclass
class Handoff:
    """One Planner turn, one Reviewer turn, and what crossed between them."""
    planner: Turn
    reviewer: Turn
    task_id: str | None = None
    leaks: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        """True when the Reviewer was told nothing specific to this task."""
        return not self.leaks

    def summary(self) -> str:
        channel = "graph only" if self.clean else f"LEAKED: {self.leaks}"
        return (
            f"Planner used {self.planner.tools_used}; "
            f"Reviewer used {self.reviewer.tools_used}; "
            f"task={self.task_id or 'none'}; {channel}"
        )


def tasks_created(turn: Turn) -> list[str]:
    """Task ids the Planner opened during a turn, read from its own steps."""
    out = []
    for step in turn.steps:
        if step.name == "create_task" and not step.is_error:
            try:
                import json
                tid = json.loads(step.result).get("task_id")
            except Exception:
                tid = None
            if tid:
                out.append(tid)
    return out


def run_handoff(store, user_message, planner_llm, reviewer_llm,
                planner_session, reviewer_session, leak_needles=()) -> Handoff:
    """Planner acts on the user's message; Reviewer then works from the graph.

    The two sessions are separate by default, so the Reviewer is also proving
    that memory survives a session boundary, not just a function call.
    `reviewer_llm` is told only REVIEWER_TRIGGER.
    """
    planner = build_planner(store, planner_session, planner_llm)
    planner_turn = planner.run(user_message)

    reviewer = build_reviewer(store, reviewer_session, reviewer_llm)
    reviewer_turn = reviewer.run(REVIEWER_TRIGGER)

    opened = tasks_created(planner_turn)
    return Handoff(
        planner=planner_turn,
        reviewer=reviewer_turn,
        task_id=opened[0] if opened else None,
        leaks=find_prompt_leaks(reviewer, reviewer_turn, leak_needles),
    )
