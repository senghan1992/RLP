# Security

## Reporting

Open a private security advisory on the repository
(`Security` → `Report a vulnerability`), or open an issue if the report contains
no exploit detail. Please do not open a public issue with a working exploit
before there is a fix.

Expect an acknowledgement within a few days. This is a small project with no
security team; there is no bug-bounty programme.

## What RLP handles that matters

### Model credentials

RLP reads and writes two files, in its own agent dir (`~/.rlp/agent` by default —
it never writes to pi's `~/.pi`, though it can *read* pi's store to offer what
pi already has connected; see the rules below):

| File | Holds | Permissions |
|---|---|---|
| `<agent dir>/models.json` | endpoint URLs and model definitions | ordinary file |
| `<agent dir>/auth.json` | bearer tokens and OAuth credentials, per provider | forced `0600` |

`RLP_CODING_AGENT_DIR` moves the agent dir (`RPI_CODING_AGENT_DIR`, the harness's
own name for it, is still honoured); `$RLP_PI_MODELS` / `$RLP_PI_AUTH` relocate
the two files individually. They live under `~/.rlp`, never inside the checkout,
and `.gitignore` covers `auth.json`, `models.json`, `.env`, `*.pem` and `*.key`
as a second line of defence.

On install, an existing pi credential store is **copied** (never moved) into
`~/.rlp/agent` so an upgrade does not force you to reconnect every provider;
`RLP_NO_MIGRATE=1` skips that, and deleting the copies afterwards changes
nothing for pi. The same promise holds at runtime: `rlp provider scan` reads
pi's live store (`$RLP_PI_AGENT_DIR`, default `~/.pi/agent`) so `/setup` can
offer it, and `rlp provider import` copies what you choose — but only into
RLP's own store, never back into pi's.

The rules the code holds to, and where:

- **A secret is never printed.** `providers.credential_state()` returns
  `oauth` / `key` / `none`; the only function that returns the value is
  `providers.stored_credential()`, which exists for live checks.
- **A secret is never passed on a command line.** argv is readable by every
  process on the host via `ps`, so the TUI hands a key to the engine over a pipe
  (`rlp-svc provider … --key-stdin`) instead. The `--key` flag still exists for
  interactive shell use, and is documented as the weaker option.
- **Every write is validated, backed up, then atomic.** A bad argument is a
  `ValueError` before a byte is written; an existing `auth.json` is copied to a
  timestamped backup and replaced with `os.replace`; a store that exists but does
  not parse is refused rather than clobbered.
- **`auth.json` is `0600`** on creation, on rewrite, and on the backup. A
  world-readable credential store is a leak no later `chmod` undoes.
- **pi's store is read for existence, never for values.**
  `providers.pi_providers()` reports `"present"`/`"absent"` per provider from
  the shape of pi's auth entry; the value itself never enters a scan row, a
  wizard dialog, an import report or an error message. `providers.import_from_pi()`
  copies an entry verbatim (as parsed data — no text ever passes through a
  prompt, and unknown fields survive because the harness is pi's fork and the
  entry means the same thing there), refuses a provider RLP already has rather
  than merging over it, and is checked by the offline suite and by
  `scripts/check-provider.mjs` against a sandboxed fake pi store.

### What a request can cause

RLP dispatches **headless coding agents** that edit files, run commands and
commit — with the same OS permissions as the session that started them. That is
the point of the tool, and it is the boundary worth understanding:

- Each node runs in **its own git worktree** and branch, so two workers cannot
  fight over the same files. When the project is not a git repository, nodes
  share the working tree and say so.
- The fan-out is **capped per turn** (`routing.maxDispatchesPerTurn`) and a
  worker is **killed by a watchdog** if it runs past `routing.workerTimeoutMs`.
- **RLP never merges, never pushes, and never force-pushes.** It commits on a
  branch and, with a GitHub remote, can open a PR. The human merges.
- There is **no sandbox**. A worker is a normal process. Do not point RLP at a
  request you would not run by hand in that repository.

### Running a worker in someone else's tool

A worker no longer has to run in RLP's own harness. The route a plan produces
can name an external coding CLI, and `rlp_dispatch` runs it headless — the same
worker contract, the same OS permissions, just a different program editing the
files. The catalog (`rlp-svc/rlp_svc/harnesses.py`) is the single source for how
each is invoked:

