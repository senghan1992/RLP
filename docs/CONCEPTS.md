# RLP — the concept

RLP exists because "run the multi-agent pipeline" is a reflex, and reflexes are
expensive. Most requests a coding agent receives are one edit, one question, or
one test run. Paying a recursive decomposer, a DAG table, a fan-out of sessions
and a worktree per node to fix a typo is not thoroughness — it is waste with a
process diagram.

So the first thing RLP does is **decide not to orchestrate**, and that decision
is made by a model that cannot talk: a 421M-parameter ModernBERT that reads
the request once, in a single forward pass, and picks `direct` or `orchestrate`
in 33–460 ms. No sampling, no generation, therefore no hallucination and no
loop to escape. A single agent doing a big job inline is still correct; a
one-line fix dragged through a fan-out is not.

The gate is not the interesting part on its own. The interesting part is that
**the rest of the orchestration is configuration, not prose** — and that both
the orchestrating brain and the headless planner read the same file.

---

## The three surfaces

RLP is one decision engine wearing three faces. All three read the same ladder
(`~/.pi/agent/orchestration.json`) and none of them can name a model the
ladder does not contain.

| Surface | Entry | What it is for |
|---|---|---|
| **Agent** | `rlp` | The tool: pi's TUI with the gate and the orchestration tools loaded. This is what you run. |
| **Planner** | `rlp plan "<request>"` · `/rlp-plan` | A pure function: request in, executable plan out, nothing executed. For a script, a bot, a CI check, or just reading. |
| **Harness** | `rpi` | The same binary with no RLP identity (`RLP_IDENTITY` unset): pi's own branding, no contract, no `rlp_*` tools, no engine. This is what workers run on — `rlp` is the brain identity that adds all of it. |

`rlp --omnigent` still reaches the older external plane, which owns the
conversation database and the web UI that local orchestration does not.

```
rlp plan "…"      think      (1 laya pass; instant for direct, ~4 min for orchestrate)
rlp -p "…"        do it      (plan + dispatch + collect + synthesize)
rpi               just work  (no orchestration at all)
```

`rlp help-tool` prints this surface. The subcommand split is not cosmetic:
planning is local and cheap, dispatching needs a session plane, and a caller
that only wants to know *what to do* should never pay for the second.

### The same surface, inside the session

A tool you have to leave the session to query is a tool you will not query, so
the engine is also in the slash menu. These commands come from dropped-in pi
extensions (`agent/rlp/extensions/*.ts`), not from the fork — adding a slash
command is a file drop plus `/reload`, never a rebuild.

**RLP engine**

| Command | What it does |
|---|---|
| `/rlp` | status card (ladder, brain, dispatchable vs excluded workers, cwd) + an action menu |
| `/rlp-plan <request>` | the headless plan, rendered in chat |
| `/rlp-triage <request>` | the gate verdict alone, one forward pass |
| `/rlp-doctor` | host health, one fix per failure |
| `/rlp-ladder` | the resolved ladder in full |
| `/rlp-config` | show the ladder, or edit it in place: brain, worker arms, per-role models, gate |
| `/rlp-roles` | the model each role resolves to, and the menu to change it: pick a role, then multi-select a provider's models (a priority chain) |
| `/rlp-run <request>` | composes `rlp -p "…"` into the editor — dispatch needs a real session |

**Finding things, and attaching models**

| Command | What it does |
|---|---|
| `/commands [filter]` | the whole menu, grouped: harness built-ins, RLP, skills, extensions, omnigent shell commands |
| `/models [filter]` | models grouped by provider — `●` this session, `★` your default, `⚑` ladder arm, `○` no credentials |
| `/models --pick` | provider → model → use for this session, set as default, add as an RLP ladder arm, or make it the orchestrator |
| `/provider` | every endpoint: baseUrl, model count, credential state |
| `/provider add <id> <baseUrl> <modelId> [name]` | attach an OpenAI-compatible endpoint, key pasted at the prompt |
| `/provider remove <id>` | detach one |

The command index reads the harness's own `BUILTIN_SLASH_COMMANDS` at runtime,
so it cannot drift the way a hardcoded list would.

All the RLP commands are read-only, and all resolve the checkout by walking up
from the session cwd, so they work in any project directory. `/orchestration`
remains the ladder-focused view; `/rlp` is the umbrella.

---

## The pipeline, and where each stage can be wrong

