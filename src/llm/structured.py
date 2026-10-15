"""JSON answers from a model, through the same neutral interface.

Consolidation needs the model to return a structured value, not prose. Doing
that provider-neutrally means asking for JSON in the prompt and validating
what comes back, rather than reaching for one vendor's structured-output
parameter -- that parameter is real and better, but it lives on the
provider's request shape and would not survive a swap. The adapter is free to
use it internally later; callers here see the same `chat` interface.

Three things this adds over a bare call:

* **Validation.** A missing key is caught here, not three frames later.
* **One retry**, with the parse error fed back, because a near-miss is the
  common failure and a second attempt usually fixes it.
* **A cache**, so re-running the demo does not re-spend on the same summary.
"""

import hashlib
import json
from pathlib import Path

from .base import LLMError
from .types import Message

_FENCE = "```"


class JsonCache:
    """Content-addressed cache of model answers, optionally file-backed.

    Keyed on model + prompt, so changing either produces a miss. Rehearsing
    the demo is the main beneficiary: the same consolidation is paid for once.
    """

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else None
        self._data: dict[str, dict] = {}
        if self.path and self.path.exists():
            try:
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self._data = {}          # a corrupt cache is not worth failing on

    @staticmethod
    def key(model: str, prompt: str) -> str:
        return hashlib.sha256(f"{model}\n{prompt}".encode("utf-8")).hexdigest()[:32]

    def get(self, model, prompt):
        return self._data.get(self.key(model, prompt))

    def put(self, model, prompt, value):
        self._data[self.key(model, prompt)] = value
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self._data, indent=2), encoding="utf-8")

    def __len__(self):
        return len(self._data)


def extract_json(text: str) -> dict:
    """Parse a JSON object out of a reply, tolerating a code fence.

    Models wrap JSON in ```json fences often enough that refusing to handle
    it would mean burning a retry on a correct answer.
    """
    body = text.strip()
    if body.startswith(_FENCE):
        lines = body.split("\n")
        lines = lines[1:]                               # drop ```json
        if lines and lines[-1].strip().startswith(_FENCE):
            lines = lines[:-1]
        body = "\n".join(lines).strip()
    if not body.startswith("{"):
        start, end = body.find("{"), body.rfind("}")
        if start == -1 or end <= start:
            raise ValueError("no JSON object found in the reply")
        body = body[start:end + 1]
    value = json.loads(body)
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object, got {type(value).__name__}")
    return value


def ask_json(adapter, prompt, required_keys=(), system=None, cache=None, retries=1):
    """Ask for a JSON object, validate it, retry once, cache the result."""
    if cache is not None:
        hit = cache.get(adapter.model, prompt)
        if hit is not None:
            return hit

    messages = [Message(role="user", text=prompt)]
    last_error = None

    for attempt in range(retries + 1):
        response = adapter.chat(messages, system=system)
        try:
            value = extract_json(response.text)
            missing = [k for k in required_keys if k not in value]
            if missing:
                raise ValueError(f"missing required key(s): {missing}")
        except ValueError as e:
            last_error = e
            if attempt == retries:
                break
            # Feed the failure back; a near-miss usually self-corrects.
            messages = messages + [
                Message(role="assistant", text=response.text,
                        provider_state=response.raw),
                Message(role="user",
                        text=f"That was not valid: {e}. Reply with a single JSON "
                             f"object and nothing else."),
            ]
            continue

        if cache is not None:
            cache.put(adapter.model, prompt, value)
        return value

    raise LLMError(f"could not get valid JSON after {retries + 1} attempt(s): {last_error}")
