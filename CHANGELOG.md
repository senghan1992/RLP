# Changelog

Notable changes, newest first. RLP follows `MAJOR.MINOR.PATCH`: the ladder and
the engine's JSON envelopes are the interface, and a change to either that a
caller can observe gets a minor bump. The version lives in three places
(`rlp-svc/pyproject.toml`, `rlp_svc/__init__.py`, `rlp_svc/cli.py`) and the
offline suite fails if they drift.

## Unreleased

### Added

- **pi's own logged-in providers are now a source, not a retype.** RLP is
  pi's fork, so a key typed into pi once used to have to be typed into RLP
  again. `/setup` now offers them as one batch before the endpoint questions:
  `rlp provider scan` reads pi's store (`$RLP_PI_AGENT_DIR`, default
  `~/.pi/agent`) for *existence only* — a row says `present`/`absent`, and no
  credential value ever reaches a report, a dialog or an error — and
  `rlp provider import <id…>` copies the chosen entries verbatim into RLP's
  own store (validated, backed up, atomic, `0600`; unknown fields and OAuth
  refresh entries survive because the harness is pi and the entry means the
  same thing there). The copy is one-way — pi's files are never touched — a
  provider RLP already has is a refusal rather than a merge, `sameDir` hosts
  are told there is nothing to import, and the step is invisible when the
  scan finds nothing new. `scripts/check-provider.mjs` now builds its session
  from this checkout's extensions (a wizard check watching a stale installed
  copy cannot see the step under review) against a sandboxed fake pi store,
  asserting both the cancel path (nothing written) and the accept path
  (verbatim, 0600, one-way, nothing echoed).

- **A finished wave can now be digested into a handoff.** The honest input to
  a downstream worker is what the previous wave did — and pasting the raw
  report bundle into the prompt is the seed of context rot. `rlp digest
  --run <id> [--wave N]` hands the bundle to RLM as external context and asks
  for a compact handoff: what changed and where, what was proven, what the
  next workers must know, with the guard that a worker's report is a claim
  about a run and not an instruction. The envelope carries the instrument:
  `engine` (`rlm`, or `fallback-raw` when the deterministic condenser
  answered — a dead gateway still produces a handoff, never a stall), plus
  `raw_bytes` and `digest_bytes`. It is a capability, not a wiring: the
  executor does not call it between waves until the measured ratios say the
  digest beats the paste.

- **The decomposer can now be given an action to exit through.** RLM frames its
  input as a context to explore and asks for the answer at the end, while the
  decomposition prompt asked for JSON *now* — and the recorded consequence was
  the plain-LLM contingency quietly authoring most DAGs. Behind
  `RLP_RLM_DECOMPOSE=1` the framing splits: the model submits by calling
  `emit_dag(answer, tasks=[...])` inside the REPL, and because the call writes
  into the dict the library's own loop watches, submitting *is* exiting. A
  malformed submission returns guidance and leaves the run going — self-
  correction instead of timeout — and DAGs built this way report
  `engine: "rlm+emit_dag"`, which is how the spike gets judged: against `rlm`
  on the host gateway, by measurement rather than preference. With the flag
  off, the documented prompt is byte-identical to the one it was before.

- **`/setup` now looks before it asks.** The guided first run had grown a
  question for every external tool — *what is its name, what is its model* —
  on a host where RLP can simply see. Detection comes first: between the mode
  question and the endpoints question, `/setup` runs `rlp harness scan
  --no-versions` (PATH and login markers only, never a spawn, so a hung
  `--version` cannot stall a dialog), and any catalogued tool that is present
  but not yet on the ladder is offered as one batched multi-select —
  `claude — Claude Code (logged in)`, `jcode — jcode (needs login)`, and so on.
  Accepting adds each as a ladder worker carrying a single `<harness>/default`
  arm (the tool picks its own model); the whole batch is written only at the
  final confirm, so escape-everything still leaves the ladder byte-identical —
  the same promise the wizard has always made, now proven on the one scenario
  where a stray write is most likely (`check-first-ask --detected`). The step is
  un-numbered and skipped whole in direct-only mode, under `RLP_HARNESS_SCAN=0`,
  or when the scan finds nothing new, so the five core needles hold on a bare CI
  host and a tool-rich one alike. `rlp progress` reflects the result without
  recording it: the `ladder` milestone now names the catalogued external tools
  the ladder carries workers in, derived from the config each time — a pi-only
  ladder reads exactly as it did before.

