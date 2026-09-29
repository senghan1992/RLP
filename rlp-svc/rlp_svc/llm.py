"""Provider config + raw chat-completion client.

Reads `<agent dir>/auth.json` + `<agent dir>/models.json` — RLP's own
`~/.rlp/agent` unless `RLP_CODING_AGENT_DIR` / `RPI_CODING_AGENT_DIR` says
otherwise (see `paths.py`), with `RLP_PI_AUTH` / `RLP_PI_MODELS` overriding the
two files individually. No dependency on rlm's client classes.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import httpx

from . import paths

#: The engine's own model needs — decomposition, routing, verdicts — are
#: answered from the ladder, never from a literal model id in this file.
#:
#: They used to be four constants naming one host's gateway. Every other host
#: then had a decomposer and a router pointed at a provider it had never heard
#: of: the laya gate worked, the LLM fallbacks behind it did not, and the
#: failure read as "the model returned no JSON". A model ref only means
#: something relative to a configured endpoint, so the ladder — the file that
#: knows which endpoints exist — is the only honest source for one.
#:
#: Resolution order, per role:
#:   1. the explicit env override (`RLP_DECOMPOSE_MODEL`, `RLP_ROUTE_MODEL`)
#:   2. the ladder's `roles.<role>` binding, when the operator set one
#:   3. the first ladder arm declaring that role
#:   4. the ladder's brain
#: Anything left after that is a ladder with no arms, which the caller reports
#: as "not configured" rather than inventing a fifth answer.
_ROLE_FOR = {"plan": "RLP_DECOMPOSE_MODEL", "route": "RLP_ROUTE_MODEL", "verify": "RLP_VERIFY_MODEL"}

#: Completion budgets, in tokens. A reasoning model spends a large part of its
#: budget *thinking* before it writes anything: measured against the host
#: gateway, one DAG request put 1968 reasoning tokens inside a 2048-token
#: budget. A budget sized for a non-reasoning model therefore truncates the
#: answer to nothing, which reaches the caller as "the model returned no JSON"
#: — the least actionable sentence available. These are ceilings, not targets:
#: the cost of headroom is zero, and the cost of too little is a failed plan.
JSON_LINE_TOKENS = 1024  # triage, routing: one short JSON object
VERDICT_TOKENS = 1500  # a verifier verdict, with room to think first
DAG_TOKENS = 8192  # a 2-12 node DAG, or a critic returning a corrected one


def _auth_path() -> str:
    return os.environ.get("RLP_PI_AUTH") or str(paths.agent_dir() / "auth.json")


def _models_path() -> str:
    return os.environ.get("RLP_PI_MODELS") or str(paths.agent_dir() / "models.json")


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


def _split_spec(spec: str) -> tuple[str, str] | None:
    """Split 'provider/model' into (provider, model), or None if it is not one.

    A bare model name with no provider is rejected rather than paired with some
    default endpoint: which endpoint serves `gpt-4o-mini` is exactly the thing
    that cannot be assumed, and guessing here is how a wrong provider reaches
    the network as a 404 that reads like a wrong model id.
    """
    provider, sep, model = spec.partition("/")
    if not sep or not provider.strip() or not model.strip():
        return None
    return provider, model


def _ladder_spec(role: str) -> tuple[str, str] | None:
    """The ladder's model for a planner-side role, as (provider, model), or None."""
    try:
        from . import orchestration as orch

        config = orch.load()
        if config is None:
            return None
        ref = orch.resolve_model_for_role(config, role) or config.get("brain")
        return _split_spec(ref) if isinstance(ref, str) else None
    except Exception:
        # A missing or unreadable ladder is not this module's error to raise:
        # the caller already reports "not configured" with the fix attached.
        return None


def _role_spec(role: str) -> tuple[str, str] | None:
    """Resolve one engine role: env override, then the ladder. See `_ROLE_FOR`."""
    env = _ROLE_FOR.get(role)
    raw = os.environ.get(env) if env else None
    if raw and raw.strip():
        explicit = _split_spec(raw.strip())
        if explicit:
            return explicit
        raise ValueError(f"{env}={raw!r} must be a 'provider/model' string")
    return _ladder_spec(role)


