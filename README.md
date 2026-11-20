# Audio Transcription

Turns recordings into text on your Mac, fully offline. It accepts any common audio
or video format (Opus, WAV in any bit depth including 32-bit float, MP3, M4A, FLAC,
OGG, MP4, MOV and more), one file or whole folders at a time.

It replaces the older `final.sh` setup in `transcript/to_marianne/20250613`.

## Quick start

```bash
cd /System/Volumes/Data/data/projects/whisper_transcription
./bootstrap.sh                          # one-time setup (a few minutes; downloads about 1.5 GB)
./transcribe.sh ~/path/to/recording.opus
./transcribe.sh                         # no arguments: dictation window (microphone)
```

That writes `recording_transcription.txt` next to the recording. The first run
downloads the speech model (about 150 MB for the default `base` model). After
that it runs offline.

You don't need to activate a virtual environment: `transcribe.sh` uses the
project's own Python automatically, and you can run it from any folder.

## Files

| File | What it is |
|---|---|
| `bootstrap.sh` | One-time setup: finds a suitable Python, creates `venv/`, installs both engines and FFmpeg, and checks that they load |
| `requirements.txt` | Python packages for the main engine (faster-whisper). Required |
| `requirements-openai.txt` | Same, plus the openai-whisper backup engine (adds PyTorch, about 1 GB). Always installed if this Mac supports it |
| `transcribe.sh` | The command you run. A thin wrapper that starts `transcribe.py` (files) or `transcribe_app.py` (no arguments, or `--dictate`) with the project's Python |
| `transcribe.py` | The program that transcribes files |
| `transcribe_app.py` | Live dictation: a window with a Transcribe toggle and a text box (see "Live dictation") |
| `venv/` | Created by the bootstrap. Holds the installed packages. Safe to delete; rerun `bootstrap.sh` to recreate it |

## Everyday use

```bash
# One file
./transcribe.sh interview.wav

# Several files, or a folder (every audio file inside it)
./transcribe.sh a.opus b.m4a
./transcribe.sh ~/Recordings/

# Include subfolders, and put all transcripts in one place
./transcribe.sh ~/Recordings/ --recursive -o ~/Transcripts/

# More accurate model (slower)
./transcribe.sh interview.wav --model small

# Subtitles and machine-readable output as well as plain text
./transcribe.sh interview.wav --format txt,srt,json

# Help Whisper spell names and jargon correctly
./transcribe.sh interview.wav --prompt "Marianne, Berkeley, Irwin"

# Non-English, or let it detect the language
./transcribe.sh entrevista.opus --language es
./transcribe.sh unknown.mp3 --language auto

# Show the transcript in the terminal too
./transcribe.sh interview.wav --print
```

### Example: a recording on an external drive

You can run the command from any folder and point it at a file anywhere, for
example a field recorder's WAV on a USB drive:

```bash
/System/Volumes/Data/data/projects/whisper_transcription/transcribe.sh \
    /Volumes/exteral_2gb/trip_media/20261116_photo_shooting_tutorial/narration/261116_164426_TrLR.WAV
```

This writes `261116_164426_TrLR_transcription.txt` into the same `narration/`
folder as the recording. The uppercase `.WAV` extension and the recorder's 32-bit
float format are both fine.

- **Transcribe the whole folder:** pass the folder instead of the file. That picks
  up `261116_164426_TrLR.WAV` and `261116_164426_TrMic.WAV`. The `._...` files
  macOS creates on exFAT drives start with a dot, so they're ignored.
- **If the drive is read-only** (for example a locked SD card), the transcript
  can't be saved next to the recording. Add `-o ~/Transcripts/` to save it
  elsewhere. Check this before a long run: the program only finds out when it
  saves, after transcribing.
- **macOS may ask** whether Terminal (or VS Code) can access removable volumes the
  first time. Allow it, or reading the file fails with "Operation not permitted".

### All options

