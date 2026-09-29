<p align="center">
  <img src="docs/banner.svg" alt="RLP — Recursive Laya Pi: a coding agent that knows when not to orchestrate" width="100%">
</p>

<p align="center">
  <a href="https://github.com/senghan1992/RLP/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/senghan1992/RLP/actions/workflows/ci.yml/badge.svg"></a>
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-4f46e5.svg"></a>
  <img alt="Python 3.10+" src="https://img.shields.io/badge/python-3.10%2B-3776ab.svg">
  <img alt="harness: pi" src="https://img.shields.io/badge/harness-pi-0f172a.svg">
  <img alt="orchestration: local" src="https://img.shields.io/badge/orchestration-local-22d3ee.svg">
</p>

<h3 align="center">A coding agent that knows when <em>not</em> to orchestrate.</h3>

Most "multi-agent" tools fan out on every request: a decomposer, a DAG table, a
worktree per node — even to fix a typo. RLP starts by deciding whether a request
deserves orchestration at all. One non-autoregressive forward pass through a
421M-parameter decision model answers `direct` or `orchestrate`; only work that
earns it is decomposed, routed across models, and dispatched to parallel workers
in isolated git worktrees. Everything else is handled inline like a normal
coding agent.

RLP composes four open-source projects, each used for what it is good at, plus a
decision layer that exists nowhere else:

- **[pi](https://github.com/earendil-works/pi)** — the terminal harness the agent
  and every worker run on (RLP builds a lightly-patched fork of it).
- **[laya](https://huggingface.co/convaiinnovations/laya)** — the
  non-autoregressive System-1 decision model used for triage and routing.
- **RLM (Recursive Language Models)** — recursive, model-driven decomposition of
  a request into a task DAG.
- **[omnigent](https://omnigent.ai)** *(optional)* — an older orchestration plane,
  reachable only via `rlp --omnigent`.

Local orchestration needs no server, no daemon, no runner: dispatch is a
`spawn`, collection is reading a file.

---

## Contents

- [Install](#install) · [First run](#first-run) · [How it works](#how-it-works) · [The model ladder](#the-model-ladder--configuration-not-prose)
- [Planning quality, verification, memory](#planning-quality-verification-and-memory)
- [Commands](#commands) · [Safety](#safety-and-guardrails) · [Troubleshooting](#troubleshooting)
- [Environment](#environment-knobs) · [Project layout](#project-layout) · [Development](#development)
- [Security](SECURITY.md) · [Changelog](CHANGELOG.md)

---

## Install

One line — fetches the installer, RLP, the harness, and the engine:

```bash
curl -fsSL https://raw.githubusercontent.com/senghan1992/RLP/main/install.sh | sh
```

Then, in any project:

```bash
cd ~/my-project
rlp                            # interactive agent — decides per request
rlp -p "add a --wc flag, test it, document it, and review the diff"
rlp plan "same request"        # plan only: gate -> DAG -> routes -> waves
```

Prefer a checkout? The bootstrap delegates to the same installer:

```bash
git clone https://github.com/senghan1992/RLP.git
cd RLP && sh scripts/install.sh
```

The installer fetches RLP into `${RLP_DIR:-~/.local/share/rlp}` first; override
with `RLP_DIR`, `RLP_REF` (branch/tag), or `RLP_REPO` — e.g.
`RLP_REF=v0.1.0 curl -fsSL … | sh`.

`rlp` is a superset of the harness: for solo work with no orchestration at all,
the same binary is available as `rpi` (RLP execs it internally, so you normally
never type it).

### Requirements

- `git`, `node` ≥ 18 with `npm`, `python` ≥ 3.10
- ~1.5 GB of disk (the harness, a CPU-only torch, and the laya checkpoint)
- Model credentials: RLP reads pi's `~/.pi/agent/auth.json` + `models.json`.
  Any OpenAI-compatible provider works. Configure them inside `rlp` with
  `/setup` (or `/provider connect`), which is the guided path; a hand-edited
  file works too, and `rlp provider` is the scriptable equivalent.
- `omni` (omnigent) is **optional** — only `rlp --omnigent` needs it.

The installer uses [`uv`](https://github.com/astral-sh/uv) when it is present and
falls back to a stdlib `venv` + `pip` otherwise.

### First run

```bash
rlp            # the agent, in the project you are working on
/setup         # guided: doctor → providers → brain → worker arms → roles
```

`/setup` asks the four questions that decide whether RLP can do anything at all,
in order, with the defaults stated: which endpoints exist and which have no
credential, which model orchestrates, which models work, and which roles they
cover. Every step is skippable and reports exactly what it changed. Afterwards
`/rlp-ladder` shows what will actually happen, and `/rlp-plan "<request>"`
shows the decision without running anything.

Attaching an endpoint on its own is `/provider connect`: pick from a preset
(OpenAI, Anthropic, OpenRouter, Groq, DeepSeek, Qwen, Ollama, any local
OpenAI-compatible server), paste a key, and then choose **from the list the
endpoint reports** over `GET /models` rather than typing an id from memory.
`/provider test <id>` does one real round trip and, when it fails, says *which*
failure it is — a rejected key, a wrong URL, a model id the endpoint does not
serve, no network, TLS — with a fix for each.

---

## How it works

```
request
  │  rlp_triage            laya, one forward pass -> direct | orchestrate
  ├─ direct ──────────────► stop. One edit, one test, one report. No DAG, no workers.
  │
  └─ orchestrate
       │  rlm_decompose   request -> 2–12 node DAG (recursive, validated)
       │  plan critique   a critic may repair the DAG before anything runs
       │  route           per node: worker + model (role binding or arm priority)
       │  waves           topological levels = one dispatch batch each
       │  dispatch        one headless worker per node, own git worktree + branch
       │  verify          an independent, cross-vendor best-of-N verdict
       │  remember        durable facts persist for the next run
       └─ synthesize      one report: what, where, verified how, what's left
```

The gate is the cheap decision made before the expensive one. Uncertain calls
default to **direct** — a single agent doing big work inline is still correct,
while forcing a one-line fix through a fan-out is pure waste. Because the bundled
laya checkpoint is weakly calibrated, the default gate is *hybrid*: when laya is
unsure, deterministic fan-out signals (an explicit "in parallel"/"delegate" cue,
an implementation plus an independent review, several deliverable verbs joined
into clauses) may raise the call to `orchestrate` and name the signal that fired.

**The human always merges.** RLP commits branches and can open PRs. It never
merges, never force-pushes, never touches a protected branch.

Deep dives: [docs/CONCEPTS.md](docs/CONCEPTS.md) (what it is and how to drive it)
and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) (a one-page diagram, Korean).

---

## The model ladder — configuration, not prose

Every model RLP can spend on lives in one file, `~/.pi/agent/orchestration.json`.
The harness renders it into the agent's prompt, the router derives its roster
from it, and the headless planner reads the same file — a model name is never
written twice.

```jsonc
{
  "brain": "agnes/agnes-3.0-flash",
  "workers": [
    { "id": "pi", "harness": "pi",
      "models": [
        { "model": "agnes/agnes-3.0-flash",
          "roles": ["code", "research", "docs", "review"],
          "when": "DEFAULT arm — most usage headroom; spend the bulk here" },
        { "model": "qwen-token-plan/deepseek-v4.1-flash",
          "roles": ["code", "debug", "review"],
          "when": "deep arm — multi-file refactors and hard debugging" }
      ] }
  ],
  "roles": { "review": ["qwen-token-plan/qwen3.8-flash"] },
  "routing": { "escalateBelow": 0.55, "maxDispatchesPerTurn": 4,
               "gate": "hybrid", "signalThreshold": 1, "workerTimeoutMs": 1200000 },
  "review": { "crossVendor": true },
  "rlm": { "maxDepth": 1, "maxIterations": 8, "maxConcurrentSubcalls": 4, "maxTimeout": 300 },
  "planning": { "critique": true, "maxRefines": 1, "recursiveDepth": 1,
                "artifactPassing": true, "verifySamples": 3 }
}
```

- **Arms are priority-ordered.** The first arm is the default — where the bulk of
  the spend goes; `when` is the operator's reasoning, kept next to the model.
- **`roles` binds a role to a model** (`code`, `debug`, `review`, `research`,
  `docs`, `explore`) or to an **ordered fallback list**, and it wins over
  priority order. Three roles name models the *planner itself* uses, never a
  worker: `plan` (the decomposer), `critique` (the plan critic), `verify`.
- **`available: false`** removes a worker from the router roster entirely, so
  the planner never dispatches onto an arm the host cannot serve.
- **Cross-vendor review is checked, not hoped for**: a review node inherits the
  ban list of the implementation it reviews and is re-picked onto another vendor
  family when the ladder allows.

### Change the models live

The ladder is editable from inside a session — no rebuild, no hand-editing JSON:

```
/rlp-config                 show the ladder, or open an edit menu
/rlp-config brain <ref>     make a model the orchestrator
/rlp-roles                  the model each role resolves to
/rlp-roles --pick           pick a role, then multi-select a provider's models
/models --pick              switch model · set default · add as a ladder arm ·
                            bind as the model for a role
```

Every edit is validated **before** it is written, keeps a timestamped backup, and
replaces the file atomically. The same edits are scriptable:
`rlp config '[{"op":"set_brain","model":"p/m"}]'`.

---

## Planning quality, verification, and memory

Effective fan-out is three things: the plan is checked before it runs, the
handoff between nodes is machine-readable, and the tool learns between runs.

- **Critique + repair.** After decomposition a cheap critic judges the DAG
  against the request (bad briefs, overlapping parallel work, missing edges,
  over/under-splitting) and may return a corrected DAG, validated before it is
  accepted. Bounded by `planning.maxRefines`.
- **Recursive re-plan.** A node that fails twice, or reports
  `acceptance: fail`, can be re-decomposed *as a node* — `rlp_replan(node)` splits
  it into a sub-DAG, re-parents its dependents, and makes the sub-nodes
  dispatchable. Bounded by `planning.recursiveDepth`.
- **Structured handoff.** Every worker writes a `report.json`
  (`{status, acceptance, files, commands, summary}`); dependents receive a short
  digest plus the **path** of each dependency report and read it themselves,
  rather than 12 kB of inlined text.
- **Independent verification.** `rlp_verify(node)` asks a **different vendor
  family** than the implementer for a best-of-N majority verdict on the node's
  acceptance and evidence (`planning.verifySamples`).
- **Project memory.** `rlp_remember` appends a durable fact (a pitfall, a
  decision, a rejected acceptance) to an append-only, per-project log at
  `~/.rlp/memory/`. `rlp_plan` folds a brief of it into the decomposer's context,
  so the second run in a project starts from what the first learned.

---

## Commands

### `rlp` — the agent, and the engine

| Command | What it does |
|---|---|
| `rlp` | the interactive agent; triages each request, orchestrates when it earns it |
| `rlp -p "<request>"` | the same, non-interactive |
| `rlp plan "<request>"` | the whole decision headless: gate → DAG → routes → waves (executes nothing) |
| `rlp triage "<request>"` | the gate alone, one laya forward pass |
| `rlp decompose "<request>"` | the DAG on its own |
| `rlp route --title T --domain D` | route one subtask to a worker |
| `rlp replan "<node brief>"` | recursively re-decompose one task into a sub-DAG |
| `rlp verify --acceptance A --avoid-family F` | independent cross-vendor best-of-N verdict |
| `rlp ladder` · `rlp roster` | the resolved ladder · the router's roster cards |
| `rlp provider list` | every endpoint: credential state, models, and the ladder arms it carries |
| `rlp provider probe <id>` | one real round trip; a failure comes back classified, with a fix |
| `rlp provider discover <id>` | ask the endpoint which models it serves |
| `rlp provider add <id> <url> <model…> [--key-stdin]` | attach an endpoint (validated, backed up, `0600` auth) |
| `rlp provider key <id> [--drop]` · `remove <id> [--drop-key]` | manage a credential · detach an endpoint |
| `rlp config '<ops-json>'` | edit the ladder (validated, backed up, atomic) |
| `rlp memory` · `rlp remember "<text>"` | the project's cross-run knowledge log |
| `rlp doctor [--warm]` | is this host runnable? one fix per failure |
| `rlp update [--check]` | update the pi fork and re-apply RLP |
| `rlp --omnigent` | the optional omnigent plane (session history + web UI) |

Add `--json` to any engine subcommand for the raw envelope. Exit codes:
`0` valid · `1` `ok:false` · `2` usage · `3` doctor found a failure.

### Inside a session (slash menu)

| Command | What it does |
|---|---|
| `/setup` | guided first run: doctor → endpoints → brain → worker arms → roles |
| `/rlp` | status card (ladder, brain, endpoints, unusable arms) + an action menu |
| `/rlp-plan <request>` | the headless plan, rendered in chat |
| `/rlp-triage <request>` | the gate verdict alone |
| `/rlp-doctor` | host health |
| `/rlp-ladder` | the resolved ladder in full |
| `/rlp-config` | show or edit the ladder (brain, arms, roles, gate, RLM knobs) |
| `/rlp-roles` | the model each role resolves to, and the menu to change it |
| `/commands [filter]` | the whole slash index, grouped (harness, RLP, skills, optional extensions) |
| `/models [filter]` · `/models --pick` | models by provider, with ladder and credential marks |
| `/provider` | endpoints, credential state, live connection test, and which arms cannot run |
| `/provider connect` · `add` · `test` · `models` · `key` · `remove` | the guided attach, the scriptable attach, a round trip, discovery, credentials, removal |

Reading commands change nothing. The ones that write back up first: `/rlp-config`
and `/rlp-roles` edit the ladder (validated, timestamped backup, atomic replace),
and `/provider` / `/setup` edit the ladder and the provider store under the same
rules — a credential is never printed and never placed on a command line.

---

## Safety and guardrails

- **Guardrails are configuration.** `maxDispatchesPerTurn` caps a turn's fan-out;
  `workerTimeoutMs` kills a wedged worker so collection cannot block forever.
- **Preflight, not hope.** A node whose arm has no credential, or whose harness
  this plane cannot spawn, is failed with a fix line instead of burning a
  dispatch (`rlp_plan` surfaces these under `preflight`).
- **Cross-vendor review** is verified against the ladder's families and reported
  as a violation when it cannot be satisfied.
- **Never merges.** Branches and (with a GitHub remote) PRs only.
- **Secrets stay out of the repo.** Credentials live in `~/.pi/agent/`, outside
  the checkout and git-ignored.
- **Credentials are handled as credentials.** `auth.json` is written `0600`;
  nothing ever prints a key (only `present`/`absent`); every write to it or to
  `models.json` is validated first, backed up, and replaced atomically; and a
  key is never passed on a command line, where `ps` could read it — the TUI
  hands it over a pipe (`--key-stdin`).

---

## Troubleshooting

```bash
rlp doctor            # every failure with a one-line fix (~0.2 s)
rlp doctor --warm     # plus one real laya round trip (~150 s cold on CPU)
rlp provider list     # endpoints, credentials, and the arms that cannot run
rpi                   # the bare harness, no orchestration — isolate the harness
```

| Symptom | Try |
|---|---|
| `rlp: decision engine not installed` | `sh scripts/install.sh` |
| `the laya decision model` seems stuck | first load is ~150 s on CPU; it is loaded once per session in the background |
| a request never fans out | the gate defaults to direct; use `rlp plan --mode orchestrate --because "…"` or ask explicitly |
| a worker dies instantly | `rlp doctor` — usually a missing credential for that arm (`/provider key <id>`, or `/login <provider>`) |
| a model is in the list but nothing runs on it | it is not a *ladder arm*: `/provider` names this under "ladder arms that cannot run", `/rlp-config add-arm` attaches it |
| a connection fails and you cannot tell why | `/provider test <id>` — a rejected key, a wrong URL, a bad model id, no network and TLS are told apart, each with a fix |
| `/settings` or `/model` default ignored | managed worker sessions follow `RPI_DEFAULT_MODEL`; interactive sessions keep your saved default |

---

## Environment knobs

| Variable | Effect |
|---|---|
| `RLP_ORCHESTRATION` | path to the ladder, overriding `<agent dir>/orchestration.json` |
| `RPI_CODING_AGENT_DIR` | the harness agent dir (`~/.pi/agent` by default) |
| `RPI_DEFAULT_MODEL` | the `rpi` session default (`provider/model`) |
| `RLP_DECOMPOSE_MODEL` · `RLP_CRITIQUE_MODEL` · `RLP_VERIFY_MODEL` | override the planner / critic / verifier model |
| `RLP_SKIP_CREDENTIAL_PREFLIGHT=1` | skip the per-arm credential check |
| `RLP_HOME` | where run ledgers and project memory live (`~/.rlp`) |
| `RLP_PI_REPO` · `RLP_PI_REF` | fork source + ref for `install.sh` |
| `RLP_REBUILD=1` · `RLP_ORCH_FORCE=1` | force a fork rebuild · overwrite the installed ladder |
| `SSL_CERT_FILE` | CA bundle (needed behind a TLS-inspecting proxy) |

---

## Project layout

```
RLP/
  install.sh          # the curl|sh bootstrap (also runs from a checkout)
  fork/pi/            # the pi fork — CLONED + BUILT by install.sh, never committed
  rlp-svc/            # the decision engine: MCP server + CLI + library
    rlp_svc/          #   triage, decompose (RLM + critique), route, verify,
                      #   memory, plan, orchestration ladder, providers,
                      #   doctor, engine, cli
  agent/rlp/          # the agent spec: dropped-in pi extensions + skills
    extensions/       #   rlp-orchestrate (local orchestration), rlp-provider
                      #   (endpoint/credential wizards), rlp-commands, menus
    skills/           #   /skill:rlp-* — workflow, models, engine, doctor, commands
    orchestration.json#   the default ladder
  scripts/            # install, rlp/rpi wrappers, selftest, fork patch,
                      #   check-harness + check-provider (live-session checks)
  docs/               # CONCEPTS.md, ARCHITECTURE.md
```

## Development

```bash
sh scripts/selftest.sh --fast   # extension typecheck + offline suite (seconds)
sh scripts/selftest.sh          # + real models, and the two live-session checks (~4 min)

node scripts/check-harness.mjs  # commands load, no duplicate registrations
node scripts/check-provider.mjs # /provider and /setup, driven through a live session
```

The offline suite stubs every model layer (including the provider transport) and
runs in milliseconds: `rlp-svc/.venv/bin/python -m rlp_svc.tests`. CI runs it,
plus a `sh -n` pass over the install scripts and a `node --check` over the two
harness checks — see `.github/workflows/ci.yml`. The live-session checks need a
built fork and installed extensions, so they run locally and in the full
selftest rather than in CI.

## Acknowledgements

RLP stands on [pi](https://github.com/earendil-works/pi) (MIT, © Mario Zechner),
[laya](https://huggingface.co/convaiinnovations/laya), and the RLM line of work on
recursive language models. It builds a patched fork of pi at install time; the
patch lives in `scripts/rlp-fork.patch`.

## License

[MIT](LICENSE).