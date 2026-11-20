#!/usr/bin/env python3
"""Transcribe audio files to text with Whisper.

Accepts any audio/video format FFmpeg understands (opus, ogg, wav in any bit
depth including 32-bit float, mp3, m4a, flac, aac, webm, mp4, ...), single files
or whole folders. The speech model is loaded once and reused for every file.

Outputs go next to each input (or into --output-dir) as
    <name>_transcription.txt   paragraphed plain text   (default)
    <name>_transcription.srt   subtitles with timestamps (--format srt)
    <name>_transcription.json  segments + metadata       (--format json)

Input files are only ever read, never modified, moved or deleted.

Exit status: 0 = every file transcribed or skipped, 1 = at least one failed,
2 = bad arguments / nothing to do.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path

# Quiet CTranslate2's startup tips/warnings (errors still show). Set CT2_VERBOSE=0 to restore.
os.environ.setdefault("CT2_VERBOSE", "-1")

AUDIO_EXTS = {
    ".opus", ".ogg", ".oga", ".wav", ".wave", ".mp3", ".m4a", ".aac", ".flac",
    ".aif", ".aiff", ".wma", ".amr", ".webm", ".mp4", ".mov", ".mkv", ".m4v",
}
OUTPUT_SUFFIX = "_transcription"
FORMATS = ("txt", "srt", "json")
MODELS = ("tiny", "base", "small", "medium", "large-v3", "turbo")


@dataclass
class Segment:
    start: float
    end: float
    text: str


@dataclass
class Result:
    segments: list[Segment]
    language: str
    duration: float | None


# --------------------------------------------------------------------------
# Engines
# --------------------------------------------------------------------------

class FasterWhisperEngine:
    """CTranslate2 implementation: ~4x faster than openai-whisper on CPU, low memory.
    Decodes audio itself via PyAV, so no ffmpeg binary is needed."""

    name = "faster-whisper"

    def __init__(self, model: str, threads: int, device: str = "cpu"):
        from faster_whisper import WhisperModel  # ImportError if not installed

        # int8 is the fastest CPU mode; the Apple GPU (mps) backend wants float16.
        compute = "int8" if device == "cpu" else "float16"
        self.model = WhisperModel(model, device=device, compute_type=compute,
                                  cpu_threads=threads)

    def transcribe(self, path: Path, language: str | None, prompt: str | None,
                   vad: bool, progress) -> Result:
        seg_iter, info = self.model.transcribe(
            str(path), language=language, initial_prompt=prompt,
            vad_filter=vad, beam_size=5,
        )
        segments = []
        for s in seg_iter:  # lazy generator: transcription happens here
            segments.append(Segment(s.start, s.end, s.text))
            progress(s.end, info.duration)
        return Result(segments, info.language, info.duration)


class OpenAIWhisperEngine:
    """OpenAI's reference implementation (PyTorch). Needs `ffmpeg` on PATH."""

    name = "openai-whisper"

    def __init__(self, model: str, threads: int, device: str = "cpu"):
        import whisper  # ImportError if not installed

        if not shutil.which("ffmpeg"):
            raise RuntimeError("openai-whisper needs the `ffmpeg` command: brew install ffmpeg")
        try:
            import torch
            torch.set_num_threads(threads)
        except Exception:
            pass
        self.whisper = whisper
        self.model = whisper.load_model(model, device="cpu")

    def transcribe(self, path: Path, language: str | None, prompt: str | None,
                   vad: bool, progress) -> Result:
        r = self.model.transcribe(str(path), language=language, initial_prompt=prompt,
                                  fp16=False, verbose=None)
        segments = [Segment(s["start"], s["end"], s["text"]) for s in r["segments"]]
        duration = segments[-1].end if segments else None
        return Result(segments, r.get("language", language or "?"), duration)


def load_engine(choice: str, model: str, threads: int, device: str = "cpu"):
    errors = []
    order = {"auto": ["faster", "openai"], "faster": ["faster"], "openai": ["openai"]}[choice]
    for name in order:
        cls = FasterWhisperEngine if name == "faster" else OpenAIWhisperEngine
        try:
            print(f"Loading {cls.name} model '{model}' (first use downloads it)...", flush=True)
            return cls(model, threads, device if name == "faster" else "cpu")
        except ImportError:
            errors.append(f"{cls.name} is not installed")
        except Exception as e:
            errors.append(f"{cls.name}: {e}")
    raise SystemExit("ERROR: no usable transcription engine.\n  " + "\n  ".join(errors)
                     + "\n  Run ./bootstrap.sh to install.")


