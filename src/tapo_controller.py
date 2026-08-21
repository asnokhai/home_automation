"""
Tapo light controller wrapper.
"""

import asyncio
from tapo import ApiClient

from config import TAPO_EMAIL, TAPO_PASSWORD, KITCHEN_LIGHT_IP, BATHROOM_LIGHT_IP, LIVING_ROOM_LIGHT_IP, VIBE_LIGHT_IP

BRIGHTNESS_STEP = 5
MIN_BRIGHTNESS = 5
MAX_BRIGHTNESS = 100

# Colour temperature in kelvin: low is warm and yellow, high is cold and blue.
COLOR_TEMP_STEP = 250
MIN_COLOR_TEMP = 2500
MAX_COLOR_TEMP = 6500
DEFAULT_COLOR_TEMP = 4000


class TapoController:
    def __init__(self):
        self._client = ApiClient(TAPO_EMAIL, TAPO_PASSWORD)
        self._lights = {}
        self._light_ips = {}
        self.night_mode = False
        self.brightness = MAX_BRIGHTNESS
        self.color_temp = DEFAULT_COLOR_TEMP

    async def connect_to_lights(self):
        """Connect to all lights. Call this after creating the instance."""
        await self._add_light("Kitchen", KITCHEN_LIGHT_IP)
        await self._add_light("Bathroom", BATHROOM_LIGHT_IP)
        await self._add_light("Living Room", LIVING_ROOM_LIGHT_IP)
        await self._add_light("Vibe", VIBE_LIGHT_IP)

    async def _add_light(self, name, ip):
        """Connect and register a light by name."""
        device = await self._client.l530(ip)
        self._lights[name] = device
        self._light_ips[name] = ip
        print(f"  {name} connected ({ip})")

    async def _with_reconnect(self, name, action):
        """Run an async action on a device, reconnecting once on auth failure."""
        device = self._lights[name]
        try:
            return await action(device)
        except Exception:
            ip = self._light_ips[name]
            device = await self._client.l530(ip)
            self._lights[name] = device
            print(f"  {name} reconnected ({ip})")
            return await action(device)

    async def _apply_mode(self, device):
        """Apply current mode settings to a light. Also turns it on."""
        if self.night_mode:
            await device.set_hue_saturation(0, 100)
            await device.set_brightness(MAX_BRIGHTNESS)
        else:
            await device.set_color_temperature(self.color_temp)
            await device.set_brightness(self.brightness)

    async def toggle(self, name):
        """Toggle a single light on/off, respecting current mode."""
        info = await self._with_reconnect(name, lambda d: d.get_device_info())
        if info.device_on:
            await self._with_reconnect(name, lambda d: d.off())
            print(f"{name} OFF")
        else:
            await self._with_reconnect(name, lambda d: self._apply_mode(d))
            print(f"{name} ON ({'night' if self.night_mode else 'day'})")

    async def all_on(self):
        """Turn all lights on in current mode."""
        await asyncio.gather(*(
            self._with_reconnect(n, lambda d: self._apply_mode(d))
            for n in self._lights
        ))
        print(f"All lights ON ({'night' if self.night_mode else 'day'})")

    async def all_off(self):
        """Turn all lights off."""
        await asyncio.gather(*(
            self._with_reconnect(n, lambda d: d.off())
            for n in self._lights
        ))
        print("All lights OFF")

    async def increase_brightness(self):
        """Step the lights up one notch."""
        await self._change_brightness(BRIGHTNESS_STEP)

    async def decrease_brightness(self):
        """Step the lights down one notch."""
        await self._change_brightness(-BRIGHTNESS_STEP)

    async def _change_brightness(self, delta):
        """Step the stored dim level and push it to the lights that are on.

        Lights that are off stay off -- set_brightness would otherwise wake
        them -- and pick the new level up from _apply_mode when toggled on.
        """
        new = max(MIN_BRIGHTNESS, min(MAX_BRIGHTNESS, self.brightness + delta))
        if new == self.brightness:
            print(f"Brightness: {self.brightness}% ({'max' if delta > 0 else 'min'})")
            return

        self.brightness = new

        async def set_if_on(device):
            info = await device.get_device_info()
            if info.device_on:
                await device.set_brightness(self.brightness)

        await asyncio.gather(*(
            self._with_reconnect(n, set_if_on)
            for n in self._lights
        ))
        print(f"Brightness: {self.brightness}%")

    async def increase_warmth(self):
        """Step the lights one notch warmer (more yellow)."""
        await self._change_color_temp(-COLOR_TEMP_STEP)

    async def decrease_warmth(self):
        """Step the lights one notch cooler (less yellow)."""
        await self._change_color_temp(COLOR_TEMP_STEP)

    async def _change_color_temp(self, delta):
        """Step the stored colour temperature and push it to the lights that are on.

        Like brightness, lights that are off stay off and pick the new value up
        from _apply_mode when toggled on. Night mode is left alone entirely --
        it paints the lights red with hue/saturation, and setting a colour
        temperature would drop them straight back out of it -- so the new value
        is only stored and takes effect on the return to day mode.
        """
        new = max(MIN_COLOR_TEMP, min(MAX_COLOR_TEMP, self.color_temp + delta))
        if new == self.color_temp:
            print(f"Colour temperature: {self.color_temp}K "
                  f"({'coldest' if delta > 0 else 'warmest'})")
            return

        self.color_temp = new

        if self.night_mode:
            print(f"Colour temperature: {self.color_temp}K (night mode, applied in day mode)")
            return

        async def set_if_on(device):
            info = await device.get_device_info()
            if info.device_on:
                await device.set_color_temperature(self.color_temp)

        await asyncio.gather(*(
            self._with_reconnect(n, set_if_on)
            for n in self._lights
        ))
        print(f"Colour temperature: {self.color_temp}K")

    async def toggle_night_mode(self):
        """Toggle between night and day mode. Applies to all lights that are currently on."""
        self.night_mode = not self.night_mode
        mode = "night" if self.night_mode else "day"

        async def apply_if_on(device):
            info = await device.get_device_info()
            if info.device_on:
                await self._apply_mode(device)

        await asyncio.gather(*(
            self._with_reconnect(n, apply_if_on)
            for n in self._lights
        ))
        print(f"Mode: {mode}")