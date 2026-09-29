"""Where RLP keeps its state — its own directory, not pi's.

RLP is a tool of its own, so it does not read or write pi's `~/.pi`. Everything
it owns lives under `~/.rlp`:

    ~/.rlp/agent/    the harness state: settings.json, auth.json, models.json,
                     sessions/, extensions/, skills/, orchestration.json
    ~/.rlp/runs/     per-run ledgers and worker logs          (`$RLP_HOME`)
    ~/.rlp/memory/   the per-project knowledge log            (`$RLP_HOME`)

`$RLP_HOME` moves the second and third only; the agent dir has its own override,
because a credential file should not move because someone relocated their logs.

Three things can move the agent dir, in order:

1. `RLP_CODING_AGENT_DIR` — RLP's own name for it, and the one to document.
2. `RPI_CODING_AGENT_DIR` — the name the patched harness derives from its
   `APP_NAME` (`rpi`). Still honoured, so an environment set up before RLP had
   its own directory keeps working.
3. `~/.rlp/agent` — the default, and the same value the fork's own
   `piConfig.configDir` produces, so the harness, the extensions and the engine
   cannot disagree about where the files are.

One function, four callers (`llm`, `orchestration`, `providers`, `doctor`) —
previously each derived the path itself, which is how a tool ends up writing two
different "auth.json"s.
"""
from __future__ import annotations

import os
from pathlib import Path

#: Env vars that relocate the agent dir, most specific first.
AGENT_DIR_ENV = ("RLP_CODING_AGENT_DIR", "RPI_CODING_AGENT_DIR")

#: Env var that relocates run ledgers and project memory.
HOME_ENV = "RLP_HOME"

DEFAULT_HOME = ".rlp"


def home() -> Path:
    """`$RLP_HOME`, else `~/.rlp`. Run ledgers, worker logs and project memory."""
    value = os.environ.get(HOME_ENV)
    return Path(value).expanduser() if value else Path.home() / DEFAULT_HOME


def agent_dir() -> Path:
    """The harness state directory: settings, credentials, models, sessions,
    extensions, skills and the orchestration ladder.

    Deliberately *not* derived from `$RLP_HOME`: that variable moves the run
    ledgers and the project knowledge log, which are RLP's data, while this is
    state the harness reads and writes. Pointing `$RLP_HOME` at a big disk
    should not silently relocate someone's credentials.
    """
    for var in AGENT_DIR_ENV:
        value = os.environ.get(var)
        if value:
            return Path(value).expanduser()
    return Path.home() / DEFAULT_HOME / "agent"


def orchestration_json() -> Path:
    """The ladder, in the agent dir — where the fork renders it from too."""
    return agent_dir() / "orchestration.json"


def described() -> str:
    """One line naming where this process thinks its files are, for a report."""
    source = next((v for v in AGENT_DIR_ENV if os.environ.get(v)), None)
    origin = f"${source}" if source else "default ~/.rlp/agent"
    return f"{agent_dir()} ({origin})"