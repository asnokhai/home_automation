# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Git

Never commit, push, or create branches yourself. Leave changes in the working tree and let the user review and commit them.

## Running

`main.py` is started from inside `src/`, because modules import each other flat (`from bindings import ...`, `from config import ...`) with no package prefix:

```bash
cd src && python main.py
```

On the Pi it runs as a systemd user service; `scripts/print_output.sh` tails it:

```bash
journalctl --user -u home_automation.service -f
```

`src/config.py` is gitignored and must exist before anything imports. It holds plain module-level constants: `TAPO_EMAIL`, `TAPO_PASSWORD`, `KITCHEN_LIGHT_IP`, `BATHROOM_LIGHT_IP`, `LIVING_ROOM_LIGHT_IP`, `VIBE_LIGHT_IP`, `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`, `SPOTIFY_DEVICE_NAME`, `SPEAKERS_MAC_ADDRESS`, `XBOX_CONTROLLER_MAC_ADDRESS`, `PHONE_IP`, `ADB_PORT`, `ADB_PATH`. `OPENAI_API_KEY` comes separately from `.env` via `python-dotenv`.

## Target platform

This runs on a Raspberry Pi under Linux and depends on it: `aplay` for TTS playback, BlueZ over D-Bus for Bluetooth and controller battery, `adb` for the phone, ALSA/pygame for audio. Most modules cannot be imported on a Windows dev machine — expect to reason about them statically or test on the Pi. `requirements.txt` is stale (it omits `openai`, `openwakeword`, `numpy`, `jeepney`); treat imports, not that file, as the dependency list.

## Architecture

Three input surfaces — Xbox controller, terminal stdin, and voice — all resolve to the same `Action` objects and run through one handler. `main.py` builds every hardware wrapper, hands them to `bindings.build_actions()`, and runs `controller.run()`, `stdin_reader()`, and `voice.run()` concurrently under `asyncio.gather`.

`src/bindings.py` is the single registry and the file to edit for almost any behaviour change. An `Action` wraps a bound method plus two optional fields:

- `say` — `None` for silent, a string key into `SoundPlayer.PHRASES` for a canned clip, or `True` to speak the function's return value.
- `desc` — the description shown to the LLM. **An action with no `desc` is invisible to voice** and can only fire from a button or the terminal. This is deliberate for things like `exit` and controller mode switches.

Adding a capability means writing a method on a wrapper class and referencing it in `build_actions()`. There is no keyword to invent and no dispatcher to extend: `build_command_map` (terminal words), `build_button_maps` (controller), and `build_voice_tools` (OpenAI function schemas) all derive from that one dict. `run_action` plays the click sound, awaits the result if it is a coroutine, then speaks — and swallows exceptions so one bad action cannot kill the loop.

Controller bindings are layered: `build_button_maps` merges a `common` map (left-stick directions switch modes) under each mode's own map, so a mode can override a common button. The three modes are lights, bluetooth, and phone.

### Voice pipeline

An ESP32 streams mic audio over UDP to port 5005; the assistant expects 16 kHz mono 16-bit PCM. The sketch that sends it is `arduino_code/wifi_mic_i2s/`, which is gitignored — the checked-in `mic_i2s` writes to serial at 8 kHz instead, so don't read it as the live source. `VoiceAssistant._next_frame` reassembles 80 ms frames, skipping a 4-byte packet header.

`src/wakeword.py` loads **only** the "hey jarvis" model and scores against it. openWakeWord's `Model()` with no arguments loads every pretrained model — alexa, hey mycroft, hey rhasspy, timers — so taking `max()` over all predictions makes any of them a wake word. Go through `wakeword.load_model()` / `wakeword.score()` rather than constructing `Model()` directly.

After waking: record until 1.5 s of silence → Whisper transcription → `gpt-4o-mini` with the generated tool schemas → either a tool call (run the action) or a spoken reply via OpenAI TTS and `aplay`. The `busy` flag gates the loop and the buffer is cleared afterwards so the assistant does not hear its own playback.

### Hardware wrappers

`TapoController` keeps `night_mode` and `brightness` as its own state and re-applies them on every turn-on, since the bulbs do not remember. Every device call goes through `_with_reconnect`, which retries once with a fresh handle because Tapo sessions expire. Brightness changes skip lights that are off — `set_brightness` would otherwise wake them.

`Bluetooth` and `XboxControllerBattery` talk to `org.bluez` over D-Bus with `jeepney` rather than scraping `bluetoothctl`. The controller's battery is on BlueZ's Battery1 interface, not the joystick node, so it reads even when pygame sees no joystick.

`SoundPlayer` pre-generates any missing clip in `PHRASES` with gTTS on first run into `resources/speech/` (gitignored) and can speak arbitrary text via `say_text`, which needs network access.

## Tests

`test/` holds standalone hardware diagnostics, not a pytest suite — each is run directly and most need the Pi and live hardware:

```bash
python test/test_wake.py              # print wake word detections from the UDP stream
python test/test_controller_battery.py
python test/test_esp32_wifi.py
```

`src/debug_buttons.py` prints raw pygame button, axis, and hat numbers — use it when controller indices in `XboxController.BUTTON_MAPPING` need remapping.
