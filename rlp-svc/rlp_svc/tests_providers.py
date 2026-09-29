"""Offline tests for the provider store: validation, atomic writes, credentials.

Split out of `rlp_svc.tests` only to keep one file readable; the same runner
calls both. Everything here is hermetic: the transport is stubbed, and the two
files live in a temporary directory.

    rlp-svc/.venv/bin/python -m rlp_svc.tests
"""
from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path

_failures: list[str] = []


def check(condition: bool, message: str) -> None:
    if not condition:
        _failures.append(message)
        print(f"FAIL {message}")


class _Stub:
    """Stands in for `providers._http`: answers from a queue, records requests."""

    def __init__(self, responses: list[object]) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    def __call__(self, method, url, *, headers, body=None, timeout=20.0):
        self.calls.append({"method": method, "url": url, "headers": headers, "body": body})
        if not self.responses:
            raise AssertionError(f"unexpected request: {method} {url}")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return _Response(item)


class _Response:
    def __init__(self, payload: object) -> None:
        self._payload = payload

    def json(self):
        return self._payload


def with_store(fn):
    """Run ``fn(providers_module, models_path, auth_path)`` hermetically.

    Both stores are redirected to a temporary directory and the transport is
    restored afterwards, so a test here can never touch the developer's real
    credentials or reach the network.
    """
    from . import providers

    with tempfile.TemporaryDirectory() as tmp:
        models = Path(tmp) / "models.json"
        auth = Path(tmp) / "auth.json"
        prev = {k: os.environ.get(k) for k in ("RLP_PI_MODELS", "RLP_PI_AUTH", "RLP_ORCHESTRATION")}
        os.environ["RLP_PI_MODELS"] = str(models)
        os.environ["RLP_PI_AUTH"] = str(auth)
        # No ladder on disk: `list_providers` then reports no ladder arms, which
        # is the honest state for a fresh install and keeps this file hermetic.
        os.environ["RLP_ORCHESTRATION"] = str(Path(tmp) / "absent.json")
        real_http = providers._http
        try:
            return fn(providers, models, auth)
        finally:
            providers._http = real_http
            for key, value in prev.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def test_providers_store() -> None:
    """add / merge / remove / key: validated, backed up, atomic, never leaking."""

    def body(providers, models, auth):
        # --- validation happens before anything is written
        for bad_id in ("", "has space", "semi;colon", "../escape"):
            try:
                providers.add_provider(bad_id, "https://api.example.com/v1", ["m"])
                check(False, f"provider id {bad_id!r} was accepted")
            except ValueError:
                pass
        check(not models.exists(), "a rejected provider wrote nothing")

        try:
            providers.add_provider("good", "api.example.com/v1", ["m"])
            check(False, "a schemeless baseUrl was accepted")
        except ValueError as e:
            check("http" in str(e), f"the baseUrl error names the rule: {e}")
        try:
            providers.add_provider("good", "https://api.example.com/v1", ["ok", ""])
            check(False, "an empty model id was accepted")
        except ValueError:
            pass
        check(not models.exists(), "still nothing written after more bad input")

        # --- the happy path
        result = providers.add_provider(
            "example", "https://api.example.com/v1/", ["model-a"], api_key="sk-secret-value"
        )
        check(result["provider"] == "example" and result["models"] == ["model-a"], f"add_provider result: {result}")
        check(result["keyWritten"] is True, "the key was written")
        doc = json.loads(models.read_text())
        check(doc["providers"]["example"]["baseUrl"] == "https://api.example.com/v1", "the trailing slash is normalised")
        check("sk-secret-value" not in models.read_text(), "the secret is never written to models.json")
        stored = json.loads(auth.read_text())
        check(stored["example"]["key"] == "sk-secret-value", "the key landed in auth.json")
        mode = stat.S_IMODE(auth.stat().st_mode)
        check(mode == 0o600, f"auth.json is 0600, got {oct(mode)}")
        check(result["path"] == str(models) and result["backup"] is None, "a first write has no backup to make")

        # --- a secret never comes back out through the reporting surface
        cards = providers.list_providers()
        check(len(cards) == 1 and cards[0]["credential"] == "key", f"credential state: {cards}")
        check("sk-secret-value" not in json.dumps(cards), "list_providers leaks nothing")
        check(providers.stored_credential("example") == "sk-secret-value", "the in-process accessor still works")

        # --- merging preserves the models already attached
        second = providers.add_provider("example", "https://api.example.com/v1", ["model-b"])
        check(second["models"] == ["model-a", "model-b"], f"models merge: {second['models']}")
        check(second["backup"] is not None and Path(second["backup"]).is_file(), "the second write is backed up")
        replaced = providers.add_provider("example", "https://api.example.com/v1", ["model-c"], replace_models=True)
        check(replaced["models"] == ["model-c"], f"replace_models replaces: {replaced['models']}")

        # --- unrelated keys survive an edit
        doc = json.loads(models.read_text())
        doc["somethingNewer"] = {"keep": True}
        models.write_text(json.dumps(doc))
        providers.add_provider("example", "https://api.example.com/v1", ["model-d"])
        check(json.loads(models.read_text()).get("somethingNewer") == {"keep": True}, "unknown top-level keys are preserved")

        # --- credentials: set, replace, drop
        providers.set_key("example", "sk-second")
        check(providers.credential_state("example") == "key", "set_key replaces the value")
        check(stat.S_IMODE(auth.stat().st_mode) == 0o600, "a replacement write keeps 0600")
        dropped = providers.clear_key("example")
        check(dropped["removed"] is True and providers.credential_state("example") == "none", "clear_key removes it")
        check(providers.clear_key("example")["removed"] is False, "clearing an absent credential is a no-op")

        # --- removal
        providers.set_key("example", "sk-third")
        gone = providers.remove_provider("example", drop_key=True)
        check(gone["keyRemoved"] is True, "drop_key removes the credential too")
        check("example" not in json.loads(models.read_text())["providers"], "the endpoint is gone")
        check("example" not in json.loads(auth.read_text()), "the credential is gone")
        try:
            providers.remove_provider("example")
            check(False, "removing an absent provider was accepted")
        except ValueError as e:
            check("no provider" in str(e), f"removing an absent provider names it: {e}")

        # --- a corrupt store is refused rather than overwritten
        models.write_text("{not json")
        try:
            providers.add_provider("other", "https://api.example.com/v1", ["m"])
            check(False, "a corrupt models.json was overwritten")
        except ValueError as e:
            check("not valid JSON" in str(e), f"the corrupt store is named: {e}")
        check(models.read_text() == "{not json", "the corrupt file was left alone")

    return with_store(body)


