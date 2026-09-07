"""The one voice assistant main.py builds.

There are two pathways -- `classic.py` (wake word, record, transcribe, one chat
completion, TTS) and `realtime.py` (a live websocket session) -- but only one
mic, one speaker and one wake-word model between them. This owns all three and
runs exactly one backend at a time, so the two can never contend for the UDP
port or talk over each other.

Switching is a request, not an act: `set_mode`/`toggle_mode` only flag the
change and return. That matters because in realtime mode the switch arrives as a
tool call running *inside* the backend being torn down -- tearing it down from
there would cancel the very coroutine trying to report the result. The
supervisor loop below does the teardown instead, safely outside it.
"""

import asyncio
import os
import sys
import traceback

_SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import wakeword

from voice.classic import ClassicVoiceAssistant
from voice.mic_stream import MicStream
from voice.modes import CLASSIC, MODES, REALTIME
from voice.realtime import RealtimeVoiceAssistant

# How long to wait for a backend to actually stop before moving on. The classic
# one can be parked in a 15 s recording window; the stop flag gets it out within
# a frame, and this is the belt to that pair of braces. The supervisor must not
# stall the event loop -- controller.run() and phone.watch() share it.
TEARDOWN_TIMEOUT = 2.0


class VoiceAssistant:
    def __init__(self, sound, port=5005, mode=CLASSIC):
        self.sound = sound
        self.mic = MicStream(port=port).start()
        wake = wakeword.load_model()      # 'hey jarvis' only, shared by both

        self._backends = {
            CLASSIC: ClassicVoiceAssistant(self.mic, sound, wake),
            REALTIME: RealtimeVoiceAssistant(self.mic, sound, wake),
        }
        self._backends[REALTIME].on_unavailable = self._realtime_unavailable

        self._mode = mode if mode in MODES else CLASSIC
        self._pending = None
        self._switch = asyncio.Event()

    # --- wiring --------------------------------------------------------
    def set_actions(self, actions):
        """Deferred rather than passed to __init__: this object is itself an
        Action target, so it has to exist before build_actions() runs."""
        for backend in self._backends.values():
            backend.set_actions(actions)

    def set_action_handler(self, handler):
        """`handler(action, args=None, speak=True) -> result`.

        Classic ignores the return value; realtime feeds it back to the model as
        the tool result, which is how it can read out a battery level or a
        Trello list instead of guessing at one.
        """
        for backend in self._backends.values():
            backend.set_action_handler(handler)

    # --- mode ----------------------------------------------------------
    @property
    def mode(self):
        return self._pending or self._mode

    def set_mode(self, mode):
        """Request a switch. Synchronous and immediate -- see the module docstring."""
        if mode not in MODES:
            return f"No such voice mode: {mode}"
        if mode == self.mode:
            return f"Already in {mode} voice mode"
        self._pending = mode
        self._switch.set()
        return f"Switching to {mode} voice mode"

    def toggle_mode(self):
        return self.set_mode(REALTIME if self.mode == CLASSIC else CLASSIC)

    # Named wrappers rather than partial(set_mode, REALTIME) at the binding
    # site: bindings.py must not import anything from this package, or the two
    # form an import cycle -- voice imports bindings for its tool schemas.
    def use_realtime(self):
        return self.set_mode(REALTIME)

    def use_classic(self):
        return self.set_mode(CLASSIC)

    def _realtime_unavailable(self, reason):
        print(f"  ⚠ Realtime voice unavailable ({reason}) -- falling back")
        self.set_mode(CLASSIC)

    # --- supervisor ----------------------------------------------------
    async def run(self):
        print(f"  Voice mode: {self._mode}")
        task = waiter = None
        try:
            while True:
                backend = self._backends[self._mode]
                backend.reset_stop()
                task = asyncio.create_task(backend.run())
                waiter = asyncio.create_task(self._switch.wait())

                # asyncio.wait, not `await task`: awaiting a task and then
                # cancelling it lets the CancelledError land in this coroutine
                # and take the whole supervisor down with the backend.
                done, _ = await asyncio.wait(
                    {task, waiter}, return_when=asyncio.FIRST_COMPLETED)

                if waiter in done:
                    backend.stop()
                    task.cancel()
                    await asyncio.wait({task}, timeout=TEARDOWN_TIMEOUT)
                    self.mic.clear()
                    self._mode = self._pending or self._mode
                    self._pending = None
                    self._switch.clear()
                    print(f"  Voice mode: {self._mode}")
                    self.sound.say(f"voice_mode_{self._mode}")
                else:
                    waiter.cancel()
                    exc = task.exception()
                    if exc:
                        print(f"  ⚠ Voice backend crashed: {exc}")
                        traceback.print_exception(
                            type(exc), exc, exc.__traceback__)
                    # Never hot-loop a crashing backend.
                    await asyncio.sleep(1)
        finally:
            for pending in (task, waiter):
                if pending and not pending.done():
                    pending.cancel()
