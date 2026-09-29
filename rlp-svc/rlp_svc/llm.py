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
    """Plain OpenAI-compatible chat-completions call. Returns the assistant text."""
    base_url = _provider_base_url(provider)
    key = _provider_key(provider)
    url = base_url.rstrip("/") + "/chat/completions"
    resp = httpx.post(
        url,
        headers={"Authorization": f"Bearer {key}"},
        json={
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        },
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]

