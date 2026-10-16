"""Ollama adapter: the same interface, a model running on your own machine.

This file exists to make one claim testable rather than asserted. The project
says swapping providers is a config change; this is the second provider, and
nothing outside this file changed to add it. No agent, tool, prompt or test
knows it exists.

Four things differ from the Anthropic wire format, and each one is handled
here so the rest of the project never sees them:

* **No tool-call ids.** Ollama returns a tool call as a name plus arguments,
  and expects the result back keyed by `tool_name`. Our neutral types use an
  id, so ids are synthesized on the way out and resolved back to names on the
  way in, by looking at the assistant turn that requested them.
* **Tool results are their own role.** Anthropic nests `tool_result` blocks
  inside a user message; Ollama uses `role: "tool"`, one message per result.
* **The system prompt is just the first message**, not a separate field.
* **Context length must be set explicitly.** Ollama defaults to a small
  `num_ctx`, and sixteen tool schemas plus a system prompt will quietly
  overflow it - the model starts forgetting the earliest tool results with no
  error at all. DEFAULT_CTX below is deliberately generous.

Requires a running Ollama (`ollama serve`, usually automatic) and a pulled
model with tool support.
"""

import json
import os
import urllib.error
import urllib.request
from collections.abc import Sequence

from .base import LLMAdapter, LLMError
from .types import (
    STOP_END_TURN,
    STOP_MAX_TOKENS,
    STOP_TOOL_USE,
    LLMResponse,
    Message,
    ToolCall,
    ToolSpec,
)

DEFAULT_HOST = "http://localhost:11434"
DEFAULT_MODEL = "ornith:9b"
# Big enough for the full tool catalogue, a system prompt and a multi-turn
# loop. Silent truncation is the worst failure mode here, so this errs high.
DEFAULT_CTX = 16384
DEFAULT_TIMEOUT = 300


def to_api_tools(tools: Sequence[ToolSpec]) -> list[dict]:
    """Our tool specs -> Ollama's OpenAI-shaped function definitions."""
    out = []
    for t in tools:
        schema = dict(t.parameters)
        schema.setdefault("type", "object")
        schema.setdefault("properties", {})
        schema.setdefault("required", sorted(schema["properties"].keys()))
        out.append({
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": schema,
            },
        })
    return out


def _call_names(messages: Sequence[Message]) -> dict[str, str]:
    """Map every tool-call id seen so far to the tool it called.

    Needed because Ollama keys results by name while our types key them by
    id. Built from the conversation itself rather than kept as adapter state,
    so the adapter stays stateless and a replayed conversation still resolves.
    """
    return {
        call.id: call.name
        for msg in messages
        for call in msg.tool_calls
    }


def to_api_messages(messages: Sequence[Message], system: str | None = None) -> list[dict]:
    """Our messages -> Ollama messages, with results flattened to role 'tool'."""
    names = _call_names(messages)
    out = []
    if system:
        out.append({"role": "system", "content": system})

    for msg in messages:
        if msg.role == "assistant":
            if msg.provider_state is not None:
                out.append(msg.provider_state)      # replay verbatim
                continue
            entry = {"role": "assistant", "content": msg.text or ""}
            if msg.tool_calls:
                entry["tool_calls"] = [
                    {"function": {"name": c.name, "arguments": c.arguments}}
                    for c in msg.tool_calls
                ]
            out.append(entry)
            continue

        if msg.tool_results:
            # One message per result, each naming its tool. An unknown id
            # would silently drop the result, so fall back to a name that
            # will at least be visible if it ever happens.
            for result in msg.tool_results:
                out.append({
                    "role": "tool",
                    "content": result.content,
                    "tool_name": names.get(result.call_id, "unknown_tool"),
                })
            if msg.text:
                out.append({"role": "user", "content": msg.text})
        else:
            out.append({"role": "user", "content": msg.text})
    return out


def from_api_response(payload: dict) -> LLMResponse:
    """Ollama response -> our normalised reply."""
    message = payload.get("message") or {}
    tool_calls = []
    for index, raw in enumerate(message.get("tool_calls") or []):
        function = raw.get("function") or {}
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            # Documented as an object, but models vary. Never string-match it.
            try:
                arguments = json.loads(arguments)
            except ValueError:
                arguments = {}
        name = function.get("name", "")
        tool_calls.append(ToolCall(
            id=f"ollama-{index}-{name}",       # synthesized; Ollama sends none
            name=name,
            arguments=dict(arguments or {}),
        ))

    if tool_calls:
        stop = STOP_TOOL_USE
    elif payload.get("done_reason") == "length":
        stop = STOP_MAX_TOKENS
    else:
        stop = STOP_END_TURN

    usage = {
        key: value
        for key, value in (
            ("input_tokens", payload.get("prompt_eval_count")),
            ("output_tokens", payload.get("eval_count")),
        )
        if isinstance(value, int)
    }

    return LLMResponse(
        text=(message.get("content") or "").strip(),
        tool_calls=tuple(tool_calls),
        stop_reason=stop,
        raw=message,                 # replayed verbatim on the next turn
        usage=usage or None,
    )


class OllamaAdapter(LLMAdapter):
    """One request, one reply, against a local Ollama server."""

    name = "ollama"

    def __init__(self, model=None, host=None, num_ctx=DEFAULT_CTX,
                 timeout=DEFAULT_TIMEOUT, think=False):
        self.model = model or os.environ.get("LLM_MODEL", DEFAULT_MODEL)
        self.host = (host or os.environ.get("OLLAMA_HOST", DEFAULT_HOST)).rstrip("/")
        self.num_ctx = num_ctx
        self.timeout = timeout
        # Some models expose a thinking mode. It costs tokens and latency and
        # we do not read the thinking text, so it is off unless asked for.
        self.think = think

    def chat(self, messages, tools=(), system=None) -> LLMResponse:
        request = {
            "model": self.model,
            "messages": to_api_messages(messages, system=system),
            "stream": False,
            "options": {"num_ctx": self.num_ctx, "temperature": 0},
        }
        if tools:
            request["tools"] = to_api_tools(tools)
        if self.think:
            request["think"] = True

        data = json.dumps(request).encode("utf-8")
        req = urllib.request.Request(
            f"{self.host}/api/chat", data=data,
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            raise LLMError(f"ollama returned {e.code}: {detail}") from e
        except urllib.error.URLError as e:
            raise LLMError(
                f"could not reach ollama at {self.host}: {e.reason}. "
                f"Is it running?") from e

        if "error" in payload:
            raise LLMError(f"ollama error: {payload['error']}")

        result = from_api_response(payload)
        if result.stop_reason == STOP_MAX_TOKENS:
            raise LLMError(
                f"reply hit the context limit (num_ctx={self.num_ctx}) and is "
                f"truncated; raise num_ctx or expose fewer tools")
        return result
