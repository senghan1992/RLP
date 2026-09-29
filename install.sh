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
#   RLP_REF    branch or tag        (default: the newest v* release tag, else main)
#
# `RLP_REF` used to default to `main`, which meant every install picked up
# whatever was pushed most recently — a mid-refactor commit included. A tool
# people download installs a *release*: the default is now the newest `v*` tag
# the remote carries, resolved with `git ls-remote` so it works against any
# `RLP_REPO` and needs no API token. `RLP_REF=main` still tracks the edge.
set -eu

REPO="${RLP_REPO:-https://github.com/senghan1992/RLP.git}"
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

# The newest `vMAJOR.MINOR.PATCH` tag on the remote.
#
# Sorted numerically field by field rather than lexically, because `sort` on
# text puts v0.10.0 before v0.9.0 and that is the one case a release tool must
# not get wrong. `--refs` drops the `^{}` peeled entries; anything that is not
# three numeric fields (a release candidate, a moving tag) is ignored, so only
# a real release can become the default.
latest_release_tag() {
  git ls-remote --tags --refs "$REPO" 'v[0-9]*' 2>/dev/null |
    sed -n 's|.*refs/tags/v\([0-9][0-9]*\.[0-9][0-9]*\.[0-9][0-9]*\)$|\1|p' |
    sort -t. -k1,1n -k2,2n -k3,3n |
    tail -1 |
    sed 's/^/v/'
}

REF="${RLP_REF:-}"
if [ -z "$REF" ]; then
  REF=$(latest_release_tag || true)
  if [ -n "$REF" ]; then
    echo "==> newest release: $REF  (RLP_REF=main to track the development branch)"
  else
    REF=main
    echo "==> no release tag on $REPO yet — installing from main"
  fi
fi

if [ -d "$DIR/.git" ]; then
  echo "==> updating RLP in $DIR ($REF)"
  git -C "$DIR" fetch --depth 1 origin "$REF"
  # `^{commit}` peels an annotated tag, so the checkout lands on the commit and
  # `git describe` reports the tag rather than a detached tag object.
  git -C "$DIR" checkout -q --detach "FETCH_HEAD^{commit}"
else
  echo "==> fetching RLP into $DIR ($REF)"
  mkdir -p "$(dirname -- "$DIR")"
  git clone --depth 1 --branch "$REF" "$REPO" "$DIR"
fi

exec sh "$DIR/scripts/install.sh" "$@"