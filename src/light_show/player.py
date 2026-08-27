"""
player.py — plays a saved show on the bulbs, in time with Spotify.

The show says what should happen; this works out what the bulbs can actually be
told. One L530 answers in roughly 300ms, so calls are paced, repeats dropped,
hue changes sent a moment ahead of the level they belong to, and a blackout long
enough to be worth it switches the bulb off rather than dimming it.

Timing is anchored to Spotify's reported playback position, not to when playback
was requested, so every light shares one clock.
"""

import asyncio
import bisect
import collections
import time
from datetime import datetime

from .show import Show

BRIGHT = 100
MIN_LEVEL = 5      # dimmest level a note is allowed to ask for
DARK_LEVEL = 1     # the bulb's floor: what "off" looks like when it must stay
                   # powered. Set before every power-off too, because a bulb
                   # wakes at its stored brightness and WAKE_LEAD puts that wake
                   # a moment *before* the note — at 5 that pre-glow was visible
WHITE_TEMP = 6500  # a desaturated hue renders warm yellow on an L530, so white
                   # goes out as a colour temperature

MIN_CALL_GAP = 0.32   # closest two calls to one bulb; the measured round trip
POWER_OFF_MIN = 0.9   # dark at least this long -> actually switch the bulb off.
                      # Below this there is no time for the off and the wake, so
                      # the bulb drops to DARK_LEVEL instead
WAKE_LEAD = 0.60      # power back on this early: covers wake + colour + level
MIN_GAP = 0.05        # closest two scheduled actions may sit
COLOR_LEAD = 0.30     # push a hue change this far ahead of its level

LEAD_TIME = 0.18      # fire this early to cover bulb latency
SYNC_TIMEOUT = 10.0   # give up waiting for Spotify to report playback
MAX_LATE = 0.20       # drop an action already this far behind
ON_TIME = 5.0         # lateness below this many ms reports as "on time"

HUE_NAMES = ((15, "red"), (45, "orange"), (70, "yellow"), (160, "green"),
             (188, "teal"), (200, "cyan"), (250, "blue"), (290, "purple"),
             (330, "pink"), (361, "red"))


def color_name(color):
    """Human name for a colour, for the log."""
    if not color:
        return ""
    if color[0] == "ct":
        return "white" if color[1] >= 4500 else "warm"
    _, hue, sat = color
    if sat < 15:
        return "white"
    for limit, name in HUE_NAMES:
        if hue < limit:
            return name
    return "red"


