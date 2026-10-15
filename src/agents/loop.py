"""The agent loop: ask the model, run the tools it asks for, repeat.

The loop is ours rather than the SDK's tool runner, on purpose. The runner
would drive this cycle for us, but the cycle would then live inside a vendor
helper and swapping providers would mean rewriting the agent. Here the loop
talks only to `LLMAdapter`, so a different provider is a config change.

Every turn is also recorded in the graph. An agent that thinks and acts
without leaving an episodic trace would break the central claim of the
project, so `Agent.run` writes the user's message as an Event before calling
the model, and the tools write their own events as they go.
"""

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

    @property
    def tools_used(self) -> list[str]:
        return [s.name for s in self.steps]


class Agent:
    """One named agent with a role prompt, an adapter, and a toolbox."""

    def __init__(self, name, toolbox, llm: LLMAdapter, prompt=None,
                 prompt_name=None, max_tool_calls=DEFAULT_MAX_TOOL_CALLS):
        if prompt is None and prompt_name is None:
            raise ValueError("pass prompt or prompt_name")
        self.name = name
        self.toolbox = toolbox
        self.llm = llm
        self.prompt = prompt if prompt is not None else load_prompt(prompt_name)
        self.max_tool_calls = max_tool_calls

    def system_prompt(self) -> str:
        """Role prompt plus the facts only the runtime knows."""
        return (
            f"{self.prompt}\n\n"
            f"You are the agent named {self.name!r}. "
            f"You are working in session {self.toolbox.session_id!r} "
            f"for tenant {self.toolbox.store.tenant!r}.\n"
            f"Today's date is {self.toolbox.store.now()[:10]}."
        )

    def run(self, user_message: str, record=True) -> Turn:
        """Answer one message, running tools until the model stops asking.

        `record=False` skips the episodic write, for read-only questions
        where an event would be noise.
        """
        if record:
            self.toolbox.store.record_event(
                self.toolbox.session_id, self.name, "message", user_message
            )

        messages: list[Message] = [Message(role="user", text=user_message)]
        specs = self.toolbox.specs()
        steps: list[Step] = []
        stopped_early = False

        while True:
            response = self.llm.chat(messages, tools=specs,
                                     system=self.system_prompt())

            if not response.wants_tools:
                messages.append(Message(role="assistant", text=response.text,
                                        provider_state=response.raw))
                return Turn(text=response.text, steps=steps, messages=messages,
                            stopped_early=stopped_early)

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
                    result = self.toolbox.dispatch(call)
                    results.append(result)
                    steps.append(Step(name=call.name, arguments=call.arguments,
                                      result=result.content,
                                      is_error=result.is_error))

            # All results for one assistant turn go back in ONE user message.
            messages.append(Message(role="user", tool_results=tuple(results)))

            if stopped_early:
                # Let the model produce a final answer from what it has, then
                # stop regardless of what it asks for next.
                final = self.llm.chat(messages, tools=specs,
                                      system=self.system_prompt())
                messages.append(Message(role="assistant", text=final.text,
                                        provider_state=final.raw))
                return Turn(text=final.text, steps=steps, messages=messages,
                            stopped_early=True)
