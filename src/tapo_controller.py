"""
Tapo light controller wrapper.

Every bulb is reached by name -- "Kitchen", "Bathroom", "Living Room", "Vibe" --
and the IP behind each name is kept alongside the handle so a dead session can
be rebuilt without going back to config.

Two things here exist because a bulb can lose mains power. `house.py` uses the
kitchen bulb's wall switch as a master switch for the flat, so at any moment a
bulb may be physically unpowered rather than merely unresponsive:

  * A handle may be None. Connecting at startup used to be mandatory, which
    meant booting with the kitchen switch off took the whole assistant down with
    it. Now a light that will not answer is still registered -- name and IP --
    with no handle, and `_with_reconnect` builds one on first use. The key has
    to stay in `_lights` either way: that dict is the canonical list of light
    names, iterated by every fan-out method here and membership-tested by
    light_show/player.py.

  * A light can be marked unpowered. A bulb with no mains does not refuse a
    connection, it simply never answers, so every call to it pays a full
    timeout -- twice, because `_with_reconnect` retries. Once House has
    established that the switch is off there is nothing left to learn by
    trying, so marked lights fail fast and the fan-outs skip them: an unpowered
    bulb is an off bulb, and that is the state the rest of the code sees.
"""

from __future__ import annotations

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

# How long to wait for one bulb's handshake. An unpowered bulb never answers --
# there is no ARP reply to refuse the connection -- so without a ceiling here a
# single dark bulb stalls startup for however long the client waits internally.
CONNECT_TIMEOUT = 6.0

# The ceiling on one device call. ApiClient's own default is 30 seconds, and
# _with_reconnect retries, so a single wedged bulb could hold a button press for
# a minute. On a LAN the measured round trip is ~320ms, so anything approaching
# this is already broken and waiting longer will not fix it.
CALL_TIMEOUT = 4.0


class LightUnreachable(RuntimeError):
    """Raised for a light already known to have no mains power."""


