"""Provider-neutral message and tool types.

Nothing in this file names a provider. Agents are written against these types
only; each adapter translates them to and from one vendor's wire format. That
is what makes swapping models a config change rather than a rewrite.

Shapes, kept deliberately small (PROJECT_CONTEXT.md section 9):
  message    = role + text (+ tool calls / tool results)
  tool       = name + description + parameters
  tool call  = id + name + arguments
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ToolCall:
    """A model's request to run one tool.

    `id` correlates the call with its result. Every provider has some notion
    of this; adapters map their own identifier onto it.
    """
    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolResult:
    """The outcome of running one tool, fed back to the model."""
    call_id: str
    content: str
    is_error: bool = False


@dataclass(frozen=True)
class Message:
    """One turn. A turn may carry text, tool calls, or tool results.

    An assistant turn that called tools has `tool_calls` set; the user turn
    that answers it has `tool_results` set. Keeping both on one type avoids a
    provider-shaped union, at the cost of two fields usually being empty.
    """
    role: str                       # "user" | "assistant"
    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    tool_results: tuple[ToolResult, ...] = ()

    # Opaque, adapter-owned, never read by agents. An adapter may stash the
    # provider's original turn here so it can be replayed byte-identically.
    # This exists for a concrete reason: some models bind reasoning blocks to
    # the conversation that produced them, and rebuilding an assistant turn
    # from `text` + `tool_calls` alone would silently drop those blocks and
    # can be rejected as an edit to history. Other adapters ignore the field,
    # so neutrality is preserved.
    provider_state: Any = None

    def __post_init__(self):
        if self.role not in ("user", "assistant"):
            raise ValueError(f"role must be 'user' or 'assistant', got {self.role!r}")


@dataclass(frozen=True)
class ToolSpec:
    """A tool offered to the model.

    `parameters` is a JSON Schema object. It must set
    `additionalProperties: false` and list `required`, because adapters may
    pass it to providers in strict mode, where a loose schema is rejected.
    """
    name: str
    description: str
    parameters: dict[str, Any]


# Stop reasons, normalised across providers. Adapters map vendor values onto
# these; an unrecognised vendor value passes through unchanged so a provider
# can never silently look like a clean finish.
STOP_END_TURN = "end_turn"      # model finished speaking
STOP_TOOL_USE = "tool_use"      # model wants tools run
STOP_MAX_TOKENS = "max_tokens"  # output ceiling hit; the reply is truncated
STOP_REFUSAL = "refusal"        # provider safety system declined


@dataclass(frozen=True)
class LLMResponse:
    """One model reply, normalised."""
    text: str
    tool_calls: tuple[ToolCall, ...] = ()
    stop_reason: str = STOP_END_TURN
    raw: Any = None                      # vendor object, for debugging only
    # Token counts, when the provider reports them: input, output, and any
    # cache figures it exposes. Kept provider-neutral (a plain dict) because
    # every provider names these differently. None when unknown -- the fake
    # adapter never sets it, so "we spent nothing" is distinguishable from
    # "we did not look".
    usage: dict[str, int] | None = None

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)
