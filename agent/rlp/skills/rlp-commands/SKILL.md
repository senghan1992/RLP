---
name: rlp-commands
description: What to type and where — the rlp session, the bare rpi harness, and the shell engine — plus which command answers which question
---

# rlp-commands

RLP is one binary with two identities, and knowing which one you are in is the
whole map:

- **`rlp`** — the agent. Same harness, plus the orchestration surface: the laya
  triage gate, the `rlp_*` tools, the resident decision engine, and the contract
  that tells the model when *not* to fan out. In **direct-only mode**
  (`/direct on`, `rlp mode direct`, `rlp --direct`) none of it is consulted: no
  gate, no workers, no decision model loaded — the harness and the guardrails,
  without the fan-out.
- **`rpi`** — the same harness with none of that. A plain coding agent, for when
  you already know the work is one thing and do not want a gate in the way.

Both load the same extensions, so every `/` command below exists in both. What
differs is that only `rlp` can orchestrate.

## In a session

| Command | What it does |
|---|---|
| `/setup` | the guided first run: mode → (coding tools already on this host) → endpoints → model → worker arms → per-role models (it starts by itself on a host that cannot work yet) |
| `/direct on\|off\|status` | direct-only mode: every request inline, never orchestrate |
| `/provider` | endpoints, credential state, a live round trip, the arms that cannot run |
| `/provider connect\|add\|test\|models\|key\|remove` | guided attach · scriptable attach · one real round trip · discovery · credentials · removal |
| `/rlp-plan <request>` | the gate → DAG → routing → waves, without dispatching anything |
| `/rlp-triage <request>` | the gate verdict alone, one laya forward pass |
| `/rlp-state` | the run ledger: every node, its arm, worktree, branch, status |
| `/rlp-watch` | the run's live worker windows and the attach command for each (tmux lens, read-only — RLP never attaches for you) |
| `/rlp-doctor` | host health, one fix per failure line |
| `/rlp-ladder` | what the orchestrator will actually do: brain, arms, roles, budgets |
| `/rlp-config` | edit the ladder live (brain, arms, workers, gate, budgets) |
| `/rlp-roles` | bind a role (code, review, plan, verify, …) to a specific model |
| `/models [filter]`, `/models --pick` | models by provider; switch one, or set the session default |
| `/commands [filter]` | the whole menu, grouped, including the shell side |
| `/orchestration` | the ladder as the model sees it in its own system prompt |

Plus the harness built-ins: `/clear`, `/compact`, `/context`, `/help`,
`/history`, `/login`, `/model`, `/new`, `/session`, `/settings`, `/theme`,
`/tree`, and the rest.

## In a shell

The decision engine is a library and a CLI, so anything with a request can ask
for a plan — a CI job, a git hook, a second tool — with no session at all:

```
rlp plan "<request>"          # gate + DAG + per-node routing + waves
rlp triage "<request>"        # direct vs orchestrate, one forward pass
rlp decompose "<request>"     # the DAG on its own
rlp ladder | rlp roster       # the ladder, and the router cards derived from it
rlp harness list | scan       # which tools can run workers here; what is installed (`--no-versions`: PATH only, spawns nothing)
rlp provider list|probe|discover|add|key|remove
rlp verify --acceptance ...   # independent cross-vendor best-of-N verdict
rlp digest [--run ID] [--wave N]  # condense a finished wave's reports into a handoff
rlp memory | rlp remember     # this project's cross-run knowledge log
rlp doctor [--warm]           # is this host runnable?
rlp serve                     # the same engine, as an MCP stdio server
rlp update [--check]          # update the harness fork, re-apply RLP on top
rpi                           # the bare harness, no orchestration
```

Add `--json` to any engine subcommand for a machine-readable envelope.

## Choosing

- **First run on a new machine** → `rlp`. It asks: the mode, the endpoints and
  their keys, the model to work on, the arms, the per-role models. `/setup`
  reruns the same flow by hand, `rlp doctor` says what is still missing in a
  shell, and `rlp progress` says how far from working this host is.
- **Deciding whether to orchestrate, without doing it** → `rlp plan` or
  `/rlp-plan`. Costs one forward pass and executes nothing.
- **Actually doing the work** → just ask `rlp`. The gate decides per request;
  you do not choose between "chat mode" and "orchestrate mode".
- **One thing, no gate at all** → `rpi`.
- **Attaching a provider** → `/provider connect` (guided) or
  `rlp provider add <id> <baseUrl> <model>` (scriptable).
- **Changing which models orchestrate** → `/rlp-config`, or `/rlp-roles` to
  pin one role. The ladder is configuration, not code.
- **Running workers on the coding CLIs you already use** → `/setup` looks for
  them on first run and mounts the ones you accept; `rlp harness list` shows
  the catalog and what this host has. A worker's `harness` field names the
  program, its arms name the model.
- **Watching a worker while it runs** → `/rlp-watch` for the attach commands
  (RLP's own tmux socket; `routing.tmux: off` skips the lens entirely).