class TapoController:
    def __init__(self):
        # timeout_s, or the client waits 30 seconds on a wedged bulb.
        self._client = ApiClient(TAPO_EMAIL, TAPO_PASSWORD,
                                 timeout_s=int(CALL_TIMEOUT))
        # Non-zero while a command is in flight. House reads it to keep its
        # switch probe from competing for the bulb's one HTTP connection slot.
        self._in_flight = 0
        self._lights = {}        # name -> device handle, or None until one is built
        self._light_ips = {}     # name -> ip
        self._unpowered = set()  # names House has found to have no mains
        self._is_on = {}         # name -> bool, or missing when not yet known
        self.night_mode = False
        self.brightness = MAX_BRIGHTNESS
        self.color_temp = DEFAULT_COLOR_TEMP

    async def connect_to_lights(self):
        """Connect to all lights. Call this after creating the instance.

        Concurrent and failure-tolerant: a bulb switched off at the wall costs
        one CONNECT_TIMEOUT rather than holding the other three up behind it,
        and is registered without a handle instead of aborting startup.
        """
        await asyncio.gather(
            self._add_light("Kitchen", KITCHEN_LIGHT_IP),
            self._add_light("Bathroom", BATHROOM_LIGHT_IP),
            self._add_light("Living Room", LIVING_ROOM_LIGHT_IP),
            self._add_light("Vibe", VIBE_LIGHT_IP),
        )
        # Warm the on/off cache once, here, where the cost is concurrent and
        # nobody is waiting on a button. Without this the first press of each
        # light would still pay the get_device_info the cache exists to avoid.
        await self.refresh_states()

    async def _add_light(self, name, ip):
        """Register a light by name, connecting to it if it answers."""
        # Registered before the handshake, not after: the name has to be in
        # _lights whether or not the bulb answers, and _with_reconnect needs the
        # IP to build a handle later.
        self._light_ips[name] = ip
        self._lights.setdefault(name, None)
        try:
            self._lights[name] = await asyncio.wait_for(
                self._client.l530(ip), CONNECT_TIMEOUT)
            print(f"  {name} connected ({ip})")
        except Exception as e:
            # str() on an asyncio.TimeoutError is empty, which is the most
            # likely failure here -- fall back to the class name so the line
            # still says something.
            print(f"  ⚠ {name} did not answer ({ip}): {e or type(e).__name__}"
                  " -- will retry on first use")

    def light_names(self):
        """Every registered light name, reachable or not."""
        return tuple(self._lights)

    # -- on/off state ----------------------------------------------------
    #
    # Every method here used to ask the bulb whether it was on before doing
    # anything -- toggle, each brightness step, each warmth step, the night-mode
    # switch. That is a whole round trip (~320ms) spent on something we almost
    # always already know, because we are the thing that turned it on.
    #
    # So it is remembered instead, warmed once at startup and updated on every
    # command we send. The cache can drift if a light is changed from the Tapo
    # app or at the wall, which is why `refresh_states` exists and why House
    # calls it on a slow cadence -- off the critical path, where a round trip
    # costs nobody anything. A stale entry is self-correcting: the next press
    # does the opposite of what you wanted, and the one after that is right.

    async def refresh_states(self):
        """Re-read on/off for every powered light, concurrently. Never raises."""
        names = self._powered_names()
        results = await asyncio.gather(
            *(self._with_reconnect(n, lambda d: d.get_device_info())
              for n in names),
            return_exceptions=True,
        )
        for name, info in zip(names, results):
            if not isinstance(info, BaseException):
                self._is_on[name] = bool(info.device_on)
        return self._is_on

    async def _known_on(self, name):
        """Is this light on? Ask the bulb only when we genuinely do not know."""
        if name not in self._is_on:
            info = await self._with_reconnect(name, lambda d: d.get_device_info())
            self._is_on[name] = bool(info.device_on)
        return self._is_on[name]

    def _lights_on(self):
        """Powered lights we believe are currently lit."""
        return [n for n in self._powered_names() if self._is_on.get(n)]

    # -- mains power -----------------------------------------------------

    def mark_unpowered(self, name):
        """Record that a light has no mains power, so it reads as off.

        Called by House when the wall switch goes. The handle goes with it: the
        bulb will have rebooted by the time the power comes back, and a session
        from before the outage is worth nothing.
        """
        if name in self._lights:
            self._unpowered.add(name)
            self._lights[name] = None
            # No mains is off, as far as anything asking is concerned.
            self._is_on[name] = False

    def mark_powered(self, name):
        """Record that a light has mains power again."""
        self._unpowered.discard(name)
        # What it came back as is the bulb's business, not ours -- House turns
        # it on straight after, and anything else re-reads it.
        self._is_on.pop(name, None)

    def is_powered(self, name):
        """False only for a light House has found to be off at the wall."""
        return name not in self._unpowered

    def _powered_names(self):
        """The lights worth sending a command to."""
        return [n for n in self._lights if n not in self._unpowered]

    # -- device plumbing -------------------------------------------------

    async def reconnect(self, name, timeout=CONNECT_TIMEOUT):
        """Build a fresh handle for one light. Raises if the bulb will not answer.

        The timeout is a parameter because House polls this while a bulb is
        cold-booting, where the point is to fail quickly and try again rather
        than to wait patiently once.
        """
        ip = self._light_ips[name]
        device = await asyncio.wait_for(self._client.l530(ip), timeout)
        self._lights[name] = device
        print(f"  {name} reconnected ({ip})")
        return device

    @property
    def busy(self):
        """True while at least one command is in flight."""
        return self._in_flight > 0

    async def _with_reconnect(self, name, action):
        """Run an async action on a device, recovering once from a stale session.

        The recovery is two-step now. A session that has merely expired is
        renewed in place with `refresh_session()`, which is one request; only if
        that fails is the handle thrown away and rebuilt with a full `l530()`
        handshake, which is several. The old code always did the expensive one.
        """
        if name in self._unpowered:
            # Fail fast rather than spend two timeouts confirming what House
            # already established by probing the switch.
            raise LightUnreachable(f"{name} has no power")

        self._in_flight += 1
        try:
            device = self._lights.get(name)
            if device is None:
                # Never connected, or the handle was dropped with the power.
                return await self._call(action, await self.reconnect(name))

            try:
                return await self._call(action, device)
            except Exception:
                pass

            try:
                await asyncio.wait_for(device.refresh_session(), CALL_TIMEOUT)
                return await self._call(action, device)
            except Exception:
                return await self._call(action, await self.reconnect(name))
        finally:
            self._in_flight -= 1

    @staticmethod
    async def _call(action, device):
        """One device call, with a ceiling on it."""
        return await asyncio.wait_for(action(device), CALL_TIMEOUT)

    async def _fan_out(self, action, label, names=None):
        """Run an action against several lights at once, tolerating failures.

        Defaults to every powered light. return_exceptions, because one bulb
        that has dropped off the wifi must not cancel the command to the other
        three -- "all off" on the way out of the door should still turn off
        everything it can reach.
        """
        names = self._powered_names() if names is None else list(names)
        if not names:
            return []
        results = await asyncio.gather(
            *(self._with_reconnect(n, action) for n in names),
            return_exceptions=True,
        )
        failed = [n for n, r in zip(names, results) if isinstance(r, BaseException)]
        if failed:
            print(f"  ⚠ {label} failed for: {', '.join(failed)}")
        return failed

    async def _apply_mode(self, device):
        """Apply the current mode to a light, turning it on, in one request.

        `set()` accumulates properties and `send()` posts them as a single
        multipleRequest. That matters more than it looks: one round trip to an
        L530 is ~320ms (light_show/player.py measured it and prints the real
        figure after a show), and this used to spend two, or three via turn_on's
        separate on(). At 320ms each that difference is most of what a button
        press felt like.

        No explicit on() is needed in either branch. brightness, colour
        temperature and hue/saturation each power the bulb on by themselves
        unless off() is part of the same batch -- which is what the original
        two-call version was already relying on.
        """
        builder = device.set()
        if self.night_mode:
            builder = builder.hue_saturation(0, 100).brightness(MAX_BRIGHTNESS)
        else:
            builder = builder.color_temperature(self.color_temp).brightness(self.brightness)
        await builder.send(device)

    # -- per-light control -----------------------------------------------

    async def turn_on(self, name):
        """Turn one light on in the current mode. One round trip."""
        await self._with_reconnect(name, self._apply_mode)
        self._is_on[name] = True
        print(f"{name} ON ({'night' if self.night_mode else 'day'})")

    async def turn_off(self, name):
        """Turn one light off. One round trip."""
        await self._with_reconnect(name, lambda d: d.off())
        self._is_on[name] = False
        print(f"{name} OFF")

    async def toggle(self, name):
        """Toggle a single light on/off, respecting current mode.

        One round trip in the normal case: the state comes from the cache
        rather than from a get_device_info() the bulb has to answer first.
        """
        if await self._known_on(name):
            await self.turn_off(name)
        else:
            await self.turn_on(name)

    # -- all lights ------------------------------------------------------

    async def all_on(self):
        """Turn all lights on in current mode."""
        failed = await self._fan_out(self._apply_mode, "all on")
        for name in self._powered_names():
            if name not in failed:
                self._is_on[name] = True
        print(f"All lights ON ({'night' if self.night_mode else 'day'})")

    async def all_off(self):
        """Turn all lights off."""
        failed = await self._fan_out(lambda d: d.off(), "all off")
        for name in self._powered_names():
            if name not in failed:
                self._is_on[name] = False
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
        Which ones those are comes from the cache rather than from a
        get_device_info() per bulb, so a step is one round trip per lit light
        instead of two. This is the button most often held down, so the
        doubling was felt more here than anywhere else.
        """
        new = max(MIN_BRIGHTNESS, min(MAX_BRIGHTNESS, self.brightness + delta))
        if new == self.brightness:
            print(f"Brightness: {self.brightness}% ({'max' if delta > 0 else 'min'})")
            return

        self.brightness = new

        await self._fan_out(lambda d: d.set_brightness(self.brightness),
                            "brightness", self._lights_on())
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

        await self._fan_out(lambda d: d.set_color_temperature(self.color_temp),
                            "colour temperature", self._lights_on())
        print(f"Colour temperature: {self.color_temp}K")

    async def toggle_night_mode(self):
        """Toggle between night and day mode. Applies to all lights that are currently on."""
        self.night_mode = not self.night_mode
        mode = "night" if self.night_mode else "day"

        await self._fan_out(self._apply_mode, f"{mode} mode", self._lights_on())
        print(f"Mode: {mode}")