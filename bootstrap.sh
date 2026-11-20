#!/bin/bash
# Set up the audio transcription environment.
#
# Usage:
#   ./bootstrap.sh                        # create ./venv and install requirements
#   ./bootstrap.sh --clean                # delete ./venv first and rebuild it
#   ./bootstrap.sh --with-openai-whisper  # also install the optional openai-whisper engine
#
# Safe to re-run: an existing venv is reused and updated unless --clean is given.

set -euo pipefail

cd "$(dirname "$0")"
PROJECT_DIR="$(pwd)"
VENV_DIR="$PROJECT_DIR/venv"

CLEAN=0
WITH_OPENAI=0
for arg in "$@"; do
    case "$arg" in
        --clean) CLEAN=1 ;;
        --with-openai-whisper) WITH_OPENAI=1 ;;
        -h|--help)
            sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *) echo "Unknown option: $arg (try --help)"; exit 2 ;;
    esac
done

say()  { echo "==> $*"; }
warn() { echo "WARNING: $*" >&2; }
die()  { echo "ERROR: $*" >&2; exit 1; }

# --- 1. Platform -----------------------------------------------------------
[[ "$(uname -s)" == "Darwin" ]] || warn "Written for macOS; continuing anyway."

# --- 2. Pick a supported Python (3.9 - 3.13) ------------------------------
# Speech-model packages (ctranslate2, torch) publish wheels for released
# Python versions only after a delay, so very new Pythons often fail to install.
# Override the choice with:  PYTHON=/path/to/python ./bootstrap.sh
version_ok() {
    "$1" -c 'import sys; sys.exit(0 if (3, 9) <= sys.version_info[:2] <= (3, 13) else 1)' 2>/dev/null
}

PY="${PYTHON:-}"
if [[ -n "$PY" ]]; then
    version_ok "$PY" || warn "$PY is outside the tested 3.9-3.13 range; trying anyway."
else
    for candidate in python3.12 python3.11 python3.13 python3.10 python3; do
        if command -v "$candidate" >/dev/null 2>&1 && version_ok "$candidate"; then
            PY="$(command -v "$candidate")"
            break
        fi
    done
fi

if [[ -z "$PY" ]]; then
    if command -v brew >/dev/null 2>&1; then
        say "No Python 3.9-3.13 found; installing python@3.12 with Homebrew..."
        brew install python@3.12
        PY="$(brew --prefix python@3.12)/bin/python3.12"
    else
        die "No Python 3.9-3.13 found and Homebrew is not installed.
       Install Homebrew (https://brew.sh), then run: brew install python@3.12"
    fi
fi
say "Using Python: $PY ($("$PY" --version 2>&1))"

# --- 3. FFmpeg (needed only by the optional openai-whisper engine) --------
if command -v ffmpeg >/dev/null 2>&1; then
    say "FFmpeg found: $(command -v ffmpeg)"
elif [[ $WITH_OPENAI -eq 1 ]]; then
    if command -v brew >/dev/null 2>&1; then
        say "Installing FFmpeg (required by openai-whisper)..."
        brew install ffmpeg
    else
        die "openai-whisper needs FFmpeg. Install Homebrew, then: brew install ffmpeg"
    fi
else
    say "FFmpeg not found. That's fine: faster-whisper decodes audio itself."
fi

# --- 4. Virtual environment -----------------------------------------------
if [[ $CLEAN -eq 1 && -d "$VENV_DIR" ]]; then
    say "Removing existing venv (--clean)..."
    rm -rf "$VENV_DIR"
fi

if [[ -x "$VENV_DIR/bin/python" ]]; then
    say "Reusing existing venv: $VENV_DIR ($("$VENV_DIR/bin/python" --version 2>&1))"
else
    say "Creating venv: $VENV_DIR"
    "$PY" -m venv "$VENV_DIR"
fi

VPY="$VENV_DIR/bin/python"

# --- 5. Packages ----------------------------------------------------------
say "Upgrading pip..."
"$VPY" -m pip install --quiet --upgrade pip setuptools wheel

if [[ $WITH_OPENAI -eq 1 ]]; then
    say "Installing requirements-openai.txt (faster-whisper + openai-whisper; PyTorch is large)..."
    "$VPY" -m pip install --prefer-binary -r requirements-openai.txt
else
    say "Installing requirements.txt..."
    "$VPY" -m pip install --prefer-binary -r requirements.txt
fi

# --- 6. Verify ------------------------------------------------------------
say "Verifying installation..."
"$VPY" - <<'PYEOF'
# Really import each library: a package can install fine yet fail to load
# (e.g. a binary built for a newer macOS than this one).
import importlib, importlib.util
ok = True
for mod, why in (("faster_whisper", "transcription engine"),
                 ("av", "audio decoding"),
                 ("ctranslate2", "model runtime"),
                 ("onnxruntime", "silence detection (VAD)"),
                 ("whisper", "optional openai-whisper engine")):
    if importlib.util.find_spec(mod) is None:
        if mod != "whisper":
            print(f"    MISSING {mod} ({why})"); ok = False
        continue
    try:
        m = importlib.import_module(mod)
        print(f"    ok      {mod} {getattr(m, '__version__', '')}  ({why})")
    except Exception as e:
        print(f"    BROKEN  {mod} ({why}): {str(e).splitlines()[0][:150]}"); ok = False
if not ok:
    raise SystemExit("Verification failed - see README 'Troubleshooting'.")
PYEOF

chmod +x transcribe.sh transcribe.py

cat <<EOF

Setup complete.

Transcribe a file (no need to activate anything):
    ./transcribe.sh path/to/audio.opus

Transcribe a whole folder:
    ./transcribe.sh path/to/folder

All options:
    ./transcribe.sh --help

The first run downloads the speech model (base model: about 150 MB).
EOF
