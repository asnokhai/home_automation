"""
Named countdown timers.

Each timer is its own asyncio task sleeping to an absolute deadline; when one
fires it rings SoundPlayer's looping alarm until someone dismisses it.

Nothing is persisted -- timers do not survive a restart, same as every other
piece of runtime state here.
"""

from __future__ import annotations

import asyncio
import time


class Timers:
    def __init__(self, sound_player):
        # The SoundPlayer is injected because a timer fires outside run_action:
        # build_actions never sees `sound_player`, so the alarm has to be reachable
        # through this object itself.
        self._sound_player = sound_player
        self._timers: dict[str, tuple[float, asyncio.Task]] = {}

    # -- actions ---------------------------------------------------------

    def set_timer(self, name, duration_seconds):
        """Start a named countdown, replacing any timer already using the name.

        Returns immediately rather than awaiting the countdown: the voice
        assistant dispatches tool calls one at a time and awaits each, so a
        blocking timer would deafen the wake word for its whole duration.
        """
        try:
            seconds = float(duration_seconds)
        except (TypeError, ValueError):
            return "Timer needs a positive duration"

        if seconds <= 0:
            return "Timer needs a positive duration"

        name = self._key(name)
        if not name:
            return "Timer needs a name"

        self._cancel(name)

        deadline = time.monotonic() + seconds
        task = asyncio.create_task(self._countdown(name, deadline))
        # Kept in the dict both to cancel later and so the event loop cannot
        # garbage collect the task out from under the countdown.
        self._timers[name] = (deadline, task)

        print(f"Timer set: {name} ({self._describe(seconds)})")
        return f"{name.capitalize()} timer set for {self._describe(seconds)}"

    def list_timers(self):
        """Return which timers are running and how long is left on each."""
        self._prune()
        if not self._timers:
            return "No timers running"

        now = time.monotonic()
        soonest_first = sorted(self._timers.items(), key=lambda item: item[1][0])
        return ". ".join(f"{name.capitalize()}, {self._describe(deadline - now)}"
                         for name, (deadline, _) in soonest_first)

    def cancel_timer(self, name):
        """Cancel one timer by name."""
        name = self._key(name)
        if not self._cancel(name):
            return f"No timer called {name}" if name else "No timer by that name"

        print(f"Timer cancelled: {name}")
        return f"{name.capitalize()} timer cancelled"

    def cancel_all_timers(self):
        """Cancel every running timer."""
        self._prune()
        count = len(self._timers)
        if not count:
            return "No timers running"

        for name in list(self._timers):
            self._cancel(name)

        said = f"Cancelled {count} timer" + ("" if count == 1 else "s")
        print(said)
        return said

    def stop_alarm(self):
        """Silence a ringing timer alarm.

        Lives here rather than binding SoundPlayer's method directly so that
        build_actions only has to learn about one new wrapper.
        """
        return self._sound_player.stop_timer_alarm()

    # -- internals -------------------------------------------------------

    async def _countdown(self, name, deadline):
        """Sleep until the deadline, then ring.

        Swallows its own exceptions: this runs as a bare task, so nothing is
        watching its result and a failure would otherwise vanish silently.
        """
        try:
            await asyncio.sleep(max(0.0, deadline - time.monotonic()))
            self._timers.pop(name, None)
            print(f"Timer finished: {name}")
            self._sound_player.play_timer_alarm()
            self._sound_player.say_text(f"{name} timer finished")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"  ⚠ Timer '{name}' failed: {e}")

    def _cancel(self, name):
        """Drop a timer and cancel its task. True if there was one."""
        entry = self._timers.pop(name, None)
        if entry is None:
            return False

        entry[1].cancel()
        return True

    def _prune(self):
        """Forget timers whose task already finished."""
        for name in [n for n, (_, task) in self._timers.items() if task.done()]:
            self._timers.pop(name, None)

    @staticmethod
    def _key(name):
        """Normalise a spoken name so casing never breaks a cancel."""
        return str(name or "").strip().lower()

    @staticmethod
    def _describe(seconds):
        """Render a duration as speakable text, e.g. '3 minutes 20 seconds'."""
        seconds = max(0, int(round(seconds)))
        hours, rest = divmod(seconds, 3600)
        minutes, seconds = divmod(rest, 60)

        parts = []
        if hours:
            parts.append(f"{hours} hour" + ("" if hours == 1 else "s"))
        if minutes:
            parts.append(f"{minutes} minute" + ("" if minutes == 1 else "s"))
        if seconds or not parts:
            parts.append(f"{seconds} second" + ("" if seconds == 1 else "s"))

        return " ".join(parts)


if __name__ == "__main__":
    class _FakeSound:
        def play_timer_alarm(self):
            print("  [alarm ringing]")

        def stop_timer_alarm(self):
            print("  [alarm stopped]")
            return True

        def say_text(self, text):
            print(f"  [say] {text}")

    async def _demo():
        timers = Timers(_FakeSound())
        print(timers.set_timer("pasta", 3))
        print(timers.set_timer("eggs", 90))
        print(timers.list_timers())
        print(timers.cancel_timer("EGGS"))
        print(timers.list_timers())
        await asyncio.sleep(4)
        print(timers.list_timers())
        print(timers.stop_alarm())

    asyncio.run(_demo())
