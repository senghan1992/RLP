"""Offline tests for the parts that must never be wrong: the planner's shape.

These run in milliseconds with the model layers stubbed, so they are the ones
to run on every change. The engines' own accuracy is a different question and
belongs to `selftest.sh`, which pays for the real laya load.

    rlp-svc/.venv/bin/python -m rlp_svc.tests
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

LADDER = {
    "brain": "agnes/agnes-3.0-flash",
    "workers": [
        {
            "id": "pi",
            "harness": "pi",
            "models": [
                {
                    "model": "agnes/agnes-3.0-flash",
                    "roles": ["code", "review", "docs"],
                    "when": "default arm, carries the bulk",
                },
                {
                    "model": "qwen-token-plan/deepseek-v4.1-flash",
                    "roles": ["code", "review", "debug"],
                    "when": "deep arm and second vendor",
                },
            ],
        },
        {
            "id": "claude_code",
            "harness": "claude-native",
            "models": [
                {
                    "model": "anthropic/claude-opus-4-8",
                    "roles": ["code", "review"],
                    "when": "opt-in, spend-limited",
                }
            ],
        },
    ],
    "routing": {"escalateBelow": 0.55, "maxDispatchesPerTurn": 4},
    "review": {"crossVendor": True},
}

TASKS = [
    {
        "id": "t1",
        "title": "implement the subtract helper",
        "brief": "add wc -l style counting to the calc module",
        "acceptance": "pytest passes",
        "depends_on": [],
        "domain": "code",
        "size": "M",
    },
    {
        "id": "t2",
        "title": "write the usage doc",
        "brief": "document the new flag in USAGE.md",
        "acceptance": "USAGE.md mentions the flag",
        "depends_on": [],
        "domain": "docs",
        "size": "S",
    },
    {
        "id": "t3",
        "title": "independent diff review",
        "brief": "judge the t1 diff against its acceptance contract, read-only",
        "acceptance": "no blocking issues",
        "depends_on": ["t1"],
        "domain": "review",
        "size": "S",
    },
]

_failures: list[str] = []


def check(condition: bool, message: str) -> None:
    if not condition:
        _failures.append(message)
        print(f"FAIL {message}")


def with_ladder(fn, *, tasks: list[dict] | None = None, triage: dict | None = None, ladder: dict | None = None) -> object:
    """Run `fn` with a ladder on disk and the two model layers stubbed."""
    from . import decompose, orchestration, plan, route, triage as triage_mod

    original = {
        "load": orchestration.load,
        "triage": triage_mod.triage,
        "decompose": decompose.decompose,
        "route": route.route,
    }
    orchestration.load = lambda: orchestration.parse(json.dumps(ladder or LADDER), "test")
    triage_mod.triage = lambda request, context="": triage or {
        "mode": "orchestrate",
        "confidence": 0.9,
        "engine": "laya",
        "escalate": False,
    }
    decompose.decompose = lambda request, context="", **kwargs: {
        "ok": True,
        "result": {"engine": "stub", "tasks": tasks if tasks is not None else TASKS},
    }
    route.route = lambda title, brief, domain, roster, gate=0.55: {
        "agent": roster[0]["id"],
        "confidence": 0.8,
        "engine": "laya",
        "escalate": False,
    }
    try:
        return fn()
    finally:
        orchestration.load = original["load"]
        triage_mod.triage = original["triage"]
        decompose.decompose = original["decompose"]
        route.route = original["route"]


def test_waves() -> None:
    from .plan import waves

    check(waves(TASKS) == [["t1", "t2"], ["t3"]], f"waves of a fan-out + review: {waves(TASKS)}")
    check(waves([TASKS[0]]) == [["t1"]], "single node is one wave")
    chain = [dict(TASKS[0], id="a", depends_on=[]), dict(TASKS[0], id="b", depends_on=["a"])]
    check(waves(chain) == [["a"], ["b"]], "a chain is one node per wave")
    try:
        cyclic = [dict(TASKS[0], id="a", depends_on=["b"]), dict(TASKS[0], id="b", depends_on=["a"])]
        waves(cyclic)
        check(False, "waves() accepted a cycle")
    except ValueError:
        check(True, "cycle rejected")


def test_pick_arm() -> None:
    from .plan import family, intent, pick_arm

    pi = LADDER["workers"][0]
    check(pick_arm(pi, "code")[0]["model"] == "agnes/agnes-3.0-flash", "priority order: first matching arm wins")
    check(pick_arm(pi, "debug")[0]["model"] == "qwen-token-plan/deepseek-v4.1-flash", "role picks its own arm")
    arm, why = pick_arm(pi, "review", avoid_families={"agnes"})
    check(arm["model"] == "qwen-token-plan/deepseek-v4.1-flash", f"cross-vendor re-pick: {arm['model']}")
    check("different vendor" in why, f"rationale names the rule: {why!r}")
    solo = LADDER["workers"][1]
    check(pick_arm(solo, "code", avoid_families={"anthropic"})[0]["model"] == "anthropic/claude-opus-4-8",
          "no alternative family: keep the arm rather than fail the plan")
    check(family("qwen-token-plan/deepseek-v4.1-flash") == "qwen-token-plan", "family is the provider")
    check(intent("code") == ("code", "implement"), "code -> implement")
    check(intent("review") == ("review", "review"), "review -> review")
    check(intent("docs") == ("docs", "explore"), "docs -> explore")
    check(intent("nonsense") == ("code", "implement"), "unknown domain defaults to code")


def test_plan_gate_closes() -> None:
    from . import plan

    def run():
        return plan.plan("fix the typo")

    direct = with_ladder(run, triage={
        "mode": "direct", "confidence": 0.99, "engine": "laya", "escalate": False,
    })
    check(direct["ok"], "direct-mode plan succeeds")
    check(direct["result"]["mode"] == "direct", "direct mode reported")
    check("tasks" not in direct["result"], f"no DAG for a direct request: {direct['result'].keys()}")
    check("inline" in direct["result"]["recommended"], "direct mode recommends inline work")

    escalated = with_ladder(run, triage={
        "mode": "direct", "confidence": 0.2, "engine": "laya", "escalate": True,
    })
    check(escalated["result"]["mode"] == "direct", "low confidence defaults to direct")
    check("--mode orchestrate" in escalated["result"].get("note", ""),
          "the note points at the documented override")

    # The gate can guess "orchestrate" while being unsure. The contract says an
    # unsure gate means direct, so the envelope must not say orchestrate: a model
    # reading the field first would orchestrate against the contract.
    unsure_orchestrate = with_ladder(run, triage={
        "mode": "orchestrate", "confidence": 0.28, "engine": "laya", "escalate": True,
    })
    check(unsure_orchestrate["result"]["mode"] == "direct",
          f"an unsure orchestrate guess is reported as direct, not orchestrate: {unsure_orchestrate['result']['mode']}")
    check(unsure_orchestrate["result"].get("gate_guess") == "orchestrate",
          "the gate's raw guess is still visible under gate_guess")
    check("tasks" not in unsure_orchestrate["result"], "an escalated plan builds no DAG")
    check(unsure_orchestrate["result"]["triage"]["mode"] == "orchestrate",
          "the raw triage answer is preserved untouched")

    def boom():
        from . import triage as triage_mod
        triage_mod.triage = lambda *a, **k: {"ok": False, "error": "engine down"}
        return plan.plan("anything")

    broken = with_ladder(boom)
    check(broken["ok"] is False and "triage failed" in broken["error"], f"triage failure is an error: {broken}")


def test_gate_override() -> None:
    from . import plan

    result = with_ladder(
        lambda: plan.plan("x", mode="orchestrate", because="code, docs and a review"),
    )
    check(result["ok"], f"a forced orchestrate plan builds: {result.get('error')}")
    r = result["result"]
    check("triage" not in r, "an override does not fake a gate answer")
    check(r["gate_override"] == {"mode": "orchestrate", "because": "code, docs and a review", "backed": True},
          f"the override is recorded: {r.get('gate_override')}")
    check(bool(r["waves"]), "an overridden plan still waves")

    unbacked = with_ladder(lambda: plan.plan("x", mode="orchestrate"))["result"]
    check(unbacked["gate_override"]["backed"] is False, "an unbacked override says so")

    cheap = with_ladder(lambda: plan.plan("x", mode="direct", because="typo"))["result"]
    check(cheap["mode"] == "direct" and "tasks" not in cheap, "forcing direct short-circuits before the DAG")
    check("gate" not in cheap, "forcing direct skips the gate entirely")

    def boom_gate():
        from . import triage as triage_mod
        triage_mod.triage = lambda *a, **k: (_ for _ in ()).throw(AssertionError("gate was consulted"))
        return plan.plan("x", mode="direct")

    forced = with_ladder(boom_gate)
    check(forced["ok"] and forced["result"]["mode"] == "direct", "forced direct never touches laya")


def test_plan_orchestrates() -> None:
    from . import plan

    result = with_ladder(lambda: plan.plan("do the thing"))["result"]
    check(result["mode"] == "orchestrate", "orchestrate mode")
    check([t["id"] for t in result["tasks"]] == ["t1", "t2", "t3"], "tasks carried through")
    check(result["waves"] == [["t1", "t2"], ["t3"]], f"waves computed: {result['waves']}")
    check(result["max_dispatches_per_turn"] == 4, "per-turn cap surfaced from the ladder")
    check(result["routes"]["t1"]["arm"] == "agnes/agnes-3.0-flash", f"t1 on the default arm: {result['routes']['t1']}")
    check(result["routes"]["t1"]["purpose"] == "implement", "code node dispatches as implement")
    check(result["routes"]["t3"]["purpose"] == "review", "review node dispatches as review")
    check(result["routes"]["t3"]["arm"] != result["routes"]["t1"]["arm"],
          f"review landed on another vendor: t1={result['routes']['t1']['arm']} t3={result['routes']['t3']['arm']}")
    check(not result["cross_vendor_violations"], f"no violations: {result['cross_vendor_violations']}")
    check(result["gate_table"][0].startswith("id | title"), f"gate table header: {result['gate_table'][0]}")
    check(len(result["gate_table"]) == 4, "one header row plus one row per node")
    check("merge" in result["recommended"], "the planner never merges")

    # A ladder with a single vendor family cannot satisfy the cross-vendor rule.
    # The planner must say so instead of quietly shipping a same-vendor review.
    single_family = {
        "brain": "agnes/agnes-3.0-flash",
        "workers": [
            {
                "id": "pi",
                "harness": "pi",
                "models": [
                    {"model": "agnes/agnes-3.0-flash", "roles": ["code", "review"], "when": "only arm"}
                ],
            }
        ],
        "routing": {"escalateBelow": 0.55, "maxDispatchesPerTurn": 4},
        "review": {"crossVendor": True},
    }
    solo = with_ladder(
        lambda: plan.plan("claude only"),
        ladder=single_family,
        tasks=[dict(TASKS[0], id="t1"), dict(TASKS[0], id="t2", domain="review", depends_on=["t1"])],
    )["result"]
    check(bool(solo["cross_vendor_violations"]), "a single-family ladder reports the violation instead of hiding it")
    check(solo["waves"] == [["t1"], ["t2"]], "a violating plan still waves, so the run can proceed knowingly")


def waves_of(result: dict) -> list[list[str]]:
    return result.get("waves", [])

def test_plan_degrade_paths() -> None:
    from . import plan

    def single():
        return plan.plan("do the thing")

    thin = with_ladder(single, tasks=[TASKS[0]])["result"]
    check("1 task" in thin.get("warning", ""), f"a one-node DAG warns: {thin.get('warning')}")

    def no_decompose():
        return plan.plan("do the thing", decompose=False)

    gated = with_ladder(no_decompose)["result"]
    check(gated["mode"] == "orchestrate" and "tasks" not in gated, "orchestrate without decomposing is still a decision")

    def bad_dag():
        return plan.plan("do the thing")

    with tempfile.TemporaryDirectory() as tmp:
        import os

        broken = Path(tmp) / "orchestration.json"
        broken.write_text('{"brain": "no-slash", "workers": []}')
        import os

        os.environ["RLP_ORCHESTRATION"] = str(broken)
        try:
            result = plan.plan("do the thing")
            check(result["ok"] is False and "ladder" in result["error"], f"invalid ladder is loud: {result}")
            missing = Path(tmp) / "nope.json"
            os.environ["RLP_ORCHESTRATION"] = str(missing)
            absent = plan.plan("do the thing")
            check(absent["ok"] is False and "doctor" in absent["error"], f"absent ladder points at doctor: {absent}")
        finally:
            os.environ.pop("RLP_ORCHESTRATION", None)


def test_availability() -> None:
    from . import orchestration as orch

    ladder = json.loads(json.dumps(LADDER))
    parsed = orch.parse(json.dumps(ladder), "test")
    check(len(orch.workers(parsed)) == 2, "no availability field means available")
    check(len(orch.roster(parsed)) == 2, "roster defaults to every worker")

    ladder["workers"][1]["available"] = False
    ladder["workers"][1]["availabilityNote"] = "entitlement exhausted"
    parsed = orch.parse(json.dumps(ladder), "test")
    check([w["id"] for w in orch.workers(parsed)] == ["pi"], "an unavailable worker is not dispatchable")
    check([c["id"] for c in orch.roster(parsed)] == ["pi"], "the router never sees an unavailable worker")
    check(len(orch.roster(parsed, include_unavailable=True)) == 2, "inspection can still see the full ladder")
    excluded = orch.excluded(parsed)
    check(excluded[0]["id"] == "claude_code" and "exhausted" in excluded[0]["reason"],
          f"the reason is carried: {excluded}")

    for bad, why in [
        ('{"brain":"p/m","workers":[{"id":"a","available":"yes","models":[{"model":"p/m","roles":["c"],"when":"x"}]}]}',
         "non-boolean availability"),
        ('{"brain":"p/m","workers":[{"id":"a","availabilityNote":3,"models":[{"model":"p/m","roles":["c"],"when":"x"}]}]}',
         "non-string availabilityNote"),
    ]:
        try:
            orch.parse(bad, "test")
            check(False, f"ladder accepted invalid availability: {why}")
        except ValueError:
            check(True, why)


def test_plan_excludes_unavailable() -> None:
    from . import plan

    ladder = json.loads(json.dumps(LADDER))
    ladder["workers"][1]["available"] = False
    ladder["workers"][1]["availabilityNote"] = "entitlement exhausted"
    result = with_ladder(lambda: plan.plan("x", mode="orchestrate"), ladder=ladder)["result"]
    check(all(r["agent"] == "pi" for r in result["routes"].values()),
          f"no node lands on the unavailable arm: {[r['agent'] for r in result['routes'].values()]}")
    check(result["excluded_workers"][0]["id"] == "claude_code", "the plan says what it excluded and why")

    dead = json.loads(json.dumps(LADDER))
    for w in dead["workers"]:
        w["available"] = False
    broken = with_ladder(lambda: plan.plan("x", mode="orchestrate"), ladder=dead)
    check(broken["ok"] is False and "available" in broken["error"],
          f"an all-unavailable ladder fails loudly instead of planning nothing: {broken}")


def test_ladder_validation() -> None:
    from . import orchestration as orch

    parsed = orch.parse(json.dumps(LADDER), "test")
    check(len(orch.roster(parsed)) == 2, "one roster card per worker")
    check(parsed["routing"]["escalateBelow"] == 0.55, "gate survives parsing")
    for bad, why in [
        ('{"workers": []}', "empty workers"),
        ('{"brain": "no-slash", "workers": [{"id":"a","models":[{"model":"p/m","roles":["code"],"when":"x"}]}]}',
         "brain without provider"),
        ('{"brain":"p/m","workers":[{"id":"a","models":[{"model":"p/m","roles":[],"when":"x"}]}]}', "empty roles"),
        ('{"brain":"p/m","workers":[{"id":"a","models":[{"model":"p/m","roles":["code"],"when":""}]}]}', "empty when"),
        ('{"brain":"p/m","workers":[{"id":"a","models":[]}]}', "worker without arms"),
        ('{"brain":"p/m","workers":[{"id":"a","models":[{"model":"p/m","roles":["c"],"when":"x"}]}],'
         '"routing":{"escalateBelow":3}}', "gate out of range"),
    ]:
        try:
            orch.parse(bad, "test")
            check(False, f"ladder accepted an invalid config: {why}")
        except ValueError:
            check(True, why)


def test_engine_protocol() -> None:
    """The resident engine's wire protocol, without paying the model load.

    `--no-warm` is the point: the protocol is what the agent depends on, and it
    must be verifiable in milliseconds. A protocol bug here breaks every tool at
    once, and the failure mode would be a hang rather than an error.
    """
    import subprocess
    import sys as _sys

    requests = [
        {"id": 1, "op": "ladder", "args": {}},
        {"id": 2, "op": "nope", "args": {}},
        {"id": 3, "op": "plan", "args": {"request": "x", "mode": "direct"}},
    ]
    payload = "\n".join(json.dumps(r) for r in requests) + "\n"
    proc = subprocess.run(
        [_sys.executable, "-m", "rlp_svc", "engine", "--no-warm"],
        input=payload, capture_output=True, text=True, timeout=120,
    )
    events = [json.loads(l) for l in proc.stdout.splitlines() if l.strip()]
    check(events and events[0].get("event") == "ready", f"engine announces ready first: {events[:1]}")
    replies = {e["id"]: e for e in events if "id" in e}
    check(set(replies) == {1, 2, 3}, f"one reply per request id: {sorted(replies)}")
    check(replies[1]["ok"] is True and replies[1]["result"]["brain"], "ladder op returns the ladder")
    check(replies[2]["ok"] is False and "unknown op" in replies[2]["error"], "unknown op is an error reply")
    check(replies[3]["ok"] is True, "a request survives the previous bad op (the server stays up)")
    check("result" in replies[3] and "ok" not in replies[3].get("result", {}),
          f"an op that returns an envelope is not wrapped twice: {list(replies[3].get('result', {}))[:4]}")
    check(replies[3]["result"]["mode"] == "direct", "a forced direct plan short-circuits the gate")

    # A broken line must not take the process down either.
    proc = subprocess.run(
        [_sys.executable, "-m", "rlp_svc", "engine", "--no-warm"],
        input="not json\n" + json.dumps({"id": 9, "op": "ladder", "args": {}}) + "\n",
        capture_output=True, text=True, timeout=120,
    )
    events = [json.loads(l) for l in proc.stdout.splitlines() if l.strip()]
    check(any(e.get("ok") is False and "bad JSON" in (e.get("error") or "") for e in events), "bad JSON is reported")
    check(any(e.get("id") == 9 and e.get("ok") for e in events), "the server keeps serving after bad JSON")


def test_engine_covers_the_cli() -> None:
    """Every CLI subcommand the extensions rely on has an engine op.

    The extension prefers the resident engine and falls back to the CLI. If the
    two drift, the fallback starts answering with different shapes than the
    engine — which is worse than failing, because it works until it does not.
    """
    from . import engine

    ops = set(engine._ops())
    for op in ("plan", "triage", "decompose", "ladder", "route", "llm_route", "warm", "config", "replan", "verify", "memory", "remember"):
        check(op in ops, f"engine op {op!r} exists (agents call it by name)")

    # and the ops the extension maps to a CLI invocation must have one
    for op in ("plan", "triage", "decompose", "ladder"):
        check(op in ops, f"{op!r} has both an engine op and a CLI subcommand")


def test_cli_surface() -> None:
    from .cli import EXIT_USAGE, build_parser

    parser = build_parser()
    expected = {"triage", "decompose", "route", "llm-route", "ladder", "roster", "provider", "config", "replan", "verify", "memory", "remember", "plan", "doctor", "serve", "version"}
    actions = [a for a in parser._actions if hasattr(a, "choices") and isinstance(a.choices, dict)]
    check(bool(actions), "the parser declares subcommands")
    if actions:
        check(expected <= set(actions[0].choices), f"every command present: missing {expected - set(actions[0].choices)}")
    try:
        parser.parse_args(["nonsense"])
        check(False, "the parser accepted an unknown subcommand")
    except SystemExit as e:
        check(e.code == EXIT_USAGE, f"usage error exit code: {e.code}")

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "sub.json"
        path.write_text(json.dumps(LADDER))
        import os

        os.environ["RLP_ORCHESTRATION"] = str(path)
        try:
            import contextlib
            import io

            from . import cli

            # Only the model-free commands: an offline suite must not pay the
            # ~170 s cold laya load to prove a parser works. stdout is captured
            # so the --json envelopes do not drown the report.
            sink = io.StringIO()
            with contextlib.redirect_stdout(sink):
                roster_code = cli.main(["roster", "--json"])
                ladder_code = cli.main(["ladder", "--json"])
            check(roster_code == 0 and ladder_code == 0, "ladder/roster exit 0")
            check('"roster"' in sink.getvalue(), "roster --json printed the derived cards")
        finally:
            os.environ.pop("RLP_ORCHESTRATION", None)


def test_version_consistency() -> None:
    """One version, three places. A drift between them is a support ticket."""
    import re as _re

    import rlp_svc

    from . import cli

    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    text = pyproject.read_text()
    declared = _re.search(r'^version\s*=\s*"([^"]+)"', text, _re.MULTILINE)
    check(declared is not None, "pyproject declares a version")
    if declared:
        check(
            declared.group(1) == rlp_svc.__version__ == cli.VERSION,
            f"pyproject={declared.group(1) if declared else '?'} package={rlp_svc.__version__} cli={cli.VERSION}",
        )
    check(callable(getattr(cli, "_cmd_provider", None)), "the provider command has a handler")


def test_doctor() -> None:
    from . import doctor

    report = doctor.run(host=False)
    check(isinstance(report["ok"], bool), "doctor returns a verdict")
    names = {c["name"] for c in report["checks"]}
    check({"python", "ladder", "laya-checkpoint"} <= names, f"core checks present: {sorted(names)}")
    check("rlp-extensions" in names, "doctor verifies RLP's own extensions")
    check("rlp-skills" in names, "doctor verifies RLP's own skills")
    check({"providers", "ladder-arms-reachable"} <= names, f"doctor checks endpoints reachability: {sorted(names)}")
    check(all(c["status"] != "fail" for c in report["checks"] if c["name"] == "optional-extensions"),
          "a third-party optional extension is never a RLP failure")
    check(report["ok"] == (report["summary"]["fail"] == 0), "verdict agrees with the failure count")
    check(all(c["status"] in ("ok", "warn", "fail") for c in report["checks"]), "graded statuses only")
    rendered = doctor.render(report)
    check("rlp doctor" in rendered and "passed" in rendered, "the report renders")

    import os

    os.environ["RLP_ORCHESTRATION"] = "/nonexistent/orchestration.json"
    try:
        broken = doctor.run(host=False)
        check(broken["ok"] is False, "a missing ladder makes doctor fail loudly")
        check(any(c["name"] == "ladder" and c["hint"] for c in broken["checks"]), "the failure carries a fix")
        rendered = doctor.render(broken)
        check("FAIL" in rendered and "->" in rendered, "a broken report still prints the fix")
    finally:
        os.environ.pop("RLP_ORCHESTRATION", None)


def test_signals_and_hybrid_gate() -> None:
    from .triage import _apply_hybrid, signals

    check(not signals("fix the typo in the README title")["strong"], "a one-line fix is not a fan-out")
    check(not signals("add a --wc flag and a test")["strong"], "one feature plus its test is not a fan-out")
    strong = signals("add a payments module, document it, and review the diff independently")
    check(strong["strong"], f"implementation + independent review is a fan-out: {strong}")
    check(signals("do these in parallel: A and B")["fanout_cues"], "explicit fan-out cue is seen")

    # An unsure laya answer is raised by the signals...
    unsure = {"mode": "direct", "confidence": 0.2, "engine": "laya", "escalate": True}
    raised = _apply_hybrid(dict(unsure), "add X, document it, review the diff", "", "hybrid", 1.0)
    check(raised["mode"] == "orchestrate" and raised["escalate"] is False, f"hybrid raises an unsure call: {raised}")
    check(raised["engine"] == "laya+signals", "the engine records that signals made the call")
    check(raised.get("reason"), "the raise carries a reason")

    # ...a confident direct stands...
    confident = {"mode": "direct", "confidence": 0.99, "engine": "laya", "escalate": False}
    check(_apply_hybrid(dict(confident), "add X, document it, review the diff", "", "hybrid", 1.0)["mode"] == "direct",
          "a confident direct is never overridden")

    # ...a thin request stays direct even when unsure...
    check(_apply_hybrid(dict(unsure), "fix the typo", "", "hybrid", 1.0)["mode"] == "direct",
          "an unsure thin request stays direct")

    # ...and routing.gate=laya restores the old behaviour exactly.
    check(_apply_hybrid(dict(unsure), "add X, document it, review the diff", "", "laya", 1.0)["mode"] == "direct",
          "gate=laya disables the signal upgrade")


def test_intent_refinement() -> None:
    from .plan import intent

    check(intent("code") == ("code", "implement"), "plain code is implement")
    check(intent("code", "debug the auth crash") == ("debug", "implement"), "a defect code node is debug")
    check(intent("code", "add a --wc flag") == ("code", "implement"), "a feature stays code")
    check(intent("review", "debug the diff") == ("review", "review"), "a review node is never refined")
    check(intent("docs", "debug notes") == ("docs", "explore"), "docs stay explore")


def test_ladder_mutation() -> None:
    import os

    from . import orchestration as orch

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "orchestration.json"
        path.write_text(json.dumps(LADDER))
        os.environ["RLP_ORCHESTRATION"] = str(path)
        try:
            applied = orch.mutate(
                [
                    {"op": "set_brain", "model": "qwen-token-plan/qwen3.8-max"},
                    {"op": "add_arm", "worker": "pi", "model": "anthropic/claude-x", "roles": ["code"], "when": "test"},
                    {"op": "set_arm", "worker": "pi", "match": "agnes/agnes-3.0-flash", "when": "edited"},
                    {"op": "move_arm", "worker": "pi", "from": 2, "to": 0},
                    {"op": "set_worker_available", "worker": "claude_code", "available": False, "note": "test"},
                    {"op": "set_routing", "key": "gate", "value": "laya"},
                    {"op": "set_review", "crossVendor": False},
                ]
            )
            check(applied["backup"] and Path(applied["backup"]).is_file(), "a backup is written before the edit")
            ladder = applied["ladder"]
            check(ladder["brain"] == "qwen-token-plan/qwen3.8-max", "brain edited")
            check(ladder["workers"][0]["models"][0]["model"] == "anthropic/claude-x", "arm reordered to first")
            check(ladder["routing"]["gate"] == "laya", "routing knob edited")
            check(ladder["review"]["crossVendor"] is False, "review knob edited")
            check([c["id"] for c in orch.roster(ladder)] == ["pi"], "the disabled worker leaves the roster")
            check(any(a["when"] == "edited" and a["model"] == "agnes/agnes-3.0-flash"
                      for a in ladder["workers"][0]["models"]),
                  "set_arm by model ref rewrote that arm")

            # An invalid op is rejected without touching the file.
            before = path.read_text()
            try:
                orch.mutate([{"op": "set_routing", "key": "escalateBelow", "value": 5}])
                check(False, "an out-of-range routing value was accepted")
            except ValueError:
                check(True, "an invalid mutation is rejected")
            check(path.read_text() == before, "a rejected mutation leaves the file untouched")
            dry = orch.mutate([{"op": "set_brain", "model": "qwen-token-plan/x"}], dry_run=True)
            check(dry["dry_run"] is True and dry["backup"] is None, "dry_run reports and writes nothing")
            check(orch.raw_load()["brain"] != "qwen-token-plan/x", "dry_run really did not write")
        finally:
            os.environ.pop("RLP_ORCHESTRATION", None)


def test_config_cli() -> None:
    import contextlib
    import io
    import os

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "orchestration.json"
        path.write_text(json.dumps(LADDER))
        os.environ["RLP_ORCHESTRATION"] = str(path)
        try:
            from . import cli

            sink = io.StringIO()
            with contextlib.redirect_stdout(sink):
                code = cli.main(["config", json.dumps([{"op": "set_brain", "model": "qwen-token-plan/qwen3.8-max"}]), "--json"])
            check(code == 0, f"config exits 0: {code}")
            payload = json.loads(sink.getvalue())
            check(payload["ok"] and payload["result"]["ladder"]["brain"] == "qwen-token-plan/qwen3.8-max",
                  "config wrote the new brain")

            stale = io.StringIO()
            with contextlib.redirect_stdout(stale):
                bad = cli.main(["config", json.dumps([{"op": "nope"}]), "--json"])
            check(bad == 1 and json.loads(stale.getvalue())["ok"] is False, "a bad op exits 1 with an envelope")
        finally:
            os.environ.pop("RLP_ORCHESTRATION", None)


def test_credential_preflight() -> None:
    import os

    from . import plan

    with tempfile.TemporaryDirectory() as tmp:
        auth = Path(tmp) / "auth.json"
        auth.write_text(json.dumps({"agnes": {"type": "api_key", "key": "x"}, "qwen-token-plan": {"key": "y"}}))
        os.environ["RLP_PI_AUTH"] = str(auth)
        os.environ.pop("RLP_SKIP_CREDENTIAL_PREFLIGHT", None)
        try:
            check(plan.credential_state("agnes") == "present", "a provider in auth.json is present")
            check(plan.credential_state("anthropic") == "missing", "a provider absent from auth.json is missing")
            os.environ["RLP_SKIP_CREDENTIAL_PREFLIGHT"] = "1"
            check(plan.credential_state("anthropic") == "unknown", "the preflight can be disabled")
            os.environ.pop("RLP_SKIP_CREDENTIAL_PREFLIGHT", None)

            # A dead default arm is demoted in favour of a usable one.
            mixed = {
                "brain": "agnes/agnes-3.0-flash",
                "workers": [
                    {
                        "id": "pi",
                        "harness": "pi",
                        "models": [
                            {"model": "anthropic/claude-x", "roles": ["code", "review"], "when": "dead here"},
                            {"model": "agnes/agnes-3.0-flash", "roles": ["code", "review"], "when": "usable"},
                        ],
                    }
                ],
                "routing": {"escalateBelow": 0.55, "maxDispatchesPerTurn": 4},
                "review": {"crossVendor": False},
            }
            routed = with_ladder(lambda: plan.plan("x", mode="orchestrate"), ladder=mixed)["result"]
            check(all(r["credential"] == "present" for r in routed["routes"].values()),
                  f"no node planned onto a dead arm: {[r['arm'] for r in routed['routes'].values()]}")
            check(not routed["preflight"], "no preflight warning when a usable arm exists")

            dead = {
                "brain": "agnes/agnes-3.0-flash",
                "workers": [
                    {
                        "id": "pi",
                        "harness": "pi",
                        "models": [{"model": "anthropic/claude-x", "roles": ["code", "review"], "when": "dead here"}],
                    }
                ],
                "routing": {"escalateBelow": 0.55, "maxDispatchesPerTurn": 4},
                "review": {"crossVendor": False},
            }
            surfaced = with_ladder(lambda: plan.plan("x", mode="orchestrate"), ladder=dead)["result"]
            check(surfaced["preflight"] and surfaced["preflight"][0]["credential"] == "missing",
                  f"a worker with no live arm is surfaced: {surfaced.get('preflight')}")
        finally:
            os.environ.pop("RLP_PI_AUTH", None)


def test_role_bindings() -> None:
    import os

    from . import orchestration as orch
    from . import plan

    ladder = json.loads(json.dumps(LADDER))
    ladder["roles"] = {"code": "qwen-token-plan/deepseek-v4.1-flash", "review": "agnes/agnes-3.0-flash"}
    parsed = orch.parse(json.dumps(ladder), "test")
    check(parsed["roles"]["code"] == "qwen-token-plan/deepseek-v4.1-flash", "a roles map parses")
    check(orch.resolve_role(parsed, "code")["binding"] is True, "a bound role resolves as a binding")
    check(orch.resolve_role(parsed, "docs")["binding"] is False, "an unbound role falls back to arm priority")
    check("code" in orch.known_roles(parsed) and "explore" in orch.known_roles(parsed),
          f"the editor sees every planner role: {orch.known_roles(parsed)}")
    check("agnes/agnes-3.0-flash" in orch.model_pool(parsed), "the pool is every arm")

    # A binding beats arm priority and says so.
    result = with_ladder(lambda: plan.plan("x", mode="orchestrate"), ladder=ladder)["result"]
    check(result["routes"]["t1"]["arm"] == "qwen-token-plan/deepseek-v4.1-flash",
          f"binding wins over priority: {result['routes']['t1']}")
    check(result["routes"]["t1"].get("role_binding") == "qwen-token-plan/deepseek-v4.1-flash",
          "the record names the binding")
    check("role binding" in result["routes"]["t1"]["why_this_arm"], "the rationale names the binding")
    check(result["role_bindings"]["code"] == "qwen-token-plan/deepseek-v4.1-flash", "the plan carries the map")
    check(not result["binding_warnings"], f"a live binding warns nothing: {result['binding_warnings']}")

    # A chain: several models in priority order; the first dispatchable one wins.
    chained = json.loads(json.dumps(LADDER))
    chained["workers"][1]["available"] = False  # claude_code carries the first entry but cannot run
    chained["roles"] = {"review": ["anthropic/claude-opus-4-8", "qwen-token-plan/deepseek-v4.1-flash"]}
    parsed_chain = orch.parse(json.dumps(chained), "test")
    check(
        orch.role_chain(parsed_chain, "review")
        == ["anthropic/claude-opus-4-8", "qwen-token-plan/deepseek-v4.1-flash"],
        "a role bound to a list is an ordered chain",
    )
    resolved = orch.resolve_role(parsed_chain, "review")
    check(
        resolved["model"] == "qwen-token-plan/deepseek-v4.1-flash" and resolved["index"] == 1,
        f"the first dispatchable chain entry wins: {resolved}",
    )
    chained_result = with_ladder(lambda: plan.plan("x", mode="orchestrate"), ladder=chained)["result"]
    check(
        chained_result["routes"]["t3"]["arm"] == "qwen-token-plan/deepseek-v4.1-flash",
        f"the planner walks the chain: {chained_result['routes']['t3']['arm']}",
    )
    check(chained_result["routes"]["t3"].get("role_binding") == "qwen-token-plan/deepseek-v4.1-flash",
          "the chosen chain entry is recorded")
    check(not chained_result["binding_warnings"], f"a satisfiable chain warns nothing: {chained_result['binding_warnings']}")

    # A binding to a model only an unavailable worker carries is reported, not hidden.
    stranded = json.loads(json.dumps(LADDER))
    stranded["workers"][1]["available"] = False
    stranded["roles"] = {"review": "anthropic/claude-opus-4-8"}
    warned = with_ladder(lambda: plan.plan("x", mode="orchestrate"), ladder=stranded)["result"]
    check(warned["binding_warnings"] and warned["binding_warnings"][0]["role"] == "review",
          f"a stranded binding is surfaced: {warned.get('binding_warnings')}")
    check(all(r["arm"] != "anthropic/claude-opus-4-8" for r in warned["routes"].values()),
          "a stranded binding falls back instead of planning a dead model")
    check(any(r.get("binding_unavailable") for r in warned["routes"].values()),
          "the fallback node records why")

    # A binding may only name a model the ladder already carries.
    bad = json.loads(json.dumps(LADDER))
    bad["roles"] = {"code": "nowhere/not-an-arm"}
    try:
        orch.parse(json.dumps(bad), "test")
        check(False, "a binding to a non-arm model was accepted")
    except ValueError:
        check(True, "a binding must name an arm the ladder carries")

    # set_role / clear_role mutate ops.
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "orchestration.json"
        path.write_text(json.dumps(LADDER))
        os.environ["RLP_ORCHESTRATION"] = str(path)
        try:
            applied = orch.mutate([{"op": "set_role", "role": "review", "model": "qwen-token-plan/deepseek-v4.1-flash"}])
            check(applied["ladder"]["roles"]["review"] == "qwen-token-plan/deepseek-v4.1-flash", "set_role writes the map")
            try:
                orch.mutate([{"op": "set_role", "role": "code", "model": "nowhere/x"}])
                check(False, "a set_role to a non-arm model was accepted")
            except ValueError:
                check(True, "set_role rejects a model that is not an arm")
            cleared = orch.mutate([{"op": "clear_role", "role": "review"}])
            check("roles" not in cleared["ladder"] or "review" not in cleared["ladder"]["roles"],
                  "clear_role removes the binding and drops an empty map")
            chain_applied = orch.mutate(
                [{"op": "set_role", "role": "code", "models": ["agnes/agnes-3.0-flash", "qwen-token-plan/deepseek-v4.1-flash"]}]
            )
            check(
                chain_applied["ladder"]["roles"]["code"]
                == ["agnes/agnes-3.0-flash", "qwen-token-plan/deepseek-v4.1-flash"],
                "set_role with a models array stores the chain",
            )
        finally:
            os.environ.pop("RLP_ORCHESTRATION", None)


def test_planning_policy() -> None:
    from . import orchestration as orch

    base = orch.parse(json.dumps(LADDER), "test")
    check(base["rlm"]["maxDepth"] is None, "unspecified rlm knobs stay unset at parse time")
    check(base["planning"]["recursiveDepth"] == 1, "planning defaults to one recursion level")

    ladder = json.loads(json.dumps(LADDER))
    ladder["rlm"] = {"maxDepth": 2, "maxIterations": 12, "maxConcurrentSubcalls": 2, "maxBudget": 3.5}
    ladder["planning"] = {"critique": False, "maxRefines": 2, "recursiveDepth": 2, "artifactPassing": False}
    parsed = orch.parse(json.dumps(ladder), "test")
    check(parsed["rlm"]["maxDepth"] == 2 and parsed["rlm"]["maxBudget"] == 3.5, f"rlm knobs parse: {parsed['rlm']}")
    check(parsed["planning"] == {"critique": False, "maxRefines": 2, "recursiveDepth": 2, "artifactPassing": False, "verifySamples": 3},
          f"planning parses: {parsed['planning']}")

    for bad, why in [
        ({"rlm": {"maxDepth": -1}}, "negative rlm depth"),
        ({"rlm": {"maxIterations": 0}}, "zero iterations"),
        ({"rlm": {"maxBudget": 0}}, "zero budget"),
        ({"planning": {"maxRefines": 9}}, "too many refines"),
        ({"planning": {"recursiveDepth": -1}}, "negative recursion depth"),
        ({"planning": {"critique": "yes"}}, "non-boolean critique"),
    ]:
        b = json.loads(json.dumps(LADDER))
        b.update(bad)
        try:
            orch.parse(json.dumps(b), "test")
            check(False, f"ladder accepted invalid planning config: {why}")
        except ValueError:
            check(True, why)


def test_decompose_pipeline() -> None:
    from . import decompose as mod

    # RLM runs `system_prompt.format(custom_tools_section=…)` on the custom
    # prompt, so a literal brace in it raises KeyError at completion time.
    try:
        mod.RLM_SYSTEM_PROMPT.format(custom_tools_section="")
        check(True, "the RLM system prompt survives RLM's .format()")
    except Exception as e:
        check(False, f"RLM system prompt has format-placeholder braces: {e}")
    try:
        mod.CRITIC_PROMPT.format(request="r", context="", tasks="[]")
        check(True, "the critic prompt survives .format()")
    except Exception as e:
        check(False, f"critic prompt has a format-placeholder brace: {e}")
    original = (mod._policy, mod._rlm_decompose, mod.chat, mod._critique_spec)
    repaired = [dict(TASKS[0], id="t1"), dict(TASKS[1], id="t2", depends_on=["t1"])]
    try:
        mod._policy = lambda: (
            dict(mod._DEFAULT_RLM),
            {**mod._DEFAULT_PLANNING, "critique": True, "maxRefines": 1},
        )
        mod._rlm_decompose = lambda prompt, knobs: json.dumps({"tasks": [TASKS[0]]})
        mod._critique_spec = lambda: ("p", "m")

        mod.chat = lambda *a, **k: json.dumps({"verdict": "repair", "issues": ["split it"], "tasks": repaired})
        env = mod.decompose("do the thing")
        check(env["ok"] and len(env["result"]["tasks"]) == 2, f"critic repair is applied: {env}")
        check(env["result"]["critique"]["applied"] and env["result"]["critique"]["issues"] == ["split it"],
              "the critique record says what changed")

        mod.chat = lambda *a, **k: json.dumps({"verdict": "ok"})
        env = mod.decompose("do the thing")
        check(not env["result"]["critique"]["applied"], "a good DAG passes the critic unchanged")

        def boom(*a, **k):
            raise RuntimeError("critic down")

        mod.chat = boom
        env = mod.decompose("do the thing")
        check(env["ok"] and env["result"]["critique"].get("critique_error"), "a broken critic is non-fatal")

        calls: list = []
        mod.chat = lambda *a, **k: calls.append(1) or json.dumps({"verdict": "repair", "tasks": repaired})
        env = mod.decompose("orig request", focus="fix the auth bug")
        check(env["ok"] and "critique" not in env["result"], "focus mode skips the whole-request critique")
        check(not calls, "focus mode never calls the critic")
    finally:
        mod._policy, mod._rlm_decompose, mod.chat, mod._critique_spec = original


def test_replan() -> None:
    from . import plan

    result = with_ladder(lambda: plan.replan("fix the auth bug", "original request"))
    check(result["ok"] and result["result"]["recursive_depth"] >= 1, f"replan returns a sub-DAG: {result}")
    check(result["result"]["tasks"], "replan produced tasks")

    ladder = json.loads(json.dumps(LADDER))
    ladder["planning"] = {"recursiveDepth": 0}
    disabled = with_ladder(lambda: plan.replan("fix it", "orig"), ladder=ladder)
    check(disabled["ok"] is False and "recursiveDepth" in disabled["error"],
          f"recursiveDepth 0 disables replanning: {disabled}")

    empty = with_ladder(lambda: plan.replan("", "orig"))
    check(empty["ok"] is False and "focus" in empty["error"], "replan needs a focus brief")


def test_planning_mutation() -> None:
    import os

    from . import orchestration as orch

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "orchestration.json"
        path.write_text(json.dumps(LADDER))
        os.environ["RLP_ORCHESTRATION"] = str(path)
        try:
            applied = orch.mutate(
                [
                    {"op": "set_planning", "key": "critique", "value": False},
                    {"op": "set_planning", "key": "recursiveDepth", "value": 2},
                    {"op": "set_rlm", "key": "maxDepth", "value": 2},
                    {"op": "set_rlm", "key": "maxBudget", "value": 5.0},
                ]
            )
            check(applied["ladder"]["planning"]["critique"] is False, "set_planning writes critique")
            check(applied["ladder"]["planning"]["recursiveDepth"] == 2, "set_planning writes recursion depth")
            check(applied["ladder"]["rlm"]["maxDepth"] == 2 and applied["ladder"]["rlm"]["maxBudget"] == 5.0,
                  "set_rlm writes the knobs")
            cleared = orch.mutate([{"op": "set_rlm", "key": "maxBudget", "value": None}])
            check(cleared["ladder"]["rlm"]["maxBudget"] is None, "a null value clears a knob")
            check("maxBudget" not in (orch.raw_load().get("rlm") or {}), "the cleared key is gone from the file")
            for bad in (
                {"op": "set_planning", "key": "recursiveDepth", "value": 9},
                {"op": "set_rlm", "key": "maxDepth", "value": -1},
                {"op": "set_rlm", "key": "nope", "value": 1},
            ):
                try:
                    orch.mutate([bad])
                    check(False, f"invalid policy mutation accepted: {bad}")
                except ValueError:
                    check(True, f"rejected {bad}")
        finally:
            os.environ.pop("RLP_ORCHESTRATION", None)


def test_decompose_normalization() -> None:
    from . import decompose as mod

    raw = {
        "tasks": [
            {"id": "T1", "title": "a", "brief": "b", "acceptance": "c",
             "domain": "context-analysis", "size": "medium", "depends_on": []},
            {"id": "T2", "title": "d", "brief": "e", "acceptance": "f",
             "domain": "code", "size": "M", "depends_on": ["T1", "t9"]},
        ]
    }
    tasks = mod._normalize_tasks(raw)
    check([t["id"] for t in tasks] == ["t1", "t2"], "ids normalize to t<N>")
    check(tasks[0]["domain"] == "code", "an unknown domain coerces to code")
    check(tasks[0]["size"] == "M", "an unknown size coerces to M")
    check(tasks[1]["depends_on"] == ["t1"], "deps normalize and unknown deps are dropped")
    check(mod._validated(tasks)[0]["id"] == "t1", "a normalized DAG validates")

    original = (mod._policy, mod._rlm_decompose, mod._plain_llm_decompose, mod.chat, mod._critique_spec)
    try:
        mod._policy = lambda: (dict(mod._DEFAULT_RLM), {**mod._DEFAULT_PLANNING, "critique": False})
        mod._rlm_decompose = lambda prompt, knobs: json.dumps({"tasks": [{"id": "t1"}]})  # missing fields
        mod._plain_llm_decompose = lambda request, context: [dict(TASKS[0])]
        env = mod.decompose("do x")
        check(env["ok"] and env["result"]["engine"] == "fallback-plain-llm",
              f"an unusable RLM DAG falls back instead of failing: {env.get('result', {}).get('engine')}")
        check(env["result"].get("rlm_error"), "the fallback records why")

        mod._rlm_decompose = lambda prompt, knobs: (_ for _ in ()).throw(RuntimeError("rlm down"))
        env = mod.decompose("do x")
        check(env["ok"] and env["result"]["engine"] == "fallback-plain-llm", "an RLM exception falls back too")
    finally:
        mod._policy, mod._rlm_decompose, mod._plain_llm_decompose, mod.chat, mod._critique_spec = original


def test_planner_role_resolution() -> None:
    import os

    from . import decompose as mod
    from . import orchestration as orch

    base = json.loads(json.dumps(LADDER))
    check(orch.resolve_model_for_role(orch.parse(json.dumps(base), "test"), "plan") is None,
          "no plan role on any arm -> None")

    ladder = json.loads(json.dumps(LADDER))
    ladder["workers"][0]["models"][0]["roles"].append("plan")
    ladder["roles"] = {"critique": "qwen-token-plan/deepseek-v4.1-flash"}
    parsed = orch.parse(json.dumps(ladder), "test")
    check(orch.resolve_model_for_role(parsed, "plan") == "agnes/agnes-3.0-flash",
          "an arm declaring `plan` resolves")
    check(orch.resolve_model_for_role(parsed, "critique") == "qwen-token-plan/deepseek-v4.1-flash",
          "a `critique` binding resolves")

    def run():
        check(mod._role_spec("critique") == ("qwen-token-plan", "deepseek-v4.1-flash"),
              f"decompose reads the ladder critique role: {mod._role_spec('critique')}")
        check(mod._planner_spec() == ("agnes", "agnes-3.0-flash"),
              f"the planner comes from the ladder plan role: {mod._planner_spec()}")

    with_ladder(run, ladder=ladder)

    os.environ["RLP_DECOMPOSE_MODEL"] = "myprov/mymodel"
    try:
        check(mod._planner_spec() == ("myprov", "mymodel"), "RLP_DECOMPOSE_MODEL beats the ladder")
    finally:
        os.environ.pop("RLP_DECOMPOSE_MODEL", None)


def test_verify() -> None:
    from . import orchestration as orch
    from . import verify as mod

    parsed = orch.parse(json.dumps(LADDER), "test")
    check(mod.pick_verifier(parsed, "") == "agnes/agnes-3.0-flash", "no avoid family -> the first review arm")
    check(mod.pick_verifier(parsed, "agnes") == "qwen-token-plan/deepseek-v4.1-flash",
          "an agnes implementer is verified by another vendor")

    original = (mod.chat, mod._spec)
    try:
        votes = [{"pass": True, "reason": "ok", "evidence": "x"},
                 {"pass": True, "reason": "ok", "evidence": "x"},
                 {"pass": False, "reason": "missing", "evidence": "y"}]
        state = {"n": 0}

        def fake_chat(*_a, **_k):
            v = votes[state["n"] % len(votes)]
            state["n"] += 1
            return json.dumps(v)

        mod.chat = fake_chat
        mod._spec = lambda avoid: ("p", "m", "qwen-token-plan")
        env = mod.verify("t", "tests pass", "report", "evidence", "agnes", 3)
        check(env["ok"] and env["result"]["pass"] is True, f"2 of 3 passes -> pass: {env}")
        check(env["result"]["pass_count"] == 2 and env["result"]["samples"] == 3, "best-of-N counts votes")
        check(env["result"]["cross_vendor"] is True, "the verdict is marked cross-vendor")

        mod.chat = lambda *_a, **_k: json.dumps({"pass": False, "reason": "no", "evidence": "y"})
        env = mod.verify("t", "a", "", "", "agnes", 3)
        check(env["result"]["pass"] is False, "unanimous fail -> fail")

        mod.chat = lambda *_a, **_k: "no json here"
        env = mod.verify("t", "a")
        check(env["ok"] is False and "no verdict" in env["error"], f"unparseable votes -> error: {env}")

        check(mod.verify("t", "")["ok"] is False, "verify needs an acceptance sentence")
    finally:
        mod.chat, mod._spec = original


def test_memory() -> None:
    import os

    from . import memory as mem

    with tempfile.TemporaryDirectory() as tmp:
        os.environ["RLP_HOME"] = tmp
        try:
            check(mem.brief(tmp) == "", "empty memory -> no brief")
            mem.append("run pytest with -q", kind="pitfall", node="t2", cwd=tmp)
            mem.append("the repo uses ruff", kind="note", cwd=tmp)
            entries = mem.load(tmp, 10)
            check(len(entries) == 2 and entries[-1]["text"] == "the repo uses ruff", "entries append and load")
            rendered = mem.brief(tmp, 12)
            check("pytest" in rendered and "earlier RLP runs" in rendered, "the brief renders")
            summary = mem.summary(tmp, 10)
            check(summary["count"] == 2 and summary["by_kind"].get("pitfall") == 1, "summary counts by kind")
            Path(mem.knowledge_path(tmp)).open("a", encoding="utf-8").write("not json\n")
            check(len(mem.load(tmp, 10)) == 2, "corrupt lines are skipped")
            try:
                mem.append("", cwd=tmp)
                check(False, "an empty entry was accepted")
            except ValueError:
                check(True, "an empty entry is rejected")
        finally:
            os.environ.pop("RLP_HOME", None)


def test_plan_reads_memory() -> None:
    import os

    from . import memory as mem
    from . import plan

    with tempfile.TemporaryDirectory() as tmp:
        os.environ["RLP_HOME"] = tmp
        try:
            mem.append("the payments module lives in svc/pay.py", kind="note", cwd=os.getcwd())
            result = with_ladder(lambda: plan.plan("x", mode="direct"))["result"]
            check(result.get("memory_brief_chars", 0) > 0,
                  f"plan folds repo memory into context: {result.get('memory_brief_chars')}")
        finally:
            os.environ.pop("RLP_HOME", None)


def main() -> None:
    # The provider store has its own file (it is long and hermetic on its own);
    # it shares this runner's `check` contract via its own module-level list.
    from . import tests_providers

    for test in (
        test_waves,
        test_pick_arm,
        test_plan_gate_closes,
        test_gate_override,
        test_plan_orchestrates,
        test_plan_degrade_paths,
        test_ladder_validation,
        test_signals_and_hybrid_gate,
        test_intent_refinement,
        test_ladder_mutation,
        test_config_cli,
        test_credential_preflight,
        test_role_bindings,
        test_planning_policy,
        test_planning_mutation,
        test_decompose_pipeline,
        test_decompose_normalization,
        test_planner_role_resolution,
        test_replan,
        test_verify,
        test_memory,
        test_plan_reads_memory,
        test_engine_protocol,
        test_engine_covers_the_cli,
        test_availability,
        test_plan_excludes_unavailable,
        test_cli_surface,
        test_version_consistency,
        test_doctor,
        tests_providers.test_providers_store,
        tests_providers.test_providers_probe,
        tests_providers.test_providers_surface,
    ):
        print(f"— {test.__name__}")
        test()
    print()
    failures = _failures + tests_providers._failures
    if failures:
        print(f"{len(failures)} failure(s):")
        for f in failures:
            print(f"  {f}")
        sys.exit(1)
    print("all offline tests passed")


if __name__ == "__main__":
    main()