```
request
  │  rlp_triage            laya, one forward pass → direct | orchestrate | escalate
  ├─ direct ──────────────► stop. One edit, one test, one report. No DAG, no workers.
  │
  └─ orchestrate
       │  rlm_decompose   request → 2-12 node DAG (recursive, Kahn-validated)
       │  laya_route      per node → worker id + confidence (below 0.55 = advisory)
       │  arm selection   node domain + cross-vendor rule → provider/model
       │  waves           topological levels = one dispatch batch each
       │  collect         a blocking wait on the workers; failures re-dispatched once
       └─ synthesize      one report: what, where, verified how, what's left
```

Each stage degrades instead of stalling, and says which engine actually ran:

| Stage | Primary | Fallback | Reported as |
|---|---|---|---|
| triage | laya | one-turn LLM | `engine: llm` + `laya_error` |
| triage, unsure | fan-out signals (hybrid) | defaults to direct | `engine: laya+signals` + `signals` |
| decompose | RLM (recursive) | plain-LLM decomposition | `engine: fallback-plain-llm` + `rlm_error` |
| route | laya | one-turn LLM | `engine: llm` + `laya_error` |
| arm pick | ladder priority order | keep the worker's default arm | `why_this_arm` says which rule fired |
| dispatch | credential + harness preflight | fail the node with a fix | `preflight` in the plan, `credential` on the node |

### Plan quality loop, structured handoff, recursive re-plan

- **Critique + repair (self-refine).** After `rlm_decompose`, a cheap one-shot
  critic reads the request and the DAG and may return a corrected DAG (missing
  edges, overlapping parallel tasks, over/under-split, missing integration). The
  repair runs through the same validator, is bounded by `planning.maxRefines`,
  and lands in the plan as `plan_critique`. A failing critic leaves the first
  DAG in place. Toggle with `planning.critique`.
- **Structured handoff.** Each worker writes `report.json`
  (`{status, acceptance, files, commands, summary}`); `rlp_collect` prefers it
  over prose. Dependents get a digest plus the report **path** and read it
  themselves, rather than 12 kB of inlined text (`planning.artifactPassing`).
- **Recursive re-plan.** `rlp_replan(node, why)` re-decomposes one failed node
  in RLM `focus` mode, routes the sub-DAG, and injects it: dependents are
  re-parented onto the sub-DAG's leaves, sub-nodes become dispatchable. Bounded
  by `planning.recursiveDepth`. This is RLM's recursion mapped onto the executor.
- **RLM knobs.** `rlm.maxDepth|maxIterations|maxConcurrentSubcalls|maxBudget|`
  `maxTimeout` are passed through, so recursion is configured, not accidental.
- **Planner-side models are ladder roles.** `plan`, `critique` and `verify` name
  the models the planner itself calls; they resolve like worker roles
  (`RLP_DECOMPOSE_MODEL`/`RLP_CRITIQUE_MODEL`/`RLP_VERIFY_MODEL` override the
  ladder), and are set with `/rlp-roles plan|critique|verify <model>`.
- **Independent verification (best-of-N).** `rlp_verify(node)` asks a *different
  vendor family* than the implementer for a majority verdict on the acceptance
  and evidence (`planning.verifySamples`). Maps to
  `rlp-svc verify --acceptance … --avoid-family <impl-family> --samples N`.
- **Project memory.** `rlp_remember` appends a durable fact to
  `~/.rlp/memory/<repo>/knowledge.jsonl`; `rlp_plan` folds its brief into the
  decomposer's context (`memory_brief_chars` on the result) and `rlp_memory`
  reads it back. Keyed by git root, shared across checkouts, append-only.

**The human always merges.** RLP commits branches and opens PRs. It never
merges, never force-pushes, never touches a protected branch.

---

## The engine is resident, and why that matters

laya is a 421M-parameter checkpoint. Measured on this host: **~150 s to load,
~4–8 s per decision** on CPU. Read that second number against the design — the
gate is supposed to be the cheap decision made before the expensive one — and
the load time is the problem, not the decision.

Invoked as a subprocess it was a fresh process per call, so the load was paid
every time, and one orchestration paid it twice because dispatch re-ran the
plan. Measured before the fix:

```
$ time rlp triage "fix a typo"   # 153 s
$ time rlp triage "fix a typo"   # 157 s   (again, from scratch)
```

So the agent starts one engine process when the session opens
(`rlp_svc.engine`, JSON lines over a pipe), loads the model in the background
while you are still typing, and keeps it. After that: **~4 s per decision**, and
ladder/ledger reads are instant.

