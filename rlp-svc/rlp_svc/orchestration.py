"""RLP orchestration config — the same ladder the fork renders into the brain's
system prompt, read here so the router's roster and the brain's prompt cannot
drift apart.

Path: `$RLP_ORCHESTRATION` when set, else `<RLP agent dir>/orchestration.json` —
`~/.rlp/agent` by default. RLP does not read pi's `~/.pi`: see `paths.py` for
the one rule the harness, the extensions and this module share.

A ladder has two parts, and only one of them can ship:

  * **policy** — the gate, the dispatch cap, the worker watchdog, cross-vendor
    review, the RLM and planning budgets. Provider-independent, so RLP ships
    real defaults for all of it.
  * **arms** — `brain` and each worker's `models`. These name a specific
    `provider/model` on *this* host, so RLP ships none of them and will not
    guess. `/setup` writes them from the endpoints the user actually connected.

That makes "installed but not configured" a first-class state rather than a
broken file: `parse()` accepts a ladder with no brain and no arms and reports
`configured: False`, so `doctor` can name it in one line and `/setup` can fill
it in place. Shipping a plausible-looking arm instead would mean every fresh
install planned dispatches onto a model the host cannot serve — which fails at
the worker, three steps from the cause.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from . import paths


#: What to say when the ladder cannot dispatch. One sentence, one fix, and the
#: same words wherever the engine hits it, so the answer a caller gets does not
#: depend on which entry point it came through.
NOT_CONFIGURED = (
    "the orchestration ladder has no model arms yet, so there is nothing to "
    "orchestrate onto — run /setup in a session to connect a provider and pick "
    "the brain and the worker arms (or `rlp provider add <id> <baseUrl> <model>` "
    "then /rlp-config). Until then every request is handled inline."
)


#: The gate modes the ladder may name, and what each one means in practice:
#:
#:   laya    the laya pass alone; unsure means direct
#:   hybrid  laya, plus the deterministic fan-out signals when unsure
#:   direct  never orchestrate — every request is handled inline by design, and
#:           laya is never loaded. The ladder keeps carrying the rest of the
#:           policy, so switching back is one edit rather than a re-setup.
GATES = ("laya", "hybrid", "direct")

#: Session-only override: `rlp --direct` (and RLP_DIRECT=1 anywhere else) says
#: "this run does not orchestrate" without rewriting the ladder, which holds
#: the user's model choices.
DIRECT_ENV = "RLP_DIRECT"

_TRUE = ("1", "true", "yes", "on")


def env_direct() -> bool:
    """Whether the session override asked for direct-only behaviour."""
    return str(os.environ.get(DIRECT_ENV, "")).strip().lower() in _TRUE


def direct_source(gate: str | None) -> str | None:
    """What makes this host direct-only, or None when it is not.

    The single place that knows the two things which decide it — the session
    override first, then the ladder's gate. A caller that compared against
    `"direct"` itself would be a caller that ignores `RLP_DIRECT` and reports a
    mode that is not in effect.
    """
    if env_direct():
        return f"${DIRECT_ENV}"
    if gate == "direct":
        return "routing.gate"
    return None


def gate_of(config: dict | None) -> str:
    """The gate a resolved ladder names, defaulting the way `triage` defaults."""
    if not config:
        return "hybrid"
    return (config.get("routing") or {}).get("gate") or "hybrid"


def direct_mode(config: dict | None = None) -> dict:
    """{"direct", "gate", "source"} — is this host in direct-only mode, and why.

    Never raises: an unreadable or absent ladder means the default (hybrid),
    which is what `triage` falls back to as well. `source` names the decider so
    every report can say "from $RLP_DIRECT" or "from routing.gate" instead of
    leaving the user to work out which file to edit.
    """
    if config is None:
        try:
            config = load()
        except Exception:
            config = None
    gate = gate_of(config)
    source = direct_source(gate)
    return {
        "direct": source is not None,
        "gate": gate,
        "source": source or ("routing.gate" if config else "default"),
    }


def config_path() -> Path:
    """`$RLP_ORCHESTRATION`, else `<agent dir>/orchestration.json`.

    The agent dir is RLP's own `~/.rlp/agent` (`RLP_CODING_AGENT_DIR`, or the
    fork's `RPI_CODING_AGENT_DIR`, relocate it) — the same file the harness
    renders into the prompt, so both sides read one file.
    """
    override = os.environ.get("RLP_ORCHESTRATION")
    if override:
        return Path(override).expanduser()
    return paths.orchestration_json()


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _model_ref(value: Any, where: str) -> str:
    _require(isinstance(value, str) and value.strip() != "", f"{where} must be a non-empty string")
    _require("/" in value, f'{where} must be a "provider/model" string')
    return value


def _string_list(value: Any, where: str) -> list[str]:
    _require(isinstance(value, list) and value, f"{where} must be a non-empty array of strings")
    _require(all(isinstance(v, str) and v for v in value), f"{where} must be a non-empty array of strings")
    return list(value)


def load() -> dict | None:
    """Resolved ladder, or None when no config is installed."""
    path = config_path()
    if not path.is_file():
        return None
    return parse(path.read_text(), str(path))


def parse(raw: str, source: str) -> dict:
    """Parse and validate. Raises ValueError naming the offending field."""
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"{source}: invalid JSON ({e})") from None
    _require(isinstance(parsed, dict), f"{source}: expected a JSON object")

    workers_raw = parsed.get("workers")
    _require(isinstance(workers_raw, list) and workers_raw, f"{source}: 'workers' must be a non-empty array")
    workers = []
    seen: set[str] = set()
    for i, raw_worker in enumerate(workers_raw):
        where = f"workers[{i}]"
        _require(isinstance(raw_worker, dict), f"{source}: {where} must be an object")
        wid = raw_worker.get("id")
        _require(isinstance(wid, str) and wid.strip() != "", f"{source}: {where}.id must be a non-empty string")
        _require(wid not in seen, f"{source}: duplicate worker id {wid!r}")
        seen.add(wid)
        harness = raw_worker.get("harness")
        _require(harness is None or isinstance(harness, str), f"{source}: {where}.harness must be a string")
        # Availability is configuration, not prose: an arm the host cannot
        # currently serve (exhausted entitlement, missing CLI) is marked
        # unavailable here, so the router never plans a dispatch that cannot
        # run. Omitted means available.
        available = raw_worker.get("available", True)
        _require(isinstance(available, bool), f"{source}: {where}.available must be a boolean")
        note = raw_worker.get("availabilityNote", "")
        _require(isinstance(note, str), f"{source}: {where}.availabilityNote must be a string")

        # An empty `models` array is the unconfigured state, not an error: the
        # worker exists (it carries the harness and the availability flag) and
        # `/setup` adds its arms once the user has an endpoint to draw them from.
        models_raw = raw_worker.get("models")
        if models_raw is None:
            models_raw = []
        _require(isinstance(models_raw, list), f"{source}: worker {wid!r} 'models' must be an array")
        models = []
        for j, raw_model in enumerate(models_raw):
            mwhere = f"{source}: worker {wid!r} models[{j}]"
            _require(isinstance(raw_model, dict), f"{mwhere} must be an object")
            when = raw_model.get("when")
            _require(isinstance(when, str) and when.strip() != "", f"{mwhere}.when must be a non-empty string")
            models.append(
                {
                    "model": _model_ref(raw_model.get("model"), f"{mwhere}.model"),
                    "roles": _string_list(raw_model.get("roles"), f"{mwhere}.roles"),
                    "when": when,
                }
            )
        entry: dict = {"id": wid, "models": models, "available": available}
        if harness is not None:
            entry["harness"] = harness
        if note:
            entry["availabilityNote"] = note
        workers.append(entry)

    routing_raw = parsed.get("routing") or {}
    _require(isinstance(routing_raw, dict), f"{source}: 'routing' must be an object")
    escalate = routing_raw.get("escalateBelow")
    if escalate is not None:
        _require(isinstance(escalate, (int, float)) and 0 <= escalate <= 1,
                 f"{source}: routing.escalateBelow must be a number in [0, 1]")
    cap = routing_raw.get("maxDispatchesPerTurn")
    if cap is not None:
        _require(isinstance(cap, int) and not isinstance(cap, bool) and cap >= 1,
                 f"{source}: routing.maxDispatchesPerTurn must be a positive integer")
    # `gate` selects how triage decides: "laya" keeps the old behaviour (low
    # confidence always defaults to direct), "hybrid" also consults the
    # deterministic fan-out signals, "direct" never orchestrates and never loads
    # laya at all. Omitted means hybrid, because on the bundled checkpoint every
    # decision lands below escalateBelow.
    gate = routing_raw.get("gate", "hybrid")
    _require(gate in GATES, f"{source}: routing.gate must be one of {', '.join(GATES)}")
    signal_threshold = routing_raw.get("signalThreshold")
    if signal_threshold is not None:
        _require(isinstance(signal_threshold, (int, float)) and signal_threshold >= 0,
                 f"{source}: routing.signalThreshold must be a non-negative number")
    # Seconds a worker may run before `rlp_collect` treats it as wedged and
    # kills it. None means no limit, which is a choice and not a default.
    worker_timeout_ms = routing_raw.get("workerTimeoutMs")
    if worker_timeout_ms is not None:
        _require(isinstance(worker_timeout_ms, int) and not isinstance(worker_timeout_ms, bool) and worker_timeout_ms >= 1000,
                 f"{source}: routing.workerTimeoutMs must be an integer >= 1000 (ms)")

    review_raw = parsed.get("review") or {}
    _require(isinstance(review_raw, dict), f"{source}: 'review' must be an object")
    cross_vendor = review_raw.get("crossVendor", False)
    _require(isinstance(cross_vendor, bool), f"{source}: review.crossVendor must be a boolean")

    # Role bindings: `role -> provider/model`. The pool of usable models is the
    # union of every worker's arms, so a binding can only name a model the
    # ladder already carries — otherwise the planner would name a model nothing
    # can dispatch. Absent means "use arm priority order", the old behaviour.
    roles_raw = parsed.get("roles") or {}
    _require(isinstance(roles_raw, dict), f"{source}: 'roles' must be an object")
    pool = {arm["model"] for worker in workers for arm in worker["models"]}
    roles: dict[str, Any] = {}
    for role, ref in roles_raw.items():
        _require(isinstance(role, str) and role.strip() != "", f"{source}: role names must be non-empty strings")
        if isinstance(ref, list):
            _require(bool(ref), f"{source}: roles.{role} must not be an empty list")
            chain = [_model_ref(item, f"{source}: roles.{role}[{i}]") for i, item in enumerate(ref)]
        else:
            chain = [_model_ref(ref, f"{source}: roles.{role}")]
        for model in chain:
            _require(
                model in pool,
                f"{source}: roles.{role} names {model!r}, which is not an arm on any worker — add the arm first",
            )
        roles[role] = chain[0] if len(chain) == 1 else chain

    # RLM knobs. The library defaults (max_depth=1, max_iterations=30, no budget)
    # are now configuration rather than an accident, and the *planner* owns its
    # own critique/recursion policy.
    rlm_raw = parsed.get("rlm") or {}
    _require(isinstance(rlm_raw, dict), f"{source}: 'rlm' must be an object")

    def _opt_int(key: str, minimum: int) -> int | None:
        value = rlm_raw.get(key)
        if value is None:
            return None
        _require(isinstance(value, int) and not isinstance(value, bool) and value >= minimum,
                 f"{source}: rlm.{key} must be an integer >= {minimum}")
        return value

    def _opt_num(key: str) -> float | None:
        value = rlm_raw.get(key)
        if value is None:
            return None
        _require(isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0,
                 f"{source}: rlm.{key} must be a positive number")
        return float(value)

    rlm = {
        "maxDepth": _opt_int("maxDepth", 0),
        "maxIterations": _opt_int("maxIterations", 1),
        "maxConcurrentSubcalls": _opt_int("maxConcurrentSubcalls", 1),
        "maxBudget": _opt_num("maxBudget"),
        "maxTimeout": _opt_num("maxTimeout"),
    }

    # Planning policy: the critique/repair loop and the depth to which a failed
    # node may be recursively re-decomposed (the RLM recursion, mapped onto the
    # agent DAG executor).
    planning_raw = parsed.get("planning") or {}
    _require(isinstance(planning_raw, dict), f"{source}: 'planning' must be an object")
    critique = planning_raw.get("critique", True)
    _require(isinstance(critique, bool), f"{source}: planning.critique must be a boolean")
    max_refines = planning_raw.get("maxRefines", 1)
    _require(isinstance(max_refines, int) and not isinstance(max_refines, bool) and 0 <= max_refines <= 3,
             f"{source}: planning.maxRefines must be an integer in [0, 3]")
    recursive_depth = planning_raw.get("recursiveDepth", 1)
    _require(isinstance(recursive_depth, int) and not isinstance(recursive_depth, bool) and 0 <= recursive_depth <= 3,
             f"{source}: planning.recursiveDepth must be an integer in [0, 3]")
    artifact_passing = planning_raw.get("artifactPassing", True)
    _require(isinstance(artifact_passing, bool), f"{source}: planning.artifactPassing must be a boolean")
    verify_samples = planning_raw.get("verifySamples", 3)
    _require(isinstance(verify_samples, int) and not isinstance(verify_samples, bool) and 1 <= verify_samples <= 7,
             f"{source}: planning.verifySamples must be an integer in [1, 7]")
    planning = {
        "critique": critique,
        "maxRefines": max_refines,
        "recursiveDepth": recursive_depth,
        "artifactPassing": artifact_passing,
        "verifySamples": verify_samples,
    }

    # `brain` absent or null means "not chosen yet". Present means it must be a
    # real `provider/model` — a half-written brain is still an error.
    raw_brain = parsed.get("brain")
    brain = None if raw_brain is None else _model_ref(raw_brain, f"{source}: 'brain'")
    arm_count = sum(len(w["models"]) for w in workers)

    return {
        "path": source,
        "brain": brain,
        # A ladder is configured when it can actually dispatch: a brain to plan
        # with, and at least one arm to dispatch to. Callers that need one branch
        # on this rather than re-deriving it and disagreeing.
        "configured": brain is not None and arm_count > 0,
        "arm_count": arm_count,
        # "direct" when the ladder chose direct-only mode, "full" otherwise. A
        # derived field, so it is never a second source of truth: `gate` stays
        # the thing that is written, and this is the thing a caller branches on.
        "mode": "direct" if gate == "direct" else "full",
        "workers": workers,
        "roles": roles,
        "routing": {
            "escalateBelow": escalate,
            "maxDispatchesPerTurn": cap,
            "gate": gate,
            "signalThreshold": signal_threshold,
            "workerTimeoutMs": worker_timeout_ms,
        },
        "review": {"crossVendor": cross_vendor},
        "rlm": rlm,
        "planning": planning,
    }


def workers(config: dict, *, only_available: bool = True) -> list[dict]:
    """Dispatchable workers. Unavailable ones are opt-in arms, not failures."""
    return [w for w in config["workers"] if w.get("available", True) or not only_available]


def excluded(config: dict) -> list[dict]:
    """Workers the router may not plan for, each with the operator's reason."""
    return [
        {"id": w["id"], "harness": w.get("harness"), "reason": w.get("availabilityNote", "marked unavailable")}
        for w in config["workers"]
        if not w.get("available", True)
    ]


