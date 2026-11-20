#!/bin/bash
# Set up the audio transcription environment.
#
# Usage:
#   ./bootstrap.sh            # create ./venv and install both engines
#   ./bootstrap.sh --clean    # delete ./venv first and rebuild it
#
# Two engines are installed, so transcription works either way:
#   faster-whisper  The main engine. Used for live dictation (transcribe_app.py)
#                   and file transcription (transcribe.py). Fast and light.
#   openai-whisper  The backup: OpenAI's original Whisper, built on PyTorch.
#                   Same speech models and about the same accuracy, around 4x
#                   slower on CPU. Both programs switch to it automatically if
#                   faster-whisper fails to load. Force it with --engine openai.
#   The backup adds about 1 GB (PyTorch). For files it also needs the ffmpeg
#   command: this script uses Homebrew's ffmpeg (installing it if possible) and
#   always puts a self-contained copy in venv/bin as a fallback, so both
#   engines work after setup even on a Mac without Homebrew.
#
# Defensive: only the main engine is required. If the backup engine or Homebrew
# can't be installed on this machine, setup still finishes and says what is
# missing. Safe to re-run; an existing venv is reused unless --clean.

# If sourced (". bootstrap.sh"), run as a separate process instead, so the
# script's settings and helper functions (say, die, ...) don't leak into your
# terminal, and an error can't close it.
if [[ "${BASH_SOURCE[0]}" != "$0" ]]; then
    bash "${BASH_SOURCE[0]}" "$@"
    return $?
fi

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"
PROJECT_DIR="$(pwd)"
VENV_DIR="$PROJECT_DIR/venv"

CLEAN=0
for arg in "$@"; do
    case "$arg" in
        --clean) CLEAN=1 ;;
        --with-openai-whisper) ;;  # old flag; openai-whisper is now always attempted
        -h|--help)
            sed -n '2,22p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *) echo "Unknown option: $arg (try --help)"; exit 2 ;;
    esac
done

say()  { echo "==> $*"; }
warn() { echo "WARNING: $*" >&2; }
die()  { echo "ERROR: $*" >&2; exit 1; }

