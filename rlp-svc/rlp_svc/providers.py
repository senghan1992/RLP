"""rlp providers — the endpoints RLP can spend on, and their credentials.

Two files hold everything the harness needs to talk to a model, both in RLP's
own agent dir (`~/.rlp/agent` by default — see `paths.py`) rather than in pi's
`~/.pi`:

* `models.json` — the endpoint (``baseUrl``, ``api``) and the models attached to
  it. No secrets.
* `auth.json` — the credential per provider. ``0600``, and never printed by
  anything in this module.

Both are shared with the harness itself, so a provider added here is
immediately usable by ``/model``, by the RLP ladder arms, and by every worker.
``$RLP_PI_MODELS`` / ``$RLP_PI_AUTH`` relocate them, exactly as ``llm.py``
does, so one resolution rule serves the whole tool.

Why this lives in the engine rather than in the TUI extension: the ladder's
mutation path (``orchestration.mutate``) already established the contract —
validate first, back up, write atomically, name the offending field — and
credentials deserve at least that much. Doing it here also makes it testable
offline, scriptable from the shell, and reachable from the MCP server, instead
of existing only as a handful of file writes inside a terminal UI.

The rules this module keeps, because it writes to a credential store:

* a secret is never returned, logged, or included in an error — only
  ``present``/``absent`` and, at most, a masked prefix;
* a bad argument is a ``ValueError`` naming the field, never a partial write;
* an existing ``models.json`` keeps every key this module does not model, so an
  edit can never silently drop a field a newer harness added;
* a failed write leaves the previous file intact (backup + atomic replace).

Network helpers (``discover_models``, ``probe``) never raise: they return an
envelope with a classified ``kind`` and a one-line ``fix``, because "why did my
key not work" is the question this module exists to answer.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

import httpx

from . import paths

#: Default shapes for a model the user attaches by hand. The harness normalises
#: anything omitted, so these are a starting point, not a contract.
DEFAULT_CONTEXT_WINDOW = 128_000
DEFAULT_MAX_TOKENS = 8_192
_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")

#: Endpoint presets the `/provider` wizard offers. `baseUrl` is prefilled and
#: editable; `models` seeds the manual fallback when `/models` cannot be read;
#: `needsKey` only changes what the wizard says, never what is written.
PRESETS: list[dict[str, Any]] = [
    {
        "id": "openai",
        "label": "OpenAI",
        "baseUrl": "https://api.openai.com/v1",
        "models": ["gpt-5.1", "gpt-5.1-mini"],
        "needsKey": True,
        "note": "api.openai.com",
    },
    {
        "id": "anthropic",
        "label": "Anthropic (Claude)",
        "baseUrl": "https://api.anthropic.com/v1",
        "models": ["claude-opus-4-8", "claude-sonnet-4-5"],
        "needsKey": True,
        "note": "api.anthropic.com",
    },
    {
        "id": "openrouter",
        "label": "OpenRouter (many vendors)",
        "baseUrl": "https://openrouter.ai/api/v1",
        "models": ["anthropic/claude-opus-4-8", "openai/gpt-5.1", "google/gemini-3-pro"],
        "needsKey": True,
        "note": "one key, many vendors — the easiest way to get a second family",
    },
    {
        "id": "groq",
        "label": "Groq",
        "baseUrl": "https://api.groq.com/openai/v1",
        "models": ["llama-3.3-70b-versatile"],
        "needsKey": True,
        "note": "fast hosted open models",
    },
    {
        "id": "deepseek",
        "label": "DeepSeek",
        "baseUrl": "https://api.deepseek.com/v1",
        "models": ["deepseek-chat", "deepseek-reasoner"],
        "needsKey": True,
        "note": "api.deepseek.com",
    },
    {
        "id": "qwen",
        "label": "Alibaba DashScope (Qwen, OpenAI-compatible)",
        "baseUrl": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        "models": ["qwen3-max", "qwen3-coder-plus"],
        "needsKey": True,
        "note": "compatible-mode endpoint",
    },
    {
        "id": "ollama",
        "label": "Ollama (local)",
        "baseUrl": "http://localhost:11434/v1",
        "models": ["qwen3", "llama3.2"],
        "needsKey": False,
        "note": "no key needed; the endpoint must be running",
    },
    {
        "id": "lmstudio",
        "label": "LM Studio / vLLM / any local OpenAI-compatible server",
        "baseUrl": "http://localhost:1234/v1",
        "models": [],
        "needsKey": False,
        "note": "serve it, then /provider add <id> <baseUrl> <model>",
    },
    {
        "id": "custom",
        "label": "Custom endpoint",
        "baseUrl": "",
        "models": [],
        "needsKey": True,
        "note": "any OpenAI-compatible /chat/completions",
    },
]


# --- paths ----------------------------------------------------------------------


def models_path() -> Path:
    return Path(os.environ.get("RLP_PI_MODELS") or (paths.agent_dir() / "models.json"))


def auth_path() -> Path:
    return Path(os.environ.get("RLP_PI_AUTH") or (paths.agent_dir() / "auth.json"))


def _read_json(path: Path) -> dict:
    """Parsed JSON object, or {} when absent/unreadable.

    A provider store that cannot be parsed is not worth crashing over: the
    caller is told by `list_providers` that the endpoint list is empty, and the
    wizard refuses to overwrite a file it could not read.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _endpoints() -> dict:
    providers = _read_json(models_path()).get("providers")
    return providers if isinstance(providers, dict) else {}


