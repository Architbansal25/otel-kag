"""Minimal, provider-agnostic LLM client.

Deliberately built on `requests` rather than a vendor SDK: the demo needs to run
on whatever key is available on the day, and swapping providers should not mean
reinstalling the environment ten minutes before the session.

Configure ONE of:

  Anthropic
      setx ANTHROPIC_API_KEY "sk-ant-..."
      (optional) setx KAG_MODEL  "claude-sonnet-5-5"
      (optional) setx KAG_EFFORT "low"     faster answers on stage

  Groq -- one variable is enough (base URL and model default sensibly)
      setx GROQ_API_KEY "gsk_..."
      (optional) setx KAG_MODEL "openai/gpt-oss-120b"
      Check it before going on stage:  py llm.py

  Anything OpenAI-compatible -- OpenAI, Azure, an internal gateway
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
import time
from typing import Any, Dict, List, Type, TypeVar

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


GROQ_BASE_URL = "https://api.groq.com/openai/v1"
# Groq's named successor to llama-3.3-70b-versatile (retired on free/dev tiers, Aug 2026).
GROQ_DEFAULT_MODEL = "openai/gpt-oss-120b"

# The answer is a small JSON object (~1k tokens). OpenAI-compatible providers
# get a tight cap because some (Groq's free tier) count the whole max_tokens
# reservation against a per-minute token limit and reject large requests.
OPENAI_STRUCTURED_MAX_TOKENS = 2000
# Reasoning models (gpt-oss, qwen3) spend part of max_tokens thinking first.
REASONING_MAX_TOKENS = 4000


def _anthropic_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    return "" if key.startswith("gsk_") else key   # a Groq key in the wrong variable


def _openai_key() -> str:
    for var in ("LLM_API_KEY", "GROQ_API_KEY", "OPENAI_API_KEY"):
        if os.environ.get(var, "").strip():
            return os.environ[var].strip()
    misplaced = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    return misplaced if misplaced.startswith("gsk_") else ""


def _is_groq() -> bool:
    base = os.environ.get("LLM_BASE_URL", "")
    return "groq.com" in base or (not base and _openai_key().startswith("gsk_"))


def _base_url() -> str:
    return (os.environ.get("LLM_BASE_URL")
            or (GROQ_BASE_URL if _is_groq() else "https://api.openai.com/v1")).rstrip("/")


def provider() -> str:
    """Returns 'anthropic', 'openai' or 'none' based on what is configured.

    KAG_PROVIDER=anthropic|openai|groq forces the choice. Otherwise Anthropic
    wins when it has a key -- unless KAG_MODEL names a non-Claude model and an
    OpenAI-compatible key is also set, which can only mean "use that one". A
    Groq key (gsk_...) is recognised in any of the key variables.
    """
    has_anthropic, has_openai = bool(_anthropic_key()), bool(_openai_key())
    forced = os.environ.get("KAG_PROVIDER", "").strip().lower()
    if forced == "anthropic" and has_anthropic:
        return "anthropic"
    if forced in ("openai", "groq") and has_openai:
        return "openai"
    requested = os.environ.get("KAG_MODEL", "").strip()
    if has_anthropic and has_openai and requested and not requested.startswith("claude"):
        return "openai"
    if has_anthropic:
        return "anthropic"
    if has_openai:
        return "openai"
    return "none"


def provider_label() -> str:
    """What to show people: 'groq' rather than the protocol name."""
    which = provider()
    return "groq" if which == "openai" and _is_groq() else which


def model_name() -> str:
    requested = os.environ.get("KAG_MODEL", "").strip()
    if provider() == "anthropic":
        # A Groq/OpenAI model name sent to Anthropic is a guaranteed 404.
        return requested if requested.startswith("claude") else ANTHROPIC_DEFAULT_MODEL
    if _substitute:
        return _substitute   # the configured model turned out to be retired
    if requested.startswith("claude"):
        requested = ""   # and a Claude name sent to Groq/OpenAI is one too
    return requested or (GROQ_DEFAULT_MODEL if _is_groq() else OPENAI_DEFAULT_MODEL)


def config_warning() -> str:
    """A one-line explanation when configuration is being reinterpreted, else ''."""
    requested = os.environ.get("KAG_MODEL", "").strip()
    if _substitute_note:
        return _substitute_note
    if os.environ.get("ANTHROPIC_API_KEY", "").strip().startswith("gsk_"):
        return ("ANTHROPIC_API_KEY holds a Groq key (gsk_...), so it is used for Groq. "
                "Tidy-up: put it in GROQ_API_KEY and delete ANTHROPIC_API_KEY.")
    if provider() == "anthropic" and requested and not requested.startswith("claude"):
        return (f"KAG_MODEL={requested} is not a Claude model, so it is ignored and "
                f"{ANTHROPIC_DEFAULT_MODEL} is used. To use {requested}, set "
                "GROQ_API_KEY (or LLM_BASE_URL + LLM_API_KEY); or remove KAG_MODEL.")
    return ""


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
        return _openai_structured(prompt, schema, system,
                                  min(max_tokens, OPENAI_STRUCTURED_MAX_TOKENS))
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
        "x-api-key": _anthropic_key(),
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


def _post_openai(url: str, key: str, body: dict) -> requests.Response:
    """POST with one short wait on 429: Groq's free tier rate-limits by the minute,
    and a few seconds' pause beats falling back to the rule-based answer on stage."""
    for attempt in range(2):
        resp = requests.post(url, headers={"Authorization": f"Bearer {key}",
                                           "Content-Type": "application/json"},
                             json=body, timeout=TIMEOUT)
        if resp.status_code != 429 or attempt == 1:
            return resp
        try:
            wait = float(resp.headers.get("retry-after", "3"))
        except ValueError:
            wait = 3.0
        time.sleep(min(max(wait, 1.0), 8.0))
    return resp


