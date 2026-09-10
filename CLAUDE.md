# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Git

Never commit, push, or create branches yourself. Leave changes in the working tree and let the user review and commit them.

Always write changes to the main worktree (`C:/coding_projects/home_automation`), never to a separate worktree, unless the user explicitly asks for one. `.claude/worktrees/` holds side worktrees for other branches — leave them alone.

## Running

`main.py` is started from inside `src/`, because modules import each other flat (`from bindings import ...`, `from config import ...`) with no package prefix:

```bash
cd src && python main.py
```

On the Pi it runs as a systemd user service; `scripts/print_output.sh` tails it:

```bash
journalctl --user -u home_automation.service -f
```

`src/config.py` is gitignored and must exist before anything imports. It holds plain module-level constants: `TAPO_EMAIL`, `TAPO_PASSWORD`, `KITCHEN_LIGHT_IP`, `BATHROOM_LIGHT_IP`, `LIVING_ROOM_LIGHT_IP`, `VIBE_LIGHT_IP`, `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`, `SPOTIFY_DEVICE_NAME`, `SPEAKERS_MAC_ADDRESS`, `XBOX_CONTROLLER_MAC_ADDRESS`, `DESKTOP_MAC_ADDRESS`, `PHONE_IP`, `ADB_PORT`, `ADB_PATH`. `OPENAI_API_KEY` comes separately from `.env` via `python-dotenv`, as do the Trello settings: `TRELLO_API_KEY`, `TRELLO_TOKEN`, one of `TRELLO_BOARD_ID` / `TRELLO_BOARD_NAME`, and optionally `TRELLO_DEFAULT_LIST` / `TRELLO_DONE_LIST`. Run `python src/trello_board.py` to print the authorize URL for the token and to list your board ids. The Bring! shopping list needs `BRING_EMAIL` and `BRING_PASSWORD`, plus `BRING_LIST_UUID` (or `BRING_LIST_NAME`) if the account has more than one list; `python src/shopping_list.py` signs in and prints the list uuids.

## Target platform

