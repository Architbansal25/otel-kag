"""Minimal, provider-agnostic LLM client.

Deliberately built on `requests` rather than a vendor SDK: the demo needs to run
on whatever key is available on the day, and swapping providers should not mean
reinstalling the environment ten minutes before the session.

Configure ONE of:

  Anthropic
      setx ANTHROPIC_API_KEY "sk-ant-..."
      (optional) setx KAG_MODEL  "claude-sonnet-5-5"
      (optional) setx KAG_EFFORT "low"     faster answers on stage

  Anything OpenAI-compatible -- Groq, OpenAI, Azure, an internal gateway
      setx LLM_BASE_URL "https://api.groq.com/openai/v1"
      setx LLM_API_KEY  "gsk_..."
      setx KAG_MODEL    "llama-3.3-70b-versatile"

With no key configured, `complete()` and `structured()` raise NoLLMConfigured.
Callers catch it and fall back (to the grounded prompt, or to a rule-based
answer), so the data pipeline still demos.

`structured()` returns a validated Pydantic object. On Anthropic it uses
structured outputs, so the response is guaranteed to match the schema; on
OpenAI-compatible providers it uses JSON mode and validates client-side.
"""

from __future__ import annotations

import copy
import json
import os
from typing import Any, Dict, Type, TypeVar

import requests
from pydantic import BaseModel, ValidationError

# Opus 5.5 by default: this is a reasoning task over a retrieved subgraph, running
# live in front of a room, and the whole demo costs a few cents a run -- there is
# nothing to save by trading down. Override with KAG_MODEL if you want
# claude-sonnet-5-5 (~2x cheaper) or claude-haiku-4-5 (~4x cheaper).
ANTHROPIC_DEFAULT_MODEL = "claude-opus-5-5"

# Models that accept the server-side refusal fallback. A refused diagnosis on
# stage is worse than an answer from a fallback model.
FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5", "claude-fable-5-1"}

T = TypeVar("T", bound=BaseModel)
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


def structured(prompt: str, schema: Type[T], system: str = "", max_tokens: int = 16000) -> T:
    """Sends one prompt and returns a validated instance of `schema`."""
    which = provider()
    if which == "anthropic":
        text = _anthropic(prompt, system, max_tokens,
                          output_format={"type": "json_schema",
                                         "schema": strict_schema(schema)})
        try:
            return schema.model_validate_json(text)
        except ValidationError as exc:
            raise LLMError(f"response did not match {schema.__name__}: {exc}") from exc
    if which == "openai":
        return _openai_structured(prompt, schema, system, max_tokens)
    raise NoLLMConfigured(
        "No LLM key found. Set ANTHROPIC_API_KEY, or LLM_BASE_URL + LLM_API_KEY."
    )


def strict_schema(schema: Type[BaseModel]) -> Dict[str, Any]:
    """Pydantic JSON schema -> the strict subset structured outputs accept.

    Inlines $defs, closes every object (additionalProperties: false), marks
    every property required (optional ones stay nullable), and drops keywords
    the API does not take.
    """
    raw = schema.model_json_schema()
    defs = raw.pop("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, list):
            return [walk(n) for n in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            return walk(copy.deepcopy(defs[node["$ref"].split("/")[-1]]))
        out = {}
        for k, v in node.items():
            if k == "properties":
                # Field names, not keywords: keep them all (a field may be called "title").
                out[k] = {name: walk(sub) for name, sub in v.items()}
            elif k not in ("default", "title", "minimum", "maximum",
                           "minLength", "maxLength"):
                out[k] = walk(v)
        if out.get("type") == "object" and "properties" in out:
            out["additionalProperties"] = False
            out["required"] = list(out["properties"])
        return out

    return walk(raw)


def _anthropic(prompt: str, system: str, max_tokens: int,
               output_format: Dict[str, Any] = None) -> str:
    base = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/")
    body = {
        "model": model_name(),
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        body["system"] = system
    output_config: Dict[str, Any] = {}
    if output_format:
        output_config["format"] = output_format
    if os.environ.get("KAG_EFFORT"):
        output_config["effort"] = os.environ["KAG_EFFORT"]
    if output_config:
        body["output_config"] = output_config

    headers = {
        "x-api-key": os.environ["ANTHROPIC_API_KEY"],
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    if model_name() in FALLBACK_MODELS and "ANTHROPIC_BASE_URL" not in os.environ:
        headers["anthropic-beta"] = "server-side-fallback-2026-07-01"
        body["fallbacks"] = "default"

    resp = requests.post(f"{base}/v1/messages", headers=headers, json=body, timeout=TIMEOUT)
    if resp.status_code != 200:
        raise LLMError(f"Anthropic returned {resp.status_code}: {resp.text[:400]}")

    data = resp.json()
    if data.get("stop_reason") == "refusal":
        raise LLMError("the model declined to answer: " + json.dumps(data.get("stop_details")))
    if data.get("stop_reason") == "max_tokens":
        raise LLMError("the response hit max_tokens before finishing")
    blocks = data.get("content", [])
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


def _openai_structured(prompt: str, schema: Type[T], system: str, max_tokens: int) -> T:
    """JSON mode + client-side validation, with one corrective retry."""
    base = os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    key = os.environ.get("LLM_API_KEY") or os.environ["OPENAI_API_KEY"]
    instructions = (system + "\n\n" if system else "") + (
        "Respond with a single JSON object and nothing else. It must validate "
        "against this JSON schema:\n" + json.dumps(strict_schema(schema)))
    messages = [{"role": "system", "content": instructions},
                {"role": "user", "content": prompt}]

    last_error = ""
    for _ in range(2):
        resp = requests.post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": model_name(), "max_tokens": max_tokens, "messages": messages,
                  "response_format": {"type": "json_object"}},
            timeout=TIMEOUT,
        )
        if resp.status_code != 200:
            raise LLMError(f"LLM returned {resp.status_code}: {resp.text[:400]}")
        text = resp.json()["choices"][0]["message"]["content"].strip()
        try:
            return schema.model_validate_json(text)
        except ValidationError as exc:
            last_error = str(exc)
            messages += [{"role": "assistant", "content": text},
                         {"role": "user", "content": "That JSON is invalid: " + last_error
                          + "\nReturn the corrected JSON object only."}]
    raise LLMError(f"response did not match {schema.__name__}: {last_error[:400]}")
