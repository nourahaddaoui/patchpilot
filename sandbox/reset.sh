#!/usr/bin/env bash
# Reset the sandbox clone the agent works in.
#
#   bash sandbox/reset.sh [SOURCE_DIR] [DEST_DIR]
#
# Copies SOURCE_DIR (default: sandbox/template) into DEST_DIR (default:
# sandbox/work), commits it as a baseline on branch "main", and leaves the
# agent on branch "patchpilot/fix". Running it twice gives the same result.
# It only ever deletes a directory it created itself (marked inside .git).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE="${1:-$HERE/template}"
DEST="${2:-$HERE/work}"
MARKER=".git/patchpilot-sandbox"
GIT=(git -c user.name=PatchPilot -c user.email=patchpilot@localhost -c commit.gpgsign=false -c core.hooksPath=/dev/null)

if [ ! -d "$SOURCE" ]; then
  echo "reset: source '$SOURCE' is not a directory" >&2
  exit 1
fi
SOURCE="$(cd "$SOURCE" && pwd)"

if [ -e "$DEST" ]; then
  if [ ! -f "$DEST/$MARKER" ]; then
    echo "reset: '$DEST' exists but is not a PatchPilot sandbox; refusing to delete it" >&2
    exit 1
  fi
  rm -rf "$DEST"
fi

mkdir -p "$DEST"
cp -R "$SOURCE"/. "$DEST"/
rm -rf "$DEST/.git"
find "$DEST" \( -name __pycache__ -o -name .pytest_cache \) -type d -prune -exec rm -rf {} +

cd "$DEST"
"${GIT[@]}" init -q
"${GIT[@]}" checkout -q -b main
"${GIT[@]}" add -A
"${GIT[@]}" commit -q -m "Baseline from $(basename "$SOURCE")"
"${GIT[@]}" checkout -q -b patchpilot/fix
touch "$MARKER"

echo "sandbox ready: $DEST (branch patchpilot/fix, baseline $(git rev-parse --short HEAD))"
