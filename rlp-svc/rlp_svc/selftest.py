"""Offline selftest: decompose + route + orchestration ladder (no MCP transport).

Run: rlp-svc/.venv/bin/python -m rlp_svc.selftest
"""
from __future__ import annotations

import json
import os
import sys
import time

FALLBACK_ROSTER = [
    {
        "id": "pi-impl",
        "description": "Implements scoped code edits headlessly on the Pi multi-model harness; wide fan-out and quick exploration",
        "strengths": ["scoped edits", "fast", "exploration", "docs"],
    },
    {
        "id": "pi-review",
        "description": "Independent diff reviewer judging against an acceptance contract; read-only, reports issues",
        "strengths": ["review", "diff analysis", "cross-vendor"],
    },
    {
        "id": "claude_code",
        "description": "Heavyweight multi-file implementer on the Claude Code subscription; subtle multi-file reasoning, spend-limited",
        "strengths": ["multi-file", "hard debugging", "subtle reasoning"],
    },
]


def main() -> None:
    failures = 0

    # 1. orchestration ladder resolves and is well-formed.
    from . import orchestration as orch

    config = orch.load()
    if config is None:
        print(f"orchestration: MISSING at {orch.config_path()} — falling back to the built-in roster")
        derived_roster = FALLBACK_ROSTER
        escalate_below = 0.55
    else:
        derived_roster = orch.roster(config)
        escalate_below = config["routing"]["escalateBelow"] or 0.55
        print(f"orchestration: {config['path']}")
        print(f"  brain={config['brain']} workers={[w['id'] for w in config['workers']]} "
              f"escalateBelow={escalate_below} crossVendor={config['review']['crossVendor']}")
        for excluded in orch.excluded(config):
            print(f"  excluded from routing: {excluded['id']} — {excluded['reason']}")
        if len(derived_roster) != len(orch.workers(config)):
            print(f"orchestration FAILED: roster has {len(derived_roster)} cards for "
                  f"{len(orch.workers(config))} dispatchable workers")
            failures += 1
        if not all(card["description"] and card["strengths"] for card in derived_roster):
            print(f"orchestration FAILED: empty card fields in {derived_roster}")
            failures += 1

    # 2. triage gate: simple -> direct, multi-part -> orchestrate
    from .triage import triage as triage_fn

    simple = triage_fn("Fix the typo in the README title", "")
    print(f"triage(simple): {json.dumps(simple)[:170]}")
    complex_ = triage_fn(
        "Add a payments module with tests and docs, refactor the two services it touches, "
        "and have the whole change independently reviewed",
        "",
    )
    print(f"triage(complex): {json.dumps(complex_)[:170]}")
    if "error" in simple or "error" in complex_:
        print("triage FAILED")
        failures += 1
    else:
        if simple.get("engine") != "laya":
            print(f"triage engine={simple.get('engine')!r} (laya expected)")
            failures += 1
        if simple.get("mode") != "direct" and not simple.get("escalate"):
            print(f"triage(simple) -> {simple.get('mode')!r} without escalate; expected direct")
            failures += 1
        if complex_.get("mode") != "orchestrate" and not complex_.get("escalate"):
            print(f"triage(complex) -> {complex_.get('mode')!r} without escalate; expected orchestrate")
            failures += 1

    # 3. decompose
    from .decompose import decompose

    t0 = time.monotonic()
    env = decompose("Add a --wc flag counting words in the README of a docs site and test it", "")
    tasks = env.get("result", {}).get("tasks", [])
    print(f"decompose: ok={env.get('ok')} engine={env.get('result', {}).get('engine')} "
          f"tasks={len(tasks)} ({time.monotonic() - t0:.1f}s)")
    if not env.get("ok") or len(tasks) < 2:
        print(f"decompose FAILED: {env}")
        failures += 1

    # 4. route via the configured roster (laya; first call may download the model)
    from .route import route

    t0 = time.monotonic()
    r = route(
        "review the wc-flag diff",
        "Judge the diff against its acceptance contract; read-only",
        "review",
        derived_roster,
        escalate_below,
    )
    print(f"laya_route: {json.dumps(r)} ({time.monotonic() - t0:.1f}s)")
    if "error" in r:
        print(f"route FAILED: {r}")
        failures += 1
    else:
        chosen = r.get("agent")
        if chosen not in {card["id"] for card in derived_roster}:
            print(f"route FAILED: {chosen!r} is not a roster id")
            failures += 1
        if r.get("engine") != "laya":
            print(f"engine is {r.get('engine')!r} with laya_error={r.get('laya_error')!r} — "
                  "laya did not load; fix deps before proceeding")
            failures += 1

    # 5. llm fallback must never raise, even with unreadable credentials.
    from .llm import chat, route_spec
    from .route import llm_route

    try:
        out = llm_route("review the wc-flag diff", "read-only review", "review", derived_roster)
        print(f"llm_route: {json.dumps(out)[:200]}")
        if out.get("engine") != "llm":
            print(f"llm_route FAILED: engine={out.get('engine')!r}")
            failures += 1
    except Exception as e:
        print(f"llm_route FAILED: {type(e).__name__}: {e}")
        failures += 1

    # 6. the headless planner: the whole pipeline as a function, and the gate
    #    that is supposed to close most of the time.
    from . import plan as plan_mod

    t0 = time.monotonic()
    simple_plan = plan_mod.plan("Fix the typo in the README title", "")
    print(f"plan(simple): mode={simple_plan.get('result', {}).get('mode')} "
          f"({'ok' if simple_plan.get('ok') else simple_plan.get('error')}) "
          f"({time.monotonic() - t0:.1f}s)")
    if not simple_plan.get("ok"):
        print(f"plan FAILED: {simple_plan}")
        failures += 1
    elif "tasks" in simple_plan["result"]:
        # Not a hard failure — the gate is a classifier, not a switch — but a
        # simple request that produces a DAG means the gate stopped gating.
        print("plan WARNING: a typo request produced a DAG; the gate is not closing")

    t0 = time.monotonic()
    full = plan_mod.plan(
        "Add a --wc flag counting words in the README of a docs site, test it, document it, "
        "and have the whole diff independently reviewed",
        "",
        mode="orchestrate",
        because="code, test, docs and an independent review are four separate deliverables",
    )
    if not full.get("ok"):
        print(f"plan FAILED: {full.get('error')}")
        failures += 1
    else:
        r = full["result"]
        print(f"plan(orchestrate): {len(r['tasks'])} tasks, waves={r['waves']}, "
              f"excluded={[w['id'] for w in r.get('excluded_workers', [])]} "
              f"({time.monotonic() - t0:.1f}s)")
        print("  " + "\n  ".join(r["gate_table"]))
        if not r["tasks"]:
            print("plan FAILED: an orchestrate plan with no tasks")
            failures += 1
        if not r["waves"]:
            print("plan FAILED: no dispatch waves")
            failures += 1
        roster_ids = {c["id"] for c in derived_roster}
        for tid, node in r["routes"].items():
            if node["agent"] not in roster_ids:
                print(f"plan FAILED: {tid} routed to {node['agent']!r}, off the dispatchable roster")
                failures += 1
        if r["cross_vendor_violations"]:
            print(f"plan WARNING: cross-vendor violations {r['cross_vendor_violations']}")

    # 7. doctor must agree with itself and never crash.
    from . import doctor

    report = doctor.run(host=False)
    print(f"doctor: {'runnable' if report['ok'] else 'NOT runnable'} — {report['summary']}")
    if any(c["status"] == "fail" for c in report["checks"]):
        for c in report["checks"]:
            if c["status"] == "fail":
                print(f"  FAIL {c['name']}: {c['detail']}")
        failures += 1

    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
