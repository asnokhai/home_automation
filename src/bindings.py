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
        "select_controller_mode_0": Action(partial(controller.select_mode, 0), say="controller_mode_lights"),
        "select_controller_mode_1": Action(partial(controller.select_mode, 1), say="controller_mode_bluetooth"),
        "select_controller_mode_2": Action(partial(controller.select_mode, 2), say="controller_mode_phone"),
        "kitchen":     Action(partial(tapo.toggle, "Kitchen")),
        "bathroom":    Action(partial(tapo.toggle, "Bathroom")),
        "living":      Action(partial(tapo.toggle, "Living Room")),
        "vibe":        Action(partial(tapo.toggle, "Vibe")),
        "all_on":      Action(tapo.all_on),
        "all_off":     Action(tapo.all_off, say="all_off"),
        "night_mode":  Action(tapo.toggle_night_mode),

        "play_song_1":   Action(partial(spotify.play_song, "Afterlife - Avenged Sevenfold"), say="play_song"),
        "play_song_2": Action(partial(spotify.play_song, "Holiday - Green Day"), say="play_song"),
        "play_song_3": Action(partial(spotify.play_song, "Automatic Sun - The Warning"), say="play_song"),
        "play_song_4": Action(partial(spotify.play_song, "Reason - Selah Sue"), say="play_song"),
        "increase_volume": Action(spotify.increase_volume),
        "decrease_volume": Action(spotify.decrease_volume),
        "toggle_pause_resume_song":  Action(spotify.toggle_pause_resume),
        "restart_song": Action(spotify.restart_song),
        "skip_song": Action(spotify.skip_song),
        "play_previous_song": Action(spotify.play_previous_song),
        "cycle_playback_devices": Action(spotify.cycle_playback_devices),
        "connect_to_speaker": Action(bluetooth.connect_to_speaker),
        "disconnect_from_speaker": Action(bluetooth.disconnect_from_speaker, say="disconnect_from_speaker"),
        "disconnect_xbox_controller": Action(bluetooth.disconnect_xbox_controller),
        "exit": Action(sys.exit),

        "toggle_instagram": Action(phone.toggle_instagram),
        "set_alarm": Action(partial(phone.set_alarm, hour=7, minute=45), say="set_alarm")
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
        "toggle pause":     actions["toggle_pause_resume_song"],
        "exit":             actions["exit"],
    }


def build_button_maps(actions):
    """Controller mode -> button -> Action."""

    # Bound in every mode. A mode can override any of these by listing the
    # same button in its own map, since mode bindings are merged in second.
    common = {
        "LJ-up": actions["select_controller_mode_0"],
        "LJ-left": actions["select_controller_mode_1"],
        "LJ-right": actions["select_controller_mode_2"],
    }

    modes = {
        "controller_mode_lights": {
            "a":     actions["kitchen"],
            "b":     actions["bathroom"],
            "y":     actions["living"],
            "x":     actions["vibe"],
            "rb":    actions["all_on"],
            "lb":    actions["all_off"],
            "start": actions["night_mode"],
        },
        "controller_mode_bluetooth": {
            "a":     actions["toggle_pause_resume_song"],
            "y":     actions["play_previous_song"],
            "x":     actions["restart_song"],
            "b":     actions["skip_song"],
            "start": actions["disconnect_xbox_controller"],
            "up":    actions["play_song_1"],
            "left":  actions["play_song_2"],
            "right": actions["play_song_3"],
            "down":  actions["play_song_4"],
            "lb":    actions["decrease_volume"],
            "rb":    actions["increase_volume"],
            "R":     actions["cycle_playback_devices"],
            "LT":    actions["disconnect_from_speaker"],
            "RT":    actions["connect_to_speaker"],
        },
        "controller_mode_phone": {
            "a":     actions["toggle_instagram"],
            "LJ-up": actions["set_alarm"],
        },
    }

    return {name: {**common, **bindings} for name, bindings in modes.items()}