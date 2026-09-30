#!/bin/sh
# RLP install — one script, from clone to `rlp -p "..."`.
#
#   sh scripts/install.sh
#
# What it does:
#   1. clones + builds the pi fork that RLP runs on (the harness `rpi`), applying
#      the RLP patch set
#   2. installs the decision engine into a python of your choosing (no venv by
#      default) + downloads the laya checkpoint
#   3. installs RLP's dropped-in slash extensions, skills and the ladder
#
# Requirements: git, node >= 18 and npm, python >= 3.10 with pip.
#   `uv` is used when present; otherwise plain pip.
#
# Env:
#   RLP_ENGINE      where the engine's python lives:
#                 system (default) — no venv, pip into a PATH python, like any
#                                    other package
#                 venv           — self-contained rlp-svc/.venv, isolated from
#                                    the system python (the old default)
#   RLP_PI_REPO   git URL of the pi fork source   (default: upstream pi)
#   RLP_PI_REF    branch/tag to check out         (default: the pinned commit in
#                 scripts/rlp-fork.base — an install is reproducible, and moving
#                 upstream is `rlp update`'s verified job, not a side effect)
#   RLP_REBUILD=1 force a rebuild of the fork
#   RLP_ORCH_FORCE=1 overwrite an existing orchestration ladder
#   RLP_SKIP_MODELS=1 install without torch/laya/rlm and without the checkpoint
#                 (everything that does not run a model still works; this is
#                  what CI installs so it can drive the real harness)
#
# Idempotent: re-running skips completed steps and is safe after an update.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
RPI_BIN="$ROOT/scripts/rpi-bin"
RLP_BIN="$ROOT/scripts/rlp"
FORK="$ROOT/fork/pi"
PATCH="$ROOT/scripts/rlp-fork.patch"

need() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "[rlp] missing required tool: $1" >&2
    echo "      install it, then re-run: sh $ROOT/scripts/install.sh" >&2
    exit 1
  }
}

have() { command -v "$1" >/dev/null 2>&1; }

need git
need node
need npm
have python3 || have python || {
  echo "[rlp] missing required tool: python3 (>= 3.10)" >&2
  exit 1
}

# --- 1. pi fork (the rpi harness) --------------------------------------------
#
# The upstream commit is PINNED, to the sha in scripts/rlp-fork.base. Cloning
# upstream's default branch instead made every install a bet on whether upstream
# had moved the lines the patch touches, and that bet was already lost: a fresh
# `curl | sh` failed for everyone at "patch did not apply cleanly", because
# upstream had moved on *and* a `--depth 1` clone has none of the blobs
# `git apply --3way` needs to recover. Pinning removes both halves of that.
#
# Moving to a newer upstream is `rlp update`'s job, which merges, re-checks that
# every RLP marker survived, rebuilds, and rolls back cleanly. `RLP_PI_REF`
# still overrides the pin for anyone who wants to try a newer upstream by hand.
PI_REPO="${RLP_PI_REPO:-https://github.com/earendil-works/pi}"
PI_BASE=$(grep -v '^#' "$ROOT/scripts/rlp-fork.base" 2>/dev/null | tr -d '[:space:]' || true)
if [ ! -d "$FORK" ]; then
  if [ -n "${RLP_PI_REF:-}" ]; then
    echo "[rlp] cloning pi from $PI_REPO at $RLP_PI_REF (RLP_PI_REF overrides the pin) ..."
    git clone --depth 1 --branch "$RLP_PI_REF" "$PI_REPO" "$FORK"
    git -C "$FORK" checkout -q -B rlp
  elif [ -n "$PI_BASE" ]; then
    echo "[rlp] cloning pi from $PI_REPO at the pinned base $(printf '%.10s' "$PI_BASE") ..."
    mkdir -p "$FORK"
    git -C "$FORK" init -q
    git -C "$FORK" remote add origin "$PI_REPO"
    # Fetch the exact commit: one commit, no history, and the blobs the patch
    # needs are exactly the ones it came from. A server that refuses a
    # by-sha fetch is handled below rather than left as a bare git error.
    if git -C "$FORK" fetch -q --depth 1 origin "$PI_BASE" 2>/dev/null; then
      git -C "$FORK" checkout -q -B rlp FETCH_HEAD
      # `rlp update` merges `origin/main`, so that ref has to exist.
      git -C "$FORK" fetch -q --depth 1 origin HEAD:refs/remotes/origin/main 2>/dev/null || true
    else
      echo "[rlp] $PI_REPO would not serve the pinned commit $PI_BASE directly;" >&2
      echo "      falling back to a full clone so it can be checked out." >&2
      rm -rf "$FORK"
      git clone -q "$PI_REPO" "$FORK" || {
        echo "[rlp] could not clone $PI_REPO (no network?)" >&2
        exit 1
      }
      git -C "$FORK" checkout -q -B rlp "$PI_BASE" || {
        echo "[rlp] $PI_REPO does not contain the pinned commit $PI_BASE." >&2
        echo "      Set RLP_PI_REF to a ref it does have, or update scripts/rlp-fork.base." >&2
        exit 1
      }
    fi
  else
    echo "[rlp] no pinned base recorded — cloning $PI_REPO's default branch" >&2
    git clone --depth 1 "$PI_REPO" "$FORK"
    git -C "$FORK" checkout -q -B rlp
  fi
