"""
light_show.py — drives four Tapo bulbs from four Beat Saber lighting channels,
pinned to where the song actually is in Spotify.

A channel's light is a sustained level, not a blink: Off and On hold, Flash
peaks then stays lit, Fade peaks then decays to black. The curves are taken
from ArcViewer, the previewer BeatSaver renders maps with, so what the bulbs
do matches what the preview shows — a fade halves every 0.15s and is black
within ~0.6s, which is where the darkness between flashes comes from.

The map's own events are the schedule; there is no grid. Actions are rate
limited to what one bulb can serve, and genuinely dark stretches switch the
bulb off rather than dimming it.

Colour comes from the map's own environment palette (left/right/white, plus
the boost variants). Hue is pushed a moment *before* the pulse, while the bulb
is still dim, so the swap is invisible and the pulse stays one fast call.

Every light runs its own loop off one shared anchor, so the four stay in sync
with each other and with the song.
"""

import asyncio
import bisect
import collections
import colorsys
import json
import math
import os
import time
from datetime import datetime

# ═══════════════════════════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════════════════════════

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

MAP_DIR    = os.path.join(PROJECT_ROOT, "resources", "Nightmare")
DIFFICULTY = "ExpertPlus"
SONG       = "Nightmare - Avenged Sevenfold"

# Which lighting channel drives which bulb. None = pick per map: channels whose
# lit/dark profiles are near-duplicates get dropped, and the two most dissimilar
# survivors go to the paired living-room lights. Set a dict to pin it by hand.
CHANNELS = None
PAIRED   = ("Living Room", "Vibe")   # same room, so give these the biggest contrast
MIN_CHANNEL_EVENTS = 20    # a channel with fewer events than this all song is unusable
MIN_CHANNEL_SHARE  = 0.05  # …as is one under this share of the busiest channel
LIGHTS   = ("Living Room", "Vibe", "Kitchen", "Bathroom")

# Beat Saber's light curves, from ArcViewer's LightManager.cs / Easings.cs
# (github.com/AllPoland/ArcViewer). Off is black, On is full, Flash spikes then
# settles back to On and stays lit, Fade spikes then decays to black:
#     flash: lerp(1.2, 1.0, cubic_out(dt / 0.6))      -> 1.0 after 0.6s
#     fade:  lerp(1.2, 0.0, expo_out (dt / 1.5))      -> 1.2 * 2**(-10*dt/1.5)
# Expo.Out is the important one: the fade halves every 0.15s, so it reads as
# black after roughly 0.6s rather than lingering for a couple of seconds.
FLASH_INTENSITY = 1.2
FLASH_SETTLE    = 0.6
FADE_TIME       = 1.5
DARK_ALPHA      = 0.06   # below this the light reads as out

BRIGHT     = 100   # bulb brightness at a flash/fade peak (alpha 1.2)
STEP       = 5     # quantise brightness to this, so tiny changes cost no call
MIN_LEVEL  = 5     # dimmest lit step
DIM        = 5     # level set just before powering off, so waking cannot flash

MIN_CALL_GAP  = 0.32  # closest two calls to one bulb may be scheduled; the measured
                      # round trip to an L530 is ~300ms
POWER_OFF_MIN = 1.0   # dark for at least this long -> actually switch the bulb off
WAKE_LEAD     = 0.60  # power back on this long before the next lit action: it has to
                      # cover the wake call, a colour call and a level call
MIN_GAP    = 0.05  # closest two calls to the same bulb may be scheduled
COLOR_LEAD = 0.30  # aim to push a hue change this long before the level it belongs to, so
                   # the two calls do not queue up; halved, then abandoned, if no room
WHITE_TEMP = 6500  # kelvin for the white palette entry — a desaturated hue renders
                   # warm yellow on an L530, so white goes out as a colour temperature

LEAD_TIME  = 0.18   # fire this early to cover bulb latency
OFFSET     = 0.0    # positive = whole show runs later against the song

SYNC_TIMEOUT = 10.0  # give up waiting for Spotify to report playback
MAX_LATE     = 0.20  # drop events we are already this far behind on; big enough
                     # that a dense burst catches up instead of being shed