class ShowPlayer:
    """Turns a Show into bulb calls and runs them against the song."""

    def __init__(self, show, spotify_player, tapo_controller=None, *,
                 lead_time=LEAD_TIME, max_late=MAX_LATE, min_call_gap=MIN_CALL_GAP):
        self.show = show
        self._player = spotify_player
        self._tapo = tapo_controller
        self._lead_time = lead_time
        self._max_late = max_late
        self._min_call_gap = min_call_gap
        self._schedules = None
        self._t0 = None

    # ── compiling ──────────────────────────────────────────────────

    def compile(self):
        """{light: [(time, light, level, kind, index, colour), …]}"""
        self._schedules = {}
        for light in self.show.lights:
            levels = self.show.levels_for(light)
            if not levels:
                # no notes at all: hold it off for the whole song rather than
                # leaving it on whatever the room had it at
                self._schedules[light] = [(0.0, light, "off", "dark", 0, None)]
                print(f"  {light:<13}   no notes — held off")
                continue
            colors = self.show.colors_for(light)
            times = [t for t, _ in colors]

            def color_at(t, _c=colors, _t=times):
                i = bisect.bisect_right(_t, t)
                return _c[i - 1][1] if i else None

            schedule, stats = self._pace(levels, color_at, light)
            self._schedules[light] = schedule
            print(f"  {light:<13}{len(levels):>4} points -> {len(schedule):>4} actions"
                  f"   {stats['levels']} levels, {stats['colors']} colours, "
                  f"{stats['dark']} blackouts"
                  + (f", {stats['dropped']} too fast for the bulb"
                     if stats["dropped"] else ""))
        return self._schedules

    def _pace(self, levels, color_at, light):
        """Pace a (time, brightness) sequence into what one bulb can serve."""
        schedule = []
        stats = collections.Counter()
        last_color = last_level = None
        last_sent = -1e9
        index = dropped = 0
        i = 0

        while i < len(levels):
            t, level = levels[i]
            i += 1
            if level == last_level:
                continue

            if level == 0:
                nxt = next((lt for lt, lv in levels[i:] if lv), None)
                power_off = nxt is None or nxt - t >= POWER_OFF_MIN
                # a long gap is worth a real power-off; a short one has no room
                # for the off and the wake, so the bulb goes to its floor instead
                want = "off" if power_off else DARK_LEVEL
                if last_level == want:
                    continue                    # already dark, the same way

                # Never drop a blackout: a dropped level just means a missed
                # change, but a dropped blackout leaves the light on. If it
                # lands too soon after the previous call, hold it a moment and
                # go dark late — unless the next note relights before then.
                soonest = last_sent + self._min_call_gap * (2 if power_off else 1)
                if t < soonest:
                    if nxt is not None and soonest >= nxt - 1e-6:
                        dropped += 1
                        continue
                    t = soonest

                if power_off:
                    schedule.append((t, light, "off", "dark", 0, None))
                    if nxt is not None:
                        schedule.append((nxt - WAKE_LEAD, light, "on", "wake", 0, None))
                    stats["dark"] += 1
                    last_color = None           # a power cycle loses the colour
                else:
                    index += 1
                    schedule.append((t, light, DARK_LEVEL, "level", index, None))
                    stats["levels"] += 1
                last_level, last_sent = want, t
                continue

            color = color_at(t)
            recolor = color is not None and color != last_color
            # a level costs one call, a level plus a hue change costs two
            if t - last_sent < self._min_call_gap * (2 if recolor else 1):
                dropped += 1
                continue

            index += 1
            send = color if recolor else None
            if recolor:
                stats["colors"] += 1
                for lead in (COLOR_LEAD, COLOR_LEAD / 2):
                    at = t - lead
                    if at < 0:
                        continue     # before the song starts: nothing can fire there
                    if not schedule or at >= schedule[-1][0] + MIN_GAP:
                        schedule.append((at, light, None, "color", index, send))
                        send = None
                        break
                else:
                    stats["late_colors"] += 1
                last_color = color
            schedule.append((t, light, level, "level", index, send))
            stats["levels"] += 1
            last_level, last_sent = level, t

        schedule.sort(key=lambda a: a[0])
        stats["dropped"] = dropped
        return schedule, stats

    # ── the bulbs ──────────────────────────────────────────────────

    async def connect(self):
        """Build and connect a controller if none was given, then check the lights."""
        if self._tapo is None:
            from tapo_controller import TapoController

            print("Connecting to lights…")
            self._tapo = TapoController()
            await self._tapo.connect_to_lights()

        missing = [n for n in (self._schedules or {}) if n not in self._tapo._lights]
        if missing:
            raise KeyError(f"no such light(s): {', '.join(missing)} — "
                           f"have {', '.join(self._tapo._lights)}")
        return self._tapo

    async def apply(self, light, level, color):
        """Push a colour and/or brightness to one bulb.

        Colour first: an action carrying both is one the schedule had no room to
        send early, so the hue has to land before the light comes up. Routed
        through the controller's reconnect, since Tapo sessions expire.
        """
        async def action(device):
            if color is not None:
                mode, *args = color
                if mode == "ct":
                    await device.set_color_temperature(*args)
                else:
                    await device.set_hue_saturation(*args)
            if level in ("off", "off-only"):
                if level == "off":
                    await device.set_brightness(DARK_LEVEL)
                await device.off()
            elif level == "on":
                await device.on()
            elif level is not None:
                await device.set_brightness(level)

        await self._tapo._with_reconnect(light, action)

    async def _prime(self, light):
        """Start every light off, with a dim level already set underneath.

        A bulb wakes at whatever brightness it last held, so the level goes in
        before the power-off — otherwise the first wake of the show flashes at
        full. Priming to *off* matters because a light is only supposed to be on
        where a note puts it, and that includes before the first note.
        """
        await self._tapo._with_reconnect(light, lambda d: d.on())
        await self.apply(light, DARK_LEVEL, ("ct", WHITE_TEMP))
        await self._tapo._with_reconnect(light, lambda d: d.off())

    async def _restore(self, light):
        try:
            await self._tapo._with_reconnect(light, self._tapo._apply_mode)
        except Exception as exc:
            print(f"  [warn] could not restore {light}: {exc}")

    # ── playback ───────────────────────────────────────────────────

    async def anchor_to_song(self):
        """Monotonic time at which the song was at position 0.0s.

        Spotify reports where the track actually is, so the show pins to that
        rather than to whenever start_playback() returned.
        """
        deadline = time.monotonic() + SYNC_TIMEOUT
        while time.monotonic() < deadline:
            before = time.monotonic()
            state = await asyncio.to_thread(self._player.get_playback_state)
            after = time.monotonic()
            if state and state.get("is_playing") and state.get("progress_ms") is not None:
                return (before + after) / 2 - state["progress_ms"] / 1000.0
            await asyncio.sleep(0.1)
        return None

    async def run(self):
        if self._schedules is None:
            print(self.show.summary())
            self.compile()
        if not self._schedules:
            print("Nothing to play — no cues placed.")
            return

        await self.connect()
        await asyncio.gather(*(self._prime(l) for l in self._schedules))

        print(f"Starting song: {self.show.song}")
        if self.show.track_uri:
            await asyncio.to_thread(self._player.play_track, self.show.track_uri)
        else:
            await asyncio.to_thread(self._player.play_song, self.show.song)

        anchor = await self.anchor_to_song()
        if anchor is None:
            print("  [warn] Spotify never reported playback — starting now")
            anchor = time.monotonic()
        self._t0 = anchor + self.show.offset

        now = datetime.now()
        print(f"▶ LIGHT SHOW START  {now:%H:%M:%S}.{now.microsecond // 1000:03d}  "
              f"(song at {time.monotonic() - self._t0:+.2f}s, "
              f"{len(self._schedules)} lights, "
              f"{sum(len(s) for s in self._schedules.values())} actions)")

        stats = await asyncio.gather(*(self._run_light(l, s)
                                       for l, s in self._schedules.items()))
        print("Done.")
        for light, s in zip(self._schedules, stats):
            drift = sum(s["latencies"]) / len(s["latencies"]) if s["latencies"] else 0
            calls = sorted(s["durations"])
            mean = sum(calls) / len(calls) if calls else 0
            print(f"  {light:<13}{s['levels']:>4} levels, {s['dark']:>2} dark, "
                  f"{s['colors']:>3} colours, {s['failed']:>2} failed, "
                  f"{s['skipped']:>3} skipped   drift {drift:.0f}ms   "
                  f"bulb {mean:.0f}ms mean")

        await asyncio.gather(*(self._restore(l) for l in self._schedules))

    async def _run_light(self, light, schedule):
        """Drive one bulb through its schedule."""
        s = dict(levels=0, colors=0, dark=0, failed=0, skipped=0,
                 latencies=[], durations=[])
        level_now = DARK_LEVEL

        for t, name, level, kind, index, color in schedule:
            fire_at = self._t0 + t - self._lead_time
            wait = fire_at - time.monotonic()
            if wait < -self._max_late and kind not in ("dark", "wake"):
                # dark/wake are exempt: a shed wake leaves the bulb off all song
                s["skipped"] += 1
                continue
            if wait > 0:
                await asyncio.sleep(wait)

            fired_at = time.monotonic()
            s["latencies"].append(max(0.0, (fired_at - fire_at) * 1000))

            if kind == "dark" and level_now == DARK_LEVEL:
                level = "off-only"          # already dark, no need to dim first

            started = time.monotonic()
            try:
                await self.apply(light, level, color)
                s["durations"].append((time.monotonic() - started) * 1000)
            except Exception as exc:
                s["failed"] += 1
                print(f"  [{fired_at - self._t0:7.2f}s] ✗ {light:<13}"
                      f"{type(exc).__name__}: {exc}")
                continue

            if kind == "dark":
                s["dark"] += 1
            elif kind == "wake":
                level_now = DARK_LEVEL
            elif kind == "level":
                s["levels"] += 1
                level_now = level
            if color is not None:
                s["colors"] += 1
        return s
