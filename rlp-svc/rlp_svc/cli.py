"""rlp-svc CLI — the decision engine as a tool, not just an MCP server.

Same five capabilities the brain calls over MCP (triage, decompose, laya/llm
route, ladder), plus three that only make sense headless: `plan` (the whole
pipeline as a function), `doctor` (is this host runnable?) and `serve` (the MCP
transport the orchestrator uses).

    rlp triage "fix the typo" --json
    rlp plan "add payments + tests, then review independently"
    rlp doctor --warm
    rlp provider list --json          # endpoints, credentials, ladder arms
    rlp provider probe my-provider    # one real round trip; classifies the failure
    rlp serve            # MCP stdio; what config.yaml launches

Contract for scripting: exit 0 on a valid result, 1 when the envelope is
`ok:false`, 2 on a usage error, 3 when `doctor` reports a failure. `--json`
prints the raw envelope and nothing else, so `jq` is never second-guessing a
pretty printer.
"""
from __future__ import annotations

import argparse
import json
import sys
import textwrap
from pathlib import Path
from typing import Any

from . import __version__ as VERSION

EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_UNHEALTHY = 0, 1, 2, 3


def _emit(payload: Any, as_json: bool, human: str = "") -> int:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(human or json.dumps(payload, ensure_ascii=False, indent=2))
    return EXIT_OK if (not isinstance(payload, dict) or payload.get("ok", True)) else EXIT_FAILED


# --- subcommand bodies --------------------------------------------------------


def _cmd_triage(args: argparse.Namespace) -> int:
    from . import triage as mod

    envelope = mod.triage(args.request, args.context)
    if not args.json:
        t = envelope
        human = "\n".join(
            filter(
                None,
                [
                    f"mode:       {t.get('mode')}",
                    f"confidence: {t.get('confidence')}",
                    f"engine:     {t.get('engine')}",
                    f"escalate:   {t.get('escalate')} (below {t.get('escalate_below')})",
                    f"note:       low confidence defaults to {t.get('default_on_escalate')}"
                    if t.get("escalate")
                    else None,
                    f"error:      {t['error']}" if "error" in t else None,
                ],
            )
        )
        return _emit(envelope, False, human)
    return _emit(envelope, True)


def _cmd_decompose(args: argparse.Namespace) -> int:
    from . import decompose as mod

    envelope = mod.decompose(args.request, args.context)
    if args.json:
        return _emit(envelope, True)
    if not envelope.get("ok"):
        return _emit(envelope, False, f"decompose failed: {envelope.get('error')}")
    inner = envelope["result"]
    lines = [f"engine: {inner['engine']}  tasks: {len(inner['tasks'])}", ""]
    for t in inner["tasks"]:
        deps = ",".join(t.get("depends_on") or []) or "-"
        lines.append(f"{t['id']} [{t['domain']}/{t['size']}] {t['title']}  (deps: {deps})")
        lines.append(f"    acceptance: {t['acceptance']}")
    return _emit(envelope, False, "\n".join(lines))


def _route(args: argparse.Namespace, use_llm: bool) -> int:
    from . import orchestration as orch
    from . import route as mod

    try:
        config = orch.load()
    except Exception as e:
        return _emit({"ok": False, "error": str(e)[:300]}, args.json)
    if config is None:
        return _emit(
            {"ok": False, "error": f"no orchestration ladder at {orch.config_path()}"},
            args.json,
            f"no orchestration ladder at {orch.config_path()} — run `rlp doctor`",
        )
    roster = orch.roster(config)
    gate = config["routing"].get("escalateBelow") or 0.55
    result = (
        mod.llm_route(args.title, args.brief, args.domain, roster)
        if use_llm
        else mod.route(args.title, args.brief, args.domain, roster, gate)
    )
    if args.json:
        return _emit(result, True)
    human = f"agent: {result.get('agent')}  engine: {result.get('engine')}  confidence: {result.get('confidence')}"
    if result.get("escalate"):
        human += f"\nescalate: below {result.get('escalate_below')} — treat as advisory"
    if "error" in result:
        human = f"routing failed: {result['error']}"
    return _emit(result, False, human)


def _cmd_route(args: argparse.Namespace) -> int:
    return _route(args, use_llm=False)


def _cmd_llm_route(args: argparse.Namespace) -> int:
    return _route(args, use_llm=True)


