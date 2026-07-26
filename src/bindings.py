"""
Action bindings: button/keyword -> a specific class method, bound directly.

Adding a new capability means writing a method on some class and referencing
it in build_actions() below -- no keyword to invent, no dispatcher to edit.
"""

import asyncio
import sys
from dataclasses import dataclass
from functools import partial
from typing import Callable

@dataclass
class Action:
    fn: Callable
    say: object = None  # None: silent, True: say fn's return value, str: say this phrase


async def run_action(action: Action, sound):
    """Play the click sound, run the bound function, then speak if configured."""
    try:
        sound.play()
        result = action.fn()
        if asyncio.iscoroutine(result):
            result = await result
        if action.say is True:
            sound.say(result)
        elif action.say:
            sound.say(action.say)
    except Exception as e:
        print(f"  ⚠ Error: {e}")


def build_actions(tapo, controller, spotify, bluetooth, phone):
    """Define every action once, bound directly to its class method."""
    return {
        "kitchen":     Action(partial(tapo.toggle, "Kitchen")),
        "bathroom":    Action(partial(tapo.toggle, "Bathroom")),
        "living":      Action(partial(tapo.toggle, "Living Room")),
        "vibe":        Action(partial(tapo.toggle, "Vibe")),
        "all_on":      Action(tapo.all_on),
        "all_off":     Action(tapo.all_off, say="all_off"),
        "night_mode":  Action(tapo.toggle_night_mode),
        "cycle_mode":  Action(controller.cycle_mode, say=True),
        "play_song_1":   Action(partial(spotify.play_song, "Afterlife - Avenged Sevenfold"), say="play_song"),
        "play_song_2": Action(partial(spotify.play_song, "Holiday - Green Day"), say="play_song"),
        "play_song_3": Action(partial(spotify.play_song, "Automatic Sun - The Warning"), say="play_song"),
        "play_song_4": Action(partial(spotify.play_song, "Reason - Selah Sue"), say="play_song"),
        "increase_volume": Action(spotify.increase_volume),
        "decrease_volume": Action(spotify.decrease_volume),
        "toggle_pause_resume_song":  Action(spotify.toggle_pause_resume),
        "restart_song": Action(spotify.restart_song),
        "skip_song": Action(spotify.skip_song),
        "toggle_speaker_connection": Action(bluetooth.toggle_speaker_connection),
        "disconnect_xbox_controller": Action(bluetooth.disconnect_xbox_controller),
        "exit": Action(sys.exit),
        "toggle_instagram": Action(phone.toggle_instagram),
    }


def build_command_map(actions):
    """Terminal keyword -> Action."""
    return {
        "kitchen":          actions["kitchen"],
        "bathroom":         actions["bathroom"],
        "living":           actions["living"],
        "vibe":             actions["vibe"],
        "on":               actions["all_on"],
        "off":              actions["all_off"],
        "lights mode":      actions["night_mode"],
        "controller mode":  actions["cycle_mode"],
        "toggle pause":     actions["toggle_pause_resume_song"],
        "exit":             actions["exit"],
    }


def build_button_maps(actions):
    """Controller mode -> button -> Action."""
    return {
        "controller_mode_lights": {
            "a":     actions["kitchen"],
            "b":     actions["bathroom"],
            "y":     actions["living"],
            "x":     actions["vibe"],
            "rb":    actions["all_on"],
            "lb":    actions["all_off"],
            "start": actions["night_mode"],
            "back":  actions["cycle_mode"],
        },
        "controller_mode_bluetooth": {
            "a":     actions["toggle_pause_resume_song"],
            "y":     actions["toggle_speaker_connection"],
            "x":     actions["restart_song"],
            "b":     actions["skip_song"],
            "start": actions["disconnect_xbox_controller"],
            "back":  actions["cycle_mode"],
            "up":    actions["play_song_1"],
            "left":  actions["play_song_2"],
            "right": actions["play_song_3"],
            "down":  actions["play_song_4"],
            "lb":    actions["decrease_volume"],
            "rb":    actions["increase_volume"],
            "L":     actions["toggle_instagram"]
        },
    }