| Option | Default | Meaning |
|---|---|---|
| `-m`, `--model` | `base` | Model size: `tiny`, `base`, `small`, `medium`, `large-v3`, `turbo`. See below |
| `-l`, `--language` | `en` | Language code (`en`, `es`, `fr`, ...) or `auto` to detect |
| `-f`, `--format` | `txt` | Any of `txt`, `srt`, `json`, comma-separated |
| `-o`, `--output-dir` | next to each input | Folder for the outputs (created if needed) |
| `-r`, `--recursive` | off | Also search subfolders |
| `--overwrite` | off | Redo files that already have transcripts (otherwise they're skipped) |
| `--prompt` | none | Example text that steers spelling and style |
| `--device` | `cpu` | `mps` uses the Apple GPU: experimental, often faster |
| `--engine` | `auto` | `faster`, `openai`, or `auto` (faster-whisper, falling back to openai-whisper) |
| `--no-vad` | VAD on | Turn off silence skipping (see "How it works") |
| `--pause` | `1.5` | Seconds of silence that start a new paragraph in the `.txt` |
| `--max-sentences` | `5` | Start a new paragraph after this many sentences |
| `--threads` | all cores | CPU threads to use |
| `--print` | off | Also print each transcript to the terminal |

### Choosing a model

| Model | Download | Speed | Accuracy | When to use |
|---|---|---|---|---|
| `tiny` | 75 MB | fastest | rough | quick previews |
| `base` | 150 MB | fast | decent | default; clear single-speaker audio |
| `small` | 500 MB | moderate | good | **good choice for real interviews** |
| `medium` | 1.5 GB | slow | very good | difficult audio, accents |
| `large-v3` | 3 GB | slowest | best | when accuracy matters most |
| `turbo` | 1.6 GB | fairly fast | near large | good accuracy/speed balance |

On CPU, `base` transcribes much faster than real time; `medium` and `large-v3` can
take about as long as the recording itself or longer. Try `--device mps` with the
larger models.

## Live dictation

Run `transcribe.sh` with no arguments to dictate straight from the microphone,
without recording a file first:

```bash
./transcribe.sh                     # opens the dictation window
```

A small window opens with a **Transcribe** button and a text box:

- **Transcribe** turns listening on and off (the Space bar does the same). While
  it's on, the button turns red and a level meter shows that it hears you.
- Speak normally. Each time you pause (0.8 s by default), what you just said is
  transcribed and added to the text box.
- The same text is written at the same moment to `dictation_YYYYMMDD_HHMMSS.txt`
  in the current folder, so the file always matches the text box, and quitting
  or a crash keeps everything said so far. The file is created when the first
  words arrive. Turning Transcribe off and on again continues in the same file,
  starting a new paragraph.
- **Copy all** copies the transcript, **Show file** reveals the file in Finder,
  and **Quit** (or just closing the window) exits. Turning Transcribe off, or
  quitting, still transcribes whatever you were saying at that moment.

The window is a page served by the app itself on `127.0.0.1` (this machine only,
protected by a random key in its address). It opens as a standalone app window
when Google Chrome, Edge or Brave is installed, otherwise as a tab in your default
browser. Recording is done by the app, not the browser, so the browser never
asks for microphone access. A browser page is used because this venv's
Homebrew Python is built without Tk, so Python's built-in GUI toolkit isn't
available, and this approach needs no extra packages.

To pass options, start with `--dictate` (or `--live`):

```bash
./transcribe.sh --dictate --list-devices          # show microphones
./transcribe.sh --dictate --input 1               # pick one by number...
./transcribe.sh --dictate --input "MacBook Air"   # ...or by part of its name
./transcribe.sh --dictate --model small           # more accurate
./transcribe.sh --dictate -o ~/Transcripts/ --prompt "Marianne, Berkeley"
./transcribe.sh --dictate --no-gui                # terminal only: listens at once, Ctrl+C stops
./transcribe.sh --dictate --help                  # all options
```

| Option | Default | Meaning |
|---|---|---|
| `--list-devices` | | Show the audio inputs and exit |
| `-i`, `--input` | `default` | `default` (whatever macOS has selected under Sound > Input), a device number, or part of its name |
| `-m`, `--model` | `base` | Same models as for files. `base` or `small` keep up best in real time |
| `-l`, `--language` | `en` | Language code, or `auto` |
| `--prompt` | none | Steers spelling of names and jargon. Recent dictated text is also passed as context automatically |
| `-o`, `--output-dir` | current folder | Where the session file goes |
| `--no-save` | off | Don't write a file (text box / terminal only) |
| `--no-gui` | off | No window: start listening immediately and print the text in the terminal; Ctrl+C stops |
| `--no-browser` | off | Don't open the window automatically; print its address to open yourself |
| `--port` | any free | Port for the window's local server |
| `--copy` | off | Copy the full transcript to the clipboard when you quit |
| `--silence` | `0.8` | Seconds of pause that end an utterance. Raise it if sentences get cut mid-thought |
| `--max-utterance` | `25` | Transcribe after this many seconds even if you haven't paused |
| `--pause` | `4` | Seconds of pause that start a new paragraph |
| `--threshold` | automatic | Fixed speech level (0-1). Try `0.01`-`0.03` if background noise keeps it recording |
| `--engine` | `auto` | `auto` (faster-whisper, switching to openai-whisper if it won't load), `faster` or `openai` |
| `--threads` | all cores | CPU threads |

**How it works:** audio is captured through macOS AVFoundation using the FFmpeg
libraries bundled in PyAV (the `ffmpeg` command isn't involved). Each time
Transcribe is turned on, the first 0.6 s measure the room's background noise;
after that, sound well above that level counts as speech. When you pause, the
utterance is transcribed by **faster-whisper** (with VAD on), or by the
openai-whisper backup if faster-whisper won't load (see "The two engines"). Recording continues on a background thread while it
transcribes, so nothing is dropped. The model is loaded once at startup, so
toggling is instant.

**Microphone permission:** the first time you press Transcribe, macOS asks to let
your terminal app (Terminal, iTerm, or VS Code) use the microphone. If the window
shows "Waiting for the microphone..." and no prompt appears, open System Settings
> Privacy & Security > Microphone, turn on that app, then quit and reopen it.

**Note:** `--input default` follows the macOS Sound setting. Virtual devices like
`ZoomAudioDevice` show up in `--list-devices` too; choose the real microphone by
name if the default isn't the one you want.

## Outputs

For an input `talk.wav`:

- **`talk_transcription.txt`**: plain text split into paragraphs. A new paragraph
  starts at a pause of 1.5 s or more, or after 5 sentences.
- **`talk_transcription.srt`**: numbered subtitle blocks with start and end
  times. Opens in video players, and handy for finding where something was said.
- **`talk_transcription.json`**: every segment with start/end seconds, plus the
  engine, model, detected language and duration. Use this for further processing.

**Name clashes:** if one folder contains both `talk.wav` and `talk.opus`, the
format is kept in the output name so neither overwrites the other:
`talk_wav_transcription.txt` and `talk_opus_transcription.txt`.

**Your recordings are never modified.** The program only reads input files.
Output names always end in `_transcription.<format>`, so they can never be the
same as an input. Outputs are written to a temporary `.part` file first and then
renamed, so an interrupted run never leaves a half-written transcript that looks
finished.

## How it works

```
recording (any format)
   │
   ├─ PyAV (FFmpeg libraries bundled inside the Python package)
   │     decodes it and resamples to 16 kHz mono floating point,
   │     which is the format Whisper expects
   │
   ├─ Silero VAD (via onnxruntime)
   │     finds the stretches of silence and skips them: faster, and it
   │     avoids Whisper "hallucinating" words during long silences
   │
   ├─ Whisper model (run by CTranslate2, int8 on CPU)
   │     turns speech into timestamped text segments
   │
   └─ transcribe.py
         joins the segments into paragraphs, writes .txt / .srt / .json
```

- **Whisper** is OpenAI's open-source speech recognition model. It runs entirely
  on your machine; no audio is uploaded anywhere.
- **faster-whisper** is a reimplementation of Whisper on the CTranslate2 runtime.
  It produces the same text as the original but is several times faster and uses
  far less memory, which is why it's the default engine.
- The model is loaded **once** per run and reused for every file, so batches
  don't pay the loading cost for each file.
- **No conversion step.** The old `final.sh` scripts converted everything to WAV
  with the `ffmpeg` command first. That isn't needed: the decoder reads the
  original file directly. Removing that step also removed the bug that broke
  non-`.opus` inputs.

### The two engines

`bootstrap.sh` always installs both, so transcription works either way:

| Engine | Role | Used for |
|---|---|---|
| **faster-whisper** | Main engine. Fast, light | **All live dictation** and file transcription, by default |
| **openai-whisper** | Backup: OpenAI's original Whisper, on PyTorch. Same models, about the same accuracy, around 4x slower on CPU | Files and live dictation, only if faster-whisper won't load, or when you choose it |

**Which engine does live dictation?** faster-whisper. If it can't be loaded on a
machine, the dictation app switches to openai-whisper automatically and prints
`engine: openai-whisper` in the terminal. File transcription does the same.
To pick one yourself:

```bash
./transcribe.sh interview.wav --engine openai     # files
./transcribe.sh --dictate --engine openai         # live dictation
```

**FFmpeg:** the openai-whisper engine needs the `ffmpeg` command to read *files*
(live dictation and faster-whisper don't need it). The bootstrap uses Homebrew's
`ffmpeg`, installing it if possible, **and always puts a self-contained copy in
`venv/bin/ffmpeg`** (from the `imageio-ffmpeg` package). `transcribe.sh` adds
`venv/bin` to the *end* of `PATH`, so Homebrew's copy is used when present and the
bundled one takes over if Homebrew's is missing or later removed. After the
bootstrap, both engines work for both files and dictation, with or without Homebrew.

## Setup details (what `bootstrap.sh` does)

1. **Picks a Python version between 3.9 and 3.13**, preferring 3.12, then 3.11.
   Very new versions such as your default `python3` (3.15) are skipped because
   the speech-model packages don't publish builds for them yet. Override with
   `PYTHON=/path/to/python3.x ./bootstrap.sh`. If no suitable Python exists, it
   installs `python@3.12` with Homebrew.
2. **FFmpeg:** uses Homebrew's, installing it if Homebrew is available.
3. **Creates `venv/`**, or reuses an existing one (and recreates it if it's
   broken, for example after a Python upgrade). `--clean` deletes and rebuilds it.
4. **Installs the main engine** (`requirements.txt`). This is the only part that
   must succeed.
5. **Installs the backup engine** (`requirements-openai.txt`, PyTorch).
6. **Installs the fallback FFmpeg** into `venv/bin/ffmpeg`.
7. **Imports every library** and lists each one as `ok`, `MISSING` or `BROKEN`,
   plus whether microphone capture is available.

All installs use `--prefer-binary`, so pip chooses the newest version of each
package that has a ready-made build for your Mac rather than trying to compile
one. Compiling fails on this machine; see Troubleshooting.

**Defensive on other machines.** Setup only stops if the main engine can't be
installed (or if no engine at all will load). Everything else degrades
gracefully, with a "Set up with these limitations" summary at the end:

| Missing on the target Mac | What the bootstrap does |
|---|---|
| Homebrew not on `PATH` (common on a fresh Apple Silicon Mac) | Finds it in `/opt/homebrew` or `/usr/local` anyway |
| Homebrew entirely | Uses the bundled FFmpeg; for Python, tells you to install Python 3.12 from python.org or Homebrew |
| A suitable Python | Installs `python@3.12` with Homebrew. Skips the `/usr/bin/python3` stub when the Xcode Command Line Tools aren't installed (it would only pop up an installer) |
| Homebrew's FFmpeg | Uses the bundled copy in `venv/bin` |
| PyTorch support (backup engine won't install) | Continues; the main engine does everything |
| faster-whisper won't load | Continues; both programs use the backup engine automatically |
| Internet / disk space | Warns up front (about 3 GB needed) instead of failing mysteriously |

Rerunning `./bootstrap.sh` is safe at any time; it just updates packages. The old
`--with-openai-whisper` flag is still accepted but no longer needed.

### Why two version limits exist (macOS 13.0)

This Mac runs macOS 13.0, and some current package builds target newer macOS
releases:

- **PyAV:** the newest releases have no ready-made macOS 13 build. Without
  `--prefer-binary`, pip tries to compile PyAV against Homebrew's FFmpeg 7.1 and
  fails. With it, pip picks av 15.1, which installs cleanly.
- **onnxruntime 1.22+** installs but crashes on load, because it was built for
  macOS 13.4 or later. `requirements.txt` caps it below 1.22 on macOS older than
  13.4 only, using the line
  `onnxruntime<1.22; sys_platform == "darwin" and platform_release < "22.5"`
  (Darwin 22.5 = macOS 13.4). On a newer Mac the cap doesn't apply.

Updating macOS removes both limitations automatically on the next
`./bootstrap.sh --clean`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `venv not found. Run bootstrap.sh first` | Run `./bootstrap.sh` |
| Bootstrap ends with "Set up with these limitations" | Read the listed items: optional parts are missing, but transcription works. Each item says what still works |
| `openai-whisper needs the ffmpeg command` | Rerun `./bootstrap.sh` (it installs a bundled copy), or `brew install ffmpeg` |
| Bootstrap prints `BROKEN <package>` | A package installed but won't load on this macOS. Run `./bootstrap.sh --clean`; if it persists, cap that package's version in `requirements.txt` the same way onnxruntime is capped |
| `Failed building wheel for av` (or another package) | Make sure `bootstrap.sh` still has `--prefer-binary`; pip is trying to compile from source |
| `Applying the VAD filter requires the onnxruntime package` | onnxruntime failed to load; see `BROKEN` above. Use `--no-vad` meanwhile |
| Words are misspelled, especially names | Use `--model small` or larger, and/or `--prompt "Name One, Name Two"` |
| Made-up repeated phrases during silent stretches | Keep VAD on (the default); try a larger model |
| `WARNING: no speech detected` | The file is silent or music-only, or `--language` is wrong; try `--language auto` |
| Slow | Use a smaller model, or try `--device mps` |
| Model download fails | Needs internet the first time only. Models are cached in `~/.cache/huggingface/` |
| Want to start completely fresh | `./bootstrap.sh --clean` (models stay cached) |
| Dictation stuck on "Waiting for the microphone..." | Grant microphone access to your terminal app (see "Live dictation"), then restart that app |
| Dictation never stops "recording" | Background noise is above the speech level; use `--threshold 0.02` (raise it until it settles) |
| Dictation cuts sentences in half | Use `--silence 1.5` |

## Exit codes (for scripting)

- `0`: every file was transcribed or skipped as already done
- `1`: at least one file failed (the others still complete; failures are listed at the end)
- `2`: bad arguments, or nothing to transcribe (for example, a path that doesn't exist)
