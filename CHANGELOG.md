# Changelog

Notable changes, newest first. RLP follows `MAJOR.MINOR.PATCH`: the ladder and
the engine's JSON envelopes are the interface, and a change to either that a
caller can observe gets a minor bump. The version lives in three places
(`rlp-svc/pyproject.toml`, `rlp_svc/__init__.py`, `rlp_svc/cli.py`) and the
offline suite fails if they drift.

## Unreleased

### Added

- **`/setup`** — the guided first run, in one flow: doctor → endpoints (and the
  keys they are missing) → the orchestrator model → the worker arms (with the
  cross-vendor rule stated) → the planner-side role bindings → a re-check. Every
  step is skippable and reports exactly what it changed.
- **`/provider connect`** — the guided attach: preset (OpenAI, Anthropic,
  OpenRouter, Groq, DeepSeek, Qwen, Ollama, any local OpenAI-compatible server)
  → id → endpoint → key → a live `GET /models` to choose models from → write.
- **`/provider test [id]`** — one real completion round trip, with the failure
  *classified*: auth / not_found / rate_limit / server / network / tls /
  bad_url, each with a fix line. A 404 on the completion route re-checks
  `/models`, so "wrong model id" is told apart from "wrong URL".
- **`rlp provider`** — the same operations as a scriptable CLI:
  `list | add | remove | key | discover | probe`, each with `--json`.
- **`rlp_svc/providers.py`** — one owner for `models.json` and `auth.json`:
  validated arguments, timestamped backups, atomic replace, `0600` on the
  credential file, and a corrupt store refused rather than clobbered.
- **`--key-stdin`** on `provider add|key|discover|probe`, so a credential can
  travel over a pipe instead of argv (where `ps` could read it).
- **`rlp doctor`** now reports the provider store: endpoints with no credential,
  and the ladder arms that therefore cannot run. The "the model is in the list
  but nothing runs" confusion has a line now.
- **`rlp doctor`** verifies RLP's own skills, and `install.sh` installs them —
  `/skill:rlp-*` was never shipped to the local plane.
- **`scripts/check-provider.mjs`** — drives the real harness over RPC, typing
  `/provider` and `/setup` and answering the dialogs, and asserts the report,
  the classified failure, and that a fully cancelled `/setup` writes nothing.
- `SECURITY.md`, describing the credential model and what a dispatch can do.

### Fixed

- **A plan can no longer be ended by one unhelpful arm.** The decomposer walked
  a single model; on a gateway that answers with an empty completion it failed
  with "no JSON object in response". It now tries the configured planner, the
  ladder's DEFAULT arm, then the fast route arm (and the same candidates for the
  plain-LLM contingency), and records which one answered (`planner`,
  `planner_fallback`).
- **An empty model reply is named, not returned.** `llm.chat` used to return
  `""`, which every caller reported as "no JSON in the response" — hiding the
  three real causes: a reply truncated at `max_tokens`, a reasoning model whose
  text went to `reasoning_content`, or a boilerplate empty completion. The
  error says `finish_reason` and which of those it was.
- **The status card lied.** Every engine exit code was collapsed to `0`, so
  `/rlp-doctor` always rendered as healthy and `/rlp` always claimed "runnable" —
  including on a host doctor had just failed on. Real exit codes propagate now.
- `/provider` no longer shows an endpoint list that says nothing about whether
  the orchestrator can use it: ladder arms are attached to their endpoint, and
  unreachable ones are named.
- `/models --pick` no longer opens an empty dialog when no provider has a
  credential; it says what to do instead.
- The TUI no longer writes `models.json` or `auth.json` directly.
- A stray tab in `pickModel` (`const inProvider = …;const model = …`) is gone.

### Changed

- `agent/rlp/extensions/` gained `rlp-provider.ts`; `menus.ts` keeps the ladder
  editor and the model browser, and no longer owns provider plumbing.
- The `rlp-models`, `rlp-commands` and `rlp-doctor` skills say what is true:
  `/provider` does write the credential store (it used to claim otherwise), and
  adding an endpoint is not the same as making it a ladder arm.

## 0.2.0

The first public release. RLP composes pi (a patched fork), laya (the
non-autoregressive triage and routing decision model), RLM (recursive,
model-driven decomposition) and an optional omnigent plane, and adds the piece
none of them have: a gate that decides whether a request deserves orchestration
at all, plus a local execution plane where a plan is a `spawn` and collection is
reading a file.

- `rlp` — the agent, triaging every request; `rlp plan | triage | decompose |
  route | replan | verify | ladder | roster | config | memory | doctor |
  serve | update`
- the orchestration ladder (`~/.pi/agent/orchestration.json`) as the single
  source for the brain, the worker arms, role bindings, the gate, the RLM knobs
  and the planning policy
- critique + repair of the DAG, recursive re-plan of a failed node, structured
  worker reports, and an independent cross-vendor best-of-N verdict
- per-project memory that survives across runs
- dropped-in slash commands (`/rlp`, `/rlp-plan`, `/rlp-triage`, `/rlp-doctor`,
  `/rlp-ladder`, `/rlp-config`, `/rlp-roles`, `/rlp-run`, `/commands`,
  `/models`, `/provider`) and RLP-owned skills
- CI: the offline suite over a stubbed model layer, a POSIX `sh -n` pass, and a
  live harness check for duplicate command registrations