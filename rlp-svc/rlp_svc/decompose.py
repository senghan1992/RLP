"""rlm_decompose — recursive task decomposition into a flat DAG.

Uses rlm (RLM) over an OpenAI-compatible backend, pointed at whichever endpoint
the ladder's `plan` role resolves to on this host. DAG shape is the contract;
engine substitution is a documented contingency, never a stall.

Three modern-planning ideas are layered on top of the raw RLM call:

1. **Configured recursion.** RLM's `max_depth` / `max_iterations` / budget are
   read from the ladder's `rlm` block instead of left at the library defaults,
   and a `custom_system_prompt` pins the model to emitting one JSON DAG rather
   than chatting through the REPL.
2. **A candidate ladder, not one arm.** The configured planner goes first, the
   ladder's DEFAULT arm is the safety net, and every remaining arm follows; the
   same candidates are reused by the plain-LLM contingency. A single arm that
   answers with an empty message used to end the whole plan, which is the
   opposite of "never stall".
3. **Critique + repair (self-refine).** After the first DAG, a cheap one-shot
   critic checks it against the request — missing dependencies, parallel tasks
   that actually overlap, over/under-splitting, a missing integration step — and
   may return a repaired DAG, which is validated before it is accepted. Bounded
   by `planning.maxRefines`.

`decompose(..., focus=...)` decomposes a *single* task into a sub-DAG, which is
how a failed node is recursively re-planned at dispatch time.
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any

from .llm import DAG_TOKENS, chat, chat_first, decomp_spec

DECOMPOSE_PROMPT = """You are a task decomposer for a multi-agent orchestrator. Given the REQUEST below,
decompose it (recursively if needed) into a flat DAG of 2-12 subtasks, each of which
a single agent can complete INDEPENDENTLY given only its brief and acceptance text.
Rules:
- Every subtask needs: unique id t1..tN, title (<=8 words), brief (self-contained,
  2-6 sentences, includes all files/paths/domain knowledge it needs), acceptance
  (one observable pass/fail sentence), depends_on (list of ids), domain (exactly one
  of: code, research, review, docs, integration), size (S, M, or L), and may include
  touches (the file paths/globs the subtask will read or write, if you know them).
- Independent subtasks MUST be marked with empty depends_on so they can run in
  parallel. No cycles. Keep it minimal: never invent optional tasks.
- Two subtasks with empty depends_on must not touch the same files. If they would,
  give one a dependency on the other instead.
- If the request needs verification of the whole result, add a final `integration`
  task depending on the leaves.
