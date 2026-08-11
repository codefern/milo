#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

if command -v uv >/dev/null 2>&1; then
  UV_BIN="$(command -v uv)"
else
  if ! command -v python3 >/dev/null 2>&1; then
    printf 'Milo setup requires Python 3.11 or newer.\n' >&2
    exit 1
  fi
  python3 -m pip install --user 'uv>=0.8,<1'
  UV_BIN="$(python3 -m site --user-base)/bin/uv"
fi

"$UV_BIN" tool install --force "$ROOT_DIR"

MILO_BIN_DIR="$("$UV_BIN" tool dir --bin)"
MILO_BIN="$MILO_BIN_DIR/milo"
if [[ ! -x "$MILO_BIN" ]]; then
  printf 'Milo installed, but its bin directory is not on PATH.\n' >&2
  printf 'Add %s to PATH, then run: milo setup\n' "$(dirname -- "$MILO_BIN")" >&2
  exit 1
fi

"$MILO_BIN" --version
printf '\nMilo is installed. Start with:\n  milo setup\n  milo\n'
