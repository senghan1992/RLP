"""rlp memory — a per-project knowledge store that survives across runs.

A fan-out that learns nothing repeats its mistakes. RLP's run directories are
per-run and ephemeral; this keeps a small, append-only, per-repository log of
what earlier runs concluded — which arms failed, which acceptance was rejected,
what the reviewer found — and feeds a brief of it back into the next plan's
`context`, so the decomposer starts from what this repo already taught it.

Storage: `$RLP_HOME/memory/<repo-slug>/knowledge.jsonl` (`RLP_HOME` defaults to
`~/.rlp`). One JSON object per line, append-only, no server. The repo is keyed by
its git root when there is one, so the same project shares one memory across
checkouts of it.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import paths

KINDS = ("note", "decision", "pitfall", "artifact", "blocked")


def home() -> Path:
    """`$RLP_HOME`, else `~/.rlp` — the same rule the run ledgers use."""
    return paths.home()


def repo_root(cwd: str | None = None) -> Path:
    start = Path(cwd or os.getcwd()).resolve()
    for d in [start, *start.parents]:
        if (d / ".git").exists():
            return d
    return start


def repo_slug(cwd: str | None = None) -> str:
    root = repo_root(cwd)
    digest = hashlib.sha1(str(root).encode()).hexdigest()[:10]
    return f"{root.name or 'root'}-{digest}"


def knowledge_path(cwd: str | None = None) -> Path:
    return home() / "memory" / repo_slug(cwd) / "knowledge.jsonl"


def append(
    text: str,
    kind: str = "note",
    node: str = "",
    run: str = "",
    tags: list[str] | None = None,
    cwd: str | None = None,
) -> dict:
    """Append one entry. Returns it. Raises ValueError on empty text."""
    if not str(text).strip():
        raise ValueError("memory entry needs text")
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "kind": kind if kind in KINDS else "note",
        "text": str(text).strip()[:2000],
        "node": str(node)[:80],
        "run": str(run)[:40],
        "tags": [str(t)[:40] for t in (tags or [])][:8],
    }
    path = knowledge_path(cwd)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def load(cwd: str | None = None, limit: int = 40) -> list[dict]:
    """Recent entries, newest last. A missing or corrupt store reads as empty."""
    path = knowledge_path(cwd)
    if not path.is_file():
        return []
    entries: list[dict] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict) and item.get("text"):
                entries.append(item)
    except OSError:
        return []
    return entries[-max(1, limit):]


def brief(cwd: str | None = None, limit: int = 12) -> str:
    """A compact block for a planner's `context`, or '' when there is nothing."""
    entries = load(cwd, limit)
    if not entries:
        return ""
    lines = []
    for e in entries:
        where = f" [{e['node']}]" if e.get("node") else ""
        lines.append(f"- ({e['kind']}){where} {e['text']}")
    return "Knowledge from earlier RLP runs in this project:\n" + "\n".join(lines)


def summary(cwd: str | None = None, limit: int = 40) -> dict:
    entries = load(cwd, limit)
    by_kind: dict[str, int] = {}
    for e in entries:
        by_kind[e.get("kind", "note")] = by_kind.get(e.get("kind", "note"), 0) + 1
    return {
        "path": str(knowledge_path(cwd)),
        "slug": repo_slug(cwd),
        "count": len(entries),
        "by_kind": by_kind,
        "entries": entries,
        "brief": brief(cwd, min(limit, 12)),
    }