fi

# Apply the RLP patch set only when it is not already present. Marker:
# RPI_DEFAULT_MODEL lives only in the RLP settings-manager patch, so a fork that
# already carries RLP (a published rlp branch) skips this entirely.
NEEDS_BUILD=""
if ! grep -q "RPI_DEFAULT_MODEL" "$FORK/packages/coding-agent/src/core/settings-manager.ts" 2>/dev/null; then
  echo "[rlp] applying fork patch set..."
  git -C "$FORK" apply --3way "$PATCH" || {
    echo "[rlp] the fork patch did not apply." >&2
    echo "      Expected upstream at $PI_BASE (scripts/rlp-fork.base); the fork is at" >&2
    echo "      $(git -C "$FORK" rev-parse HEAD 2>/dev/null || echo '?')." >&2
    if [ -n "${RLP_PI_REF:-}" ]; then
      echo "      RLP_PI_REF=$RLP_PI_REF overrode the pin — that is the likely cause." >&2
      echo "      Unset it to install the combination this RLP was built and tested against." >&2
    else
      echo "      Delete $FORK and re-run to fetch the pinned base cleanly." >&2
    fi
    exit 1
  }
  # Commit it. `git apply` leaves the patch in the working tree, which left every
  # installed host with a permanently dirty fork — and `rlp update` refuses to
  # start from a dirty tree, so the update path was unreachable on every machine
  # but a developer's. As one commit on the `rlp` branch, the tree is clean, the
  # upstream merge `rlp update` performs is a real merge, and a rollback is one
  # `git reset --hard` away. The identity is fixed so the commit says what it is
  # rather than inheriting whoever happened to run the installer.
  git -C "$FORK" add -A
  git -C "$FORK" -c user.name="RLP installer" -c user.email="rlp@localhost" \
    commit -q -m "RLP fork patch

Applied by scripts/install.sh from scripts/rlp-fork.patch.
Regenerate the patch with:
  git -C fork/pi diff \$(git -C fork/pi merge-base origin/main HEAD) HEAD" || {
    echo "[rlp] could not commit the fork patch — 'rlp update' will refuse to run." >&2
    exit 1
  }
  NEEDS_BUILD=1
fi

if [ ! -x "$RPI_BIN" ]; then
  NEEDS_BUILD=1
fi

if [ -n "$NEEDS_BUILD" ] || [ -n "${RLP_REBUILD:-}" ]; then
  echo "[rlp] building pi fork (npm install + build)..."
  (cd "$FORK" && npm install --ignore-scripts && npm run build)
fi

# The rpi wrapper lives in the repo and finds the fork through its own
# symlink-chased path, so a relocated clone keeps working. Recreated only if it
# went missing (e.g. a tarball export lost the exec bit).
if [ ! -f "$RPI_BIN" ]; then
  cat > "$RPI_BIN" <<'WRAPPER'
#!/bin/sh
# Regenerated fallback: scripts/rpi-bin is the real one (it also derives the
# session default from the ladder's brain). This copy only exists so a tarball
# export that lost the exec bit still boots.
RPI_CODING_AGENT_DIR="${RPI_CODING_AGENT_DIR:-${RLP_CODING_AGENT_DIR:-$HOME/.rlp/agent}}"
export RPI_CODING_AGENT_DIR
exec node __RLP_ROOT__/fork/pi/packages/coding-agent/dist/bundle/cli.js "$@"
WRAPPER
  sed -i "s|__RLP_ROOT__|$ROOT|g" "$RPI_BIN"
fi
chmod +x "$RPI_BIN" "$RLP_BIN"

mkdir -p "$HOME/.local/bin"
ln -sf "$RPI_BIN" "$HOME/.local/bin/rpi"
ln -sf "$RLP_BIN" "$HOME/.local/bin/rlp"
echo "[rlp] installed: $("$RPI_BIN" --version 2>/dev/null || echo 'rpi built')"

# --- 2. rlp-svc (the decision engine) ----------------------------------------
# Where the engine's python lives is the installer's choice, and the user's:
#   RLP_ENGINE=system (default)  no venv — `pip install -e .` into a PATH
#                                python, exactly like any other package
#   RLP_ENGINE=venv             self-contained: create rlp-svc/.venv and install
#                                there (isolated, does not touch the system
#                                python; the old default, still fully supported)
# Both are honored at runtime by scripts/svc-py, so an install and a run never
# diverge.
cd "$ROOT/rlp-svc"
ENGINE="${RLP_ENGINE:-system}"
case "$ENGINE" in
  venv)
    if [ ! -d .venv ]; then
      echo "[rlp] RLP_ENGINE=venv — creating rlp-svc/.venv (isolated install)"
      if have uv; then
        uv venv --python 3.12 .venv
      else
        "${PYTHON:-python3}" -m venv .venv
      fi
    fi
    PY="$ROOT/rlp-svc/.venv/bin/python"
    echo "[rlp] installing the engine into .venv (isolated from the system python)"
    ;;
  system)
    PY="$(sh "$ROOT/scripts/svc-py" 2>/dev/null || true)"
    if [ -z "$PY" ] || [ ! -x "$PY" ]; then
      PY="${PYTHON:-python3}"
    fi
    echo "[rlp] installing the engine into $PY (system/user python, no venv)"
    ;;
  *)
    echo "[rlp] RLP_ENGINE must be 'system' (default) or 'venv', got: $ENGINE" >&2
    exit 2
    ;;