def _read_required(path: Path) -> dict:
    """The store, refusing to proceed over a file that does not parse.

    A read-only path can shrug at a broken file (`_read_json`); a *write* path
    cannot, because rewriting it would silently destroy whatever was in there.
    The distinction that matters is "absent" (fine, start empty) versus "present
    but unparseable" (stop, tell the user) — an empty ``{}`` is perfectly valid
    and must not be mistaken for corruption.
    """
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"{path} is not valid JSON ({e}) — fix or move it first") from None
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


# --- validation -----------------------------------------------------------------


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _valid_id(provider: str) -> str:
    _require(isinstance(provider, str) and provider.strip() != "", "provider id must be a non-empty string")
    provider = provider.strip()
    _require(
        bool(_ID_RE.match(provider)),
        f'provider id {provider!r} is not usable (letters, digits, and . _ - only)',
    )
    return provider


def _valid_base_url(base_url: str) -> str:
    _require(isinstance(base_url, str) and base_url.strip() != "", "baseUrl must be a non-empty string")
    base_url = base_url.strip()
    _require(
        base_url.startswith("http://") or base_url.startswith("https://"),
        f'baseUrl {base_url!r} must start with http:// or https://',
    )
    return base_url.rstrip("/")


def _valid_model_ids(models: Any) -> list[str]:
    _require(isinstance(models, list), "models must be an array of model ids")
    out: list[str] = []
    for entry in models:
        # Accept a bare id, or the harness shape {id, name, …}.
        if isinstance(entry, dict):
            entry = entry.get("id")
        _require(isinstance(entry, str) and entry.strip() != "", f"bad model id {entry!r}")
        if entry not in out:
            out.append(entry.strip())
    return out


def _model_entry(model_id: str, *, context_window: int | None = None, max_tokens: int | None = None) -> dict:
    """A models.json model record the harness accepts, with sane defaults."""
    return {
        "id": model_id,
        "name": model_id,
        "reasoning": False,
        "input": ["text"],
        "contextWindow": int(context_window or DEFAULT_CONTEXT_WINDOW),
        "maxTokens": int(max_tokens or DEFAULT_MAX_TOKENS),
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
    }


# --- credential state -------------------------------------------------------------


