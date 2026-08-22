"""
light_show.py — bare minimum, no filtering, no dedup, no merging.
Reads every light event from a Beat Saber map and fires it at the right time,
pinned to where the song actually is in Spotify.
"""

import asyncio
import json
import os
import time
from datetime import datetime

# ═══════════════════════════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════════════════════════

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

MAP_DIR    = os.path.join(PROJECT_ROOT, "resources", "Afterlife")
DIFFICULTY = "Expert"
SONG       = "Afterlife - Avenged Sevenfold"
LIGHT_NAME = "Living Room"

LEAD_TIME  = 0.18   # fire this early to cover bulb latency
OFFSET     = 0.0    # positive = whole show runs later against the song

SYNC_TIMEOUT = 10.0  # give up waiting for Spotify to report playback
MAX_LATE     = 0.10  # drop events we are already this far behind on

# ═══════════════════════════════════════════════════════════════════


class LightShow:
    """Plays a Beat Saber map's light events on one bulb, synced to Spotify.

    The show is timed against real playback position, so a SpotifyPlayer is
    required — without one there is nothing to sync to.
    """

    ON_VALUES = {1, 2, 3, 5, 6, 7, 10, 11}

    def __init__(self, spotify_player, tapo_controller=None, *,
                 map_dir=MAP_DIR, difficulty=DIFFICULTY, song=SONG,
                 light_name=LIGHT_NAME, lead_time=LEAD_TIME, offset=OFFSET,
                 sync_timeout=SYNC_TIMEOUT, max_late=MAX_LATE):
        if spotify_player is None:
            raise ValueError("LightShow requires a SpotifyPlayer: the show is timed "
                             "against playback position, not against the wall clock.")

        self._player = spotify_player
        self._tapo = tapo_controller  # None = build and connect our own on run()

        self._map_dir = map_dir
        self._difficulty = difficulty
        self._song = song
        self._light_name = light_name
        self._lead_time = lead_time
        self._offset = offset
        self._sync_timeout = sync_timeout
        self._max_late = max_late

        self._schedule = None
        self._t0 = None  # monotonic time of song position 0.0s

    # ── map parsing ────────────────────────────────────────────────

    def parse_map(self):
        """Read every light event out of the map as (seconds, event_type, is_on)."""
        with open(os.path.join(self._map_dir, "Info.dat"), encoding="utf-8") as f:
            info = json.load(f)
        bpm = float(info["_beatsPerMinute"])

        diff_file = None
        for bms in info.get("_difficultyBeatmapSets", []):
            for bm in bms.get("_difficultyBeatmaps", []):
                if bm["_difficulty"].lower() == self._difficulty.lower():
                    diff_file = bm["_beatmapFilename"]

        with open(os.path.join(self._map_dir, diff_file), encoding="utf-8") as f:
            diff_data = json.load(f)

        b2s = self._beat_to_seconds(diff_data, bpm)

        # Every single event, no filtering
        schedule = []
        for e in diff_data.get("basicBeatmapEvents", []):
            t = b2s(float(e["b"]))
            is_on = e.get("i", 0) in self.ON_VALUES
            schedule.append((t, e["et"], is_on))

        schedule.sort()
        print(f"Total events: {len(schedule)}, song length: {schedule[-1][0]:.1f}s")
        self._schedule = schedule
        return schedule

    @staticmethod
    def _beat_to_seconds(diff_data, bpm):
        """Build a BPM-aware beat → seconds converter for this map."""
        bpm_changes = sorted(diff_data.get("bpmEvents", []), key=lambda e: e["b"])
        if not bpm_changes or bpm_changes[0]["b"] > 0:
            bpm_changes = [{"b": 0.0, "m": bpm}] + bpm_changes

        anchors = []
        for i, c in enumerate(bpm_changes):
            if i == 0:
                anchors.append((c["b"], 0.0, c["m"]))
            else:
                pb, pt, pm = anchors[-1]
                anchors.append((c["b"], pt + (c["b"] - pb) / pm * 60.0, c["m"]))

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

    # ── the show ───────────────────────────────────────────────────

    async def _connect_light(self):
        """Return the bulb handle, building our own controller if none was given."""
        if self._tapo is None:
            from tapo_controller import TapoController

            print("Connecting to lights…")
            self._tapo = TapoController()
            await self._tapo.connect_to_lights()
        return self._tapo._lights[self._light_name]

    async def run(self):
        schedule = self._schedule if self._schedule is not None else self.parse_map()
        light = await self._connect_light()

        # Nothing slow is left, so the song only starts once the lights are ready
        print(f"Starting song: {self._song}")
        await asyncio.to_thread(self._player.play_song, self._song)

        anchor = await self.anchor_to_song()
        if anchor is None:
            print("  [warn] Spotify never reported playback — starting the show now")
            anchor = time.monotonic()

        self._t0 = anchor + self._offset

        now = datetime.now()
        position = time.monotonic() - self._t0
        print(f"▶ LIGHT SHOW START  {now:%H:%M:%S}.{now.microsecond // 1000:03d}  "
              f"(song at {position:+.2f}s, {len(schedule)} events queued)")

        skipped = 0
        for t, et, is_on in schedule:
            fire_at = self._t0 + t - self._lead_time
            wait = fire_at - time.monotonic()
            if wait < -self._max_late:
                skipped += 1  # already behind; drop it rather than fall further out of sync
                continue
            if wait > 0:
                await asyncio.sleep(wait)

            try:
                if is_on:
                    await light.set_brightness(100)
                else:
                    await light.off()
            except Exception as exc:
                print(f"  [warn] {exc}")

            elapsed = time.monotonic() - self._t0
            print(f"  t={elapsed:6.1f}s  et={et}  {'ON ' if is_on else 'off'}")

        print(f"Done. Fired {len(schedule) - skipped}, skipped {skipped} late events.")


if __name__ == "__main__":
    from spotify_player import SpotifyPlayer

    asyncio.run(LightShow(SpotifyPlayer()).run())