def _ladder_envelope() -> dict:
    from . import orchestration as orch

    try:
        config = orch.load()
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}
    if config is None:
        return {"ok": False, "error": f"no orchestration config at {orch.config_path()}"}
    return {
        "ok": True,
        "result": {
            **config,
            "roster": orch.roster(config),
            "roster_all": orch.roster(config, include_unavailable=True),
            "excluded_workers": orch.excluded(config),
            "known_roles": orch.known_roles(config),
            "model_pool": orch.model_pool(config),
            "role_resolution": {r: orch.resolve_role(config, r) for r in orch.known_roles(config)},
        },
    }


def _cmd_ladder(args: argparse.Namespace) -> int:
    envelope = _ladder_envelope()
    if args.json:
        return _emit(envelope, True)
    if not envelope["ok"]:
        return _emit(envelope, False, f"ladder unavailable: {envelope['error']}")
    c = envelope["result"]
    lines = [
        f"ladder: {c['path']}",
        f"brain:  {c['brain'] or '(not chosen yet)'}",
        f"gate:   escalateBelow={c['routing'].get('escalateBelow')} "
        f"mode={c['routing'].get('gate')} signalThreshold={c['routing'].get('signalThreshold')} "
        f"maxDispatchesPerTurn={c['routing'].get('maxDispatchesPerTurn')} "
        f"workerTimeoutMs={c['routing'].get('workerTimeoutMs')} "
        f"crossVendor={c['review'].get('crossVendor')}",
        f"plan:   critique={(c.get('planning') or {}).get('critique')} "
        f"maxRefines={(c.get('planning') or {}).get('maxRefines')} "
        f"recursiveDepth={(c.get('planning') or {}).get('recursiveDepth')} "
        f"artifactPassing={(c.get('planning') or {}).get('artifactPassing')} "
        f"verifySamples={(c.get('planning') or {}).get('verifySamples')}",
        f"rlm:    maxDepth={(c.get('rlm') or {}).get('maxDepth')} "
        f"maxIterations={(c.get('rlm') or {}).get('maxIterations')} "
        f"maxBudget={(c.get('rlm') or {}).get('maxBudget')} "
        f"maxTimeout={(c.get('rlm') or {}).get('maxTimeout')}",
        "",
    ]
    if not c.get("configured"):
        lines[1:1] = [
            "status: NOT CONFIGURED — policy is set, but there are no model arms, so nothing",
            "        can be dispatched and every request is handled inline.",
            "        Fix: run /setup in a session (it reads your endpoint's own model list),",
            "        or `rlp provider add <id> <baseUrl> <model>` then /rlp-config.",
        ]
    for w in c["workers"]:
        flag = "" if w.get("available", True) else "  [UNAVAILABLE]"
        lines.append(f"[{w['id']}]" + (f" on {w['harness']}" if w.get("harness") else "") + flag)
        if not w.get("available", True):
            lines.append(f"           reason: {w.get('availabilityNote', 'marked unavailable')}")
        for i, arm in enumerate(w["models"], 1):
            tag = "DEFAULT" if i == 1 else f"arm {i}"
            lines.append(f"  {tag:<8} {arm['model']}  roles={','.join(arm['roles'])}")
            lines.append(f"           when: {arm['when'][:160]}")
    roles = c.get("role_resolution") or {}
    if roles:
        lines.append("")
        lines.append("role bindings (role -> model the planner uses for that role):")
        for role, res in roles.items():
            if res is None:
                lines.append(f"  {role:<12} (no arm carries this role)")
                continue
            via = "binding" if res.get("binding") else "arm priority"
            where = f" on {res['worker']}" if res.get("worker") else "  [NO DISPATCHABLE WORKER]"
            chain = res.get("chain") or [res["model"]]
            chain_note = f"  chain: {' > '.join(chain)}" if len(chain) > 1 else ""
            lines.append(f"  {role:<12} {res['model']}{where}  ({via}){chain_note}")
    if c.get("excluded_workers"):
        lines.append("")
        lines.append("excluded from routing: " + ", ".join(w["id"] for w in c["excluded_workers"]))
    return _emit(envelope, False, "\n".join(lines))


