"""
Xbox Controller / Terminal → Tapo Lights
"""

import asyncio
import sys

from sound_player import SoundPlayer
from spotify_player import SpotifyPlayer
from voice import VoiceAssistant
from adb import ADB
from xbox_controller.xbox_controller import XboxController
from xbox_controller.xbox_controller_battery import XboxControllerBattery
from tapo_controller import TapoController
from bluetooth import Bluetooth
from timers import Timers
from trello_board import TrelloBoard
from shopping_list import ShoppingList
from desktop import DesktopInterface
from system import System
from house import House
from bindings import build_actions, build_command_map, build_button_maps, run_action


async def stdin_reader(commands, handler):
    """Read terminal input, passing recognized commands to handler."""
    loop = asyncio.get_event_loop()
    while True:
        line = await loop.run_in_executor(None, sys.stdin.readline)
        cmd = line.strip().lower()
        if cmd in commands:
            await handler(commands[cmd])
        elif cmd:
            print(f"  Unknown command: {cmd}")
            print(f"  Available: {', '.join(commands.keys())}")


async def main():
    print("Connecting to lights...")

    sound = SoundPlayer()
    spotify = SpotifyPlayer()
    controller = XboxController()
    controller_battery = XboxControllerBattery()
    tapo = TapoController()
    bluetooth = Bluetooth()
    phone = ADB()
    timers = Timers(sound)
    # Constructing this touches no network -- the board is fetched on first use,
    # so a missing token or a Trello outage cannot stop the assistant booting.
    trello = TrelloBoard()
    # Same promise as the board above: no session, no login, no network until
    # the first shopping action actually asks for one.
    shopping = ShoppingList()
    # Nothing to connect to: waking is one broadcast packet, sent on demand.
    desktop = DesktopInterface()
    # Takes the SoundPlayer because it has to speak before it reboots.
    system = System(sound)

    print("BATTERY startup probe:", controller_battery.read())

    await tapo.connect_to_lights()

    # Built before the actions, not after: switching voice mode is itself an
    # action, so the assistant has to exist to be bound to one. It gets the
    # action table back below.
    voice = VoiceAssistant(sound)

    # After the voice assistant, because standby stops it listening. Building it
    # touches no network -- the switch is probed by watch() -- so a kitchen bulb
    # that is off at the wall cannot keep the assistant from booting.
    house = House(tapo, sound, voice)

    actions = build_actions(tapo, controller, controller_battery, spotify, bluetooth, phone,
                            timers, trello, shopping, desktop, system, voice, house)
    commands = build_command_map(actions)
    button_maps = build_button_maps(actions)

    async def on_action(action, args=None, speak=True):
        return await run_action(action, sound, args, speak)

    controller.set_button_maps(button_maps)
    controller.set_action_handler(on_action)

    voice.set_actions(actions)
    voice.set_action_handler(on_action)

    print("\nReady!")
    print("  Controller: A=Kitchen  B=Bathroom  X=Living Room  Y=Vibe")
    print("  Controller: RB=All on  LB=All off  Start=Night/Day mode")
    print("  Controller: D-pad up/down = brighter / dimmer")
    print("  Controller: LJ-down = misc mode, then B = switch voice mode, Y = standby")
    print("  House:      the kitchen wall switch is the master switch")
    print(f"  Terminal:   {' | '.join(commands.keys())}\n")

    try:
        await asyncio.gather(
            controller.run(),
            stdin_reader(commands, on_action),
            voice.run(),
            phone.watch(),
            house.watch(),
        )
    except KeyboardInterrupt:
        pass
    finally:
        # In a finally, not after the gather: a cancelled task re-raises here,
        # and aiohttp complains about an unclosed session on the way out.
        controller.close()
        await shopping.aclose()

    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