NOTES=""            # problems that didn't stop setup; repeated at the end
note() { warn "$*"; NOTES="$NOTES
  - $*"; }

# --- 1. Platform -----------------------------------------------------------
if [[ "$(uname -s)" != "Darwin" ]]; then
    warn "Written for macOS; continuing anyway (live dictation needs macOS)."
fi
say "Machine: $(uname -s) $(uname -m), macOS $(sw_vers -productVersion 2>/dev/null || echo '?')"

# Homebrew is often installed but missing from PATH in a fresh shell.
if ! command -v brew >/dev/null 2>&1; then
    for b in /opt/homebrew/bin/brew /usr/local/bin/brew; do
        if [[ -x "$b" ]]; then
            eval "$("$b" shellenv)"
            say "Found Homebrew at $b (it was not on PATH)"
            break
        fi
    done
fi
HAVE_BREW=0
command -v brew >/dev/null 2>&1 && HAVE_BREW=1

# Disk space: packages ~1.5 GB plus speech models (150 MB - 3 GB).
FREE_KB="$(df -Pk "$PROJECT_DIR" | awk 'NR==2 {print $4}')"
if [[ -n "$FREE_KB" && "$FREE_KB" -lt 3000000 ]]; then
    warn "Only $((FREE_KB / 1024)) MB free here; setup needs about 3 GB. It may fail."
fi

# --- 2. Pick a supported Python (3.9 - 3.13) ------------------------------
# Speech-model packages (ctranslate2, torch) publish builds for new Python
# versions only after a delay, so very new Pythons often fail to install.
# Override the choice with:  PYTHON=/path/to/python ./bootstrap.sh
version_ok() {
    "$1" -c 'import sys, venv; sys.exit(0 if (3, 9) <= sys.version_info[:2] <= (3, 13) else 1)' \
        >/dev/null 2>&1
}

# /usr/bin/python3 is only a stub (it opens an installer dialog) until the
# Xcode Command Line Tools are installed, so skip it in that case.
usable() {
    local p
    p="$(command -v "$1" 2>/dev/null)" || return 1
    if [[ "$p" == "/usr/bin/python3" ]] && ! xcode-select -p >/dev/null 2>&1; then
        return 1
    fi
    version_ok "$p" && echo "$p"
}

PY="${PYTHON:-}"
if [[ -n "$PY" ]]; then
    [[ -x "$PY" ]] || command -v "$PY" >/dev/null 2>&1 || die "PYTHON=$PY not found."
    version_ok "$PY" || warn "$PY is outside the tested 3.9-3.13 range; trying anyway."
else
    for candidate in python3.12 python3.11 python3.13 python3.10 python3.9 \
                     /opt/homebrew/bin/python3.12 /usr/local/bin/python3.12 \
                     /Library/Frameworks/Python.framework/Versions/3.12/bin/python3 \
                     python3; do
        if p="$(usable "$candidate")"; then PY="$p"; break; fi
    done
fi

if [[ -z "$PY" ]]; then
    if [[ $HAVE_BREW -eq 1 ]]; then
        say "No Python 3.9-3.13 found; installing python@3.12 with Homebrew..."
        brew install python@3.12 || die "brew install python@3.12 failed (see above)."
        PY="$(brew --prefix python@3.12)/bin/python3.12"
    else
        die "No Python 3.9-3.13 found, and Homebrew is not installed.
       Either install Python 3.12 from https://www.python.org/downloads/macos/
       or install Homebrew (https://brew.sh) and run: brew install python@3.12
       Then run ./bootstrap.sh again."
    fi
fi
say "Using Python: $PY ($("$PY" --version 2>&1))"

# --- 3. FFmpeg (the backup engine uses it for files) ----------------------
# faster-whisper and live dictation decode audio themselves. A self-contained
# ffmpeg is also put in venv/bin in step 5, so failing here is not a problem.
if command -v ffmpeg >/dev/null 2>&1; then
    say "FFmpeg found: $(command -v ffmpeg)"
elif [[ $HAVE_BREW -eq 1 ]]; then
    say "Installing FFmpeg with Homebrew (used by the openai-whisper backup for files)..."
    brew install ffmpeg || warn "brew install ffmpeg failed; the venv's own copy will be used."
else
    say "No system FFmpeg and no Homebrew; the venv's own copy will be used."
fi

# --- 4. Virtual environment -----------------------------------------------
if [[ $CLEAN -eq 1 && -d "$VENV_DIR" ]]; then
    say "Removing existing venv (--clean)..."
    rm -rf "$VENV_DIR"
fi

if [[ -x "$VENV_DIR/bin/python" ]] && "$VENV_DIR/bin/python" -c 'import sys' >/dev/null 2>&1; then
    say "Reusing existing venv: $VENV_DIR ($("$VENV_DIR/bin/python" --version 2>&1))"
else
    if [[ -d "$VENV_DIR" ]]; then
        say "Existing venv is broken (its Python is gone?); recreating it..."
        rm -rf "$VENV_DIR"
    fi
    say "Creating venv: $VENV_DIR"
    "$PY" -m venv "$VENV_DIR" || die "Could not create the venv with $PY."
fi

VPY="$VENV_DIR/bin/python"
"$VPY" -m pip --version >/dev/null 2>&1 || "$VPY" -m ensurepip --upgrade >/dev/null 2>&1 \
    || die "pip is missing from the venv. Try ./bootstrap.sh --clean"

# --- 5. Packages ----------------------------------------------------------
if ! curl -s --head --max-time 10 https://pypi.org/simple/ >/dev/null 2>&1; then
    warn "Can't reach pypi.org. Installing packages needs internet; trying anyway..."
fi

say "Upgrading pip..."
# Not setuptools: torch pins an older one, and upgrading it here made every run
# downgrade it again with a scary-looking pip conflict message.
"$VPY" -m pip install --quiet --upgrade pip wheel \
    || warn "pip upgrade failed; continuing with the current pip."

# Required: the main engine. Setup stops if this fails.
say "Installing requirements.txt (main engine: faster-whisper)..."
"$VPY" -m pip install --prefer-binary -r requirements.txt \
    || die "Installing the main engine failed (see the pip messages above).
       Check the internet connection, then try ./bootstrap.sh --clean"

# Optional: the backup engine. A failure here is reported but not fatal, so the
# main engine stays usable on machines PyTorch doesn't support.
say "Installing requirements-openai.txt (backup engine: openai-whisper; PyTorch is about 1 GB)..."
if ! "$VPY" -m pip install --prefer-binary -r requirements-openai.txt; then
    note "The openai-whisper backup engine could not be installed on this machine.
    Everything still works with the main engine (faster-whisper)."
fi

# Always: a self-contained ffmpeg in venv/bin (imageio-ffmpeg ships a static
# build). transcribe.sh adds venv/bin to the END of PATH, so a system ffmpeg
# (e.g. Homebrew's) still wins when present and this one is the fallback.
say "Installing the fallback FFmpeg into the venv (imageio-ffmpeg)..."
if "$VPY" -m pip install --quiet --prefer-binary imageio-ffmpeg \
   && FF="$("$VPY" -c 'import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())')" \
   && "$FF" -version >/dev/null 2>&1; then
    ln -sf "$FF" "$VENV_DIR/bin/ffmpeg"
    say "Fallback FFmpeg: $VENV_DIR/bin/ffmpeg -> $FF"
else
    warn "Could not install the fallback FFmpeg."
fi
export PATH="$PATH:$VENV_DIR/bin"
if command -v ffmpeg >/dev/null 2>&1; then
    say "FFmpeg that will be used: $(command -v ffmpeg)"
else
    note "No FFmpeg available. The openai-whisper backup can't read files
    (live dictation and the main engine don't need it). Fix: brew install ffmpeg"
fi

# --- 6. Verify ------------------------------------------------------------
say "Verifying installation..."
set +e
"$VPY" - <<'PYEOF'
# Really import each library: a package can install fine yet fail to load
# (e.g. a binary built for a newer macOS than this one).
import importlib, importlib.util, sys
REQUIRED = (("faster_whisper", "main engine"),
            ("av", "audio decoding + microphone capture"),
            ("ctranslate2", "main engine runtime"),
            ("numpy", "audio arrays"))
OPTIONAL = (("onnxruntime", "silence detection (VAD); without it use --no-vad"),
            ("whisper", "openai-whisper backup engine"),
            ("torch", "backup engine runtime"))

def check(mod):
    if importlib.util.find_spec(mod) is None:
        return "MISSING", ""
    try:
        m = importlib.import_module(mod)
        return "ok", getattr(m, "__version__", "")
    except Exception as e:
        return "BROKEN", (str(e).splitlines() or [type(e).__name__])[0][:150]

status = {}
for group, items in (("required", REQUIRED), ("optional", OPTIONAL)):
    for mod, why in items:
        st, info = check(mod)
        status[mod] = st
        tag = st if st == "ok" else st + (" (optional)" if group == "optional" else "")
        print(f"    {tag:<18} {mod} {info}  ({why})")

mic = False
if status["av"] == "ok":
    import av
    mic = "avfoundation" in av.formats_available
    print(f"    {'ok' if mic else 'MISSING':<18} microphone capture (AVFoundation in PyAV)")

main_ok = all(status[m] == "ok" for m, _ in REQUIRED)
backup_ok = status["whisper"] == "ok" and status["torch"] == "ok"
if not main_ok and not backup_ok:
    sys.exit(2)          # no engine at all
if not main_ok:
    sys.exit(3)          # only the backup engine
sys.exit(0 if (backup_ok and mic and status["onnxruntime"] == "ok") else 4)
PYEOF
RC=$?
set -e
case $RC in
    0) ;;
    2) die "No transcription engine could be loaded. Try ./bootstrap.sh --clean;
       see README 'Troubleshooting'." ;;
    3) note "The main engine (faster-whisper) won't load on this machine. The openai-whisper
    backup will be used automatically: it works, but more slowly." ;;
    4) note "Some optional parts are unavailable (see the list above). The main features still work." ;;
    *) die "Verification crashed (exit $RC)." ;;
esac

chmod +x transcribe.sh transcribe.py transcribe_app.py 2>/dev/null || true

cat <<EOF

Setup complete.

Live dictation window (microphone):
    ./transcribe.sh

Transcribe a file (no need to activate anything):
    ./transcribe.sh path/to/audio.opus

All options:
    ./transcribe.sh --help            (files)
    ./transcribe.sh --dictate --help  (live dictation)

The first run downloads the speech model (base model: about 150 MB).
EOF
if [[ -n "$NOTES" ]]; then
    echo
    echo "Set up with these limitations:$NOTES"
fi
