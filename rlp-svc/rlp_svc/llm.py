"""Provider config + raw chat-completion client.

Reads ~/.pi/agent/auth.json + ~/.pi/agent/models.json (paths overridable via
RLP_PI_AUTH / RLP_PI_MODELS). No dependency on rlm's client classes.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import httpx

DECOMP_PROVIDER = "qwen-token-plan"
DECOMP_MODEL = "qwen3.8-max"
ROUTE_PROVIDER = "qwen-token-plan"
ROUTE_MODEL = "deepseek-v4.1-flash"


def _auth_path() -> str:
    return os.environ.get("RLP_PI_AUTH", str(Path.home() / ".pi" / "agent" / "auth.json"))


def _models_path() -> str:
    return os.environ.get("RLP_PI_MODELS", str(Path.home() / ".pi" / "agent" / "models.json"))


def _provider_key(provider: str) -> str:
    """Return the bearer token for a provider. Handles both api_key and oauth shapes."""
    auth = json.loads(Path(_auth_path()).read_text())
    entry = auth.get(provider)
    if entry is None:
        raise KeyError(f"provider {provider!r} not found in auth.json")
    key = entry.get("key") or entry.get("access")
    if key is None:
        raise ValueError(f"provider {provider!r} has neither 'key' nor 'access'")
    return key


def _provider_base_url(provider: str) -> str:
    """Return the baseUrl for a provider from models.json."""
    models = json.loads(Path(_models_path()).read_text())
    entry = models.get("providers", {}).get(provider)
    if entry is None:
        raise KeyError(f"provider {provider!r} not found in models.json")
    return entry["baseUrl"]


def _split_spec(spec: str) -> tuple[str, str]:
    """Split 'provider/model' into (provider, model)."""
    provider, sep, model = spec.partition("/")
    if not sep:
        return DECOMP_PROVIDER, spec
    return provider, model


def decomp_spec() -> tuple[str, str]:
    """Effective decompose model spec, honouring RLP_DECOMPOSE_MODEL (provider/model)."""
    raw = os.environ.get("RLP_DECOMPOSE_MODEL")
    if raw:
        return _split_spec(raw)
    return DECOMP_PROVIDER, DECOMP_MODEL


def route_spec() -> tuple[str, str]:
    """Effective route model spec (fixed to the fast arm)."""
    return ROUTE_PROVIDER, ROUTE_MODEL


def chat(
    provider: str,
    model: str,
    messages: list[dict[str, str]],
    max_tokens: int = 2048,
    temperature: float = 0.0,
) -> str:
    """Plain OpenAI-compatible chat-completions call. Returns the assistant text.

    Raises `RuntimeError` when the endpoint answers without any text. That case
    used to return `""`, which every caller then reported as "the model returned
    no JSON" — the least useful sentence available, and one that hides the three
    things that actually cause it: the reply was truncated at `max_tokens`, the
    endpoint put the text in `reasoning_content` (a reasoning model, mid-thought),
    or the gateway returned a boilerplate empty completion. Naming the cause is
    the difference between a fixable error and a mystery.
    """
    base_url = _provider_base_url(provider)
    key = _provider_key(provider)
    url = base_url.rstrip("/") + "/chat/completions"
    resp = _post(
        url,
        headers={"Authorization": f"Bearer {key}"},
        body={
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        },
        timeout=120.0,
    )
    resp.raise_for_status()
    data = resp.json()
    choices = data.get("choices") if isinstance(data, dict) else None
    if not isinstance(choices, list) or not choices:
        raise RuntimeError(f"{provider}/{model} returned no choices")
    choice = choices[0] if isinstance(choices[0], dict) else {}
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return content
    reasoning = message.get("reasoning_content") or message.get("reasoning")
    detail = [f"finish_reason={choice.get('finish_reason')!r}"]
    if reasoning:
        detail.append("the text is in reasoning_content (a reasoning model, or the reply was cut off mid-thought)")
    if isinstance(content, str):
        detail.append("content was empty")
    raise RuntimeError(f"{provider}/{model} returned an empty message ({', '.join(detail)})")


def chat_first(
    specs: list[tuple[str, str]],
    messages: list[dict[str, str]],
    chat_fn=None,
    **kwargs,
) -> tuple[str, tuple[str, str]]:
    """First real reply from an ordered list of `(provider, model)` candidates.

    The engine's callers all have the same contract — *shape* is the contract,
    not the engine — and every one of them had the same hole: a single arm that
    answers with nothing ends the call. Walking a short candidate list is what
    makes "never stall" true rather than aspirational. Returns
    `(text, spec_used)` so a caller can record which arm carried it.

    `chat_fn` is the client to call, defaulting to this module's `chat`; a caller
    that has its own seam (the decomposer stubs one for the offline suite) passes
    it rather than being reached through two different patch points.
    """
    send = chat_fn or chat
    errors: list[str] = []
    for spec in specs:
        try:
            return send(*spec, messages=messages, **kwargs), spec
        except Exception as e:  # noqa: BLE001 - each candidate is reported, not raised
            errors.append(f"{spec[0]}/{spec[1]}: {str(e)[:160]}")
    raise RuntimeError("every candidate model failed — " + "; ".join(errors))


def _post(url: str, headers: dict, body: dict, timeout: float) -> httpx.Response:
    """The one place the engine's chat client touches the network.

    Extracted so the offline suite can assert on the request and the reply shape
    without a key, a network, or a model.
    """
    return httpx.post(url, headers=headers, json=body, timeout=timeout)

