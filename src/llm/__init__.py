"""Swappable LLM layer.

Changing model or provider is a config change:

    LLM_PROVIDER=claude  LLM_MODEL=claude-sonnet-5-5   # default
    LLM_PROVIDER=claude  LLM_MODEL=claude-haiku-5-5    # cheaper, for bulk work
    LLM_PROVIDER=fake                                  # tests, zero tokens

Adding a provider means writing one adapter and adding one line to
`_PROVIDERS`. No agent code changes.
"""

import os

from .base import LLMAdapter, LLMError
from .fake import FakeAdapter, calls, calls_many, says
from .types import (
    STOP_END_TURN,
    STOP_MAX_TOKENS,
    STOP_REFUSAL,
    STOP_TOOL_USE,
    LLMResponse,
    Message,
    ToolCall,
    ToolResult,
    ToolSpec,
)


def _make_claude(**kwargs):
    # Imported here so `import llm` works with no SDK installed.
    from .claude import ClaudeAdapter
    return ClaudeAdapter(**kwargs)


def _make_fake(**kwargs):
    return FakeAdapter(script=kwargs.pop("script", []), **kwargs)


_PROVIDERS = {
    "claude": _make_claude,
    "fake": _make_fake,
}


def get_adapter(provider: str | None = None, **kwargs) -> LLMAdapter:
    """Build the configured adapter. Provider comes from LLM_PROVIDER."""
    name = (provider or os.environ.get("LLM_PROVIDER", "claude")).lower()
    if name not in _PROVIDERS:
        raise ValueError(
            f"unknown LLM provider {name!r}; known: {sorted(_PROVIDERS)}"
        )
    return _PROVIDERS[name](**kwargs)


__all__ = [
    "LLMAdapter", "LLMError", "LLMResponse", "Message", "ToolCall",
    "ToolResult", "ToolSpec", "FakeAdapter", "get_adapter",
    "says", "calls", "calls_many",
    "STOP_END_TURN", "STOP_TOOL_USE", "STOP_MAX_TOKENS", "STOP_REFUSAL",
]
