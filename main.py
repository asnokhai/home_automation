"""
Xbox Controller / Terminal → Tapo Lights
------------------------------------------
Controller:
  A / B / X / Y   → Toggle individual lights
  LB              → All lights on
  RB              → All lights off
  Start           → Toggle night/day mode

Terminal commands:
  kitchen / bathroom / living / vibe → Toggle light
  on / off                           → All lights on/off
  mode                               → Toggle night/day
"""

import asyncio
import sys
from config import (
    KITCHEN_LIGHT_IP, BATHROOM_LIGHT_IP,
    LIVING_ROOM_LIGHT_IP, VIBE_LIGHT_IP,
)
from sound_player import SoundPlayer
from xbox_controller import XboxController
from tapo_controller import TapoController

COMMANDS = {
    "kitchen":  ("toggle", "Kitchen"),
    "bathroom": ("toggle", "Bathroom"),
    "living":   ("toggle", "Living Room"),
    "vibe":     ("toggle", "Vibe"),
    "on":       ("all_on",),
    "off":      ("all_off",),
    "mode":     ("toggle_mode",),
}


async def handle_action(action, tapo_controller, sound_player):
    """Execute a single action tuple."""
    try:
        sound_player.play()
        if action[0] == "toggle":
            await tapo_controller.toggle(action[1])
        elif action[0] == "all_on":
            await tapo_controller.all_on()
        elif action[0] == "all_off":
            await tapo_controller.all_off()
        elif action[0] == "toggle_mode":
            await tapo_controller.toggle_mode()
    except Exception as e:
        print(f"  ⚠ Error: {e}")


async def stdin_reader(tapo_controller, sound_player):
    """Read terminal input in a non-blocking loop."""
    loop = asyncio.get_event_loop()
    while True:
        line = await loop.run_in_executor(None, sys.stdin.readline)
        cmd = line.strip().lower()
        if cmd in COMMANDS:
            await handle_action(COMMANDS[cmd], tapo_controller, sound_player)
        elif cmd:
            print(f"  Unknown command: {cmd}")
            print(f"  Available: {', '.join(COMMANDS.keys())}")


async def controller_loop(controller, tapo_controller, sound_player):
    """Poll the Xbox controller for button presses."""
    action = None

    def make_toggle(name):
        def toggle():
            nonlocal action
            action = ("toggle", name)
        return toggle

    def set_action(act):
        def handler():
            nonlocal action
            action = (act,)
        return handler

    controller.on_button("a", make_toggle("Kitchen"))
    controller.on_button("b", make_toggle("Bathroom"))
    controller.on_button("x", make_toggle("Living Room"))
    controller.on_button("y", make_toggle("Vibe"))
    controller.on_button("lb", set_action("all_on"))
    controller.on_button("rb", set_action("all_off"))
    controller.on_button("start", set_action("toggle_mode"))

    while True:
        controller.update()
        if action:
            await handle_action(action, tapo_controller, sound_player)
            action = None
        await asyncio.sleep(0.05)


async def main():
    print("Connecting to lights...")
    tapo_controller = TapoController()
    await tapo_controller.connect_to_lights()

    sound_player = SoundPlayer()
    controller = XboxController()

    print("\nReady!")
    print("  Controller: A=Kitchen  B=Bathroom  X=Living Room  Y=Vibe")
    print("  Controller: LB=All on  RB=All off  Start=Night/Day mode")
    print("  Terminal:   kitchen | bathroom | living | vibe | on | off | mode\n")

    try:
        await asyncio.gather(
            controller_loop(controller, tapo_controller, sound_player),
            stdin_reader(tapo_controller, sound_player),
        )
    except KeyboardInterrupt:
        pass

    controller.close()
    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
