"""How far this host has got from "installed" to "a worker came back green".

The question this answers is the one a new user actually has — *what do I do
next* — and the one the tool's author has about them: where does onboarding
stop. `doctor` reports whether each capability works; it does not say which of
them are the wall you are currently standing at, and a report of twenty lines
does not tell a beginner which one to read first.

**Every milestone is derived from state that already exists.** Nothing here
records anything, and nothing is sent anywhere:

    harness    the fork's built bundle is on disk
    engine     laya + rlm importable, and the checkpoint cached
    provider   at least one endpoint has a credential
    ladder     the ladder names a brain and at least one arm
    plan       `$RLP_HOME/last-plan.json` exists (the extension writes it)
    dispatch   some run ledger has a node that was actually started
    result     some run ledger has a node that finished with acceptance: pass

That is a deliberate constraint rather than a shortcut. A progress file would be
one more thing to write, to get out of step with reality, and to be wrong about
after someone deletes a directory; derived state cannot drift from the thing it
describes. It also means this is honest on a machine RLP has never run on, and
correct again the moment it has.

The last three come from the run ledgers `rlp_dispatch` already keeps under
`$RLP_HOME/runs/`, which is also why "did a dispatch ever work here" is
answerable at all: the evidence outlives the session.

**Direct-only mode shortens the sequence.** A host whose gate never orchestrates
has chosen not to have a decision model, a plan, a dispatch or a worker verdict,
so those four milestones are dropped rather than counted as missing — reporting
3/7 forever, with no door on that wall, is worse than reporting 3/3 of the work
that mode can actually do. `rlp progress` says so on its face.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

from . import paths

#: The checkpoint cache directory name, shared with `doctor`.
HF_REPO_DIR = "models--convaiinnovations--laya"


def _harness() -> tuple[bool, str, str]:
    repo = Path(__file__).resolve().parents[2]
    bundle = repo / "fork" / "pi" / "packages" / "coding-agent" / "dist" / "bundle" / "cli.js"
    if bundle.is_file():
        return True, str(bundle.parent), ""
    return False, f"no bundle at {bundle}", "sh scripts/install.sh"


def _engine() -> tuple[bool, str, str]:
    missing = [m for m in ("laya", "rlm") if importlib.util.find_spec(m) is None]
    hub = os.environ.get("HF_HOME") or os.environ.get("HUGGINGFACE_HUB_CACHE")
    roots = [Path(hub)] if hub else [
        Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "huggingface" / "hub",
        Path.home() / ".cache" / "huggingface" / "hub",
    ]
    cached = any((root / HF_REPO_DIR).is_dir() for root in roots)
    if missing:
        return False, f"not importable: {', '.join(missing)}", "sh scripts/install.sh (without RLP_SKIP_MODELS)"
    if not cached:
        return False, "the laya checkpoint is not cached", "sh scripts/install.sh (downloads ~400 MB, once)"
    return True, "laya and rlm are installed, checkpoint cached", ""


def _provider() -> tuple[bool, str, str]:
    try:
        from . import providers as mod

        cards = mod.list_providers()
    except Exception as e:
        return False, f"the provider store is unreadable: {str(e)[:80]}", "/provider"
    credentialed = [c["id"] for c in cards if c["credential"] != "none"]
    if credentialed:
        return True, f"{len(credentialed)} endpoint(s) with a credential: {', '.join(credentialed[:3])}", ""
    if cards:
        return False, f"{len(cards)} endpoint(s), none with a credential", "/provider key <id>"
    return False, "no endpoint configured", "/setup"


def _ladder() -> tuple[bool, str, str]:
    try:
        from . import orchestration as orch

        config = orch.load()
    except Exception as e:
        return False, f"the ladder is invalid: {str(e)[:80]}", "fix it, or /rlp-config"
    if config is None:
        return False, "no ladder installed", "sh scripts/install.sh"
    # In direct-only mode the ladder's job is to say "do not orchestrate", and it
    # is doing that, arms or not. Requiring arms here would tell someone who
    # chose the simple mode that their install is unfinished.
    direct = orch.direct_mode(config)
    if direct["direct"]:
        return True, f"direct-only mode (from {direct['source']}) — every request handled inline", ""
    if not config["configured"]:
        return False, "the ladder has no model arms", "/setup"
    return True, f"brain={config['brain']}, {config['arm_count']} arm(s)", ""


def _plan() -> tuple[bool, str, str]:
    path = paths.home() / "last-plan.json"
    if not path.is_file():
        return False, "no plan has been produced here yet", 'rlp plan "<a multi-part request>"'
    try:
        plan = json.loads(path.read_text())
    except Exception:
        return True, f"{path} exists but is unreadable", ""
    mode = plan.get("mode", "?")
    tasks = len(plan.get("tasks") or [])
    return True, f"last plan: mode={mode}" + (f", {tasks} node(s)" if tasks else ""), ""


def _ledgers() -> list[dict]:
    """Every run ledger, newest first. Unreadable ones are skipped, not fatal."""
    out: list[dict] = []
    runs = paths.home() / "runs"
    if not runs.is_dir():
        return out
    try:
        names = sorted((p.name for p in runs.iterdir() if p.is_dir()), reverse=True)
    except Exception:
        return out
    for name in names:
        try:
            out.append(json.loads((runs / name / "ledger.json").read_text()))
        except Exception:
            continue
    return out


def _node_passed(node: dict) -> bool:
    """Did this node actually meet its contract?

    Exit code 0 is not the question — a worker can exit clean and report
    `ACCEPTANCE: fail`, and the contract is the acceptance line and the
    machine-readable report, not the exit status. This is the same rule the
    orchestration contract gives the brain.
    """
    if node.get("status") != "done":
        return False
    verdict = str(node.get("verdict") or "").strip().lower()
    if verdict.startswith("fail") or "changes requested" in verdict:
        return False
    return verdict.startswith("pass") or verdict.startswith("approved") or bool(verdict)


def _dispatch(ledgers: list[dict]) -> tuple[bool, str, str]:
    started = [
        (run.get("runId", "?"), node)
        for run in ledgers
        for node in (run.get("nodes") or {}).values()
        if node.get("startedAt")
    ]
    if not started:
        return (
            False,
            "no worker has ever been started here",
            'ask `rlp` for something with two or more independent deliverables',
        )
    run_id, _ = started[0]
    return True, f"{len(started)} worker dispatch(es) across {len(ledgers)} run(s); newest {run_id}", ""


def _result(ledgers: list[dict]) -> tuple[bool, str, str]:
    passed = [
        (run.get("runId", "?"), node)
        for run in ledgers
        for node in (run.get("nodes") or {}).values()
        if _node_passed(node)
    ]
    if not passed:
        any_started = any(n.get("startedAt") for r in ledgers for n in (r.get("nodes") or {}).values())
        if any_started:
            return (
                False,
                "workers have run, but none has reported acceptance: pass",
                "/rlp-state shows each node's verdict and its log",
            )
        return False, "nothing has come back yet", "dispatch something first"
    run_id, node = passed[0]
    return True, f"{len(passed)} node(s) met their acceptance; newest {run_id}/{node.get('id', '?')}", ""


#: Ordered, because onboarding is a sequence: a later one cannot be reached
#: without the earlier ones, and the first unreached is the only one worth
#: telling somebody about.
MILESTONES = ("harness", "engine", "provider", "ladder", "plan", "dispatch", "result")

#: The milestones that only exist *because* of orchestration. A host in
#: direct-only mode has chosen never to do any of them, and laya is the model
#: the gate reads — so counting four of the seven as "not reached" would report
#: a finished, working install as stuck at 3/7 forever, with no door on that
#: wall. They are dropped from the sequence rather than marked done, because
#: marking them done would be a lie.
ORCHESTRATION_MILESTONES = ("engine", "plan", "dispatch", "result")

#: Shown when the sequence is shortened, so `rlp progress` cannot read as if it
#: had forgotten the rest of the list.
DIRECT_NOTE = (
    "direct-only mode: the gate, the decision model and everything downstream of "
    "them are not counted here — this host chose not to orchestrate. "
    "`rlp mode full` puts the sequence back."
)

TITLES = {
    "harness": "the harness is built",
    "engine": "the decision model is installed",
    "provider": "a provider is connected",
    "ladder": "the ladder has model arms",
    "plan": "a plan has been produced",
    "dispatch": "a worker has been dispatched",
    "result": "a worker met its acceptance",
}


def _direct_mode() -> dict | None:
    """The direct-only verdict, or None when this host may orchestrate.

    Read once per `progress()` and shared by the probes: four milestones are
    about orchestration, and they have to agree with the `ladder` probe about
    whether they exist at all. Never raises — a ladder nobody can read is not
    in direct-only mode, and `_ladder` reports why.
    """
    try:
        from . import orchestration as orch

        direct = orch.direct_mode()
    except Exception:
        return None
    return direct if direct["direct"] else None


def progress() -> dict:
    """{"reached", "total", "next", "complete", "steps": [...]}. Never raises."""
    ledgers = _ledgers()
    checks = {
        "harness": _harness,
        "engine": _engine,
        "provider": _provider,
        "ladder": _ladder,
        "plan": _plan,
        "dispatch": lambda: _dispatch(ledgers),
        "result": lambda: _result(ledgers),
    }
    milestones = list(MILESTONES)
    direct = _direct_mode()
    if direct:
        milestones = [m for m in milestones if m not in ORCHESTRATION_MILESTONES]
    steps: list[dict] = []
    for name in milestones:
        try:
            done, detail, action = checks[name]()
        except Exception as e:  # a broken probe must not hide the rest
            done, detail, action = False, f"could not tell: {str(e)[:80]}", ""
        steps.append(
            {
                "id": name,
                "title": "the mode is chosen (direct-only)" if direct and name == "ladder" else TITLES[name],
                "done": done,
                "detail": detail,
                "action": action,
            }
        )
    # `reached` counts the leading run of completed steps, not the total number
    # completed: onboarding is a sequence, and "5 of 7, but not the first two"
    # describes a broken install rather than progress.
    reached = 0
    for step in steps:
        if not step["done"]:
            break
        reached += 1
    pending = next((s for s in steps if not s["done"]), None)
    return {
        "reached": reached,
        "total": len(steps),
        "direct_only": bool(direct),
        "complete": pending is None,
        "next": None
        if pending is None
        else {"id": pending["id"], "title": pending["title"], "action": pending["action"], "detail": pending["detail"]},
        "steps": steps,
    }


def summary_line(report: dict | None = None) -> str:
    """One line for the top of another report. The whole point is the `next`."""
    r = report or progress()
    mode = " — direct-only mode" if r.get("direct_only") else ""
    if r["complete"]:
        if r.get("direct_only"):
            return f"onboarding: {r['total']}/{r['total']} — ready to work inline{mode}"
        return f"onboarding: {r['total']}/{r['total']} — a worker has met its acceptance here{mode}"
    nxt = r["next"]
    action = f" — next: {nxt['action']}" if nxt["action"] else f" — next: {nxt['title']}"
    return f"onboarding: {r['reached']}/{r['total']}{action}{mode}"


def render(report: dict | None = None) -> str:
    """The full ladder of milestones, for `rlp progress`."""
    r = report or progress()
    glyph = {True: "[done]", False: "[    ]"}
    lines = ["rlp progress — install to first green worker", ""]
    if r.get("direct_only"):
        lines[0] = "rlp progress — install to a working agent (direct-only mode)"
    for step in r["steps"]:
        lines.append(f"{glyph[step['done']]} {step['title']}")
        lines.append(f"         {step['detail']}")
        if not step["done"] and step["action"]:
            lines.append(f"         -> {step['action']}")
    lines.append("")
    if r.get("direct_only"):
        lines.append(DIRECT_NOTE)
        lines.append("")
    if r["complete"]:
        lines.append(
            f"{r['total']}/{r['total']} — RLP is ready to work on this host, inline."
            if r.get("direct_only")
            else f"{r['total']}/{r['total']} — RLP has done real work on this host."
        )
    else:
        nxt = r["next"]
        lines.append(f"{r['reached']}/{r['total']}. Next: {nxt['title']}.")
        if nxt["action"]:
            lines.append(f"  {nxt['action']}")
        lines.append("")
        lines.append("Steps after the next one are not failures — they are simply not reached yet.")
        lines.append("`rlp doctor` explains any capability that is broken rather than unstarted.")
    # A derived glance at what else this host could work with. Not a milestone:
    # nothing about the ladder or onboarding *records* it, and a broken or slow
    # catalog read must never hide the report, so it is wrapped in its absence.
    try:
        from . import harnesses

        lines.append("")
        lines.append(f"  harnesses {harnesses.one_line_summary(harnesses.scan_result(versions=False))}")
    except Exception:
        pass
    return "\n".join(lines)