# --------------------------------------------------------------------------
# Inputs and output naming
# --------------------------------------------------------------------------

def collect_inputs(paths: list[str], recursive: bool) -> tuple[list[Path], list[str]]:
    files, problems, seen = [], [], set()
    for raw in paths:
        p = Path(raw).expanduser()
        if p.is_dir():
            walker = p.rglob("*") if recursive else p.glob("*")
            found = sorted(f for f in walker if f.is_file() and f.suffix.lower() in AUDIO_EXTS
                           and not f.name.startswith("."))
            if not found:
                problems.append(f"no audio files in folder: {p}")
            candidates = found
        elif p.is_file():
            if p.suffix.lower() not in AUDIO_EXTS:
                print(f"Note: unrecognised extension '{p.suffix}', trying anyway: {p}")
            candidates = [p]
        else:
            problems.append(f"not found: {p}")
            candidates = []
        for f in candidates:
            key = f.resolve()
            if key not in seen:
                seen.add(key)
                files.append(f)
    return files, problems


def output_bases(files: list[Path], output_dir: Path | None) -> dict[Path, Path]:
    """Map each input to '<dir>/<stem>_transcription' (extension added per format).

    If two inputs would collide (e.g. talk.wav and talk.opus in one folder), the
    original extension is kept in the name: talk_wav_transcription, talk_opus_...
    The suffix guarantees an output path can never equal an input path."""
    def base(f: Path, keep_ext: bool) -> Path:
        d = output_dir or f.parent
        stem = f"{f.stem}_{f.suffix.lstrip('.').lower()}" if keep_ext else f.stem
        return d / f"{stem}{OUTPUT_SUFFIX}"

    plain = {f: base(f, False) for f in files}
    counts: dict[Path, int] = {}
    for b in plain.values():
        counts[b] = counts.get(b, 0) + 1
    return {f: (base(f, True) if counts[b] > 1 else b) for f, b in plain.items()}


# --------------------------------------------------------------------------
# Formatting
# --------------------------------------------------------------------------

SENTENCE_END = re.compile(r"[.!?][\"')\]]*$")


def to_paragraphs(segments: list[Segment], pause: float, max_sentences: int) -> str:
    """Join segments into paragraphs. A new paragraph starts after a silence of
    at least `pause` seconds, or after `max_sentences` sentences end."""
    paras, cur, sentences, prev_end = [], [], 0, None
    for s in segments:
        text = s.text.strip()
        if not text:
            continue
        if cur and prev_end is not None and s.start - prev_end >= pause:
            paras.append(" ".join(cur))
            cur, sentences = [], 0
        cur.append(text)
        sentences += len(re.findall(r"[.!?](?=\s|$)", text))
        prev_end = s.end
        if sentences >= max_sentences and SENTENCE_END.search(text):
            paras.append(" ".join(cur))
            cur, sentences = [], 0
    if cur:
        paras.append(" ".join(cur))
    return "\n\n".join(paras) + "\n"


def srt_time(t: float) -> str:
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def to_srt(segments: list[Segment]) -> str:
    blocks = [f"{i}\n{srt_time(s.start)} --> {srt_time(s.end)}\n{s.text.strip()}\n"
              for i, s in enumerate((s for s in segments if s.text.strip()), 1)]
    return "\n".join(blocks)


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def make_progress(label: str):
    last = [-1]

    def progress(pos: float, total: float | None):
        if not total or not sys.stdout.isatty():
            return
        pct = min(100, int(pos / total * 100))
        if pct != last[0]:
            last[0] = pct
            print(f"\r  {label}: {pct:3d}%", end="", flush=True)

    return progress


