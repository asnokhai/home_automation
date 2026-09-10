"""Realtime voice: one websocket session, many turns.

`classic.py` is one HTTP round trip per utterance and forgets everything
afterwards. This one opens a session to the OpenAI Realtime API and keeps it up,
so the model answers while you are still listening and remembers the last thing
you said without another wake word.

The session is still wake-word gated: it bills for as long as it is open and it
would otherwise answer the television, so it opens on 'hey jarvis' and closes
itself after IDLE_TIMEOUT of silence.

Wire format is the GA Realtime API -- audio config nested under `session.audio`,
tool schemas flat rather than wrapped in a "function" key. If OpenAI moves a
field, the server says so in an `error` event naming it, which is printed.
"""

import asyncio
import base64
import json
import os
import queue
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime

import numpy as np
import websockets
from dotenv import load_dotenv

_SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import wakeword
from bindings import build_realtime_tools
from voice.mic_stream import RATE as MIC_RATE

load_dotenv()

REALTIME_URL = "wss://api.openai.com/v1/realtime"
MODEL = "gpt-realtime"
VOICE = "alloy"
API_RATE = 24000              # the Realtime API only speaks pcm16 at 24 kHz

IDLE_TIMEOUT = 5.0           # silence that ends the conversation
MAX_SESSION = 100.0           # hard cap, so a stuck session cannot bill all night
PLAYBACK_TAIL = 0.3           # extra mute after playback, for room reverb
MAX_TOOL_ROUNDS = 5           # consecutive tool round-trips before we stop looping
MAX_ACTIONS = 8               # per response, guards against a runaway fan-out

# Whether an action still plays its canned SoundPlayer clip in realtime mode.
# True keeps the instant "Kitchen on" confirmation you get everywhere else, at
# the cost of hearing it again in the model's own words a second later. Set
# False to let the realtime voice be the only thing that speaks.
SPEAK_ACTION_RESULTS = True


def _resample_to_api(frame):
    """16 kHz mic frame -> 24 kHz, the only rate the API accepts.

    Linear interpolation is fine here: upsampling cannot alias, so the worst it
    leaves is mild imaging above 8 kHz that the model does not care about.
    """
    n_out = int(round(len(frame) * API_RATE / MIC_RATE))
    xi = np.linspace(0, len(frame) - 1, n_out)
    up = np.interp(xi, np.arange(len(frame)), frame.astype(np.float32))
    return np.clip(up, -32768, 32767).astype("<i2")


def _connect(url, headers):
    """websockets renamed this keyword in v14; accept both so the Pi's pin does
    not get to decide whether voice works."""
    try:
        return websockets.connect(url, additional_headers=headers, max_size=None)
    except TypeError:
        return websockets.connect(url, extra_headers=headers, max_size=None)


class _Playback:
    """Streams audio deltas into a long-lived `aplay`.

    The model generates faster than real time, so ALSA back-pressure would stall
    the websocket receive loop -- and with it every function call -- if we wrote
    from the event loop. A worker thread absorbs that instead.
    """

    def __init__(self, rate=API_RATE):
        self.rate = rate
        self._q = queue.Queue()
        self._proc = None
        self._lock = threading.Lock()
        threading.Thread(target=self._worker, daemon=True).start()

    def _spawn(self):
        return subprocess.Popen(
            # -t raw is not optional: without it aplay tries to read a WAV
            # header off stdin. The small buffer bounds how much audio is
            # already inside ALSA when stop() kills it.
            ["aplay", "-q", "-t", "raw", "-f", "S16_LE", "-c", "1",
             "-r", str(self.rate), "--buffer-time=100000", "-"],
            stdin=subprocess.PIPE)

    def _worker(self):
        while True:
            pcm = self._q.get()
            if pcm is None:
                return
            with self._lock:
                if self._proc is None or self._proc.poll() is not None:
                    self._proc = self._spawn()
                proc = self._proc
            try:
                proc.stdin.write(pcm)
                proc.stdin.flush()
            except (BrokenPipeError, ValueError, OSError):
                pass

    def write(self, pcm):
        self._q.put(pcm)

    def _drain(self):
        while True:
            try:
                self._q.get_nowait()
            except queue.Empty:
                return

    def stop(self):
        """Cut playback now -- mode switch. The next write respawns aplay."""
        self._drain()
        with self._lock:
            proc, self._proc = self._proc, None
        if proc and proc.poll() is None:
            try:
                proc.stdin.close()
            except Exception:
                pass
            proc.kill()

    def close(self, timeout=5.0):
        """Let what is already queued finish, then shut aplay down."""
        deadline = time.monotonic() + timeout
        while not self._q.empty() and time.monotonic() < deadline:
            time.sleep(0.02)
        with self._lock:
            proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=max(0.0, deadline - time.monotonic()))
        except Exception:
            proc.kill()