#: Roles the planner can actually dispatch. The union of these and whatever the
#: ladder's arms declare is what the role editor offers, so a custom arm role
#: still shows up even though the built-in domain map does not know it.
#: (`integration` is a *domain* that maps to the `code` role, not a role itself.)
#: `plan` / `critique` / `verify` / `route` are not worker roles: they name the
#: models the *engine itself* uses — for decomposition, plan review, best-of-N
#: verdicts, and the LLM fallback behind the laya gate. Binding `route` to a
#: cheap fast arm is usually right: it answers a one-line classification, and it
#: is the only one of the four on the critical path of every request.
PLANNER_ROLES = ("code", "debug", "review", "research", "docs", "explore", "plan", "critique", "verify", "route")


def arm_roles(config: dict) -> list[str]:
    """Every role any arm declares, in first-seen order."""
    seen: list[str] = []
    for worker in config["workers"]:
        for arm in worker["models"]:
            for role in arm["roles"]:
                if role not in seen:
                    seen.append(role)
    return seen


def known_roles(config: dict) -> list[str]:
    """Roles the role editor should offer: planner roles plus ladder-declared ones."""
    roles = list(PLANNER_ROLES)
    for role in arm_roles(config):
        if role not in roles:
            roles.append(role)
    for role in (config.get("roles") or {}):
        if role not in roles:
            roles.append(role)
    return roles


