"""
Xbox Controller → Tapo Light
------------------------------
Install:  pip install tapo pygame
Controls: Y = light on, A = light off, B = quit
"""

import asyncio
from tapo import ApiClient
from config import TAPO_EMAIL, TAPO_PASSWORD, LIGHT_IP
from xbox_controller import XboxController


async def main():
    # Connect to light
    print("Connecting to light...")
    client = ApiClient(TAPO_EMAIL, TAPO_PASSWORD)
    device = await client.l530(LIGHT_IP)
    print("Light connected!")

    # Set up controller
    controller = XboxController()
    running = True
    action = None

    def turn_on():
        nonlocal action
        action = "on"

    def turn_off():
        nonlocal action
        action = "off"

    def quit_app():
        nonlocal running
        running = False

    controller.on_button("y", turn_on)
    controller.on_button("a", turn_off)
    controller.on_button("b", quit_app)

    print("\nReady!  Y = on, A = off, B = quit\n")

    try:
        while running:
            controller.update()

            if action == "on":
                await device.on()
                print("Light ON")
            elif action == "off":
                await device.off()
                print("Light OFF")
            action = None

            await asyncio.sleep(0.05)
    except KeyboardInterrupt:
        pass

    controller.close()
    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())