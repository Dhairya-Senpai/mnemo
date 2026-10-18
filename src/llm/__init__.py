"""Swappable LLM layer.

Changing model or provider is a config change:

    LLM_PROVIDER=ollama  LLM_MODEL=ornith:9b     # default; local, free
    LLM_PROVIDER=ollama  LLM_MODEL=llama3.2:3b   # smaller, faster, weaker

Adding a provider means writing one adapter and adding one line to
`_PROVIDERS`. No agent code changes -- nothing above this package names a
provider, which is the whole point of the layer.

Two other adapters lived here and were both removed on 18 October: a hosted
(Anthropic) one, because no request was ever sent through it, and a scripted
one used by the tests and by a demo replayer. Writing them is what forced
`base.py` to be genuinely provider-neutral; that boundary is what remains,
and it is what a new adapter owes.
"""

import os

from .base import LLMAdapter, LLMError
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


_PROVIDERS = {
    "ollama": _make_ollama,
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
    "ToolResult", "ToolSpec", "get_adapter",
    "STOP_END_TURN", "STOP_TOOL_USE", "STOP_MAX_TOKENS", "STOP_REFUSAL",
]