| | process per call | resident engine |
|---|---|---|
| first decision | ~155 s | ~155 s, started at session open rather than at the request |
| second onwards | ~155 s each | ~4–8 s each |
| after a plan | dispatch re-ran the plan (~150 s) | dispatch reuses the stored plan (0 ms) |

Two consequences worth stating:

- **Dispatch executes the plan you were shown.** Re-planning there was also a
  correctness hole: the gate table in the transcript could differ from what got
  dispatched. The plan is stored, in memory and on disk.
- **The engine is a fallback, not a single point of failure.** If it cannot
  start, the CLI answers instead — same shapes, still correct, still slow.

## The ladder is the configuration

One file decides who does what, and it is read three ways: the harness renders
it into the brain's `<rlp_orchestration>` system-prompt section, `rlp ladder`
prints it, and the router derives its roster from it. A model name is never
written twice.

```jsonc
{
  "brain": "agnes/agnes-3.0-flash",
  "workers": [
    {
      "id": "pi", "harness": "pi",
      "models": [
        // Arms are priority-ordered. The FIRST arm is the default: that is
        // where the bulk of the spend goes. `when` is the operator's
        // reasoning, kept next to the model it justifies.
        { "model": "agnes/agnes-3.0-flash", "roles": ["code","research","docs","review"],
          "when": "DEFAULT arm — carries the most usage headroom on this host …" },
        { "model": "qwen-token-plan/deepseek-v4.1-flash", "roles": ["code","debug","review"],
          "when": "the deep arm — multi-file refactors and hard debugging …" }
      ]
    },
    {
      "id": "claude_code", "harness": "claude-native",
      // Availability is configuration, not prose. An arm the host cannot
      // serve is excluded from the roster, so a plan is never dispatched
      // onto it. Omit the field to re-enable.
      "available": false,
      "availabilityNote": "Claude entitlement is routinely exhausted on this host; opt-in.",
      "models": [ { "model": "anthropic/claude-opus-4-8", "roles": ["code","review"], "when": "…" } ]
    }
  ],
  "routing": { "escalateBelow": 0.55, "maxDispatchesPerTurn": 4 },
  "review": { "crossVendor": true }
}
```

`rlp ladder` shows the resolved file; `rlp roster` shows the cards the decision
model actually sees; `rlp doctor` validates both. A malformed ladder fails at
load naming the exact field. An absent ladder means no section and plain-pi
behaviour.

