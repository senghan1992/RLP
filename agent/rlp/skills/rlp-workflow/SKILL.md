---
name: rlp-workflow
description: RLP orchestration pipeline — triage, then decompose, route, dispatch, collect, synthesize
---

# RLP workflow

Triage gate first; the five-stage pipeline only for what earns it. Act in the
same turn you announce.

## Gate: TRIAGE

Call `rlp_triage(request, context)` before any planning.

- `mode: direct` → do the work yourself inline (sys_os_shell/read/write/edit),
  run a check, report. No DAG, no routing, no dispatch, no worktrees, no gate
  table. One line: "triage: direct (<confidence>) — handling inline". If a
  second independent deliverable appears mid-flight, upgrade to orchestrate.
- `mode: orchestrate` → run the pipeline below.
- `escalate: true` → default to direct; upgrade only if you can already name
  ≥2 independent deliverables.

## Pipeline stages

1. **DECOMPOSE** — call `rlm_decompose(request, context)`. Work the returned
   DAG. `ok:false` or a 1-task DAG for a multi-part request → retry once
   with sharper text; twice → manual DAG, note it in the gate message.

2. **ROUTE** — for every node call `laya_route(title, brief, domain)` with
   the roster omitted: it is derived from the installed orchestration ladder,
   the same source as your `<rlp_orchestration>` section. `rlp_orchestration`
   returns the ladder as JSON on demand. `escalate:true` or `engine:llm` →
   advisory; re-justify in one line.

3. **DISPATCH** — waves by `depends_on`. All ready nodes in one turn via
   `sys_session_send` with task-shaped titles, purpose, model (first arm of
   the ladder that fits the node's roles — default arm = bulk of work), and
   input containing the node brief, acceptance sentence, full text of each
   dependency's result, and the worktree path. Coding nodes get their own
   `sys_os_shell` worktree first.

4. **COLLECT** — inbox auto-wake only. Claude failure of any kind →
   `CLAUDE_EXHAUSTED`, re-dispatch to pi same turn. Missed acceptance →
   re-dispatch once on another ladder arm; second failure → re-decompose the
   node alone (depth 2 max) or escalate.

5. **SYNTHESIZE** — one final report from node results: what, where
   (branch/PR/files), what was verified and how, what is left. Never merge.

## Invariants

- The model ladder lives in `orchestration.json`, never in prose: read
  `<rlp_orchestration>` / `rlp_orchestration` and route from it. Arms are
  priority-ordered; the first arm carries the bulk.
- Cross-vendor: review runs on a different provider family than implement
  (agnes / qwen-token-plan / anthropic).
- Gate before first dispatch: DAG table + routing confidences, same turn.
- A turn that ends after only announcing intent is a bug.
