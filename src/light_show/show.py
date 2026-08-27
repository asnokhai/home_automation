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
                 presets=None):
        self.song = song
        self.audio = audio            # path relative to the project root
        self.track_uri = track_uri    # the exact Spotify recording, if known
        self.duration = float(duration)
        self.lights = list(lights)
        self.cues = list(cues or [])
        self.offset = float(offset)   # nudge the whole show against the song
        self.presets = list(presets or [])   # saved level+colour looks
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
                    cues=sorted(self.cues, key=lambda c: (c["t"], c["light"])))

    # ── cues → levels ──────────────────────────────────────────────

    def cues_for(self, light):
        return sorted((c for c in self.cues if c["light"] == light),
                      key=lambda c: c["t"])

    def levels_for(self, light, step=FADE_STEP):
        """(time, brightness) points for one light, notes expanded.

        A note lights from its start for its length and then goes dark, so a
        light is only ever on where a note puts it. A fade ramps up from black
        over the first `fade` seconds instead of snapping on. Where a later note
        starts before this one ends, the later one wins and no blackout is
        inserted between them.
        """
        notes = self.cues_for(light)
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

    def colors_for(self, light):
        """(time, colour) for one light — a cue without a colour keeps the last."""
        return [(float(c["t"]), _as_color(c.get("color")))
                for c in self.cues_for(light) if c.get("color")]

    # ── describing ─────────────────────────────────────────────────

    def summary(self):
        lines = [f"{self.name}: {self.song or '(no song)'}  "
                 f"{self.duration:.1f}s, {len(self.cues)} notes"]
        for light in self.lights:
            notes = self.cues_for(light)
            lit = sum(float(n.get("dur") or DEFAULT_DUR) for n in notes)
            fades = sum(1 for n in notes if n.get("fade"))
            lines.append(f"  {light:<13}{len(notes):>4} notes, {lit:>6.1f}s lit "
                         f"({fades} fading)")
        return "\n".join(lines)
