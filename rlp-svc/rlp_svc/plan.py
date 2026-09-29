"""rlp plan — the orchestration decision, headless.

The brain in `agent/rlp/config.yaml` runs the pipeline in prose: TRIAGE ->
DECOMPOSE -> ROUTE -> DISPATCH waves -> COLLECT -> SYNTHESIZE. This module is
the same pipeline as a pure function: request in, an executable plan out, no
session, no MCP transport, no worker processes.

That makes RLP usable as a *library* by anything that has a request and wants
an orchestration decision -- a CI job, a bot, another orchestrator, a test.

Three properties are load-bearing and are what the prose cannot guarantee:

1. **The gate is real.** A request that does not earn fan-out comes back as
   `{"mode": "direct"}` with zero downstream calls. `plan()` on a one-line fix
   costs one forward pass and nothing else -- the whole point of the tool.
2. **Routing and arm selection read the installed ladder**, never a literal
   model id. `plan()` cannot invent a model that is not in the config the
   harness also renders.
3. **Cross-vendor review is checked, not hoped for.** A `review` node routed
   onto the same vendor family as the code it reviews is reported as a
   violation; the planner then re-picks its arm on a different family when the
   ladder allows, and says so.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from . import decompose as decompose_mod
from . import orchestration as orch
from . import route as route_mod
from . import triage as triage_mod

DEFAULT_ESCALATE_BELOW = 0.55

#: DAG domain -> (model arm role, dispatch purpose). Mirrors the brain's
#: "args.purpose = implement|review|explore|search" rule.
DOMAIN_INTENT: dict[str, tuple[str, str]] = {
    "code": ("code", "implement"),
    "integration": ("code", "implement"),
    "review": ("review", "review"),
    "research": ("research", "explore"),
    "docs": ("docs", "explore"),
}

#: A code node whose text is about a defect is a *debug* node, not a plain
#: implement one: the ladder reserves its deep/slow arms for exactly that.
_DEBUG_CUES = ("debug", "bug", "regression", "stack trace", "traceback", "root cause", "investigate")

#: Vendor families for the cross-vendor rule. Two nodes on the same family do
#: not review each other, however different the model. Informational: `family()`
#: derives from the provider prefix, so a provider added to the ladder still
#: gets a correct family without editing this set.
FAMILIES = {"agnes", "qwen-token-plan", "anthropic"}


def family(model_ref: str) -> str:
    """Vendor family of a `provider/model` ref."""
    return model_ref.partition("/")[0]


def credential_state(provider: str) -> str:
    """Does this host hold a credential for `provider`? present|missing|unknown.

    The dispatch-plane analogue of omnigent's `sys_session_get_info` readiness
    preflight: an arm whose provider has no credential cannot run, so the
    planner should not spend a dispatch on it. `unknown` (no auth file, or an
    unreadable one) is deliberately permissive — a missing file is not proof a
    provider is dead, and a preflight must never be the thing that blocks a run.
    Set `RLP_SKIP_CREDENTIAL_PREFLIGHT=1` to disable the check entirely.
    """
    if os.environ.get("RLP_SKIP_CREDENTIAL_PREFLIGHT"):
        return "unknown"
    try:
        from . import llm

        auth = json.loads(Path(llm._auth_path()).read_text())
    except Exception:
        return "unknown"
    if not isinstance(auth, dict):
        return "unknown"
    entry = auth.get(provider)
    if isinstance(entry, dict) and (entry.get("key") or entry.get("access")):
        return "present"
    return "present" if provider in auth else "missing"


def intent(domain: str, title: str = "", brief: str = "") -> tuple[str, str]:
    """(arm role, dispatch purpose) for a DAG node.

    Domain decides the purpose; the title/brief can refine the *role* so a code
    node that is really debugging lands on the ladder's debug arm instead of the
    default one. A review or explore domain is never refined — its role is the
    contract.
    """
    if domain == "review":
        return "review", "review"
    role, purpose = DOMAIN_INTENT.get(domain, DOMAIN_INTENT["code"])
    if role == "code":
        text = f"{title} {brief}".lower()
        if any(cue in text for cue in _DEBUG_CUES):
            return "debug", purpose
    return role, purpose


def _load_ladder() -> dict:
    """The installed ladder. Planning without one would mean inventing models."""
    try:
        config = orch.load()
    except Exception as e:
        raise ValueError(f"orchestration ladder is invalid: {str(e)[:300]}") from None
    if config is None:
        raise ValueError(
            f"no orchestration ladder at {orch.config_path()} — run `rlp doctor` "
            "or set RLP_ORCHESTRATION"
        )
    return config


def waves(tasks: list[dict]) -> list[list[str]]:
    """Group a validated, topologically ordered DAG into dispatch waves.

    Wave k holds every node whose dependencies all sit in waves < k, so one
    wave is exactly one `sys_session_send` batch: the maximum parallelism the
    dependency graph allows, and no serialization of independent work.
    """
    done: set[str] = set()
    pending = list(tasks)
    result: list[list[str]] = []
    while pending:
        ready = [t["id"] for t in pending if set(t.get("depends_on") or []) <= done]
        if not ready:  # unreachable for a validated DAG; never loop forever
            raise ValueError("cycle detected while computing dispatch waves")
        result.append(ready)
        done.update(ready)
        pending = [t for t in pending if t["id"] not in done]
    return result


def pick_arm(
    worker: dict,
    role: str,
    avoid_families: set[str] | None = None,
    credential: Any = None,
) -> tuple[dict, str]:
    """Choose one model arm for a worker, honouring role, then vendor avoidance.

    Arms are priority-ordered: the first match is the default, i.e. where the
    bulk of the spend goes. `avoid_families` only ever demotes an arm that
    would otherwise match, and only if a same-role arm survives on another
    family; the rationale string says which rule fired so a reader never has to
    guess why a node is not on the ladder's first arm. `credential`, when
    given, is a `provider -> present|missing|unknown` callable: arms whose
    provider is known to have no credential are skipped in favour of a usable
    one, because planning a dispatch onto a dead arm wastes the whole node.
    """
    arms = worker["models"]
    matching = [a for a in arms if role in a["roles"]] or arms
    if credential is not None:
        usable = [a for a in matching if credential(family(a["model"])) != "missing"]
        if usable:
            matching = usable
    if avoid_families:
        for a in matching:
            if family(a["model"]) not in avoid_families:
                return a, "re-picked to keep the review on a different vendor family"
    arm = matching[0]
    rationale = f"first arm carrying the {role!r} role (priority order = spend order)"
    if role not in arm["roles"]:
        rationale = f"no arm declares the {role!r} role; using the worker default"
    return arm, rationale


def _implemented_families(task: dict, by_id: dict[str, dict], routes: dict[str, dict]) -> set[str]:
    """Vendor families of the code this node depends on (its reviewers' ban list)."""
    fams: set[str] = set()
    for dep in task.get("depends_on") or []:
        dep_task = by_id.get(dep)
        if not dep_task or dep_task.get("domain") == "review":
            continue
        arm = routes.get(dep, {}).get("arm")
        if arm:
            fams.add(family(arm))
    return fams


def route_nodes(config: dict, tasks: list[dict]) -> tuple[dict[str, dict], list[dict], list[dict]]:
    """Route every DAG node to a worker and a model arm.

    Returns (routes keyed by task id, cross-vendor violations, binding warnings).
    A violation is a review node that ends up on the same vendor family as the
    code it reviews *and* has no alternative left on that worker -- reported,
    never silently accepted. A binding warning is a `roles.<role>` binding whose
    model has no dispatchable worker: the node falls back to arm priority, but
    the mismatch is named rather than hidden.

    Role bindings win over arm priority: `roles.<role>` is the operator saying
    "this role runs on this model", so it is honoured even when the priority
    order would have picked otherwise (and even when it breaks cross-vendor,
    which is then reported as a violation).
    """
    roster = orch.roster(config)
    gate = config["routing"].get("escalateBelow") or DEFAULT_ESCALATE_BELOW
    dispatchable = orch.workers(config)
    workers = {w["id"]: w for w in dispatchable}
    by_id = {t["id"]: t for t in tasks}
    cross_vendor = config["review"].get("crossVendor", False)
    routes: dict[str, dict] = {}
    violations: list[dict] = []
    binding_warnings: list[dict] = []

    for task in tasks:
        role, purpose = intent(task.get("domain", "code"), task.get("title", ""), task.get("brief", ""))
        decision = route_mod.route(task["title"], task.get("brief", ""), task.get("domain", "code"), roster, gate)
        agent = decision.get("agent")
        if agent not in workers:
            # Advisory-only path: the router named something off-roster.
            agent = roster[0]["id"] if roster else "pi"
            decision["agent_corrected_from"] = decision.get("agent")
        worker = workers[agent]
        avoid = _implemented_families(task, by_id, routes) if (cross_vendor and role == "review") else None

        chain = orch.role_chain(config, role)
        chosen_binding: str | None = None
        binding_unavailable: list[str] = []
        if chain:
            carrier = None
            for index, model in enumerate(chain):
                found = next((w for w in dispatchable if any(a["model"] == model for a in w["models"])), None)
                if found is not None:
                    carrier, chosen_binding = found, model
                    worker = found
                    agent = found["id"]
                    arm = next(a for a in found["models"] if a["model"] == model)
                    suffix = f" (fallback {index + 1}/{len(chain)})" if index else ""
                    rationale = f"role binding: {role!r} -> {model} (on worker {found['id']!r}){suffix}"
                    break
            if carrier is None:
                binding_unavailable = list(chain)
                arm, rationale = pick_arm(worker, role, avoid, credential=credential_state)
                rationale += f" (role binding {role!r} -> {', '.join(chain)} has no dispatchable worker)"
        else:
            arm, rationale = pick_arm(worker, role, avoid, credential=credential_state)

        record = {
            "agent": agent,
            "arm": arm["model"],
            "role": role,
            "purpose": purpose,
            "harness": worker.get("harness"),
            "model_family": family(arm["model"]),
            "credential": credential_state(family(arm["model"])),
            "confidence": decision.get("confidence"),
            "engine": decision.get("engine"),
            "escalate": decision.get("escalate", False),
            "advisory": decision.get("escalate", False) or decision.get("engine") == "llm",
            "why_this_arm": rationale,
            "arm_guidance": arm["when"],
        }
        if chosen_binding:
            record["role_binding"] = chosen_binding
            record["role_chain"] = chain
        if binding_unavailable:
            record["binding_unavailable"] = binding_unavailable
            binding_warnings.append(
                {"node": task["id"], "role": role, "model": binding_unavailable[0], "models": binding_unavailable}
            )
        if avoid and record["model_family"] in avoid:
            record["cross_vendor_violation"] = sorted(avoid)
            violations.append({"node": task["id"], "families": sorted(avoid), "arm": arm["model"]})
        if decision.get("laya_error"):
            record["laya_error"] = decision["laya_error"]
        routes[task["id"]] = record
    return routes, violations, binding_warnings


def replan(focus: str, request: str, context: str = "") -> dict:
    """Recursively re-decompose one failed node into a sub-DAG.

    This is RLM's recursion mapped onto the agent DAG executor: when a node
    cannot be done as one unit, split *it* rather than the whole request. The
    caller namespaces the returned `t1..tN` ids so they cannot collide with the
    run's existing nodes, and dispatches them as a sub-DAG.
    """
    try:
        config = _load_ladder()
    except ValueError as e:
        return {"ok": False, "error": str(e)[:300]}
    depth = (config.get("planning") or {}).get("recursiveDepth", 1)
    if not depth:
        return {"ok": False, "error": "planning.recursiveDepth is 0 — recursive re-planning is disabled"}
    if not focus.strip():
        return {"ok": False, "error": "replan needs the failed node's brief in `focus`"}
    envelope = decompose_mod.decompose(request, context, focus=focus)
    if not envelope.get("ok"):
        return envelope
    inner = envelope["result"]
    tasks = inner["tasks"]
    # Route the sub-DAG here so the executor can inject it as runnable nodes
    # without a second engine round trip.
    routes, violations, binding_warnings = route_nodes(config, tasks)
    return {
        "ok": True,
        "result": {
            "tasks": tasks,
            "engine": inner.get("engine"),
            "routes": routes,
            "waves": waves(tasks),
            "cross_vendor_violations": violations,
            "binding_warnings": binding_warnings,
            "recursive_depth": depth,
            "focus": focus[:400],
        },
    }


def _node_lines(tasks: list[dict], routes: dict[str, dict]) -> list[str]:
    """Human gate table, printed before the first dispatch."""
    by_id = {t["id"]: t for t in tasks}
    lines = ["id | title | agent | model | deps | acceptance"]
    for task in tasks:
        r = routes.get(task["id"], {})
        deps = ",".join(task.get("depends_on") or []) or "-"
        lines.append(
            f"{task['id']} | {task['title']} | {r.get('agent', '?')} | {r.get('arm', '?')} | "
            f"{deps} | {task.get('acceptance', '')}"
        )
    assert by_id  # keeps the id set alive for readers of the table
    return lines


def plan(
    request: str,
    context: str = "",
    *,
    decompose: bool = True,
    mode: str | None = None,
    because: str = "",
) -> dict:
    """Decide, decompose, route and wave a request. Never raises.

    `mode` overrides the gate: "direct" skips the laya pass entirely (the
    cheapest possible way to say "not worth orchestrating"), "orchestrate"
    goes straight to the DAG. Both record why in the envelope, because the
    override is a contract — the brain may only escalate to orchestrate when it
    can name >= 2 independent deliverables, and a caller overriding the gate is
    making that same claim out loud. An override with no `because` is still
    accepted, but it is reported as unbacked.

    Returns an envelope:
      {"ok": true, "result": {
         "mode": "direct" | "orchestrate",
         "triage": {...},            # the gate's own answer, absent when overridden
         "tasks": [...],             # orchestrate only
         "routes": {t<N>: {...}},
         "waves": [["t1","t2"], ["t3"]],
         "max_dispatches_per_turn": 4,
         "cross_vendor_violations": [...],
         "gate_table": ["id | title | …", …],
         "recommended": "…one line for the caller to act on…" }}
    """
    try:
        config = _load_ladder()
    except ValueError as e:
        return {"ok": False, "error": str(e)[:300]}
    if not orch.workers(config):
        return {
            "ok": False,
            "error": (
                "every worker in the ladder is marked unavailable — set \"available\": true on at "
                f"least one of {orch.excluded(config)}"
            ),
        }

    result: dict[str, Any] = {"ladder": config["path"], "brain": config["brain"]}
    if orch.excluded(config):
        result["excluded_workers"] = orch.excluded(config)

    # Repo memory: what earlier runs in this project learned, folded into the
    # context the gate and the decomposer see. Read-only — writing is an
    # explicit `remember`, so plan() stays a function of its inputs plus the
    # project's append-only log.
    try:
        from . import memory as mem

        memory_brief = mem.brief()
    except Exception:
        memory_brief = ""
    if memory_brief:
        context = f"{context}\n\n{memory_brief}" if context else memory_brief
        result["memory_brief_chars"] = len(memory_brief)

    if mode in ("direct", "orchestrate"):
        # A forced call is the caller's assertion, not the gate's answer.
        result["mode"] = mode
        result["gate_override"] = {"mode": mode, "because": because, "backed": bool(because.strip())}
        if mode == "direct":
            result["recommended"] = "forced direct; no gate, no DAG, no workers, no worktrees"
            return {"ok": True, "result": result}
    else:
        decision = triage_mod.triage(request, context)
        if "error" in decision:
            return {"ok": False, "error": f"triage failed: {decision['error']}"}
        result["mode"] = decision["mode"]
        result["triage"] = decision

        # The gate said no, or said so uncertainly: stop. One forward pass spent.
        #
        # The escalate branch has to report the *effective* mode, not the gate's
        # guess. The gate can answer "orchestrate" while being unsure of it, and
        # the contract says an unsure gate means direct — so an envelope carrying
        # `mode: "orchestrate"` alongside a note saying "defaulting to direct" is
        # a contradiction a model will resolve in favour of the field it reads
        # first, against the contract. The raw answer stays in `triage`.
        if decision["mode"] == "direct" or decision.get("escalate"):
            if decision.get("escalate"):
                result["mode"] = "direct"
                result["note"] = (
                    "low-confidence gate: defaulting to direct. Override to orchestrate only by "
                    "naming >= 2 independent deliverables (`--mode orchestrate --because ...`)."
                )
                if decision["mode"] != "direct":
                    result["gate_guess"] = decision["mode"]
            result["recommended"] = "handle inline; no DAG, no workers, no worktrees"
            return {"ok": True, "result": result}

    if not decompose:
        result["recommended"] = "orchestrate; call with decompose=True to build the DAG"
        return {"ok": True, "result": result}

    envelope = decompose_mod.decompose(request, context)
    if not envelope.get("ok"):
        return {
            "ok": False,
            "error": f"decompose failed: {envelope.get('error')}",
            "triage": result.get("triage"),
        }
    inner = envelope["result"]
    tasks = inner["tasks"]

    routes, violations, binding_warnings = route_nodes(config, tasks)
    result.update(
        {
            "tasks": tasks,
            "decompose_engine": inner.get("engine"),
            "planning": config.get("planning"),
            "routes": routes,
            "waves": waves(tasks),
            "role_bindings": config.get("roles") or {},
            "max_dispatches_per_turn": config["routing"].get("maxDispatchesPerTurn"),
            "worker_timeout_ms": config["routing"].get("workerTimeoutMs"),
            "gate_config": {
                "gate": config["routing"].get("gate"),
                "escalate_below": config["routing"].get("escalateBelow"),
            },
            "cross_vendor_violations": violations,
            "binding_warnings": binding_warnings,
            "gate_table": _node_lines(tasks, routes),
            "advisory_nodes": sorted(tid for tid, r in routes.items() if r.get("advisory")),
            "preflight": [
                {"node": tid, "arm": r["arm"], "credential": r.get("credential")}
                for tid, r in routes.items()
                if r.get("credential") == "missing"
            ],
        }
    )
    if inner.get("rlm_error"):
        result["rlm_error"] = inner["rlm_error"]
    if inner.get("critique"):
        result["plan_critique"] = inner["critique"]
    if len(tasks) < 2:
        result["warning"] = f"multi-part request decomposed into {len(tasks)} task(s); re-run with a sharper request"
    result["recommended"] = (
        f"dispatch {sum(len(w) for w in result['waves'])} node(s) in {len(result['waves'])} wave(s); "
        "the human merges — never the planner"
    )
    return {"ok": True, "result": result}
