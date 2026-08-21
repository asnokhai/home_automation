"""
Action bindings: button/keyword -> a specific class method, bound directly.

Adding a new capability means writing a method on some class and referencing
it in build_actions() below -- no keyword to invent, no dispatcher to edit.

`desc` is what the voice assistant shows the model. An Action with desc=None
is invisible to voice and can only be triggered by button or terminal.
"""

import asyncio
import sys
from dataclasses import dataclass
from functools import partial
from typing import Callable


@dataclass
class Action:
    fn: Callable
    say: object = None   # None: silent, True: say fn's return value, str: say this phrase
    desc: str = None     # None: hidden from voice. str: description shown to the model.
    params: dict = None  # None: takes no arguments. dict: JSON-schema properties the model fills in.


async def run_action(action: Action, sound, args=None):
    """Play the click sound, run the bound function, then speak if configured.

    `args` carries the arguments the voice model filled in for an Action with
    `params`. Buttons and terminal keywords pass nothing -- their actions are
    already fully bound -- so it defaults to no arguments.
    """
    try:
        sound.play_click()
        result = action.fn(**(args or {}))
        if asyncio.iscoroutine(result):
            result = await result
        if action.say is True:
            sound.say(result)
        elif action.say:
            sound.say(action.say)
    except Exception as e:
        print(f"  ⚠ Error: {e}")


def build_actions(tapo, controller, controller_battery, spotify, bluetooth, phone):
    """Define every action once, bound directly to its class method."""
    return {
        # -- controller modes: button-only, meaningless by voice ----------
        "select_controller_mode_0": Action(partial(controller.select_mode, 0), say="controller_mode_lights"),
        "select_controller_mode_1": Action(partial(controller.select_mode, 1), say="controller_mode_bluetooth"),
        "select_controller_mode_2": Action(partial(controller.select_mode, 2), say="controller_mode_phone"),

        # -- lights -------------------------------------------------------
        "kitchen": Action(
            partial(tapo.toggle, "Kitchen"),
            desc="Toggle the kitchen light on or off"),
        "bathroom": Action(
            partial(tapo.toggle, "Bathroom"),
            desc="Toggle the bathroom light on or off"),
        "living": Action(
            partial(tapo.toggle, "Living Room"),
            desc="Toggle the living room light on or off"),
        "vibe": Action(
            partial(tapo.toggle, "Vibe"),
            desc="Toggle the vibe light on or off"),
        "all_on": Action(
            tapo.all_on,
            desc="Turn on every light in the house"),
        "all_off": Action(
            tapo.all_off, say="all_off",
            desc="Turn off every light in the house"),
        "night_mode": Action(
            tapo.toggle_night_mode,
            desc="Switch the lights between night mode and day mode"),
        "increase_brightness": Action(
            tapo.increase_brightness,
            desc="Make the lights brighter"),
        "decrease_brightness": Action(
            tapo.decrease_brightness,
            desc="Dim the lights"),
        "increase_warmth": Action(
            tapo.increase_warmth,
            desc="Make the lights warmer and more yellow"),
        "decrease_warmth": Action(
            tapo.decrease_warmth,
            desc="Make the lights cooler and less yellow"),

        # -- music --------------------------------------------------------
        "play_song": Action(
            spotify.play_song, say="play_song",
            desc="Play a specific song on Spotify. Pass the song the user asked "
                 "for, including the artist if they named one.",
            params={
                "song_name": {
                    "type": "string",
                    "description": "Song title, or 'Title - Artist' when the "
                                   "artist is known",
                },
            }),

        # The D-pad favourites. Voice-hidden on purpose: "play_song" above already
        # covers them, and leaving them visible gives the model five overlapping
        # ways to start music.
        "play_song_1": Action(
            partial(spotify.play_song, "Afterlife - Avenged Sevenfold"), say="play_song"),
        "play_song_2": Action(
            partial(spotify.play_song, "Holiday - Green Day"), say="play_song"),
        "play_song_3": Action(
            partial(spotify.play_song, "Automatic Sun - The Warning"), say="play_song"),
        "play_song_4": Action(
            partial(spotify.play_song, "Reason - Selah Sue"), say="play_song"),
        "increase_volume": Action(
            spotify.increase_volume,
            desc="Turn the music volume up"),
        "decrease_volume": Action(
            spotify.decrease_volume,
            desc="Turn the music volume down"),
        "toggle_pause_resume_song": Action(
            spotify.toggle_pause_resume,
            desc="Pause the music if it is playing, or resume it if it is paused"),
        "restart_song": Action(
            spotify.restart_song,
            desc="Start the current song again from the beginning"),
        "skip_song": Action(
            spotify.skip_song,
            desc="Skip to the next song"),
        "play_previous_song": Action(
            spotify.play_previous_song,
            desc="Go back to the previous song"),
        "cycle_playback_devices": Action(
            spotify.cycle_playback_devices,
            desc="Switch Spotify playback to the next available device"),

        # -- bluetooth ----------------------------------------------------
        "connect_to_speaker": Action(
            bluetooth.connect_to_speaker,
            desc="Connect to the bluetooth speaker"),
        "disconnect_from_speaker": Action(
            bluetooth.disconnect_from_speaker, say="disconnect_from_speaker",
            desc="Disconnect from the bluetooth speaker"),
        "disconnect_xbox_controller": Action(
            bluetooth.disconnect_xbox_controller,
            desc="Disconnect the Xbox controller"),
        "call_battery_level": Action(
            partial(controller_battery.say_battery_level), say=True,
            desc="Report the Xbox controller's remaining battery level"),

        # -- phone --------------------------------------------------------
        "toggle_distractions": Action(
            phone.toggle_distractions,
            desc="Block or unblock the distracting apps on the phone "
                 "(Instagram, Reddit and YouTube)"),
        "set_alarm": Action(
            partial(phone.set_alarm, hour=7, minute=45), say="set_alarm",
            desc="Set an alarm on the phone for 7:45"),

        # -- deliberately voice-hidden ------------------------------------
        "exit": Action(sys.exit),
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


def build_voice_tools(actions):
    """Actions with a desc -> OpenAI function-calling tool schema."""
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": action.desc,
                "parameters": {
                    "type": "object",
                    "properties": action.params or {},
                    "required": list(action.params or {}),
                },
            },
        }
        for name, action in actions.items()
        if action.desc
    ]


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
            "up":    actions["increase_brightness"],
            "down":  actions["decrease_brightness"],
            "left":  actions["increase_warmth"],
            "right": actions["decrease_warmth"],
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
            "L":     actions["call_battery_level"],
            "R":     actions["cycle_playback_devices"],
            "LT":    actions["disconnect_from_speaker"],
            "RT":    actions["connect_to_speaker"],
        },
        "controller_mode_phone": {
            "a":     actions["toggle_distractions"],
            "b": actions["set_alarm"],
        },
    }

    return {name: {**common, **bindings} for name, bindings in modes.items()}