def _cmd_config(args: argparse.Namespace) -> int:
    """Apply ladder mutations from a JSON array of ops (validated, backed up)."""
    from . import orchestration as orch

    try:
        ops = json.loads(args.ops)
    except json.JSONDecodeError as e:
        return _emit({"ok": False, "error": f"--ops is not valid JSON: {e}"}, args.json,
                     f"config: --ops is not valid JSON: {e}")
    try:
        applied = orch.mutate(ops, dry_run=args.dry_run)
    except Exception as e:
        return _emit({"ok": False, "error": str(e)[:300]}, args.json, f"config rejected: {e}")
    envelope = {"ok": True, "result": {**applied}}
    if args.json:
        return _emit(envelope, True)
    verb = "would apply" if applied["dry_run"] else "applied"
    lines = [f"config {verb} {len(ops)} op(s)", f"ladder: {applied['path']}"]
    if applied.get("backup"):
        lines.append(f"backup: {applied['backup']}")
    lines.append(f"brain:  {applied['ladder']['brain'] or '(not set)'}")
    for w in applied["ladder"]["workers"]:
        flag = "" if w.get("available", True) else " [UNAVAILABLE]"
        lines.append(f"[{w['id']}]{flag} " + ", ".join(a["model"] for a in w["models"]))
    return _emit(envelope, False, "\n".join(lines))


def _cmd_roster(args: argparse.Namespace) -> int:
    envelope = _ladder_envelope()
    if args.json:
        return _emit(envelope, True)
    if not envelope["ok"]:
        return _emit(envelope, False, f"roster unavailable: {envelope['error']}")
    lines = ["router roster (derived from the ladder — the brain routes on these cards)"]
    for card in envelope["result"]["roster"]:
        lines += ["", f"[{card['id']}]", f"  {card['description']}", f"  strengths: {', '.join(card['strengths'])}"]
    if envelope["result"].get("excluded_workers"):
        lines += ["", "excluded (not dispatchable on this host):"]
        for w in envelope["result"]["excluded_workers"]:
            lines.append(f"  {w['id']}: {w['reason']}")
    return _emit(envelope, False, "\n".join(lines))


def _cmd_plan(args: argparse.Namespace) -> int:
    from . import plan as mod

    envelope = mod.plan(
        args.request,
        args.context,
        decompose=not args.no_decompose,
        mode=args.mode,
        because=args.because,
    )
    if args.json:
        return _emit(envelope, True)
    if not envelope.get("ok"):
        return _emit(envelope, False, f"plan failed: {envelope.get('error')}")
    r = envelope["result"]
    head = [f"mode:       {r['mode']}"]
    if r.get("triage"):
        t = r["triage"]
        head.append(f"triage:     engine={t.get('engine')} conf={t.get('confidence')} escalate={t.get('escalate')}")
    if r.get("gate_override"):
        o = r["gate_override"]
        head.append(
            f"gate:       OVERRIDDEN -> {o['mode']}"
            + (f" ({o['because']})" if o["backed"] else " (unbacked: no --because given)")
        )
    if r.get("note"):
        head.append(f"note:       {r['note']}")
    # The reason orchestration was unavailable is the whole answer in that case:
    # `recommended` alone says "not available on this host yet", which names no
    # cause and no fix — the report that sends someone reading source code.
    if r.get("orchestration_unavailable"):
        head.append("")
        head.append("why:        orchestration is unavailable, so direct is the only verdict:")
        for chunk in textwrap.wrap(str(r["orchestration_unavailable"]), 74):
            head.append(f"            {chunk}")
        head.append("")
    if r["mode"] == "direct" or r.get("tasks") is None:
        head.append(f"recommended: {r['recommended']}")
        return _emit(envelope, False, "\n".join(head))
    lines = head + ["", "gate table (report, not a question):", *(f"  {l}" for l in r["gate_table"]), ""]
    lines.append(f"dispatch waves ({r['max_dispatches_per_turn']} per turn max):")
    for i, wave in enumerate(r["waves"], 1):
        lines.append(f"  wave {i}: {', '.join(wave)}")
    if r["advisory_nodes"]:
        lines.append(f"advisory (below gate — re-justify): {', '.join(r['advisory_nodes'])}")
    for violation in r["cross_vendor_violations"]:
        lines.append(f"cross-vendor VIOLATION on {violation['node']}: {violation['arm']} vs {violation['families']}")
    for p in r.get("preflight", []):
        provider = p["arm"].partition("/")[0]
        lines.append(f"preflight: {p['node']} → {p['arm']} has no credential — /login {provider} or edit the ladder")
    for b in r.get("binding_warnings", []):
        models = b.get("models") or [b.get("model")]
        lines.append(f"role binding: {b['role']} → {', '.join(models)} has no dispatchable worker; {b['node']} fell back")
    if r.get("worker_timeout_ms"):
        lines.append(f"worker watchdog: {r['worker_timeout_ms']} ms")
    if r.get("gate_config"):
        g = r["gate_config"]
        lines.append(f"gate: {g.get('gate')} (escalateBelow={g.get('escalate_below')})")
    for w in r.get("excluded_workers", []):
        lines.append(f"excluded worker: {w['id']} ({w['reason']})")
    if r.get("warning"):
        lines.append(f"warning: {r['warning']}")
    lines.append(f"recommended: {r['recommended']}")
    return _emit(envelope, False, "\n".join(lines))


