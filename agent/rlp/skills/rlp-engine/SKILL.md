---
name: rlp-engine
description: Run RLP's decision engine on a request from this REPL — triage gate, then DAG, routing and dispatch waves, without dispatching
---

# rlp-engine

The RLP decision engine, reachable from this REPL. Use it to decide **what**
should happen to a request before spending anything on doing it.

Two surfaces exist for the same engine, and this skill is the one that works
here (in the `rlp` REPL). The shell and the `rpi` harness have their own
equivalents; neither is reachable from this screen.

| I want | in this REPL | in `rpi` | in a shell |
|---|---|---|---|
| the gate alone | `/rlp-triage` | `/rlp-triage` | `rlp triage "…"` |
| the full plan | `/rlp-engine` | `/rlp-plan` | `rlp plan "…"` |
| host health | `/rlp-doctor` | `/rlp-doctor` | `rlp doctor` |
| the ladder | `/rlp-models` | `/rlp-ladder` | `rlp ladder` |

## How to run it

Prefer the MCP tools, which are already wired into this session:

1. `rlp_triage(request, context)` — one non-autoregressive forward pass.
   - `mode: "direct"` → do the work yourself. No DAG, no workers, no worktrees.
   - `mode: "orchestrate"` → run the pipeline.
   - `escalate: true` → default to `direct`; escalate only by naming ≥2
     independent deliverables.
2. `rlm_decompose(request, context)` — a validated DAG of 2–12 nodes.
3. `laya_route(title, brief, domain)` — per node, with the roster omitted so it
   is derived from the installed ladder.
4. `rlp_orchestration()` — the resolved ladder plus the derived router roster
   and `excluded_workers`.

If a tool is unavailable, fall back to the shell and report the output verbatim:

```bash
rlp plan "<request>"     # gate -> DAG -> routes -> waves
rlp triage "<request>"   # the gate alone
```

`rlp mode` says whether this host orchestrates at all. In **direct-only mode**
(`routing.gate: "direct"`, or `RLP_DIRECT=1` for one session) every request
answers `direct` with `engine: "direct-mode"`, no decomposition is ever built,
and the decision model is not loaded — so do not "warm up" a plan that cannot
fan out. An explicit `--mode` still outranks the mode, which is the way out when
somebody asks for a fan-out by name.

`rlp plan --mode orchestrate --because "…"` forces orchestration when the gate
is unsure; `--because` is the justification and is recorded in the output, so
give a real one. An orchestrate plan pays a cold laya load (~170 s on CPU) and
a decomposition call; a direct plan costs one forward pass.

## Reporting

Give the mode, the engine that actually ran, and — for a direct verdict — one
line saying what you did instead. For an orchestrate plan, post the gate table
(id | title | agent | model | deps | acceptance) and the waves **before**
dispatching, then act in the same turn. Never merge; the human merges.
