---
name: rlp-doctor
description: Diagnose whether this host can actually run RLP — dependencies, credentials, ladder, laya checkpoint, and one fix per failure
---

# rlp-doctor

Run the health check and report it. It is read-only, takes about 0.2 s, and is
the fastest way to explain a failure that looks like RLP being broken.

```bash
rlp doctor
```

Add `--warm` only when a laya round trip is genuinely needed — it pays the
checkpoint load (~170 s cold on CPU) and says so. Do not use it to "check
dependencies"; plain `rlp doctor` already covers that without the wait.

If the shell is unavailable, use `sys_os_shell` with the same command.

## What it reports

Graded, one line per check, with the fix on the failure line:

- `ok` — good
- `warn` — degraded but the run proceeds
- `fail` — that capability will not work

Checks: Python and the `laya` / `rlm` / `mcp` / `httpx` imports; the
credential files and the decomposer model; the orchestration ladder, its
validation, the derived router roster and which workers are excluded as
unavailable; whether a single-vendor ladder is claiming cross-vendor review;
the laya checkpoint cache; the CA bundle; and the omnigent wiring (the `omni`
binary, the installed agent spec, the harness override).

Exit code 0 when runnable, 3 when a `fail` is present.

## How to report it

Lead with the verdict, then only the `fail` and `warn` lines, then the fix
each one names. Do not paste all 20 `ok` lines at the user. If a check fails,
say what is broken and what to run — do not attempt to repair the
configuration yourself unless asked.
