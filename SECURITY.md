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
never pi's `~/.pi`):

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
nothing for pi.

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