def _cmd_doctor(args: argparse.Namespace) -> int:
    from . import doctor

    report = doctor.run(warm=args.warm)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(doctor.render(report))
    return EXIT_OK if report["ok"] else EXIT_UNHEALTHY


def _cmd_serve(_args: argparse.Namespace) -> int:
    from .server import main

    main()
    return EXIT_OK


def _cmd_replan(args: argparse.Namespace) -> int:
    from . import plan as mod

    envelope = mod.replan(args.focus, args.request, args.context)
    if args.json:
        return _emit(envelope, True)
    if not envelope.get("ok"):
        return _emit(envelope, False, f"replan failed: {envelope.get('error')}")
    inner = envelope["result"]
    lines = [f"engine: {inner.get('engine')}  sub-tasks: {len(inner['tasks'])}  recursiveDepth: {inner.get('recursive_depth')}", ""]
    for t in inner["tasks"]:
        deps = ",".join(t.get("depends_on") or []) or "-"
        lines.append(f"{t['id']} [{t['domain']}/{t['size']}] {t['title']}  (deps: {deps})")
    return _emit(envelope, False, "\n".join(lines))


def _cmd_verify(args: argparse.Namespace) -> int:
    from . import verify as mod

    report = args.report or (Path(args.report_file).read_text() if args.report_file else "")
    evidence = args.evidence or (Path(args.evidence_file).read_text() if args.evidence_file else "")
    envelope = mod.verify(args.title, args.acceptance, report, evidence, args.avoid_family, args.samples)
    if args.json:
        return _emit(envelope, True)
    if not envelope.get("ok"):
        return _emit(envelope, False, f"verify failed: {envelope.get('error')}")
    r = envelope["result"]
    human = (
        f"pass:       {r['pass']}\n"
        f"verifier:   {r['verifier']} (cross-vendor={r['cross_vendor']})\n"
        f"votes:      {r['pass_count']}/{r['samples']}  agreement={r['agreement']}\n"
        f"reason:     {r['reason']}"
    )
    return _emit(envelope, False, human)


