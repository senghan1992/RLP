"""rlp harnesses — which coding CLIs this host could actually run a worker in.

RLP used to know exactly one harness: its own `pi` fork. The ladder always had a
`harness` string per worker, and every string but "pi" meant "cannot dispatch
here" — a policy written down as a fact nobody checked. This module is the fact:
one catalog of the coding CLIs worth driving, where each one's binary lives,
what its headless invocation looks like, and whether this host can log into it.

The catalog is *data*, and the driver object that rides a route record is also
data (see `driver_for`). The brain extension assembles argv from it and spawns;
adding a harness — codex, opencode, the next one — is one entry here, not a
change in two languages. pi is deliberately the exception: its dispatch path is
older, richer (worktree + contract + ledger), and byte-tested; it keeps it.

Everything the catalog does to the outside world arrives through the four
injectable seams (`which`, `env`, `home`, `probe`) so the offline suite can
answer this on a host with zero CLIs installed — which is what CI is.

Secrets: this file knows *env-var names*, never values, and never reads a
credential file's contents — only that one exists. A driver record that carried
a key would end up in a ledger and a log.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

#: `--version` is a spawn; a hung binary must not hang a diagnostic.
PROBE_TIMEOUT_S = 5

#: The one grammar external arms follow (design decision D2): `<harness>/<native-model-id>`,
#: and `<harness>/default` means "whatever this tool picks" — no --model flag.
DEFAULT_MODEL = "default"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


HARNESSES: dict[str, dict[str, Any]] = {
    "pi": {
        "title": "pi / rpi (RLP's own fork)",
        "one_liner": "RLP's harness: the worker contract, the ladder arms, the default worker",
        "vendor": "pi",
        "binaries": ["rpi", "pi"],
        "headless": True,
        "dispatch": "internal",
        "config_files": [],
        "env": [],
        "login_hint": "",
        "version_cmd": ["--version"],
        "argv": [],
        "model_args": [],
    },
    "omp": {
        "title": "omp (pi family)",
        "one_liner": "pi-family CLI headless -p: fuzzy model match, cheap fast paths, plan modes",
        "vendor": "pi",
        "binaries": ["omp"],
        "headless": True,
        "dispatch": "template",
        "config_files": ["~/.omp/agent/models.yml", "~/.omp/agent/config.yml"],
        "env": ["ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY"],
        "login_hint": "give omp an endpoint: ~/.omp/agent/models.yml, or export the provider key env above",
        "version_cmd": ["--version"],
        "argv": ["-p", "--no-session", "{prompt}"],
        "model_args": ["--model", "{model}"],
    },
    "claude": {
        "title": "Claude Code",
        "one_liner": "Anthropic's CLI: strong at large refactors; headless -p with its own permission story",
        "vendor": "anthropic",
        "binaries": ["claude"],
        "headless": True,
        "dispatch": "template",
        "config_files": ["~/.claude/.credentials.json", "~/.claude.json"],
        "env": ["ANTHROPIC_API_KEY"],
        "login_hint": "run `claude` once and log in, or export ANTHROPIC_API_KEY",
        "version_cmd": ["--version"],
        "argv": ["-p", "--dangerously-skip-permissions", "{prompt}"],
        "model_args": ["--model", "{model}"],
    },
    "jcode": {
        "title": "jcode",
        "one_liner": "subscription-backed multi-provider runner: `run` takes one message and exits",
        "vendor": "multi",
        "binaries": ["jcode"],
        "headless": True,
        "dispatch": "template",
        "config_files": ["~/.jcode/auth.json", "~/.jcode/config.toml"],
        "env": ["ANTHROPIC_API_KEY", "OPENAI_API_KEY"],
        "login_hint": "`jcode login` (OAuth, API key, or local credentials)",
        "version_cmd": ["--version"],
        "argv": ["run", "{prompt}"],
        "model_args": ["-m", "{model}"],
    },
    "muse": {
        "title": "Muse Code",
        "one_liner": "Meta's CLI: `exec` runs one prompt headless, reads the prompt from a file, JSONL events",
        "vendor": "meta",
        "binaries": ["muse"],
        "headless": True,
        "dispatch": "template",
        "config_files": ["~/.config/muse/auth.json", "~/.muse/auth.json"],
        "env": ["MODEL_API_KEY"],
        "login_hint": "`muse login` (or `muse auth`), or export MODEL_API_KEY",
        "version_cmd": ["--version"],
        "argv": ["exec", "--prompt-file", "{prompt_file}"],
        "model_args": ["--model", "{model}"],
    },
}

# --- the injectable seams -------------------------------------------------------

Which = Callable[[str], "str | None"]
Probe = Callable[[str, list[str]], "str | None"]


def _default_which(name: str) -> str | None:
    return shutil.which(name)


def _default_probe(binary: str, args: list[str]) -> str | None:
    """`<binary> --version`, first line, or None. Never raises, never hangs.

    Deliberately not `subprocess.run(capture_output=True)`: a CLI that detaches
    a daemon inherits the *pipe*, and reading stdout then waits for an EOF that
    only the daemon can deliver — measured on this host, where `jcode --version`
    left the probe blocked in `communicate()` past its own timeout. Output goes
    to a file (a closed process is enough; no writer's schedule matters), and a
    timed-out probe dies as a *process group* — whatever the probe spawned dies
    with it, while a daemon the user already had running is in another group
    and untouched.
    """
    import signal
    import tempfile

    try:
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as sink:
            proc = subprocess.Popen(
                [binary, *args],
                stdin=subprocess.DEVNULL,
                stdout=sink,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                proc.wait(timeout=PROBE_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    proc.kill()
                try:
                    proc.wait(timeout=5)
                except Exception:
                    pass
            sink.seek(0)
            text = sink.read()
    except Exception:
        return None
    line = text.strip().splitlines()
    return (line[0] if line else "")[:80] or None


def scan_disabled(env: dict[str, str] | None = None) -> bool:
    """RLP_HARNESS_SCAN=0: no probing, no exclusion, no doctor lines.

    The offline suite and CI hosts have none of these CLIs and must not pay a
    spawn for the guess; a user on an odd host can turn the whole feature off
    the same way `RLP_SKIP_CREDENTIAL_PREFLIGHT` turns off the credential gate.
    """
    e = os.environ if env is None else env
    return e.get("RLP_HARNESS_SCAN", "").strip() == "0"


# --- the three questions the catalog answers ------------------------------------

def resolve_binary(harness_id: str, which: Which | None = None) -> str | None:
    """PATH lookup only — no spawn, no version, no auth. Safe on every hot path.

    pi resolves through the checkout's `scripts/rpi-bin` rather than PATH,
    because that is what dispatch actually execs.
    """
    spec = HARNESSES.get(harness_id)
    if spec is None:
        return None
    if spec["dispatch"] == "internal":
        rpi = _repo_root() / "scripts" / "rpi-bin"
        return str(rpi) if rpi.is_file() else None
    w = _default_which if which is None else which
    for name in spec["binaries"]:
        found = w(name)
        if found:
            return found
    return None


def binary_present(harness_id: str, which: Which | None = None, env: dict[str, str] | None = None) -> bool:
    """Is a worker on this harness actually runnable here? PATH-only — no spawn.

    `False` is reserved for the one fact that is cheap *and* certain: a
    catalogued external tool whose binary is not on PATH. pi answers `True`
    because RLP ships it (its dispatch resolves `rpi-bin` at spawn time, and a
    Python-side guess must not exclude it), and a harness the catalog does not
    carry answers `True` because D5's rule still holds — *not knowing* must not
    silently drop a worker; the dispatcher's loud per-node failure is where an
    unknown tool belongs. `RLP_HARNESS_SCAN=0` claims nothing: an offline host
    probes nothing, and routing behaves exactly as it did before this existed.
    """
    e = dict(os.environ) if env is None else env
    if scan_disabled(e):
        return True
    spec = HARNESSES.get(harness_id)
    if spec is None or spec["dispatch"] == "internal":
        return True
    return resolve_binary(harness_id, which) is not None


def vendor_of(harness_id: str) -> str | None:
    """The tool vendor behind a harness, or None when there is no tool to name.

    Only external (`template`) harnesses answer: pi's vendor string is RLP's
    own name for itself, and an unlisted harness has no vendor to claim —
    neither belongs in a reviewer's avoid set (see `plan.harness_vendor`).
    """
    spec = HARNESSES.get(harness_id)
    if spec is None or spec["dispatch"] == "internal":
        return None
    return spec["vendor"]


def auth_state(
    harness_id: str,
    env: dict[str, str] | None = None,
    home: Path | None = None,
) -> dict[str, str]:
    """Can this harness actually authenticate *here* — without reading any secret.

    States: `internal` (pi: credentials are RLP's own ladder/auth.json, and
    doctor's `worker-harness`/`creds:` lines own that), `authenticated` (a
    marker file exists, or one of the named env vars is set — set is not
    verified, the run itself is the test), `needs-login`, `unknown` (an
    unlisted harness: permissive, because *not knowing* must not stall a run
    that would have worked; a real failure lands in the worker log instead).
    """
    spec = HARNESSES.get(harness_id)
    if spec is None:
        return {"state": "unknown", "marker": "", "hint": f"rlp: no catalog entry for harness {harness_id!r} — add one in rlp_svc/harnesses.py, or fix the worker's harness name"}
    if spec["dispatch"] == "internal":
        return {"state": "internal", "marker": "RLP's own auth.json", "hint": ""}
    e = dict(os.environ) if env is None else env
    root = Path.home() if home is None else home
    for rel in spec["config_files"]:
        p = Path(os.path.expanduser(rel.replace("~", str(root), 1))) if rel.startswith("~") else Path(rel)
        if p.exists():
            return {"state": "authenticated", "marker": str(p), "hint": ""}
    for name in spec["env"]:
        if e.get(name):
            return {"state": "authenticated", "marker": f"env {name}", "hint": ""}
    return {"state": "needs-login", "marker": "", "hint": spec["login_hint"]}


def driver_for(harness_id: str, model: str | None = None, binary: str | None = None) -> dict[str, Any] | None:
    """The route record's driver: argv *templates*, so the TS side only assembles.

    `{prompt}` / `{prompt_file}` / `{model}` are substituted by the dispatcher —
    they stay placeholders here because the prompt file is created per dispatch,
    in the run directory, by the thing that spawns. `model_args` (which carries
    `{model}`) is dropped entirely when the arm is `<harness>/default`: telling
    a tool to pick its own model is not the same as telling it a model name that
    does not exist. Returns None for pi (internal dispatch) and for unknown
    harnesses (the caller fails the node with a fix line, not a silent fallback).
    """
    spec = HARNESSES.get(harness_id)
    if spec is None or spec["dispatch"] == "internal":
        return None
    model_id = None if not model or model == DEFAULT_MODEL else model
    argv = list(spec["argv"])
    if model_id is not None:
        tokens = [t.replace("{model}", model_id) for t in spec["model_args"]]
        # Flags go *before* the positional prompt: the prompt swallows the tail
        # of argv in `run <MESSAGE>` grammars, and a flag after it is a word of
        # the prompt.
        if "{prompt}" in argv:
            i = argv.index("{prompt}")
            argv = argv[:i] + tokens + argv[i:]
        else:
            argv = argv + tokens
    return {
        "kind": "external",
        "harness": harness_id,
        "binary": binary or spec["binaries"][0],
        "argv": argv,
        "promptVia": "file" if "{prompt_file}" in argv else "argv",
        "tmux": True,
        "interactive": False,
        "envNames": list(spec["env"]),
        "vendor": spec["vendor"],
    }


def native_model(arm: str, harness_id: str) -> str | None:
    """`<harness>/<native-id>` (D2) -> the native id, or None for `default`/absent."""
    prefix = f"{harness_id}/"
    if arm.startswith(prefix):
        rest = arm[len(prefix):].strip()
        return None if not rest or rest == DEFAULT_MODEL else rest
    return None


# --- the results the CLI and doctor print ----------------------------------------

def list_result(
    which: Which | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """The catalog as a table: what RLP knows, and whether it is on PATH. No spawn."""
    e = dict(os.environ) if env is None else env
    rows = []
    for harness_id, spec in HARNESSES.items():
        found = None if scan_disabled(e) else resolve_binary(harness_id, which)
        rows.append(
            {
                "harness": harness_id,
                "title": spec["title"],
                "oneLiner": spec["one_liner"],
                "vendor": spec["vendor"],
                "headless": spec["headless"],
                "dispatch": spec["dispatch"],
                "armGrammar": "rpi-bin (internal)" if spec["dispatch"] == "internal" else f"{harness_id}/<model> or {harness_id}/default",
                "present": bool(found),
                "binary": found,
            }
        )
    return {"disabled": scan_disabled(e), "harnesses": rows}


def scan_result(
    which: Which | None = None,
    env: dict[str, str] | None = None,
    home: Path | None = None,
    probe: Probe | None = None,
    versions: bool = True,
) -> dict[str, Any]:
    """The full answer: present *and* logged in *and* which version. One spawn per hit.

    `versions=False` keeps it spawn-free for `rlp progress`, which runs on every
    session start; auth and PATH checks are file reads, so they still happen.
    """
    e = dict(os.environ) if env is None else env
    if scan_disabled(e):
        return {"disabled": True, "harnesses": [], "tmux": {"present": False, "binary": None, "version": None}}
    w: Which = _default_which if which is None else which
    p: Probe = _default_probe if probe is None else probe
    rows = []
    for harness_id, spec in HARNESSES.items():
        found = resolve_binary(harness_id, which)
        state = auth_state(harness_id, e, home)
        rows.append(
            {
                "harness": harness_id,
                "title": spec["title"],
                "vendor": spec["vendor"],
                "dispatch": spec["dispatch"],
                "present": bool(found),
                "binary": found,
                "version": (p(found, spec["version_cmd"]) if found and versions and spec["dispatch"] == "template" else None),
                "auth": state["state"],
                "marker": state["marker"],
                "hint": state["hint"],
                "headless": spec["headless"],
                "oneLiner": spec["one_liner"],
            }
        )
    tmux_path = w("tmux")
    tmux = {
        "present": bool(tmux_path),
        "binary": tmux_path,
        "version": (p(tmux_path, ["-V"]) if tmux_path and versions else None),
    }
    return {"disabled": False, "harnesses": rows, "tmux": tmux}


def render(result: dict[str, Any]) -> str:
    if result.get("disabled"):
        return "harnesses: scan off (RLP_HARNESS_SCAN=0) — only the bundled pi harness will be considered"
    lines = []
    for row in result["harnesses"]:
        mark = "present" if row["present"] else "absent"
        auth = "" if not row["present"] else f"  ({row['auth']}{', ' + row['version'] if row.get('version') else ''})"
        lines.append(f"{row['harness']:<8} {mark:<8}{auth:<44} {row['oneLiner']}")
        if row["present"] and row["auth"] == "needs-login" and row.get("hint"):
            lines.append(f"         fix: {row['hint']}")
    tmux = result.get("tmux") or {}
    lines.append(
        f"{'tmux':<8} {'present' if tmux.get('present') else 'absent':<8}"
        + (f"  ({tmux.get('version')})" if tmux.get("version") else "")
        + "     the observation lens: workers run in windows you can attach to"
    )
    return "\n".join(lines)


def one_line_summary(result: dict[str, Any]) -> str:
    """`claude, jcode, muse authed · omp needs login · tmux on` — for progress/doctor."""
    if result.get("disabled"):
        return "scan off (RLP_HARNESS_SCAN=0)"
    detected = [r for r in result["harnesses"] if r["present"] and r["dispatch"] == "template"]
    authed = [r["harness"] for r in detected if r["auth"] == "authenticated"]
    needing = [r["harness"] for r in detected if r["auth"] != "authenticated"]
    parts = []
    if authed:
        parts.append(f"{', '.join(authed)} authed")
    if needing:
        parts.append(f"{', '.join(needing)} need login")
    if not detected:
        parts.append("no external coding CLIs on PATH")
    parts.append("tmux " + ("on" if (result.get("tmux") or {}).get("present") else "off"))
    return " · ".join(parts)
