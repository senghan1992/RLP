"""rlp doctor — one command that answers "is RLP actually runnable here?".

Every RLP failure mode is silent-ish: a missing ladder makes the router fall
back, a stalled laya load looks like a hang, an unwired omnigent harness makes
workers launch the wrong binary, an exhausted Claude arm burns a dispatch
before anyone notices. `doctor` turns all of them into one report with a fix
per line.

Checks are graded, not binary:
  ok    — good
  warn  — degraded but the run proceeds (e.g. the opt-in claude arm)
  fail  — the named capability will not work

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

OK, WARN, FAIL = "ok", "warn", "fail"
_RANK = {OK: 0, WARN: 1, FAIL: 2}

HASSES = ("laya", "rlm", "mcp", "httpx")
HF_REPO_DIR = "models--convaiinnovations--laya"


def _check(status: str, name: str, detail: str = "", hint: str = "") -> dict:
    return {"status": status, "name": name, "detail": detail, "hint": hint}


def _python() -> list[dict]:
    out = [_check(OK, "python", sys.version.split()[0], "")]
    if sys.version_info < (3, 10):
        out.append(_check(FAIL, "python-version", "needs >= 3.10", "recreate the venv with 3.12"))
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
            out.append(_check(FAIL, f"creds:{label}", f"missing at {path}", "log in once with the harness (`rpi`)"))
            continue
        try:
            json.loads(p.read_text())
            out.append(_check(OK, f"creds:{label}", str(path), ""))
        except Exception as e:
            out.append(_check(FAIL, f"creds:{label}", f"unparseable: {str(e)[:120]}", "re-login with `rpi`"))

    provider, model = llm.decomp_spec()
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
                "check auth.json/models.json, or set RLP_DECOMPOSE_MODEL=provider/model",
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


def _extensions() -> list[dict]:
    """RLP's own dropped-in extensions, and the optional third-party ones beside them.

    The harness agent dir is shared: myviking, databricks-tool-schema-sanitizer
    and friends live in the same `extensions/` directory RLP installs into. RLP
    needs none of them, so this check verifies only the files RLP recorded as its
    own (in `rlp-location.json`) and lists the rest as optional — never a
    failure, and never implied as a dependency.
    """
    agent = Path(os.environ.get("RPI_CODING_AGENT_DIR") or Path.home() / ".pi" / "agent")
    ext_dir = agent / "extensions"
    out: list[dict] = []
    listed: object = None
    try:
        listed = json.loads((agent / "rlp-location.json").read_text()).get("extensions")
    except Exception:
        listed = None
    owned = set(listed) if isinstance(listed, list) and listed else {
        "menus.ts",
        "rlp-commands.ts",
        "rlp-orchestrate.ts",
    }
    present = {p.name for p in ext_dir.glob("*.ts")} | {p.name for p in ext_dir.glob("*.js")} if ext_dir.is_dir() else set()
    missing = sorted(owned - present)
    out.append(
        _check(
            FAIL if missing else OK,
            "rlp-extensions",
            f"{len(owned)} owned, all installed" if not missing else f"missing {missing}",
            "sh scripts/install.sh" if missing else "",
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
    return out


def _host() -> list[dict]:
    """Optional host wiring: the orchestrator plane behind the planner."""
    out: list[dict] = []
    for tool in ("omni",):
        out.append(
            _check(OK if shutil.which(tool) else WARN, f"bin:{tool}", shutil.which(tool) or "not on PATH",
                   "needed only to dispatch workers, not to plan")
        )
    spec = Path.home() / ".omnigent" / "agents" / "rlp"
    out.append(
        _check(OK if (spec / "config.yaml").is_file() else WARN, "agent-spec", str(spec),
               "sh scripts/install.sh" if not (spec / "config.yaml").is_file() else "")
    )
    cfg = Path.home() / ".omnigent" / "config.yaml"
    if cfg.is_file():
        text = cfg.read_text()
        out.append(
            _check(
                OK if "harness:" in text and "rpi" in text else WARN,
                "harness-override",
                "omnigent pi harness -> rpi" if "rpi" in text else "no rpi harness override",
                "re-run scripts/install.sh so workers boot the fork",
            )
        )
    else:
        out.append(_check(WARN, "harness-override", f"no {cfg}", "sh scripts/install.sh"))
    return out


def _env() -> list[dict]:
    knobs = {
        "RLP_ORCHESTRATION": "ladder path override",
        "RLP_DECOMPOSE_MODEL": "decomposer model (provider/model)",
        "RPI_DEFAULT_MODEL": "harness session default",
        "RPI_CODING_AGENT_DIR": "harness agent dir",
    }
    return [
        _check(OK if os.environ.get(k) else OK, f"env:{k}", os.environ.get(k, "(unset)"), v)
        for k, v in knobs.items()
    ]


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


def run(*, warm: bool = False, host: bool = True) -> dict:
    """Collect every check. Returns {"ok", "summary", "checks"}; never raises."""
    checks: list[dict] = []
    for group in (_python, _credentials, _ladder, _laya, _env, _extensions):
        try:
            checks.extend(group())
        except Exception as e:  # a broken group must not hide the others
            checks.append(_check(FAIL, group.__name__.lstrip("_"), f"check crashed: {str(e)[:160]}", ""))
    if host:
        try:
            checks.extend(_host())
        except Exception as e:
            checks.append(_check(WARN, "host", f"check crashed: {str(e)[:160]}", ""))
    if warm:
        try:
            checks.extend(_warm())
        except Exception as e:
            checks.append(_check(FAIL, "warm", f"round trip crashed: {str(e)[:160]}", ""))
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
    lines = ["rlp doctor", ""]
    for c in report["checks"]:
        lines.append(f"[{glyph[c['status']]}] {c['name']:<22} {c['detail']}")
        if c["hint"] and c["status"] != OK:
            lines.append(f"{'':<9} -> {c['hint']}")
    s = report["summary"]
    lines += ["", f"{s['pass']} passed, {s['warn']} warning(s), {s['fail']} failure(s)"]
    lines.append("runnable." if report["ok"] else "not runnable yet — fix the FAIL lines above.")
    return "\n".join(lines)