def credential_state(provider: str) -> str:
    """``oauth`` | ``key`` | ``none``. Never the secret, only its shape."""
    entry = _read_json(auth_path()).get(provider)
    if not isinstance(entry, dict):
        return "none"
    if isinstance(entry.get("access"), str) and entry["access"]:
        return "oauth"
    if isinstance(entry.get("key"), str) and entry["key"]:
        return "key"
    return "none"


def credential_label(provider: str) -> str:
    return {"oauth": "oauth token", "key": "api key", "none": "no credential"}[credential_state(provider)]


def stored_credential(provider: str) -> str | None:
    """The raw credential for in-process use (a live check, never a report).

    Kept separate from `credential_state` so the only function that returns a
    secret is one whose name says so, and no caller can leak it by accident.
    """
    entry = _read_json(auth_path()).get(provider)
    if not isinstance(entry, dict):
        return None
    value = entry.get("key") or entry.get("access")
    return value if isinstance(value, str) and value else None


# --- atomic, backed-up writes -----------------------------------------------------


def _backup(path: Path) -> str | None:
    if not path.is_file():
        return None
    target = path.with_name(f"{path.name}.bak.{int(time.time())}")
    target.write_bytes(path.read_bytes())
    try:
        os.chmod(target, 0o600 if "auth" in path.name else 0o644)
    except OSError:
        pass
    return str(target)


