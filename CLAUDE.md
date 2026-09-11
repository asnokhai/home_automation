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

Three input surfaces — Xbox controller, terminal stdin, and voice — all resolve to the same `Action` objects and run through one handler. `main.py` builds every hardware wrapper, hands them to `bindings.build_actions()`, and runs `controller.run()`, `stdin_reader()`, and `voice.run()` concurrently under `asyncio.gather`, alongside two background watchers — `phone.watch()` and `house.watch()`. That gather has no `return_exceptions`, so **a watcher that lets an exception escape kills the whole assistant**; both swallow their own, re-raising `CancelledError` so shutdown still works.

A fourth input surface has no code in it at all: the kitchen wall switch. See `House` below.

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

`TapoController` keeps `night_mode` and `brightness` as its own state and re-applies them on every turn-on, since the bulbs do not remember. Every device call goes through `_with_reconnect`. Brightness changes skip lights that are off — `set_brightness` would otherwise wake them.

**Round trips are the latency budget.** One L530 answers in ~320ms, so how a button feels is almost entirely how many requests one press makes. Three rules keep that at one:

- **Batch with the builder.** `device.set().color_temperature(k).brightness(b).send(device)` applies every property in a single request; sending them separately is a round trip each. Note that `brightness`, `color_temperature` and `hue_saturation` all turn the bulb on by themselves unless `.off()` is in the same batch — an explicit `on()` is a wasted request, not a safety net.
- **Never ask a bulb what it already told us.** `_is_on` caches on/off, warmed by `refresh_states()` at the end of `connect_to_lights()` and updated on every command. It is what lets `toggle` and each brightness step skip a `get_device_info`. It can drift if a light is changed from the Tapo app or at the wall, so `House` refreshes it every `STATE_REFRESH` seconds — off the critical path, and never while a command is in flight. A stale entry is self-correcting.
- **Bound every call.** `ApiClient` is built with `timeout_s`; its own default is 30s, and with a retry on top one wedged bulb could hold a press for a minute. `_with_reconnect` recovers in two steps — `refresh_session()` first, which is one request, and only then a full `l530()` handshake, which is several.

`test/test_light_latency.py` measures all of this against real bulbs and prints the implied round-trip count; `--probe` runs House's switch probe alongside to reproduce contention.

**`House.watch()` must not probe while `tapo.busy`.** An L530 serves very few concurrent connections to port 80 — often one — and the probe wants the same socket a command needs. Losing that race fails the command, which costs a session refresh or a handshake, and that is how a 0.3s press becomes a multi-second one.

Two more things run on the event loop and must not block it: `run_action` calls `action.fn()` synchronously (so a blocking wrapper freezes the controller poll and every light command with it), and the wake-word `predict()` is a synchronous ONNX pass that goes through `asyncio.to_thread` for the same reason.

Two things in it exist only because a bulb can lose mains power (see `House`). A handle in `_lights` may be `None`: `connect_to_lights` no longer aborts startup when a bulb will not answer, it registers the name and IP and lets `_with_reconnect` build the handle on first use. **The key must stay in `_lights` regardless** — that dict is the canonical list of light names, iterated by every fan-out here and membership-tested by `light_show/player.py`. And a light can be marked unpowered (`mark_unpowered` / `mark_powered` / `is_powered`), after which calls to it raise `LightUnreachable` immediately and the fan-outs skip it: an unpowered bulb is an off bulb. Without that, every call to a dark bulb would wait out a full timeout twice, since nothing refuses the connection and `_with_reconnect` retries. The fan-outs all go through `_fan_out`, which uses `return_exceptions=True` and names what it could not reach, so one bulb off the wifi no longer fails "all off" for the other three.