| harness | headless invocation | model flag | permission story |
|---|---|---|---|
| **pi** (bundled) | `rpi -p "<prompt>" --model <arm>` | `--model` | RLP's own harness; nothing new to trust |
| **omp** | `omp -p --no-session <prompt>` | `--model` | pi-family; `--no-session`, no approval prompt to skip |
| **claude** | `claude -p --dangerously-skip-permissions <prompt>` | `--model` | runs with the tool's own skip flag — see below |
| **jcode** | `jcode run <prompt>` | `-m` | subscription runner; takes one message and exits |
| **muse** | `muse exec --prompt-file <path>` | `--model` | reads the prompt from a `0600` file; JSONL events |

**Why `--dangerously-skip-permissions`.** Claude Code gates each file write and
command behind an interactive approval prompt. A headless worker has nobody at
its terminal to answer that prompt, so a run would simply stop. The flag hands
Claude its own "make the edits" decision and lets the worker proceed. The safety
boundary is *not* that prompt — it is the layer RLP controls, and it applies to
every harness equally: each node runs in its own git worktree on its own branch,
RLP never merges, pushes, or force-pushes (the human merges), the fan-out is
capped per turn, and a wedged worker is killed by the watchdog. A worker that
edits the wrong files in its own worktree has a blast radius of one branch that
is never merged without a person reading the diff.

**Overriding it.** The permission flags are one `argv` list per harness in the
catalog — plain data, not buried logic. To run Claude with a narrower flag (a
`--permission-mode`, or no skip at all), edit that `argv` in
`rlp-svc/rlp_svc/harnesses.py`; RLP never rewrites it silently. To keep a node
off an external tool entirely, leave that tool unmounted (a ladder with no
worker on it), or mark such a worker `available: false`.

Three rules hold across every harness:

- **The catalog knows env-var *names*, never values.** A driver record carries
  `envNames` (`["ANTHROPIC_API_KEY", …]`) for preflight and observability — and
  a secret that reached a driver record would land in a ledger and a log — so
  the record never holds a key. An external worker is spawned with the session's
  own environment; RLP injects no credential into it.
- **Login is checked as existence, never contents.** Whether an external tool is
  usable is decided by whether its marker file *exists* or a named env var is
  *set* — `~/.claude.json`, `~/.jcode/auth.json`, and so on. RLP reads only that
  a file is there, never what is in it, and never opens `~/.claude` or any
  provider's credential store.
- **The prompt is a `0600` file.** Each dispatch writes `<node>.prompt.txt` into
  the run directory `0600` before spawning. Small prompts also ride the command
  line, but the file is always there — it is what `{prompt_file}` harnesses
  (Muse) read, and it is the post-mortem for the rest. Run ledgers live under
  `~/.rlp/runs/`, outside any checkout and never committed.

And the tmux lens keeps its own lane: workers run on **RLP's private socket**
(`tmux -L rlp`, sessions `rlp-<run>-<node>`), which a stray `tmux ls` on the
user's default server never shows. RLP **never attaches to, sends keys into, or
kills a session on the user's own tmux server** — the only sessions it touches
are the ones it named on that socket, and stopping a worker means
`kill-session` on its own.

### Untrusted input

Two things RLP reads are attacker-influenceable in the general case, and are
treated as data rather than instructions:

- **Model output** (the DAG, the critic's repair, worker reports). It is
  validated: ids, domains, sizes and dependency edges are coerced or rejected,
  and an invalid DAG falls back to the plain-LLM decomposer rather than running
  something unvalidated.
- **`GET /models` listings and provider error bodies** are parsed for ids and
  truncated; they are never executed or eval'd.

Worker output is fed to the *next* worker as context, so a worker can influence
a downstream worker. That is inherent to orchestration, and the mitigation is
the same one a human reviewer provides: review nodes and `rlp_verify` exist to
judge a node's work with a different model, and the gate table is printed before
anything is dispatched.

## Out of scope

- A compromised model provider, or a model that is simply wrong.
- The security of the pi harness, the fork, or any third-party extension in the
  shared agent directory (RLP records which files are its own; the rest are not
  its code and are never a dependency).
- Anything reachable only from a session the attacker already controls.