def _write_json(path: Path, data: dict, *, secret: bool = False) -> str | None:
    """Back up, merge-safe write, atomic replace. Returns the backup path.

    `secret=True` forces ``0600``: auth.json holds bearer tokens, and creating
    it world-readable once is a leak that no later chmod undoes.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = _backup(path)
    payload = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600 if secret else 0o644)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(tmp, path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    if secret:
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    return backup


# --- the provider catalogue -------------------------------------------------------


def list_providers() -> list[dict]:
    """Every configured endpoint, with credential state and ladder usage.

    `ladder_arms` is what makes this a diagnosis rather than a directory: it
    says which endpoints RLP actually orchestrates on, so a user can see that
    the arm the router prefers is exactly the one with no credential.
    """
    from . import orchestration as orch

    arms: dict[str, list[dict]] = {}
    try:
        config = orch.load()
    except Exception:
        config = None
    if config:
        for worker in config["workers"]:
            for arm in worker["models"]:
                provider = arm["model"].partition("/")[0]
                arms.setdefault(provider, []).append(
                    {
                        "model": arm["model"],
                        "worker": worker["id"],
                        "roles": arm["roles"],
                        "available": worker.get("available", True),
                    }
                )

    cards: list[dict] = []
    for provider_id, entry in _endpoints().items():
        if not isinstance(entry, dict):
            continue
        models = entry.get("models") if isinstance(entry.get("models"), list) else []
        ids = [m.get("id") for m in models if isinstance(m, dict) and m.get("id")]
        state = credential_state(provider_id)
        in_ladder = arms.get(provider_id, [])
        cards.append(
            {
                "id": provider_id,
                "name": entry.get("name") or provider_id,
                "baseUrl": entry.get("baseUrl") or "",
                "api": entry.get("api") or "openai-completions",
                "models": ids,
                "modelCount": len(ids),
                "credential": state,
                "credentialLabel": credential_label(provider_id),
                "ladderArms": in_ladder,
                "runnable": state != "none" or bool(entry.get("apiKey")),
            }
        )
    cards.sort(key=lambda c: (c["credential"] == "none", c["id"]))
    return cards


def orphan_arms() -> list[dict]:
    """Ladder arms whose provider has no endpoint or no credential.

    These are the "visible but unusable" models that confuse every new install,
    named instead of left as a silent routing fallback.
    """
    from . import orchestration as orch

    try:
        config = orch.load()
    except Exception:
        return []
    if not config:
        return []
    endpoints = _endpoints()
    out: list[dict] = []
    for worker in config["workers"]:
        if not worker.get("available", True):
            continue
        for arm in worker["models"]:
            provider = arm["model"].partition("/")[0]
            entry = endpoints.get(provider)
            if not isinstance(entry, dict) or not entry.get("baseUrl"):
                out.append({"arm": arm["model"], "worker": worker["id"], "why": f"no endpoint configured for {provider!r}"})
            elif credential_state(provider) == "none" and not entry.get("apiKey"):
                out.append(
                    {
                        "arm": arm["model"],
                        "worker": worker["id"],
                        "why": f"{provider!r} has no credential — run /login {provider} or /provider key {provider}",
                    }
                )
    return out


# --- mutations --------------------------------------------------------------------


def add_provider(
    provider: str,
    base_url: str,
    models: list[Any] | None = None,
    *,
    api_key: str | None = None,
    name: str | None = None,
    api: str = "openai-completions",
    replace_models: bool = False,
) -> dict:
    """Attach an OpenAI-compatible endpoint, optionally with its credential.

    `replace_models=False` (the default) merges the given ids into whatever the
    endpoint already declares, so adding one model to an existing provider
    cannot silently delete the others. Raises ValueError on a bad argument,
    before anything is written.
    """
    provider = _valid_id(provider)
    base_url = _valid_base_url(base_url)
    _require(isinstance(api, str) and api.strip() != "", "api must be a non-empty string")
    ids = _valid_model_ids(models or [])
    if api_key is not None:
        _require(isinstance(api_key, str) and api_key.strip() != "", "api key must be a non-empty string")
        api_key = api_key.strip()

    doc = _read_required(models_path())
    bag = doc.get("providers") if isinstance(doc.get("providers"), dict) else {}
    existing = bag.get(provider) if isinstance(bag.get(provider), dict) else {}
    if replace_models or not existing:
        merged = ids
    else:
        merged = _valid_model_ids([*(existing.get("models") or []), *ids])

    entry: dict[str, Any] = {
        **existing,
        "name": name or existing.get("name") or provider,
        "baseUrl": base_url,
        "api": api.strip(),
        "models": [_model_entry(m, context_window=None, max_tokens=None) for m in merged],
    }
    bag = {**bag, provider: entry}
    backup = _write_json(models_path(), {**doc, "providers": bag})

    key_written = False
    if api_key:
        set_key(provider, api_key)
        key_written = True
    return {
        "path": str(models_path()),
        "backup": backup,
        "provider": provider,
        "baseUrl": base_url,
        "models": merged,
        "keyWritten": key_written,
    }


def set_key(provider: str, key: str, *, kind: str = "api_key") -> dict:
    """Write or replace one provider's credential in auth.json (0600)."""
    provider = _valid_id(provider)
    _require(isinstance(key, str) and key.strip() != "", "the credential must be a non-empty string")
    auth = _read_required(auth_path())
    auth = {**auth, provider: {"type": kind, "key": key.strip()}}
    backup = _write_json(auth_path(), auth, secret=True)
    return {"path": str(auth_path()), "backup": backup, "provider": provider, "credential": credential_state(provider)}


def clear_key(provider: str) -> dict:
    """Remove one credential. A missing one is a no-op, not an error."""
    provider = _valid_id(provider)
    auth = _read_required(auth_path())
    if provider not in auth:
        return {"path": str(auth_path()), "backup": None, "provider": provider, "removed": False}
    auth = {k: v for k, v in auth.items() if k != provider}
    backup = _write_json(auth_path(), auth, secret=True)
    return {"path": str(auth_path()), "backup": backup, "provider": provider, "removed": True}


def remove_provider(provider: str, *, drop_key: bool = False) -> dict:
    """Detach an endpoint. The credential is kept unless `drop_key` is set."""
    provider = _valid_id(provider)
    doc = _read_required(models_path())
    bag = doc.get("providers") if isinstance(doc.get("providers"), dict) else {}
    if provider not in bag:
        raise ValueError(f"no provider {provider!r} in {models_path()}")
    remaining = {k: v for k, v in bag.items() if k != provider}
    backup = _write_json(models_path(), {**doc, "providers": remaining})
    key = clear_key(provider) if drop_key else {"removed": False}
    return {
        "path": str(models_path()),
        "backup": backup,
        "provider": provider,
        "keyRemoved": bool(key.get("removed")),
    }


