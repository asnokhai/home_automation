"""
Tapo light controller wrapper.
"""

from tapo import ApiClient


class TapoController:
    def __init__(self, email, password):
        self._client = ApiClient(email, password)
        self._lights = {}
        self.night_mode = False

    async def add_light(self, name, ip):
        """Connect and register a light by name."""
        device = await self._client.l530(ip)
        self._lights[name] = device
        print(f"  {name} connected ({ip})")

    async def _apply_mode(self, device):
        """Apply current mode settings to a light."""
        if self.night_mode:
            await device.set_brightness(100)
            await device.set_hue_saturation(0, 100)
        else:
            await device.set_brightness(100)
            await device.set_color_temperature(4000)

    async def toggle(self, name):
        """Toggle a single light on/off, respecting current mode."""
        device = self._lights[name]
        info = await device.get_device_info()
        if info.device_on:
            await device.off()
            print(f"{name} OFF")
        else:
            await device.on()
            await self._apply_mode(device)
            print(f"{name} ON ({'night' if self.night_mode else 'day'})")

    async def all_on(self):
        """Turn all lights on in current mode."""
        for name, device in self._lights.items():
            await device.on()
            await self._apply_mode(device)
        print(f"All lights ON ({'night' if self.night_mode else 'day'})")

    async def all_off(self):
        """Turn all lights off."""
        for name, device in self._lights.items():
            await device.off()
        print("All lights OFF")

    async def toggle_mode(self):
        """Toggle between night and day mode. Applies to all lights that are currently on."""
        self.night_mode = not self.night_mode
        mode = "night" if self.night_mode else "day"
        for name, device in self._lights.items():
            info = await device.get_device_info()
            if info.device_on:
                await self._apply_mode(device)
        print(f"Mode: {mode}")