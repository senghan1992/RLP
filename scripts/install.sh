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
# The /rlp* commands are dropped-in pi extensions, not a fork patch, so they
# install and update without a rebuild. Everything goes to RLP's own agent dir
# (`~/.rlp/agent`), never pi's `~/.pi`: RLP is its own tool, and the fork's
# `piConfig.configDir` points here too, so the two agree by construction.
# RLP records exactly which files it owns so /commands and `rlp doctor` can
# label the rest optional instead of implying a dependency.
RLP_AGENT_DIR="${RLP_CODING_AGENT_DIR:-${RPI_CODING_AGENT_DIR:-$HOME/.rlp/agent}}"
mkdir -p "$RLP_AGENT_DIR"

# Upgrade path for an install made before RLP had its own directory: copy the
# credentials pi and RLP used to share, so an existing user is not asked to
# reconnect every provider. Copy, never move — the harness keeps working, and
# the user can delete the copies whenever they like. RLP_NO_MIGRATE=1 skips it.
if [ -z "${RLP_NO_MIGRATE:-}" ] && [ ! -f "$RLP_AGENT_DIR/auth.json" ] && [ -f "$HOME/.pi/agent/auth.json" ]; then
  echo "[rlp] migrating credentials from $HOME/.pi/agent (copy, not move)"
  for f in auth.json models.json; do
    if [ -f "$HOME/.pi/agent/$f" ]; then
      cp "$HOME/.pi/agent/$f" "$RLP_AGENT_DIR/$f"
      chmod 600 "$RLP_AGENT_DIR/$f" 2>/dev/null || true
      echo "[rlp]   $f -> $RLP_AGENT_DIR/$f"
    fi
  done
  echo "[rlp]   (RLP_NO_MIGRATE=1 skips this; /provider connects one from scratch)"
fi

mkdir -p "$RLP_AGENT_DIR/extensions"
RLP_EXT_LIST=""
for ext in "$ROOT"/agent/rlp/extensions/*.ts; do
  [ -e "$ext" ] || continue
  name=$(basename "$ext")
  cp "$ext" "$RLP_AGENT_DIR/extensions/"
  echo "[rlp] installed extension $name"
  RLP_EXT_LIST="${RLP_EXT_LIST}${RLP_EXT_LIST:+, }\"$name\""
done
# Skills are the harness's `/skill:<name>` commands. They are part of the tool's
# surface — the local plane never had them installed, so /commands advertised
# whatever happened to be in the shared skills dir and RLP's own were missing.
RLP_SKILL_LIST=""
mkdir -p "$RLP_AGENT_DIR/skills"
for skill in "$ROOT"/agent/rlp/skills/*/SKILL.md; do
  [ -e "$skill" ] || continue
  name=$(basename "$(dirname "$skill")")
  mkdir -p "$RLP_AGENT_DIR/skills/$name"
  cp "$skill" "$RLP_AGENT_DIR/skills/$name/SKILL.md"
  echo "[rlp] installed skill $name"
  RLP_SKILL_LIST="${RLP_SKILL_LIST}${RLP_SKILL_LIST:+, }\"$name\""
done
printf '{\n  "root": "%s",\n  "agentDir": "%s",\n  "extensions": [%s],\n  "skills": [%s]\n}\n' \
  "$ROOT" "$RLP_AGENT_DIR" "$RLP_EXT_LIST" "$RLP_SKILL_LIST" > "$RLP_AGENT_DIR/rlp-location.json"

# The ladder ships with RLP's orchestration *policy* (gate, waves, dispatch cap,
# cross-vendor review, RLM and planning budgets) and no model arms: which models
# orchestrate depends on which providers you connect, and RLP will not guess. A
# ladder with no arms is a documented state — `rlp doctor` names it and `/setup`
# fills it from your real endpoints.
if [ -f "$RLP_AGENT_DIR/orchestration.json" ] && [ -z "${RLP_ORCH_FORCE:-}" ]; then
  echo "[rlp] kept existing $RLP_AGENT_DIR/orchestration.json (RLP_ORCH_FORCE=1 to overwrite)"
else
  cp "$ROOT/agent/rlp/orchestration.json" "$RLP_AGENT_DIR/orchestration.json"
  echo "[rlp] installed the orchestration ladder to $RLP_AGENT_DIR/orchestration.json"
  echo "[rlp]   policy only, no model arms yet — /setup fills them from your providers"
fi

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