esac
# mcp is pinned <2: `rlp serve` builds on FastMCP's 1.x API surface.
TORCH_INDEX="https://download.pytorch.org/whl/cpu"   # CPU-only torch; the default index pulls multi-GB CUDA
#
# PY is the interpreter we install into. It is the same interpreter the
# launcher scripts will use later, so an install and a run can never diverge.
# RLP_SKIP_MODELS=1 installs everything except the model stack: no torch, no
# laya, no rlm, no 400 MB checkpoint. What still works is every part that does
# not run a model — the harness, the extensions, `rlp provider`, `rlp ladder`,
# `rlp doctor`, the offline suite and the live-session checks — which is exactly
# the subset CI can afford to exercise on every push, and is why the TUI surface
# is now testable there at all. `rlp doctor` reports the missing pieces as the
# failures they are, so nobody mistakes this for a complete install.
if [ -n "${RLP_SKIP_MODELS:-}" ]; then
  echo "[rlp] RLP_SKIP_MODELS=1 — installing the engine without torch/laya/rlm"
  echo "[rlp]   triage, routing and decomposition will NOT run; everything else will"
  if have uv; then
    uv pip install --python "$PY" "mcp>=1,<2" httpx
    uv pip install --python "$PY" --no-deps -e .
  else
    "$PY" -m pip install -q --upgrade pip
    "$PY" -m pip install -q "mcp>=1,<2" httpx
    "$PY" -m pip install -q --no-deps -e .
  fi
else
  if have uv; then
    uv pip install --python "$PY" torch --index-url "$TORCH_INDEX"
    uv pip install --python "$PY" "mcp>=1,<2" rlms laya httpx
    uv pip install --python "$PY" -e .
  else
    "$PY" -m pip install --upgrade pip
    "$PY" -m pip install torch --index-url "$TORCH_INDEX"
    "$PY" -m pip install "mcp>=1,<2" rlms laya httpx
    "$PY" -m pip install -e .
  fi
  # laya checkpoints (CPU, ~400 MB) — cached after the first run.
  "$PY" -c "
import os
os.environ.setdefault('SSL_CERT_FILE', '/etc/ssl/certs/ca-certificates.crt')
from huggingface_hub import snapshot_download
snapshot_download('convaiinnovations/laya', allow_patterns=['rl_agent_config.json','model.safetensors','tokenizer/*','encoder/*'])
" 2>/dev/null || echo "[rlp] laya checkpoint download skipped (offline, or already cached)"
fi

# --- 3. Harness extensions + skills + ladder, in RLP's own agent dir --------
# One implementation, shared with `rlp update`: an update that moves the engine
# forward while leaving yesterday's extensions in place is a half-applied state
# that nothing would report, so both paths run the same script.
sh "$ROOT/scripts/sync-agent-dir" "$ROOT"

# --- 4. Verify, do not assume ------------------------------------------------
# A fresh install has no provider and no model arms, so the doctor *will* report
# failures here and that is the correct answer, not a broken install. Saying so
# before the report is the difference between "next step" and "it crashed".
echo ""
echo "[rlp] doctor — on a first install the provider and ladder-arm lines are"
echo "[rlp] expected to FAIL. Starting \`rlp\` asks for what they need."
echo ""
(cd "$ROOT/rlp-svc" && "$PY" -m rlp_svc doctor) || true

cat <<EOF

[rlp] installed. One step left — start it, and it asks:

  cd <any project> && rlp

The first run on a host with no credential and no model arms begins the guided
setup by itself: the mode, then the endpoints and their keys, then the model RLP
works on, then the worker arms, then the model for each role. Every question is
skippable, and cancelling all of them writes nothing. \`/setup\` reruns it by
hand, and \`rlp doctor\` says the same thing with no terminal in the way.

Want RLP without the fan-out? Say so at the first question ("Direct only"), or
any time afterwards: \`/direct on\`, \`rlp mode direct\`, or \`rlp --direct\`
for one session. Nothing is orchestrated, and no decision model is loaded.

  RLP_NO_SETUP=1 rlp                               # never ask (scripted sessions)
  rlp -p "add a --wc flag, test it, document it"   # one shot, same agent
  rlp plan "<request>"                             # plan only, nothing executed
  rlp mode                                         # does this host orchestrate?
  rlp doctor --warm                                # health, incl. a real laya pass
  sh $ROOT/scripts/selftest.sh --fast              # the offline suite
EOF