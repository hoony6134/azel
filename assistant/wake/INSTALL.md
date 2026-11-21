# 아젤 wake-word listener: install (Mac)

Listens on the default mic for the wake word, says "네?", records the request, sends it to the
voice server (`POST /v1/turn`), plays the reply, then keeps listening 6 s for a follow-up.

1. Config (optional, every key has a default): `cp wake.example.toml ~/.config/biseo/wake.toml`.
   The token must already be in `~/.config/biseo/token` (0600).
2. `env -u CONDA_PREFIX uv sync`, then try it in a terminal: `env -u CONDA_PREFIX uv run python biseo_wake.py`
   and say "hey Jarvis". The first run downloads the openWakeWord models (~10 MB) into `.venv`.
3. Load it with launchd from this directory. Logs: `~/Library/Logs/biseo-wake.log`.
   ```sh
   sed "s|__WAKE_DIR__|$PWD|g" xyz.scian.biseo-wake.plist > ~/Library/LaunchAgents/xyz.scian.biseo-wake.plist
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/xyz.scian.biseo-wake.plist
   ```
   Stop: `launchctl bootout gui/$(id -u)/xyz.scian.biseo-wake`.

## Microphone permission (do this once, right after step 3)

Mic access on macOS belongs to the program that asks, not to you: Terminal having access does
not cover the launchd job. The job is started as `uv`, so within a few seconds of loading it
macOS shows a dialog like:

> **"uv" would like to access the microphone.**  [Don't Allow] [Allow]

(On some macOS versions it names `python3.11` instead.) Click **Allow**. If you miss it or click
Don't Allow, open **System Settings → Privacy & Security → Microphone**, turn on the switch next
to `uv` (or `python3.11`), then restart the job:
`launchctl kickstart -k gui/$(id -u)/xyz.scian.biseo-wake`.

Without permission macOS gives the process silence instead of an error. The listener notices and
writes `WARNING: microphone is silent …` to `~/Library/Logs/biseo-wake.log` about 3 s after start.
If no dialog ever appears and nothing is listed under Microphone, reset the entry and restart the
job to get the dialog again: `tccutil reset Microphone`, then the kickstart command above
(this resets mic permission for all apps; each asks again once).

## Switching to the Korean wake word

When `azel.onnx` is trained, put it at `~/.config/biseo/azel.onnx`, set
`model = "~/.config/biseo/azel.onnx"` (and tune `threshold`) in `wake.toml`, and run the
kickstart command. Check a clip first: `uv run python biseo_wake.py --score clip.wav`.

## Wake log

Every detection is one JSON line in `~/.local/state/biseo/wake.jsonl`:
`{"time","model","score","request","transcript"}` (`request: false` = likely false wake).
No audio is kept unless `save_audio = true`, which stores ~2 s around each detection in
`~/.local/state/biseo/wake-audio/`.

Tests: `env -u CONDA_PREFIX uv run pytest`.