# --- live checks (never raise) ----------------------------------------------------


ERR_FIX = {
    "auth": "the endpoint rejected the credential — check the key, or set it again with /provider key <id>",
    "not_found": "the URL or model id is wrong — the base URL must end at the API root (…/v1), not at /chat/completions",
    "rate_limit": "the provider is rate-limiting this key — wait, or use a different arm",
    "server": "the provider returned a server error — its status page is the next step, not your config",
    "network": "could not reach the endpoint — check the URL, your network, and any proxy (SSL_CERT_FILE)",
    "tls": "TLS verification failed — behind a TLS-inspecting proxy, set SSL_CERT_FILE to the CA bundle",
    "bad_url": "that does not look like an http(s) URL",
    "bad_response": "the endpoint answered in a shape this tool does not recognise — check that it is OpenAI-compatible",
    "unknown": "see the message above",
}


def _classify(exc: Exception) -> tuple[str, str]:
    """Map a transport failure onto (kind, message) without leaking the key."""
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code in (401, 403):
            return "auth", f"HTTP {code} from the endpoint"
        if code == 404:
            return "not_found", f"HTTP {code} — no such route or model"
        if code == 429:
            return "rate_limit", "HTTP 429"
        if 500 <= code < 600:
            return "server", f"HTTP {code} from the endpoint"
        return "unknown", f"HTTP {code}: {exc.response.text[:160]}"
    if isinstance(exc, httpx.ConnectError):
        text = str(exc)
        if "CERTIFICATE" in text.upper() or "SSL" in text.upper():
            return "tls", "certificate verification failed"
        return "network", text[:200]
    if isinstance(exc, httpx.TimeoutException):
        return "network", f"timed out: {exc}"
    if isinstance(exc, httpx.UnsupportedProtocol):
        return "bad_url", str(exc)[:160]
    if isinstance(exc, httpx.HTTPError):
        return "network", str(exc)[:200]
    return "unknown", f"{type(exc).__name__}: {str(exc)[:200]}"


def failure(kind: str, message: str, **extra: Any) -> dict:
    return {"ok": False, "kind": kind, "error": message, "fix": ERR_FIX.get(kind, ERR_FIX["unknown"]), **extra}


def _headers(key: str | None) -> dict:
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return headers


def _http(method: str, url: str, *, headers: dict, body: dict | None = None, timeout: float = 20.0) -> httpx.Response:
    """The one place this module touches the network.

    Extracted so the offline suite can stub the transport and assert on the
    request it would have sent — the alternative is a test that needs an API
    key, which is a test nobody runs.
    """
    resp = httpx.request(method, url, headers=headers, json=body, timeout=timeout, follow_redirects=True)
    resp.raise_for_status()
    return resp


def _model_ids_from(payload: Any) -> list[str]:
    """Pull model ids out of the shapes OpenAI-compatible endpoints use."""
    items: Any = payload
    if isinstance(payload, dict):
        for key in ("data", "models", "result"):
            if isinstance(payload.get(key), list):
                items = payload[key]
                break
    if not isinstance(items, list):
        raise ValueError("no model list in the response")
    ids: list[str] = []
    for item in items:
        if isinstance(item, dict):
            value = item.get("id") or item.get("name") or item.get("model")
        else:
            value = item
        if isinstance(value, str) and value.strip() and value not in ids:
            ids.append(value.strip())
    return ids