`House` (`src/house.py`) treats the kitchen light's wall switch as a master switch for the flat. Nothing reports the switch position; the only sensor is whether `KITCHEN_LIGHT_IP` answers a TCP connect on port 80 — which is not a proxy for reachability but the exact transport the Tapo client uses, so a successful connect means the path it needs is open. Going through the Tapo API instead would pay the client's own connect timeout against a dead bulb, twice, inside a loop meant to tick every two seconds.

The debounce is deliberately lopsided. A powered-off bulb never answers (no ARP reply, so the connect hangs to `PROBE_TIMEOUT`), so only a *down* probe costs anything. Going down needs `DOWN_STREAK` agreeing samples, coming up needs one, because nothing else lives at that address.

**Those extra samples are taken back to back, in `_confirm`, not one per poll tick.** Spreading them across ticks put a whole `POLL_INTERVAL` between each one and made leaving the house take the better part of ten seconds to notice — three timeouts and two sleeps. The samples are just as independent taken immediately, and a wall switch does not flicker. What actually makes a short streak safe is the reference-bulb check, not the streak length: if *nothing* answers, that is our wifi rather than their mains, and the state is held.

Welcome home splits the lights by whether they had power all along. The always-powered ones come on immediately; only the ones whose mains just returned wait on `_ensure_api`, which **polls for readiness rather than sleeping** — a bulb accepts TCP well before it will serve a handshake, and a fixed settle paid its full length even when the bulb was ready at once. Holding every light back until the slowest was ready is what made walking in feel slow when half the answer was available instantly.

**A restart is not a homecoming.** The first observation only seeds state — it plays the startup chime (`resources/startup.mp3`, converted to wav on first run), touches no light, and logs what it assumed. Otherwise every `systemctl restart` would greet you for sitting still. Standby turns off every reachable light and suspends the voice assistant; the kitchen bulb is *marked* unpowered rather than commanded, since there is nothing there to answer.

`VoiceAssistant.suspend()` / `.resume()` are requests, not acts, for the same reason `set_mode` is — they may be called while a backend is mid-turn, so the supervisor does the teardown. While suspended no backend runs at all, and a resume comes through the same `_switch` event as a mode change but announces nothing, so it cannot talk over the greeting.

`Bluetooth` and `XboxControllerBattery` talk to `org.bluez` over D-Bus with `jeepney` rather than scraping `bluetoothctl`. The controller's battery is on BlueZ's Battery1 interface, not the joystick node, so it reads even when pygame sees no joystick.

`ShoppingList` (`src/shopping_list.py`) talks to Bring! through the unofficial `bring-api` package. It is the one wrapper that is async all the way down -- `TrelloBoard` pushes synchronous `py-trello` into `asyncio.to_thread`, but bring-api is aiohttp-native, so it needs no worker thread. It owns an `aiohttp.ClientSession` built lazily on the running loop, because `Bring.__init__` calls `asyncio.get_running_loop()` and a session belongs to the loop that created it; `main.py` closes it in a `finally`. Items carry a name and a separate `specification` -- the amount has to go in the latter or Bring cannot match the product to its catalogue tile.

`SoundPlayer` pre-generates any missing clip in `PHRASES` with gTTS on first run into `resources/speech/` (gitignored) and can speak arbitrary text via `say_text`, which needs network access.

## Tests

`test/` holds standalone hardware diagnostics, not a pytest suite — each is run directly and most need the Pi and live hardware:

```bash
python test/test_wake.py              # print wake word detections from the UDP stream
python test/test_controller_battery.py
python test/test_esp32_wifi.py
python src/house.py                   # probe the kitchen switch and time its edges
cd src && python ../test/test_light_latency.py   # how long a light command really takes
```

`src/house.py` run directly is a probe-only diagnostic: it builds no `TapoController` and touches no light. Run it on the Pi and flip the kitchen switch — it prints how long each edge actually took, which is how the constants at the top of that file should be chosen rather than by the guesses they currently are.

`src/debug_buttons.py` prints raw pygame button, axis, and hat numbers — use it when controller indices in `XboxController.BUTTON_MAPPING` need remapping.
