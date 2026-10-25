#!/usr/bin/env bash
# Start Hermes Hook from the project's virtual environment, e.g. from the
# desktop menu. Works from any working directory.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
VENV_DIR="${HERMES_VENV:-$PROJECT_DIR/.venv}"

if [[ ! -f "$VENV_DIR/bin/activate" ]]; then
    echo "No virtual environment at $VENV_DIR (see readme.md, Installation)." >&2
    exit 1
fi

# shellcheck source=/dev/null
source "$VENV_DIR/bin/activate"
cd "$PROJECT_DIR"
exec python main.py "$@"
