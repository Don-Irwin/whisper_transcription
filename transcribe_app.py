#!/usr/bin/env python3
"""Live dictation: transcribe speech from the Mac's microphone as you talk.

Opens a small window with a "Transcribe" toggle button and a text box. While
the toggle is on, audio from a system sound input (the macOS default input
unless you pick one) is cut into utterances at natural pauses, and each one is
transcribed with faster-whisper as soon as you stop speaking. The text appears
in the text box and is appended to a session file at the same moment, so the
file always matches the box and nothing is lost if the program is interrupted.

The window is a local web page served on 127.0.0.1 only (Python's standard
library; this venv's Python has no Tk). It opens as a standalone Chrome/Edge
app window when one of those is installed, otherwise in the default browser.
Closing the window quits the app. Use --no-gui for the terminal-only mode.

Audio is captured through the FFmpeg libraries bundled with PyAV (macOS
AVFoundation), which bootstrap.sh already installs: no extra packages.

Usage (normally through transcribe.sh, which picks the project's Python):
    ./transcribe.sh                      # open the dictation window
    ./transcribe.sh --dictate --list-devices
    ./transcribe.sh --dictate --input "MacBook Air Microphone" --model small
    ./transcribe.sh --dictate --no-gui   # terminal only, starts listening at once
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import queue
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

os.environ.setdefault("CT2_VERBOSE", "-1")

import numpy as np

from transcribe import MODELS, FasterWhisperEngine, fmt_secs

RATE = 16_000                  # Whisper's sample rate
BLOCK = int(RATE * 0.03)       # 30 ms analysis blocks
PREROLL_S = 0.3                # audio kept from before speech starts
MIN_SPEECH_S = 0.3             # shorter blips are ignored
CALIBRATE_S = 0.6              # initial noise-floor measurement

PERMISSION_HINT = ("Waiting for the microphone. If macOS shows a permission prompt, allow it. "
                   "If nothing appears: System Settings > Privacy & Security > Microphone, "
                   "enable your terminal app (Terminal, iTerm or VS Code), then restart it.")


# --------------------------------------------------------------------------
# Devices
# --------------------------------------------------------------------------

def list_audio_devices() -> list[tuple[int, str]]:
    """Return [(index, name)] of AVFoundation audio inputs.

    FFmpeg only reports devices through its log, so capture the log lines."""
    import av
    import av.logging

    devices, in_audio = [], False
    old = av.logging.get_level()
    av.logging.set_level(av.logging.INFO)
    try:
        with av.logging.Capture(local=False) as logs:
            try:
                av.open("", format="avfoundation", options={"list_devices": "true"})
            except Exception:
                pass  # listing always "fails" after printing
    finally:
        av.logging.set_level(old)
    for _level, _name, msg in logs:
        msg = msg.strip()
        if msg.startswith("AVFoundation audio devices"):
            in_audio = True
        elif msg.startswith("AVFoundation video devices"):
            in_audio = False
        elif in_audio:
            m = re.match(r"\[(\d+)\]\s+(.*)", msg)
            if m:
                devices.append((int(m.group(1)), m.group(2)))
    return devices


def resolve_device(choice: str) -> tuple[str, str]:
    """Map --input (default / index / part of a name) to (avfoundation spec, label)."""
    if choice.lower() == "default":
        return ":default", "system default input"
    devices = list_audio_devices()
    if choice.isdigit():
        for i, name in devices:
            if i == int(choice):
                return f":{i}", name
        raise SystemExit(f"ERROR: no audio input #{choice}. Use --list-devices.")
    matches = [(i, n) for i, n in devices if choice.lower() in n.lower()]
    if len(matches) == 1:
        return f":{matches[0][0]}", matches[0][1]
    if not matches:
        raise SystemExit(f"ERROR: no audio input matching '{choice}'. Use --list-devices.")
    names = ", ".join(f"[{i}] {n}" for i, n in matches)
    raise SystemExit(f"ERROR: '{choice}' matches several inputs: {names}. Be more specific.")


# --------------------------------------------------------------------------
# Capture
# --------------------------------------------------------------------------

class Microphone:
    """Reads the device on a background thread and queues 16 kHz mono float32
    arrays, so audio keeps being captured while the model is busy. A None in
    the queue marks the end of the stream."""

    def __init__(self, spec: str):
        self.spec = spec
        self.q: queue.Queue = queue.Queue()
        self.stop = threading.Event()
        self.error: BaseException | None = None
        self.opened = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self, on_wait=None) -> bool:
        """Open the device. Returns False if close() was called before it opened.
        Opening blocks while macOS asks for microphone permission."""
        self.thread.start()
        t0, hinted = time.time(), False
        while not self.opened.wait(0.2):
            if self.error is not None:
                raise RuntimeError(f"Could not open audio input {self.spec}: {self.error}. "
                                   "Check --list-devices and the Microphone privacy setting.")
            if self.stop.is_set():
                return False
            if not hinted and time.time() - t0 > 6:
                hinted = True
                if on_wait:
                    on_wait()
        return True

    def _run(self):
        import av
        try:
            container = av.open(self.spec, format="avfoundation")
        except BaseException as e:
            self.error = e
            self.q.put(None)
            return
        self.opened.set()
        resampler = av.AudioResampler(format="flt", layout="mono", rate=RATE)
        stream = container.streams.audio[0]
        try:
            # A live device often has no packet ready yet. FFmpeg then returns
            # EAGAIN ("try again"), which PyAV raises as BlockingIOError (errno
            # 35 on macOS). That is normal, not an error: wait briefly and read
            # again. demux() has to be restarted after the exception.
            while not self.stop.is_set():
                try:
                    for packet in container.demux(stream):
                        if self.stop.is_set():
                            break
                        for frame in packet.decode():
                            frame.pts = None  # live input: resampler keeps its own clock
                            for out in resampler.resample(frame):
                                self.q.put(out.to_ndarray().reshape(-1)
                                           .astype(np.float32, copy=False))
                    else:
                        break  # the device stream ended
                except OSError as e:
                    if e.errno != errno.EAGAIN:
                        raise
                    time.sleep(0.005)
        except BaseException as e:
            if not self.stop.is_set():
                self.error = e
        finally:
            container.close()
            self.q.put(None)

    def close(self):
        self.stop.set()


# --------------------------------------------------------------------------
# Utterance detection (simple adaptive energy gate)
# --------------------------------------------------------------------------

class Segmenter:
    """Splits the stream into utterances: speech followed by `silence` seconds
    of quiet, or `max_len` seconds of continuous speech."""

    def __init__(self, silence: float, max_len: float, threshold: float | None):
        self.silence_blocks = max(1, int(silence * RATE / BLOCK))
        self.max_samples = int(max_len * RATE)
        self.fixed = threshold
        self.noise: float | None = None
        self.calib: list[float] = []
        self.pending = np.zeros(0, np.float32)
        self.preroll: list[np.ndarray] = []
        self.cur: list[np.ndarray] = []
        self.speech_blocks = 0
        self.quiet_blocks = 0
        self.samples_seen = 0
        self.utt_start = 0.0
        self.level = 0.0

    @property
    def active(self) -> bool:
        return bool(self.cur)

    def current_len(self) -> float:
        return sum(len(b) for b in self.cur) / RATE

    def threshold(self) -> float:
        if self.fixed is not None:
            return self.fixed
        return max(0.006, (self.noise or 0.0) * 3.0)

    def feed(self, audio: np.ndarray):
        """Yield (start_seconds, samples) for every finished utterance."""
        self.pending = np.concatenate([self.pending, audio])
        while len(self.pending) >= BLOCK:
            block, self.pending = self.pending[:BLOCK], self.pending[BLOCK:]
            self.samples_seen += BLOCK
            rms = float(np.sqrt(np.mean(block * block)))
            self.level = rms
            if self.fixed is None and self.noise is None:
                self.calib.append(rms)
                if len(self.calib) * BLOCK >= CALIBRATE_S * RATE:
                    self.noise = float(np.median(self.calib))
                continue
            loud = rms > self.threshold()
            if not self.cur:
                if self.fixed is None and not loud:  # track the noise floor slowly
                    self.noise = 0.95 * self.noise + 0.05 * rms
                self.preroll.append(block)
                self.preroll = self.preroll[-int(PREROLL_S * RATE / BLOCK):]
                if loud:
                    self.utt_start = (self.samples_seen - BLOCK) / RATE - PREROLL_S
                    self.cur, self.preroll = self.preroll, []
                    self.speech_blocks, self.quiet_blocks = 1, 0
                continue
            self.cur.append(block)
            if loud:
                self.speech_blocks += 1
                self.quiet_blocks = 0
            else:
                self.quiet_blocks += 1
            n = sum(len(b) for b in self.cur)
            if self.quiet_blocks >= self.silence_blocks or n >= self.max_samples:
                out = self.flush()
                if out is not None:
                    yield out

    def flush(self):
        cur, self.cur = self.cur, []
        speech = self.speech_blocks * BLOCK / RATE
        self.speech_blocks = self.quiet_blocks = 0
        if not cur or speech < MIN_SPEECH_S:
            return None
        return max(0.0, self.utt_start), np.concatenate(cur)



# --------------------------------------------------------------------------
# Engines: faster-whisper (main), openai-whisper (automatic backup)
# --------------------------------------------------------------------------

class LiveEngine:
    """Transcribes in-memory audio arrays. Tries faster-whisper first and falls
    back to openai-whisper if it is missing or won't load (--engine auto).
    openai-whisper takes the array directly, so it needs no ffmpeg here."""

    def __init__(self, choice: str, model: str, threads: int):
        order = {"auto": ["faster", "openai"], "faster": ["faster"], "openai": ["openai"]}[choice]
        errors = []
        for name in order:
            try:
                if name == "faster":
                    print(f"Loading faster-whisper model '{model}' (first use downloads it)...",
                          flush=True)
                    self.model = FasterWhisperEngine(model, threads).model
                    self.name = "faster-whisper"
                else:
                    print(f"Loading openai-whisper model '{model}' (first use downloads it)...",
                          flush=True)
                    import whisper
                    try:
                        import torch
                        torch.set_num_threads(threads)
                    except Exception:
                        pass
                    self.model = whisper.load_model(model, device="cpu")
                    self.name = "openai-whisper"
                return
            except ImportError:
                errors.append(f"{name} engine is not installed")
            except Exception as e:
                errors.append(f"{name} engine: {e}")
            if choice == "auto" and name == "faster":
                print("  faster-whisper unavailable; trying the openai-whisper backup...",
                      flush=True)
        raise SystemExit("ERROR: no usable transcription engine.\n  " + "\n  ".join(errors)
                         + "\n  Run ./bootstrap.sh to install.")

    def transcribe(self, audio: np.ndarray, language: str | None, prompt: str | None) -> str:
        if self.name == "faster-whisper":
            segments, _info = self.model.transcribe(audio, language=language,
                                                    initial_prompt=prompt,
                                                    vad_filter=True, beam_size=5)
            return " ".join(s.text.strip() for s in segments).strip()
        r = self.model.transcribe(audio, language=language, initial_prompt=prompt,
                                  fp16=False, verbose=None, condition_on_previous_text=False)
        return r.get("text", "").strip()


# --------------------------------------------------------------------------
# Dictation session (shared by the window and the terminal mode)
# --------------------------------------------------------------------------

# Whisper sometimes invents these on near-silent clips.
HALLUCINATIONS = {"thank you.", "thanks for watching!", "thank you for watching.",
                  "you", "bye.", "thanks for watching.", "."}


class Dictation:
    """Turns the microphone on and off (toggle) and accumulates the transcript.

    `self.text` is exactly what has been written to the session file, so the
    text box and the file always match. The file is created when the first
    words arrive; every on/off period of one run goes into the same file, each
    starting a new paragraph."""

    def __init__(self, args, model, spec: str, out_path: Path | None, on_text=None):
        self.args, self.model, self.spec = args, model, spec
        self.out_path = out_path
        self.on_text = on_text          # callback(new_text) for terminal printing
        self.lock = threading.Lock()
        self.text = ""
        self.state = "idle"             # idle | starting | recording | stopping
        self.message = ""               # error or permission hint, shown under the button
        self.busy = False               # model is transcribing
        self.mic: Microphone | None = None
        self.seg: Segmenter | None = None
        self.worker: threading.Thread | None = None
        self.file = None
        self.new_para = False
        self.last_end: float | None = None

    # -- control ---------------------------------------------------------
    def toggle(self):
        if self.state == "idle":
            self.start()
        elif self.state in ("starting", "recording"):
            self.stop()

    def start(self):
        with self.lock:
            if self.state != "idle":
                return
            self.state, self.message = "starting", ""
            self.new_para = bool(self.text)
            self.last_end = None
            self.mic = Microphone(self.spec)
            self.worker = threading.Thread(target=self._run, daemon=True)
            self.worker.start()

    def stop(self):
        with self.lock:
            if self.state not in ("starting", "recording"):
                return
            self.state = "stopping"
            self.mic.close()

    def wait(self):
        if self.worker:
            self.worker.join()

    def close(self):
        self.stop()
        self.wait()
        if self.file:
            self.file.write("\n")
            self.file.close()
            self.file = None

    # -- status ----------------------------------------------------------
    def snapshot(self) -> dict:
        seg, state = self.seg, self.state
        level = 0.0
        if state == "starting":
            status = "Opening microphone..."
        elif state == "stopping":
            status = "Finishing the last words..."
        elif state == "recording" and seg is not None:
            level = min(1.0, seg.level / max(seg.threshold(), 1e-6) / 2)
            if self.busy:
                status = "Transcribing..."
            elif seg.noise is None and seg.fixed is None:
                status = "Measuring background noise..."
            elif seg.active:
                status = f"Hearing speech ({seg.current_len():.1f}s)"
            else:
                status = "Listening"
        else:
            status = "Ready" if not self.text else "Paused"
        return {"state": state, "status": status, "message": self.message,
                "level": round(level, 3),
                "file": str(self.out_path) if self.out_path and self.out_path.exists() else ""}

    # -- worker ----------------------------------------------------------
    def _run(self):
        mic = self.mic
        try:
            def hint():
                self.message = PERMISSION_HINT
                print("\n" + PERMISSION_HINT, flush=True)
            if not mic.start(on_wait=hint):
                self.state = "idle"
                return
            self.message = ""
            with self.lock:
                if self.state == "starting":
                    self.state = "recording"
            self.seg = Segmenter(self.args.silence, self.args.max_utterance, self.args.threshold)
            while True:
                audio = mic.q.get()
                if audio is None:
                    break
                for start, utt in self.seg.feed(audio):
                    self._transcribe(start, utt)
            last = self.seg.flush()
            if last is not None:
                self._transcribe(*last)
            if mic.error:
                self.message = f"Audio input stopped: {mic.error}"
        except Exception as e:
            self.message = str(e)
            mic.close()
        finally:
            self.seg = None
            self.busy = False
            self.state = "idle"

    def _transcribe(self, start: float, audio: np.ndarray):
        self.busy = True
        try:
            context = self.text[-200:]
            prompt = " ".join(x for x in (self.args.prompt, context) if x) or None
            words = self.model.transcribe(audio, self.args.lang, prompt)
        finally:
            self.busy = False
        if not words or words.lower() in HALLUCINATIONS:
            return
        if self.last_end is not None and start - self.last_end >= self.args.pause:
            self.new_para = True
        self.last_end = start + len(audio) / RATE
        if not self.text:
            chunk = words
        elif self.new_para:
            chunk = "\n\n" + words
        else:
            chunk = " " + words
        self.new_para = False
        if self.out_path:
            if self.file is None:
                self.out_path.parent.mkdir(parents=True, exist_ok=True)
                self.file = self.out_path.open("a", encoding="utf-8")
            self.file.write(chunk)
            self.file.flush()
        with self.lock:
            self.text += chunk
        if self.on_text:
            self.on_text(chunk)


# --------------------------------------------------------------------------
# Window (local web page)
# --------------------------------------------------------------------------

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Live Dictation</title>
<style>
:root {
  --bg: #f6f6f4; --panel: #ffffff; --text: #1d1d1f; --muted: #6e6e73;
  --border: #d9d9de; --accent: #2f6fdf; --accent-text: #ffffff;
  --rec: #d93a2b; --rec-soft: #fbe3e0; --meter: #34a853;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #161618; --panel: #1f1f22; --text: #f2f2f4; --muted: #9a9aa2;
    --border: #36363c; --accent: #5b8ff0; --accent-text: #ffffff;
    --rec: #ff5b4a; --rec-soft: #3a1f1c; --meter: #4cc36c;
  }
}
* { box-sizing: border-box; }
html, body { height: 100%; margin: 0; }
body {
  background: var(--bg); color: var(--text);
  font: 15px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  display: flex; flex-direction: column; padding: 16px; gap: 12px;
}
header { display: flex; align-items: center; gap: 14px; flex-wrap: wrap; }
#toggle {
  font: inherit; font-weight: 600; font-size: 16px;
  display: inline-flex; align-items: center; gap: 10px;
  padding: 10px 22px; border-radius: 999px; cursor: pointer;
  border: 2px solid var(--accent); background: var(--accent); color: var(--accent-text);
}
#toggle .dot { width: 10px; height: 10px; border-radius: 50%; background: currentColor; opacity: .55; }
#toggle[aria-pressed="true"] { background: var(--rec-soft); color: var(--rec); border-color: var(--rec); }
#toggle[aria-pressed="true"] .dot { opacity: 1; animation: pulse 1.2s ease-in-out infinite; }
#toggle:disabled { opacity: .6; cursor: progress; }
#toggle:focus-visible, button:focus-visible { outline: 3px solid var(--accent); outline-offset: 2px; }
@keyframes pulse { 50% { opacity: .25; } }
.status { display: flex; flex-direction: column; gap: 4px; min-width: 180px; flex: 1; }
#status { font-weight: 500; }
.meter { height: 5px; border-radius: 3px; background: var(--border); overflow: hidden; max-width: 260px; }
#level { height: 100%; width: 0; background: var(--meter); transition: width .12s linear; }
#message { color: var(--rec); font-size: 13px; }
#message:empty { display: none; }
textarea {
  flex: 1; width: 100%; resize: none; padding: 14px 16px;
  border: 1px solid var(--border); border-radius: 10px;
  background: var(--panel); color: var(--text);
  font: 17px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}
textarea:focus { outline: 2px solid var(--accent); outline-offset: -1px; }
footer { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; color: var(--muted); font-size: 13px; }
#file { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
footer button {
  font: inherit; padding: 5px 12px; border-radius: 6px; cursor: pointer;
  border: 1px solid var(--border); background: var(--panel); color: var(--text);
}
footer button:disabled { opacity: .5; cursor: default; }
#offline { display: none; color: var(--rec); }
body.offline #offline { display: inline; }
body.offline #toggle { opacity: .5; pointer-events: none; }
</style>
</head>
<body>
<header>
  <button id="toggle" type="button" aria-pressed="false" title="Start or stop transcribing (Space)">
    <span class="dot" aria-hidden="true"></span>Transcribe
  </button>
  <div class="status">
    <span id="status" role="status">Connecting...</span>
    <div class="meter" aria-hidden="true"><div id="level"></div></div>
    <span id="message"></span>
  </div>
</header>
<textarea id="text" readonly aria-label="Transcript"
  placeholder="Press Transcribe and start speaking. Text appears here after each pause."></textarea>
<footer>
  <span id="file">Transcript file is created when the first words arrive.</span>
  <span id="offline">App stopped. You can close this window.</span>
  <button id="copy" type="button">Copy all</button>
  <button id="reveal" type="button" disabled>Show file</button>
  <button id="quit" type="button">Quit</button>
</footer>
<script>
const token = new URLSearchParams(location.search).get("t");
const $ = (id) => document.getElementById(id);
const box = $("text"), toggle = $("toggle");

function post(path) {
  return fetch(path + "?t=" + encodeURIComponent(token), { method: "POST" }).catch(() => {});
}

const events = new EventSource("/events?t=" + encodeURIComponent(token));
events.onmessage = (e) => {
  const s = JSON.parse(e.data);
  document.body.classList.remove("offline");
  if (s.reset) box.value = "";
  if (s.append) {
    const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
    box.value += s.append;
    if (atBottom) box.scrollTop = box.scrollHeight;
  }
  const on = s.state === "starting" || s.state === "recording";
  toggle.setAttribute("aria-pressed", String(on));
  toggle.disabled = s.state === "stopping";
  $("status").textContent = s.status;
  $("message").textContent = s.message;
  $("level").style.width = (s.level * 100).toFixed(0) + "%";
  $("file").textContent = s.file ? "Saving to " + s.file
                                 : "Transcript file is created when the first words arrive.";
  $("file").title = s.file;
  $("reveal").disabled = !s.file;
};
events.onerror = () => {
  if (events.readyState === EventSource.CLOSED) document.body.classList.add("offline");
  $("status").textContent = "Disconnected";
};

toggle.addEventListener("click", () => post("/toggle"));
document.addEventListener("keydown", (e) => {
  if (e.code === "Space" && e.target !== box && !e.target.closest("button")) {
    e.preventDefault(); post("/toggle");
  }
});
$("copy").addEventListener("click", async () => {
  try { await navigator.clipboard.writeText(box.value); }
  catch { box.select(); document.execCommand("copy"); }
  $("copy").textContent = "Copied";
  setTimeout(() => { $("copy").textContent = "Copy all"; }, 1200);
});
$("reveal").addEventListener("click", () => post("/reveal"));
$("quit").addEventListener("click", () => {
  events.close(); post("/quit");
  document.body.classList.add("offline");
  $("status").textContent = "Stopped";
});
</script>
</body>
</html>
"""


class UIServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, dictation: Dictation, token: str, port: int):
        super().__init__(("127.0.0.1", port), UIHandler)
        self.dictation = dictation
        self.token = token
        self.clients = 0
        self.ever_connected = False
        self.last_change = time.time()
        self.clients_lock = threading.Lock()
        self.quit_requested = threading.Event()

    def client_delta(self, d: int):
        with self.clients_lock:
            self.clients += d
            self.ever_connected = True
            self.last_change = time.time()


class UIHandler(BaseHTTPRequestHandler):
    server: UIServer

    def log_message(self, *a):
        pass

    def _authorized(self) -> tuple[bool, str]:
        u = urlparse(self.path)
        t = parse_qs(u.query).get("t", [""])[0]
        return secrets.compare_digest(t, self.server.token), u.path

    def _send(self, code: int, body: bytes = b"", ctype: str = "text/plain"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        ok, path = self._authorized()
        if not ok:
            return self._send(403, b"forbidden")
        if path == "/":
            return self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        if path == "/events":
            return self._events()
        self._send(404, b"not found")

    def do_POST(self):
        ok, path = self._authorized()
        if not ok:
            return self._send(403, b"forbidden")
        d = self.server.dictation
        if path == "/toggle":
            d.toggle()
        elif path == "/reveal":
            if d.out_path and d.out_path.exists():
                subprocess.run(["open", "-R", str(d.out_path)], check=False)
        elif path == "/quit":
            self.server.quit_requested.set()
        else:
            return self._send(404, b"not found")
        self._send(204)

    def _events(self):
        """Server-sent events: pushes status ~6x a second plus new transcript
        text. The event id is how much text the page has, so an automatic
        reconnect resumes without duplicating or losing text."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        d = self.server.dictation
        try:
            sent = int(self.headers.get("Last-Event-ID", "0"))
        except ValueError:
            sent = 0
        reset = sent == 0 or sent > len(d.text)
        if reset:
            sent = 0
        self.server.client_delta(+1)
        try:
            while not self.server.quit_requested.is_set():
                text = d.text
                msg = d.snapshot()
                msg["append"] = text[sent:]
                if reset:
                    msg["reset"], reset = True, False
                sent = len(text)
                self.wfile.write(f"id: {sent}\ndata: {json.dumps(msg)}\n\n".encode("utf-8"))
                self.wfile.flush()
                time.sleep(0.15)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.server.client_delta(-1)


def open_window(url: str) -> str:
    """Open the page as a standalone app window if a Chromium browser is
    installed (no tabs or address bar), else in the default browser."""
    for app in ("Google Chrome", "Microsoft Edge", "Brave Browser", "Chromium"):
        for root in ("/Applications", str(Path.home() / "Applications")):
            if Path(root, f"{app}.app").exists():
                subprocess.Popen(["open", "-na", app, "--args", f"--app={url}",
                                  "--window-size=760,640"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return f"{app} app window"
    webbrowser.open(url)
    return "default browser"


def run_gui(args, dictation: Dictation, label: str) -> None:
    token = secrets.token_urlsafe(16)
    server = UIServer(dictation, token, args.port)
    url = f"http://127.0.0.1:{server.server_address[1]}/?t={token}"

    def watchdog():
        # Quit when the window has been closed (no connected page for a few
        # seconds; the grace period covers a reload), or on the Quit button.
        while True:
            if server.quit_requested.wait(0.5):
                break
            with server.clients_lock:
                idle = time.time() - server.last_change if server.clients == 0 else 0
                limit = 6 if server.ever_connected else 180
            if idle > limit:
                print("Window closed." if server.ever_connected
                      else "No window connected; giving up.", flush=True)
                server.quit_requested.set()
                break
        server.shutdown()

    threading.Thread(target=watchdog, daemon=True).start()
    print(f"\nInput: {label} | engine: {dictation.model.name} | model: {args.model} "
          f"| language: {args.lang or 'auto'}")
    if args.no_browser:
        print(f"Open this address in a browser: {url}")
    else:
        where = open_window(url)
        print(f"Opened the dictation window ({where}). If it didn't appear, open:\n  {url}")
    print("Close the window, press Quit, or press Ctrl+C here to exit.", flush=True)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.quit_requested.set()
        server.server_close()


def run_terminal(dictation: Dictation, label: str, args) -> None:
    tty = sys.stdout.isatty()
    print(f"\nListening on: {label} | engine: {dictation.model.name} | model: {args.model} "
          f"| language: {args.lang or 'auto'}")
    print("Speak now. Press Ctrl+C to stop.\n", flush=True)
    dictation.start()
    shown = ""
    try:
        while dictation.state != "idle" or dictation.worker.is_alive():
            if tty:
                s = dictation.snapshot()
                line = s["status"]
                if s["state"] == "recording":
                    line += " " + "▮" * int(s["level"] * 20)
                if line != shown:
                    print("\r\033[2K  " + line, end="", flush=True)
                    shown = line
            time.sleep(0.15)
    except KeyboardInterrupt:
        pass
    if tty:
        print("\r\033[2K", end="", flush=True)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Dictate from the microphone. Opens a window with a Transcribe toggle; "
                    "text appears in the window and in a file as you speak.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="examples:\n"
               "  ./transcribe.sh                                   # open the dictation window\n"
               "  ./transcribe.sh --dictate --list-devices\n"
               "  ./transcribe.sh --dictate -i 1 --model small\n"
               "  ./transcribe.sh --dictate -o ~/Transcripts/ --prompt 'Marianne, Berkeley'\n"
               "  ./transcribe.sh --dictate --no-gui --copy        # terminal only\n",
    )
    p.add_argument("--list-devices", action="store_true", help="show audio inputs and exit")
    p.add_argument("-i", "--input", default="default",
                   help="audio input: 'default' (macOS's current input), a number from "
                        "--list-devices, or part of its name (default: default)")
    p.add_argument("-m", "--model", default="base",
                   help=f"model size: {', '.join(MODELS)} (default: base)")
    p.add_argument("-l", "--language", default="en",
                   help="language code such as en, es, fr, or 'auto' (default: en)")
    p.add_argument("--prompt", help="hint text to steer spelling of names/jargon")
    p.add_argument("--engine", choices=("auto", "faster", "openai"), default="auto",
                   help="auto = faster-whisper, falling back to openai-whisper if it won't "
                        "load (default)")
    p.add_argument("-o", "--output-dir", type=Path, default=Path.cwd(),
                   help="folder for the session transcript (default: current folder)")
    p.add_argument("--no-save", action="store_true", help="don't write a transcript file")
    p.add_argument("--no-gui", action="store_true",
                   help="terminal only: start listening immediately, print text, Ctrl+C to stop")
    p.add_argument("--no-browser", action="store_true",
                   help="don't open the window; just print its address")
    p.add_argument("--port", type=int, default=0,
                   help="port for the window's local server (default: any free port)")
    p.add_argument("--copy", action="store_true",
                   help="copy the whole transcript to the clipboard when you quit")
    p.add_argument("--silence", type=float, default=0.8,
                   help="pause in seconds that ends an utterance (default: 0.8)")
    p.add_argument("--max-utterance", type=float, default=25.0,
                   help="transcribe after this many seconds even without a pause (default: 25)")
    p.add_argument("--pause", type=float, default=4.0,
                   help="pause in seconds that starts a new paragraph (default: 4)")
    p.add_argument("--threshold", type=float,
                   help="fixed speech level 0-1 instead of automatic calibration (try 0.01-0.03 "
                        "in a noisy room)")
    p.add_argument("--threads", type=int, default=os.cpu_count() or 4,
                   help="CPU threads (default: all cores)")
    args = p.parse_args(argv)
    args.lang = None if args.language.lower() == "auto" else args.language
    return args


def main(argv=None) -> int:
    args = parse_args(argv)

    if args.list_devices:
        devices = list_audio_devices()
        if not devices:
            print("No audio inputs found.")
            return 1
        print("Audio inputs (use the number or part of the name with --input):")
        for i, name in devices:
            print(f"  [{i}] {name}")
        return 0

    spec, label = resolve_device(args.input)
    out_path = None
    if not args.no_save:
        out_path = (args.output_dir.expanduser()
                    / f"dictation_{datetime.now():%Y%m%d_%H%M%S}.txt")

    model = LiveEngine(args.engine, args.model, args.threads)

    t_begin = time.time()
    if args.no_gui:
        def show(chunk: str):
            if sys.stdout.isatty():
                print("\r\033[2K", end="")
            print(chunk.lstrip(" "), end="\n", flush=True)
        dictation = Dictation(args, model, spec, out_path, on_text=show)
        run_terminal(dictation, label, args)
    else:
        dictation = Dictation(args, model, spec, out_path)
        run_gui(args, dictation, label)

    if dictation.state != "idle":
        print("Finishing the last words...", flush=True)
    try:
        dictation.close()
    except KeyboardInterrupt:
        pass
    if dictation.message:
        print(f"Note: {dictation.message}", file=sys.stderr)

    text = dictation.text.strip()
    print(f"\nStopped after {fmt_secs(time.time() - t_begin)}.")
    if out_path and out_path.exists():
        print(f"  -> {out_path}")
    elif out_path:
        print("  No speech captured; no file written.")
    if args.copy and text:
        if shutil.which("pbcopy"):
            subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=False)
            print("  Copied to clipboard.")
        else:
            print("  (pbcopy not found; not copied)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
