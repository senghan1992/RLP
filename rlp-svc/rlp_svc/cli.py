"""rlp-svc CLI — the decision engine as a tool, not just an MCP server.

Same five capabilities the brain calls over MCP (triage, decompose, laya/llm
route, ladder), plus three that only make sense headless: `plan` (the whole
pipeline as a function), `doctor` (is this host runnable?) and `serve` (the MCP
transport the orchestrator uses).

    rlp triage "fix the typo" --json
    rlp plan "add payments + tests, then review independently"
    rlp doctor --warm
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
from pathlib import Path
from typing import Any

VERSION = "0.2.0"

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
        f"brain:  {c['brain']}",
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
    lines.append(f"brain:  {applied['ladder']['brain']}")
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

    report = doctor.run(warm=args.warm, host=not args.no_host)
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


def _cmd_version(_args: argparse.Namespace) -> int:
    import rlp_svc

    print(f"rlp-svc {VERSION} (package {getattr(rlp_svc, '__version__', 'n/a')})")
    return EXIT_OK


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
    sp.add_argument("--no-host", action="store_true", help="skip omnigent wiring checks")
    sp.add_argument("--json", action="store_true")

    sub.add_parser("serve", help="run the MCP stdio server the orchestrator launches")
    sp = sub.add_parser(
        "engine",
        help="run the resident engine (JSON lines on stdio; loads laya once)",
    )
    sp.add_argument(
        "--no-warm",
        action="store_true",
        help="do not pay the model load up front",
    )
    sub.add_parser("version", help="print the version")
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
        "config": _cmd_config,
        "replan": _cmd_replan,
        "verify": _cmd_verify,
        "memory": _cmd_memory,
        "remember": _cmd_remember,
        "plan": _cmd_plan,
        "doctor": _cmd_doctor,
        "serve": _cmd_serve,
        "engine": _cmd_engine,
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
