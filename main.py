"""
Xbox Controller / Terminal → Tapo Lights
"""

import asyncio
import sys

from pygame._sdl2 import controller

import spotify_player
from sound_player import SoundPlayer
from spotify_player import SpotifyPlayer
from xbox_controller import XboxController
from tapo_controller import TapoController

COMMANDS = {
    "kitchen":  ("toggle", "Kitchen"),
    "bathroom": ("toggle", "Bathroom"),
    "living":   ("toggle", "Living Room"),
    "vibe":     ("toggle", "Vibe"),
    "on":       ("all_on",),
    "off":      ("all_off",),
    "lights mode":     ("toggle_night_mode",),
    "controller mode":     ("cycle_controller_mode",),
    "play":     ("bt_play_song",),
    "pause":     ("bt_pause_song",),
    "resume":     ("bt_resume_song",),
}

BUTTON_MAPS = {
    "lights_mode": {
        "a":     ("toggle", "Kitchen"),
        "b":     ("toggle", "Bathroom"),
        "y":     ("toggle", "Living Room"),
        "x":     ("toggle", "Vibe"),
        "rb":    ("all_on",),
        "lb":    ("all_off",),
        "start": ("toggle_night_mode",),
        "back":  ("cycle_controller_mode",),
    },
    "bluetooth_mode": {
        "a":     ("bt_play_song",),
        "x":     ("bt_pause_song",),
        "b":     ("bt_resume_song",),
    },
}


async def dispatch(action, tapo, sound, controller, spotify_player):
    """Route an action tuple to the appropriate TapoController method."""
    try:
        sound.play()
        cmd, *args = action
        if cmd == "toggle":
            await tapo.toggle(args[0])
        elif cmd == "all_on":
            await tapo.all_on()
        elif cmd == "all_off":
            await tapo.all_off()
            sound.say("all_off")
        elif cmd == "toggle_night_mode":
            await tapo.toggle_night_mode()
        elif cmd == "cycle_controller_mode":
            controller_mode = controller.cycle_mode()
            sound.say(controller_mode)
        elif cmd == "bt_play_song":
            sound.say("play_song")
            spotify_player.play_song("Afterlife - Avenged Sevenfold")
        elif cmd == "bt_pause_song":
            spotify_player.pause()
        elif cmd == "bt_resume_song":
            spotify_player.resume()
    except Exception as e:
        print(f"  ⚠ Error: {e}")


async def stdin_reader(handler):
    """Read terminal input, passing recognized commands to handler."""
    loop = asyncio.get_event_loop()
    while True:
        line = await loop.run_in_executor(None, sys.stdin.readline)
        cmd = line.strip().lower()
        if cmd in COMMANDS:
            await handler(COMMANDS[cmd])
        elif cmd:
            print(f"  Unknown command: {cmd}")
            print(f"  Available: {', '.join(COMMANDS.keys())}")


async def main():
    print("Connecting to lights...")
    tapo = TapoController()
    await tapo.connect_to_lights()

    sound = SoundPlayer()
    controller = XboxController()
    spotify_player = SpotifyPlayer()

    async def on_action(action):
        await dispatch(action, tapo, sound, controller, spotify_player)

    controller.set_button_maps(BUTTON_MAPS)
    controller.set_action_handler(on_action)

    print("\nReady!")
    print("  Controller: A=Kitchen  B=Bathroom  X=Living Room  Y=Vibe")
    print("  Controller: LB=All on  RB=All off  Start=Night/Day mode")
    print("  Terminal:   kitchen | bathroom | living | vibe | on | off | mode\n")

    try:
        await asyncio.gather(
            controller.run(),
            stdin_reader(on_action),
        )
    except KeyboardInterrupt:
        pass

    controller.close()
    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())