"""Claude adapter: the only file in the project that knows Anthropic exists.

Translation lives in three pure functions (`to_api_messages`, `to_api_tools`,
`from_api_response`) that take and return plain data. They are unit-tested
without a network call or an API key; only `ClaudeAdapter.chat` touches the
wire.

Model behaviour this file depends on (Claude Sonnet 5.5 / Opus 5.5 family):

* **Thinking is on and cannot be disabled.** `thinking: {"type": "disabled"}`
  returns a 400, so the parameter is omitted entirely and the model runs
  adaptive thinking. Reasoning blocks come back with empty text by default.
* **Forced tool choice is gone.** `tool_choice: {"type": "any"}` and
  `{"type": "tool", ...}` both return a 400. We rely on the default `auto`
  and steer from the prompt, keeping argument validity via `strict: true`.
* **Reasoning blocks are bound to the conversation.** The assistant turn is
  replayed from `Message.provider_state` -- the original content blocks --
  rather than rebuilt from text, so replay is byte-identical.
* **A refusal is an HTTP 200.** `stop_reason == "refusal"` must be checked
  before reading content, or a declined request looks like an empty answer.
"""

import os
from collections.abc import Sequence

from .base import LLMAdapter, LLMError
from .types import (
    STOP_END_TURN,
    STOP_MAX_TOKENS,
    STOP_REFUSAL,
    STOP_TOOL_USE,
    LLMResponse,
    Message,
    ToolCall,
    ToolSpec,
)

# PROJECT_CONTEXT.md section 9 names Sonnet for the agent loop and Haiku for
# bulk summarisation. Both are overridable by environment variable.
DEFAULT_MODEL = "claude-sonnet-5-5"
DEFAULT_MAX_TOKENS = 16000

# Vendor stop reasons -> our normalised set. Anything not listed passes
# through unchanged, so an unknown reason can never be mistaken for a clean
# finish.
_STOP_MAP = {
    "end_turn": STOP_END_TURN,
    "tool_use": STOP_TOOL_USE,
    "max_tokens": STOP_MAX_TOKENS,
    "refusal": STOP_REFUSAL,
}


# --------------------------------------------------------------- translation

def to_api_tools(tools: Sequence[ToolSpec]) -> list[dict]:
    """Our tool specs -> Anthropic tool definitions.

    `strict` is set because forced tool choice no longer exists on these
    models: we cannot make the model call a tool, but we can guarantee the
    arguments validate when it does. Strict mode requires the schema to set
    `additionalProperties: false` and list `required`, which ToolSpec documents.
    """
    out = []
    for t in tools:
        schema = dict(t.parameters)
        schema.setdefault("type", "object")
        schema.setdefault("properties", {})
        schema.setdefault("required", sorted(schema["properties"].keys()))
        schema.setdefault("additionalProperties", False)
        out.append({
            "name": t.name,
            "description": t.description,
            "input_schema": schema,
            "strict": True,
        })
    return out


def to_api_messages(messages: Sequence[Message]) -> list[dict]:
    """Our messages -> Anthropic messages.

    An assistant turn is replayed from `provider_state` when the adapter
    stored it, so reasoning blocks survive the round trip untouched.
    """
    out = []
    for msg in messages:
        if msg.role == "assistant":
            if msg.provider_state is not None:
                out.append({"role": "assistant", "content": msg.provider_state})
                continue
            content = []
            if msg.text:
                content.append({"type": "text", "text": msg.text})
            for call in msg.tool_calls:
                content.append({
                    "type": "tool_use",
                    "id": call.id,
                    "name": call.name,
                    "input": call.arguments,
                })
            out.append({"role": "assistant", "content": content or [{"type": "text", "text": ""}]})
            continue

        # User turn: either plain text, or a batch of tool results.
        if msg.tool_results:
            # All results for one assistant turn go back in ONE user message.
            # Splitting them across messages teaches the model to stop making
            # parallel tool calls.
            content = [
                {
                    "type": "tool_result",
                    "tool_use_id": r.call_id,
                    "content": r.content,
                    **({"is_error": True} if r.is_error else {}),
                }
                for r in msg.tool_results
            ]
            if msg.text:
                content.append({"type": "text", "text": msg.text})
            out.append({"role": "user", "content": content})
        else:
            out.append({"role": "user", "content": msg.text})
    return out