class RealtimeVoiceAssistant:
    def __init__(self, mic, sound, wake=None):
        self.mic = mic
        self.sound = sound
        # Shared with the other backend: only one runs at a time, and
        # loading openWakeWord twice on a Pi is a real cost.
        self.oww, self.wake_keys = wake or wakeword.load_model()

        self.actions = {}
        self.tools = []
        self.handler = None
        # Set by the facade so a session that cannot connect at all falls back
        # to the pathway that works instead of leaving the house deaf.
        self.on_unavailable = None

        self._audio_end = 0.0     # monotonic time playback is expected to finish
        self._mute_until = 0.0
        self._deadline = 0.0
        self._hard_deadline = 0.0

    def set_actions(self, actions):
        self.actions = actions
        self.tools = build_realtime_tools(actions)

    def set_action_handler(self, handler):
        self.handler = handler

    def stop(self):
        """Here only to match the classic backend's interface. This pathway is
        pure asyncio, so cancelling the task is already enough."""

    def reset_stop(self):
        pass

    # --- session configuration -----------------------------------------
    def _instructions(self):
        return (
            "You are Jarvis, the voice of a smart home. Call a function for each "
            "action the user asks for -- several in one turn when they ask for "
            "several things. Only call functions that exist. Keep spoken replies "
            "to one or two short sentences; the user is across the room, not "
            "reading a screen. If the user says goodbye or that's all, say a "
            "short goodbye and stop talking.\n"
            # Dated per session, not at startup: this process stays up for days,
            # so a baked-in date would quietly drift and every "tomorrow" would
            # land on the wrong day.
            f"Today is {datetime.now().strftime('%A %d %B %Y')}. Use it to turn "
            "any deadline the user speaks into an ISO 8601 date."
        )

    def _session_update(self):
        return {
            "type": "session.update",
            "session": {
                "type": "realtime",
                "instructions": self._instructions(),
                "output_modalities": ["audio"],
                "audio": {
                    "input": {
                        "format": {"type": "audio/pcm", "rate": API_RATE},
                        "turn_detection": {
                            "type": "server_vad",
                            "threshold": 0.5,
                            "prefix_padding_ms": 300,
                            "silence_duration_ms": 600,
                        },
                        "transcription": {"model": "whisper-1"},
                    },
                    "output": {
                        "format": {"type": "audio/pcm", "rate": API_RATE},
                        "voice": VOICE,
                    },
                },
                "tools": self.tools,
                "tool_choice": "auto",
            },
        }

    # --- main loop -----------------------------------------------------
    async def run(self):
        self.oww.reset()
        print(f"  Voice (realtime): say '{wakeword.WAKEWORD}'")
        while True:
            frame = await self.mic.next_frame_async()
            score = wakeword.score(self.oww.predict(frame), self.wake_keys)
            if score < wakeword.THRESHOLD:
                continue

            self.sound.play_voice_assistant_activated()
            self.oww.reset()
            print(f"  Wake word detected ({score:.2f}) -- opening realtime session")
            started = time.monotonic()

            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"  ⚠ Realtime error: {e}")
                traceback.print_exc()
                if time.monotonic() - started < 2.0 and self.on_unavailable:
                    # Died before a word was spoken -- almost certainly auth,
                    # entitlement or network. Hand the house back to the
                    # pathway that works.
                    self.on_unavailable(str(e))
            finally:
                self.sound.play_voice_assistant_deactivated()
                self.mic.clear()
                self.oww.reset()
                print(f"  Realtime session closed after "
                      f"{time.monotonic() - started:.0f}s")

    async def _session(self):
        key = os.environ.get("OPENAI_API_KEY")
        if not key:
            raise RuntimeError("OPENAI_API_KEY is not set")

        now = time.monotonic()
        self._audio_end = 0.0
        self._mute_until = 0.0
        self._deadline = now + IDLE_TIMEOUT
        self._hard_deadline = now + MAX_SESSION

        url = f"{REALTIME_URL}?model={MODEL}"
        async with _connect(url, {"Authorization": f"Bearer {key}"}) as ws:
            await ws.send(json.dumps(self._session_update()))
            player = _Playback()
            pump = asyncio.create_task(self._pump_mic(ws))
            try:
                await self._receive(ws, player)
            except asyncio.CancelledError:
                player.stop()
                raise
            finally:
                pump.cancel()
                await asyncio.gather(pump, return_exceptions=True)
                player.close()

    async def _pump_mic(self, ws):
        """Mic -> API, except while the assistant is talking.

        Half duplex on purpose: the mic is an ESP32 across the room from the
        speaker and there is no echo cancellation anywhere in this system, so a
        full-duplex stream would let the assistant interrupt itself. The cost is
        that you cannot barge in mid-reply.
        """
        while True:
            frame = await self.mic.next_frame_async()
            if time.monotonic() < self._mute_until:
                continue
            await ws.send(json.dumps({
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(
                    _resample_to_api(frame).tobytes()).decode("ascii"),
            }))

    async def _receive(self, ws, player):
        outputs = []        # function_call_output items awaiting response.done
        handled = set()     # call_ids already run, so no path fires one twice
        rounds = 0

        while True:
            now = time.monotonic()
            if now > self._hard_deadline:
                print("  Realtime: session cap reached, closing")
                return
            if now > self._deadline and now > self._audio_end:
                print("  Realtime: idle, closing")
                return

            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            except websockets.exceptions.ConnectionClosed:
                return

            ev = json.loads(raw)
            kind = ev.get("type", "")

            if kind == "error":
                print(f"  ⚠ Realtime API error: {ev.get('error')}")

            elif kind == "input_audio_buffer.speech_started":
                self._deadline = time.monotonic() + IDLE_TIMEOUT

            elif kind == "conversation.item.input_audio_transcription.completed":
                text = (ev.get("transcript") or "").strip()
                if text:
                    print(f"  You: {text}")
                self._deadline = time.monotonic() + IDLE_TIMEOUT

            elif kind == "response.output_audio.delta":
                self._play(player, ev.get("delta", ""))

            elif kind == "response.output_audio_transcript.done":
                text = (ev.get("transcript") or "").strip()
                if text:
                    print(f"  Bot: {text}")

            elif kind == "response.function_call_arguments.done":
                item = await self._run_call(
                    ev.get("name"), ev.get("call_id"), ev.get("arguments"),
                    handled, len(outputs))
                if item:
                    outputs.append(item)

            elif kind == "response.done":
                # Belt and braces: the streamed argument events are the normal
                # path, but a response can also carry function_call items that
                # never streamed. `handled` keeps the two from double-firing.
                for item in (ev.get("response") or {}).get("output") or []:
                    if item.get("type") == "function_call":
                        got = await self._run_call(
                            item.get("name"), item.get("call_id"),
                            item.get("arguments"), handled, len(outputs))
                        if got:
                            outputs.append(got)

                if not outputs:
                    rounds = 0
                    continue
                rounds += 1
                if rounds > MAX_TOOL_ROUNDS:
                    print("  ⚠ Tool loop cut off")
                    outputs = []
                    continue
                for item in outputs:
                    await ws.send(json.dumps(
                        {"type": "conversation.item.create", "item": item}))
                outputs = []
                await ws.send(json.dumps({"type": "response.create"}))
                self._deadline = time.monotonic() + IDLE_TIMEOUT

    def _play(self, player, b64):
        if not b64:
            return
        pcm = base64.b64decode(b64)
        player.write(pcm)
        # Mute the mic for as long as this audio will take to come out of the
        # speaker. Chained off _audio_end rather than "now" so back-to-back
        # deltas extend one continuous window instead of resetting it.
        now = time.monotonic()
        self._audio_end = max(self._audio_end, now) + (len(pcm) / 2) / API_RATE
        self._mute_until = self._audio_end + PLAYBACK_TAIL
        self._deadline = self._audio_end + IDLE_TIMEOUT

    async def _run_call(self, name, call_id, arguments, handled, so_far):
        """Run one tool call and build the item that reports it back."""
        if not call_id or call_id in handled:
            return None
        handled.add(call_id)

        if so_far >= MAX_ACTIONS:
            print(f"  ⚠ Too many actions, skipping: {name}")
            return {"type": "function_call_output", "call_id": call_id,
                    "output": json.dumps({"error": "too many actions in one turn"})}

        try:
            args = json.loads(arguments or "{}")
        except ValueError:
            args = {}

        action = self.actions.get(name)
        if action is None:
            print(f"  ⚠ Unknown action: {name}")
            result = f"No such action: {name}"
        else:
            print(f"  → {name}{args or ''}")
            result = await self.handler(action, args, SPEAK_ACTION_RESULTS)

        return {
            "type": "function_call_output",
            "call_id": call_id,
            "output": json.dumps({"result": result if result is not None else "done"}),
        }
