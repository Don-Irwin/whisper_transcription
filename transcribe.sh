#!/bin/bash
# Run the transcriber with the project's venv, from any directory.
# Usage:
#   ./transcribe.sh [options] <file-or-folder> [...]   transcribe files     (see --help)
#   ./transcribe.sh                                    open the live dictation window
#   ./transcribe.sh --dictate [options]                live dictation with options
#                                                      (alias: --live; see --dictate --help)

# If sourced (". transcribe.sh"), run as a separate process instead. Otherwise
# "exec" below would replace your shell and the terminal would close when the
# program ends, and "set -euo pipefail" would stay on in your terminal.
if [[ "${BASH_SOURCE[0]}" != "$0" ]]; then
    bash "${BASH_SOURCE[0]}" "$@"
    return $?
fi

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$HERE/venv/bin/python"
if [[ ! -x "$PY" ]]; then
    echo "ERROR: venv not found. Run $HERE/bootstrap.sh first." >&2
    exit 1
fi

# Fallback tools installed by bootstrap (e.g. ffmpeg) live in venv/bin. Added at
# the end of PATH so system versions (Homebrew's ffmpeg) are preferred.
export PATH="$PATH:$HERE/venv/bin"

if [[ $# -eq 0 ]]; then
    exec "$PY" "$HERE/transcribe_app.py"
fi
case "$1" in
    --dictate|--live)
        shift
        exec "$PY" "$HERE/transcribe_app.py" "$@" ;;
esac
exec "$PY" "$HERE/transcribe.py" "$@"