def model_pool(config: dict) -> list[str]:
    """Models a role binding may name: every arm on every worker (available or not)."""
    pool: list[str] = []
    for worker in config["workers"]:
        for arm in worker["models"]:
            if arm["model"] not in pool:
                pool.append(arm["model"])
    return pool


def role_chain(config: dict, role: str) -> list[str]:
    """A role's binding as an ordered preference list, or [] when unbound.

    A `roles` value is either a `provider/model` string or an array of them, in
    priority order. A role bound to several models is a fallback chain: the
    planner uses the first one a dispatchable worker can actually serve.
    """
    bound = (config.get("roles") or {}).get(role)
    if isinstance(bound, str):
        return [bound]
    if isinstance(bound, list):
        return list(bound)
    return []


def resolve_model_for_role(config: dict, role: str) -> str | None:
    """The model a planner-side role (plan/critique/verify) should use, or None.

    A `roles.<role>` binding wins; otherwise the first arm (priority order) that
    declares the role. Unlike `resolve_role` this ignores worker availability —
    these roles are called directly by the planner, not dispatched to a worker.
    """
    chain = role_chain(config, role)
    if chain:
        return chain[0]
    for worker in config["workers"]:
        for arm in worker["models"]:
            if role in arm["roles"]:
                return arm["model"]
    return None


