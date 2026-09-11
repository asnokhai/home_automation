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
        self._suspended = False
        # One event for both kinds of change -- a mode switch and a
        # suspend/resume -- because the supervisor's response to either is the
        # same: tear the running backend down and look at the flags again.
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

    # --- suspend -------------------------------------------------------
    @property
    def suspended(self):
        return self._suspended

    def suspend(self):
        """Stop listening entirely until resume().

        A request, not an act, for exactly the reason a mode switch is: this can
        be called from House while a backend is mid-turn, and the teardown
        belongs to the supervisor. With the flat empty there is nobody to talk
        to, and an open realtime session would happily answer the television.
        """
        if self._suspended:
            return "Voice already suspended"
        self._suspended = True
        self._switch.set()
        return "Voice suspended"

    def resume(self):
        """Start listening again."""
        if not self._suspended:
            return "Voice already listening"
        self._suspended = False
        self._switch.set()
        return "Voice resumed"

    def _realtime_unavailable(self, reason):
        print(f"  ⚠ Realtime voice unavailable ({reason}) -- falling back")
        self.set_mode(CLASSIC)

    # --- supervisor ----------------------------------------------------
    async def run(self):
        print(f"  Voice mode: {self._mode}")
        task = waiter = None
        try:
            while True:
                if self._suspended:
                    # No backend at all while suspended -- not a paused one.
                    # The mic thread keeps the UDP port bound and its queue
                    # bounded, so nothing accumulates with no consumer.
                    print("  Voice suspended")
                    self._switch.clear()
                    await self._switch.wait()
                    self._switch.clear()
                    # Drop whatever arrived while nobody was listening, so the
                    # returning backend does not wake on stale audio.
                    self.mic.clear()
                    print(f"  Voice mode: {self._mode}")
                    continue

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
                    self._switch.clear()

                    if self._suspended:
                        # Torn down; the branch at the top of the loop parks
                        # until someone resumes.
                        continue

                    previous = self._mode
                    self._mode = self._pending or self._mode
                    self._pending = None
                    # Only on a real mode change: a resume comes through this
                    # same event, and announcing the mode then would talk over
                    # the greeting House has just played.
                    if self._mode != previous:
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
