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
    required: list = None  # None: every param is required. list: only these are, rest are optional.


async def run_action(action: Action, sound, args=None, speak=True):
    """Play the click sound_player, run the bound function, then speak if configured.

    `args` carries the arguments the voice model filled in for an Action with
    `params`. Buttons and terminal keywords pass nothing -- their actions are
    already fully bound -- so it defaults to no arguments.

    Returns whatever the function returned. The realtime voice pathway hands
    that back to the model as the tool result, so it can read out a real battery
    level or Trello list instead of guessing at one; `speak=False` is there for
    when the model's own voice should be the only one that answers. Errors come
    back as a string for the same reason -- silence would leave the model
    claiming success.
    """
    try:
        sound.play_click()
        result = action.fn(**(args or {}))
        if asyncio.iscoroutine(result):
            result = await result
        if speak:
            if action.say is True:
                sound.say(result)
            elif action.say:
                sound.say(action.say)
        return result
    except Exception as e:
        print(f"  ⚠ Error: {e}")
        return f"Error: {e}"


def build_actions(tapo, controller, controller_battery, spotify, bluetooth, phone, timers,
                  trello, shopping, desktop, system, voice, house):
    """Define every action once, bound directly to its class method."""
    return {
        # -- controller modes: button-only, meaningless by voice ----------
        "select_controller_mode_0": Action(partial(controller.select_mode, 0), say="controller_mode_lights"),
        "select_controller_mode_1": Action(partial(controller.select_mode, 1), say="controller_mode_bluetooth"),
        "select_controller_mode_2": Action(partial(controller.select_mode, 2), say="controller_mode_phone"),
        "select_controller_mode_3": Action(partial(controller.select_mode, 3), say="controller_mode_misc"),

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

        # -- desktop ------------------------------------------------------
        # say=True: the packet is fire-and-forget, so the only thing worth
        # hearing is whether it went out or why it didn't.
        "wake_desktop": Action(
            desktop.wakeonlan, say=True,
            desc="Turn on the desktop PC by waking it over the network"),

        # -- the house ----------------------------------------------------
        # Normally these fire themselves, off the kitchen wall switch. They are
        # here for the times the switch is not the thing that changed: settling
        # down for the night without getting up, or telling the house you are
        # back when it missed the edge.
        #
        # say=None on welcome_home because House speaks the greeting itself --
        # it has to land before the bulbs are waited on, and run_action only
        # speaks once the call has returned. Same reasoning as reboot above.
        "welcome_home": Action(
            house.welcome_home,
            desc="Wake the house up as if someone just came home: greet them "
                 "and turn on the kitchen and vibe lights"),
        "standby": Action(
            house.standby_now, say="standby",
            desc="Put the house into standby: turn every light off and stop "
                 "the voice assistant listening, as when leaving or going to bed"),
        "house_status": Action(
            house.status, say=True,
            desc="Report whether the house is awake or in standby, and whether "
                 "the kitchen light switch has power"),

        # -- this pi ------------------------------------------------------
        # say=None because System.reboot speaks for itself: the clip has to
        # play before the kernel goes down, and run_action only speaks after
        # the call returns. desc is worded tightly on purpose -- a loose match
        # here costs the whole assistant, not just a wrong light.
        "reboot": Action(
            system.reboot,
            desc="Reboot the Raspberry Pi that runs this assistant. Only use "
                 "this when the user clearly asks to reboot or restart the pi "
                 "or the assistant itself -- it kills everything, including "
                 "the lights and music control, for about a minute."),

        # -- timers -------------------------------------------------------
        "set_timer": Action(
            timers.set_timer, say=True,
            desc="Set a countdown timer. Convert whatever duration the user said "
                 "into seconds. If they did not name the timer, use a short name "
                 "based on what it is for, or just 'timer'.",
            params={
                "name": {
                    "type": "string",
                    "description": "Short name for the timer, e.g. 'pasta' or "
                                   "'laundry'",
                },
                "duration_seconds": {
                    "type": "number",
                    "description": "How long the timer runs, in seconds",
                },
            }),
        "list_timers": Action(
            timers.list_timers, say=True,
            desc="Report which timers are running and how much time is left on each"),
        "cancel_timer": Action(
            timers.cancel_timer, say=True,
            desc="Cancel one timer by name",
            params={
                "name": {
                    "type": "string",
                    "description": "Name of the timer to cancel",
                },
            }),
        "cancel_all_timers": Action(
            timers.cancel_all_timers, say=True,
            desc="Cancel every running timer"),
        "stop_timer_alarm": Action(
            timers.stop_alarm, say="timer_stopped",
            desc="Silence the timer alarm that is currently ringing"),

        # -- trello -------------------------------------------------------
        # Every one of these speaks its return value: nothing feeds a tool
        # result back to the model, so the wrapper has to phrase the answer.
        "create_task": Action(
            trello.create_task, say=True,
            desc="Add a task to the Trello board. Only pass list_name if the "
                 "user named a list, and only pass due if they gave a deadline. "
                 "Not for groceries or anything to buy at a shop -- those go "
                 "to add_shopping_item.",
            params={
                "task_name": {
                    "type": "string",
                    "description": "What the task is, phrased as a short card title",
                },
                "list_name": {
                    "type": "string",
                    "description": "The list to add it to, if the user named one",
                },
                "due": {
                    "type": "string",
                    "description": "Deadline as an ISO 8601 date or date-time, "
                                   "resolved against today's date",
                },
            },
            required=["task_name"]),
        "mark_task_completed": Action(
            trello.mark_as_completed, say=True,
            desc="Mark a task on the Trello board as done. Pass the task name "
                 "roughly as the user said it, it is matched loosely.",
            params={
                "task_name": {
                    "type": "string",
                    "description": "Name of the task to complete",
                },
            }),
        "archive_task": Action(
            trello.archive, say=True,
            desc="Archive a task, taking it off the Trello board entirely. Use "
                 "this only when the user says archive, remove or delete -- use "
                 "mark_task_completed when they say done or finished.",
            params={
                "task_name": {
                    "type": "string",
                    "description": "Name of the task to archive",
                },
            }),
        "list_tasks": Action(
            trello.list_tasks, say=True,
            desc="Read back the open tasks on the Trello board. Only pass "
                 "list_name if the user asked about one particular list. This "
                 "is the task board, not the shopping list.",
            params={
                "list_name": {
                    "type": "string",
                    "description": "Restrict to this list, if the user named one",
                },
            },
            required=[]),
        "set_task_due_date": Action(
            trello.set_due_date, say=True,
            desc="Put a deadline on a task that is already on the Trello board",
            params={
                "task_name": {
                    "type": "string",
                    "description": "Name of the task to schedule",
                },
                "due": {
                    "type": "string",
                    "description": "Deadline as an ISO 8601 date or date-time, "
                                   "resolved against today's date",
                },
            }),
        "list_due_tasks": Action(
            trello.list_due_tasks, say=True,
            desc="Report which Trello tasks are due. With no days argument this "
                 "is today plus anything overdue; pass days=7 for the week ahead.",
            params={
                "days": {
                    "type": "number",
                    "description": "How many days ahead to look. Omit for today",
                },
            },
            required=[]),

        # -- shopping list ------------------------------------------------
        # Worded to stay clear of the Trello block above: the model hears
        # "put it on the list" for both, so each side has to name the other.
        "add_shopping_item": Action(
            shopping.add_item, say=True,
            desc="Add a grocery or household item to the Bring shopping list. "
                 "Use this for anything bought at a shop -- food, drink, "
                 "cleaning supplies -- and for anything the user says to put "
                 "on the shopping list. Not for to-dos or errands: those are "
                 "create_task. Keep item_name to the product alone and put "
                 "any amount in quantity.",
            params={
                "item_name": {
                    "type": "string",
                    "description": "The product on its own, with no amount in it, "
                                   "like 'milk' or 'kitchen roll'",
                },
                "quantity": {
                    "type": "string",
                    "description": "How much or which kind, like 'two litres' or "
                                   "'the oat one'. Omit if they did not say",
                },
            },
            required=["item_name"]),
        "list_shopping_items": Action(
            shopping.list_items, say=True,
            desc="Read back what is still to buy on the Bring shopping list. "
                 "Use this for what is on the shopping list or what do we need "
                 "from the shop. For tasks and chores use list_tasks."),

        # -- voice mode ---------------------------------------------------
        # say=None on all three: the switch is a request, and the facade plays
        # the confirmation once the swap has actually happened. Speaking here
        # would announce a mode that is not live yet.
        "realtime_voice_mode": Action(
            voice.use_realtime,
            desc="Switch the voice assistant to realtime mode, where it holds a "
                 "live conversation and you can keep talking without repeating "
                 "the wake word"),
        "classic_voice_mode": Action(
            voice.use_classic,
            desc="Switch the voice assistant back to classic mode, where it "
                 "answers one request per wake word"),
        "toggle_voice_mode": Action(
            voice.toggle_mode,
            desc="Switch the voice assistant between realtime and classic mode "
                 "when the user does not say which one they want"),

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
        "voice mode":       actions["toggle_voice_mode"],
        "shopping":         actions["list_shopping_items"],
        "desktop":          actions["wake_desktop"],
        "home":             actions["welcome_home"],
        "standby":          actions["standby"],
        "house":            actions["house_status"],
        "reboot":           actions["reboot"],
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
                    "required": (list(action.params or {}) if action.required is None
                                 else action.required),
                },
            },
        }
        for name, action in actions.items()
        if action.desc
    ]


def build_realtime_tools(actions):
    """Same registry, flat shape.

    The Realtime API puts name/description/parameters directly on the tool
    rather than under a nested "function" key the way chat completions do.
    """
    return [{"type": "function", **tool["function"]}
            for tool in build_voice_tools(actions)]


def build_button_maps(actions):
    """Controller mode -> button -> Action."""

    # Bound in every mode. A mode can override any of these by listing the
    # same button in its own map, since mode bindings are merged in second.
    common = {
        "LJ-up": actions["select_controller_mode_0"],
        "LJ-left": actions["select_controller_mode_1"],
        "LJ-right": actions["select_controller_mode_2"],
        "LJ-down": actions["select_controller_mode_3"],
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
        "misc_mode": {
            "a": actions["stop_timer_alarm"],
            "b": actions["toggle_voice_mode"],
            "x": actions["wake_desktop"],
            # No button for welcome_home: the switch on the wall is that button.
            "y": actions["standby"],
        }

    }

    return {name: {**common, **bindings} for name, bindings in modes.items()}