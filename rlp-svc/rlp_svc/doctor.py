"""rlp doctor — one command that answers "is RLP actually runnable here?".

Every RLP failure mode is silent-ish: a ladder with no arms makes every request
go inline, a stalled laya load looks like a hang, an unbuilt fork makes a
dispatch die the moment it starts, a provider with no credential burns a
dispatch before anyone notices. `doctor` turns all of them into one report with
a fix per line.

Checks are graded, not binary:
  ok    — good
  warn  — degraded but the run proceeds (e.g. an opt-in arm)
  fail  — the named capability will not work

Every `fail` and `warn` carries a hint that *this host can act on*. A check
whose only fix is a step the installer deliberately skips is not a diagnostic,
it is noise, and it does not belong here.

Model loading is *not* triggered by default: a fresh laya load costs ~170 s on
CPU and a diagnostic must never be the slow thing. `--warm` opts into one real
round trip.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path

from . import paths

OK, WARN, FAIL = "ok", "warn", "fail"
_RANK = {OK: 0, WARN: 1, FAIL: 2}

HASSES = ("laya", "rlm", "mcp", "httpx")
HF_REPO_DIR = "models--convaiinnovations--laya"


def _check(status: str, name: str, detail: str = "", hint: str = "") -> dict:
    return {"status": status, "name": name, "detail": detail, "hint": hint}


def _python() -> list[dict]:
    out = [_check(OK, "python", sys.version.split()[0], "")]
    if sys.version_info < (3, 10):
        out.append(_check(FAIL, "python-version", "needs >= 3.10", "install the engine with a python >= 3.10: sh scripts/install.sh"))
    for mod in HASSES:
        if importlib.util.find_spec(mod) is None:
            out.append(_check(FAIL, f"dep:{mod}", "not importable", "sh scripts/install.sh"))
        else:
            out.append(_check(OK, f"dep:{mod}", "importable", ""))
    return out


def _credentials() -> list[dict]:
    from . import llm

    out: list[dict] = []
    for label, path in (("auth.json", llm._auth_path()), ("models.json", llm._models_path())):
        p = Path(path)
        if not p.is_file():
            out.append(
                _check(
                    FAIL,
                    f"creds:{label}",
                    f"missing at {path}",
                    "connect a provider: /setup or /provider connect in a session (`rpi`), or `rlp provider add <id> <baseUrl> <model>`",
                )
            )
            continue
        try:
            json.loads(p.read_text())
            out.append(_check(OK, f"creds:{label}", str(path), ""))
        except Exception as e:
            out.append(_check(FAIL, f"creds:{label}", f"unparseable: {str(e)[:120]}", "re-login with `rpi`"))

    # The decomposer model comes from the ladder, so with no arms there is no
    # model to check and the ladder line already says why. Reporting a second
    # failure for the same cause just makes the report harder to act on.
    try:
        spec = llm.decomp_spec()
    except ValueError as e:
        out.append(
            _check(
                FAIL,
                "decompose-model",
                str(e)[:160],
                "unset it, or set it to a 'provider/model' string naming a configured endpoint",
            )
        )
        return out
    if spec is None:
        # In direct-only mode nothing is decomposed, so an unresolved planner model
        # is not a warning about a missing piece — it is a piece that is not part of
        # this host's design. Saying so is what keeps the report readable for the
        # person who chose the simple mode.
        from . import orchestration as orch

        if orch.direct_mode()["direct"]:
            out.append(
                _check(
                    OK,
                    "decompose-model",
                    "not resolved — direct-only mode never decomposes, so no planner model is needed",
                    "",
                )
            )
            return out
        out.append(
            _check(
                WARN,
                "decompose-model",
                "not resolved — the ladder has no arms (see the `ladder` line)",
                "/setup writes the arms; RLP_DECOMPOSE_MODEL=provider/model overrides them",
            )
        )
        return out
    provider, model = spec
    try:
        llm._provider_base_url(provider)
        llm._provider_key(provider)
        out.append(_check(OK, "decompose-model", f"{provider}/{model}", ""))
    except Exception as e:
        out.append(
            _check(
                FAIL,
                "decompose-model",
                f"{provider}/{model} unusable: {str(e)[:120]}",
                "/setup picks the planner/critic/verifier models, or set RLP_DECOMPOSE_MODEL=provider/model",
            )
        )
    return out


def _ladder() -> list[dict]:
    from . import orchestration as orch

    out: list[dict] = []
    path = orch.config_path()
    try:
        config = orch.load()
    except Exception as e:
        out.append(_check(FAIL, "ladder", f"invalid: {str(e)[:200]}", f"fix {path}"))
        return out
    if config is None:
        out.append(
            _check(
                FAIL,
                "ladder",
                f"not installed at {path}",
                "sh scripts/install.sh, or set RLP_ORCHESTRATION=<file>",
            )
        )
        return out
    # Direct-only mode is a choice rather than a hole in the ladder, so it gets
    # its own answer: nothing to fix, and the line says where the mode came from
    # — a $RLP_DIRECT left over in a shell is otherwise invisible, and reads
    # like a broken install. Everything below only means something to a host
    # that may orchestrate.
    direct = orch.direct_mode(config)
    if direct["direct"]:
        back = "`rlp mode full` (or /direct off) gives orchestration back"
        if direct["source"] == f"${orch.DIRECT_ENV}":
            back += " — unsetting $RLP_DIRECT ends it for this shell only"
        out.append(
            _check(
                OK,
                "ladder",
                f"{path} — direct-only mode (from {direct['source']}): every request is handled "
                f"inline, by choice, so no brain and no model arms are needed. {back}",
                "",
            )
        )
        return out
    # "Installed with no arms" is the shipped state, and it is a different
    # problem from a missing or broken file: the policy is fine, nobody has
    # chosen the models yet. One line, one fix, and no pretence that a ladder
    # with nothing to dispatch to is ok.
    if not config["configured"]:
        missing = []
        if config["brain"] is None:
            missing.append("no brain")
        if config["arm_count"] == 0:
            missing.append("no model arms")
        out.append(
            _check(
                FAIL,
                "ladder",
                f"{path} — policy is set, but {' and '.join(missing)}: nothing can be dispatched, "
                "so every request is handled inline",
                "run /setup in a session — it reads your endpoint's model list and writes the "
                "brain and the arms (`rlp provider add …` then /rlp-config does the same by hand)",
            )
        )
        return out
    out.append(
        _check(
            OK,
            "ladder",
            f"{path} — brain={config['brain']} workers={[w['id'] for w in config['workers']]} "
            f"escalateBelow={config['routing'].get('escalateBelow')} crossVendor={config['review'].get('crossVendor')}",
            "",
        )
    )
    cards = orch.roster(config)
    out.append(
        _check(
            OK if cards else FAIL,
            "router-roster",
            f"{len(cards)} dispatchable card(s): {[c['id'] for c in cards]}",
            "mark a worker \"available\": true to re-enable it" if not cards else "",
        )
    )
    for w in orch.excluded(config):
        out.append(_check(WARN, f"worker:{w['id']}", f"unavailable — {w['reason']}",
                          "opt-in arm; re-enable with \"available\": true when entitlement returns"))
    families = {a["model"].partition("/")[0] for w in config["workers"] for a in w["models"]}
    if config["review"].get("crossVendor") and len(families) < 2:
        out.append(
            _check(
                FAIL,
                "cross-vendor",
                f"crossVendor=true but the ladder names one family: {sorted(families)}",
                "add a second provider family or set review.crossVendor=false",
            )
        )
    else:
        out.append(_check(OK, "cross-vendor", f"families: {sorted(families)}", ""))
    # The bundled checkpoint's confidence tops out around 0.50, so a laya-only
    # gate with a threshold at or above that defers to direct almost always —
    # orchestration then depends entirely on a manual override. The hybrid gate
    # exists to fix exactly this; warn when it is switched off.
    gate = config["routing"].get("gate") or "hybrid"
    threshold = config["routing"].get("escalateBelow")
    if gate == "laya" and threshold is not None and float(threshold) >= 0.5:
        out.append(
            _check(
                WARN,
                "gate-calibration",
                f"routing.gate=laya with escalateBelow={threshold}: this checkpoint rarely exceeds it, "
                "so the gate will almost always default to direct",
                'set routing.gate="hybrid" (deterministic fan-out signals) or lower escalateBelow',
            )
        )
    else:
        out.append(_check(OK, "gate-calibration", f"gate={gate} escalateBelow={threshold}", ""))
    timeout = config["routing"].get("workerTimeoutMs")
    out.append(
        _check(
            OK if timeout else WARN,
            "worker-timeout",
            f"{timeout} ms" if timeout else "no worker watchdog configured",
            "set routing.workerTimeoutMs so a wedged worker cannot block collect forever",
        )
    )
    bindings = config.get("roles") or {}
    if not bindings:
        out.append(_check(OK, "role-bindings", "none — the planner uses arm priority order", ""))
    else:
        resolved = {role: orch.resolve_role(config, role) for role in bindings}
        stranded = [
            f"{role}->{res['model']}"
            for role, res in resolved.items()
            if res is None or res.get("unavailable") or not res.get("worker")
        ]
        if stranded:
            out.append(
                _check(
                    FAIL,
                    "role-bindings",
                    f"bound to a model with no dispatchable worker: {', '.join(stranded)}",
                    "make a worker carrying that arm \"available\": true, or rebind with /rlp-roles",
                )
            )
        else:
            summary = ", ".join(f"{role}->{res['model']}" for role, res in resolved.items() if res)
            out.append(_check(OK, "role-bindings", summary[:180], ""))
    planning = config.get("planning") or {}
    out.append(
        _check(
            OK,
            "planning",
            f"critique={planning.get('critique')} maxRefines={planning.get('maxRefines')} "
            f"recursiveDepth={planning.get('recursiveDepth')} artifactPassing={planning.get('artifactPassing')} "
            f"verifySamples={planning.get('verifySamples')}",
            "",
        )
    )
    rlm = config.get("rlm") or {}
    if rlm.get("maxBudget") is None and rlm.get("maxTimeout") is None:
        out.append(
            _check(
                WARN,
                "rlm-budget",
                "no maxBudget/maxTimeout — an RLM decomposition can run unbounded",
                "set rlm.maxBudget and/or rlm.maxTimeout in the ladder (/rlp-config rlm-budget <n>)",
            )
        )
    else:
        out.append(_check(OK, "rlm-budget", f"maxBudget={rlm.get('maxBudget')} maxTimeout={rlm.get('maxTimeout')}", ""))
    if any(w["id"] == "claude_code" and w.get("available", True) for w in config["workers"]):
        out.append(
            _check(
                WARN,
                "claude_code",
                "available; entitlement is routinely exhausted on this host",
                "set \"available\": false to keep the router off it — the brain re-dispatches to pi on failure",
            )
        )
    return out


def _providers() -> list[dict]:
    """Endpoints and credentials, and the arms that cannot run because of them.

    The most common silent failure in a fresh install is an arm the ladder
    prefers whose provider has no credential: the model is visible, the router
    picks it, and the dispatch dies. `preflight` catches it per node; this
    catches it before a request is ever made.
    """
    from . import providers as mod

    out: list[dict] = []
    try:
        cards = mod.list_providers()
    except Exception as e:
        return [_check(WARN, "providers", f"could not read {mod.models_path()}: {str(e)[:160]}", "")]

    credentialed = [c["id"] for c in cards if c["credential"] != "none"]
    keyless = [c["id"] for c in cards if c["credential"] == "none"]
    if not cards:
        out.append(
            _check(
                FAIL,
                "providers",
                f"no endpoints configured in {mod.models_path()}",
                "connect one: /setup or /provider connect in a session, or `rlp provider add <id> <baseUrl> <model>`",
            )
        )
    else:
        out.append(
            _check(
                OK if credentialed else FAIL,
                "providers",
                f"{len(cards)} endpoint(s) · {len(credentialed)} with a credential"
                + (f" · no credential: {', '.join(keyless)}" if keyless else ""),
                "" if credentialed else "run /provider key <id>, or /login <id> in a session",
            )
        )
    if keyless:
        out.append(
            _check(
                WARN,
                "providers:no-credential",
                f"{', '.join(keyless)} — configured but unusable",
                "nothing orchestrates on these until a key is stored: /provider key <id>",
            )
        )

    # Arm reachability is its own question — whether the router may pick a model
    # nothing can serve — so it is answered even when the endpoint list is empty,
    # where the answer is "all of them" and the fix is the same sentence.
    from . import orchestration as orch

    try:
        ladder = orch.load()
    except Exception:
        ladder = None
    try:
        orphans = mod.orphan_arms()
    except Exception:
        orphans = []
    if ladder is None:
        out.append(
            _check(
                WARN,
                "ladder-arms-reachable",
                "no usable ladder, so there are no arms to check",
                "sh scripts/install.sh installs the default ladder",
            )
        )
    elif ladder["arm_count"] == 0 and orch.direct_mode(ladder)["direct"]:
        # In direct-only mode the router is never consulted, so "no arms to
        # reach" is the design and not a warning with a fix attached.
        out.append(_check(OK, "ladder-arms-reachable", "direct-only mode: the router is not used", ""))
    elif ladder["arm_count"] == 0:
        # "Every arm is reachable" is vacuously true of zero arms, and reading it
        # as `ok` next to a failing ladder line is the kind of report that makes
        # a person stop trusting the whole thing.
        out.append(
            _check(
                WARN,
                "ladder-arms-reachable",
                "no arms on the ladder yet, so there is nothing to reach",
                "/setup writes them (see the `ladder` line)",
            )
        )
    elif orphans:
        out.append(
            _check(
                WARN,
                "ladder-arms-reachable",
                f"{len(orphans)} ladder arm(s) cannot run: "
                + "; ".join(f"{o['arm']} ({o['why'].split(' — ')[0]})" for o in orphans[:3])
                + (" …" if len(orphans) > 3 else ""),
                "fix the endpoint/credential, or remove the arm with /rlp-config set-arm",
            )
        )
    else:
        out.append(
            _check(OK, "ladder-arms-reachable", "every available ladder arm has an endpoint and a credential", "")
        )
    return out


def _laya() -> list[dict]:
    """Checkpoint presence, NOT the model itself (see module docstring)."""
    out: list[dict] = []
    ca = os.environ.get("SSL_CERT_FILE", "/etc/ssl/certs/ca-certificates.crt")
    out.append(
        _check(OK if os.path.exists(ca) else WARN, "ca-bundle", ca, "SSL_CERT_FILE must cover the MITM proxy CA")
    )
    hub = os.environ.get("HF_HOME") or os.environ.get("HUGGINGFACE_HUB_CACHE")
    roots = [Path(hub)] if hub else [
        Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "huggingface" / "hub",
        Path.home() / ".cache" / "huggingface" / "hub",
    ]
    for root in roots:
        if (root / HF_REPO_DIR).is_dir():
            out.append(_check(OK, "laya-checkpoint", str(root / HF_REPO_DIR), ""))
            return out
    out.append(
        _check(
            FAIL,
            "laya-checkpoint",
            f"not cached under {roots[0] / HF_REPO_DIR}",
            "sh scripts/install.sh (downloads ~400 MB, CPU)",
        )
    )
    return out


def _shipped() -> tuple[set[str], set[str]]:
    """What this RLP version ships: (extension file names, skill names).

    Read from the checkout this engine belongs to (`__file__` → `<repo>/
    rlp-svc/rlp_svc/doctor.py`), not from a hardcoded list. The list used to be
    literal, so it went stale the moment a fourth extension was added: a fresh
    install was then told that *three* files were installed while a fourth was
    checked by nothing. Returns empty sets when the checkout is not there to
    read (a wheel install), where the caller says so rather than guessing.
    """
    repo = Path(__file__).resolve().parents[2]
    ext_dir = repo / "agent" / "rlp" / "extensions"
    skill_dir = repo / "agent" / "rlp" / "skills"
    extensions = {p.name for p in ext_dir.glob("*.ts")} if ext_dir.is_dir() else set()
    skills = {p.parent.name for p in skill_dir.glob("*/SKILL.md")} if skill_dir.is_dir() else set()
    return extensions, skills


def _extensions() -> list[dict]:
    """RLP's own dropped-in extensions and skills, and the optional third-party ones beside them.

    RLP installs into its own agent dir, so everything beside its own files is
    third-party by definition. This check verifies only the files RLP recorded as
    its own (in `rlp-location.json`, written by install.sh) or — when that marker
    is missing — the names this checkout ships, and lists the rest as optional:
    never a failure, and never implied as a dependency. The same split applies to
    the skills directory, where RLP ships the `/skill:rlp-*` entry points.
    """
    agent = paths.agent_dir()
    ext_dir = agent / "extensions"
    out: list[dict] = []
    listed: object = None
    try:
        listed = json.loads((agent / "rlp-location.json").read_text()).get("extensions")
    except Exception:
        listed = None
    shipped_ext, shipped_skills = _shipped()
    owned = set(listed) if isinstance(listed, list) and listed else shipped_ext
    present = {p.name for p in ext_dir.glob("*.ts")} | {p.name for p in ext_dir.glob("*.js")} if ext_dir.is_dir() else set()
    if not owned:
        out.append(
            _check(
                WARN,
                "rlp-extensions",
                "no rlp-location.json and no checkout to compare against",
                "sh scripts/install.sh records what it owns",
            )
        )
    else:
        missing = sorted(owned - present)
        out.append(
            _check(
                FAIL if missing else OK,
                "rlp-extensions",
                f"{len(owned)} owned, all installed" if not missing else f"missing {missing}",
                "sh scripts/install.sh" if missing else "",
            )
        )
    if isinstance(listed, list) and listed and shipped_ext:
        stale = sorted(set(listed) - shipped_ext)
        if stale:
            out.append(
                _check(
                    WARN,
                    "rlp-extensions-current",
                    f"{stale} recorded but not shipped by this RLP version",
                    "sh scripts/install.sh, or delete the stale file from the agent dir",
                )
            )
    optional = sorted(present - owned)
    if optional:
        out.append(
            _check(
                OK,
                "optional-extensions",
                f"{', '.join(optional)} — not required by RLP",
                "",
            )
        )
    # RLP's shipped skills are the `/skill:<name>` entry points for the engine.
    # A skills directory with a missing entry is silent: the command is simply
    # not in the menu, and nothing says so. Same fallback as the extensions: the
    # marker if it is there, otherwise this checkout's own list.
    skill_dir = agent / "skills"
    listed_skills: object = None
    try:
        listed_skills = json.loads((agent / "rlp-location.json").read_text()).get("skills")
    except Exception:
        listed_skills = None
    owned_skills = set(listed_skills) if isinstance(listed_skills, list) and listed_skills else shipped_skills
    if not owned_skills:
        out.append(
            _check(
                WARN,
                "rlp-skills",
                "no rlp-location.json and no checkout to compare against",
                "sh scripts/install.sh adds /skill:rlp-* to the menu",
            )
        )
    else:
        missing_skills = sorted(name for name in owned_skills if not (skill_dir / name / "SKILL.md").is_file())
        out.append(
            _check(
                FAIL if missing_skills else OK,
                "rlp-skills",
                f"{len(owned_skills)} owned, all installed" if not missing_skills else f"missing {missing_skills}",
                "sh scripts/install.sh" if missing_skills else "",
            )
        )
    return out


def _dispatch() -> list[dict]:
    """Can this host actually start a worker?

    Dispatch is local: `rlp_dispatch` spawns the `rpi` harness in a git worktree.
    That makes the whole plane two binaries — the harness and git — and both are
    checkable. This is the group that used to ask about an external orchestration
    plane and answer in warnings nothing could clear; the question worth asking
    is whether a worker can be spawned, and it has a real answer.
    """
    out: list[dict] = []
    repo = Path(__file__).resolve().parents[2]
    rpi = repo / "scripts" / "rpi-bin"
    bundle = repo / "fork" / "pi" / "packages" / "coding-agent" / "dist" / "bundle" / "cli.js"
    if not rpi.is_file():
        out.append(_check(FAIL, "worker-harness", f"no {rpi}", "sh scripts/install.sh builds the harness"))
    elif not bundle.is_file():
        out.append(
            _check(
                FAIL,
                "worker-harness",
                f"{rpi} is present but the fork is not built ({bundle} missing)",
                "sh scripts/install.sh (or RLP_REBUILD=1 sh scripts/install.sh)",
            )
        )
    else:
        out.append(_check(OK, "worker-harness", str(rpi), ""))
    git = shutil.which("git")
    out.append(
        _check(
            OK if git else WARN,
            "bin:git",
            git or "not on PATH",
            "without git every worker shares one working tree instead of its own worktree",
        )
    )
    return out


def _env() -> list[dict]:
    knobs = {
        "RLP_CODING_AGENT_DIR": "agent dir override (settings, creds, models, sessions, ladder)",
        "RPI_CODING_AGENT_DIR": "the same, under the harness's own name",
        "RLP_HOME": "run ledgers and project memory (default ~/.rlp)",
        "RLP_ORCHESTRATION": "ladder path override",
        "RLP_DECOMPOSE_MODEL": "decomposer model (provider/model)",
        "RPI_DEFAULT_MODEL": "harness session default",
    }
    out = [
        _check(OK, "agent-dir", paths.described(), "RLP keeps its own state here; pi's ~/.pi is untouched"),
    ]
    out += [
        _check(OK, f"env:{k}", os.environ.get(k, "(unset)"), v)
        for k, v in knobs.items()
    ]
    return out


def _warm() -> list[dict]:
    """One real laya round trip — the only check that proves the model loads."""
    from . import orchestration as orch
    from .triage import triage

    out: list[dict] = []
    t0 = __import__("time").monotonic()
    answer = triage("Fix the typo in the README title", "")
    elapsed = __import__("time").monotonic() - t0
    engine = answer.get("engine")
    out.append(
        _check(
            OK if engine == "laya" else FAIL,
            "laya-roundtrip",
            f"engine={engine} mode={answer.get('mode')} conf={answer.get('confidence')} in {elapsed:.1f}s"
            + (f" ({answer.get('laya_error', '')[:80]})" if engine != "laya" else ""),
            "a cold CPU load takes ~170 s; a warm one is milliseconds"
            if engine == "laya" else "the LLM fallback carried the call — see laya-checkpoint",
        )
    )
    return out


def run(*, warm: bool = False) -> dict:
    """Collect every check. Returns {"ok", "summary", "checks"}; never raises."""
    checks: list[dict] = []
    for group in (_python, _credentials, _providers, _ladder, _laya, _dispatch, _env, _extensions):
        try:
            checks.extend(group())
        except Exception as e:  # a broken group must not hide the others
            checks.append(
                _check(
                    FAIL,
                    group.__name__.lstrip("_"),
                    f"check crashed: {type(e).__name__}: {str(e)[:160]}",
                    "this is a bug in rlp doctor, not in your setup — the other lines are still valid",
                )
            )
    if warm:
        try:
            checks.extend(_warm())
        except Exception as e:
            checks.append(
                _check(
                    FAIL,
                    "warm",
                    f"round trip crashed: {type(e).__name__}: {str(e)[:160]}",
                    "see the laya-checkpoint and dep: lines above; `sh scripts/install.sh` re-fetches both",
                )
            )
    failures = sum(1 for c in checks if c["status"] == FAIL)
    warnings = sum(1 for c in checks if c["status"] == WARN)
    return {
        "ok": failures == 0,
        "summary": {"fail": failures, "warn": warnings, "pass": len(checks) - failures - warnings},
        "checks": sorted(checks, key=lambda c: _RANK[c["status"]]),
    }


def render(report: dict) -> str:
    """Terminal report: status glyph, name, detail, then the fix per failure."""
    glyph = {OK: "  ok  ", WARN: " warn ", FAIL: " FAIL "}
    # Where you are, before twenty lines of what is true. A beginner reading a
    # doctor report cannot tell which line is the wall they are standing at;
    # this names it, and `rlp progress` expands it.
    header = ""
    try:
        from . import onboarding

        header = onboarding.summary_line()
    except Exception:
        header = ""
    lines = ["rlp doctor"]
    if header:
        lines.append(f"  {header}   (rlp progress for the full path)")
    lines.append("")
    for c in report["checks"]:
        lines.append(f"[{glyph[c['status']]}] {c['name']:<22} {c['detail']}")
        if c["hint"] and c["status"] != OK:
            lines.append(f"{'':<9} -> {c['hint']}")
    s = report["summary"]
    lines += ["", f"{s['pass']} passed, {s['warn']} warning(s), {s['fail']} failure(s)"]
    lines.append("runnable." if report["ok"] else "not runnable yet — fix the FAIL lines above.")
    return "\n".join(lines)
