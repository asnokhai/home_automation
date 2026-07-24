"""
Xbox Controller / Terminal → Tapo Lights
"""

import asyncio
import sys

from sound_player import SoundPlayer
from spotify_player import SpotifyPlayer
from xbox_controller import XboxController
from tapo_controller import TapoController
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
    tapo = TapoController()

    await tapo.connect_to_lights()

    actions = build_actions(tapo, controller, spotify)
    commands = build_command_map(actions)
    button_maps = build_button_maps(actions)

    async def on_action(action):
        await run_action(action, sound)

    controller.set_button_maps(button_maps)
    controller.set_action_handler(on_action)

    print("\nReady!")
    print("  Controller: A=Kitchen  B=Bathroom  X=Living Room  Y=Vibe")
    print("  Controller: LB=All on  RB=All off  Start=Night/Day mode")
    print(f"  Terminal:   {' | '.join(commands.keys())}\n")

    try:
        await asyncio.gather(
            controller.run(),
            stdin_reader(commands, on_action),
        )
    except KeyboardInterrupt:
        pass

    controller.close()
    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