def _cmd_provider(args: argparse.Namespace) -> int:
    """model endpoints and credentials: list, add, remove, key, discover, probe.

    The TUI drives this instead of writing models.json/auth.json itself, so the
    validation, the backup and the 0600 on auth.json have one implementation
    and the offline suite covers them.
    """
    from . import providers as mod

    if not hasattr(args, "json"):
        # `rlp-svc provider` with no subcommand reaches here without the
        # subparser's own flag, and a missing attribute is not a reason to fail.
        args.json = False
    if getattr(args, "key_stdin", False):
        # A key on argv is visible in `ps` to every other process on the host.
        # `--key-stdin` lets the TUI hand the secret over a pipe instead, which
        # is the only reason this flag exists.
        args.key = sys.stdin.read().strip() or None
    verb = args.provider_command
    if verb in (None, "show"):
        verb = "list"
    try:
        if verb == "list":
            data = mod.summary()
            envelope = {"ok": True, "result": data}
        elif verb == "add":
            data = mod.add_provider(
                args.id,
                args.base_url,
                args.model or [],
                api_key=args.key,
                name=args.name,
                api=args.api,
                replace_models=args.replace_models,
            )
            envelope = {"ok": True, "result": data}
        elif verb == "remove":
            envelope = {"ok": True, "result": mod.remove_provider(args.id, drop_key=args.drop_key)}
        elif verb == "key":
            if args.key:
                envelope = {"ok": True, "result": mod.set_key(args.id, args.key)}
            elif args.drop:
                envelope = {"ok": True, "result": mod.clear_key(args.id)}
            else:
                return _emit(
                    {"ok": False, "error": "provider key needs a key, or --drop to remove it"},
                    args.json,
                    "usage: rlp-svc provider key <id> <key> | provider key <id> --drop",
                )
        elif verb == "discover":
            base_url = args.base_url or next(
                (p["baseUrl"] for p in mod.list_providers() if p["id"] == args.id), ""
            )
            if not base_url:
                return _emit(
                    {"ok": False, "error": f"no endpoint for {args.id!r}"},
                    args.json,
                    f"no endpoint configured for {args.id!r} — pass one: provider discover --base-url URL",
                )
            key = args.key
            if key is None and args.id:
                key = mod.stored_credential(args.id)
            envelope = mod.discover_models(base_url, key)
        elif verb == "probe":
            envelope = mod.probe(args.id, base_url=args.base_url, api_key=args.key, model=args.model)
        else:  # pragma: no cover - argparse restricts the choices
            raise ValueError(f"unknown provider subcommand {verb!r}")
    except ValueError as e:
        return _emit({"ok": False, "error": str(e)}, args.json, f"provider: {e}")

    if args.json:
        return _emit(envelope, True)
    if not envelope.get("ok"):
        lines = [
            f"✗ {envelope.get('error', 'failed')}",
            f"  kind  {envelope.get('kind', 'unknown')}",
            f"  fix   {envelope.get('fix', '')}",
        ]
        for model in (envelope.get("availableModels") or [])[:12]:
            lines.append(f"        {model}")
        return _emit(envelope, False, "\n".join(lines))
    # `list`/`add`/`remove`/`key` wrap their payload in `result`; the two live
    # checks return the payload itself, because that is the envelope.
    payload = envelope["result"] if isinstance(envelope.get("result"), dict) else envelope
    return _emit(envelope, False, _render_provider(payload, verb))


def _render_provider(data: dict, verb: str) -> str:
    if verb == "probe":
        return (
            f"✓ {data.get('provider') or data.get('baseUrl')} answered in {data.get('latency')}\n"
            f"  model  {data.get('model')}\n  url    {data.get('baseUrl')}"
        )
    if verb == "discover":
        models = data.get("models") or []
        head = [f"{data.get('count')} model(s) at {data.get('baseUrl')}", ""]
        head += [f"  {m}" for m in models[:40]]
        if len(models) > 40:
            head.append(f"  …and {len(models) - 40} more")
        return "\n".join(head)
    if verb in ("add", "remove", "key"):
        lines = [f"{verb}: {data.get('provider')}"]
        for key in ("baseUrl", "credential", "models", "backup", "path"):
            if data.get(key):
                value = data[key]
                lines.append(f"  {key:<10} {', '.join(value) if isinstance(value, list) else value}")
        if verb == "key":
            lines = [f"credential stored for {data.get('provider')} ({data.get('credential')})", f"  backup  {data.get('backup') or 'none'}"]
        return "\n".join(lines)

    cards = data.get("providers") or []
    lines = [f"endpoints · {data.get('modelsPath')}", ""]
    for card in cards:
        badge = {"oauth": "◆", "key": "●", "none": "○"}[card["credential"]]
        arms = ", ".join(a["model"] for a in card.get("ladderArms") or [])
        lines.append(f"{badge} {card['id']}  —  {card['baseUrl']}  ({card['modelCount']} model(s), {card['credentialLabel']})")
        if arms:
            lines.append(f"   ladder: {arms}")
        else:
            lines.append("   ladder: not an arm — add it with /rlp-config or `rlp config`")
    if not cards:
        lines.append("  no endpoints configured — /provider add <id> <baseUrl> <modelId>")
    orphans = data.get("orphanArms") or []
    if orphans:
        lines += ["", "arms that cannot run (visible but unusable):"]
        for o in orphans:
            lines.append(f"  {o['arm']} — {o['why']}")
    lines += ["", "  ● api key   ◆ oauth   ○ no credential"]
    return "\n".join(lines)


