"""
show.py — the show file: cues you placed, and how they become bulb levels.

A note says "from here, for this long, hold this light at this brightness and
colour", optionally fading up at its start and down at its end instead of
snapping. Outside its notes a light is off. That is the whole model. Everything else — pacing the calls, switching a
bulb off through a long blackout, getting the hue in before the light comes up —
is the player's job.
"""

import json
import os

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SHOW_DIR = os.path.join(PROJECT_ROOT, "resources", "shows")

LIGHTS = ("Living Room", "Vibe", "Kitchen", "Bathroom")

# Every light has two layers. The base layer carries long, static washes; the
# top layer cuts across them with short accents. Where both have a note the top
# one wins, and when it ends the base resumes wherever it had got to.
BASE, TOP = 0, 1
LAYERS = (TOP, BASE)

VERSION = 1
FADE_STEP = 0.34   # seconds between interpolated points inside a fade; one bulb
                   # call takes ~300ms, so finer than this is wasted work
DEFAULT_DUR = 1.0  # how long a freshly placed note lasts, in seconds


def _as_color(value):
    """JSON gives lists; the player compares colours by equality, so normalise."""
    return tuple(value) if isinstance(value, (list, tuple)) else None


class Show:
    """A song, the lights it drives, and the cues placed against its timeline."""

    def __init__(self, song="", audio="", track_uri=None, duration=0.0,
                 lights=LIGHTS, cues=None, offset=0.0, name="untitled",
                 presets=None, bpm=0.0, beat_offset=0.0):
        self.song = song
        self.audio = audio            # path relative to the project root
        self.track_uri = track_uri    # the exact Spotify recording, if known
        self.duration = float(duration)
        self.lights = list(lights)
        self.cues = list(cues or [])
        self.offset = float(offset)   # nudge the whole show against the song
        self.presets = list(presets or [])   # saved level+colour looks
        self.bpm = float(bpm or 0.0)         # the grid notes snap to
        self.beat_offset = float(beat_offset or 0.0)
        self.name = name

    # ── files ──────────────────────────────────────────────────────

    @classmethod
    def path_for(cls, name):
        if os.path.isabs(name) or name.endswith(".json"):
            return name if os.path.isabs(name) else os.path.join(PROJECT_ROOT, name)
        return os.path.join(SHOW_DIR, f"{name}.json")

    @classmethod
    def load(cls, name):
        path = cls.path_for(name)
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return cls(song=data.get("song", ""), audio=data.get("audio", ""),
                   track_uri=data.get("track_uri"), duration=data.get("duration", 0.0),
                   lights=data.get("lights", LIGHTS), cues=data.get("cues", []),
                   offset=data.get("offset", 0.0), presets=data.get("presets", []),
                   bpm=data.get("bpm", 0.0), beat_offset=data.get("beatOffset", 0.0),
                   name=os.path.splitext(os.path.basename(path))[0])

    def save(self, name=None):
        """Write atomically — the editor saves on every edit and a truncated
        show file would lose the session's work."""
        path = self.path_for(name or self.name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=1)
        os.replace(tmp, path)
        return path

    def to_dict(self):
        return dict(version=VERSION, song=self.song, audio=self.audio,
                    track_uri=self.track_uri, duration=self.duration,
                    offset=self.offset, lights=self.lights, presets=self.presets,
                    bpm=self.bpm, beatOffset=self.beat_offset,
                    cues=sorted(self.cues, key=lambda c: (c["t"], c["light"])))

    # ── cues → levels ──────────────────────────────────────────────

    def cues_for(self, light):
        return sorted((c for c in self.cues if c["light"] == light),
                      key=lambda c: c["t"])

    def notes_on(self, light, layer):
        return [n for n in self.cues_for(light) if int(n.get("layer", BASE)) == layer]

    def _layer_points(self, light, layer, step=FADE_STEP):
        """(time, brightness) points for one layer of one light, notes expanded.

        A note lights from its start for its length and then goes dark, so a
        light is only ever on where a note puts it. A fade ramps up from black
        over the first `fade` seconds instead of snapping on. Where a later note
        starts before this one ends, the later one wins and no blackout is
        inserted between them.
        """
        notes = self.notes_on(light, layer)
        if not notes:
            return []

        points = []
        if notes[0]["t"] > 0:
            points.append((0.0, 0))          # dark until the first note

        for i, note in enumerate(notes):
            start = float(note["t"])
            level = int(note["level"])
            fade = float(note.get("fade") or 0.0)     # fade in
            following = float(notes[i + 1]["t"]) if i + 1 < len(notes) else None

            end = start + float(note.get("dur") or DEFAULT_DUR)
            if following is not None:
                end = min(end, following)    # overlapped: the next note takes over

            fade_out = float(note.get("fadeOut") or 0.0)
            span = max(1e-3, end - start)
            if fade + fade_out > span:
                # a note cannot spend longer fading than it lasts
                squeeze = span / (fade + fade_out)
                fade, fade_out = fade * squeeze, fade_out * squeeze

            if fade > 0:
                steps = max(1, int(round(fade / step)))
                for k in range(1, steps + 1):
                    at = start + fade * k / steps
                    if at >= end:
                        break
                    points.append((at, int(round(level * k / steps))))
            else:
                points.append((start, level))

            if fade_out > 0:
                # ramp down into the blackout that closes the note
                began = end - fade_out
                steps = max(1, int(round(fade_out / step)))
                for k in range(1, steps):
                    at = began + fade_out * k / steps
                    if at > start:
                        points.append((at, int(round(level * (1 - k / steps)))))

            if following is None or following > end + 1e-6:
                points.append((end, 0))      # the note is over: go dark
        return points

    def _layer_spans(self, light, layer):
        """(start, end, colour) for each note on a layer, ends clamped by the next."""
        notes = self.notes_on(light, layer)
        spans = []
        for i, note in enumerate(notes):
            start = float(note["t"])
            end = start + float(note.get("dur") or DEFAULT_DUR)
            if i + 1 < len(notes):
                end = min(end, float(notes[i + 1]["t"]))
            spans.append((start, end, _as_color(note.get("color"))))
        return spans

    @staticmethod
    def _at(points, t):
        """The value of a step function at t — points are (time, value), sorted."""
        found = 0
        for at, value in points:
            if at <= t + 1e-9:
                found = value
            else:
                break
        return found

    def timeline(self, light):
        """The light's actual state over time as (seconds, level, colour).

        Both layers are expanded independently, then merged: wherever a top note
        is running it decides the level and the colour, and everywhere else the
        base layer shows through. Because the base is evaluated at the moment the
        top note ends, a long base wash resumes exactly where it had got to
        rather than restarting.
        """
        top_points = self._layer_points(light, TOP)
        base_points = self._layer_points(light, BASE)
        top_spans = self._layer_spans(light, TOP)
        base_spans = self._layer_spans(light, BASE)
        if not top_points and not base_points:
            return []

        moments = {t for t, _ in top_points} | {t for t, _ in base_points}
        for start, end, _ in top_spans:
            moments.update((start, end))     # the seams where control changes hands

        def covering(spans, t):
            for start, end, color in spans:
                if start <= t + 1e-9 < end:
                    return color
                if start > t:
                    break
            return None

        out = []
        for t in sorted(moments):
            on_top = any(s <= t + 1e-9 < e for s, e, _ in top_spans)
            if on_top:
                level, color = self._at(top_points, t), covering(top_spans, t)
            else:
                level, color = self._at(base_points, t), covering(base_spans, t)
            if out and out[-1][1] == level and out[-1][2] == color:
                continue                      # nothing actually changed here
            out.append((t, level, color))
        return out

    def levels_for(self, light):
        return [(t, level) for t, level, _ in self.timeline(light)]

    def colors_for(self, light):
        return [(t, color) for t, _, color in self.timeline(light) if color]

    # ── describing ─────────────────────────────────────────────────

    def summary(self):
        lines = [f"{self.name}: {self.song or '(no song)'}  "
                 f"{self.duration:.1f}s, {len(self.cues)} notes"]
        for light in self.lights:
            top = self.notes_on(light, TOP)
            base = self.notes_on(light, BASE)
            lit = sum(1 for _, level, _ in self.timeline(light) if level)
            lines.append(f"  {light:<13}{len(top):>4} top + {len(base):>3} base notes, "
                         f"{lit} state changes")
        return "\n".join(lines)