Respond ONLY with JSON: {{"tasks": [...]}} matching exactly the schema above.
REQUEST:
{request}
CONTEXT (may be empty):
{context}"""

#: RLM's own system prompt is about a REPL and recursion; left alone it competes
#: with "respond only with JSON". This pins the final answer while still letting
#: the model use the environment to reason. NOTE: RLM runs
#: `system_prompt.format(custom_tools_section=…)` on this string, so it must not
#: contain literal braces — hence "tasks" is described, not written as JSON here.
RLM_SYSTEM_PROMPT = (
    "You are the planning module of RLP, a coding orchestrator. Reply with one JSON object now: "
    "its only key is tasks, an array of subtask objects with the fields id, title, brief, acceptance, "
    "depends_on, domain and size, with ids t1, t2, and so on, in order. domain is one of "
    "code, research, review, docs, integration; size is S, M or L. Use the python environment "
    "only if the input is too large to read directly; otherwise do not write code. Finish with the "
    "JSON and nothing else."
)

CRITIC_PROMPT = """Return raw JSON only — your reply's first character must be {{. No prose, no markdown, no code fences.
You are a planning critic for a multi-agent orchestrator. Judge this DAG against the REQUEST.
Look for concrete, high-value defects only: a subtask that cannot be done from its brief alone,
parallel subtasks (empty depends_on) that would edit the same files, a missing dependency edge,
over-splitting (trivial tasks) or under-splitting (one task hiding several deliverables), and a
missing final integration/verification step when the request asks to verify the whole result.
Respond ONLY with JSON: either {{"verdict": "ok"}} when the DAG is good, or
{{"verdict": "repair", "issues": [<short strings>], "tasks": [<the full corrected DAG, same schema>]}}.
REQUEST:
{request}
CONTEXT (may be empty):
{context}
CURRENT DAG:
{tasks}"""

_DOMAINS = {"code", "research", "review", "docs", "integration"}
_SIZES = {"S", "M", "L"}
_ID_RE = re.compile(r"^t\d+$")

_DEFAULT_RLM = {
    # RLM's own default is 30 iterations, which is right for a long-context
    # exploration and wrong for a planning prompt: the system prompt already
    # says "finish with the JSON and nothing else", so the work is done in one
    # or two. Measured on the host gateway, an iteration costs ~45 s (192 s for
    # four), so eight iterations cannot finish inside any sane budget — the RLM
    # path would only ever time out and hand over to the plain-LLM contingency.
    # Three leaves room for the fallback within `maxTimeout`, which is the
    # budget for the whole decomposition.
    "maxDepth": 1,
    "maxIterations": 3,
    "maxConcurrentSubcalls": 4,
    "maxBudget": None,
    "maxTimeout": 300.0,
}
_DEFAULT_PLANNING = {"critique": True, "maxRefines": 1, "recursiveDepth": 1, "artifactPassing": True, "verifySamples": 3}


def _policy() -> tuple[dict, dict]:
    """(rlm knobs, planning policy) from the ladder, with safe defaults.

    A missing or broken ladder must not stop decomposition — the defaults are
    the same values the shipped ladder carries, and `doctor` reports the break.
    """
    try:
        from . import orchestration as orch

        config = orch.load()
    except Exception:
        config = None
    if not config:
        return dict(_DEFAULT_RLM), dict(_DEFAULT_PLANNING)
    return {**_DEFAULT_RLM, **(config.get("rlm") or {})}, {**_DEFAULT_PLANNING, **(config.get("planning") or {})}


def _role_spec(role: str) -> tuple[str, str] | None:
    """Ladder-resolved `(provider, model)` for a planner-side role, or None."""
    try:
        from . import orchestration as orch

        config = orch.load()
    except Exception:
        return None
    if not config:
        return None
    model = orch.resolve_model_for_role(config, role)
    if not model:
        return None
    from .llm import _split_spec

    return _split_spec(model)


def _planner_spec() -> tuple[str, str] | None:
    """The decomposition model: `RLP_DECOMPOSE_MODEL`, else the ladder's `plan` role.

    None when the ladder has no arms. There is deliberately no literal model id
    behind this: one would only be correct on the host it was written for.
    """
    return decomp_spec()


def _critique_spec() -> tuple[str, str] | None:
    """Critic model: `RLP_CRITIQUE_MODEL` > ladder role `critique`/`plan` > router arm."""
    from .llm import _split_spec, route_spec

    raw = os.environ.get("RLP_CRITIQUE_MODEL")
    if raw and raw.strip():
        explicit = _split_spec(raw.strip())
        if explicit:
            return explicit
        raise ValueError(f"RLP_CRITIQUE_MODEL={raw!r} must be a 'provider/model' string")
    return _role_spec("critique") or _role_spec("plan") or route_spec()


def _default_arm_spec() -> tuple[str, str] | None:
    """The ladder's first dispatchable arm — the operator's DEFAULT arm.

    That is the arm the `when` prose says carries the most headroom, which makes
    it the honest second choice for a planner that just failed.
    """
    try:
        from . import orchestration as orch

        config = orch.load()
    except Exception:
        return None
    if not config:
        return None
    from .llm import _split_spec

    for worker in orch.workers(config):
        for arm in worker["models"]:
            return _split_spec(arm["model"])
    return None


def _planner_fallbacks() -> list[tuple[str, str]]:
    """Candidate decomposition models, best first, deduplicated.

    The module's contract is that the DAG shape matters and the engine does not,
    and that a decomposition never stalls. Both were aspirational while the
    planner was a single arm: an arm that occasionally answers with an empty
    message took the whole plan down with "no JSON object in response". The
    configured planner goes first, the ladder's DEFAULT arm is the safety net,
    and the router arm is the last resort.

    Empty means the ladder has no arms at all, which the caller reports as "not
    configured" — never as a failed decomposition.
    """
    from .llm import role_candidates

    out: list[tuple[str, str]] = []
    for spec in (_planner_spec(), _default_arm_spec()):
        if spec and spec not in out:
            out.append(spec)
    for spec in role_candidates("plan"):
        if spec not in out:
            out.append(spec)
    return out


#: Shortest attempt worth starting. Below this, an arm cannot plausibly answer,
#: and spending the remainder on a doomed request is worse than reporting the
#: budget as exhausted.
_MIN_ATTEMPT_SECONDS = 15.0


def _attempt_timeout(deadline: float | None, attempts_left: int) -> float | None:
    """Per-attempt ceiling: the time left, shared by the attempts left.

    A candidate list is a *bounded* retry, so the budget is shared rather than
    multiplied. This was learned the hard way: with a per-attempt timeout, a
    gateway that hangs instead of erroring made one `decompose()` take
    N × `rlm.maxTimeout` — 15 minutes of a stalled plan on a three-arm ladder.
    Fair-share means a hung first arm cannot starve the rest, and the whole call
    is bounded by `rlm.maxTimeout`. `None` (no budget configured) leaves each
    client its own default.
    """
    if deadline is None:
        return None
    return max(_MIN_ATTEMPT_SECONDS, (deadline - time.monotonic()) / max(1, attempts_left))


def _extract_first_json(text: str) -> Any:
    """Extract the first balanced {…} block from an LLM response."""
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object in response")
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return json.loads(text[start : i + 1])
    raise ValueError("unbalanced JSON object in response")


def _topo_sort(tasks: list[dict]) -> list[dict]:
    """Kahn's algorithm; raises ValueError on cycle."""
    ids = {t["id"] for t in tasks}
    indeg = {t["id"]: 0 for t in tasks}
    children: dict[str, list[str]] = {i: [] for i in ids}
    for t in tasks:
        for d in t.get("depends_on", []):
            if d in ids:
                indeg[t["id"]] += 1
                children[d].append(t["id"])
    queue = [i for i in ids if indeg[i] == 0]
    order: list[dict] = []
    by_id = {t["id"]: t for t in tasks}
    while queue:
        node = queue.pop(0)
        order.append(by_id[node])
        for child in children[node]:
            indeg[child] -= 1
            if indeg[child] == 0:
                queue.append(child)
    if len(order) != len(tasks):
        raise ValueError("cycle detected in depends_on")
    return order


