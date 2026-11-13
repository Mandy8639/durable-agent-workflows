"""Model calls for the demo: OpenRouter (free models) or a deterministic fake.

DAW_LLM=openrouter (default) needs OPENROUTER_API_KEY; the model is DAW_MODEL
(default below). DAW_LLM=fake needs nothing and always returns the same text
for the same input.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request

from daw.context import StepError

DEFAULT_MODEL = "nvidia/nemotron-3.5-lightning:free"
ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
ENV = ("OPENROUTER_API_KEY", "DAW_LLM", "DAW_MODEL")


def complete(task: str, system: str, user: str, *, max_tokens: int = 8000) -> dict:
    mode = os.environ.get("DAW_LLM", "openrouter")
    if mode == "fake":
        return {"text": FAKES[task](user), "model": "fake", "usage": {}, "cost": 0.0}
    if mode != "openrouter":
        raise StepError(f"unknown DAW_LLM={mode!r} (use openrouter or fake)")
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise StepError("OPENROUTER_API_KEY is not set (or run with DAW_LLM=fake)")
    model = os.environ.get("DAW_MODEL", DEFAULT_MODEL)
    if not model.endswith(":free"):
        raise StepError(f"refusing non-free model {model!r}: this demo has a $0 cost ceiling")
    body = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "max_tokens": max_tokens,
        "temperature": 0.2,
        "usage": {"include": True},
        # Reasoning models otherwise spend the whole token budget thinking and return no content.
        "reasoning": {"effort": "low", "exclude": True},
    }
    req = urllib.request.Request(
        ENDPOINT,
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "X-Title": "durable-agent-workflows",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=100) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise StepError(f"OpenRouter HTTP {e.code}: {e.read()[:300]!r}") from None
    except (urllib.error.URLError, TimeoutError) as e:
        raise StepError(f"OpenRouter request failed: {e}") from None
    if "error" in data:
        raise StepError(f"OpenRouter error: {data['error']}")
    choice = data["choices"][0]
    text = (choice["message"].get("content") or "").strip()
    if not text:
        raise StepError(f"model returned empty content (finish_reason={choice.get('finish_reason')!r}, "
                        f"model={data.get('model', model)!r})")
    usage = data.get("usage") or {}
    return {"text": text, "model": data.get("model", model), "usage": usage,
            "cost": float(usage.get("cost") or 0.0)}


# --- deterministic fake model ---------------------------------------------

def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if len(s.split()) >= 8]


def _fake_draft(prompt: str) -> str:
    topic = prompt.split("\n", 1)[0].removeprefix("Topic: ").strip()
    source = prompt.split("SOURCE:", 1)[-1]
    picks = _sentences(source)[:6]
    bullets = "\n".join(f"- {s}" for s in picks[1:5])
    return (f"## Summary\n\n{picks[0] if picks else topic}\n\n## Key points\n\n{bullets}\n\n"
            f"## Open questions\n\n- What does {topic} cost in practice, and when is it the wrong choice?")


def _fake_critique(prompt: str) -> str:
    draft = prompt.split("DRAFT:", 1)[-1].split("SOURCE:", 1)[0]
    words = len(draft.split())
    return (f"- Length: {words} words.\n"
            "- Every key point is a sentence copied from the source, so it is supported but not synthesised.\n"
            "- The brief does not compare alternatives or quantify trade-offs.")


FAKES = {"draft": _fake_draft, "critique": _fake_critique}
