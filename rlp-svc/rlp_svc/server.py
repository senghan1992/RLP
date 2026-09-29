"""MCP stdio server: rlp_triage, rlm_decompose, laya_route, llm_route, rlp_orchestration.

The route tools take an optional roster; when it is omitted the roster is
derived from the installed orchestration ladder, so the router and the brain's
`<rlp_orchestration>` prompt section read the same configuration.
"""
from __future__ import annotations

import json

from mcp.server.fastmcp import FastMCP

from . import decompose as decompose_mod
from . import orchestration as orch_mod
from . import route as route_mod
from . import triage as triage_mod

mcp = FastMCP("rlp-svc")

_DEFAULT_ESCALATE_BELOW = 0.55


def _ok(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _roster_from_arg(raw: str) -> list[dict] | None:
    """Caller-supplied roster, or None to fall back to the configured ladder."""
    return json.loads(raw) if raw.strip() else None


def _resolve_roster(raw: str) -> tuple[list[dict], str | None]:
    """Effective roster plus the reason a fallback happened, if any."""
    supplied = _roster_from_arg(raw)
    if supplied:
        return supplied, None
    try:
        config = orch_mod.load()
    except Exception as e:
        return [], f"orchestration config unusable: {str(e)[:200]}"
    if config is None:
        return [], f"no orchestration config at {orch_mod.config_path()}"
    return orch_mod.roster(config), None


def _escalate_below() -> float:
    """Escalate threshold from the ladder, else the built-in default."""
    try:
        config = orch_mod.load()
    except Exception:
        return _DEFAULT_ESCALATE_BELOW
    if config is None:
        return _DEFAULT_ESCALATE_BELOW
    threshold = config["routing"].get("escalateBelow")
    return _DEFAULT_ESCALATE_BELOW if threshold is None else float(threshold)


@mcp.tool()
def rlm_decompose(request: str, context: str = "") -> str:
    """Decompose a request into a flat DAG of independent subtasks.

    Returns a JSON envelope: {"ok": true, "result": {"engine": ..., "tasks": [...]}}
    where each task has id, title, brief, acceptance, depends_on, domain, size,
    topologically ordered. Or {"ok": false, "error": ...} on failure.
    """
    return _ok(decompose_mod.decompose(request, context))


@mcp.tool()
def rlp_triage(request: str, context: str = "") -> str:
    """Decide whether a request is simple (mode "direct") or needs the full
    orchestration pipeline (mode "orchestrate").

    The laya decision model in one forward pass; LLM fallback on any error. Call
    this BEFORE rlm_decompose on every new user request.
    {"mode":"direct"} -> handle it yourself inline: no DAG, no workers, no
    worktrees, no gate table. {"mode":"orchestrate"} -> run the pipeline.
    `escalate:true` means low confidence and defaults to "direct"; override to
    "orchestrate" only when you can name >= 2 independent deliverables.
    Returns {"ok": true, "result": {"mode", "confidence", "engine", "escalate", ...}}.
    """
    return _ok(triage_mod.triage(request, context))

@mcp.tool()
def rlp_orchestration() -> str:
    """The resolved RLP orchestration ladder (brain + worker model arms + routing).

    Reads $RLP_ORCHESTRATION or ~/.pi/agent/orchestration.json — the same file
    the harness renders into the brain's system prompt. Returns a JSON envelope
    with the ladder, or {"ok": false, "error": ...} when nothing is installed.
    """
    try:
        config = orch_mod.load()
    except Exception as e:
        return _ok({"ok": False, "error": str(e)[:300]})
    if config is None:
        return _ok({"ok": False, "error": f"no orchestration config at {orch_mod.config_path()}"})
    return _ok(
        {
            "ok": True,
            "result": {
                **config,
                "roster": orch_mod.roster(config),
                "excluded_workers": orch_mod.excluded(config),
            },
        }
    )


@mcp.tool()
def laya_route(title: str, brief: str, domain: str, roster: str = "") -> str:
    """Route a subtask to the best worker agent using the laya decision model.

    roster: JSON array of {"id", "description" (<=90 words), "strengths"
    (<=5 phrases)}. Omit it to use the configured orchestration ladder — workers
    marked "available": false in the ladder are excluded, so a dispatch is
    never planned onto an arm the host cannot run.
    Returns a JSON envelope with agent, confidence, engine ("laya", or "llm" on
    transparent downgrade), and escalate.
    """
    roster_data, fallback_reason = _resolve_roster(roster)
    if not roster_data:
        return _ok({"ok": False, "error": fallback_reason or "empty roster"})
    result = route_mod.route(title, brief, domain, roster_data, _escalate_below())
    if fallback_reason:
        result["roster_fallback"] = fallback_reason
    return _ok(result)


@mcp.tool()
def llm_route(title: str, brief: str, domain: str, roster: str = "") -> str:
    """Route via a one-turn LLM call (explicit fallback; no laya model needed).

    roster: same shape as laya_route; omit it to use the configured ladder.
    Returns a JSON envelope with agent, confidence, engine ("llm"), reason.
    """
    roster_data, fallback_reason = _resolve_roster(roster)
    if not roster_data:
        return _ok({"ok": False, "error": fallback_reason or "empty roster"})
    try:
        result = route_mod.llm_route(title, brief, domain, roster_data)
    except Exception as e:
        return _ok({"ok": False, "error": f"llm_route: {str(e)[:200]}"})
    if fallback_reason:
        result["roster_fallback"] = fallback_reason
    return _ok(result)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()