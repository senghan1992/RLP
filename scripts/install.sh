#!/bin/sh
# RLP install — one script, from clone to `rlp -p "..."`.
#
#   sh scripts/install.sh
#
# What it does:
#   1. clones + builds the pi fork that RLP runs on (the harness `rpi`), applying
#      the RLP patch set
#   2. creates rlp-svc/.venv and installs the decision engine + laya checkpoint
#   3. installs RLP's dropped-in slash extensions, skills and the ladder
#
# Requirements: git, node >= 18 and npm, python >= 3.10.
#   `uv` is used when present; otherwise a stdlib venv + pip.
#
# Env:
#   RLP_PI_REPO   git URL of the pi fork source   (default: upstream pi)
#   RLP_PI_REF    branch/tag to check out         (default: upstream default)
#   RLP_REBUILD=1 force a rebuild of the fork
#   RLP_ORCH_FORCE=1 overwrite an existing orchestration ladder
#
# Idempotent: re-running skips completed steps and is safe after an update.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
RPI_BIN="$ROOT/scripts/rpi-bin"
RLP_BIN="$ROOT/scripts/rlp"
FORK="$ROOT/fork/pi"
PATCH="$ROOT/scripts/rlp-fork.patch"
VENV="$ROOT/rlp-svc/.venv"
PY="$VENV/bin/python"

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
PI_REPO="${RLP_PI_REPO:-https://github.com/earendil-works/pi}"
if [ ! -d "$FORK" ]; then
  echo "[rlp] cloning pi fork from $PI_REPO ..."
  if [ -n "${RLP_PI_REF:-}" ]; then
    git clone --depth 1 --branch "$RLP_PI_REF" "$PI_REPO" "$FORK"
  else
    git clone --depth 1 "$PI_REPO" "$FORK"
    git -C "$FORK" checkout -b rlp
  fi
fi

# Apply the RLP patch set only when it is not already present. Marker:
# RPI_DEFAULT_MODEL lives only in the RLP settings-manager patch, so a fork that
# already carries RLP (a published rlp branch) skips this entirely.
NEEDS_BUILD=""
if ! grep -q "RPI_DEFAULT_MODEL" "$FORK/packages/coding-agent/src/core/settings-manager.ts" 2>/dev/null; then
  echo "[rlp] applying fork patch set..."
  git -C "$FORK" apply --3way "$PATCH" || {
    echo "[rlp] patch did not apply cleanly against $PI_REPO." >&2
    echo "      If upstream moved, set RLP_PI_REPO/RLP_PI_REF to a fork that carries the rlp branch." >&2
    echo "      Or resolve the conflicts in $FORK and re-run." >&2
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
cd "$ROOT/rlp-svc"
if [ ! -d "$VENV" ]; then
  if have uv; then
    uv venv --python 3.12 .venv
  else
    "${PYTHON:-python3}" -m venv .venv
  fi
fi
# mcp is pinned <2: `rlp serve` builds on FastMCP's 1.x API surface.
TORCH_INDEX="https://download.pytorch.org/whl/cpu"   # CPU-only torch; the default index pulls multi-GB CUDA
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
echo "[rlp] expected to FAIL; /setup is what clears them."
echo ""
(cd "$ROOT/rlp-svc" && "$PY" -m rlp_svc doctor) || true

cat <<EOF

[rlp] installed. One step left — connect a provider:

  cd <any project> && rlp      # start the agent
  /setup                       # guided: providers -> brain -> worker arms -> roles

  \`/setup\` is what turns the ladder from policy into something dispatchable:
  it reads your endpoint's own model list and writes the arms. Until then RLP
  runs every request inline, and \`rlp doctor\` says exactly what is missing.

Then:
  rlp -p "add a --wc flag, test it, document it"   # one shot, same agent
  rlp plan "<request>"                             # plan only, nothing executed
  rlp doctor --warm                                # health, incl. a real laya pass
  sh $ROOT/scripts/selftest.sh --fast              # the offline suite
EOF