def test_providers_probe() -> None:
    """discover/probe report, classify, and never raise."""
    from . import providers as mod

    class _Boom(Exception):
        pass

    def body(providers, models, auth):
        # --- discover: the shapes real endpoints use
        providers._http = _Stub([{"data": [{"id": "b"}, {"id": "a"}, {"id": "a"}]}])
        found = providers.discover_models("https://api.example.com/v1", "k")
        check(found["ok"] and found["models"] == ["a", "b"], f"discover sorts and dedupes: {found}")
        call = providers._http.calls[-1]
        check(call["url"] == "https://api.example.com/v1/models", f"the /models route: {call['url']}")
        check(call["headers"]["Authorization"] == "Bearer k", "the key is sent as a bearer token")

        providers._http = _Stub([["x", "y"]])
        check(providers.discover_models("https://api.example.com/v1")["models"] == ["x", "y"], "a bare list works")
        providers._http = _Stub([{"models": [{"name": "n"}]}])
        check(providers.discover_models("https://api.example.com/v1")["models"] == ["n"], "the {models:[{name}]} shape works")

        # --- discover failures are classified, not raised
        import httpx

        request = httpx.Request("GET", "https://api.example.com/v1/models")
        providers._http = _Stub(
            [httpx.HTTPStatusError("401", request=request, response=httpx.Response(401, request=request))]
        )
        denied = providers.discover_models("https://api.example.com/v1", "bad")
        check(denied["ok"] is False and denied["kind"] == "auth", f"401 -> auth: {denied}")
        check("key" not in denied["error"].lower() or "credential" in denied["fix"], "the fix line names the remedy")

        providers._http = _Stub([httpx.ConnectError("Connection refused")])
        check(providers.discover_models("https://api.example.com/v1")["kind"] == "network", "a refused connection is network")
        providers._http = _Stub([_Boom("weird")])
        check(providers.discover_models("https://api.example.com/v1")["kind"] == "unknown", "an unknown failure still reports")
        check(providers.discover_models("ftp://x")["kind"] == "bad_url", "a non-http URL is caught before the network")

        # --- probe against a configured provider
        providers.add_provider(
            "example", "https://api.example.com/v1", ["model-a"], api_key="sk-live", replace_models=True
        )
        providers._http = _Stub([{"choices": [{"message": {"content": ""}}]}])
        good = providers.probe("example")
        check(good["ok"] and good["model"] == "model-a", f"probe picks the provider's model: {good}")
        check(isinstance(good["latencyMs"], int), "probe reports a latency")
        call = providers._http.calls[-1]
        check(call["url"].endswith("/chat/completions") and call["body"]["max_tokens"] == 1, "probe sends one token")
        check(call["headers"]["Authorization"] == "Bearer sk-live", "probe uses the stored key")

        # a 404 on the model: the extra /models call turns it into an answer
        providers._http = _Stub(
            [
                httpx.HTTPStatusError("404", request=request, response=httpx.Response(404, request=request)),
                {"data": [{"id": "model-z"}]},
            ]
        )
        missing = providers.probe("example")
        check(missing["ok"] is False and missing["availableModels"] == ["model-z"], f"the model list is offered: {missing}")
        check("model-z" in missing["fix"] or "availableModels" in missing["fix"], "the fix points at the list")

        # a provider with no credential fails before any request
        providers.clear_key("example")
        providers._http = _Stub([])
        no_key = providers.probe("example")
        check(no_key["kind"] == "auth" and not providers._http.calls, f"no credential -> no request: {no_key}")
        check(providers.probe("nope")["kind"] == "not_found", "an unknown provider is reported, not raised")

        # probe before writing an endpoint
        providers._http = _Stub([{"choices": [{"message": {"content": ""}}]}])
        inline = providers.probe(None, base_url="https://api.example.com/v1", api_key="k", model="m")
        check(inline["ok"] is True, f"probe accepts an un-written endpoint: {inline}")

    return with_store(body)