def _cmd_memory(args: argparse.Namespace) -> int:
    from . import memory as mem

    data = mem.summary(args.cwd or None, args.limit)
    if args.json:
        print(json.dumps({"ok": True, "result": data}, ensure_ascii=False, indent=2))
        return EXIT_OK
    lines = [f"memory: {data['path']}  ({data['count']} entries)"]
    if data["brief"]:
        lines += ["", data["brief"]]
    print("\n".join(lines))
    return EXIT_OK


def _cmd_remember(args: argparse.Namespace) -> int:
    from . import memory as mem

    try:
        entry = mem.append(
            args.text,
            args.kind,
            args.node,
            args.run,
            [t.strip() for t in args.tags.split(",") if t.strip()] if args.tags else [],
            args.cwd or None,
        )
    except Exception as e:
        print(f"remember failed: {str(e)[:200]}", file=sys.stderr)
        return EXIT_FAILED
    if args.json:
        print(json.dumps({"ok": True, "result": entry}, ensure_ascii=False, indent=2))
        return EXIT_OK
    print(f"remembered [{entry['kind']}] {entry['text'][:140]}")
    return EXIT_OK


def _cmd_engine(args: argparse.Namespace) -> int:
    """The resident engine: one process, model loaded once, JSON lines on stdio."""
    from . import engine

    return engine.main(warm=not args.no_warm)


def _build_identity() -> dict:
    """What version of RLP this is, and what it was built from.

    The single most useful datum in a bug report is the one nobody has: *which*
    RLP. A release version alone does not answer it — the same version can be a
    tag, a tag plus local commits, or a dirty working tree — so this reports the
    checkout's own `git describe` alongside it, plus the harness build and the
    engine's optional dependencies. `rlp version` printed only `rlp-svc 0.2.0`
    before, which named the engine and not the tool.
    """
    import importlib.util
    import platform
    import subprocess

    from . import paths

    repo = Path(__file__).resolve().parents[2]

    def git(*args: str) -> str:
        try:
            out = subprocess.run(
                ["git", "-C", str(repo), *args],
                capture_output=True, text=True, timeout=10, check=False,
            )
            return out.stdout.strip() if out.returncode == 0 else ""
        except Exception:
            return ""

    # `--always` so a shallow clone with no tags still answers with a sha, and
    # `--dirty` so a modified checkout says so rather than claiming the tag.
    describe = git("describe", "--tags", "--always", "--dirty") or None
    commit = git("rev-parse", "HEAD") or None
    harness = None
    bundle = repo / "fork" / "pi" / "packages" / "coding-agent" / "dist" / "bundle" / "cli.js"
    if bundle.is_file():
        pkg = repo / "fork" / "pi" / "packages" / "coding-agent" / "package.json"
        try:
            harness = json.loads(pkg.read_text()).get("version")
        except Exception:
            harness = "built (version unreadable)"
    return {
        "rlp": VERSION,
        "checkout": {"path": str(repo), "describe": describe, "commit": commit},
        "harness": harness,
        "engine_deps": {
            name: importlib.util.find_spec(name) is not None
            for name in ("laya", "rlm", "mcp", "httpx")
        },
        "agent_dir": str(paths.agent_dir()),
        "python": platform.python_version(),
        "platform": f"{platform.system().lower()}-{platform.machine()}",
    }


def _cmd_progress(args: argparse.Namespace) -> int:
    from . import onboarding

    report = onboarding.progress()
    # Exit 0 either way: "you are 3 of 7 through setting up" is an answer, not a
    # failure. `doctor` owns the question of whether something is broken.
    return _emit({"ok": True, "result": report}, args.json, onboarding.render(report))


def _cmd_version(args: argparse.Namespace) -> int:
    identity = _build_identity()
    if getattr(args, "json", False):
        return _emit({"ok": True, "result": identity}, True)
    missing = [n for n, ok in identity["engine_deps"].items() if not ok]
    lines = [
        f"rlp {identity['rlp']}"
        + (f"  ({identity['checkout']['describe']})" if identity["checkout"]["describe"] else ""),
        f"  checkout  {identity['checkout']['path']}",
        f"  harness   {identity['harness'] or 'not built — sh scripts/install.sh'}",
        f"  engine    laya/rlm/mcp/httpx "
        + ("all importable" if not missing else f"MISSING {', '.join(missing)} — sh scripts/install.sh"),
        f"  agent dir {identity['agent_dir']}",
        f"  host      python {identity['python']} on {identity['platform']}",
    ]
    return _emit({"ok": True, "result": identity}, False, "\n".join(lines))


