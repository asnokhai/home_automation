"""
Xbox Controller → Tapo Lights
-------------------------------
Controls:
  A / B / X / Y   → Toggle individual lights
  LB              → All lights on
  RB              → All lights off
  Start           → Toggle night/day mode
"""

import asyncio
from config import (
    TAPO_EMAIL, TAPO_PASSWORD,
    KITCHEN_LIGHT_IP, BATHROOM_LIGHT_IP,
    LIVING_ROOM_LIGHT_IP, VIBE_LIGHT_IP,
)
from xbox_controller import XboxController
from tapo_controller import TapoController


async def main():
    # Connect to lights
    print("Connecting to lights...")
    tapo = TapoController(TAPO_EMAIL, TAPO_PASSWORD)
    await tapo.add_light("Kitchen", KITCHEN_LIGHT_IP)
    await tapo.add_light("Bathroom", BATHROOM_LIGHT_IP)
    await tapo.add_light("Living Room", LIVING_ROOM_LIGHT_IP)
    await tapo.add_light("Vibe", VIBE_LIGHT_IP)

    # Set up controller
    controller = XboxController()
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
    controller.on_button("y", make_toggle("Living Room"))
    controller.on_button("x", make_toggle("Vibe"))
    controller.on_button("lb", set_action("all_on"))
    controller.on_button("rb", set_action("all_off"))
    controller.on_button("start", set_action("toggle_mode"))

    print("\nReady!")
    print("  A=Kitchen  B=Bathroom  X=Living Room  Y=Vibe")
    print("  LB=All on  RB=All off  Start=Night/Day mode\n")

    try:
        while True:
            controller.update()

            if action:
                try:
                    if action[0] == "toggle":
                        await tapo.toggle(action[1])
                    elif action[0] == "all_on":
                        await tapo.all_on()
                    elif action[0] == "all_off":
                        await tapo.all_off()
                    elif action[0] == "toggle_mode":
                        await tapo.toggle_mode()
                except Exception as e:
                    print(f"  ⚠ Error: {e}")
                action = None

            await asyncio.sleep(0.05)
    except KeyboardInterrupt:
        pass

    controller.close()
    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())