def resolve_role(config: dict, role: str) -> dict | None:
    """What a role currently resolves to: {model, worker, binding, chain} or None.

    A binding wins; within a binding the first model a dispatchable worker
    carries is used, so a chain is a fallback list rather than a promise that
    the first entry always runs. With no binding, the first arm (priority order)
    on a dispatchable worker that declares the role is what the planner picks.
    """
    chain = role_chain(config, role)
    if chain:
        for index, model in enumerate(chain):
            for worker in workers(config):
                if any(a["model"] == model for a in worker["models"]):
                    return {
                        "model": model,
                        "worker": worker["id"],
                        "binding": True,
                        "chain": chain,
                        "index": index,
                    }
        return {"model": chain[0], "worker": None, "binding": True, "chain": chain, "index": 0, "unavailable": True}
    for worker in workers(config):
        for arm in worker["models"]:
            if role in arm["roles"]:
                return {"model": arm["model"], "worker": worker["id"], "binding": False, "chain": [arm["model"]], "index": 0}
    return None


def raw_load() -> dict:
    """The ladder file as plain JSON, unvalidated, for mutation.

    Preserves keys this module does not model, so an edit never silently drops
    a field a newer version added. Raises FileNotFoundError when absent.
    """
    path = config_path()
    if not path.is_file():
        raise FileNotFoundError(
            f"no orchestration ladder at {path} — run `rlp doctor` or set RLP_ORCHESTRATION"
        )
    return json.loads(path.read_text())