ON_TIME      = 5.0   # lateness below this many ms is reported as "on time"

# ═══════════════════════════════════════════════════════════════════


class LightShow:
    """Plays Beat Saber lighting channels across several bulbs, synced to Spotify.

    The show is timed against real playback position, so a SpotifyPlayer is
    required — without one there is nothing to sync to.
    """

    # Beat Saber light values: what the light does…
    STEADY_VALUES     = {1, 5, 9}      # on, and stays on
    FLASH_VALUES      = {2, 6, 10}     # spike, back to the previous level
    FADE_VALUES       = {3, 7, 11}     # spike, decay to dark
    TRANSITION_VALUES = {4, 8, 12}     # ramp to this level and hold
    # …and which palette entry it uses (0 and unknown values are off)
    RIGHT_VALUES = {1, 2, 3, 4}
    LEFT_VALUES  = {5, 6, 7, 8}
    WHITE_VALUES = {9, 10, 11, 12}

    # Stock Beat Saber environment colours, used when the map defines none
    DEFAULT_COLORS = {
        "left":  (0.7843, 0.0784, 0.0784),   # red
        "right": (0.1568, 0.5568, 0.8235),   # blue
    }
    CUSTOM_KEYS = {
        "left": "_envColorLeft",             "right": "_envColorRight",
        "left_boost": "_envColorLeftBoost",  "right_boost": "_envColorRightBoost",
    }

    EVENT_NAMES = {
        0: "back lasers",       1: "ring lights",   2: "left lasers",
        3: "right lasers",      4: "center lights", 8: "ring spin",
        9: "ring zoom",        10: "extra left",   11: "extra right",
        12: "left laser speed", 13: "right laser speed",
    }

    # Rotation and speed channels — not on/off lighting at all
    NON_LIGHT_TYPES = {8, 9, 12, 13}

    HUE_NAMES = ((15, "red"), (45, "orange"), (70, "yellow"), (160, "green"),
                 (188, "teal"), (200, "cyan"), (250, "blue"), (290, "purple"),
                 (330, "pink"), (361, "red"))

    def __init__(self, spotify_player, tapo_controller=None, *,
                 map_dir=MAP_DIR, difficulty=DIFFICULTY, song=SONG,
                 channels=None, bright=BRIGHT, dim=DIM,
                 min_call_gap=MIN_CALL_GAP, min_gap=MIN_GAP, color_lead=COLOR_LEAD,
                 lead_time=LEAD_TIME, offset=OFFSET,
                 sync_timeout=SYNC_TIMEOUT, max_late=MAX_LATE):
        if spotify_player is None:
            raise ValueError("LightShow requires a SpotifyPlayer: the show is timed "
                             "against playback position, not against the wall clock.")

        self._player = spotify_player
        self._tapo = tapo_controller  # None = build and connect our own on run()

        self._map_dir = map_dir
        self._difficulty = difficulty
        self._song = song
        override = channels if channels is not None else CHANNELS
        self._channels = dict(override) if override else None  # None = pick per map
        self._bright = bright
        self._dim = dim
        self._fade_time = FADE_TIME
        self._min_call_gap = min_call_gap
        self._power_off_min = POWER_OFF_MIN
        self._wake_lead = WAKE_LEAD
        self._min_gap = min_gap
        self._color_lead = color_lead
        self._lead_time = lead_time
        self._offset = offset
        self._sync_timeout = sync_timeout
        self._max_late = max_late

        self._schedules = None
        self._palette = None
        self._t0 = None  # monotonic time of song position 0.0s

    # ── classification and colour ──────────────────────────────────

    @classmethod
    def _kind(cls, value):
        """Classify a Beat Saber light value into what the bulb should do."""
        if value in cls.STEADY_VALUES or value in cls.TRANSITION_VALUES:
            return "on"
        if value in cls.FLASH_VALUES:
            return "flash"          # peaks, then stays lit
        if value in cls.FADE_VALUES:
            return "fade"           # peaks, then decays to black
        return "off"

    @classmethod
    def _event_name(cls, et):
        return cls.EVENT_NAMES.get(et, f"et={et}") if et is not None else "all channels"

    @classmethod
    def _color_name(cls, color):
        """Human name for a palette entry, for the log."""
        if color is None:
            return ""
        if color[0] == "ct":
            return "white"
        _, hue, sat = color
        if sat < 15:
            return "white"
        for limit, name in cls.HUE_NAMES:
            if hue < limit:
                return name
        return "red"

    def _load_palette(self, custom):
        """Map the beatmap's environment colours to bulb settings.

        Entries are tagged with the call they need: ("hs", hue, saturation) for
        the colours, ("ct", kelvin) for white — a desaturated hue comes out warm
        yellow on an L530, so white has to go out as a colour temperature.

        Boost entries fall back to their non-boost counterpart, and the whole
        palette falls back to the stock Beat Saber colours, so a map that
        defines nothing still lights up in red and blue.
        """
        palette = {}
        for key, custom_key in self.CUSTOM_KEYS.items():
            rgb = custom.get(custom_key)
            if rgb is None:
                rgb = custom.get(self.CUSTOM_KEYS[key.replace("_boost", "")])
            if rgb is None:
                r, g, b = self.DEFAULT_COLORS[key.replace("_boost", "")]
            else:
                r, g, b = rgb["r"], rgb["g"], rgb["b"]

            h, s, _ = colorsys.rgb_to_hsv(r, g, b)
            palette[key] = ("hs", round(h * 360), max(1, round(s * 100)))

        palette["white"] = ("ct", WHITE_TEMP)
        return palette

    def _color_for(self, value, boost):
        """Palette entry a light value should use, or None when it is an off event."""
        if value in self.WHITE_VALUES:
            return self._palette["white"]
        if value in self.RIGHT_VALUES:
            return self._palette["right_boost" if boost else "right"]
        if value in self.LEFT_VALUES:
            return self._palette["left_boost" if boost else "left"]
        return None

    # ── map parsing ────────────────────────────────────────────────

    def _read_map(self):
        """Load the map, normalising v2 and v3 into one shape.

        v3 keeps lighting in `basicBeatmapEvents` with short keys (b/et/i) and
        splits out BPM, boosts and notes; v2 puts everything in `_events` with
        `_time`/`_type`/`_value`, where type 5 is the colour boost and type 100
        is a BPM change. Everything downstream sees the v3-shaped result.
        """
        with open(os.path.join(self._map_dir, "Info.dat"), encoding="utf-8") as f:
            info = json.load(f)

        beatmap = None
        for bms in info.get("_difficultyBeatmapSets", []):
            for bm in bms.get("_difficultyBeatmaps", []):
                if bm["_difficulty"].lower() == self._difficulty.lower():
                    beatmap = bm
        if beatmap is None:
            have = ", ".join(bm["_difficulty"]
                             for bms in info.get("_difficultyBeatmapSets", [])
                             for bm in bms.get("_difficultyBeatmaps", []))
            raise KeyError(f"no '{self._difficulty}' difficulty in "
                           f"{os.path.basename(self._map_dir)} — have: {have}")

        with open(os.path.join(self._map_dir, beatmap["_beatmapFilename"]),
                  encoding="utf-8") as f:
            data = json.load(f)

        bpm = float(info["_beatsPerMinute"])
        custom = beatmap.get("_customData", {})

        if "_events" in data:                                    # v2
            version = data.get("_version", "2.x")
            events = [(float(e["_time"]), e["_type"], e["_value"])
                      for e in data["_events"] if e["_type"] not in (5, 100)]
            boosts = [(float(e["_time"]), bool(e["_value"]))
                      for e in data["_events"] if e["_type"] == 5]
            changes = [(float(e["_time"]), float(e["_value"]))
                       for e in data["_events"] if e["_type"] == 100]
            changes += [(float(c["_time"]), float(c.get("_BPM", c.get("_bpm", bpm))))
                        for c in data.get("_customData", {}).get("_BPMChanges", [])]
            notes = [float(n["_time"]) for n in data.get("_notes", [])]
        else:                                                    # v3
            version = data.get("version", "3.x")
            events = [(float(e["b"]), e["et"], e.get("i", 0))
                      for e in data.get("basicBeatmapEvents", [])]
            boosts = [(float(e["b"]), bool(e.get("o", False)))
                      for e in data.get("colorBoostBeatmapEvents", [])]
            changes = [(float(e["b"]), float(e["m"]))
                       for e in data.get("bpmEvents", [])]
            notes = [float(n["b"]) for n in data.get("colorNotes", [])]

        events.sort()
        return dict(version=version, bpm=bpm, events=events, notes=notes,
                    bpm_changes=sorted(changes), boosts=sorted(boosts), custom=custom)

    def parse_map(self):
        """Build one schedule per light. Returns {light name: schedule}.

        A schedule entry is (seconds, event_type, level, kind, index, colour).
        """
        m = self._read_map()
        b2s = self._beat_to_seconds(m["bpm_changes"], m["bpm"])
        self._palette = self._load_palette(m["custom"])
        boosted = self._boost_lookup(m["boosts"])
        all_events = m["events"]
        if not all_events:
            raise ValueError(f"{os.path.basename(self._map_dir)} "
                             f"[{self._difficulty}] has no lighting events at all")

        print(f"Parsed {os.path.basename(self._map_dir)} [{self._difficulty}] "
              f"v{m['version']}: {len(all_events)} events, {m['bpm']:.0f} bpm")
        shown = ", ".join(f"{k}={self._color_name(v)}({v[1]}°)"
                          for k, v in self._palette.items() if k != "white")
        stock = not any(k in m["custom"] for k in self.CUSTOM_KEYS.values())
        print(f"  palette: {shown}, white={WHITE_TEMP}K"
              f"{' (map defines none — stock colours)' if stock else ''}")

        channels = self._channels or self._select_channels(all_events, b2s)

        self._schedules = {}
        for light, channel in channels.items():
            events = (all_events if channel is None else
                      [e for e in all_events if e[1] == channel])
            timed = sorted((b2s(b), et, self._kind(v), self._color_for(v, boosted(b)))
                           for b, et, v in events)
            if not any(k != "off" for _, _, k, _ in timed):
                print(f"  [warn] {light:<13}← ch{channel} "
                      f"{self._event_name(channel)}: no lit events, skipping")
                continue

            schedule, stats = self._build_schedule(timed)
            self._schedules[light] = schedule
            self._report_light(light, channel, timed, schedule, stats)

        return self._schedules

    # ── Beat Saber's light curves ──────────────────────────────────

    @staticmethod
    def alpha(kind, dt):
        """Light intensity `dt` seconds after an event, as Beat Saber renders it.

        0 is black and 1.0 is a plain On; a flash or fade peaks at 1.2 first.
        Ported from ArcViewer's GetFlashColor / GetFadeColor.
        """
        if kind == "off":
            return 0.0
        if kind == "on":
            return 1.0
        if kind == "flash":
            if dt >= FLASH_SETTLE:
                return 1.0                       # settled, and stays lit
            t = dt / FLASH_SETTLE
            return FLASH_INTENSITY + (1.0 - FLASH_INTENSITY) * (1 - (1 - t) ** 3)
        if dt >= FADE_TIME:
            return 0.0
        return FLASH_INTENSITY * 2 ** (-10 * dt / FADE_TIME)

    @staticmethod
    def _brightness(a):
        """Intensity → bulb brightness. 0 means the light is out."""
        if a < DARK_ALPHA:
            return 0
        return max(MIN_LEVEL, min(BRIGHT, int(round(a * (BRIGHT / FLASH_INTENSITY)
                                                    / STEP)) * STEP))

    def _levels(self, timed):
        """Every brightness the map asks this channel for, as (seconds, level).

        One entry at each event, plus the shape that follows it: a fade gets a
        decay step and then a go-dark, a flash gets its settle back to On. Trailing
        steps are dropped when the next event lands first, since that event
        overrides them anyway.
        """
        out = []
        if timed and timed[0][0] > 0:
            out.append((0.0, 0))     # nothing has happened yet, so the light is out
        for i, (t, _, kind, _) in enumerate(timed):
            nxt = timed[i + 1][0] if i + 1 < len(timed) else float("inf")
            out.append((t, self._brightness(self.alpha(kind, 0.0))))

            if kind == "fade":
                # halves every 0.15s, so one step on the way down then black
                for dt in (0.30, self._dark_after_fade()):
                    if t + dt < nxt:
                        out.append((t + dt, self._brightness(self.alpha(kind, dt))))
            elif kind == "flash" and t + FLASH_SETTLE < nxt:
                out.append((t + FLASH_SETTLE, self._brightness(1.0)))
        return out

    @staticmethod
    def _dark_after_fade():
        """When a fade crosses DARK_ALPHA — 1.2 * 2**(-10t/1.5) == DARK_ALPHA."""
        return min(FADE_TIME, FADE_TIME * math.log2(FLASH_INTENSITY / DARK_ALPHA) / 10)

    # ── channel selection ──────────────────────────────────────────

    def _profiles(self, all_events, b2s, step=0.05):
        """A lit/dark sample every `step` seconds, per channel."""
        by_channel = collections.defaultdict(list)
        for b, et, v in all_events:
            if et not in self.NON_LIGHT_TYPES:
                by_channel[et].append((b2s(b), self._kind(v)))

        end = max((ts[-1][0] for ts in by_channel.values()), default=0.0)
        profiles = {}
        for et, events in by_channel.items():
            events.sort()
            times = [t for t, _ in events]
            samples, t = [], 0.0
            while t < end:
                i = bisect.bisect_right(times, t) - 1
                a = self.alpha(events[i][1], t - events[i][0]) if i >= 0 else 0.0
                samples.append(a >= DARK_ALPHA)
                t += step
            profiles[et] = samples
        return profiles

    def _select_channels(self, all_events, b2s):
        """Pick one channel per light, favouring channels that differ from each other.

        Maps often drive two groups identically — Nightmare's left and right lasers
        agree 74% of the time — and spending two bulbs on those wastes half the
        show. Drop the most duplicated channel until one is left per light, then
        give the two most dissimilar to the lights that share a room.
        """
        profiles = {et: p for et, p in self._profiles(all_events, b2s).items() if any(p)}
        lights = [l for l in LIGHTS]
        assignment = {}

        # A channel that barely fires is not "contrast", it is a dead bulb. Some maps
        # leave a group with a handful of events all song; drop those before comparing.
        counts = collections.Counter(et for _, et, _ in all_events if et in profiles)
        busiest = max(counts.values(), default=0)
        for et, n in sorted(counts.items()):
            if n < max(MIN_CHANNEL_EVENTS, busiest * MIN_CHANNEL_SHARE) and len(profiles) > 1:
                print(f"  ignoring ch{et} ({self._event_name(et)}): only {n} events all song")
                del profiles[et]

        def similarity(a, b):
            pa, pb = profiles[a], profiles[b]
            n = min(len(pa), len(pb))
            return sum(1 for i in range(n) if pa[i] == pb[i]) / n if n else 1.0

        kept = sorted(profiles)
        while len(kept) > len(lights) and len(kept) > 1:
            worst = max(((a, b) for i, a in enumerate(kept) for b in kept[i + 1:]),
                        key=lambda ab: similarity(*ab))
            # drop whichever of the pair is the more redundant against the rest
            drop = max(worst, key=lambda c: sum(similarity(c, o) for o in kept if o != c))
            kept.remove(drop)
            print(f"  dropping ch{drop} ({self._event_name(drop)}): "
                  f"{100 * similarity(*worst):.0f}% identical to "
                  f"ch{[c for c in worst if c != drop][0]}")

        paired = [l for l in PAIRED if l in lights]
        assignment = {}
        if len(paired) == 2 and len(kept) >= 2:
            # The room you are actually in gets the punchiest channel — the one lit
            # least of the time, so its blackouts are the most dramatic — plus
            # whichever surviving channel least resembles it.
            punchy = min(kept, key=lambda c: sum(profiles[c]) / len(profiles[c]))
            partner = min((c for c in kept if c != punchy),
                          key=lambda c: similarity(punchy, c))
            assignment[paired[0]], assignment[paired[1]] = punchy, partner
            kept = [c for c in kept if c not in (punchy, partner)]
            print(f"  {paired[0]} ← ch{punchy} (lit "
                  f"{100 * sum(profiles[punchy]) / len(profiles[punchy]):.0f}% of the song, "
                  f"punchiest) / {paired[1]} ← ch{partner}, "
                  f"{100 * similarity(punchy, partner):.0f}% alike")

        # fewer usable channels than bulbs: let the extras mirror, rather than sit dead
        spare = list(kept)
        for light in lights:
            if light in assignment:
                continue
            if not spare:
                spare = sorted(profiles)
            assignment[light] = spare.pop(0)
        return assignment

    def _build_schedule(self, timed):
        """Turn one channel's events into bulb actions.

        The map's events are the schedule — there is no grid. Every brightness the
        map asks for becomes a candidate, repeats are dropped, and what is left is
        rate limited to what a single bulb can serve. A dark stretch long enough to
        be worth it switches the bulb off rather than dimming it, which is what
        makes the gaps between flashes actually black.
        """
        levels = self._levels(timed)
        lit = [(t, color) for t, _, kind, color in timed
               if kind != "off" and color is not None]
        lit_times = [t for t, _ in lit]

        def color_at(t):
            i = bisect.bisect_right(lit_times, t)
            return lit[i - 1][1] if i else None

        channel = timed[0][1] if timed else None
        schedule = []
        stats = collections.Counter()
        last_color = last_level = None
        last_sent = -1e9          # when the bulb was last given work
        index = dropped = 0
        i = 0

        while i < len(levels):
            t, level = levels[i]
            i += 1
            if level == last_level:
                continue

            color = color_at(t) if level else None
            recolor = level and color is not None and color != last_color
            # a level costs one call, a level plus a hue change costs two, and a
            # blackout costs a dim and a power-off — pace against what it will take
            cost = 2 if (recolor or level == 0) else 1
            if t - last_sent < self._min_call_gap * cost:
                dropped += 1
                continue

            if level == 0:
                nxt = next((lt for lt, lv in levels[i:] if lv), None)
                if nxt is None or nxt - t >= self._power_off_min:
                    schedule.append((t, channel, "off", "dark", 0, None))
                    if nxt is not None:
                        schedule.append((nxt - self._wake_lead, channel,
                                         "on", "wake", 0, None))
                    stats["dark"] += 1
                    last_color = None       # a power cycle loses the colour
                else:
                    index += 1
                    schedule.append((t, channel, MIN_LEVEL, "level", index, None))
                    stats["levels"] += 1
                last_level, last_sent = 0, t
                continue

            index += 1
            send = color if recolor else None
            if recolor:
                stats["colors"] += 1
                for lead in (self._color_lead, self._color_lead / 2):
                    at = t - lead
                    if not schedule or at >= schedule[-1][0] + self._min_gap:
                        schedule.append((at, channel, None, "color", index, send))
                        send = None
                        break
                else:
                    stats["late_colors"] += 1
                last_color = color
            schedule.append((t, channel, level, "level", index, send))
            stats["levels"] += 1
            last_level, last_sent = level, t

        schedule.sort(key=lambda a: a[0])
        stats["dropped"] = dropped
        return schedule, stats

    def _report_light(self, light, channel, timed, schedule, stats):
        dur = max(a[0] for a in schedule)
        dark = 100 * sum(1 for a in schedule if a[3] == "dark") / max(1, stats["levels"])
        line = (f"  {light:<13}← ch{channel} {self._event_name(channel):<15}"
                f"{len(timed):>5} events → {len(schedule):>4} actions "
                f"({len(schedule) / dur:.2f}/s)   {stats['levels']} levels, "
                f"{stats['colors']} colours, {stats['dark']} blackouts")
        extra = []
        if stats["dropped"]:
            extra.append(f"{stats['dropped']} too fast for the bulb")
        if stats["late_colors"]:
            extra.append(f"{stats['late_colors']} late colour")
        print(line + (f"   ({', '.join(extra)})" if extra else ""))

    @staticmethod
    def _boost_lookup(boosts):
        """Return beat → bool, whether the colour boost palette is active."""
        def boosted(beat):
            on = False
            for b, state in boosts:
                if b <= beat: on = state
                else: break
            return on

        return boosted

    @staticmethod
    def _beat_to_seconds(changes, bpm):
        """Build a BPM-aware beat → seconds converter for this map."""
        if not changes or changes[0][0] > 0:
            changes = [(0.0, bpm)] + list(changes)

        anchors = []
        for i, (b, m) in enumerate(changes):
            if i == 0:
                anchors.append((b, 0.0, m))
            else:
                pb, pt, pm = anchors[-1]
                anchors.append((b, pt + (b - pb) / pm * 60.0, m))

        def b2s(beat):
            seg = anchors[0]
            for a in anchors:
                if a[0] <= beat: seg = a
                else: break
            return seg[1] + (beat - seg[0]) / seg[2] * 60.0

        return b2s

    # ── playback sync ──────────────────────────────────────────────

    async def anchor_to_song(self):
        """Monotonic timestamp at which the song was at position 0.0s.

        Spotify reports where the track actually is, so the show can be pinned to
        that instead of to whenever start_playback() happened to return — the two
        differ by however long the request and the device handoff took.
        """
        deadline = time.monotonic() + self._sync_timeout
        while time.monotonic() < deadline:
            before = time.monotonic()
            state = await asyncio.to_thread(self._player.get_playback_state)
            after = time.monotonic()

            if state and state.get("is_playing") and state.get("progress_ms") is not None:
                # the reported position was true somewhere inside the request window
                sampled_at = (before + after) / 2
                return sampled_at - state["progress_ms"] / 1000.0

            await asyncio.sleep(0.1)
        return None

    # ── logging ────────────────────────────────────────────────────

    @staticmethod
    def _lateness(late_ms):
        return "on time" if late_ms < ON_TIME else f"{late_ms:.0f}ms late"

    def _log(self, position, light, symbol, what, et, index, total, note):
        """One line. Only colour changes and problems get one — with four lights
        pulsing at ~4Hz each, logging every pulse is unreadable."""
        counter = f"#{index}/{total}" if index else ""
        print(f"  [{position:7.2f}s] {symbol} {light:<13}{what:<8}"
              f"{self._event_name(et):<15} {counter:<11}{note}")

    # ── the bulbs ──────────────────────────────────────────────────

    async def _connect(self):
        """Build and connect a controller if none was given, then check the lights exist."""
        if self._tapo is None:
            from tapo_controller import TapoController

            print("Connecting to lights…")
            self._tapo = TapoController()
            await self._tapo.connect_to_lights()

        missing = [n for n in self._schedules if n not in self._tapo._lights]
        if missing:
            raise KeyError(f"no such light(s): {', '.join(missing)} — "
                           f"have {', '.join(self._tapo._lights)}")

    async def _apply(self, light, level, color):
        """Push a colour and/or a brightness to one bulb.

        Colour first: an action carrying both is one the schedule could not send
        early, so the hue has to land before the light comes up. Routed through
        the controller's reconnect, since Tapo sessions expire and four of them
        running for six minutes will hit that.
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
                    # drop to the dark level first, so on() cannot flash bright later
                    await device.set_brightness(self._dim)
                await device.off()
            elif level == "on":
                await device.on()
            elif level is not None:
                await device.set_brightness(level)

        await self._tapo._with_reconnect(light, action)

    async def _prime(self, light):
        """Put a bulb at a known level and colour before the show starts."""
        await self._tapo._with_reconnect(light, lambda d: d.on())
        await self._apply(light, self._dim, self._palette["white"])

    async def _restore(self, light):
        """Put a bulb back the way the rest of the app expects to find it."""
        try:
            await self._tapo._with_reconnect(light, self._tapo._apply_mode)
        except Exception as exc:
            print(f"  [warn] could not restore {light}: {exc}")

    # ── the show ───────────────────────────────────────────────────

    async def run(self):
        schedules = self._schedules if self._schedules is not None else self.parse_map()
        if not schedules:
            print("Nothing to play.")
            return

        await self._connect()
        await asyncio.gather(*(self._prime(light) for light in schedules))

        # Nothing slow is left, so the song only starts once the lights are ready
        print(f"Starting song: {self._song}")
        await asyncio.to_thread(self._player.play_song, self._song)

        anchor = await self.anchor_to_song()
        if anchor is None:
            print("  [warn] Spotify never reported playback — starting the show now")
            anchor = time.monotonic()

        # one anchor for every light, so the four stay in sync with each other
        self._t0 = anchor + self._offset

        now = datetime.now()
        position = time.monotonic() - self._t0
        total_actions = sum(len(s) for s in schedules.values())
        print(f"▶ LIGHT SHOW START  {now:%H:%M:%S}.{now.microsecond // 1000:03d}  "
              f"(song at {position:+.2f}s, {len(schedules)} lights, "
              f"{total_actions} actions queued)")

        stats = await asyncio.gather(*(
            self._run_light(light, schedule) for light, schedule in schedules.items()
        ))

        print("Done.")
        for light, s in zip(schedules, stats):
            mean = sum(s["latencies"]) / len(s["latencies"]) if s["latencies"] else 0.0
            worst = max(s["latencies"], default=0.0)
            calls = sorted(s["durations"])
            call_mean = sum(calls) / len(calls) if calls else 0.0
            call_p90 = calls[int(len(calls) * 0.9)] if calls else 0.0
            print(f"  {light:<13}{s['levels']:>4} levels, {s['dark']:>2} dark, "
                  f"{s['colors']:>3} colours, {s['failed']:>2} failed, "
                  f"{s['skipped']:>3} skipped   "
                  f"drift {mean:.0f}/{worst:.0f}ms   "
                  f"bulb {call_mean:.0f}ms mean / {call_p90:.0f}ms p90 "
                  f"({1000 / call_mean if call_mean else 0:.1f} calls/s ceiling)")

        await asyncio.gather(*(self._restore(light) for light in schedules))

    async def _run_light(self, light, schedule):
        """Drive one bulb through its schedule. Logs colour changes and problems only."""
        total = max((a[4] for a in schedule), default=0)
        s = dict(levels=0, colors=0, dark=0, failed=0, skipped=0,
                 latencies=[], durations=[])
        wakes = [a[0] for a in schedule if a[3] == "wake"]
        level_now = self._dim  # what the bulb is showing, so we can skip a needless pre-dim
        since_color = 0

        for t, et, level, kind, index, color in schedule:
            fire_at = self._t0 + t - self._lead_time
            wait = fire_at - time.monotonic()
            if wait < -self._max_late and kind not in ("dark", "wake"):
                # dark/wake are exempt: they are rare, and a shed wake would leave
                # the bulb switched off for the rest of the song
                s["skipped"] += 1  # already behind; drop it rather than drift further
                continue
            if wait > 0:
                await asyncio.sleep(wait)

            # taken before the call, so this is scheduling drift, not bulb latency
            fired_at = time.monotonic()
            late_ms = max(0.0, (fired_at - fire_at) * 1000)
            s["latencies"].append(late_ms)

            if kind == "dark" and level_now == self._dim:
                level = "off-only"        # already dark, no need to dim first

            call_started = time.monotonic()
            try:
                await self._apply(light, level, color)
                s["durations"].append((time.monotonic() - call_started) * 1000)
            except Exception as exc:
                s["failed"] += 1
                self._log(fired_at - self._t0, light, "✗", "FAILED", et, index, total,
                          f"{type(exc).__name__}: {exc}")
                continue

            if kind == "dark":
                s["dark"] += 1
                i = bisect.bisect_right(wakes, fired_at - self._t0)
                until = (f"{wakes[i] + self._wake_lead - (t):.1f}s"
                         if i < len(wakes) else "end of song")
                self._log(fired_at - self._t0, light, "○", "dark", et, index, total,
                          f"lights out for {until}")
                continue
            if kind == "wake":
                level_now = self._dim   # pre-dimmed before the power-off, so it wakes dim
                continue

            if kind == "level":
                s["levels"] += 1
                since_color += 1
                level_now = level

            if color is not None:
                s["colors"] += 1
                note = self._lateness(late_ms)
                if since_color:
                    note += f", +{since_color} level" + ("s" if since_color > 1 else "")
                self._log(fired_at - self._t0, light, "◆", self._color_name(color),
                          et, index, total, f"({note})")
                since_color = 0

        return s


if __name__ == "__main__":
    from spotify_player import SpotifyPlayer

    asyncio.run(LightShow(SpotifyPlayer()).run())