# --- surviving model retirements ------------------------------------------------
# Hosted model ids come and go (Groq retired llama-3.3-70b-versatile on free and
# developer tiers in August 2026). When the configured one is gone, ask the
# provider what this key can use and take the best match, in this order.
MODEL_PREFERENCE = ["openai/gpt-oss-120b", "gpt-oss-120b", "openai/gpt-oss-20b", "gpt-oss-20b",
                    "llama-4-maverick", "llama-4-scout", "qwen", "llama-3.3-70b", "llama-3.1-8b",
                    "gpt-4.1-mini", "gpt-4o-mini"]
NOT_CHAT = ("whisper", "tts", "guard", "playai", "orpheus", "embed", "compound", "distil",
            "moderation", "dall-e", "transcribe")

_substitute = ""        # model chosen at runtime because the configured one is gone
_substitute_note = ""


def available_models() -> List[str]:
    """Chat model ids this key can use, per the provider's /models endpoint."""
    try:
        resp = requests.get(f"{_base_url()}/models",
                            headers={"Authorization": f"Bearer {_openai_key()}"}, timeout=15)
        ids = [m.get("id", "") for m in resp.json().get("data", [])] if resp.ok else []
    except (requests.RequestException, ValueError):
        ids = []
    return sorted(i for i in ids if i and not any(x in i.lower() for x in NOT_CHAT))


def _model_missing(resp: requests.Response) -> bool:
    text = resp.text.lower()
    return resp.status_code in (400, 404) and (
        "model_not_found" in text or "does not exist" in text or "decommissioned" in text)


def _switch_model(gone: str) -> bool:
    """Pick a replacement for a model the provider says is gone. True if found."""
    global _substitute, _substitute_note
    ids = [i for i in available_models() if i != gone]
    choice = next((i for pref in MODEL_PREFERENCE for i in ids if pref in i), ids[0] if ids else "")
    if not choice:
        return False
    _substitute = choice
    _substitute_note = (f"{gone} is not available to this key (retired or not enabled), "
                        f"so {choice} is used instead. Set KAG_MODEL to pick another.")
    print("  LLM: " + _substitute_note)
    return True