# --- parser -------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="rlp-svc",
        description="RLP decision engine: triage gate, RLM decomposition, laya routing, ladder, plan, doctor.",
    )
    p.add_argument("--json", action="store_true", help="print the raw JSON envelope and nothing else")
    sub = p.add_subparsers(dest="command", required=True)

    def add_request(name: str, help_: str) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("request", help="the request, verbatim")
        sp.add_argument("--context", default="", help="repo context the caller already has")
        sp.add_argument("--json", action="store_true", help="raw JSON output")
        return sp

    add_request("triage", "decide direct vs orchestrate (one laya forward pass)")

    add_request("decompose", "decompose into a validated DAG of 2-12 nodes")

    sp = sub.add_parser("replan", help="recursively re-decompose one failed node into a sub-DAG")
    sp.add_argument("focus", help="the failed node's brief")
    sp.add_argument("--request", default="", help="the original request, for context")
    sp.add_argument("--context", default="")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("verify", help="independent cross-vendor best-of-N verdict on a node's acceptance")
    sp.add_argument("--title", default="")
    sp.add_argument("--acceptance", required=True, help="the pass/fail contract sentence")
    sp.add_argument("--report", default="", help="the worker's report text")
    sp.add_argument("--report-file", default="", help="read the report from this file")
    sp.add_argument("--evidence", default="", help="result text / diff")
    sp.add_argument("--evidence-file", default="", help="read the evidence from this file")
    sp.add_argument("--avoid-family", default="", help="the implementer's vendor family, to go cross-vendor")
    sp.add_argument("--samples", type=int, default=3, help="best-of-N samples (1-7)")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("memory", help="recent per-project knowledge from earlier RLP runs")
    sp.add_argument("--cwd", default="", help="project directory (default: the current one)")
    sp.add_argument("--limit", type=int, default=40)
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("remember", help="append a knowledge entry for this project")
    sp.add_argument("text", help="the knowledge, one or two sentences")
    sp.add_argument("--kind", default="note", choices=["note", "decision", "pitfall", "artifact", "blocked"])
    sp.add_argument("--node", default="")
    sp.add_argument("--run", default="")
    sp.add_argument("--tags", default="")
    sp.add_argument("--cwd", default="")
    sp.add_argument("--json", action="store_true")

    def add_route(name: str, help_: str) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("--title", required=True)
        sp.add_argument("--brief", default="")
        sp.add_argument("--domain", default="code")
        sp.add_argument("--json", action="store_true")
        return sp

    add_route("route", "route one subtask with the laya decision model")
    add_route("llm-route", "route one subtask with the transparent LLM fallback")

    sp = sub.add_parser("provider", help="model endpoints and credentials: list, add, remove, key, discover, probe")
    psub = sp.add_subparsers(dest="provider_command")
    psub.add_parser("list", help="every endpoint, its credential state and its ladder arms").add_argument("--json", action="store_true")
    psub.add_parser("show", help="alias for list").add_argument("--json", action="store_true")

    pa = psub.add_parser("add", help="attach an OpenAI-compatible endpoint (validate, back up, write atomically)")
    pa.add_argument("id", help="provider id (letters, digits, . _ -)")
    pa.add_argument("base_url", help="API root, e.g. https://api.example.com/v1")
    pa.add_argument("model", nargs="*", help="model id(s) to attach")
    pa.add_argument("--key", default=None, help="API key; written to auth.json (0600), never printed")
    pa.add_argument("--key-stdin", action="store_true", help="read the API key from stdin (keeps it out of ps)")
    pa.add_argument("--name", default=None, help="display name")
    pa.add_argument("--api", default="openai-completions", help="api shape (default: openai-completions)")
    pa.add_argument("--replace-models", action="store_true", help="replace the model list instead of merging into it")
    pa.add_argument("--json", action="store_true")

    pr = psub.add_parser("remove", help="detach an endpoint")
    pr.add_argument("id")
    pr.add_argument("--drop-key", action="store_true", help="also remove its credential from auth.json")
    pr.add_argument("--json", action="store_true")

    pk = psub.add_parser("key", help="set, replace, or (--drop) remove a provider's credential")
    pk.add_argument("id")
    pk.add_argument("key", nargs="?", help="the credential; omit with --drop or --key-stdin")
    pk.add_argument("--key-stdin", action="store_true", help="read the credential from stdin (keeps it out of ps)")
    pk.add_argument("--drop", action="store_true", help="remove the stored credential")
    pk.add_argument("--json", action="store_true")

    pd = psub.add_parser("discover", help="ask an endpoint which models it serves (GET /models)")
    pd.add_argument("id", nargs="?", default="", help="a configured provider to read the URL and key from")
    pd.add_argument("--base-url", default="", help="probe this URL instead")
    pd.add_argument("--key", default=None, help="credential for --base-url")
    pd.add_argument("--key-stdin", action="store_true", help="read the credential from stdin")
    pd.add_argument("--json", action="store_true")

    pp = psub.add_parser("probe", help="one real completion round trip: does this endpoint answer?")
    pp.add_argument("id", nargs="?", default=None, help="a configured provider")
    pp.add_argument("--base-url", default=None, help="check this URL before writing it")
    pp.add_argument("--key", default=None, help="credential for --base-url")
    pp.add_argument("--key-stdin", action="store_true", help="read the credential from stdin")
    pp.add_argument("--model", default=None, help="model to test")
    pp.add_argument("--json", action="store_true")

    sub.add_parser("ladder", help="print the resolved orchestration ladder").add_argument("--json", action="store_true")
    sub.add_parser("roster", help="print the router roster derived from the ladder").add_argument("--json", action="store_true")

    sp = sub.add_parser("config", help="edit the ladder: validate, back up, write atomically")
    sp.add_argument("ops", help='a JSON array of ops, e.g. \'[{"op":"set_brain","model":"p/m"}]\'')
    sp.add_argument("--dry-run", action="store_true", help="validate and report, write nothing")
    sp.add_argument("--json", action="store_true")

    sp = add_request("plan", "the whole pipeline headless: triage -> DAG -> routes -> waves")
    sp.add_argument("--no-decompose", action="store_true", help="stop after the gate")
    sp.add_argument(
        "--mode",
        choices=["auto", "direct", "orchestrate"],
        default="auto",
        help="override the gate; 'direct' skips the laya pass entirely",
    )
    sp.add_argument(
        "--because",
        default="",
        help="the >= 2 independent deliverables that justify an override (kept in the plan)",
    )

    sp = sub.add_parser("doctor", help="diagnose this host: deps, creds, ladder, checkpoint, wiring")
    sp.add_argument("--warm", action="store_true", help="also pay one real laya load (~170 s cold)")
    sp.add_argument("--json", action="store_true")

    sub.add_parser("serve", help="run the MCP stdio server (the decision engine, as MCP tools)")
    sp = sub.add_parser(
        "engine",
        help="run the resident engine (JSON lines on stdio; loads laya once)",
    )
    sp.add_argument(
        "--no-warm",
        action="store_true",
        help="do not pay the model load up front",
    )
    sp = sub.add_parser(
        "progress", help="how far this host is from install to a first green worker"
    )
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("version", help="RLP's version, and what this checkout was built from")
    sp.add_argument("--json", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handlers = {
        "triage": _cmd_triage,
        "decompose": _cmd_decompose,
        "route": _cmd_route,
        "llm-route": _cmd_llm_route,
        "ladder": _cmd_ladder,
        "roster": _cmd_roster,
        "provider": _cmd_provider,
        "config": _cmd_config,
        "replan": _cmd_replan,
        "verify": _cmd_verify,
        "memory": _cmd_memory,
        "remember": _cmd_remember,
        "plan": _cmd_plan,
        "doctor": _cmd_doctor,
        "serve": _cmd_serve,
        "engine": _cmd_engine,
        "progress": _cmd_progress,
        "version": _cmd_version,
    }
    try:
        return handlers[args.command](args)
    except KeyboardInterrupt:
        return 130
    except SystemExit:
        raise
    except Exception as e:  # a CLI must print the failure, not a traceback wall
        print(f"rlp-svc {args.command}: {type(e).__name__}: {e}", file=sys.stderr)
        return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
