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
#   harness contract    — every command loads, exactly once, in a real session
#   provider / setup    — the wizards, driven through a real session over RPC
#   drivers             — fake CLIs dispatched, watched and killed for real
#   first-run report    — every non-ok doctor line names an actionable fix
#   first-run asks      — a real TUI starts the setup by itself, escape writes
#                        nothing, and RLP_NO_SETUP is honoured
#
# Pass --fast to stop after the offline suite.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
PY="$(sh "$ROOT/scripts/svc-py" 2>/dev/null || true)"
if [ -z "$PY" ] || [ ! -x "$PY" ]; then
  echo "selftest: decision engine not installed (no python with rlp_svc on PATH) — run sh $ROOT/scripts/install.sh" >&2
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

# --- 2. the launcher routes every engine subcommand ---
# `rlp <name>` dispatches on a hardcoded list in `scripts/rlp`, and a name that
# is missing from it is not an error — it is handed to the harness as an
# argument, so `rlp progress` silently became "start a session and ignore the
# word progress". One list, checked against the parser that owns it.
CASE_LINE=$(grep -m1 -E '^  [a-z|-]+\)$' "$ROOT/scripts/rlp" || true)
if [ -n "$CASE_LINE" ]; then
  ROUTES=$("$PY" -c '
from rlp_svc.cli import build_parser

p = build_parser()
for a in p._actions:
    if hasattr(a, "choices") and isinstance(a.choices, dict):
        print(" ".join(sorted(a.choices)))
        break
')
  ROUTED=$(printf '%s' "${CASE_LINE%)}" | tr -d ' \t')
  unrouted=""
  for name in $ROUTES; do
    case "|$ROUTED|" in
      *"|$name|"*) ;;
      *) unrouted="$unrouted $name" ;;
    esac
  done
  if [ -n "$unrouted" ]; then
    echo "selftest: engine subcommands not routed by scripts/rlp:$unrouted" >&2
    echo "          add them to the dispatch case in scripts/rlp" >&2
    exit 1
  fi
  echo "== launcher routing ok: every engine subcommand is dispatched =="
else
  echo "selftest: could not find the dispatch case in scripts/rlp" >&2
  exit 1
fi

# --- 3. offline suite ---
echo "== offline suite =="
(cd "$ROOT/rlp-svc" && "$PY" -m rlp_svc.tests)

if [ "${1:-}" = "--fast" ]; then
  echo "== integration skipped (--fast) =="
  exit 0
fi

# --- 4. integration suite ---
echo
echo "== integration suite (real models, slow) =="
(cd "$ROOT/rlp-svc" && "$PY" -m rlp_svc.selftest)

# --- 5. harness contract ---
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

# --- drivers: fake CLIs, real dispatch ------------------------------------------
# The external-harness lane spawns, watches and collects things this repo does
# not own. The check proves the mechanism with fakes on a sandbox path (never a
# real tool), and skips the tmux lens on hosts without tmux — both lanes end in
# the same collected contract either way.
echo
echo "== drivers (fake CLIs, real dispatch/collect/kill) =="
node "$ROOT/scripts/check-drivers.mjs" || {
  echo "selftest: driver check FAILED" >&2
  exit 1
}

# --- 6. the first-run report ---
# Cheap, and it guards the property users judge the tool by: every non-ok
# doctor line names a fix this host can act on.
echo
echo "== first-run asks (real TUI in a pty) =="
sh "$ROOT/scripts/check-first-ask" "$ROOT/scripts/rlp" || {
  echo "selftest: first-run asks FAILED" >&2
  exit 1
}

echo
echo "== first-run report =="
RLP_FIRST_RUN_EXPECT_MODELS=1 sh "$ROOT/scripts/check-first-run" "$ROOT/scripts/rlp" || {
  echo "selftest: first-run check FAILED" >&2
  exit 1
}