def _is_reasoning(model: str) -> bool:
    return "gpt-oss" in model or "qwen3" in model or model.startswith(("o1", "o3", "o4"))


def _chat(messages: list, max_tokens: int, json_mode: bool = False) -> str:
    """One chat completion, handling retired models, reasoning models and
    providers that do not support JSON mode."""
    base, key = _base_url(), _openai_key()
    for _ in range(3):
        model = model_name()
        body = {"model": model, "messages": messages, "max_tokens": max_tokens}
        if _is_reasoning(model):
            # Thinking tokens count against max_tokens: leave room for them, and
            # keep the thinking short so answers stay quick on stage.
            body["max_tokens"] = max(max_tokens, REASONING_MAX_TOKENS)
            effort = os.environ.get("KAG_EFFORT", "low").lower()
            body["reasoning_effort"] = effort if effort in ("low", "medium", "high") else "low"
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        resp = _post_openai(f"{base}/chat/completions", key, body)
        if resp.status_code == 200:
            choice = resp.json()["choices"][0]
            text = (choice.get("message") or {}).get("content") or ""
            if not text.strip() and choice.get("finish_reason") == "length":
                raise LLMError("the response hit max_tokens before finishing")
            return text.strip()
        if _model_missing(resp) and model != _substitute and _switch_model(model):
            continue
        if json_mode and resp.status_code == 400 and "response_format" in resp.text:
            json_mode = False   # provider/model without JSON mode: ask in the prompt only
            continue
        raise LLMError(f"LLM returned {resp.status_code} for model {model}: {resp.text[:400]}")
    raise LLMError("LLM call failed after switching models")


def _extract_json(text: str) -> str:
    """The JSON object in a reply, even if wrapped in ``` fences or prose."""
    start, end = text.find("{"), text.rfind("}")
    return text[start:end + 1] if 0 <= start < end else text


def _openai_compatible(prompt: str, system: str, max_tokens: int) -> str:
    messages = ([{"role": "system", "content": system}] if system else []) + [
        {"role": "user", "content": prompt}
    ]
    return _chat(messages, max_tokens)


def _openai_structured(prompt: str, schema: Type[T], system: str, max_tokens: int) -> T:
    """JSON mode + client-side validation, with one corrective retry."""
    instructions = (system + "\n\n" if system else "") + (
        "Respond with a single JSON object and nothing else. It must validate "
        "against this JSON schema:\n" + json.dumps(strict_schema(schema)))
    messages = [{"role": "system", "content": instructions},
                {"role": "user", "content": prompt}]

    last_error = ""
    for _ in range(2):
        text = _chat(messages, max_tokens, json_mode=True)
        try:
            return schema.model_validate_json(_extract_json(text))
        except ValidationError as exc:
            last_error = str(exc)
            messages += [{"role": "assistant", "content": text},
                         {"role": "user", "content": "That JSON is invalid: " + last_error
                          + "\nReturn the corrected JSON object only."}]
    raise LLMError(f"response did not match {schema.__name__}: {last_error[:400]}")


if __name__ == "__main__":
    # Quick pre-flight check:  py llm.py
    print("provider :", provider_label())
    print("model    :", model_name())
    if config_warning():
        print("note     :", config_warning())
    if provider() == "openai":
        print("base URL :", _base_url())
        models = available_models()
        print("your key can use:", ", ".join(models) if models else "(could not list models)")
    if provider() != "none":
        try:
            reply = complete("Reply with exactly: OK", max_tokens=200)
            print("test call:", reply[:80] or "(empty)", "| model:", model_name())
        except LLMError as exc:
            print("test call FAILED:", exc)
