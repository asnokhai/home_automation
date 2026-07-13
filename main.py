"""
Simple Tapo Light Control Script
---------------------------------
Install:  pip install tapo
Usage:    python tapo_light_test.py
"""

import asyncio
from tapo import ApiClient
from config import TAPO_EMAIL, TAPO_PASSWORD, LIGHT_IP

async def main():
    # Connect to the light
    client = ApiClient(TAPO_EMAIL, TAPO_PASSWORD)

    # Use the right handler for your bulb:
    #   .l510e()  → dimmable white bulb
    #   .l530()   → multicolour bulb
    #   .l630()   → multicolour bulb (newer)
    device = await client.l530(LIGHT_IP)

    # 1. Get current device info
    info = await device.get_device_info()
    print(f"Device: {info.model}  (nickname: {info.nickname})")
    print(f"Currently on: {info.device_on}")

    # 2. Turn on
    print("\n→ Turning light ON...")
    await device.on()

    # 3. Set brightness to 50 %
    print("→ Setting brightness to 50 %...")
    await device.set_brightness(50)

    # 4. Set colour (hue 240 = blue, saturation 100 %)
    print("→ Setting colour to blue...")
    await device.set_hue_saturation(240, 100)
    await asyncio.sleep(2)

    # 5. Switch to warm white (colour temperature in Kelvin)
    print("→ Switching to warm white (2700 K)...")
    await device.set_color_temperature(2700)
    await asyncio.sleep(2)

    # 6. Turn off
    print("→ Turning light OFF...")
    await device.off()

    print("\n✓ All done — your Tapo light is working!")


if __name__ == "__main__":
    asyncio.run(main())