def fmt_secs(t: float | None) -> str:
    if t is None:
        return "?"
    m, s = divmod(int(round(t)), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Transcribe audio files (any format) to text with Whisper.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="examples:\n"
               "  ./transcribe.sh interview.opus\n"
               "  ./transcribe.sh recordings/ --recursive --format txt,srt\n"
               "  ./transcribe.sh talk.wav --model small --language auto -o transcripts/\n",
    )
    p.add_argument("inputs", nargs="+", help="audio files and/or folders")
    p.add_argument("-m", "--model", default="base",
                   help=f"model size: {', '.join(MODELS)} (default: base). Bigger = more "
                        "accurate but slower")
    p.add_argument("-l", "--language", default="en",
                   help="language code such as en, es, fr, or 'auto' to detect (default: en)")
    p.add_argument("-f", "--format", default="txt",
                   help=f"comma-separated output formats from {', '.join(FORMATS)} (default: txt)")
    p.add_argument("-o", "--output-dir", type=Path,
                   help="write outputs here instead of next to each input")
    p.add_argument("-r", "--recursive", action="store_true", help="search folders recursively")
    p.add_argument("--overwrite", action="store_true",
                   help="redo files whose outputs already exist (default: skip them)")
    p.add_argument("--engine", choices=("auto", "faster", "openai"), default="auto",
                   help="auto = faster-whisper, falling back to openai-whisper (default)")
    p.add_argument("--prompt",
                   help="hint text to steer spelling of names/jargon, e.g. 'Marianne, Berkeley'")
    p.add_argument("--no-vad", action="store_true",
                   help="disable silence skipping (faster-whisper only)")
    p.add_argument("--pause", type=float, default=1.5,
                   help="silence in seconds that starts a new paragraph (default: 1.5)")
    p.add_argument("--max-sentences", type=int, default=5,
                   help="start a new paragraph after this many sentences (default: 5)")
    p.add_argument("--device", choices=("cpu", "mps"), default="cpu",
                   help="cpu (default, reliable) or mps = Apple GPU (experimental, often "
                        "faster; faster-whisper only)")
    p.add_argument("--threads", type=int, default=os.cpu_count() or 4,
                   help="CPU threads (default: all cores)")
    p.add_argument("--print", action="store_true", dest="print_text",
                   help="also print each transcript to the terminal")
    args = p.parse_args(argv)

    args.formats = [f.strip().lower() for f in args.format.split(",") if f.strip()]
    bad = [f for f in args.formats if f not in FORMATS]
    if bad or not args.formats:
        p.error(f"unknown format(s): {', '.join(bad) or '(none)'}; choose from {', '.join(FORMATS)}")
    args.lang = None if args.language.lower() == "auto" else args.language
    return args


def main(argv=None) -> int:
    args = parse_args(argv)

    files, problems = collect_inputs(args.inputs, args.recursive)
    for msg in problems:
        print(f"WARNING: {msg}", file=sys.stderr)
    if not files:
        print("ERROR: nothing to transcribe.", file=sys.stderr)
        return 2

    bases = output_bases(files, args.output_dir)
    todo = []
    skipped = 0
    for f in files:
        outs = [bases[f].with_name(f"{bases[f].name}.{fmt}") for fmt in args.formats]
        if not args.overwrite and all(o.exists() for o in outs):
            print(f"Skip (already done; use --overwrite to redo): {f}")
            skipped += 1
        else:
            todo.append(f)

    if not todo:
        print("Nothing new to transcribe.")
        return 0

    engine = load_engine(args.engine, args.model, args.threads, args.device)
    print(f"Engine: {engine.name} | model: {args.model} | language: {args.lang or 'auto'} "
          f"| {len(todo)} file(s)\n")

    failed = []
    for i, f in enumerate(todo, 1):
        label = f"[{i}/{len(todo)}] {f.name}"
        print(label, flush=True)
        t0 = time.time()
        try:
            result = engine.transcribe(f, args.lang, args.prompt, not args.no_vad,
                                       make_progress(label))
            if sys.stdout.isatty():
                print("\r" + " " * (len(label) + 10) + "\r", end="")
            if not any(s.text.strip() for s in result.segments):
                print("  WARNING: no speech detected; writing empty transcript.")

            base = bases[f]
            written = []
            for fmt in args.formats:
                out = base.with_name(f"{base.name}.{fmt}")
                if fmt == "txt":
                    text = to_paragraphs(result.segments, args.pause, args.max_sentences)
                elif fmt == "srt":
                    text = to_srt(result.segments)
                else:
                    text = json.dumps({
                        "source": str(f.resolve()), "engine": engine.name,
                        "model": args.model, "language": result.language,
                        "duration": result.duration,
                        "segments": [asdict(s) for s in result.segments],
                    }, indent=2, ensure_ascii=False) + "\n"
                write_atomic(out, text)
                written.append(out)

            elapsed = time.time() - t0
            print(f"  done: audio {fmt_secs(result.duration)}, took {fmt_secs(elapsed)}, "
                  f"language {result.language}")
            for out in written:
                print(f"  -> {out}")
            if args.print_text and "txt" in args.formats:
                print("\n" + written[args.formats.index("txt")].read_text(encoding="utf-8"))
        except KeyboardInterrupt:
            print("\nInterrupted.")
            return 1
        except Exception as e:
            print(f"\n  FAILED: {type(e).__name__}: {e}", file=sys.stderr)
            failed.append(f)

    print(f"\nSummary: {len(todo) - len(failed)} transcribed, {skipped} skipped, "
          f"{len(failed)} failed.")
    for f in failed:
        print(f"  failed: {f}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
