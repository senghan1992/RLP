#!/bin/sh
# RLP selftest, cheap first.
#
#   extension typecheck — the dropped-in /rlp* extensions against the harness's
#                        real API types. A member that does not exist on
#                        ExtensionContext or ExtensionAPI (ctx.setModel,
#                        ctx.modelRegistry, ...) is a crash inside the TUI, so
#                        it has to fail here.
#   offline suite       — planner shape, ladder validation, CLI surface, doctor
#                         (stubbed models, milliseconds)
#   integration         — the real laya load, real decomposition, real routing
#                         (~4 min on CPU; pays the checkpoint load once)
#
# Pass --fast to stop after the offline suite.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
PY="$ROOT/rlp-svc/.venv/bin/python"

if [ ! -x "$PY" ]; then
  echo "selftest: no venv at $PY — run sh $ROOT/scripts/install.sh" >&2
  exit 1
fi

# --- 1. extension typecheck (skipped when the fork has not been built) ---
# Which typechecker is spelled depends on the fork's version: pi 0.87.0 used
# the standalone `tsgo` binary, 0.87.1 replaced it with typescript 7's `tsc`.
# Resolving from the fork's own node_modules means an upstream rename cannot
# turn this check into a silent npx download failure.
TSC=""
if [ -f "$ROOT/fork/pi/packages/coding-agent/dist/index.d.ts" ]; then
  if [ -x "$ROOT/fork/pi/node_modules/.bin/tsc" ]; then
    TSC="$ROOT/fork/pi/node_modules/.bin/tsc"
  elif [ -x "$ROOT/fork/pi/node_modules/.bin/tsgo" ]; then
    TSC="$ROOT/fork/pi/node_modules/.bin/tsgo"
  fi
fi
if [ -n "$TSC" ]; then
  echo "== extension typecheck =="
  "$TSC" --noEmit -p "$ROOT/agent/rlp/extensions/tsconfig.json" || {
    echo "selftest: extension typecheck FAILED" >&2
    exit 1
  }
  echo "ok ($(basename "$TSC"))"
  echo
fi

# --- 2. offline suite ---
echo "== offline suite =="
(cd "$ROOT/rlp-svc" && "$PY" -m rlp_svc.tests)

if [ "${1:-}" = "--fast" ]; then
  echo "== integration skipped (--fast) =="
  exit 0
fi

# --- 3. integration suite ---
echo
echo "== integration suite (real models, slow) =="
(cd "$ROOT/rlp-svc" && "$PY" -m rlp_svc.selftest)

# --- 4. harness contract ---
# Only reachable from the running harness: two extensions registering one
# command name type-checks fine and then shows up in the menu as /name:1 and
# /name:2, where neither invocation looks like what you typed. Then the same
# live session is used to drive /provider and /setup as a person would, which is
# the only way to test a wizard: stubbing the conversation tests the stub.
if [ "${1:-}" = "--fast" ] || [ ! -x "$ROOT/scripts/rlp" ]; then
  exit 0
fi
echo
echo "== harness contract =="
node "$ROOT/scripts/check-harness.mjs" "$ROOT/scripts/rlp" || {
  echo "selftest: harness contract FAILED" >&2
  exit 1
}
echo
echo "== provider / setup (live session) =="
node "$ROOT/scripts/check-provider.mjs" "$ROOT/scripts/rlp" || {
  echo "selftest: provider check FAILED" >&2
  exit 1
}
