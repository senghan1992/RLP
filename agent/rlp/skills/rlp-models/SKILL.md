---
name: rlp-models
description: Which models this host can use — the RLP ladder arms, which are dispatchable, and which providers have no credential
---

# rlp-models

Answer "what models can I actually use, and which will RLP pick?".

```bash
rlp mode      # direct-only or full: does this host orchestrate at all? (source included)
rlp ladder    # the resolved ladder, arms in priority order, exclusions marked
rlp models    # every model grouped by provider, with credential state
```

`rlp models` lists **authenticated providers only** by default, because the
full catalogue on this host is 45 providers and ~1500 models and scrolling it
finds nothing. `rlp models --all` includes providers with no credential.

Prefer the `rlp_orchestration` MCP tool over the shell when it is available; it
returns the same ladder as structured JSON with `roster` and
`excluded_workers`.

## What to tell the user

- the **brain** arm — the model the orchestrator itself runs on
- the **worker ladder**, arms in priority order, first arm = where the bulk of
  the spend goes
- which workers are marked `available: false` and therefore never routed to
- providers that are configured but have **no credential** — those are models
  the user can see and cannot use, and it is the most common confusion

## Attaching another model or provider

In the harness — this is a terminal tool, and so is that:

```
/setup                                     the guided first run: endpoints, brain, worker arms, roles
/provider                                  endpoints, credential state, and the arms that cannot run
/provider connect                          guided: preset → endpoint → key → live GET /models → pick models
/provider add <id> <baseUrl> <model…>      the scriptable attach
/provider test <id>                        one real round trip, failure classified (auth / url / model / network)
/provider key <id>                         set or replace a credential (--drop removes it)
/provider models <id>                      ask the endpoint what it serves, then merge in what is missing
/login <provider>                          the harness's own OAuth/key flow, for a built-in provider
/models --pick                             switch model, or set the default
```

The same thing without a dialog, from any shell:

```
rlp provider list | probe <id> | discover <id>
rlp provider add <id> <baseUrl> <model…> [--key-stdin]
rlp provider key <id> [--drop] | remove <id> [--drop-key]
```

Every write is validated first, backed up, and replaced atomically; `auth.json`
is kept at `0600`; no command ever prints a key or takes one on argv (the TUI
pipes it to `--key-stdin`). Do not hand-edit `models.json` or `auth.json`: the
engine is the only writer that keeps those rules.

Adding an endpoint does not by itself make RLP use it. A model becomes usable by
the orchestrator only when it is a **ladder arm** (`/rlp-config add-arm`, or the
menu offered right after `/provider connect`) — otherwise it is a model the
harness can run and the router will never pick. `rlp provider list` says which
arms each endpoint carries, and `rlp doctor` reports the arms that cannot run.

## Session model vs default

This REPL's own `/model` sets the **orchestrator brain's** model for this
session. The harness default that new sessions start on is a different setting,
changed with `/models --pick` in `rpi`. Managed workers always take the model
the dispatch names, falling back to the ladder's first arm.