def decomp_spec() -> tuple[str, str] | None:
    """Model for decomposition: `RLP_DECOMPOSE_MODEL`, else the ladder's `plan` role."""
    return _role_spec("plan")


def route_spec() -> tuple[str, str] | None:
    """Model for the LLM routing/triage fallback: `RLP_ROUTE_MODEL`, else the ladder."""
    return _role_spec("route")


def verify_spec() -> tuple[str, str] | None:
    """Model for independent verdicts: `RLP_VERIFY_MODEL`, else the ladder."""
    return _role_spec("verify")


def role_candidates(role: str) -> list[tuple[str, str]]:
    """Ordered, deduped `(provider, model)` candidates for an engine-side role.

    The preferred spec first, then every other ladder arm, then the brain. This
    is what `chat_first` walks, and the reason it is a list rather than one spec
    is that a single arm answering with nothing used to end the call: the LLM
    fallbacks behind the laya gate are the last line, so they of all callers
    must not have a single point of failure.

    Empty means the ladder has no arms — "not configured", which the caller
    reports with its own fix rather than dressing up as a network error.
    """
    candidates: list[tuple[str, str]] = []

    def add(spec: tuple[str, str] | None) -> None:
        if spec and spec not in candidates:
            candidates.append(spec)

    add(_role_spec(role))
    try:
        from . import orchestration as orch

        config = orch.load()
    except Exception:
        config = None
    if config is not None:
        for worker in config["workers"]:
            for arm in worker["models"]:
                if role in arm["roles"]:
                    add(_split_spec(arm["model"]))
        for worker in config["workers"]:
            for arm in worker["models"]:
                add(_split_spec(arm["model"]))
        if isinstance(config.get("brain"), str):
            add(_split_spec(config["brain"]))
    return candidates


def chat(
    provider: str,
    model: str,
    messages: list[dict[str, str]],
    max_tokens: int = 2048,
    temperature: float = 0.0,
    timeout: float = 120.0,
) -> str:
    """Plain OpenAI-compatible chat-completions call. Returns the assistant text.

    `timeout` is the whole-request ceiling in seconds. It is a parameter because
    the callers that walk several candidate arms share one budget between them:
    a hung gateway must not be able to hold a plan open for N × 120 s.

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
        timeout=timeout,
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
    timeout: float | None = None,
    **kwargs,
) -> tuple[str, tuple[str, str]]:
    """First real reply from an ordered list of `(provider, model)` candidates.

    The engine's callers all have the same contract — *shape* is the contract,
    not the engine — and every one of them had the same hole: a single arm that
    answers with nothing ends the call. Walking a short candidate list is what
    makes "never stall" true rather than aspirational. Returns
    `(text, spec_used)` so a caller can record which arm carried it.

    `timeout` is the per-candidate ceiling. A caller walking N candidates with a
    budget shares it out before calling (see `decompose._attempt_timeout`): the
    point of a candidate list is a *bounded* retry, and N × the single-call
    timeout is not bounded in any sense that matters.

    `chat_fn` is the client to call, defaulting to this module's `chat`; a caller
    that has its own seam (the decomposer stubs one for the offline suite) passes
    it rather than being reached through two different patch points.
    """
    send = chat_fn or chat
    errors: list[str] = []
    for spec in specs:
        try:
            extra = {"timeout": timeout} if timeout is not None else {}
            return send(*spec, messages=messages, **extra, **kwargs), spec
        except Exception as e:  # noqa: BLE001 - each candidate is reported, not raised
            errors.append(f"{spec[0]}/{spec[1]}: {str(e)[:160]}")
    raise RuntimeError("every candidate model failed — " + "; ".join(errors))


def _post(url: str, headers: dict, body: dict, timeout: float) -> httpx.Response:
    """The one place the engine's chat client touches the network.

    Extracted so the offline suite can assert on the request and the reply shape
    without a key, a network, or a model.
    """
    return httpx.post(url, headers=headers, json=body, timeout=timeout)

