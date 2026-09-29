#!/bin/sh
# RLP installer (bootstrap).
#
#   curl -fsSL https://raw.githubusercontent.com/senghan1992/RLP/main/install.sh | sh
#
# Piped, it fetches RLP into $RLP_DIR and runs the real installer. Run from a
# checkout (`sh install.sh`), it simply delegates to scripts/install.sh, so both
# entry points behave the same.
#
# Env:
#   RLP_DIR    where to fetch RLP   (default: ${XDG_DATA_HOME:-$HOME/.local/share}/rlp)
#   RLP_REPO   git URL              (default: https://github.com/senghan1992/RLP.git)
#   RLP_REF    branch or tag        (default: main)
set -eu

REPO="${RLP_REPO:-https://github.com/senghan1992/RLP.git}"
REF="${RLP_REF:-main}"
DIR="${RLP_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/rlp}"

# If this script is a real file whose checkout has scripts/install.sh, we were
# run from a clone: use it directly.
SELF="${0:-}"
if [ -f "$SELF" ]; then
  HERE=$(CDPATH= cd -- "$(dirname -- "$SELF")" && pwd)
  if [ -f "$HERE/scripts/install.sh" ]; then
    exec sh "$HERE/scripts/install.sh" "$@"
  fi
fi

command -v git >/dev/null 2>&1 || {
  echo "RLP: git is required to install but was not found." >&2
  echo "     install git, then re-run this installer." >&2
  exit 1
}

if [ -d "$DIR/.git" ]; then
  echo "==> updating RLP in $DIR ($REF)"
  git -C "$DIR" fetch --depth 1 origin "$REF"
  git -C "$DIR" checkout -q --detach FETCH_HEAD
else
  echo "==> fetching RLP into $DIR ($REF)"
  mkdir -p "$(dirname -- "$DIR")"
  git clone --depth 1 --branch "$REF" "$REPO" "$DIR"
fi

exec sh "$DIR/scripts/install.sh" "$@"