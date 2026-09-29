---
name: rlp-models
description: Which models this host can use — the RLP ladder arms, which are dispatchable, and which providers have no credential
---

# rlp-models

Answer "what models can I actually use, and which will RLP pick?".

```bash
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

Mutation happens in the `rpi` harness, not here — this REPL cannot write the
credential store. Tell the user to run `rpi` and then:

```
/provider                                  list endpoints and credential state
/provider add <id> <baseUrl> <modelId>     attach an OpenAI-compatible endpoint
/login <provider>                          set or replace a credential
/models --pick                             switch model, or set the default
```

Every one of those backs up the file it writes, and `/provider` never echoes a
secret. Do not attempt to edit `models.json` or `auth.json` from here.

## Session model vs default

This REPL's own `/model` sets the **orchestrator brain's** model for this
session. The harness default that new sessions start on is a different setting,
changed with `/models --pick` in `rpi`. Managed workers always take the model
the dispatch names, falling back to the ladder's first arm.