def _worker_raw(raw: dict, worker_id: str) -> dict:
    for worker in raw.get("workers") or []:
        if isinstance(worker, dict) and worker.get("id") == worker_id:
            return worker
    raise ValueError(f"no worker {worker_id!r} in the ladder")


def _arm_index(worker: dict, match: Any) -> int:
    """Locate an arm by 0-based index or by `provider/model` ref."""
    models = worker.get("models") or []
    if isinstance(match, bool):
        raise ValueError("arm `match` must be an index or a provider/model string")
    if isinstance(match, int):
        if not 0 <= match < len(models):
            raise ValueError(f"arm index {match} out of range (worker has {len(models)} arm(s))")
        return match
    if isinstance(match, str):
        for i, arm in enumerate(models):
            if isinstance(arm, dict) and arm.get("model") == match:
                return i
        raise ValueError(f"no arm {match!r} on this worker")
    raise ValueError("arm `match` must be an index or a provider/model string")


def _apply_op(raw: dict, op: dict) -> None:
    """Apply one mutation op to the raw ladder, in place. Raises ValueError."""
    _require(isinstance(op, dict), "each op must be an object")
    name = op.get("op")
    if name == "set_brain":
        raw["brain"] = op.get("model")
    elif name == "add_arm":
        worker = _worker_raw(raw, op.get("worker"))
        arm = {"model": op.get("model"), "roles": op.get("roles"), "when": op.get("when")}
        position = op.get("position")
        models = worker.setdefault("models", [])
        if position is None:
            models.append(arm)
        else:
            _require(isinstance(position, int) and 0 <= position <= len(models),
                     f"position must be an index in [0, {len(models)}]")
            models.insert(position, arm)
    elif name == "set_arm":
        worker = _worker_raw(raw, op.get("worker"))
        index = _arm_index(worker, op.get("match"))
        arm = worker["models"][index]
        for field in ("model", "roles", "when"):
            if op.get(field) is not None:
                arm[field] = op[field]
    elif name == "remove_arm":
        worker = _worker_raw(raw, op.get("worker"))
        index = _arm_index(worker, op.get("match"))
        _require(len(worker.get("models") or []) > 1,
                 "a worker must keep at least one arm; add the replacement first")
        worker["models"].pop(index)
    elif name == "move_arm":
        worker = _worker_raw(raw, op.get("worker"))
        models = worker.get("models") or []
        frm, to = op.get("from"), op.get("to")
        _require(isinstance(frm, int) and isinstance(to, int), "move_arm needs integer from/to")
        _require(0 <= frm < len(models) and 0 <= to < len(models), "move_arm from/to out of range")
        models.insert(to, models.pop(frm))
    elif name == "add_worker":
        workers = raw.setdefault("workers", [])
        wid = op.get("worker") or op.get("id")
        if any(isinstance(w, dict) and w.get("id") == wid for w in workers):
            raise ValueError(f"worker {wid!r} already exists")
        entry: dict = {"id": wid, "models": [{"model": op.get("model"), "roles": op.get("roles"), "when": op.get("when")}]}
        if op.get("harness"):
            entry["harness"] = op["harness"]
        workers.append(entry)
    elif name == "remove_worker":
        workers = raw.get("workers") or []
        remaining = [w for w in workers if not (isinstance(w, dict) and w.get("id") == op.get("worker"))]
        if len(remaining) == len(workers):
            raise ValueError(f"no worker {op.get('worker')!r} in the ladder")
        _require(remaining, "the ladder must keep at least one worker")
        raw["workers"] = remaining
    elif name == "set_worker_available":
        worker = _worker_raw(raw, op.get("worker"))
        available = op.get("available")
        _require(isinstance(available, bool), "set_worker_available needs a boolean `available`")
        if available:
            worker.pop("available", None)
        else:
            worker["available"] = False
        if op.get("note") is not None:
            worker["availabilityNote"] = op["note"]
    elif name == "set_routing":
        routing = raw.setdefault("routing", {})
        key = op.get("key")
        _require(key in ("escalateBelow", "maxDispatchesPerTurn", "gate", "signalThreshold", "workerTimeoutMs"),
                 f"unknown routing key {key!r}")
        if op.get("value") is None:
            routing.pop(key, None)
        else:
            routing[key] = op["value"]
    elif name == "set_review":
        review = raw.setdefault("review", {})
        _require(isinstance(op.get("crossVendor"), bool), "set_review needs a boolean crossVendor")
        review["crossVendor"] = op["crossVendor"]
    elif name == "set_planning":
        planning = raw.setdefault("planning", {})
        key = op.get("key")
        _require(key in ("critique", "maxRefines", "recursiveDepth", "artifactPassing", "verifySamples"),
                 f"unknown planning key {key!r}")
        if op.get("value") is None:
            planning.pop(key, None)
        else:
            planning[key] = op["value"]
    elif name == "set_rlm":
        rlm = raw.setdefault("rlm", {})
        key = op.get("key")
        _require(key in ("maxDepth", "maxIterations", "maxConcurrentSubcalls", "maxBudget", "maxTimeout"),
                 f"unknown rlm key {key!r}")
        if op.get("value") is None:
            rlm.pop(key, None)
        else:
            rlm[key] = op["value"]
    elif name == "set_role":
        role = op.get("role")
        _require(isinstance(role, str) and role.strip() != "", "set_role needs a role name")
        raw_models = op.get("models")
        if raw_models is None:
            single = op.get("model")
            _require(isinstance(single, str) and "/" in single, "set_role needs a provider/model, or a models array")
            chain = [single]
        else:
            _require(isinstance(raw_models, list) and raw_models, "set_role `models` must be a non-empty array")
            chain = list(raw_models)
        pool = {m.get("model") for w in raw.get("workers") or [] for m in (w.get("models") or [])}
        for model in chain:
            _require(isinstance(model, str) and "/" in model, "each set_role model must be a provider/model string")
            _require(model in pool, f"model {model!r} is not an arm on any worker — add the arm first (add_arm)")
        # One model stays a string (the old shape); several become an ordered
        # preference list, which the planner walks in order.
        raw.setdefault("roles", {})[role] = chain[0] if len(chain) == 1 else chain
    elif name == "clear_role":
        role = op.get("role")
        _require(isinstance(role, str) and role.strip() != "", "clear_role needs a role name")
        roles = raw.get("roles") or {}
        if isinstance(roles, dict):
            roles.pop(role, None)
        if not roles:
            raw.pop("roles", None)
    else:
        raise ValueError(f"unknown config op {name!r}")


