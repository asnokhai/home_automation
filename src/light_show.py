"""
beatsaber_lightshow.py — bare minimum, no filtering, no dedup, no merging.
Just reads every light event from the map and fires it at the right time.
"""

import asyncio
import json
import os
import time

# ═══════════════════════════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════════════════════════

MAP_DIR    = r"resources/Afterlife"
DIFFICULTY = "Expert"
LEAD_TIME  = 0.18
OFFSET     = 0.0

# ═══════════════════════════════════════════════════════════════════

ON_VALUES = {1, 2, 3, 5, 6, 7, 10, 11}


def parse_map(map_dir, difficulty):
    with open(os.path.join(map_dir, "Info.dat"), encoding="utf-8") as f:
        info = json.load(f)
    bpm = float(info["_beatsPerMinute"])

    diff_file = None
    for bms in info.get("_difficultyBeatmapSets", []):
        for bm in bms.get("_difficultyBeatmaps", []):
            if bm["_difficulty"].lower() == difficulty.lower():
                diff_file = bm["_beatmapFilename"]

    with open(os.path.join(map_dir, diff_file), encoding="utf-8") as f:
        diff_data = json.load(f)

    # BPM-aware beat → seconds
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

    # Every single event, no filtering
    schedule = []
    for e in diff_data.get("basicBeatmapEvents", []):
        t = b2s(float(e["b"]))
        is_on = e.get("i", 0) in ON_VALUES
        schedule.append((t, e["et"], is_on))

    schedule.sort()
    print(f"Total events: {len(schedule)}, song length: {schedule[-1][0]:.1f}s")
    return schedule


async def run_show():
    from tapo_controller import TapoController

    schedule = parse_map(MAP_DIR, DIFFICULTY)

    print("Connecting to lights…")
    ctrl = TapoController()
    await ctrl.connect_to_lights()
    light = ctrl._lights["Living Room"]

    print("Starting in 3s — hit play now!")
    await asyncio.sleep(3)

    t0 = time.monotonic() + OFFSET

    for t, et, is_on in schedule:
        fire_at = t0 + t - LEAD_TIME
        wait = fire_at - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)

        try:
            if is_on:
                await light.set_brightness(100)
            else:
                await light.off()
        except Exception as exc:
            print(f"  [warn] {exc}")

        elapsed = time.monotonic() - t0
        print(f"  t={elapsed:6.1f}s  et={et}  {'ON ' if is_on else 'off'}")
    print("Done.")


if __name__ == "__main__":
    asyncio.run(run_show())