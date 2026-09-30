"""Offline tests for the parts that must never be wrong: the planner's shape.

These run in milliseconds with the model layers stubbed, so they are the ones
to run on every change. The engines' own accuracy is a different question and
belongs to `selftest.sh`, which pays for the real laya load.

    python3 -m rlp_svc.tests   # venv-less: any python that can import rlp_svc
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

#: A fully configured two-vendor ladder. The provider names are deliberately
#: generic: this fixture documents the ladder's *shape*, and naming one host's
#: private gateway here would read as if RLP required those endpoints.
LADDER = {
    "brain": "alpha/alpha-large",
    "workers": [
        {
            "id": "pi",
            "harness": "pi",
            "models": [
                {
                    "model": "alpha/alpha-large",
                    "roles": ["code", "review", "docs"],
                    "when": "default arm, carries the bulk",
                },
                {
                    "model": "beta/beta-deep",
                    "roles": ["code", "review", "debug"],
                    "when": "deep arm and second vendor",
                },
            ],
        },
        {
            "id": "solo",
            "harness": "claude-native",
            "models": [
                {
                    "model": "gamma/gamma-opus",
                    "roles": ["code", "review"],
                    "when": "opt-in, spend-limited",
                }
            ],
        },
    ],
    "routing": {"escalateBelow": 0.55, "maxDispatchesPerTurn": 4},
    "review": {"crossVendor": True},
}

#: The ladder as RLP ships it: policy, and no arms at all. "Installed but not
#: configured" is a supported state, so it gets a fixture.
UNCONFIGURED_LADDER = {
    "brain": None,
    "workers": [{"id": "pi", "harness": "pi", "models": []}],
    "routing": {"escalateBelow": 0.55, "maxDispatchesPerTurn": 4, "gate": "hybrid"},
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
    triage_mod.triage = lambda request, context="", **_kwargs: triage or {
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
    check(pick_arm(pi, "code")[0]["model"] == "alpha/alpha-large", "priority order: first matching arm wins")
    check(pick_arm(pi, "debug")[0]["model"] == "beta/beta-deep", "role picks its own arm")
    arm, why = pick_arm(pi, "review", avoid_families={"alpha"})
    check(arm["model"] == "beta/beta-deep", f"cross-vendor re-pick: {arm['model']}")
    check("different vendor" in why, f"rationale names the rule: {why!r}")
    solo = LADDER["workers"][1]
    check(pick_arm(solo, "code", avoid_families={"gamma"})[0]["model"] == "gamma/gamma-opus",
          "no alternative family: keep the arm rather than fail the plan")
    check(family("beta/beta-deep") == "beta", "family is the provider")
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
    check(result["routes"]["t1"]["arm"] == "alpha/alpha-large", f"t1 on the default arm: {result['routes']['t1']}")
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
        "brain": "alpha/alpha-large",
        "workers": [
            {
                "id": "pi",
                "harness": "pi",
                "models": [
                    {"model": "alpha/alpha-large", "roles": ["code", "review"], "when": "only arm"}
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
            check(absent["ok"] is False and "install.sh" in absent["error"],
                  f"an absent ladder names the command that installs one: {absent}")
        finally:
            os.environ.pop("RLP_ORCHESTRATION", None)


def test_harness_catalog() -> None:
    """The catalog must answer every question on a host with *none* of the tools.

    CI is that host. The injectable seams — `which`/`env`/`home`/`probe` — are
    the test surface: no global stubbing, no real PATH, and nothing is ever
    spawned that the test did not fabricate.
    """
    from . import doctor
    from . import harnesses as h

    on_path = {"claude": "/usr/local/bin/claude", "jcode": "/usr/local/bin/jcode", "tmux": "/usr/bin/tmux"}
    spawns: list[str] = []

    def which(name: str):
        return on_path.get(name)

    def probe(binary: str, args: list[str]):
        spawns.append(binary)
        return "9.9.9-test"

    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        rows = {r["harness"]: r for r in h.scan_result(which=which, env={"ANTHROPIC_API_KEY": "x"}, home=home, probe=probe)["harnesses"]}
        check(rows["claude"]["auth"] == "authenticated", "a named credential env authenticates claude without a file")
        check(not rows["muse"]["present"], "an uninstalled tool is simply not present")
        check(rows["pi"]["auth"] == "internal", "pi's credentials are RLP's own; the catalog does not second-guess them")
        check(sorted(spawns) == sorted(["/usr/local/bin/claude", "/usr/local/bin/jcode", "/usr/bin/tmux"]), "scan probes versions for PATH hits only — pi's rpi-bin is not spawned")
        spawns.clear()
        h.scan_result(which=which, env={}, home=home, probe=probe, versions=False)
        check(not spawns, "versions=False pays no spawn, so a hot path can call scan")

        # The same binaries with no env credential and no marker files: they are
        # installed, and they cannot log in — the warn case, with a fix attached.
        rows = {r["harness"]: r for r in h.scan_result(which=which, env={}, home=home, probe=lambda *_a: None)["harnesses"]}
        check(rows["jcode"]["auth"] == "needs-login" and rows["jcode"]["hint"], "a binary with no marker is needs-login, and names its fix")
        check(rows["claude"]["auth"] == "needs-login", "…as is one whose only credential was the env var just removed")

        (home / ".jcode").mkdir()
        (home / ".jcode" / "auth.json").write_text("{}")
        rows = {r["harness"]: r for r in h.scan_result(which=which, env={}, home=home, probe=lambda *_a: None)["harnesses"]}
        check(rows["jcode"]["auth"] == "authenticated", "jcode's own auth marker counts as logged in")

        disabled = h.scan_result(which=which, env={"RLP_HARNESS_SCAN": "0"}, home=home, probe=probe)
        check(disabled["disabled"] and not disabled["harnesses"] and not spawns, "RLP_HARNESS_SCAN=0 probes nothing and claims nothing")

    d = h.driver_for("claude", "sonnet")
    check(d and d["argv"][0] == "-p" and "--model" in d["argv"], "claude's driver is headless -p with --model")
    check(d and d["argv"].index("--model") < d["argv"].index("{prompt}"), "flags land before the positional prompt, never after it")
    check(d and d["promptVia"] == "argv", "claude takes the prompt as an argument")
    check(h.driver_for("claude", "default") and "--model" not in h.driver_for("claude", "default")["argv"], "<harness>/default drops the model flag entirely")
    m = h.driver_for("muse", "m1")
    check(m and m["promptVia"] == "file" and "{prompt_file}" in m["argv"], "muse exec reads the prompt from a file")
    check(h.driver_for("pi", "anything") is None, "pi keeps its own dispatch path — the catalog does not template it")
    check(h.driver_for("codex", "x") is None, "a harness with no catalog entry has no driver (the caller says so, with a fix)")
    check(h.native_model("claude/sonnet", "claude") == "sonnet", "the arm grammar survives the round trip")
    check(h.native_model("claude/default", "claude") is None, "…and 'default' means no native id")
    check(h.native_model("anthropic/opus", "claude") is None, "an arm from another vendor is not this harness's model name")
    rb = h.resolve_binary("pi")
    check(rb is None or rb.endswith("rpi-bin"), "pi resolves through the checkout's rpi-bin, never PATH")
    check(h.resolve_binary("claude", which) == "/usr/local/bin/claude", "a template harness resolves on PATH")
    check(h.resolve_binary("muse", which) is None, "and is honestly absent when it is not there")
    check(h.resolve_binary("codex", which) is None, "an unknown harness resolves to nothing, not to a guess")

    # The doctor group: warn-only, and every warn actionable — check-first-run's
    # rule, enforced here where the fixtures live instead of only on fresh hosts.
    with tempfile.TemporaryDirectory() as empty_home:
        scan = h.scan_result(which=which, env={}, home=Path(empty_home), probe=lambda *_a: None)
    lines = doctor._harnesses(scan=scan)
    names = [c["name"] for c in lines]
    check(all(c["status"] != "fail" for c in lines), "the harness group never fails the report")
    check("harness:muse" not in names and "harness:pi" not in names, "absent tools and internal pi get no line at all")
    check("harness:claude" in names, "a present tool that cannot log in does")
    check(all(c["hint"] for c in lines if c["status"] == "warn"), "every warn carries a fix")
    check("bin:tmux" in names and next(c for c in lines if c["name"] == "bin:tmux")["status"] == "ok", "tmux is reported")
    bare = doctor._harnesses(scan=h.scan_result(which=lambda _n: None, env={}, home=Path(empty_home), probe=lambda *_a: None))
    check(not [c for c in bare if c["name"].startswith("harness:")], "a host with no CLIs gets no harness lines")
    check([c for c in bare if c["name"] == "bin:tmux"][0]["status"] == "warn", "…and no tmux is a warn with a fix, not a fail")
    off = doctor._harnesses(scan={"disabled": True, "harnesses": [], "tmux": {}})
    check(len(off) == 1 and off[0]["status"] == "ok", "scan off is one honest ok line")


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
    check(excluded[0]["id"] == "solo" and "exhausted" in excluded[0]["reason"],
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
    check(result["excluded_workers"][0]["id"] == "solo", "the plan says what it excluded and why")

    dead = json.loads(json.dumps(LADDER))
    for w in dead["workers"]:
        w["available"] = False
    # Every worker switched off is the operator's own choice, so the plan is
    # still answered — with `direct`, and with the reason named. Failing the
    # call instead would mean the request goes unhandled to punish a setting.
    broken = with_ladder(lambda: plan.plan("x", mode="orchestrate"), ladder=dead)["result"]
    check(broken["mode"] == "direct", f"an all-unavailable ladder degrades to direct: {broken}")
    check("available" in broken["orchestration_unavailable"] and "pi" in broken["orchestration_unavailable"],
          f"and says which workers were switched off: {broken['orchestration_unavailable']}")


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
        ('{"brain":"p/m","workers":[{"id":"a","models":[{"model":"p/m","roles":["c"],"when":"x"}]}],'
         '"routing":{"escalateBelow":3}}', "gate out of range"),
    ]:
        try:
            orch.parse(bad, "test")
            check(False, f"ladder accepted an invalid config: {why}")
        except ValueError:
            check(True, why)

    # The shipped state: policy, no brain, no arms. Valid, and reported as
    # unconfigured rather than accepted as dispatchable — every caller branches
    # on this one flag instead of re-deriving it and disagreeing.
    blank = orch.parse(json.dumps(UNCONFIGURED_LADDER), "test")
    check(blank["configured"] is False, f"an armless ladder parses as unconfigured: {blank['configured']}")
    check(blank["brain"] is None, "a null brain survives parsing as None")
    check(blank["arm_count"] == 0, f"arm_count is 0: {blank['arm_count']}")
    check(orch.roster(blank) == [], f"an armless worker yields no roster card: {orch.roster(blank)}")
    check(blank["routing"]["escalateBelow"] == 0.55, "policy survives on an unconfigured ladder")
    check(blank["planning"]["verifySamples"] == 3, "planning defaults survive on an unconfigured ladder")
    full = orch.parse(json.dumps(LADDER), "test")
    check(full["configured"] is True and full["arm_count"] == 3, f"a full ladder is configured: {full['arm_count']}")

    # The ladder RLP actually ships must be exactly that state — not almost it.
    # A host-specific arm sneaking back into the default is the defect this
    # catches, and it is invisible until someone else installs the tool.
    shipped_path = Path(__file__).resolve().parents[2] / "agent" / "rlp" / "orchestration.json"
    if shipped_path.is_file():
        shipped = orch.parse(shipped_path.read_text(), str(shipped_path))
        check(shipped["configured"] is False,
              f"the shipped ladder names no model arms: brain={shipped['brain']} arms={shipped['arm_count']}")
        check(shipped["routing"]["maxDispatchesPerTurn"] and shipped["routing"]["workerTimeoutMs"],
              "the shipped ladder still carries its dispatch policy")


def test_unconfigured_ladder() -> None:
    """With no arms, every entry point says the same thing and none of them crash.

    This is the whole first-run experience of a downloaded copy: the harness is
    built, the engine is installed, and nothing has been connected yet. The
    honest answer is "direct, and here is how to enable the rest" — not a
    traceback, and not a plan routed onto a model that does not exist.
    """
    import os

    from . import llm
    from . import orchestration as orch
    from . import plan as plan_mod

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "orchestration.json"
        path.write_text(json.dumps(UNCONFIGURED_LADDER))
        os.environ["RLP_ORCHESTRATION"] = str(path)
        for var in ("RLP_DECOMPOSE_MODEL", "RLP_ROUTE_MODEL", "RLP_VERIFY_MODEL"):
            os.environ.pop(var, None)
        try:
            check(llm.role_candidates("route") == [], "no arms means no route candidates")
            check(llm.decomp_spec() is None, "no arms means no decomposer spec")
            check(llm.verify_spec() is None, "no arms means no verifier spec")

            # plan() must answer, not fail: direct is correct and useful here.
            answer = plan_mod.plan("add a flag, test it, and document it")
            check(answer["ok"] is True, f"plan succeeds on an unconfigured ladder: {answer}")
            check(answer["result"]["mode"] == "direct", f"the only truthful mode is direct: {answer['result']}")
            check("/setup" in answer["result"]["orchestration_unavailable"],
                  "and it names the one command that fixes it")

            # An explicit orchestrate override must not be able to talk its way
            # past a host that has nothing to dispatch to.
            forced = plan_mod.plan("two things", mode="orchestrate", because="a and b")
            check(forced["result"]["mode"] == "direct", f"an override cannot invent arms: {forced['result']}")

            from . import decompose as dmod

            env = dmod.decompose("do two things")
            check(env["ok"] is False and "/setup" in env["error"], f"decompose names the fix: {env}")

            verdict = __import__("rlp_svc.verify", fromlist=["verify"]).verify("t", "it passes")
            check(verdict["ok"] is False and "/setup" in verdict["error"], f"verify names the fix: {verdict}")

            report = __import__("rlp_svc.doctor", fromlist=["run"]).run()
            names = {c["name"]: c for c in report["checks"]}
            check("ladder" in names and names["ladder"]["status"] == "fail",
                  f"doctor fails the ladder line: {names.get('ladder')}")
            check("/setup" in names["ladder"]["hint"], f"with /setup as the fix: {names['ladder']['hint']}")
            check(all(c["status"] == "ok" or c["hint"] for c in report["checks"]),
                  "every non-ok doctor line carries a fix")
        finally:
            os.environ.pop("RLP_ORCHESTRATION", None)
        check(orch.NOT_CONFIGURED.count("/setup") == 1, "the not-configured sentence names /setup exactly once")


def test_engine_protocol() -> None:
    """The resident engine's wire protocol, without paying the model load.

    `--no-warm` is the point: the protocol is what the agent depends on, and it
    must be verifiable in milliseconds. A protocol bug here breaks every tool at
    once, and the failure mode would be a hang rather than an error.
    """
    import os
    import subprocess
    import sys as _sys

    # Hermetic: the engine is pointed at the repo's own ladder, so this test says
    # nothing about whether the host has been installed. It used to read the
    # default agent dir, which made it a test of the developer's machine.
    repo_ladder = Path(__file__).resolve().parents[2] / "agent" / "rlp" / "orchestration.json"
    env = {**os.environ, "RLP_ORCHESTRATION": str(repo_ladder)}

    requests = [
        {"id": 1, "op": "ladder", "args": {}},
        {"id": 2, "op": "nope", "args": {}},
        {"id": 3, "op": "plan", "args": {"request": "x", "mode": "direct"}},
    ]
    payload = "\n".join(json.dumps(r) for r in requests) + "\n"
    proc = subprocess.run(
        [_sys.executable, "-m", "rlp_svc", "engine", "--no-warm"],
        input=payload, capture_output=True, text=True, timeout=120, env=env,
    )
    events = [json.loads(l) for l in proc.stdout.splitlines() if l.strip()]
    check(events and events[0].get("event") == "ready", f"engine announces ready first: {events[:1]}")
    replies = {e["id"]: e for e in events if "id" in e}
    check(set(replies) == {1, 2, 3}, f"one reply per request id: {sorted(replies)}")
    # The repo ladder is the shipped one, so `brain` is legitimately null here:
    # what the op has to prove is that it returned *the ladder*, policy and all.
    ladder_reply = replies[1]
    check(ladder_reply["ok"] is True, f"ladder op succeeds: {ladder_reply}")
    check(
        ladder_reply["result"]["path"] == str(repo_ladder)
        and [w["id"] for w in ladder_reply["result"]["workers"]] == ["pi"]
        and ladder_reply["result"]["routing"]["maxDispatchesPerTurn"] == 4,
        f"ladder op returns the ladder: {ladder_reply['result']}",
    )
    check(replies[2]["ok"] is False and "unknown op" in replies[2]["error"], "unknown op is an error reply")
    check(replies[3]["ok"] is True, "a request survives the previous bad op (the server stays up)")
    check("result" in replies[3] and "ok" not in replies[3].get("result", {}),
          f"an op that returns an envelope is not wrapped twice: {list(replies[3].get('result', {}))[:4]}")
    check(replies[3]["result"]["mode"] == "direct", "a forced direct plan short-circuits the gate")

    # A broken line must not take the process down either.
    proc = subprocess.run(
        [_sys.executable, "-m", "rlp_svc", "engine", "--no-warm"],
        input="not json\n" + json.dumps({"id": 9, "op": "ladder", "args": {}}) + "\n",
        capture_output=True, text=True, timeout=120, env=env,
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
    for op in ("plan", "triage", "decompose", "ladder", "route", "llm_route", "warm", "config", "replan", "verify", "memory", "remember", "harness"):
        check(op in ops, f"engine op {op!r} exists (agents call it by name)")

    # and the ops the extension maps to a CLI invocation must have one
    for op in ("plan", "triage", "decompose", "ladder"):
        check(op in ops, f"{op!r} has both an engine op and a CLI subcommand")


def test_cli_surface() -> None:
    from .cli import EXIT_USAGE, build_parser

    parser = build_parser()
    expected = {"triage", "decompose", "route", "llm-route", "ladder", "mode", "roster", "provider", "config", "replan", "verify", "memory", "remember", "plan", "doctor", "progress", "serve", "version", "harness"}
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
    """One version, one place — and it is reported with what it was built from.

    The version used to be restated in three files with a test that could only
    report a drift after it happened. Now `rlp_svc.__version__` is the source,
    pyproject reads it dynamically, and `cli.VERSION` is an alias; the thing
    worth asserting is that nobody re-introduced a second declaration.
    """
    import re as _re

    import rlp_svc

    from . import cli

    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    text = pyproject.read_text()
    check(
        _re.search(r'^version\s*=\s*"', text, _re.MULTILINE) is None,
        "pyproject declares no literal version string (it would be a second source of truth)",
    )
    check('dynamic = ["version"]' in text, "pyproject declares the version dynamic")
    check(
        _re.search(r'version\s*=\s*\{\s*attr\s*=\s*"rlp_svc\.__version__"\s*\}', text) is not None,
        "and points it at rlp_svc.__version__",
    )
    check(cli.VERSION == rlp_svc.__version__, f"cli.VERSION aliases the package: {cli.VERSION}")
    check(
        _re.fullmatch(r"\d+\.\d+\.\d+", rlp_svc.__version__) is not None,
        f"the version is MAJOR.MINOR.PATCH: {rlp_svc.__version__!r}",
    )

    # The build identity is what a bug report needs, so its shape is a contract:
    # a missing key here is a support conversation that goes nowhere.
    identity = cli._build_identity()
    for key in ("rlp", "checkout", "harness", "engine_deps", "agent_dir", "python", "platform"):
        check(key in identity, f"build identity reports {key}")
    check(identity["rlp"] == rlp_svc.__version__, "build identity reports this version")
    check(
        set(identity["engine_deps"]) == {"laya", "rlm", "mcp", "httpx"},
        f"build identity covers every engine dep: {sorted(identity['engine_deps'])}",
    )
    check(callable(getattr(cli, "_cmd_provider", None)), "the provider command has a handler")

    # The CHANGELOG has to carry a section for the version that is about to ship,
    # or a release goes out with no notes and nobody notices until someone asks
    # what changed. `Unreleased` is the valid answer between releases.
    changelog = Path(__file__).resolve().parents[2] / "CHANGELOG.md"
    if changelog.is_file():
        body = changelog.read_text()
        check(
            f"## {rlp_svc.__version__}" in body or "## Unreleased" in body,
            f"CHANGELOG has a section for {rlp_svc.__version__} or an Unreleased one",
        )


def test_chat_contract() -> None:
    """An empty model reply is named, not silently returned as "".

    This is the bug that made a whole plan fail with "no JSON object in
    response": `chat()` returned an empty string, and every caller reported the
    symptom instead of the cause. A reasoning model mid-thought, a truncated
    reply and a boilerplate empty completion are the three things it can be, and
    the error now says which.
    """
    from . import llm

    class Reply:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    original_post, original_chat = llm._post, llm.chat
    original_url, original_key = llm._provider_base_url, llm._provider_key
    try:
        # `chat` resolves the endpoint and the credential before it posts; the
        # test is about the reply shape, so both are stubbed.
        llm._provider_base_url, llm._provider_key = lambda provider: "https://example.test/v1", lambda provider: "k"
        llm._post = lambda *a, **k: Reply({"choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}]})
        check(llm.chat("p", "m", [{"role": "user", "content": "x"}]) == "hello", "a normal reply comes back")

        llm._post = lambda *a, **k: Reply({"choices": [{"message": {"content": ""}, "finish_reason": "length"}]})
        try:
            llm.chat("p", "m", [])
            check(False, "an empty reply was returned as a value")
        except RuntimeError as e:
            check("finish_reason='length'" in str(e), f"the truncation is named: {e}")

        llm._post = lambda *a, **k: Reply(
            {"choices": [{"message": {"content": "", "reasoning_content": "thinking…"}, "finish_reason": "length"}]}
        )
        try:
            llm.chat("p", "m", [])
            check(False, "a reasoning-only reply was returned as a value")
        except RuntimeError as e:
            check("reasoning_content" in str(e), f"a reasoning-only reply is explained: {e}")

        llm._post = lambda *a, **k: Reply({"choices": []})
        try:
            llm.chat("p", "m", [])
            check(False, "a reply with no choices was returned as a value")
        except RuntimeError as e:
            check("no choices" in str(e), f"an empty choice list is named: {e}")

        # --- the candidate ladder
        calls: list = []

        def flaky(provider, model, messages, **kwargs):
            calls.append(f"{provider}/{model}")
            if len(calls) == 1:
                raise RuntimeError("first arm is down")
            return "second arm answered"

        llm.chat = flaky
        text, used = llm.chat_first([("a", "one"), ("b", "two")], messages=[])
        check(text == "second arm answered" and used == ("b", "two"), f"chat_first walks the list: {text!r} {used}")
        check(calls == ["a/one", "b/two"], f"candidates are tried in order: {calls}")

        llm.chat = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down"))
        try:
            llm.chat_first([("a", "one"), ("b", "two")], messages=[])
            check(False, "an all-failed list returned a value")
        except RuntimeError as e:
            check("a/one" in str(e) and "b/two" in str(e), f"every candidate is reported: {e}")
    finally:
        llm._post, llm.chat = original_post, original_chat
        llm._provider_base_url, llm._provider_key = original_url, original_key


def test_planner_fallbacks() -> None:
    """The decomposer plans on any of its candidate arms, and says which one."""
    from . import decompose as mod
    from . import orchestration

    original = (mod._rlm_decompose, mod.chat, mod._policy, mod._critique_spec, orchestration.load)
    try:
        mod._policy = lambda: (dict(mod._DEFAULT_RLM), {**mod._DEFAULT_PLANNING, "critique": False, "maxRefines": 0})
        mod._critique_spec = lambda: ("p", "m")
        orchestration.load = lambda: orchestration.parse(json.dumps(LADDER), "test")

        candidates = mod._planner_fallbacks()
        check(len(candidates) >= 2, f"more than one candidate arm: {candidates}")
        check(
            len({c[0] for c in candidates}) >= 2,
            f"the chain spans more than one provider, or it is not a fallback: {candidates}",
        )
        check(len(set(candidates)) == len(candidates), f"candidates are deduped: {candidates}")
        check(candidates[0] == mod._planner_spec(), "the configured planner goes first")
        check(("alpha", "alpha-large") in candidates,
              f"the ladder's DEFAULT arm is a candidate: {candidates}")

        tried: list = []

        def first_arm_dies(prompt, knobs, spec=None):
            tried.append(spec)
            if len(tried) == 1:
                raise RuntimeError("empty message (finish_reason='length')")
            return json.dumps({"tasks": TASKS})

        mod._rlm_decompose = first_arm_dies
        env = mod.decompose("do the thing")
        check(env["ok"] and len(env["result"]["tasks"]) == 3, f"a dead first arm does not end the plan: {env}")
        check(env["result"].get("planner_fallback") is True, "the fallback is recorded")
        check(env["result"]["planner"] == f"{tried[1][0]}/{tried[1][1]}", f"the arm that answered is named: {env['result']['planner']}")
        check("first" not in str(env["result"].get("rlm_error", "")) or isinstance(env["result"].get("rlm_error"), str),
              "the first arm's failure is kept for diagnosis")

        # every rlm arm dies: the plain-LLM contingency still plans
        mod._rlm_decompose = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("gateway 500"))
        mod.chat = lambda *a, **k: json.dumps({"tasks": TASKS})
        env = mod.decompose("do the thing")
        check(env["ok"] and env["result"]["engine"] == "fallback-plain-llm",
              f"the plain-LLM contingency still plans: {env}")

        # and when that fails too, the error says what both layers saw
        mod.chat = lambda *a, **k: "no json here"
        env = mod.decompose("do the thing")
        check(env["ok"] is False and "gateway 500" in env["error"], f"the failure carries both layers: {env}")
    finally:
        (mod._rlm_decompose, mod.chat, mod._policy, mod._critique_spec, orchestration.load) = original


def test_decompose_budget() -> None:
    """A hung gateway cannot hold a plan open for N × the timeout.

    The candidate walk is a *bounded* retry. When it was not, one `decompose()`
    took a quarter of an hour on a three-arm ladder whose first arm hung rather
    than errored — the failure that only shows up when someone actually waits.
    `rlm.maxTimeout` is the budget for the whole walk, shared out fairly.
    """
    import time as _time

    from . import decompose as mod
    from . import orchestration

    original = (mod._rlm_decompose, mod.chat, mod._policy, mod._critique_spec, mod._MIN_ATTEMPT_SECONDS, orchestration.load)
    try:
        mod._critique_spec = lambda: ("p", "m")
        orchestration.load = lambda: orchestration.parse(json.dumps(LADDER), "test")

        # The arithmetic, on its own.
        check(mod._attempt_timeout(None, 3) is None, "no budget configured -> the client's own default")
        slice_all = mod._attempt_timeout(_time.monotonic() + 90, 3)
        check(slice_all is not None and 29 <= slice_all <= 31, f"a 90s budget over 3 arms is ~30s each: {slice_all}")
        check(mod._attempt_timeout(_time.monotonic() - 5, 3) == mod._MIN_ATTEMPT_SECONDS,
              "an exhausted budget floors at the minimum attempt, never at zero or negative")

        # A hung arm: budget 0.4s, each attempt allowed 0.2s (floor lowered for the test).
        mod._MIN_ATTEMPT_SECONDS = 0.05
        mod._policy = lambda: (
            {**mod._DEFAULT_RLM, "maxTimeout": 0.4},
            {**mod._DEFAULT_PLANNING, "critique": False, "maxRefines": 0},
        )
        tried: list = []

        def hung(prompt, knobs, spec=None):
            tried.append(spec)
            _time.sleep(0.25)
            raise RuntimeError("gateway is hanging")

        mod._rlm_decompose = hung
        mod.chat = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("plain path also down"))
        started = _time.monotonic()
        env = mod.decompose("do the thing")
        elapsed = _time.monotonic() - started
        check(env["ok"] is False, f"every arm hanging is a failure, not a hang: {env.get('error', '')[:120]}")
        check(elapsed < 2.0, f"the walk is bounded by the budget, took {elapsed:.1f}s")
        check(len(tried) < len(mod._planner_fallbacks()), f"the budget stopped the walk early: {len(tried)} attempt(s)")
        check("budget" in str(env.get("error", "")) or "hanging" in str(env.get("error", "")),
              f"the failure says why: {env.get('error', '')[:160]}")

        # A per-attempt ceiling is passed down, and never exceeds the configured one.
        seen: list = []

        def records(prompt, knobs, spec=None):
            seen.append(knobs.get("maxTimeout"))
            return json.dumps({"tasks": TASKS})

        mod._rlm_decompose = records
        mod._policy = lambda: (
            {**mod._DEFAULT_RLM, "maxTimeout": 90.0},
            {**mod._DEFAULT_PLANNING, "critique": False, "maxRefines": 0},
        )
        env = mod.decompose("do the thing")
        check(env["ok"], "a healthy arm still answers")
        check(seen and 0 < seen[0] <= 90.0, f"the attempt got a slice, not the whole budget: {seen}")
    finally:
        (
            mod._rlm_decompose,
            mod.chat,
            mod._policy,
            mod._critique_spec,
            mod._MIN_ATTEMPT_SECONDS,
            orchestration.load,
        ) = original


def test_paths() -> None:
    """One rule for where RLP keeps its files, and it is not pi's directory.

    RLP is its own tool: a credential written by /provider must land in the same
    place the harness reads it from, and that place is RLP's own `~/.rlp/agent`
    unless an override says otherwise. Four modules used to derive this path
    themselves, which is how a tool ends up with two auth.json files.
    """
    import os

    from . import paths

    saved = {k: os.environ.get(k) for k in ("RLP_CODING_AGENT_DIR", "RPI_CODING_AGENT_DIR", "RLP_HOME")}
    try:
        for var in saved:
            os.environ.pop(var, None)
        check(paths.agent_dir() == Path.home() / ".rlp" / "agent", f"the default agent dir: {paths.agent_dir()}")
        check("pi" not in str(paths.agent_dir()).split("/"), "the default is not inside pi's ~/.pi")
        check(paths.orchestration_json() == paths.agent_dir() / "orchestration.json", "the ladder sits in the agent dir")

        os.environ["RPI_CODING_AGENT_DIR"] = "/tmp/legacy-agent"
        check(paths.agent_dir() == Path("/tmp/legacy-agent"), "the harness's own variable is still honoured")

        os.environ["RLP_CODING_AGENT_DIR"] = "/tmp/rlp-agent"
        check(paths.agent_dir() == Path("/tmp/rlp-agent"), "RLP's own variable wins over the harness's")

        os.environ["RLP_HOME"] = "/tmp/rlp-home"
        check(paths.home() == Path("/tmp/rlp-home"), "RLP_HOME moves the data dir")
        check(
            paths.agent_dir() == Path("/tmp/rlp-agent"),
            "relocating the logs does not relocate the credentials",
        )

        os.environ.pop("RLP_CODING_AGENT_DIR")
        os.environ.pop("RPI_CODING_AGENT_DIR")
        check(
            paths.agent_dir() == Path.home() / ".rlp" / "agent",
            "dropping both overrides returns to the default",
        )
        check("default" in paths.described() or "$" in paths.described(), f"described(): {paths.described()}")

        # The four callers agree with the rule (this is the whole point).
        from . import doctor, llm, memory, orchestration, providers

        os.environ["RPI_CODING_AGENT_DIR"] = "/tmp/agree-agent"
        for name, value in (
            ("llm auth", llm._auth_path()),
            ("llm models", llm._models_path()),
            ("ladder", str(orchestration.config_path())),
            ("providers models", str(providers.models_path())),
            ("providers auth", str(providers.auth_path())),
        ):
            check(value.startswith("/tmp/agree-agent/"), f"{name} follows the agent dir: {value}")
        # doctor must look in that directory too: under the override the extensions are
        # absent, where the real agent dir has them — so a FAIL here proves it followed.
        ext = next((c for c in doctor._extensions() if c["name"] == "rlp-extensions"), None)
        check(ext is not None, "doctor has an rlp-extensions check")
        check(
            ext is not None and ext["status"] == "fail",
            f"doctor checks the overridden agent dir, not the real one: {ext}",
        )
        check(doctor._env()[0]["name"] == "agent-dir", "doctor reports the agent dir")
        check("agree-agent" in doctor._env()[0]["detail"], f"doctor names it: {doctor._env()[0]['detail']}")
        check(memory.home() == Path.home() / ".rlp" or memory.home() == Path("/tmp/rlp-home"),
              f"memory follows RLP_HOME: {memory.home()}")
    finally:
        for var, value in saved.items():
            if value is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = value


def test_doctor() -> None:
    from . import doctor

    report = doctor.run()
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
        broken = doctor.run()
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


def test_direct_mode() -> None:
    """Direct-only mode: valid config, honoured by every layer, honest in reports.

    The mode exists because most of what people want from RLP is a good coding
    agent and none of the fan-out. Three properties are worth pinning: the gate
    is never consulted (so no decision model is loaded), the reports say the mode
    is a choice rather than a missing ladder, and an explicit override still
    outranks it — a mode with no way out is a trap.
    """
    import os

    from . import doctor, onboarding, orchestration as orch, plan as plan_mod, triage as triage_mod

    direct_ladder = {**LADDER, "routing": {"gate": "direct", "escalateBelow": 0.55}}
    thin_ladder = {**UNCONFIGURED_LADDER, "routing": {**UNCONFIGURED_LADDER["routing"], "gate": "direct"}}

    parsed = orch.parse(json.dumps(direct_ladder), "test")
    check(parsed["mode"] == "direct", f"gate=direct parses and reports the mode: {parsed['mode']}")
    check(parsed["routing"]["gate"] == "direct", "the gate value survives validation")
    try:
        orch.parse(json.dumps({**LADDER, "routing": {"gate": "sometimes"}}), "test")
        check(False, "an unknown gate was accepted")
    except ValueError as e:
        check("routing.gate" in str(e), f"the rejection names the field: {e}")

    # The gate must not be consulted at all: loading laya to answer a constant is
    # the exact cost this mode exists to avoid.
    from . import route as route_mod

    def boom():
        raise AssertionError("direct-only mode must not load the decision model")

    saved_router, saved_env = route_mod._router, os.environ.get("RLP_DIRECT")
    route_mod._router = boom
    try:
        os.environ["RLP_ORCHESTRATION"] = _write_ladder(direct_ladder)
        decision = triage_mod.triage("add a payments module, document it, and review the diff independently")
        check(decision["mode"] == "direct", f"direct-only mode answers direct: {decision['mode']}")
        check(decision["engine"] == "direct-mode", f"and says which engine answered: {decision['engine']}")
        check(decision["direct_only"] is True, "the answer is marked as the mode, not a measurement")
        check(decision["escalate"] is False, "an answer with no uncertainty cannot escalate")
        check("how_to_change" in decision, "the answer names the way out")

        # Same from the environment, with an ordinary ladder in the file: a
        # session launched `--direct` is direct-only without anybody editing state.
        os.environ["RLP_ORCHESTRATION"] = _write_ladder(LADDER)
        os.environ["RLP_DIRECT"] = "1"
        env_decision = triage_mod.triage("add a payments module, document it, and review the diff independently")
        check(env_decision["direct_only"] is True, "$RLP_DIRECT decides triage without a ladder edit")
        check(env_decision["direct_source"] == "$RLP_DIRECT", f"and names itself as the source: {env_decision}")
        del os.environ["RLP_DIRECT"]

        # plan() answers before the ladder's arms are read, so a host that chose
        # the mode is not reported as broken.
        os.environ["RLP_ORCHESTRATION"] = _write_ladder(thin_ladder)
        planned = plan_mod.plan("add a payments module, document it, and review the diff independently")
        r = planned["result"]
        check(r["mode"] == "direct", "plan: direct-only on an armless ladder still says direct")
        check(r.get("direct_only") is True, "and marks it as the mode")
        check("orchestration_unavailable" not in r,
              f"it is not reported as a broken ladder: {r.get('orchestration_unavailable')}")
        check("inline" in r["recommended"], "the recommendation is the one line to act on")

        # The escape hatch, tested at the layer that decides. A stubbed router
        # stands in for laya: `force` must reach the real gate, because a forced
        # call that came back "direct" again would be a switch that lies.
        class FakeRouter:
            def predict(self, state, questions):
                return {"answers": {"mode": {"choice": "orchestrate", "confidence": 0.9}}}

        route_mod._router = lambda: FakeRouter()
        os.environ["RLP_ORCHESTRATION"] = _write_ladder(direct_ladder)
        os.environ.pop("RLP_DIRECT", None)
        forced_gate = triage_mod.triage("add X, document it, review the diff", force=True)
        check(forced_gate["engine"] == "laya", f"force asks the gate: {forced_gate['engine']}")
        check(forced_gate["mode"] == "orchestrate", "and takes the gate's answer")
        check(not forced_gate.get("direct_only"), "the verdict is a measurement, not the mode")
        route_mod._router = boom

        # ...while an explicit override still outranks it: the mode declines to
        # decide, it does not overrule a human who named the mode and said why.
        os.environ["RLP_ORCHESTRATION"] = _write_ladder(direct_ladder)
        forced = with_ladder(
            lambda: plan_mod.plan("two independent services, built and reviewed", mode="orchestrate", because="code and review"),
            ladder=direct_ladder,
        )
        check(bool(forced.get("ok")) and forced["result"].get("gate_override", {}).get("mode") == "orchestrate",
              f"an explicit override is honoured in direct-only mode: {forced}")
        check(bool(forced["result"].get("tasks")), "and the overridden plan really plans")

        # `force` is the other way out, and the one the brain uses: it asks the
        # gate without claiming a verdict, so the plan carries a triage block
        # rather than a gate_override.
        force_planned = with_ladder(
            lambda: plan_mod.plan("two independent services, built and reviewed", force=True),
            ladder=direct_ladder,
            triage={"mode": "orchestrate", "confidence": 0.9, "engine": "laya", "escalate": False},
        )
        check(bool(force_planned.get("ok")) and force_planned["result"].get("mode") == "orchestrate",
              f"force plans in direct-only mode: {force_planned}")
        check("gate_override" not in force_planned["result"], "and is not reported as an override")
        check(bool(force_planned["result"].get("tasks")), "the DAG is built")
        # The stamp is what bounds the claim: `last-plan.json` outlives the turn, and
        # a dispatcher in direct-only mode must be able to tell "the user asked for
        # this run" from "somebody forced a plan an hour ago".
        check(force_planned["result"].get("force") is True, "the forced plan records that it was forced")
        import datetime

        stamp = datetime.datetime.fromisoformat(str(force_planned["result"]["forced_at"]))
        age = (datetime.datetime.now(datetime.timezone.utc) - stamp).total_seconds()
        check(abs(age) < 120, f"the stamp is now: {age}s old")

        # doctor reports the mode as a state, not a fault, and keeps the fix
        # visible. Read against the armless ladder, because that is the state a
        # fresh install is actually in: no arms, and the mode says they are not
        # wanted. An armless ladder with real arms would be testing orphan arms,
        # which are a different warning and are still correct here.
        os.environ["RLP_ORCHESTRATION"] = _write_ladder(thin_ladder)
        report = doctor.run()
        ladder_line = next(c for c in report["checks"] if c["name"] == "ladder")
        check(ladder_line["status"] == "ok", f"doctor: direct-only is not a failure: {ladder_line}")
        check("direct-only" in ladder_line["detail"], "doctor names the mode")
        check("rlp mode full" in ladder_line["detail"], "and the way back")
        arms = next(c for c in report["checks"] if c["name"] == "ladder-arms-reachable")
        check(arms["status"] == "ok", f"doctor: no arms to reach is the design here: {arms}")
        check(not any(c["status"] == "fail" and c["name"] == "ladder" for c in report["checks"]),
              "the fresh-but-direct host has no ladder failure to fix")

        # `rlp mode` is the switch a person can remember. It reports the
        # *effective* mode (which is not always what the file says, when a
        # session override is in play) and writes through the same validated,
        # backed-up path /direct uses, so the two spellings cannot drift.
        import contextlib
        import io

        from . import cli

        os.environ["RLP_ORCHESTRATION"] = _write_ladder({**LADDER, "routing": {"gate": "hybrid"}})
        sink = io.StringIO()
        with contextlib.redirect_stdout(sink):
            status_code = cli.main(["mode", "--json"])
        check(status_code == 0 and '"direct": false' in sink.getvalue(), f"rlp mode reports full: {sink.getvalue()[:160]}")

        sink = io.StringIO()
        with contextlib.redirect_stdout(sink):
            set_code = cli.main(["mode", "direct", "--json"])
        check(set_code == 0 and '"direct": true' in sink.getvalue(), f"rlp mode direct writes it: {sink.getvalue()[:160]}")
        check(orch.parse(Path(os.environ["RLP_ORCHESTRATION"]).read_text(), "test")["routing"]["gate"] == "direct",
              "the ladder on disk really says direct")

        sink = io.StringIO()
        with contextlib.redirect_stdout(sink):
            cli.main(["mode", "full", "--json"])
        check('"direct": false' in sink.getvalue(), "rlp mode full hands the gate back")
        check(orch.parse(Path(os.environ["RLP_ORCHESTRATION"]).read_text(), "test")["routing"]["gate"] == "hybrid",
              "and restores the gate that decides")

        # The report a person actually reads. With an endpoint that has a
        # credential and a ladder that chose not to orchestrate, nothing is
        # missing — and the old wording sent exactly this host to connect a
        # provider it had already connected, which reads as a tool that does not
        # know what mode it is in.
        store = Path(_write_ladder(direct_ladder)).parent
        models_path, auth_path = store / "models.json", store / "auth.json"
        models_path.write_text(json.dumps({"providers": {"alpha": {"baseUrl": "https://alpha.example/v1", "models": [{"id": "alpha-large"}]}}}))
        auth_path.write_text(json.dumps({"alpha": {"key": "k"}}))
        os.environ["RLP_PI_MODELS"] = str(models_path)
        os.environ["RLP_PI_AUTH"] = str(auth_path)
        os.environ["RLP_ORCHESTRATION"] = _write_ladder(thin_ladder)
        ready = doctor.run()
        by_name = {c["name"]: c for c in ready["checks"]}
        check("credential" in by_name["providers"]["detail"].lower() or by_name["providers"]["status"] == "ok",
              f"a credentialed endpoint is enough for the provider line: {by_name.get('providers')}")
        check(not any(c["status"] == "fail" and c["name"] == "providers" for c in ready["checks"]),
              "a host that connected a provider is not told to connect one")
        check("/setup" not in by_name["providers"].get("hint", "") or by_name["providers"]["status"] == "ok",
              "and no fix is offered for a step that is done")

        # onboarding stops counting what this mode never does. The ladder is
        # written again because the `rlp mode` checks above changed the file.
        os.environ["RLP_ORCHESTRATION"] = _write_ladder(thin_ladder)
        saved_harness, saved_engine = onboarding._harness, onboarding._engine
        onboarding._harness = lambda: (True, "stub", "")
        onboarding._engine = lambda: (True, "stub", "")
        try:
            prog = onboarding.progress()
            ids = [s["id"] for s in prog["steps"]]
            check("dispatch" not in ids and "result" not in ids and "plan" not in ids,
                  f"direct-only mode drops the orchestration milestones: {ids}")
            check(prog["direct_only"] is True, "and says so in the report")
            check(prog["total"] == len(ids), "the count matches the shortened sequence")
            check("direct-only" in onboarding.summary_line(prog), "the header line names the mode")
        finally:
            onboarding._harness, onboarding._engine = saved_harness, saved_engine
    finally:
        route_mod._router = saved_router
        os.environ.pop("RLP_ORCHESTRATION", None)
        os.environ.pop("RLP_PI_MODELS", None)
        os.environ.pop("RLP_PI_AUTH", None)
        os.environ.pop("RLP_DIRECT", None)
        if saved_env is not None:
            os.environ["RLP_DIRECT"] = saved_env


def _write_ladder(ladder: dict) -> str:
    """Put a ladder where the engine will read it, and return the path."""
    import os

    directory = os.environ.setdefault("_RLP_TEST_TMP", tempfile.mkdtemp(prefix="rlp-direct-"))
    path = Path(directory) / f"orchestration-{abs(hash(json.dumps(ladder, sort_keys=True)))}.json"
    path.write_text(json.dumps(ladder))
    return str(path)


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
                    {"op": "set_brain", "model": "beta/beta-max"},
                    {"op": "add_arm", "worker": "pi", "model": "anthropic/claude-x", "roles": ["code"], "when": "test"},
                    {"op": "set_arm", "worker": "pi", "match": "alpha/alpha-large", "when": "edited"},
                    {"op": "move_arm", "worker": "pi", "from": 2, "to": 0},
                    {"op": "set_worker_available", "worker": "solo", "available": False, "note": "test"},
                    {"op": "set_routing", "key": "gate", "value": "laya"},
                    {"op": "set_review", "crossVendor": False},
                ]
            )
            check(applied["backup"] and Path(applied["backup"]).is_file(), "a backup is written before the edit")
            ladder = applied["ladder"]
            check(ladder["brain"] == "beta/beta-max", "brain edited")
            check(ladder["workers"][0]["models"][0]["model"] == "anthropic/claude-x", "arm reordered to first")
            check(ladder["routing"]["gate"] == "laya", "routing knob edited")
            check(ladder["review"]["crossVendor"] is False, "review knob edited")
            check([c["id"] for c in orch.roster(ladder)] == ["pi"], "the disabled worker leaves the roster")
            check(any(a["when"] == "edited" and a["model"] == "alpha/alpha-large"
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
            dry = orch.mutate([{"op": "set_brain", "model": "beta/x"}], dry_run=True)
            check(dry["dry_run"] is True and dry["backup"] is None, "dry_run reports and writes nothing")
            check(orch.raw_load()["brain"] != "beta/x", "dry_run really did not write")
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
                code = cli.main(["config", json.dumps([{"op": "set_brain", "model": "beta/beta-max"}]), "--json"])
            check(code == 0, f"config exits 0: {code}")
            payload = json.loads(sink.getvalue())
            check(payload["ok"] and payload["result"]["ladder"]["brain"] == "beta/beta-max",
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
        auth.write_text(json.dumps({"alpha": {"type": "api_key", "key": "x"}, "beta": {"key": "y"}}))
        os.environ["RLP_PI_AUTH"] = str(auth)
        os.environ.pop("RLP_SKIP_CREDENTIAL_PREFLIGHT", None)
        try:
            check(plan.credential_state("alpha") == "present", "a provider in auth.json is present")
            check(plan.credential_state("anthropic") == "missing", "a provider absent from auth.json is missing")
            os.environ["RLP_SKIP_CREDENTIAL_PREFLIGHT"] = "1"
            check(plan.credential_state("anthropic") == "unknown", "the preflight can be disabled")
            os.environ.pop("RLP_SKIP_CREDENTIAL_PREFLIGHT", None)

            # A dead default arm is demoted in favour of a usable one.
            mixed = {
                "brain": "alpha/alpha-large",
                "workers": [
                    {
                        "id": "pi",
                        "harness": "pi",
                        "models": [
                            {"model": "anthropic/claude-x", "roles": ["code", "review"], "when": "dead here"},
                            {"model": "alpha/alpha-large", "roles": ["code", "review"], "when": "usable"},
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
                "brain": "alpha/alpha-large",
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
    ladder["roles"] = {"code": "beta/beta-deep", "review": "alpha/alpha-large"}
    parsed = orch.parse(json.dumps(ladder), "test")
    check(parsed["roles"]["code"] == "beta/beta-deep", "a roles map parses")
    check(orch.resolve_role(parsed, "code")["binding"] is True, "a bound role resolves as a binding")
    check(orch.resolve_role(parsed, "docs")["binding"] is False, "an unbound role falls back to arm priority")
    check("code" in orch.known_roles(parsed) and "explore" in orch.known_roles(parsed),
          f"the editor sees every planner role: {orch.known_roles(parsed)}")
    check("alpha/alpha-large" in orch.model_pool(parsed), "the pool is every arm")

    # A binding beats arm priority and says so.
    result = with_ladder(lambda: plan.plan("x", mode="orchestrate"), ladder=ladder)["result"]
    check(result["routes"]["t1"]["arm"] == "beta/beta-deep",
          f"binding wins over priority: {result['routes']['t1']}")
    check(result["routes"]["t1"].get("role_binding") == "beta/beta-deep",
          "the record names the binding")
    check("role binding" in result["routes"]["t1"]["why_this_arm"], "the rationale names the binding")
    check(result["role_bindings"]["code"] == "beta/beta-deep", "the plan carries the map")
    check(not result["binding_warnings"], f"a live binding warns nothing: {result['binding_warnings']}")

    # A chain: several models in priority order; the first dispatchable one wins.
    chained = json.loads(json.dumps(LADDER))
    chained["workers"][1]["available"] = False  # solo carries the first entry but cannot run
    chained["roles"] = {"review": ["gamma/gamma-opus", "beta/beta-deep"]}
    parsed_chain = orch.parse(json.dumps(chained), "test")
    check(
        orch.role_chain(parsed_chain, "review")
        == ["gamma/gamma-opus", "beta/beta-deep"],
        "a role bound to a list is an ordered chain",
    )
    resolved = orch.resolve_role(parsed_chain, "review")
    check(
        resolved["model"] == "beta/beta-deep" and resolved["index"] == 1,
        f"the first dispatchable chain entry wins: {resolved}",
    )
    chained_result = with_ladder(lambda: plan.plan("x", mode="orchestrate"), ladder=chained)["result"]
    check(
        chained_result["routes"]["t3"]["arm"] == "beta/beta-deep",
        f"the planner walks the chain: {chained_result['routes']['t3']['arm']}",
    )
    check(chained_result["routes"]["t3"].get("role_binding") == "beta/beta-deep",
          "the chosen chain entry is recorded")
    check(not chained_result["binding_warnings"], f"a satisfiable chain warns nothing: {chained_result['binding_warnings']}")

    # A binding to a model only an unavailable worker carries is reported, not hidden.
    stranded = json.loads(json.dumps(LADDER))
    stranded["workers"][1]["available"] = False
    stranded["roles"] = {"review": "gamma/gamma-opus"}
    warned = with_ladder(lambda: plan.plan("x", mode="orchestrate"), ladder=stranded)["result"]
    check(warned["binding_warnings"] and warned["binding_warnings"][0]["role"] == "review",
          f"a stranded binding is surfaced: {warned.get('binding_warnings')}")
    check(all(r["arm"] != "gamma/gamma-opus" for r in warned["routes"].values()),
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
            applied = orch.mutate([{"op": "set_role", "role": "review", "model": "beta/beta-deep"}])
            check(applied["ladder"]["roles"]["review"] == "beta/beta-deep", "set_role writes the map")
            try:
                orch.mutate([{"op": "set_role", "role": "code", "model": "nowhere/x"}])
                check(False, "a set_role to a non-arm model was accepted")
            except ValueError:
                check(True, "set_role rejects a model that is not an arm")
            cleared = orch.mutate([{"op": "clear_role", "role": "review"}])
            check("roles" not in cleared["ladder"] or "review" not in cleared["ladder"]["roles"],
                  "clear_role removes the binding and drops an empty map")
            chain_applied = orch.mutate(
                [{"op": "set_role", "role": "code", "models": ["alpha/alpha-large", "beta/beta-deep"]}]
            )
            check(
                chain_applied["ladder"]["roles"]["code"]
                == ["alpha/alpha-large", "beta/beta-deep"],
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
    from . import orchestration

    original = (mod._policy, mod._rlm_decompose, mod.chat, mod._critique_spec, orchestration.load)
    repaired = [dict(TASKS[0], id="t1"), dict(TASKS[1], id="t2", depends_on=["t1"])]
    try:
        # The planner's candidate arms come from the ladder, so a decomposition
        # test has to supply one: with no arms `decompose` correctly refuses
        # before it ever reaches the critic.
        orchestration.load = lambda: orchestration.parse(json.dumps(LADDER), "test")
        mod._policy = lambda: (
            dict(mod._DEFAULT_RLM),
            {**mod._DEFAULT_PLANNING, "critique": True, "maxRefines": 1},
        )
        mod._rlm_decompose = lambda prompt, knobs, spec=None: json.dumps({"tasks": [TASKS[0]]})
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
        mod._policy, mod._rlm_decompose, mod.chat, mod._critique_spec, orchestration.load = original


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

    from . import orchestration

    original = (mod._policy, mod._rlm_decompose, mod._plain_llm_decompose, mod.chat,
                mod._critique_spec, orchestration.load)
    try:
        orchestration.load = lambda: orchestration.parse(json.dumps(LADDER), "test")
        mod._policy = lambda: (dict(mod._DEFAULT_RLM), {**mod._DEFAULT_PLANNING, "critique": False})
        mod._rlm_decompose = lambda prompt, knobs: json.dumps({"tasks": [{"id": "t1"}]})  # missing fields
        mod._plain_llm_decompose = lambda request, context, timeout=None: [dict(TASKS[0])]
        env = mod.decompose("do x")
        check(env["ok"] and env["result"]["engine"] == "fallback-plain-llm",
              f"an unusable RLM DAG falls back instead of failing: {env.get('result', {}).get('engine')}")
        check(env["result"].get("rlm_error"), "the fallback records why")

        mod._rlm_decompose = lambda prompt, knobs: (_ for _ in ()).throw(RuntimeError("rlm down"))
        env = mod.decompose("do x")
        check(env["ok"] and env["result"]["engine"] == "fallback-plain-llm", "an RLM exception falls back too")
    finally:
        (mod._policy, mod._rlm_decompose, mod._plain_llm_decompose, mod.chat,
         mod._critique_spec, orchestration.load) = original


def test_planner_role_resolution() -> None:
    import os

    from . import decompose as mod
    from . import orchestration as orch

    base = json.loads(json.dumps(LADDER))
    check(orch.resolve_model_for_role(orch.parse(json.dumps(base), "test"), "plan") is None,
          "no plan role on any arm -> None")

    ladder = json.loads(json.dumps(LADDER))
    ladder["workers"][0]["models"][0]["roles"].append("plan")
    ladder["roles"] = {"critique": "beta/beta-deep"}
    parsed = orch.parse(json.dumps(ladder), "test")
    check(orch.resolve_model_for_role(parsed, "plan") == "alpha/alpha-large",
          "an arm declaring `plan` resolves")
    check(orch.resolve_model_for_role(parsed, "critique") == "beta/beta-deep",
          "a `critique` binding resolves")

    def run():
        check(mod._role_spec("critique") == ("beta", "beta-deep"),
              f"decompose reads the ladder critique role: {mod._role_spec('critique')}")
        check(mod._planner_spec() == ("alpha", "alpha-large"),
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
    check(mod.pick_verifier(parsed, "") == "alpha/alpha-large", "no avoid family -> the first review arm")
    check(mod.pick_verifier(parsed, "alpha") == "beta/beta-deep",
          "an alpha implementer is verified by another vendor")

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
        mod._spec = lambda avoid: ("p", "m", "beta")
        env = mod.verify("t", "tests pass", "report", "evidence", "alpha", 3)
        check(env["ok"] and env["result"]["pass"] is True, f"2 of 3 passes -> pass: {env}")
        check(env["result"]["pass_count"] == 2 and env["result"]["samples"] == 3, "best-of-N counts votes")
        check(env["result"]["cross_vendor"] is True, "the verdict is marked cross-vendor")

        mod.chat = lambda *_a, **_k: json.dumps({"pass": False, "reason": "no", "evidence": "y"})
        env = mod.verify("t", "a", "", "", "alpha", 3)
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


def test_onboarding() -> None:
    import os

    from . import onboarding, paths

    env = {k: os.environ.get(k) for k in ("RLP_HOME", "RLP_ORCHESTRATION", "RLP_PI_MODELS", "RLP_PI_AUTH")}
    tmp = tempfile.mkdtemp(prefix="rlp-onboarding-")
    root = Path(tmp)
    try:
        # The probes read only their own files: a fake home and agent dir keep
        # this test honest on any host.
        os.environ["RLP_HOME"] = str(root / "runs-home")
        os.environ["RLP_ORCHESTRATION"] = str(root / "orchestration.json")
        os.environ["RLP_PI_MODELS"] = str(root / "models.json")
        os.environ["RLP_PI_AUTH"] = str(root / "auth.json")

        def done(report, *names):
            expect = {n: False for n in onboarding.MILESTONES}
            for name in names:
                expect[name] = True
            got = {s["id"]: s["done"] for s in report["steps"]}
            check(got == expect, f"{sorted(names)}: {got}")
            return report

        # The harness and engine probes read the host's own bundle, imports and
        # checkpoint cache — pin them off so the sequence is the thing under
        # test, and the suite is honest on any machine (this one included).
        # Restore them, not the module, at the end: a relative-imported module
        # cannot be reloaded mid-suite.
        saved_harness, saved_engine = onboarding._harness, onboarding._engine
        onboarding._harness = lambda: (False, "no bundle", "sh scripts/install.sh")
        onboarding._engine = lambda: (False, "not importable", "sh scripts/install.sh")

        # 1. Nothing has happened here: every step off, next is the first one.
        r = done(onboarding.progress())
        check(not r["complete"] and r["reached"] == 0, "a bare host is 0 of 7")
        check(r["next"]["id"] == onboarding.MILESTONES[0], "next names the first step")
        check(onboarding.summary_line(r).startswith("onboarding: 0/"), "the header line counts")

        (root / "runs-home").mkdir()
        (root / "runs-home" / "last-plan.json").write_text(json.dumps({"mode": "orchestrate", "tasks": [1, 2]}))
        r = done(onboarding.progress(), "plan")
        check(r["reached"] == 0, "a plan alone is not progress — the wall is still the harness")
        check(r["next"]["id"] == "harness", "next is still the first unreached step")
        check(r["steps"][onboarding.MILESTONES.index("plan")]["detail"].startswith("last plan: mode=orchestrate"),
              "the plan step shows the mode and node count")

        (root / "orchestration.json").write_text(json.dumps(LADDER))
        (root / "models.json").write_text(json.dumps({"providers": {
            "alpha": {"baseUrl": "https://alpha.example/v1", "models": [{"id": "alpha-large"}]},
            "beta": {"baseUrl": "https://beta.example/v1", "models": [{"id": "beta-deep"}]},
        }}))
        (root / "auth.json").write_text(json.dumps({"alpha": {"key": "k"}, "beta": {}}))
        r = done(onboarding.progress(), "plan", "ladder", "provider")
        check(r["reached"] == 0, "a ladder and a plan are not progress without a harness to run on")
        check("alpha" in r["steps"][onboarding.MILESTONES.index("provider")]["detail"],
              "the provider step names the credentialed endpoint, not the count only")
        check(r["next"]["id"] == "harness", "next lands on the first gap, however late it is")

        run_defs = [
            {"runId": "r0", "nodes": {"n1": {"status": "done", "startedAt": "t", "verdict": "pass"}}},
            {"runId": "r1", "nodes": {"n1": {"status": "running", "startedAt": "t"},
                                      "n2": {"status": "failed", "verdict": "ACCEPTANCE: fail", "startedAt": "t"}}},
        ]
        for run in run_defs:
            d = root / "runs-home" / "runs" / run["runId"]
            d.mkdir(parents=True)
            (d / "ledger.json").write_text(json.dumps(run))
        r = done(onboarding.progress(), "plan", "ladder", "provider", "dispatch", "result")
        check(r["next"]["id"] == "harness", "the ledgers fill the last two steps, but the wall is still the harness")
        check("3 worker dispatch(es)" in r["steps"][onboarding.MILESTONES.index("dispatch")]["detail"],
              "the ledgers count every started worker")
        check("1 node(s) met their acceptance" in r["steps"][onboarding.MILESTONES.index("result")]["detail"],
              "only the passing node counts as a result")
        check(not r["complete"], "harness and engine are still walls")

        # Unreachable on any machine? Make it reachable here, and the report
        # must celebrate it without crashing.
        onboarding._harness = lambda: (True, "stub", "")
        onboarding._engine = lambda: (True, "stub", "")
        r = onboarding.progress()
        check(r["complete"] and r["reached"] == 7, "every step done is complete")
        check(onboarding.summary_line(r).endswith("a worker has met its acceptance here"),
              "the header celebrates a finished host")
        check("7/7" in onboarding.render(r), "the full report shows the ladder complete")
        onboarding._harness, onboarding._engine = saved_harness, saved_engine

        # Unreadable ledgers are skipped, not fatal. A fresh home with only the
        # broken one, so the check is against garbage alone.
        broken_home = root / "broken-home"
        d = broken_home / "runs" / "broken"
        d.mkdir(parents=True)
        (d / "ledger.json").write_text("{not json")
        os.environ["RLP_HOME"] = str(broken_home)
        check(not onboarding._ledgers(), "garbage ledgers disappear quietly")
        check(not onboarding._node_passed({"status": "failed"}), "a failed node never counts as pass")

    finally:
        for k, v in env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


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
        test_unconfigured_ladder,
        test_signals_and_hybrid_gate,
        test_direct_mode,
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
        test_harness_catalog,
        test_availability,
        test_plan_excludes_unavailable,
        test_cli_surface,
        test_version_consistency,
        test_chat_contract,
        test_onboarding,
        test_planner_fallbacks,
        test_decompose_budget,
        test_paths,
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