- **The planner now reads the host, not just the ladder.** Two guards, both
  answering the same question — *is this worker real here?* First: an external
  harness whose binary is not on PATH marks its workers unavailable in memory,
  just before routing, through the very `excluded()` channel the operator's own
  `available: false` uses — the plan names them with a "not on PATH" note and
  a fix, a ladder whose every tool is missing degrades to an honest `direct`
  instead of a doomed dispatch, and `RLP_HARNESS_SCAN=0` claims nothing (the
  probe is a PATH lookup only — never a spawn — so offline hosts and CI are
  untouched). Second: the cross-vendor review rule gained its other axis. A
  review node's ban list now holds the *tool vendors* of the code it reviews
  beside the model families, so `anthropic/opus`-labelled arms on a claude-run
  worker are demoted when claude wrote the code — and a review that survives
  its arms but not its tool is reported as a violation naming the harness that
  collided. pi's own vendor never enters a ban list, so a pi-only ladder's
  plans are byte-stable; that is what the unmodified existing tests prove.

- **Workers can now actually run on other people's tools.** The route's driver
  stopped being paperwork: `rlp_dispatch` executes it. The argv template
  assembles into a headless `claude`, `omp`, `jcode` or `muse` in the node's
  own worktree, under the same worker contract pi has always had — the final
  `ACCEPTANCE:` line, the `report.json`, the promoted log — and `routing.tmux`
  decides only how it is *watched*: behind the lens a worker runs in a window
  on RLP's own tmux socket (`tmux -L rlp`, sessions `rlp-<run>-<node>`),
  `rlp_watch` and the new `/rlp-watch` name each window and its attach command
  (watching is the human's move; RLP never attaches for anyone), completion is
  the session going away plus its exit file, and the watchdog and
  `rlp_cancel` end the whole tree with `kill-session`. A host without tmux
  spawns the same workers plainly; the lens changes nothing about how a run is
  collected. The prompt rides a 0600 file (inline on argv only while it fits
  comfortably, and `{prompt_file}` tools read the file directly), a harness
  with no driver fails its *node* with the catalog's word rather than silently
  falling back to pi, and a plan on external harnesses no longer needs the `pi`
  binary at all. New `scripts/check-drivers.mjs` proves the lane end to end —
  three real catalog drivers spawned, contracts collected, a driverless harness
  refused, watchdog and cancel killing the tree — against fake CLIs on a
  sandbox path: it never calls a real tool and never needs a model, and it
  skips the lens lane honestly on a host without tmux.

- **A ladder worker can now name another harness.** The ladder schema grew two
  honest keys: a worker's `harness` means what the catalog says it means — arms
  on an external tool follow `<harness>/<native-id>` (`claude/sonnet`), and
  `<harness>/default` lets the tool pick its own model — and `routing.tmux`
  (`auto`|`on`|`off`, shipped default `auto`) is validated like every other
  routing knob. Both are visible in `rlp ladder` and reachable by the same
  validated mutation path (`set_worker` with `harness`, `set_routing` with
  `tmux`). A worker whose harness the catalog does not carry still parses —
  marked `harnessKnown: false` with a warning naming `rlp harness list` —
  because a plan that cannot run should say so loudly, not be silently
  rewritten. Every route record `rlp plan` emits now carries its `driver`: the
  argv template, prompt channel and resolved binary riding along as data, so
  the extension assembles and executes without knowing any tool by name.
  Credential judgement for external harnesses moved from `auth.json` to the
  catalog — a logged-in tool reads *present*, an unproven one reads *unknown*,
  and unknown never trips preflight, because a guess must not stall a run. The
  roster cards name each worker's tool and its auth state, so laya's existing
  "which worker" question is now, unmodified, a cross-harness tool-selection
  question. Dispatch still runs `pi` workers only; the tmux lens and the
  external drivers land in the next stage.

- **RLP now knows what other coding tools are on this host.** A new catalog
  (`rlp_svc/harnesses.py`) is the single source for the coding CLIs RLP can run
  workers in — `omp`, `claude`, `jcode`, `muse` beside the bundled `pi` — with
  each one's headless invocation as an argv *template*, where its credential
  lives (paths and env-var *names*, never values), and how to log in. `rlp
  harness scan` asks the host; `rlp harness list` prints the catalog. The
  drivers ride route records as data, so the brain extension assembles and
  spawns without knowing any tool by name — adding the next harness is one
  entry in one file. Workers still dispatch only to `pi` until the multi-harness
  stages land; this is the ground truth they will be planned against. `rlp
  doctor` gains a warn-only harness group (a tool present but not logged in
  names its own login command; absent tools are not news and get no line, and
  `bin:tmux` finally says whether workers can be watched), `rlp progress` ends
  with a one-line glance, and the resident engine answers the `harness` op.
  `RLP_HARNESS_SCAN=0` turns the whole feature off — CI and offline hosts pay no
  spawn for a guess — and the offline suite proves the catalog answers correctly
  on a host where nothing is installed.

- **A fresh install asks.** Starting `rlp` in a terminal on a host with no
  credential and no model arms now runs the guided setup by itself — the mode,
  then the endpoints and their keys, then the model RLP works on, then the
  worker arms, then the model for each role — instead of printing a line and
  waiting for somebody to know the word `/setup`. It is a TUI behaviour only
  (`RLP_NO_SETUP=1` is the quiet start, `rpi` and every dispatched worker never
  ask), the trigger is derived from `rlp progress` rather than recorded in a
  flag, and answering every question with escape writes nothing — which is what
  makes being asked again honest. `scripts/check-first-ask` drives the real TUI
  in a pty and fails if any of that stops being true.
- **Direct-only mode: `/direct on`, `rlp mode direct`, `rlp --direct`.**
  `routing.gate` gains a third value, and it means what it says: every request
  is handled inline, the gate never runs, `rlp plan` returns before a
  decomposition exists, the resident engine is never warmed, and the brain gets
  a short contract instead of the fan-out one. `$RLP_DIRECT` does the same for
  one session without touching the ladder, and `/direct off` clears it rather
  than writing a file that changes nothing. Every report tells the mode from a
  fault — `rlp doctor` says the ladder needs no arms *in this mode* and where
  the mode came from, `rlp ladder` leads with `mode:`, and `rlp progress`
  stops counting the four milestones that only exist for orchestration instead
  of reporting a finished install as stuck at 3/7.
- **`rlp mode [direct|full]`** — the two-word version of "does this host
  orchestrate", reporting the *effective* mode and what set it, writing through
  the same validated, backed-up config path `/direct` uses. `rlp config mode
  direct` and `/rlp-config gate direct` are the same key, not a second switch.
- **A forced plan is the mode's escape hatch.** `rlp_plan(force=true)` — and
  `rlp plan --force` · `rlp triage --force` — asks the gate on the *user's*
  instruction instead of taking the mode's answer, and stamps the plan
  (`force`, `forced_at`). `rlp_dispatch` honours that stamp for 15 minutes and
  nothing else: the run the user asked for is licensed, a `last-plan.json` left
  over from an hour ago is not. Explicit `--mode` outranks the mode as before,
  because a switch with no way out under pressure is a trap.

### Fixed

- **`rlp progress` did nothing.** The subcommand existed in the engine, was
  documented in its own report, and was missing from the launcher's dispatch
  list — so the word `progress` was handed to the harness as an argument and
  swallowed. `scripts/selftest.sh` now checks the launcher's list against the
  parser that owns it, which is the only way this class of gap fails loudly.

- **Every in-session RLP command was dead on the default install.** The
  extensions looked for the decision engine at `rlp-svc/.venv/bin/python`, but
  `RLP_ENGINE=system` — the default since the venv-less install — creates no
  `.venv` at all. `/provider`, `/setup`, `/rlp-plan` and the resident engine all
  reported "the decision engine is not installed" on a host where `rlp provider`
  worked fine from the shell. The interpreter install actually used is now
  recorded in `rlp-location.json`, and the extensions fall back to a PATH python
  that can `import rlp_svc`, following `scripts/svc-py`'s order instead of
  inventing a fourth one.
- **The interpreter walk stopped at `/provider`.** The entry above says *the
  extensions* fall back now; when it was written only `rlp-provider.ts` did.
  `rlp-orchestrate.ts` and `rlp-commands.ts` still probed `.venv` and nothing
  else — so `/rlp-plan`, the resident engine, and the whole brain's toolset
  still reported "the decision engine is not installed" on the default
  venv-less install, one file away from a `/provider` that worked. All three
  now run the identical walk (`svc-py`'s order: `.venv`, the recorded marker
  `python`, a PATH python that can `import rlp_svc`), duplicated per file on
  purpose because the loader makes every extension independent. A comment in
  each names the other two as the files to keep in step.
- **`/rlp-state` existed in four places and nowhere.** The onboarding copy, the
  skill, and the launcher all pointed at it; no extension ever registered it,
  so the one read-only view of a run was the command that could not be typed.
  It is registered now — in `rlp-orchestrate.ts`, beside `/rlp-run`, behind the
  same `IS_BRAIN` gate as the `rlp_state` tool it shares a renderer with, so
  the model's view and the person's view of the ledger cannot drift.

- **A first run said "connect a provider" to a host that had one.** In
  direct-only mode the ladder check still failed with the fix "run /setup", and
  the endpoint check kept pointing at a step that was already done. Each line
  now reports the mode it is in.


- **A fresh `curl | sh` failed for everyone.** `install.sh` cloned upstream pi's
  default branch and applied `scripts/rlp-fork.patch` to whatever it found
  there. Upstream had moved past the patch's base, so the patch no longer
  applied — and because the clone was `--depth 1`, it also had none of the blobs
  `git apply --3way` needs to recover, so the 3-way fallback could not run
  either. The install aborted at "patch did not apply cleanly". Nobody could
  install RLP.

  The upstream commit is now **pinned**, in `scripts/rlp-fork.base`, and the
  installer fetches exactly that commit (a by-sha `--depth 1` fetch, with a full
  clone as the fallback for a server that refuses one). An install is
  reproducible; moving upstream is `rlp update`'s verified job, not a side
  effect of installing today rather than yesterday. `RLP_PI_REF` still
  overrides. The patch and the base are one fact in two places and are
  regenerated together.
- **`rlp update` reported a missing git identity as a merge conflict.** A merge
  commit needs a committer, and a fresh machine — a CI runner, a container, a
  new laptop — often has none. The merge failed on "Committer identity unknown"
  and was announced as `CONFLICTS:` with an empty file list, which is a false
  diagnosis of the one thing that step exists to detect. It now supplies a
  fallback identity (only when none is configured, so a real one is kept), and a
  merge that fails with no unmerged paths says so instead of blaming content.

### Added

- **CI runs the live-session checks on every push.** They were parsed with
  `node --check` and never executed, because they need a built harness — so the
  entire TUI surface was covered by nothing automated. The new `live-session`
  job installs RLP for real, then drives `/provider` and `/setup` through a live
  session over RPC, runs the harness contract and the extension typecheck, and
  asserts the first-run report. It is affordable because of `RLP_SKIP_MODELS`.
- **`RLP_SKIP_MODELS=1 sh scripts/install.sh`** — install everything except the
  model stack: no torch, no laya, no rlm, no 400 MB checkpoint. Triage, routing
  and decomposition will not run; the harness, the extensions, `rlp provider`,
  `rlp ladder`, `rlp doctor`, the offline suite and every live-session check
  will. That subset is what made CI coverage of the TUI possible at all, and it
  is also what a container image or a docs build wants.
- **`scripts/check-first-run`** — asserts that a fresh install's `rlp doctor`
  report is one a person can act on: every non-ok line carries a fix, no fix
  names a slash command that does not exist, the lines that tell a new user what
  to do are present, and the *set* of failures is pinned so a new unresolvable
  line cannot appear unnoticed. This is the regression guard for the tool's
  loudest complaint — three warnings whose stated fix was to re-run the
  installer, which then skipped that step and changed nothing. Run by CI and by
  `scripts/selftest.sh`.
- **A scheduled `upstream-drift` job** (weekly, or on demand) runs the real
  `rlp update --no-self` against current upstream pi, then rebuilds, typechecks,
  and re-runs the harness and first-run checks. Pinning makes installs
  reproducible and also means nothing would notice upstream refactoring past the
  patch until a user ran `rlp update`. A failure there is not a broken release —
  installs are unaffected — it is notice that the next update will not work.
  Verified against current upstream at the time of writing: seven commits ahead,
  merge clean, all nine RLP markers intact, builds, typechecks.

- **Versioned releases.** `install.sh` resolved `RLP_REF` to `main`, so every
  `curl | sh` installed whatever was pushed most recently — a mid-refactor
  commit included. It now resolves the newest `vMAJOR.MINOR.PATCH` tag on the
  remote (via `git ls-remote`, so it works against any `RLP_REPO` and needs no
  API token) and says which one it chose; `RLP_REF=main` still tracks the edge,
  and a repository with no tags falls back to `main` with that stated. Tags are
  sorted numerically field by field, because a text sort puts `v0.10.0` before
  `v0.9.0` and that is the one comparison a release tool must not get wrong.
- **`scripts/release`** — the release command: verify, bump the single version,
  close `## Unreleased` as `## <version> — <date>`, commit, annotated tag. It
  refuses a dirty tree, a detached HEAD, a version that is not greater than the
  current one, an existing tag, an empty changelog section, a syntax error in
  any shipped script, or a failing offline suite. It never pushes — pushing the
  tag is the publish, so it stays a decision.
- **`.github/workflows/release.yml`** — on a `v*` tag push, re-verifies that the
  tag matches `rlp_svc.__version__` and that release notes exist, re-runs the
  offline suite and the syntax passes, then creates the GitHub release with the
  changelog section plus install and update instructions as its body.
- **`scripts/changelog-section`** — one extractor for a changelog section, used
  by both the annotated tag and the release body, so the tag and the release
  page cannot describe different things.
- **`rlp version`** now reports the build identity: the release, the checkout's
  `git describe` (including `-dirty`), the harness build, which engine
  dependencies are importable, the agent dir, and the host. It printed
  `rlp-svc 0.2.0` before — the engine's version, not the tool's — which left a
  bug report with no way to say *which* RLP. `--json` for the envelope.
- **A release-readiness job in CI**, on every push rather than only on tags: the
  version is well-formed and declared once, and there are notes to release.
  Learning at tag time that the changelog is empty means learning after the tag.

### Fixed

- **`rlp update` had never worked on an installed host.** `install.sh` applied
  the fork patch with `git apply` and never committed it, so every installed
  machine had a permanently dirty `fork/pi` — and `rlp update` refuses to start
  from a dirty tree, by design. The update path was therefore unreachable
  everywhere except a developer's checkout. The installer now commits the patch
  as one commit on the `rlp` branch, which is also what makes the upstream merge
  `rlp update` performs a real merge. Regenerate the patch with
  `git -C fork/pi diff "$(git -C fork/pi merge-base origin/main HEAD)" HEAD`.
- **`rlp update` now updates RLP itself**, not only the harness fork. It moved
  the fork forward and left RLP a release behind with nothing saying so. A new
  stage 0 fetches the newest release tag, checks it out, re-syncs the agent dir
  and re-execs the updated script (a script cannot go on running from a file it
  has just rewritten). A checkout with local changes is reported and left
  untouched — someone working on RLP is the likeliest person to run this, and
  resetting their tree would be the worst thing the command could do.
  `--no-self` keeps the old fork-only behaviour; `--self-ref <ref>` picks the
  target.
- `rlp update` ends by printing `rlp version`, so the release that is now
  running comes from the tool rather than from the updater's idea of it.
- **An up-to-date install was told it had an update.** `git rev-parse
  FETCH_HEAD` returns the *annotated tag object*, not the commit, so it never
  equalled `HEAD` and every `rlp update` did a pointless detach and re-sync.
  `scripts/release` writes annotated tags, so this was not an edge case — it was
  the only case. Both `rlp update` and the bootstrap now peel with `^{commit}`.
- `rlp update` on a host where the fork was never cloned printed a bare
  `cd: can't cd to …/fork/pi`. It now says that RLP itself is fine, that only
  the fork half needs it, and which command creates it.

### Changed

- **`scripts/sync-agent-dir`** now owns installing the extensions, skills and
  ladder into `~/.rlp/agent`, and both `install.sh` and `rlp update` call it. An
  update that moves the engine forward while leaving yesterday's extensions in
  place is exactly the half-applied state `rlp update` promises never to leave,
  and it is invisible — the commands still load, they just belong to another
  version. The sync also *reports* an extension a previous version owned and
  this one no longer ships, without deleting it.
- **The version has one declaration.** It was restated in `pyproject.toml`,
  `rlp_svc/__init__.py` and `cli.py`, guarded by a test that could only report a
  drift after it happened. `rlp_svc.__version__` is now the source; pyproject
  reads it through `[tool.setuptools.dynamic]` and `cli.VERSION` aliases it.

### Removed

- **The omnigent plane is gone**, and with it `rlp --omnigent`,
  `scripts/rlp-launch`, the `agent/rlp/config.yaml` agent spec, the
  `agent/rlp/agents/` sub-agent specs, step 4 of the installer, the
  `~/.omnigent` skill discovery in the fork patch, and `rlp doctor --no-host`.
  RLP is built on three things — pi, laya and RLM — and orchestration has been
  local since dispatch became a `spawn`; the plane was a vestige that still cost
  a maintenance surface and, worse, produced a report nobody could act on.

  Concretely: on a host without `omni`, `rlp doctor` printed three warnings
  (`bin:omni`, `agent-spec`, `harness-override`) whose stated fix was
  `sh scripts/install.sh` — which then printed `omnigent not found — skipping`
  and changed nothing. An unresolvable warning loop, with no explanation of what
  omnigent was or that it was not needed. The `bin:omni` hint was also simply
  false: it claimed `omni` was "needed to dispatch workers" long after dispatch
  had moved in-process.

  The group is replaced by `worker-harness` and `bin:git`, which ask the
  question that actually has an answer: *can this host start a worker?* The
  doctor now holds a rule — every `fail` and `warn` names a fix this host can
  act on — and a check whose only remedy is a step the installer deliberately
  skips does not belong in it.

### Changed

- **The shipped ladder names no model arms.** `agent/rlp/orchestration.json`
  carried `agnes/agnes-3.0-flash`, `qwen-token-plan/deepseek-v4.1-flash` and
  `anthropic/claude-opus-4-8` — one host's private gateway, with `when` prose
  that said "on this host" out loud. Anyone who installed RLP got a ladder whose
  every arm was an orphan, so nothing orchestrated and the reason was three
  steps away. It now ships **policy and no arms**: `"brain": null`, an empty
  `models` array, and real defaults for the gate, the dispatch cap, the worker
  watchdog, cross-vendor review and the RLM/planning budgets.

  "Installed but not configured" is now a first-class state rather than an
  invalid file. `orchestration.parse` accepts a null brain and an empty `models`
  array and reports `configured` and `arm_count`; `rlp ladder` prints
  `NOT CONFIGURED` with the fix; `rlp doctor` fails one line with `/setup` as
  the remedy; `rlp plan` answers `mode: "direct"` with
  `orchestration_unavailable` set — because with nothing to dispatch to,
  `direct` is the only truthful verdict *and* a perfectly good one. `decompose`
  and `verify` refuse with the same sentence instead of a transport error.
  `/setup` fills the arms from the endpoint's own `GET /models`.
- **The engine's own models come from the ladder, not from constants.** `llm.py`
  hardcoded `qwen-token-plan/qwen3.8-max` as the decomposer and
  `qwen-token-plan/deepseek-v4.1-flash` as the router. `route.py`, `triage.py`
  and `verify.py` called them with **no fallback**, so on any host but one the
  laya router's LLM fallback and the triage fallback were dead on arrival and
  the failure read "the model returned no JSON". Each engine role now resolves
  as: env override (`RLP_DECOMPOSE_MODEL`, `RLP_ROUTE_MODEL`,
  `RLP_VERIFY_MODEL`, `RLP_CRITIQUE_MODEL`) → the ladder's `roles.<role>`
  binding → the first arm declaring that role → the brain. `route` joins the
  bindable roles, because the gate's LLM fallback is the one engine call on
  every request's critical path and is usually worth pinning to a cheap arm.
- **The LLM fallbacks walk a candidate list.** `llm.role_candidates(role)` is
  the shared resolver — preferred spec, then role-matching arms, then every
  other arm, then the brain — and `route.llm_route` / `triage.llm_triage` go
  through `chat_first` like the decomposer already did. Both record which arm
  answered (`router_model`, `triage_model`). `_planner_fallbacks` uses the same
  resolver: resolving every role from the ladder had collapsed its chain onto
  the brain, which would have made "never stall" one arm long.
- `plan.FAMILIES` is gone. It listed three provider prefixes while `family()`
  already derived the family from the prefix, so it was a table that could only
  go stale.
- `/setup` no longer returns without a summary when no provider has a
  credential: it says which steps it is skipping and why, then closes with the
  doctor re-check and the next command. A wizard that ends silently at the one
  moment a first-time user needs guidance is not finished.
- `/setup` offers to add the chosen brain as the default arm when the ladder
  still has none. Choosing a brain and skipping the arm step is the likeliest
  path through a first run, and it used to end with a ladder that could not
  dispatch for a reason nobody would guess.
- `rlp doctor`: a crashed check group now carries a hint saying it is a bug in
  the doctor rather than in the user's setup; `ladder-arms-reachable` no longer
  reports "every arm is reachable" when there are no arms (vacuously true, and
  reading as `ok` beside a failing ladder line).
- `scripts/check-provider.mjs` watched `~/.pi/agent/orchestration.json` — stale
  since RLP moved to `~/.rlp` — so its "cancelling wrote nothing to the ladder"
  assertion was hashing a file that does not exist. It now resolves the agent
  dir the way everything else does, and asserts the shape of each `/setup`
  branch rather than a fixed dialog count.
- The test fixtures use generic provider names (`alpha`, `beta`, `gamma`). They
  document the ladder's *shape*, and one host's gateway names in them read as
  if RLP required those endpoints. A new `test_unconfigured_ladder` covers the
  whole first-run path, and `test_ladder_validation` asserts that the **shipped**
  ladder names no arms — the defect it catches is invisible until someone else
  installs the tool.

- **RLP keeps its own state under `~/.rlp`, not pi's `~/.pi`.** The fork's
  `piConfig.configDir` is `.rlp` and `paths.py` is the single rule every module
  and extension resolves the agent dir with (`RLP_CODING_AGENT_DIR`, then the
  harness's `RPI_CODING_AGENT_DIR`, then `~/.rlp/agent`). Settings, credentials,
  models, sessions, extensions, skills and the ladder all move together, so RLP
  no longer reads or writes another tool's directory — and a pi extension is no
  longer loaded by `rlp` (copy or symlink one into `~/.rlp/agent/extensions` to
  use it in both). `install.sh` copies an existing pi credential store into the
  new directory on upgrade (never moves it; `RLP_NO_MIGRATE=1` skips it), and
  `rlp doctor` prints which directory it resolved.
- Four modules derived the agent dir themselves; they now share `paths.py`, so
  the engine, the extensions and the harness cannot disagree about where
  `auth.json` is.

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

- **The completion budgets were sized for models that do not think.** Measured
  against the host gateway: one DAG request put 1968 *reasoning* tokens inside a
  2048-token budget, so a slightly longer train of thought truncates the answer
  to nothing and surfaces as "the model returned no JSON". `llm` now names the
  budgets (`DAG_TOKENS` 8192 for the decomposer, critic and the RLM call,
  `VERDICT_TOKENS` 1500 for the verifier, `JSON_LINE_TOKENS` 1024 for triage and
  routing) and every caller uses one. Both decomposer paths pass them down; the
  RLM library's own default was the same too-small number.
- **A hung gateway can no longer hold a plan open for the arm count times the
  timeout.** Following the candidate-arm walk above, `decompose` took 15 minutes
  on a three-arm ladder whose arms hung instead of erroring (found by running a
  fresh install and waiting). `rlm.maxTimeout` is now the budget for the whole
  walk — shared fairly across the candidate arms, with the last quarter reserved
  for the plain-LLM contingency — so the same call returns in 226 s with a usable
  DAG. A per-attempt stop is a stall in disguise: it is bounded only by the
  ladder's length.
- **`rlp doctor` no longer guesses which files RLP ships.** The expected extension
  and skill lists are read from the checkout this engine belongs to, instead of a
  literal list that went stale the moment a fourth extension was added (a fresh
  install was told that *three* files were installed while the fourth was checked
  by nothing). It also warns when the install marker records a file this version
  does not ship.
- **The slash index listed RLP's skills twice.** The same five skills exist in
  RLP's agent dir and in the agent spec (the REPL loads them from
  there); the index now lists a name once.

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
non-autoregressive triage and routing decision model) and RLM (recursive,
model-driven decomposition), and adds the piece none of them have: a gate that decides whether a request deserves orchestration
at all, plus a local execution plane where a plan is a `spawn` and collection is
reading a file.

- `rlp` — the agent, triaging every request; `rlp plan | triage | decompose |
  route | replan | verify | ladder | roster | config | memory | doctor |
  serve | update`
- the orchestration ladder (`~/.rlp/agent/orchestration.json`) as the single
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