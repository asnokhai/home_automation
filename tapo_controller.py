from tapo import ApiClient


class TapoController:
    def __init__(self, email, password):
        self._client = ApiClient(email, password)
        self._lights = {}

    async def add_light(self, name, ip):
        """Connect and register a light by name."""
        device = await self._client.l530(ip)
        self._lights[name] = device
        print(f"  {name} connected ({ip})")

    async def toggle(self, name):
        """Toggle a single light on/off."""
        device = self._lights[name]
        info = await device.get_device_info()
        if info.device_on:
            await device.off()
            print(f"{name} OFF")
        else:
            await device.on()
            print(f"{name} ON")

    async def all_on(self):
        """Turn all lights on."""
        for name, device in self._lights.items():
            await device.on()
        print("All lights ON")

    async def all_off(self):
        """Turn all lights off."""
        for name, device in self._lights.items():
            await device.off()
        print("All lights OFF")

    async def all_red(self):
        """Set all lights to full brightness red."""
        for name, device in self._lights.items():
            await device.on()
            await device.set_brightness(100)
            await device.set_hue_saturation(0, 100)
        print("All lights RED")