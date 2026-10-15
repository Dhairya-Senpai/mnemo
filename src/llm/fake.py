"""A scripted adapter so the whole agent loop can be tested for zero tokens.

This is not a mock bolted on for tests -- it is how the memory logic, the
tool dispatch and the loop's control flow get exercised without an API key,
a network call, or a cent of spend. Every behaviour in the agent layer that
does not strictly require a model is tested through this.

It also records what it was asked, so a test can assert on the prompt and the
tool list the agent actually built, not just on the final answer.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from .base import LLMAdapter
from .types import (
    STOP_END_TURN,
    STOP_TOOL_USE,
    LLMResponse,
    Message,
    ToolCall,
    ToolSpec,
)


def says(text: str) -> LLMResponse:
    """Scripted reply: plain text, turn over."""
    return LLMResponse(text=text, stop_reason=STOP_END_TURN)


def calls(name: str, arguments: dict[str, Any] | None = None,
          call_id: str | None = None, text: str = "") -> LLMResponse:
    """Scripted reply: one tool call."""
    return LLMResponse(
        text=text,
        tool_calls=(ToolCall(id=call_id or f"call-{name}", name=name,
                             arguments=arguments or {}),),
        stop_reason=STOP_TOOL_USE,
    )


def calls_many(*specs: tuple[str, dict[str, Any]], text: str = "") -> LLMResponse:
    """Scripted reply: several tool calls in one turn (parallel tool use)."""
    return LLMResponse(
        text=text,
        tool_calls=tuple(
            ToolCall(id=f"call-{i}-{n}", name=n, arguments=a)
            for i, (n, a) in enumerate(specs)
        ),
        stop_reason=STOP_TOOL_USE,
    )


def last_tool_result(messages) -> str | None:
    """The most recent tool result in a conversation, for scripted reactions."""
    for msg in reversed(messages):
        if msg.tool_results:
            return msg.tool_results[-1].content
    return None


@dataclass
class Exchange:
    """What the adapter was asked on one call."""
    messages: tuple[Message, ...]
    tools: tuple[str, ...]
    system: str | None


class FakeAdapter(LLMAdapter):
    """Replays a fixed list of responses, one per `chat` call.

    Raising on an exhausted script is deliberate: a loop that calls the model
    more times than the test expected is a bug worth failing on, not a
    condition to paper over with a default reply.
    """

    name = "fake"

    def __init__(self, script: Sequence[LLMResponse], model: str = "fake-1"):
        self.model = model
        self._script = list(script)
        self._next = 0
        self.exchanges: list[Exchange] = []

    @property
    def call_count(self) -> int:
        return self._next

    @property
    def exhausted(self) -> bool:
        return self._next >= len(self._script)

    def chat(self, messages, tools=(), system=None) -> LLMResponse:
        self.exchanges.append(Exchange(
            messages=tuple(messages),
            tools=tuple(t.name for t in tools),
            system=system,
        ))
        if self._next >= len(self._script):
            raise AssertionError(
                f"FakeAdapter script exhausted after {self._next} call(s); "
                f"the agent asked for another reply"
            )
        entry = self._script[self._next]
        self._next += 1
        # A script entry may be a callable, so a scripted agent can react to
        # what the tools returned. Needed whenever the next call depends on an
        # id the test could not know in advance -- claiming a task, then
        # reading the task it just claimed.
        if callable(entry):
            return entry(tuple(messages), tuple(tools), system)
        return entry

    # Convenience for assertions.
    def last_user_text(self) -> str:
        for msg in reversed(self.exchanges[-1].messages):
            if msg.role == "user" and msg.text:
                return msg.text
        return ""

    def tool_results_seen(self) -> list[str]:
        """Every tool result the agent fed back, in order."""
        out = []
        for ex in self.exchanges:
            for msg in ex.messages:
                for r in msg.tool_results:
                    out.append(r.content)
        return out
