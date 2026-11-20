#!/bin/bash
# Run transcribe.py with the project's venv, from any directory.
# Usage: ./transcribe.sh [options] <file-or-folder> [...]    (see --help)
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
if [[ ! -x "$HERE/venv/bin/python" ]]; then
    echo "ERROR: venv not found. Run $HERE/bootstrap.sh first." >&2
    exit 1
fi
exec "$HERE/venv/bin/python" "$HERE/transcribe.py" "$@"
