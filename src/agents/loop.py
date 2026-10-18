"""The agent loop: ask the model, run the tools it asks for, repeat.

The loop is ours rather than the SDK's tool runner, on purpose. The runner
would drive this cycle for us, but the cycle would then live inside a vendor
helper and swapping providers would mean rewriting the agent. Here the loop
talks only to `LLMAdapter`, so a different provider is a config change.

Every turn is also recorded in the graph. An agent that thinks and acts
without leaving an episodic trace would break the central claim of the
project, so `Agent.run` writes the user's message as an Event before calling
the model, and the tools write their own events as they go.

Reads are recorded too. Each one becomes a Lookup node, and the answer the
agent finally gives points at the lookups behind it, so the reasoning can be
read back out of the graph later -- by a human, or by the agent itself.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

from llm.base import LLMAdapter
from llm.types import STOP_TOOL_USE, Message, ToolCall, ToolResult

PROMPT_DIR = Path(__file__).resolve().parent.parent.parent / "prompts"

# Token discipline (PROJECT_CONTEXT.md section 9): cap tool calls per question.
# The cap is a backstop against a loop that never converges, not a target.
DEFAULT_MAX_TOOL_CALLS = 6


def load_prompt(name: str) -> str:
    """Prompts live in plain text files, with no provider-specific syntax."""
    path = PROMPT_DIR / f"{name}.txt"
    if not path.exists():
        raise FileNotFoundError(f"no prompt file at {path}")
    return path.read_text(encoding="utf-8").strip()


@dataclass
class Step:
    """One tool call and its result, kept for the UI's memory-changes panel."""
    name: str
    arguments: dict
    result: str
    is_error: bool


@dataclass
class Turn:
    """What one `Agent.run` produced."""
    text: str
    steps: list[Step] = field(default_factory=list)
    messages: list[Message] = field(default_factory=list)
    stopped_early: bool = False       # hit the tool-call cap
    lookups: list[str] = field(default_factory=list)   # Lookup ids behind it

    @property
    def tools_used(self) -> list[str]:
        return [s.name for s in self.steps]