def discover_models(base_url: str, api_key: str | None = None, *, timeout: float = 20.0) -> dict:
    """`GET {baseUrl}/models` — what the endpoint says it can serve.

    Returns ``{"ok": True, "models": [...]}`` or a classified failure. This is
    what turns "attach a provider" from "type a model id from memory" into
    "pick from the list".
    """
    try:
        base_url = _valid_base_url(base_url)
    except ValueError as e:
        return failure("bad_url", str(e))
    try:
        resp = _http("GET", f"{base_url}/models", headers=_headers(api_key), timeout=timeout)
        models = _model_ids_from(resp.json())
    except Exception as e:  # noqa: BLE001 - a probe reports, it never raises
        kind, message = _classify(e)
        return failure(kind, message)
    if not models:
        return failure("bad_response", "the endpoint returned an empty model list")
    return {"ok": True, "baseUrl": base_url, "models": sorted(models), "count": len(models)}


def probe(
    provider: str | None = None,
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
    timeout: float = 30.0,
) -> dict:
    """One real completion round trip: does this endpoint actually answer?

    `provider` resolves base URL, model and credential from disk; `base_url`
    (+ `api_key`) checks an endpoint before it is written. Reports the model it
    used, the latency, and — on failure — a classified reason and a fix, which
    is the difference between "it did not work" and "your key is rejected".
    """
    if provider and not base_url:
        provider = _valid_id(provider)
        entry = _endpoints().get(provider)
        if not isinstance(entry, dict) or not entry.get("baseUrl"):
            return failure("not_found", f"no endpoint configured for {provider!r}", provider=provider)
        base_url = str(entry["baseUrl"])
        state = credential_state(provider)
        if state == "none" and not entry.get("apiKey"):
            return failure(
                "auth",
                f"{provider!r} has no credential",
                provider=provider,
                baseUrl=base_url,
            )
        if api_key is None:
            api_key = stored_credential(provider)
        if model is None:
            models = entry.get("models") if isinstance(entry.get("models"), list) else []
            model = next((m.get("id") for m in models if isinstance(m, dict) and m.get("id")), None)

    try:
        base_url = _valid_base_url(base_url or "")
    except ValueError as e:
        return failure("bad_url", str(e), provider=provider)
    if not model:
        return failure("bad_response", "no model to test — give the endpoint a model first", provider=provider, baseUrl=base_url)

    body = {"model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1}
    started = time.monotonic()
    try:
        resp = _http("POST", f"{base_url}/chat/completions", headers=_headers(api_key), body=body, timeout=timeout)
        payload = resp.json()
    except Exception as e:  # noqa: BLE001
        kind, message = _classify(e)
        result = failure(kind, message, provider=provider, baseUrl=base_url, model=model)
        # A 404 on the completion route often means the model id is wrong rather
        # than the URL: one cheap extra call tells the user which.
        if kind == "not_found":
            listing = discover_models(base_url, api_key, timeout=min(timeout, 15.0))
            if listing.get("ok"):
                result["availableModels"] = listing["models"][:40]
                result["fix"] = (
                    f"the endpoint answered /models, so the URL is right — {model!r} is not a model it serves. "
                    "Pick one from `availableModels` (/provider add … or /models after /reload)."
                )
        return result

    latency_ms = int((time.monotonic() - started) * 1000)
    choices = payload.get("choices") if isinstance(payload, dict) else None
    if not isinstance(choices, list) or not choices:
        return failure(
            "bad_response",
            "the completion returned no choices",
            provider=provider,
            baseUrl=base_url,
            model=model,
            latencyMs=latency_ms,
        )
    return {
        "ok": True,
        "provider": provider,
        "baseUrl": base_url,
        "model": model,
        "latencyMs": latency_ms,
        "latency": f"{latency_ms / 1000:.1f}s",
    }


def summary() -> dict:
    """Everything the TUI needs in one call: endpoints, gaps, presets, paths."""
    cards = list_providers()
    return {
        "modelsPath": str(models_path()),
        "authPath": str(auth_path()),
        "providers": cards,
        "credentialed": [c["id"] for c in cards if c["credential"] != "none"],
        "withoutCredential": [c["id"] for c in cards if c["credential"] == "none"],
        "orphanArms": orphan_arms(),
        "presets": PRESETS,
    }