def _validate_dag(tasks: list[dict]) -> None:
    if not 1 <= len(tasks) <= 12:
        raise ValueError(f"DAG has {len(tasks)} tasks (want 1-12)")
    seen: set[str] = set()
    for t in tasks:
        tid = t.get("id", "")
        if not _ID_RE.match(tid):
            raise ValueError(f"bad task id {tid!r} (want t<N>)")
        if tid in seen:
            raise ValueError(f"duplicate task id {tid!r}")
        seen.add(tid)
        for field in ("title", "brief", "acceptance"):
            if not str(t.get(field, "")).strip():
                raise ValueError(f"task {tid} missing {field!r}")
        if t.get("domain") not in _DOMAINS:
            raise ValueError(f"task {tid} has bad domain {t.get('domain')!r}")
        if t.get("size") not in _SIZES:
            raise ValueError(f"task {tid} has bad size {t.get('size')!r}")
        deps = t.get("depends_on", [])
        if not isinstance(deps, list):
            raise ValueError(f"task {tid} depends_on is not a list")
        for d in deps:
            if d not in seen:
                raise ValueError(f"task {tid} depends on unknown id {d!r}")
    _topo_sort(tasks)  # raises on cycle


def _normalize_tasks(raw: Any) -> list[dict]:
    """Coerce a model-produced DAG into the schema before validating it.

    Models drift: ids come back as `T1`, a domain as `context-analysis`, a size
    as `medium`, a dependency as `T2`. None of that is worth failing a plan
    over — the fields are routing hints — so known-vocabulary fields are coerced
    to the nearest legal value and unknown dependencies are dropped. What is
    *not* coerced is prose (`title`/`brief`/`acceptance`), which is validated.
    """
    tasks = raw.get("tasks", []) if isinstance(raw, dict) else raw
    if not isinstance(tasks, list):
        raise ValueError("no task list in the model output")
    out: list[dict] = []
    used: set[str] = set()
    for i, task in enumerate(tasks, 1):
        if not isinstance(task, dict):
            continue
        match = re.match(r"^[tT](\d+)$", str(task.get("id") or "").strip())
        tid = f"t{match.group(1)}" if match else f"t{i}"
        while tid in used:
            tid = f"t{int(tid[1:]) + 1}"
        used.add(tid)
        domain = str(task.get("domain") or "code").strip().lower()
        size = str(task.get("size") or "M").strip().upper()[:1]
        out.append(
            {
                **task,
                "id": tid,
                "title": str(task.get("title") or "").strip(),
                "brief": str(task.get("brief") or "").strip(),
                "acceptance": str(task.get("acceptance") or "").strip(),
                "domain": domain if domain in _DOMAINS else "code",
                "size": size if size in _SIZES else "M",
            }
        )
    ids = {t["id"] for t in out}
    for t in out:
        deps: list[str] = []
        for dep in t.get("depends_on") or []:
            dm = re.match(r"^[tT](\d+)$", str(dep).strip())
            nd = f"t{dm.group(1)}" if dm else str(dep)
            if nd in ids and nd != t["id"] and nd not in deps:
                deps.append(nd)
        t["depends_on"] = deps
    if not out:
        raise ValueError("no usable tasks in the model output")
    return out