This runs on a Raspberry Pi under Linux and depends on it: `aplay` for TTS playback, BlueZ over D-Bus for Bluetooth and controller battery, `adb` for the phone, the `wakeonlan` apt package for waking the desktop, passwordless sudo for `reboot` (run `scripts/allow_reboot.sh` once to grant it -- do not assume the user already has it; `sudo -n` is used so a password prompt fails fast instead of hanging a service with no tty, and `System.reboot` then falls back to a plain `systemctl reboot`, which works only where logind's polkit treats the session as active), ALSA/pygame for audio. Most modules cannot be imported on a Windows dev machine — expect to reason about them statically or test on the Pi. `requirements.txt` has drifted before; treat imports, not that file, as the dependency list. The realtime voice pathway needs `websockets`, which nothing else in the project uses.

## Architecture

Three input surfaces — Xbox controller, terminal stdin, and voice — all resolve to the same `Action` objects and run through one handler. `main.py` builds every hardware wrapper, hands them to `bindings.build_actions()`, and runs `controller.run()`, `stdin_reader()`, and `voice.run()` concurrently under `asyncio.gather`.

`src/bindings.py` is the single registry and the file to edit for almost any behaviour change. An `Action` wraps a bound method plus three optional fields:

- `say` — `None` for silent, a string key into `SoundPlayer.PHRASES` for a canned clip, or `True` to speak the function's return value.
- `desc` — the description shown to the LLM. **An action with no `desc` is invisible to voice** and can only fire from a button or the terminal. This is deliberate for things like `exit`, the controller mode switches, and the four `play_song_N` D-pad favourites that `play_song` supersedes for voice.
- `params` — `None` for a no-argument action, or a dict of JSON-schema properties the LLM fills in (every key is emitted as required). `run_action` splats them in as keyword arguments, so they must match the method's parameter names. `play_song` is the one user: it takes any `song_name`. Buttons and terminal keywords never supply arguments — they use `partial`-bound ones instead — so `run_action(action, sound)` still works unchanged.

Adding a capability means writing a method on a wrapper class and referencing it in `build_actions()`. There is no keyword to invent and no dispatcher to extend: `build_command_map` (terminal words), `build_button_maps` (controller), `build_voice_tools` (chat-completion function schemas) and `build_realtime_tools` (the same schemas flattened for the Realtime API) all derive from that one dict. `run_action` plays the click sound, awaits the result if it is a coroutine, then speaks — and swallows exceptions so one bad action cannot kill the loop. It **returns** the result (or an `Error: ...` string), which is what lets the realtime pathway feed a real battery level or Trello list back to the model instead of letting it guess; `speak=False` suppresses the canned clip for callers that would otherwise answer twice.

Controller bindings are layered: `build_button_maps` merges a `common` map (left-stick directions switch modes) under each mode's own map, so a mode can override a common button. The four modes are lights, bluetooth, phone, and misc — misc holds A for `stop_timer_alarm` and B for `toggle_voice_mode`.

### Voice pipeline

`src/voice/` holds two pathways behind one facade. **`VoiceAssistant` in `voice/assistant.py` is the only one `main.py` builds**; it owns the shared mic, the wake-word model and the mode, and runs exactly one backend at a time:

- `voice/classic.py` — the original turn-based pipeline. Wake word → record until 1.5 s of silence → Whisper → `gpt-4o-mini` with the generated tool schemas → a tool call or a spoken reply via OpenAI TTS and `aplay`. One request per utterance, no history.
- `voice/realtime.py` — a live GA Realtime API session over a websocket. Still wake-word gated (an open session bills and would otherwise answer the television), but once open you can keep talking without repeating the wake word. It closes on `IDLE_TIMEOUT` and hard-stops at `MAX_SESSION`.

An ESP32 streams mic audio over UDP to port 5005; the assistant expects 16 kHz mono 16-bit PCM. The sketch that sends it is `arduino_code/wifi_mic_i2s/`, which is gitignored — the checked-in `mic_i2s` writes to serial at 8 kHz instead, so don't read it as the live source. **Only one socket can bind 5005**, so `voice/mic_stream.py` owns it for both backends: a daemon thread reassembles 80 ms frames (skipping the 4-byte sequence header) into a bounded queue, offering a blocking `next_frame()` for worker threads and a cancellable `next_frame_async()`. That cancellability is what makes a mode switch safe — a bare blocking `get()` in an executor would outlive cancellation and steal frames from the incoming backend.

`src/wakeword.py` loads **only** the "hey jarvis" model and scores against it. openWakeWord's `Model()` with no arguments loads every pretrained model — alexa, hey mycroft, hey rhasspy, timers — so taking `max()` over all predictions makes any of them a wake word. Go through `wakeword.load_model()` / `wakeword.score()` rather than constructing `Model()` directly. The facade loads it once and hands the same instance to both backends.

**Switching modes is a request, not an act.** `set_mode` / `toggle_mode` are synchronous: they set a pending mode, fire an `asyncio.Event` and return. They have to be, because in realtime mode the switch arrives as a *tool call running inside the backend being torn down* — tearing it down inline would cancel the coroutine trying to report the result. The supervisor loop in `assistant.py` does the teardown instead, and uses `asyncio.wait` rather than `await task` so a cancelled backend cannot take the supervisor down with it. The classic backend additionally carries a `_stop` flag, because its blocking half runs in an executor thread that outlives the cancelled coroutine and would otherwise fire actions up to 15 s after the switch.

Realtime specifics worth knowing before editing `realtime.py`: the API only accepts pcm16 at **24 kHz**, so mic frames are resampled 16k→24k on the way out; audio comes back at 24 kHz and is streamed into a long-lived `aplay -t raw` over stdin from a worker thread, because the model generates faster than real time and ALSA back-pressure would otherwise stall the receive loop and every function call with it. The pathway is deliberately **half duplex** — the mic is an ESP32 across the room from the speaker with no echo cancellation anywhere, so `_pump_mic` stops sending while the assistant is talking. That costs barge-in; it also stops the assistant interrupting itself. `SPEAK_ACTION_RESULTS` controls whether an action still plays its canned clip in realtime mode (on by default, so you get the instant "Kitchen on" you get everywhere else).

Tool results come back to the model as `function_call_output` items sent at `response.done`, followed by one `response.create`. Calls are collected from both `response.function_call_arguments.done` and any `function_call` items in `response.done`, deduplicated by `call_id` so neither path can fire an action twice.

### Hardware wrappers

`TapoController` keeps `night_mode` and `brightness` as its own state and re-applies them on every turn-on, since the bulbs do not remember. Every device call goes through `_with_reconnect`, which retries once with a fresh handle because Tapo sessions expire. Brightness changes skip lights that are off — `set_brightness` would otherwise wake them.

`Bluetooth` and `XboxControllerBattery` talk to `org.bluez` over D-Bus with `jeepney` rather than scraping `bluetoothctl`. The controller's battery is on BlueZ's Battery1 interface, not the joystick node, so it reads even when pygame sees no joystick.

`ShoppingList` (`src/shopping_list.py`) talks to Bring! through the unofficial `bring-api` package. It is the one wrapper that is async all the way down -- `TrelloBoard` pushes synchronous `py-trello` into `asyncio.to_thread`, but bring-api is aiohttp-native, so it needs no worker thread. It owns an `aiohttp.ClientSession` built lazily on the running loop, because `Bring.__init__` calls `asyncio.get_running_loop()` and a session belongs to the loop that created it; `main.py` closes it in a `finally`. Items carry a name and a separate `specification` -- the amount has to go in the latter or Bring cannot match the product to its catalogue tile.

`SoundPlayer` pre-generates any missing clip in `PHRASES` with gTTS on first run into `resources/speech/` (gitignored) and can speak arbitrary text via `say_text`, which needs network access.

## Tests

`test/` holds standalone hardware diagnostics, not a pytest suite — each is run directly and most need the Pi and live hardware:

```bash
python test/test_wake.py              # print wake word detections from the UDP stream
python test/test_controller_battery.py
python test/test_esp32_wifi.py
```

`src/debug_buttons.py` prints raw pygame button, axis, and hat numbers — use it when controller indices in `XboxController.BUTTON_MAPPING` need remapping.
