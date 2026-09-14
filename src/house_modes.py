"""Whole-house themes: one word that sets the lights, the music and the TV.

Separate from `House`, deliberately. House is about one physical fact -- does
the kitchen bulb answer, i.e. is the wall switch on -- and the two transitions
that fall out of it. Nothing in it is a matter of taste. A theme is nothing
*but* taste: which lights, how warm, what is playing. Putting them in the same
class would mean the master switch and the mood settings shared a lock, a
watcher and a set of constants for no reason other than both being "the house".

A theme is a `Theme` value, not code, so adding one is a constant plus a
two-line method plus a line in `bindings.build_actions`. What `activate` does
with it is fixed:

  lights      the theme's lights come on, every other powered light goes off
  music       the theme's song starts on Spotify
  fireplace   the looping video goes on the TV, if the theme asks for it

Three things here are load-bearing:

**The colour and brightness are written into TapoController, not just sent.**
`turn_on` applies whatever mode the controller currently holds, so setting
`color_temp` and `brightness` first is both how the theme is painted -- one
round trip per bulb, through the same batched `set()` builder everything else
uses -- and how it survives. The dimmer buttons afterwards step from the cosy
level rather than snapping back to wherever the lights were this morning.
Night mode is cleared for the same reason: it paints the bulbs red at full
brightness and would ignore the colour temperature entirely.

**Every limb is allowed to fail on its own.** Spotify being unreachable must
not cost you the lights, and a fireplace that will not start must not cost you
the music, so the three run concurrently under `return_exceptions=True` and
what went wrong comes back in the returned sentence instead of as an exception.
The realtime voice pathway feeds that sentence back to the model, so it is
phrased to be read out.

**The confirmation is spoken here, not by `run_action`.** Same reason House
greets you itself: `run_action` only speaks once the call has returned, and
this call waits on a fireplace that takes a second or more to prove itself.
The binding for a theme therefore carries `say=None`.

The non-theme lights are turned off unconditionally rather than only when the
cache believes they are lit. That is one deliberate extra round trip against
TapoController's usual rule: those calls are concurrent with the rest, and a
theme that leaves a lamp burning because a cached state had drifted is a
visibly wrong result, where a redundant `off` costs nothing anyone can see.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

# The warmest an L530 goes. MIN_COLOR_TEMP in tapo_controller is the same
# number; it is repeated rather than imported because this is a statement about
# how the room should look, not about what the bulb happens to support.
COZY_COLOR_TEMP = 2500
COZY_BRIGHTNESS = 25


@dataclass(frozen=True)
class Theme:
    """One mood, as data.

    `lights` is exhaustive: anything not named in it and currently powered is
    turned off. `fireplace` False means "leave the TV alone", not "turn it off"
    -- a theme that says nothing about the TV should not reach over and stop
    something that was started on purpose.
    """
    name: str
    lights: tuple
    color_temp: int
    brightness: int
    song: str = None
    fireplace: bool = False
    phrase: str = None      # a SoundPlayer.PHRASES key, spoken as it starts


COZY = Theme(
    name="cozy",
    lights=("Vibe", "Kitchen"),
    color_temp=COZY_COLOR_TEMP,
    brightness=COZY_BRIGHTNESS,
    song="Gravity - John Mayer",
    fireplace=True,
    phrase="cozy_mode",
)

THEMES = {COZY.name: COZY}


class HouseModes:
    def __init__(self, tapo, spotify, tv, sound_player=None):
        self._tapo = tapo
        self._spotify = spotify
        self._tv = tv
        # Optional so the diagnostic at the bottom of this file can run without
        # pygame and the resources tree, the same way House takes its voice
        # assistant. Everything it is used for here is a nicety.
        self._sound_player = sound_player
        # A theme is several seconds of work across three subsystems. Two
        # arriving at once -- a button and a voice command, say -- would leave
        # the lights painted by one and the music chosen by the other.
        self._lock = asyncio.Lock()

        self.current = None   # the last theme activated

    # -- the themes ------------------------------------------------------

    async def cozy(self):
        """Cozy: vibe and kitchen only, warm and low, Gravity, fire on the TV."""
        return await self.activate(COZY)

    # -- the machinery ---------------------------------------------------

    async def activate(self, theme):
        """Apply one theme. Never raises; failures come back in the sentence."""
        if isinstance(theme, str):
            key = theme.strip().lower()
            if key not in THEMES:
                return f"I do not have a {theme} theme"
            theme = THEMES[key]

        async with self._lock:
            # Before anything else: the fireplace alone takes a second to prove
            # it started, and a confirmation landing after that is a
            # confirmation of nothing.
            if self._sound_player and theme.phrase:
                self._sound_player.say(theme.phrase)

            self._tapo.night_mode = False
            self._tapo.color_temp = theme.color_temp
            self._tapo.brightness = theme.brightness

            results = await asyncio.gather(
                self._set_lights(theme),
                self._play_music(theme),
                self._set_fireplace(theme),
                return_exceptions=True,
            )

            self.current = theme.name

            # An exception here is a bug rather than a limb reporting itself,
            # but it still has to be readable rather than swallowed.
            problems = [str(r) if isinstance(r, BaseException) else r
                        for r in results if r]
            label = theme.name.capitalize()
            if not problems:
                print(f"HouseModes: {theme.name}")
                return f"{label} mode"

            print(f"  ! {theme.name}: {'; '.join(problems)}")
            return f"{label} mode, but {self._join(problems)}"

    async def _set_lights(self, theme):
        """The theme's lights on, every other powered light off, all at once."""
        known = self._tapo.light_names()

        # A light with no mains is already off, and asking would only cost two
        # timeouts to be told so -- TapoController raises LightUnreachable for
        # exactly this. It is named in the answer, because cozy mode with the
        # kitchen dark otherwise looks like a failure with no explanation.
        wanted = [n for n in theme.lights if n in known]
        dark = [n for n in wanted if not self._tapo.is_powered(n)]
        on = [n for n in wanted if self._tapo.is_powered(n)]
        off = [n for n in known
               if n not in theme.lights and self._tapo.is_powered(n)]

        results = await asyncio.gather(
            *(self._tapo.turn_on(n) for n in on),
            *(self._tapo.turn_off(n) for n in off),
            return_exceptions=True,
        )
        failed = [n for n, r in zip(on + off, results)
                  if isinstance(r, BaseException)]

        trouble = []
        if dark:
            trouble.append(f"{self._names(dark)} has no power")
        if failed:
            trouble.append(f"I could not reach {self._names(failed)}")
        return self._join(trouble) if trouble else None

    async def _play_music(self, theme):
        """Start the theme's song, off the event loop.

        spotipy is synchronous and every call is an HTTPS round trip -- several
        of them here, since play_song searches, transfers playback and starts.
        Run inline that would freeze the controller poll and every light
        command with it for as long as it took.
        """
        if not theme.song:
            return None
        try:
            await asyncio.to_thread(self._spotify.play_song, theme.song)
        except Exception as e:
            return f"Spotify would not play {theme.song} ({e})"
        return None

    async def _set_fireplace(self, theme):
        """Put the fire on the TV, if the theme asks for one.

        show_fireplace turns the TV on and switches it to us first, so there is
        nothing to arrange around it.
        """
        if not theme.fireplace:
            return None
        try:
            await self._tv.show_fireplace()
        except Exception as e:
            return f"the fireplace would not start ({e})"
        return None

    @classmethod
    def _names(cls, names):
        """'the kitchen', or 'the kitchen and the vibe', for speaking."""
        return cls._join([f"the {n.lower()}" for n in names])

    @staticmethod
    def _join(parts):
        """Read a list out the way a person would: a, b and c.

        Everything this class returns is spoken -- by the canned clip in
        classic mode, by the model itself in realtime -- so "x, and y, and z"
        is not a cosmetic problem.
        """
        if len(parts) == 1:
            return parts[0]
        return f"{', '.join(parts[:-1])} and {parts[-1]}"


if __name__ == "__main__":
    # Run from inside src/, like main.py: `python house_modes.py cozy`.
    # Builds the real wrappers and activates one theme, so the whole path can
    # be exercised on the pi without the controller, the mic or the watchers.
    import sys

    from spotify_player import SpotifyPlayer
    from tapo_controller import TapoController
    from tv import TV

    USAGE = "usage: python house_modes.py " + "|".join(THEMES)

    async def _main(argv):
        name = (argv[1] if len(argv) > 1 else "").strip().lower()
        if name not in THEMES:
            print(USAGE)
            return

        tapo = TapoController()
        await tapo.connect_to_lights()
        tv = TV()
        # No SoundPlayer: its asset paths are relative to the repo root, and
        # the confirmation clip is not what is being tested here.
        modes = HouseModes(tapo, SpotifyPlayer(), tv)
        try:
            print(await modes.activate(THEMES[name]))
            print("Ctrl-C to stop the fireplace and exit.")
            while True:
                await asyncio.sleep(1)
        finally:
            # The player is our child; leaving without this orphans it holding
            # the display plane.
            tv.close()

    try:
        asyncio.run(_main(sys.argv))
    except KeyboardInterrupt:
        pass
