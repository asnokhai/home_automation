"""
Action bindings: button/keyword -> a specific class method, bound directly.

Adding a new capability means writing a method on some class and referencing
it in build_actions() below -- no keyword to invent, no dispatcher to edit.
"""

import asyncio
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


def build_actions(tapo, controller, spotify, bluetooth):
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
        "play_song":   Action(partial(spotify.play_song, "Afterlife - Avenged Sevenfold"), say="play_song"),
        "pause_song":  Action(spotify.pause),
        "resume_song": Action(spotify.resume),
        "toggle_speaker_connection": Action(bluetooth.toggle_speaker_connection),
        "disconnect_xbox_controller": Action(bluetooth.disconnect_xbox_controller),
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
        "play":             actions["play_song"],
        "pause":            actions["pause_song"],
        "resume":           actions["resume_song"],
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
            "a":    actions["play_song"],
            "x":    actions["pause_song"],
            "y":    actions["toggle_speaker_connection"],
            "b": actions["resume_song"],
            "start": actions["disconnect_xbox_controller"],
            "back": actions["cycle_mode"],
        },
    }