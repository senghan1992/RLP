"""Resident engine: the decision engine as one long-lived process.

Why this exists: laya is a 421M-parameter checkpoint that takes ~150 s to load
on CPU. Invoked as `rlp plan` it is a fresh process every time, so every
decision paid that load — the README's "33-460 ms typed choice" was really
"plus two and a half minutes, every time", and one orchestration paid it twice
because dispatch re-ran the plan.

This server loads the model once and then answers requests over stdin/stdout as
newline-delimited JSON, so the second decision onwards costs what it should:

    {"id":1,"op":"triage","args":{"request":"fix a typo"}}

and back:

    {"id":1,"ok":true,"result":{"mode":"direct",...}}

One line in, one line out, no framing to get wrong. It is deliberately *not*
the MCP server: `rlp serve` exists so other tools can call this engine, while
this is the private pipe between the agent and its own engine — one fewer
protocol between a keystroke and a decision.

Startup emits `{"event":"warming"}` immediately and `{"event":"ready"}` once
the model is resident, so a client can show the one-time cost honestly instead
of looking hung. A `warm` op does the same on demand.
"""
from __future__ import annotations

import json
import sys
import traceback
from typing import Any, Callable


def _ops() -> dict[str, Callable[[dict], Any]]:
    """Op table. Imported lazily so `--help` and a broken dependency do not
    fight each other."""
    from . import orchestration as orch_mod

    def ladder(_args: dict) -> Any:
        config = orch_mod.load()
        if config is None:
            raise RuntimeError(f"no orchestration config at {orch_mod.config_path()}")
        return {**config, "roster": orch_mod.roster(config), "excluded_workers": orch_mod.excluded(config)}

    def triage(args: dict) -> Any:
        from . import triage as mod

        return mod.triage(args.get("request", ""), args.get("context", ""))

    def decompose(args: dict) -> Any:
        from . import decompose as mod

        return mod.decompose(args.get("request", ""), args.get("context", ""))

    def plan(args: dict) -> Any:
        from . import plan as mod

        return mod.plan(
            args.get("request", ""),
            args.get("context", ""),
            decompose=args.get("decompose", True),
            mode=args.get("mode"),
            because=args.get("because", ""),
        )

    def route(args: dict) -> Any:
        from . import route as mod

        config = orch_mod.load()
        roster = orch_mod.roster(config) if config else []
        if not roster:
            raise RuntimeError("no dispatchable roster")
        gate = (config["routing"].get("escalateBelow") if config else None) or 0.55
        return mod.route(args.get("title", ""), args.get("brief", ""), args.get("domain", "code"), roster, gate)

    def llm_route(args: dict) -> Any:
        from . import route as mod

        config = orch_mod.load()
        roster = orch_mod.roster(config) if config else []
        if not roster:
            raise RuntimeError("no dispatchable roster")
        return mod.llm_route(args.get("title", ""), args.get("brief", ""), args.get("domain", "code"), roster)

    def warm(_args: dict) -> Any:
        """Pay the model load now, so the next real call is milliseconds."""
        from .route import _router

        _router()
        return {"warm": True}

    def config(args: dict) -> Any:
        """Edit the ladder (validated, backed up, atomic). See orchestration.mutate."""
        return orch_mod.mutate(args.get("ops") or [], dry_run=bool(args.get("dryRun") or args.get("dry_run")))

    def replan(args: dict) -> Any:
        """Recursively re-decompose one failed node into a sub-DAG (RLM recursion)."""
        from . import plan as plan_mod

        return plan_mod.replan(args.get("focus", ""), args.get("request", ""), args.get("context", ""))

    def verify(args: dict) -> Any:
        """Independent cross-vendor best-of-N verdict on a node's acceptance."""
        from . import verify as verify_mod

        return verify_mod.verify(
            args.get("title", ""),
            args.get("acceptance", ""),
            args.get("report", ""),
            args.get("evidence", ""),
            args.get("avoidFamily", "") or args.get("avoid_family", ""),
            args.get("samples", 3),
        )

    def memory(args: dict) -> Any:
        """Recent per-project knowledge from earlier runs."""
        from . import memory as mem

        return mem.summary(args.get("cwd") or None, int(args.get("limit", 40)))

    def remember(args: dict) -> Any:
        """Append one knowledge entry for this project."""
        from . import memory as mem

        return mem.append(
            args.get("text", ""),
            args.get("kind", "note"),
            args.get("node", ""),
            args.get("run", ""),
            args.get("tags") or [],
            args.get("cwd") or None,
        )

    return {
        "warm": warm,
        "ladder": ladder,
        "triage": triage,
        "decompose": decompose,
        "plan": plan,
        "route": route,
        "llm_route": llm_route,
        "config": config,
        "replan": replan,
        "verify": verify,
        "memory": memory,
        "remember": remember,
    }


def _emit(payload: dict) -> None:
    sys.stdout.write(f"{json.dumps(payload, ensure_ascii=False)}\n")
    sys.stdout.flush()


def main(*, warm: bool = True) -> int:
    """Serve requests on stdin until EOF. Returns a process exit code."""
    ops = _ops()
    if warm:
        _emit({"event": "warming"})
        try:
            ops["warm"]({})
            _emit({"event": "ready"})
        except Exception as e:  # a failed warm is not fatal: ops degrade to the LLM
            _emit({"event": "ready", "warn": f"warm failed: {str(e)[:200]}"})
    else:
        _emit({"event": "ready"})

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError as e:
            _emit({"id": None, "ok": False, "error": f"bad JSON: {e}"})
            continue

        request_id = request.get("id")
        op = request.get("op", "")
        handler = ops.get(op)
        if handler is None:
            _emit({"id": request_id, "ok": False, "error": f"unknown op {op!r}; have {sorted(ops)}"})
            continue
        try:
            result = handler(request.get("args") or {})
            # Ops that already return an envelope (plan, triage, decompose) carry
            # their own `ok`, so wrapping them again would produce
            # `result.result.mode` and every caller would read the wrong level.
            # The wire shape is therefore always {id, ok, result|error}, whatever
            # the op returns.
            if isinstance(result, dict) and "ok" in result and ("result" in result or "error" in result):
                payload = dict(result)
                payload["id"] = request_id
                _emit(payload)
            else:
                _emit({"id": request_id, "ok": True, "result": result})
        except Exception as e:
            # An op must never take the engine down: the next request still works.
            _emit(
                {
                    "id": request_id,
                    "ok": False,
                    "error": f"{type(e).__name__}: {str(e)[:300]}",
                    "trace": traceback.format_exc(limit=3)[-500:],
                }
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())