---
name: rlp-commands
description: Which slash menu belongs to which screen — the RLP REPL, the rpi harness, or a shell — and what to use where
---

# rlp-commands

RLP has **two different TUIs with two different slash menus**, and confusing
them is the main reason a command "does not exist" or "does nothing". They are
separate programs, not one program with one menu.

## This screen: the `rlp` REPL (omnigent)

Built-ins: `/cancel /clear /compact /context /effort /fork /help /history
/logs /model /new /quit /report /switch /theme`, plus every user-invocable
skill — which is where the RLP commands below live.

| Command | What it does |
|---|---|
| `/rlp-engine <request>` | triage gate → DAG → routing → dispatch waves, without dispatching |
| `/rlp-triage <request>` | the gate verdict alone, one forward pass |
| `/rlp-doctor` | host health, one fix per failure |
| `/rlp-models` | which models are usable; the ladder; missing credentials |
| `/rlp-commands` | this map |
| `/model` | the **orchestrator brain's** model for this session — not the harness default |

There is no `/settings`, no `/login`, no `/scoped-models` here. Those belong to
the pi harness.

## The `rpi` harness (a coding agent)

Has the full pi menu — `/settings`, `/model`, `/login`, `/scoped-models`,
`/session`, `/tree`, `/reload` and so on — plus the dropped-in RLP extensions:

| Command | What it does |
|---|---|
| `/rlp`, `/rlp-plan`, `/rlp-triage`, `/rlp-doctor`, `/rlp-ladder`, `/rlp-run` | the engine, in-session |
| `/commands [filter]` | the whole menu, grouped, including the omnigent shell commands |
| `/models [filter]`, `/models --pick` | models by provider; switch one, or set the default |
| `/provider`, `/provider add\|remove` | attach or detach a provider endpoint |

Pi extensions load in the harness only. They do **not** appear in this REPL,
and REPL commands do not appear in the harness.

## A shell

`rlp <subcommand>` is the same engine, plus `rpi` and `omni`:

```
rlp plan | rlp triage | rlp decompose | rlp route | rlp ladder | rlp roster
rlp doctor [--warm] | rlp serve | rlp help-tool
rpi                    # solo agent, no orchestration
omni run|attach|session|config|doctor|usage|setup|server
```

## Choosing

- Deciding whether to orchestrate → `rlp plan` or `/rlp-engine`.
- Actually orchestrating → `rlp -p "…"` (this REPL, or a shell).
- Doing one thing, no fan-out → `rpi`.
- Attaching a model or provider → `rpi`, then `/provider add` or `/login`.
- Changing the model new sessions start on → `rpi`, then `/models --pick`.
