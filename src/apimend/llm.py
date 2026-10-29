"""Optional: ask Claude to propose a source-code fix for a drifted integration.

Needs ``ANTHROPIC_API_KEY``. Set ``APIMEND_MODEL`` to pick another model.
Nothing is applied automatically; the proposal is printed or saved for review.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

API_URL = os.environ.get("APIMEND_API_URL", "https://api.anthropic.com/v1/messages")
DEFAULT_MODEL = "claude-sonnet-5-5"

SYSTEM = (
    "You maintain API integrations. An upstream API changed shape. Given the drift "
    "report, the repair plan, and the consumer's source file, propose the smallest "
    "code change that makes the consumer work with the NEW API directly. Reply with "
    "a short explanation followed by a unified diff in a ```diff block. Do not invent "
    "fields that are not in the report. If the change cannot be made safely, say what "
    "a human must decide instead."
)


def build_prompt(changes: list[dict], plan: dict | None, source_name: str, source: str) -> str:
    parts = ["## Drift report", "```json", json.dumps(changes, indent=2), "```"]
    if plan:
        parts += ["## Repair plan (old shape restored at runtime)", "```json", json.dumps(plan, indent=2), "```"]
    parts += [f"## Consumer source: {source_name}", "```", source, "```"]
    return "\n".join(parts)


def propose_fix(prompt: str, *, model: str | None = None, max_tokens: int = 4000, timeout: float = 120) -> str:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("set ANTHROPIC_API_KEY to use `apimend propose`")
    body = json.dumps({
        "model": model or os.environ.get("APIMEND_MODEL", DEFAULT_MODEL),
        "max_tokens": max_tokens,
        "system": SYSTEM,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(API_URL, data=body, method="POST", headers={
        "content-type": "application/json",
        "x-api-key": key,
        "anthropic-version": "2023-06-01",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:500]
        raise RuntimeError(f"Claude API returned {e.code}: {detail}") from e
    return "".join(block.get("text", "") for block in data.get("content", []) if block.get("type") == "text")