class Agent:
    """One named agent with a role prompt, an adapter, and a toolbox."""

    def __init__(self, name, toolbox, llm: LLMAdapter, prompt=None,
                 prompt_name=None, max_tool_calls=DEFAULT_MAX_TOOL_CALLS,
                 standing_rules=False):
        if prompt is None and prompt_name is None:
            raise ValueError("pass prompt or prompt_name")
        self.name = name
        self.toolbox = toolbox
        self.llm = llm
        self.prompt = prompt if prompt is not None else load_prompt(prompt_name)
        self.max_tool_calls = max_tool_calls
        # Off by default, and deliberately. An agent that is handed its rules
        # has not recalled them, and the Reviewer exists to show that an
        # agent can work from the graph alone -- find_prompt_leaks checks
        # exactly that, and would count an injected rule as having been told.
        # The Planner talks to a person in free-form chat, where a small model
        # will not think to look; the Reviewer gets one fixed trigger and must
        # read everything, including its rules.
        self.standing_rules = standing_rules

    def system_prompt(self) -> str:
        """Role prompt plus the facts only the runtime knows."""
        store = self.toolbox.store
        user = store.session_user(self.toolbox.session_id)
        # Naming the person matters more than it looks: without a referent for
        # "I" the agent invents a subject when recording a personal fact, and
        # has been observed filing the user's own appointment under its own
        # name.
        about_user = (
            f"You are talking to {user!r}. When they say \"I\", \"me\" or "
            f"\"my\", that means {user!r} - use that name as the subject when "
            f"you record something about them.\n" if user else "")
        return (
            f"{self.prompt}\n\n"
            f"You are the agent named {self.name!r}. "
            f"You are working in session {self.toolbox.session_id!r} "
            f"for tenant {store.tenant!r}.\n"
            f"{about_user}"
            f"Today's date is {store.now()[:10]}.\n"
            f"{self._standing_rules()}"
        )

    # A rule the person taught once has to apply every time, including on the
    # turns where the model sees no reason to consult its memory. Leaving that
    # to a tool call does not work: asked to write hello world, a model that
    # had been taught "prefer Java" called no tools at all and answered in
    # Python, because it did not think the question needed remembering.
    #
    # So the rules are read from the graph on every turn and put in front of
    # it. This is still graph-backed memory -- the prompt is rebuilt from a
    # FalkorDB query each time, and retiring a rule removes it from the next
    # turn -- it just does not depend on the model electing to look.
    MAX_STANDING_RULES = 10

    def _standing_rules(self) -> str:
        """Live procedural memory, rendered into the prompt."""
        if not self.standing_rules:
            return ""
        try:
            rules = self.toolbox.store.get_applicable_procedures()
        except Exception:
            return ""                      # never break a turn over this
        if not rules:
            return ""
        lines = "\n".join(
            f"- {r['name']}: {r['action']}"
            + (f" (applies when: {r['trigger']})" if r.get("trigger") else "")
            for r in rules[:self.MAX_STANDING_RULES])
        return (
            "\nStanding rules this person has taught you. They apply whether "
            "or not\nanything in the message mentions them, and they are not "
            "to be restated\nback at the person as news:\n" + lines + "\n")

    def _remember(self, speaker, text):
        return self.toolbox.store.record_event(
            self.toolbox.session_id, self.name, "message", text, speaker=speaker)

    def _close(self, text, steps, messages, stopped_early, record) -> Turn:
        """Write the answer, then point it at the reads that produced it.

        The READ edges are what make "why did you say that" a traversal
        instead of a screenshot: they live in the graph, so the trace
        outlives this process, this page and this session.

        With `record=False` there is no answer node to attach them to, so
        the lookups stay in the graph unlinked, findable by their `session`
        property. That is the honest outcome -- the reads did happen.
        """
        lookups = list(self.toolbox.lookups)
        if record and text:
            answer_id = self._remember(self.name, text)
            if lookups:
                self.toolbox.store.link_reasoning(answer_id, lookups)
        return Turn(text=text, steps=steps, messages=messages,
                    stopped_early=stopped_early, lookups=lookups)

    def run(self, user_message: str, record=True, speaker="user") -> Turn:
        """Answer one message, running tools until the model stops asking.

        Both sides of the exchange are written to the graph, tagged with who
        said them. That is what lets a conversation be read back out of
        memory later -- by this agent in a future session, by the other
        agent, or by a UI rebuilding a transcript.

        `record=False` skips the episodic write, for read-only questions
        where an event would be noise.
        """
        if record:
            self._remember(speaker, user_message)

        # One turn, one trace. The toolbox fills this as the model reads.
        self.toolbox.lookups.clear()
        messages: list[Message] = [Message(role="user", text=user_message)]
        specs = self.toolbox.specs()
        steps: list[Step] = []
        stopped_early = False
        # Smaller models re-ask for things they already have. Caching the
        # answer and saying so is cheaper than re-querying, and the nudge
        # pushes the model to answer instead of circling.
        seen: dict[tuple, str] = {}

        while True:
            response = self.llm.chat(messages, tools=specs,
                                     system=self.system_prompt())

            if not response.wants_tools:
                messages.append(Message(role="assistant", text=response.text,
                                        provider_state=response.raw))
                return self._close(response.text, steps, messages,
                                   stopped_early, record)

            # Carry provider_state so the assistant turn replays byte-identically.
            messages.append(Message(
                role="assistant",
                text=response.text,
                tool_calls=response.tool_calls,
                provider_state=response.raw,
            ))

            remaining = self.max_tool_calls - len(steps)
            if remaining <= 0:
                # Refuse the calls rather than dropping them: every tool_use
                # block must get a tool_result, or the next request is invalid.
                results = [
                    ToolResult(call_id=c.id, is_error=True,
                               content="Error: tool-call budget exhausted for "
                                       "this question. Answer with what you have.")
                    for c in response.tool_calls
                ]
                stopped_early = True
            else:
                results = []
                for call in response.tool_calls:
                    key = (call.name, json.dumps(call.arguments, sort_keys=True,
                                                 default=str))
                    if key in seen:
                        result = ToolResult(
                            call_id=call.id,
                            content=f"You already called {call.name} with these "
                                    f"arguments. The answer has not changed:\n"
                                    f"{seen[key]}\n"
                                    f"Use it and give your answer now.")
                    else:
                        result = self.toolbox.dispatch(call)
                        if not result.is_error:
                            seen[key] = result.content
                    results.append(result)
                    steps.append(Step(name=call.name, arguments=call.arguments,
                                      result=result.content,
                                      is_error=result.is_error))

            # All results for one assistant turn go back in ONE user message.
            messages.append(Message(role="user", tool_results=tuple(results)))

            if stopped_early:
                # Let the model produce a final answer from what it has, then
                # stop regardless of what it asks for next.
                final = self._final_answer(messages)
                messages.append(Message(role="assistant", text=final,
                                        provider_state=None))
                return self._close(final, steps, messages, True, record)

    def _final_answer(self, messages) -> str:
        """Get prose out of the model once the tool budget is spent.

        Asking again *with* tools often just produces another tool call and
        no text, which surfaces as a silent empty reply. Dropping the tools
        leaves the model nothing to do but answer.
        """
        reply = self.llm.chat(messages, tools=self.toolbox.specs(),
                              system=self.system_prompt())
        if reply.text:
            return reply.text
        closing = list(messages) + [Message(
            role="user",
            text="Do not call any more tools. Answer now, in plain prose, "
                 "using only what the tools above returned.")]
        return self.llm.chat(closing, tools=(),
                             system=self.system_prompt()).text