def _validated(tasks: list[dict]) -> list[dict]:
    _validate_dag(tasks)
    return _topo_sort(tasks)


def _plain_llm_decompose(request: str, context: str, timeout: float | None = None) -> list[dict]:
    """Contingency (c): recursive plain-LLM decomposition. Returns tasks list.

    `timeout` is the per-candidate ceiling, so the contingency costs the same
    bounded walk as the RLM path rather than an unbounded one.
    """
    prompt = DECOMPOSE_PROMPT.format(request=request, context=context)
    # `chat_fn=chat` keeps this module's own client as the seam (the offline
    # suite stubs it here), rather than reaching through to llm.chat.
    text, _spec = chat_first(
        _planner_fallbacks(),
        messages=[{"role": "user", "content": prompt}],
        chat_fn=chat,
        timeout=timeout,
        max_tokens=DAG_TOKENS,
    )
    return _extract_first_json(text)


def _rlm_decompose(prompt: str, knobs: dict, spec: tuple[str, str] | None = None) -> str:
    """One configured RLM completion. Returns the final response text.

    `spec` is the arm to run it on; omitting it means the configured planner.
    """
    from rlm import RLM

    from .llm import _provider_base_url, _provider_key

    provider, model = spec or _planner_spec()
    kwargs: dict[str, Any] = {"max_depth": knobs.get("maxDepth")}
    for key, arg in (("maxIterations", "max_iterations"), ("maxConcurrentSubcalls", "max_concurrent_subcalls"),
                     ("maxBudget", "max_budget"), ("maxTimeout", "max_timeout")):
        if knobs.get(key) is not None:
            kwargs[arg] = knobs[key]
    rlm = RLM(
        backend="openai",
        backend_kwargs={
            "model_name": model,
            "api_key": _provider_key(provider),
            "base_url": _provider_base_url(provider),
        },
        environment="local",
        custom_system_prompt=RLM_SYSTEM_PROMPT,
        verbose=False,
        # The completion budget goes to `sampling_args`, which reaches each
        # chat-completions call. It must NOT go to RLM's own `max_tokens`: that
        # one is a *total* input+output ceiling for the whole recursive run, and
        # 8 kB of it is spent by the time the REPL has run three iterations —
        # `TokenLimitExceededError` after 118 s, which is a self-inflicted wound
        # rather than a model problem. The run stays bounded by `max_iterations`
        # and `max_timeout` instead.
        sampling_args={"max_tokens": DAG_TOKENS},
        sub_sampling_args={"max_tokens": DAG_TOKENS},
        **{k: v for k, v in kwargs.items() if v is not None},
    )
    return rlm.completion(prompt).response


