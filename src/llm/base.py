"""The one interface every agent talks to.

Agents call `adapter.chat(...)` and nothing else. No agent imports a provider
SDK, and no provider type crosses this boundary.

Deliberately NOT used: the Anthropic SDK's tool runner. It drives the whole
agentic loop for you, which is genuinely convenient, but the loop would then
live inside a vendor helper -- swapping providers would mean rewriting the
agent, not changing a config value. We own the loop (src/agents/loop.py) and
keep each adapter down to a single request/response translation.
"""

from abc import ABC, abstractmethod
from collections.abc import Sequence

from .types import LLMResponse, Message, ToolSpec


class LLMAdapter(ABC):
    """One request, one reply. No looping, no state."""

    name: str = "abstract"
    model: str = "unset"

    @abstractmethod
    def chat(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] = (),
        system: str | None = None,
    ) -> LLMResponse:
        """Send a conversation and get one normalised reply back.

        Implementations must:
          * translate `messages`, `tools` and `system` to the vendor format
          * translate the reply back into an LLMResponse
          * map the vendor's stop reason onto the STOP_* constants
          * never loop, never execute a tool, never mutate `messages`
        """
        raise NotImplementedError


class LLMError(RuntimeError):
    """Raised when a provider refuses, truncates, or otherwise cannot answer."""
