"""Minimal OpenRouter chat-completions client (stdlib only). Never log the API key."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

DEFAULT_MODEL = "nex-agi/nex-n2.5-mini"
DEFAULT_BASE = "https://openrouter.ai/api/v1"
DEFAULT_MAX_TOKENS = 512


class OpenRouterError(RuntimeError):
    pass


def get_api_key() -> str:
    key = (os.environ.get("OPENROUTER_API_KEY") or "").strip()
    if not key:
        raise OpenRouterError(
            "OPENROUTER_API_KEY is not set; required for TICKETBENCH_SUT_MODE=llm"
        )
    return key


def _message_text(message: dict[str, Any]) -> str:
    """Some models put usable text in reasoning when content is null."""
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
            elif isinstance(block, str):
                parts.append(block)
        joined = "".join(parts).strip()
        if joined:
            return joined
    for key in ("reasoning", "reasoning_content"):
        val = message.get(key)
        if isinstance(val, str) and val.strip():
            return val
    raise OpenRouterError(
        "OpenRouter message content is empty/null "
        "(and no reasoning text); raise max_tokens or check model"
    )


def chat_completion(
    messages: list[dict[str, str]],
    *,
    model: str | None = None,
    temperature: float = 0.0,
    max_tokens: int | None = None,
) -> str:
    key = get_api_key()
    model = model or os.environ.get("OPENROUTER_MODEL") or DEFAULT_MODEL
    base = (os.environ.get("OPENROUTER_BASE_URL") or DEFAULT_BASE).rstrip("/")
    if max_tokens is None:
        max_tokens = int(os.environ.get("OPENROUTER_MAX_TOKENS") or DEFAULT_MAX_TOKENS)
    body = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    req = urllib.request.Request(
        f"{base}/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/ya-yu-zhang/ticketbench",
            "X-Title": "ticketbench",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:500]
        raise OpenRouterError(f"OpenRouter HTTP {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise OpenRouterError(f"OpenRouter network error: {e}") from e

    try:
        message = payload["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as e:
        raise OpenRouterError(f"unexpected OpenRouter response shape: {payload!r}"[:400]) from e
    if not isinstance(message, dict):
        raise OpenRouterError("OpenRouter message is not an object")
    return _message_text(message)