def _refine(request: str, context: str, tasks: list[dict], max_refines: int) -> tuple[list[dict], dict]:
    """Critique-and-repair the DAG. Never raises; returns (tasks, critique record)."""
    record: dict[str, Any] = {"applied": False, "issues": [], "rounds": 0}
    # "Never raises" has to include the config errors: a ladder with no arms
    # resolves no critic, and a malformed RLP_CRITIQUE_MODEL raises on read. The
    # critique is an improvement pass over a DAG that is already valid, so both
    # cases skip it with the reason recorded rather than losing the plan.
    try:
        spec = _critique_spec()
    except ValueError as e:
        return tasks, {**record, "critique_error": str(e)[:200]}
    if spec is None:
        return tasks, {**record, "critique_error": "no model resolved for the critic (the ladder has no arms)"}
    provider, model = spec
    for _ in range(max(0, max_refines)):
        record["rounds"] += 1
        prompt = CRITIC_PROMPT.format(
            request=request,
            context=context,
            tasks=json.dumps({"tasks": tasks}, ensure_ascii=False),
        )
        try:
            text = chat(provider, model, messages=[{"role": "user", "content": prompt}], max_tokens=DAG_TOKENS)
            verdict = _extract_first_json(text)
        except Exception as e:
            record["critique_error"] = str(e)[:200]
            break
        if not isinstance(verdict, dict) or verdict.get("verdict") != "repair":
            break
        candidate = verdict.get("tasks")
        if not isinstance(candidate, list) or not candidate:
            record["issues"] = [str(i)[:160] for i in (verdict.get("issues") or [])][:8]
            record["critique_error"] = "critic returned repair without usable tasks"
            break
        try:
            _validate_dag(candidate)
            candidate = _topo_sort(candidate)
        except Exception as e:
            record["critique_error"] = f"repaired DAG invalid: {str(e)[:160]}"
            break
        tasks = candidate
        record["applied"] = True
        record["issues"] = [str(i)[:160] for i in (verdict.get("issues") or [])][:8]
    return tasks, record


