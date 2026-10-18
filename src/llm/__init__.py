"""Swappable LLM layer.

Changing model or provider is a config change:

    LLM_PROVIDER=ollama  LLM_MODEL=ornith:9b     # default; local, free
    LLM_PROVIDER=ollama  LLM_MODEL=llama3.2:3b   # smaller, faster, weaker
    LLM_PROVIDER=fake                            # scripted, zero tokens

Adding a provider means writing one adapter and adding one line to
`_PROVIDERS`. No agent code changes -- nothing above this package names a
provider, which is the whole point of the layer.

A hosted adapter (Anthropic, OpenAI) lived here until it was removed: it was
dead weight, because no request was ever sent through it. The interface it
proved is still the interface, and `base.py` records what a new one owes.
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


def _make_ollama(**kwargs):
    # Imported here so `import llm` works without a running Ollama.
    from .ollama import OllamaAdapter
    return OllamaAdapter(**kwargs)


def _make_fake(**kwargs):
    return FakeAdapter(script=kwargs.pop("script", []), **kwargs)


_PROVIDERS = {
    "ollama": _make_ollama,
    "fake": _make_fake,
}


def get_adapter(provider: str | None = None, **kwargs) -> LLMAdapter:
    """Build the configured adapter. Provider comes from LLM_PROVIDER."""
    name = (provider or os.environ.get("LLM_PROVIDER", "ollama")).lower()
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