**It is editable in-session.** The same file the harness renders is written by
the engine's validated `config` op, so `/rlp-config` (menu or
`brain|add-arm|set-arm|move-arm|rm-arm|worker|gate|escalate|cap|timeout|cross-vendor`)
and the *add to the RLP ladder* / *make it the RLP orchestrator* actions in
`/models --pick` change the orchestrating models without a rebuild. Every edit
is validated *before* it is written, keeps an `orchestration.json.bak.<ts>`
backup, and replaces the file atomically; the harness re-reads it on the next
prompt rebuild (it caches on the file's mtime). `rlp config '<ops-json>'` is
the scriptable form.

### Models per role

Arm priority is a fine default but a blunt instrument for "which model should
review?": you want to name the model for the *role*, the way `/model` names the
model for the session. So a role can be bound explicitly:

```jsonc
"roles": { "review": ["qwen-token-plan/qwen3.8-flash",
                       "qwen-token-plan/deepseek-v4.1-flash"],
           "debug":  "qwen-token-plan/deepseek-v4.1-flash" }
```

A role may bind to **one model or an ordered list** of them. A list is a
preference/fallback chain: the planner uses the first entry a dispatchable
worker can serve, and the plan records which entry won (`role_chain`) or, if none
can run, reports it (`binding_warnings`) instead of hiding the mismatch.

```
/rlp-roles                  every role -> chain, and which entry resolved
/rlp-roles --pick           pick a role, then a provider, then multi-select its models
                            (numbers/ranges, or "all") — order is priority
/rlp-roles review qwen-token-plan/qwen3.8-flash,qwen-token-plan/deepseek-v4.1-flash
/rlp-roles review off       back to arm priority
```

### Cross-vendor review is checked, not hoped for

A review node inherits the ban list of every implementation it depends on. If
its default arm would land on the same vendor family, the planner re-picks the
next arm on another family and records `why_this_arm`. If the ladder offers no
other family, the plan still proceeds — but it reports
`cross_vendor_violations` rather than pretending the rule was satisfied.

---

## When the gate is wrong

The bundled checkpoint is weakly calibrated for this question shape: measured
confidence runs 0.003–0.50, so most calls land below `escalateBelow`. A
therefore-always-unsure gate that blindly defaults to `direct` makes the
orchestrator depend on a manual override every time, so the default gate is
**hybrid** and not laya-alone:

- laya makes the System-1 pass, as before;
- if laya is *unsure*, deterministic fan-out signals in `triage.py` decide — an
explicit cue ("in parallel", "delegate", "each with", …), an implementation
plus an independent review, or three deliverable verbs joined into clauses;
- a signal raises the call to `orchestrate` and the envelope records
`engine: laya+signals` and the `signals` block, so the reason is visible;
- a *confident* laya `direct` is never overridden — the cheap,
always-correct-enough default stands.

Set `routing.gate: "laya"` to restore the old behaviour. The brain's contract
still applies on top: the gate sets the direction and the safe default, and the
brain may override only by naming two or more independent deliverables.
`rlp plan` makes the same override available, and records it as an assertion
rather than a measurement:

```bash
rlp plan --mode orchestrate --because "code, tests, docs, and a review are four deliverables" "…"
# gate: OVERRIDDEN -> orchestrate (code, tests, docs, and a review are four deliverables)
```

`--mode direct` is the cheap override: it skips the laya pass entirely, which
is how a caller says "this is not worth thinking about" without paying for the
thinking.

Confidence numbers from affected choice buckets are clamped by the checkpoint
and should be read as relative, not calibrated.

---

## Using it as a library

The engine is importable, so the gate is available to anything with a request
and an opinion about fan-out:

```python
from rlp_svc import plan, triage, doctor

decision = triage.triage("add a --wc flag and a test")   # {"mode": "direct", …}
blueprint = plan.plan("add a payments module, document it, review the diff",
                      mode="orchestrate", because="three deliverables")
blueprint["result"]["waves"]     # [["t1", "t2"], ["t3"]] — one dispatch batch per wave
doctor.run()["ok"]               # is this host able to run any of it?
```

Every function returns an envelope, `{"ok": true, "result": …}` or
`{"ok": false, "error": …}`. None of them raise on a bad request; a plan is a
decision, not an operation.

---

## Operating it

```bash
rlp doctor            # deps, credentials, ladder, checkpoint, host wiring — 0.2 s
rlp doctor --warm     # plus one real laya round trip (~170 s cold, honest about it)
/rlp-doctor           # the same report, inside a session
/commands              # the whole slash index, grouped
/models                # models by provider, with ladder and credential marks
/provider              # endpoints and which ones have no key
sh scripts/selftest.sh --fast   # offline suite: planner shape, validation, CLI
sh scripts/selftest.sh          # + the real models (~4 min)
```

`doctor` grades instead of failing flat: `ok` / `warn` / `fail`, with the fix
on the failure line. A missing ladder, an unparseable credential file, a
missing checkpoint, an unwired omnigent harness and a single-family ladder
claiming `crossVendor: true` are all named explicitly.

### Optional integrations

RLP depends on nothing outside its own checkout: the fork, `rlp-svc`, the ladder
and its three dropped-in extensions. The harness agent dir is shared with
extensions RLP does not own — `myviking.ts` (a knowledge library), and whatever
else the user installed. Those are optional by construction: RLP records its own
file list in `rlp-location.json`, `/commands` shows them under a separate
*optional — not part of RLP* heading, and `rlp doctor` verifies only RLP's own
and never fails on the others. No tool RLP ships names a third-party extension,
so removing myviking cannot change a plan or a dispatch.

### Environment knobs

| Variable | Effect |
|---|---|
| `RLP_ORCHESTRATION` | ladder path, overriding `<agent dir>/orchestration.json` |
| `RPI_CODING_AGENT_DIR` | harness agent dir (moves ladder, sessions, skills together) |
| `RLP_IDENTITY` | `rlp` = brain (contract+tools+engine); unset/`worker` = bare harness |
| `RPI_DEFAULT_MODEL` | the `rpi` session default, beating saved settings |
| `RLP_DECOMPOSE_MODEL` | decomposer model, `provider/model` |
| `RLP_SKIP_CREDENTIAL_PREFLIGHT` | skip the planner's per-arm credential check |
| `RLP_ORCH_FORCE=1` | let `install.sh` overwrite an existing ladder |
| `SSL_CERT_FILE` | CA bundle for the HF download behind a MITM proxy |

### Exit codes

`0` a valid result · `1` an `ok:false` envelope · `2` a usage error · `3`
`doctor` found a failure. `rlp <engine subcommand> --json` prints the raw
envelope and nothing else, so scripting never has to parse a pretty printer.
