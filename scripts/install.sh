#!/bin/sh
# RLP install — one script, from clone to `rlp -p "..."`.
#
#   sh scripts/install.sh
#
# What it does:
#   1. clones + builds the pi fork that RLP runs on (the harness `rpi`), applying
#      the RLP patch set
#   2. creates rlp-svc/.venv and installs the decision engine + laya checkpoint
#   3. installs RLP's dropped-in slash extensions and the orchestration ladder
#   4. OPTIONALLY wires the omnigent plane, only if `omni` is on PATH
#
# Requirements: git, node >= 18 and npm, python >= 3.10.
#   `uv` is used when present; otherwise a stdlib venv + pip.
#   `omni` (omnigent) is OPTIONAL — local orchestration (`rlp`, `rlp -p`) does
#   not need it. It is only used by `rlp --omnigent`.
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
RLP_LAUNCH="$ROOT/scripts/rlp-launch"
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
RPI_DEFAULT_MODEL="${RPI_DEFAULT_MODEL:-agnes/agnes-3.0-flash}"
export RPI_DEFAULT_MODEL
exec node __RLP_ROOT__/fork/pi/packages/coding-agent/dist/bundle/cli.js "$@"
WRAPPER
  sed -i "s|__RLP_ROOT__|$ROOT|g" "$RPI_BIN"
fi
chmod +x "$RPI_BIN" "$RLP_BIN" "$RLP_LAUNCH"

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
# mcp is pinned <2: omnigent's MCP client is 1.x and rejects a 2.x server.
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

# --- 3. Harness slash extensions + ladder (the local plane needs these) -------
# The /rlp* commands are dropped-in pi extensions, not a fork patch, so they
# install and update without a rebuild. The directory is shared with unrelated,
# optional extensions (myviking, …): RLP records exactly which files it owns so
# /commands and `rlp doctor` can label the rest optional instead of implying a
# dependency.
PI_AGENT_DIR="${RPI_CODING_AGENT_DIR:-$HOME/.pi/agent}"
mkdir -p "$PI_AGENT_DIR/extensions"
RLP_EXT_LIST=""
for ext in "$ROOT"/agent/rlp/extensions/*.ts; do
  [ -e "$ext" ] || continue
  name=$(basename "$ext")
  cp "$ext" "$PI_AGENT_DIR/extensions/"
  echo "[rlp] installed extension $name"
  RLP_EXT_LIST="${RLP_EXT_LIST}${RLP_EXT_LIST:+, }\"$name\""
done
printf '{\n  "root": "%s",\n  "extensions": [%s]\n}\n' "$ROOT" "$RLP_EXT_LIST" > "$PI_AGENT_DIR/rlp-location.json"

if [ -f "$PI_AGENT_DIR/orchestration.json" ] && [ -z "${RLP_ORCH_FORCE:-}" ]; then
  echo "[rlp] kept existing $PI_AGENT_DIR/orchestration.json (RLP_ORCH_FORCE=1 to overwrite)"
else
  cp "$ROOT/agent/rlp/orchestration.json" "$PI_AGENT_DIR/orchestration.json"
  echo "[rlp] installed orchestration ladder to $PI_AGENT_DIR/orchestration.json"
fi

# --- 4. Omnigent plane (optional) --------------------------------------------
if have omni; then
  mkdir -p "$HOME/.omnigent/agents"
  rm -rf "$HOME/.omnigent/agents/rlp"
  cp -r "$ROOT/agent/rlp" "$HOME/.omnigent/agents/rlp"
  # Fill the spec's placeholders with this checkout's real paths, so the public
  # repo carries no host-specific absolute paths.
  sed -i "s|__RLP_PYTHON__|$PY|g; s|__RLP_PI_AUTH__|$HOME/.pi/agent/auth.json|g; s|__RLP_PI_MODELS__|$HOME/.pi/agent/models.json|g" \
    "$HOME/.omnigent/agents/rlp/config.yaml"
  CFG="$HOME/.omnigent/config.yaml"
  if [ -f "$CFG" ] && grep -q "harness:" "$CFG" 2>/dev/null; then
    if grep -q "local/bin/rlp" "$CFG"; then
      cp "$CFG" "$CFG.bak.$(date +%s)"
      sed -i 's|local/bin/rlp$|local/bin/rpi|; s|local/bin/rlp *$|local/bin/rpi|' "$CFG"
      echo "[rlp] repointed harness.pi.command -> $HOME/.local/bin/rpi"
    fi
  else
    mkdir -p "$(dirname "$CFG")"
    cp "$CFG" "$CFG.bak.$(date +%s)" 2>/dev/null || true
    printf '\nharness:\n  pi:\n    command: %s\n' "$HOME/.local/bin/rpi" >> "$CFG"
    echo "[rlp] wired harness.pi.command -> $HOME/.local/bin/rpi"
  fi
  omni stop 2>/dev/null || true
  omni start 2>/dev/null || echo "[rlp] 'omni start' failed — run it later if you want the web UI"
  echo "[rlp] omnigent plane installed (rlp --omnigent)"
else
  echo "[rlp] omnigent not found — skipping the optional --omnigent plane."
  echo "      Local orchestration needs nothing here: rlp, rlp -p, rlp plan all work."
fi

# --- 5. Verify, do not assume ------------------------------------------------
echo ""
echo "[rlp] doctor:"
(cd "$ROOT/rlp-svc" && "$PY" -m rlp_svc doctor) || echo "[rlp] doctor reported failures — see FAIL lines above"

cat <<EOF

[rlp] done.
  use:      cd <any project> && rlp            # the agent (triage -> local workers)
  one-shot: rlp -p "add a --wc flag, test it, document it"
  decide:   rlp plan "<request>"               # plan only, nothing executed
  health:   rlp doctor --warm
  verify:   sh $ROOT/scripts/selftest.sh --fast
EOF