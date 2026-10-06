"""Minimal, provider-agnostic LLM client.

Deliberately built on `requests` rather than a vendor SDK: the demo needs to run
on whatever key is available on the day, and swapping providers should not mean
reinstalling the environment ten minutes before the session.

Configure ONE of:

  Anthropic
      setx ANTHROPIC_API_KEY "sk-ant-..."
      (optional) setx KAG_MODEL "claude-sonnet-5"

  Anything OpenAI-compatible -- Groq, OpenAI, Azure, an internal gateway
      setx LLM_BASE_URL "https://api.groq.com/openai/v1"
      setx LLM_API_KEY  "gsk_..."
      setx KAG_MODEL    "llama-3.3-70b-versatile"

With no key configured, `complete()` raises NoLLMConfigured. Callers catch it and
fall back to printing the grounded prompt, so the data pipeline still demos.
"""

from __future__ import annotations

import os
import requests

# Opus 5 by default: this is a reasoning task over a retrieved subgraph, running
# live in front of a room, and the whole demo costs about five cents a run --
# there is nothing to save by trading down. Override with KAG_MODEL if you want
# claude-sonnet-5 (~2.5x cheaper) or claude-haiku-4-5 (~5x cheaper).
ANTHROPIC_DEFAULT_MODEL = "claude-opus-5"
OPENAI_DEFAULT_MODEL = "gpt-4o-mini"
TIMEOUT = 120


class NoLLMConfigured(RuntimeError):
    """No API key found in the environment."""


class LLMError(RuntimeError):
    """The provider was reachable but the call failed."""


def provider() -> str:
    """Returns 'anthropic', 'openai' or 'none' based on what is configured."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY"):
        return "openai"
    return "none"


def model_name() -> str:
    explicit = os.environ.get("KAG_MODEL")
    if explicit:
        return explicit
    return ANTHROPIC_DEFAULT_MODEL if provider() == "anthropic" else OPENAI_DEFAULT_MODEL


def complete(prompt: str, system: str = "", max_tokens: int = 1600) -> str:
    """Sends one prompt, returns the text response."""
    which = provider()
    if which == "anthropic":
        return _anthropic(prompt, system, max_tokens)
    if which == "openai":
        return _openai_compatible(prompt, system, max_tokens)
    raise NoLLMConfigured(
        "No LLM key found. Set ANTHROPIC_API_KEY, or LLM_BASE_URL + LLM_API_KEY."
    )


def _anthropic(prompt: str, system: str, max_tokens: int) -> str:
    base = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/")
    body = {
        "model": model_name(),
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        body["system"] = system

    resp = requests.post(
        f"{base}/v1/messages",
        headers={
            "x-api-key": os.environ["ANTHROPIC_API_KEY"],
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json=body,
        timeout=TIMEOUT,
    )
    if resp.status_code != 200:
        raise LLMError(f"Anthropic returned {resp.status_code}: {resp.text[:400]}")

    blocks = resp.json().get("content", [])
    return "".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()


def _openai_compatible(prompt: str, system: str, max_tokens: int) -> str:
    base = os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    key = os.environ.get("LLM_API_KEY") or os.environ["OPENAI_API_KEY"]

    messages = ([{"role": "system", "content": system}] if system else []) + [
        {"role": "user", "content": prompt}
    ]
    resp = requests.post(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={"model": model_name(), "max_tokens": max_tokens, "messages": messages},
        timeout=TIMEOUT,
    )
    if resp.status_code != 200:
        raise LLMError(f"LLM returned {resp.status_code}: {resp.text[:400]}")

    return resp.json()["choices"][0]["message"]["content"].strip()