def decompose(request: str, context: str = "", *, focus: str = "", refine: bool | None = None) -> dict:
    """Decompose a request (or one focused task) into a flat DAG.

    `focus` switches from "split this request" to "split THIS task" — the
    recursive re-plan used when a dispatched node fails. `refine` overrides the
    ladder's `planning.critique` for this call.

    Envelope shape: {ok, result|error}. `result` carries `engine`, `tasks`, and
    an optional `critique` record.
    """
    knobs, policy = _policy()
    if refine is None:
        refine = bool(policy["critique"])

    # No arms means no planner, and that has a fix worth naming. Checked before
    # any prompt is built so the answer is the cause rather than "every
    # candidate model failed" with an empty list behind it.
    if not _planner_fallbacks():
        from . import orchestration as orch

        return {"ok": False, "error": orch.NOT_CONFIGURED}

    target = request
    if focus.strip():
        target = (
            "Break THIS single task into 2-6 subtasks that together complete it. Do not add work "
            "the task does not ask for.\nTASK:\n" + focus.strip() + "\n\nOriginal request, for context only:\n" + request
        )
    prompt = DECOMPOSE_PROMPT.format(request=target, context=context)

    # Contingency ladder (plan): rlm on each candidate arm, then plain-LLM on
    # the same candidates. Never stall: the DAG shape is the contract, not the
    # engine — and not the arm either. The whole walk shares one wall-clock
    # budget (`rlm.maxTimeout`), with the last quarter reserved for the plain-LLM
    # contingency, so a gateway that hangs cannot hold a plan open for
    # N × the timeout.
    budget = knobs.get("maxTimeout")
    deadline = (time.monotonic() + float(budget)) if budget else None
    rlm_deadline = (deadline - max(30.0, float(budget) / 4)) if deadline else None

    engine = "rlm"
    rlm_error = None
    raw = None
    candidates = _planner_fallbacks()
    planner_used: tuple[str, str] | None = None
    for index, spec in enumerate(candidates):
        timeout = _attempt_timeout(rlm_deadline, len(candidates) - index)
        if timeout is None or (timeout <= _MIN_ATTEMPT_SECONDS and index > 0):
            rlm_error = (
                f"stopped before {spec[0]}/{spec[1]}: the {budget:.0f}s budget (rlm.maxTimeout) is spent"
            )
            break
        attempt_knobs = {**knobs}
        if timeout is not None:
            configured = attempt_knobs.get("maxTimeout")
            attempt_knobs["maxTimeout"] = timeout if configured is None else min(float(configured), timeout)
        try:
            raw = _rlm_decompose(prompt, attempt_knobs, spec)
            planner_used = spec
            break
        except Exception as e:
            rlm_error = f"{spec[0]}/{spec[1]}: {str(e)[:200]}"
            raw = None

    if raw is None:
        engine = "fallback-plain-llm"

    plain_timeout = _attempt_timeout(deadline, len(candidates)) if deadline else None
    if engine == "fallback-plain-llm":
        try:
            tasks = _validated(_normalize_tasks(_plain_llm_decompose(target, context, plain_timeout)))
        except Exception as e:
            detail = f" ({rlm_error})" if rlm_error else ""
            return {"ok": False, "error": f"decomposition failed: {str(e)[:200]}{detail}"}
    else:
        try:
            tasks = _validated(_normalize_tasks(_extract_first_json(raw)))
        except Exception as e:
            # A model DAG that cannot be made valid is not a dead end: fall back
            # to the plain-LLM decomposer instead of failing the whole plan.
            engine = "fallback-plain-llm"
            rlm_error = f"rlm DAG unusable: {str(e)[:240]}"
            try:
                tasks = _validated(_normalize_tasks(_plain_llm_decompose(target, context, plain_timeout)))
            except Exception as e2:
                return {"ok": False, "error": f"decomposition failed: {str(e2)[:200]} ({rlm_error})"}

    result: dict[str, Any] = {"engine": engine, "tasks": tasks}
    if planner_used:
        result["planner"] = f"{planner_used[0]}/{planner_used[1]}"
        if candidates and planner_used != candidates[0]:
            result["planner_fallback"] = True
    if rlm_error:
        result["rlm_error"] = rlm_error
    # Recursive re-plan of a single node is already scoped: critique the whole
    # request against it would only confuse the critic.
    if refine and not focus.strip():
        tasks, critique = _refine(request, context, tasks, int(policy["maxRefines"]))
        result["tasks"] = tasks
        if critique.get("rounds"):
            result["critique"] = critique
    return {"ok": True, "result": result}