def test_providers_surface() -> None:
    """The CLI and the report a human reads."""
    from . import cli, orchestration, providers

    LADDER = {
        "brain": "example/model-a",
        "workers": [
            {
                "id": "pi",
                "harness": "pi",
                "models": [
                    {"model": "example/model-a", "roles": ["code", "review"], "when": "default"},
                    {"model": "ghost/model-g", "roles": ["code"], "when": "no endpoint at all"},
                ],
            }
        ],
        "routing": {"escalateBelow": 0.55, "maxDispatchesPerTurn": 4},
    }

    def body(mod, models, auth):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as tmp:
            ladder = Path(tmp) / "orchestration.json"
            ladder.write_text(json.dumps(LADDER))
            os.environ["RLP_ORCHESTRATION"] = str(ladder)
            try:
                mod.add_provider("example", "https://api.example.com/v1", ["model-a"], api_key="k")
                mod.add_provider("keyless", "https://api.example.com/v1", ["model-b"])

                cards = {c["id"]: c for c in mod.list_providers()}
                check(cards["example"]["runnable"] is True, "a credentialed endpoint is runnable")
                check(cards["keyless"]["runnable"] is False, "a keyless endpoint is not")
                check(all(a["model"].startswith("example/") for a in cards["example"]["ladderArms"]),
                      f"ladder arms are attached to their provider: {cards['example']['ladderArms']}")
                check(cards["keyless"]["ladderArms"] == [] and cards["example"]["modelCount"] == 1,
                      "one endpoint with arms, one without")

                orphans = {o["arm"] for o in mod.orphan_arms()}
                check("ghost/model-g" in orphans, f"an arm with no endpoint is an orphan: {orphans}")
                check("keyless/model-b" not in orphans, "an endpoint with no arm is not an orphan")

                summary = mod.summary()
                check(summary["withoutCredential"] == ["keyless"] and summary["credentialed"] == ["example"],
                      f"summary splits by credential: {summary['withoutCredential']}")
                check(any(p["id"] == "openai" for p in summary["presets"]), "the presets are offered")

                # --- the CLI path the TUI drives
                sink = io.StringIO()
                with contextlib.redirect_stdout(sink):
                    code = cli.main(["provider", "list", "--json"])
                check(code == 0, "provider list exits 0")
                payload = json.loads(sink.getvalue())
                check(payload["ok"] and len(payload["result"]["providers"]) == 2, f"the envelope: {payload.get('ok')}")

                sink = io.StringIO()
                with contextlib.redirect_stdout(sink):
                    code = cli.main(["provider", "add", "cli-added", "https://api.example.com/v1", "m1", "m2", "--key", "sk-cli-secret"])
                check(code == 0, f"provider add exits 0 (got {code})")
                check("cli-added" in json.loads(models.read_text())["providers"], "the CLI wrote the endpoint")
                check("sk-cli-secret" not in models.read_text(), "the CLI kept the secret out of models.json")

                sink = io.StringIO()
                with contextlib.redirect_stdout(sink):
                    code = cli.main(["provider", "add", "bad id", "https://api.example.com/v1"])
                check(code == 1, "a bad provider id exits 1")
                check("not usable" in sink.getvalue(), f"the failure names the rule: {sink.getvalue()}")

                sink = io.StringIO()
                with contextlib.redirect_stdout(sink):
                    code = cli.main(["provider", "key", "cli-added", "--drop"])
                check(code == 0 and mod.credential_state("cli-added") == "none", "provider key --drop clears it")

                # provider with no subcommand is the list view, not a crash
                sink = io.StringIO()
                with contextlib.redirect_stdout(sink):
                    code = cli.main(["provider"])
                check(code == 0 and "endpoints" in sink.getvalue(), f"bare provider lists: {sink.getvalue()[:120]}")

                rendered = sink.getvalue()
                check("● api key" in rendered or "○" in rendered, "the list explains its badges")

                # --- the live checks render on success (a flat payload, not a
                #     {result: …} envelope — the two shapes must not be confused)
                mod._http = _Stub([{"choices": [{"message": {"content": ""}}]}])
                sink = io.StringIO()
                with contextlib.redirect_stdout(sink):
                    code = cli.main(["provider", "probe", "example"])
                check(code == 0, f"provider probe exits 0 on success (got {code})")
                check("answered in" in sink.getvalue() and "model-a" in sink.getvalue(),
                      f"probe renders the round trip: {sink.getvalue()[:160]}")

                mod._http = _Stub([{"data": [{"id": "m1"}, {"id": "m2"}]}])
                sink = io.StringIO()
                with contextlib.redirect_stdout(sink):
                    code = cli.main(["provider", "discover", "example"])
                check(code == 0 and "2 model(s)" in sink.getvalue(),
                      f"discover renders the catalogue: {sink.getvalue()[:160]}")

                mod._http = _Stub([])
                sink = io.StringIO()
                with contextlib.redirect_stdout(sink):
                    code = cli.main(["provider", "probe", "ghost-provider"])
                check(code == 1 and "not_found" in sink.getvalue(), f"a probe failure exits 1: {sink.getvalue()[:160]}")
            finally:
                os.environ.pop("RLP_ORCHESTRATION", None)

    return with_store(body)


def main() -> None:
    """Run only this file's tests (the combined runner lives in rlp_svc.tests)."""
    for test in (test_providers_store, test_providers_probe, test_providers_surface):
        print(f"— {test.__name__}")
        test()
    print()
    if _failures:
        print(f"{len(_failures)} failure(s):")
        for f in _failures:
            print(f"  {f}")
        raise SystemExit(1)
    print("all provider tests passed")


if __name__ == "__main__":
    main()