def mutate(ops: list[dict], *, dry_run: bool = False) -> dict:
    """Edit the ladder in place: validate first, then back up and write atomically.

    The same validator `load()` uses runs on the candidate before a byte hits
    disk, so a bad op is a printed error and an untouched file, never a broken
    ladder the harness then refuses to read. Writes go through a timestamped
    backup and an atomic replace.

    Returns {"path", "backup", "dry_run", "ladder"}.
    """
    _require(isinstance(ops, list) and ops, "expected a non-empty array of ops")
    import time

    raw = raw_load()
    for op in ops:
        _apply_op(raw, op)
    candidate = json.dumps(raw, indent=2, ensure_ascii=False) + "\n"
    parsed = parse(candidate, str(config_path()))  # raises on anything invalid
    path = config_path()
    if dry_run:
        return {"path": str(path), "backup": None, "dry_run": True, "ladder": parsed}
    backup = path.with_suffix(f"{path.suffix}.bak.{int(time.time())}")
    backup.write_text(path.read_text())
    tmp = path.with_suffix(f"{path.suffix}.tmp.{os.getpid()}")
    tmp.write_text(candidate)
    os.replace(tmp, path)
    return {"path": str(path), "backup": str(backup), "dry_run": False, "ladder": parsed}


def roster(config: dict, *, include_unavailable: bool = False) -> list[dict]:
    """Router roster cards derived from the ladder.

    One card per dispatchable worker: strengths are the union of its arms'
    roles plus a short reach-for-it line per arm, so the decision model sees
    the same guidance the brain's prompt carries. Unavailable workers are
    omitted by default — routing to an arm that cannot run wastes the whole
    plan — and `include_unavailable=True` returns the full set for inspection.

    A worker with no arms is omitted too, and for the same reason: an
    unconfigured ladder must produce an empty roster rather than a card with
    nothing behind it, so the caller reports "no arms" instead of routing to
    one that does not exist.
    """
    cards = []
    for worker in workers(config, only_available=not include_unavailable):
        if not worker["models"]:
            continue
        roles: list[str] = []
        for entry in worker["models"]:
            for role in entry["roles"]:
                if role not in roles:
                    roles.append(role)
        arm_notes = [f"{e['model']} when {e['when']}" for e in worker["models"]]
        strengths = (roles + arm_notes)[:5]
        arms = ", ".join(e["model"] for e in worker["models"])
        harness = f" on the {worker['harness']} harness" if worker.get("harness") else ""
        description = (
            f"{worker['id']}{harness}. Model arms: {arms}. "
            + " ".join(e["when"] for e in worker["models"])
        )
        cards.append(
            {
                "id": worker["id"],
                "description": description[:520],
                "strengths": strengths,
            }
        )
    return cards