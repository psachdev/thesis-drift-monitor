#!/usr/bin/env python3
"""
llm.py — one place that talks to the model.

Both capture.py and audit.py used to carry their own copy of this. Having two
copies meant fixing the max_tokens bug twice, so it lives here now.

Provider is configurable because the DeepSeek API is OpenAI-compatible, which
means anything speaking that protocol works without code changes. Set these in
your shell if you want a different provider:

    export LLM_BASE_URL=https://api.openai.com/v1
    export LLM_API_KEY=sk-...
    export LLM_MODEL_FAST=gpt-4o-mini
    export LLM_MODEL_SMART=gpt-4o

Defaults are DeepSeek, and DEEPSEEK_API_KEY is still honoured so existing
setups keep working.
"""

from __future__ import annotations

import json
import os
import re

import requests

BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.deepseek.com").rstrip("/")

# Two tiers. Fast for extraction and high-volume classification, smart for the
# judgment calls. Auditing kill criteria is a judgment call and measurably
# better on the smart tier - it used roughly half the tokens to reach better
# answers, so the extra cost per token is partly offset.
MODEL_FAST = os.environ.get("LLM_MODEL_FAST", "deepseek-v4-flash")
MODEL_SMART = os.environ.get("LLM_MODEL_SMART", "deepseek-v4-pro")

# NOT the answer length. On a reasoning model this budget covers the hidden
# reasoning tokens as well as the visible answer, and the reasoning half scales
# with task difficulty rather than with how long you want the output. Set this
# too low and you get HTTP 200 with an empty string.
MAX_TOKENS = 16_000


class LLMError(RuntimeError):
    pass


def api_key() -> str:
    key = os.environ.get("LLM_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        raise LLMError(
            "No API key. Set one of:\n"
            "  export DEEPSEEK_API_KEY=sk-...\n"
            "  export LLM_API_KEY=sk-...   (with LLM_BASE_URL for other providers)"
        )
    return key


def complete_json(system: str, user: str, model: str,
                  temperature: float = 0.2, timeout: int = 180) -> dict:
    """One call, JSON back. Raises LLMError with something useful on failure."""
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
        "max_tokens": MAX_TOKENS,
        "response_format": {"type": "json_object"},
    }

    try:
        resp = requests.post(
            f"{BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {api_key()}",
                     "Content-Type": "application/json"},
            json=payload,
            timeout=timeout,
        )
    except requests.exceptions.Timeout:
        raise LLMError(f"Timed out after {timeout}s.")
    except requests.exceptions.RequestException as exc:
        raise LLMError(f"Request failed: {exc}")

    if resp.status_code == 401:
        raise LLMError("401 Unauthorized. Check your API key.")
    if resp.status_code == 402:
        raise LLMError("402 Payment Required. Top up your balance.")
    if resp.status_code == 400 and "model" in resp.text.lower():
        raise LLMError(
            f"400 on model '{model}'. DeepSeek retired the deepseek-chat and "
            "deepseek-reasoner aliases in July 2026 - use deepseek-v4-flash or "
            "deepseek-v4-pro, or set LLM_MODEL_FAST / LLM_MODEL_SMART."
        )
    if resp.status_code != 200:
        raise LLMError(f"HTTP {resp.status_code}: {resp.text[:400]}")

    body = resp.json()
    choice = body["choices"][0]
    message = choice["message"]
    content = (message.get("content") or "").strip()
    finish = choice.get("finish_reason")
    usage = body.get("usage", {})

    if not content:
        reasoning = message.get("reasoning_content") or ""
        raise LLMError(
            f"Empty content (finish_reason={finish}). Reasoning produced "
            f"{len(reasoning):,} chars. If that is non-zero, MAX_TOKENS "
            f"({MAX_TOKENS:,}) was too low - the model spent the whole budget "
            "thinking before it started answering."
        )

    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*", "", content)
        content = re.sub(r"\s*```$", "", content)

    try:
        result = json.loads(content)
    except json.JSONDecodeError as exc:
        raise LLMError(f"Model did not return valid JSON: {exc}\n{content[:600]}")

    result["_usage"] = {
        "in": usage.get("prompt_tokens"),
        "out": usage.get("completion_tokens"),
        "cached": usage.get("prompt_cache_hit_tokens", 0),
        "hit_ceiling": usage.get("completion_tokens") == MAX_TOKENS,
    }
    return result


def coerce_list(value) -> list[str]:
    """Schema compliance is per-field and intermittent.

    In one run the model returned kill_criteria as a proper array and
    supporting_facts as a bare string, in the same response. Iterating a string
    yields characters, so anything declared as a list gets normalised here
    before it reaches display or storage.
    """
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, str):
        parts = [p.strip(" -\u2022\t") for p in value.split("\n") if p.strip()]
        return parts if len(parts) > 1 else [value.strip()]
    return [str(value)]