def from_api_response(response) -> LLMResponse:
    """Anthropic response -> our normalised reply.

    Only `text` blocks contribute to the text. Reasoning blocks are skipped:
    their text is empty by default, and they are carried forward through
    `provider_state`, not through our text field.
    """
    text_parts, tool_calls = [], []
    for block in response.content:
        btype = getattr(block, "type", None)
        if btype == "text":
            text_parts.append(block.text)
        elif btype == "tool_use":
            # block.input is already parsed by the SDK. Never string-match the
            # serialised form: escaping varies between models.
            tool_calls.append(ToolCall(id=block.id, name=block.name,
                                       arguments=dict(block.input or {})))

    raw_stop = getattr(response, "stop_reason", None)

    # Token counts, when reported. Collected so spend can be shown rather
    # than guessed at; the field names vary by provider, so they are
    # normalised here instead of leaking upward.
    usage = None
    raw_usage = getattr(response, "usage", None)
    if raw_usage is not None:
        usage = {
            key: value
            for key, value in (
                ("input_tokens", getattr(raw_usage, "input_tokens", None)),
                ("output_tokens", getattr(raw_usage, "output_tokens", None)),
                ("cache_read_input_tokens",
                 getattr(raw_usage, "cache_read_input_tokens", None)),
                ("cache_creation_input_tokens",
                 getattr(raw_usage, "cache_creation_input_tokens", None)),
            )
            if isinstance(value, int)
        }

    return LLMResponse(
        text="\n".join(p for p in text_parts if p).strip(),
        tool_calls=tuple(tool_calls),
        stop_reason=_STOP_MAP.get(raw_stop, raw_stop or STOP_END_TURN),
        raw=response.content,
        usage=usage or None,
    )


# ------------------------------------------------------------------- adapter

class ClaudeAdapter(LLMAdapter):
    """One request, one reply, against the Anthropic Messages API."""

    name = "claude"

    def __init__(self, model=None, max_tokens=DEFAULT_MAX_TOKENS,
                 api_key=None, client=None, refusal_fallback=True):
        self.model = model or os.environ.get("LLM_MODEL", DEFAULT_MODEL)
        self.max_tokens = max_tokens
        self.refusal_fallback = refusal_fallback
        if client is not None:
            self._client = client
        else:
            # Imported lazily so the package works -- and the fake adapter
            # runs -- on a machine with no SDK and no API key.
            import anthropic
            self._client = anthropic.Anthropic(api_key=api_key) if api_key \
                else anthropic.Anthropic()

    def chat(self, messages, tools=(), system=None) -> LLMResponse:
        request = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": to_api_messages(messages),
        }
        if system:
            request["system"] = system
        if tools:
            request["tools"] = to_api_tools(tools)
        if self.refusal_fallback:
            # Safety classifiers can decline with HTTP 200. Server-side
            # fallback re-routes a refused request to another model by
            # category, so a false positive does not break the demo.
            request["betas"] = ["server-side-fallback-2026-07-01"]
            request["fallbacks"] = "default"

        response = self._client.beta.messages.create(**request)
        result = from_api_response(response)

        if result.stop_reason == STOP_REFUSAL:
            details = getattr(response, "stop_details", None)
            raise LLMError(f"model declined the request: {details}")
        if result.stop_reason == STOP_MAX_TOKENS:
            raise LLMError(
                f"reply hit the {self.max_tokens}-token ceiling and is truncated; "
                f"raise max_tokens or shorten the tool results"
            )
        return result
