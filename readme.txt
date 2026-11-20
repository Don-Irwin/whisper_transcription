SETUP AND TEST FROM SCRATCH
===========================

Tip: use the macOS Terminal app, not the VS Code terminal. The microphone
permission is then given to Terminal.


1. Set up

   cd /System/Volumes/Data/data/projects/whisper_transcription
   ./bootstrap.sh --clean

   Use "./bootstrap.sh" and "./transcribe.sh". (". bootstrap.sh" also works
   now: the scripts notice and run separately, so your terminal stays open.)

   Takes a few minutes (downloads about 1.5 GB). It should end with
   "Setup complete." If it also prints "Set up with these limitations",
   read the list: something optional is missing, but transcription works.


2. Test a file (try both engines)

   /usr/bin/say -o /tmp/test.aiff "This is a test of the transcription setup."
   ./transcribe.sh /tmp/test.aiff --print --overwrite
   ./transcribe.sh /tmp/test.aiff --print --overwrite --engine openai

   You should see the sentence printed both times.


3. Test live dictation

   ./transcribe.sh

   - A window opens with a Transcribe button and a text box.
   - Click Transcribe. If macOS asks to use the microphone, click OK.
   - Say a sentence, then pause. The text should appear in the box.
   - Click "Show file". The file should match the text box.
   - Click Quit (or close the window).


ENGINES

   Bootstrap always installs both engines (and FFmpeg), so it works either way.

   faster-whisper   Main engine. Does ALL live dictation, and files by default.
   openai-whisper   Backup (OpenAI's original, slower). Used automatically if
                    faster-whisper won't load, or when you add --engine openai.

   The terminal shows which engine is running, e.g. "engine: faster-whisper".

   FFmpeg: the backup engine needs it to read files. Bootstrap uses Homebrew's
   ffmpeg and also keeps its own copy in venv/bin, so files work with both
   engines even without Homebrew.


IF SOMETHING GOES WRONG

   Stuck on "Waiting for the microphone"
       System Settings > Privacy & Security > Microphone: turn on Terminal,
       then quit and reopen Terminal.

   No window opens
       Paste the http://127.0.0.1 address shown in the terminal into a browser.

   Wrong microphone
       ./transcribe.sh --dictate --list-devices
       ./transcribe.sh --dictate --input "MacBook Air"

   Never stops "Hearing speech" (noisy room)
       ./transcribe.sh --dictate --threshold 0.02

   "openai-whisper needs the ffmpeg command"
       Run ./bootstrap.sh again.

   "VS Code was prevented from modifying apps" notification
       Don't click Allow. Let Claude know.
