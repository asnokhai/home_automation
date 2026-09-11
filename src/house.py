"""The kitchen light switch, used as a master switch for the flat.

The kitchen bulb sits on a normal wall switch. Flipping that switch cuts its
mains, which means the bulb drops off the network -- and that disappearance is
the only sensor here. There is no contact on the switch and nothing reports its
position; the house infers it from whether one known IP answers.

Two transitions follow from that:

  power cut       everything still reachable goes off and the voice assistant
                  stops listening. The kitchen bulb is already dark, so it is
                  marked unpowered rather than commanded -- see below.
  power restored  a greeting, then the kitchen and vibe lights come up.

Why a bare TCP connect rather than the tapo API
-----------------------------------------------
The tapo client talks plain HTTP to http://<ip>/app on port 80, so opening a
socket to that port is not a proxy for reachability -- it is the exact transport
the real client needs. If it connects, the path is open. Going through
`get_device_info()` instead would mean paying the client's own connect timeout
against a dead bulb, twice, since `_with_reconnect` retries; a poll meant to
tick every two seconds could sit blocked for far longer than that.

Why the debounce is lopsided
----------------------------
A powered-off bulb does not refuse the connection -- no ARP reply ever comes and
the connect hangs to the timeout -- so a failed cycle costs interval + timeout,
not interval. Given that:

  down needs DOWN_STREAK    a wifi blip, an AP channel change or the Pi's own
                            NIC hiccuping must never black out the flat. Losing
                            the lights ten seconds after the switch flips costs
                            nothing; nobody is in the room. A false blackout
                            costs a great deal.
  up needs only one         nothing else lives at that address, so there is no
                            plausible false positive, and this is what makes the
                            welcome feel immediate.

On top of that, a down edge is checked against a light that is *not* on the
switched circuit. If nothing at all answers, the wifi is ours to blame, not the
mains, and the state is held rather than acted on.

What a restart is not
---------------------
A restart is not a homecoming. The service comes back on every reboot and every
`systemctl restart`, and greeting each time would be wrong -- you never left.
The first observation therefore only seeds state: it plays the startup chime, it
takes no action on the lights, and it leaves a line in the journal saying what
it assumed. Boot with the switch off and then flip it on, and you are greeted
properly, because that is a real transition.
"""

from __future__ import annotations

import asyncio
import time

from config import (KITCHEN_LIGHT_IP, LIVING_ROOM_LIGHT_IP, BATHROOM_LIGHT_IP,
                    VIBE_LIGHT_IP)

PROBE_PORT = 80        # the port the tapo client itself uses: http://<ip>/app
PROBE_TIMEOUT = 0.8    # only ever paid by a *down* probe: a bulb on the LAN
                       # accepts in milliseconds, while a dead one never answers
                       # at all. So this is the cost of noticing you left.
POLL_INTERVAL = 1.0    # the idle wait before an up edge is noticed
DOWN_STREAK = 2        # samples that must agree before believing a cut. Kept
                       # low because the reference-bulb check below, not the
                       # streak, is what actually rules out a wifi blip.
UP_STREAK = 1          # nothing else answers on that address

# A bulb answers TCP while still booting, well before its login endpoint will
# serve a handshake. How long that takes varies, so it is polled rather than
# slept through: try immediately, fail fast, retry often. A fixed settle always
# costs its full length even when the bulb was ready at once.
API_WAIT = 20.0            # total budget for a cold-booting bulb
API_ATTEMPT_TIMEOUT = 2.0  # per handshake attempt
API_RETRY_DELAY = 0.3

# How often to re-read the bulbs' on/off state into TapoController's cache.
# That cache is what lets a button press skip a get_device_info, and it drifts
# only when a light is changed from the Tapo app or at the wall. Refreshing it
# here costs nothing anyone waits on -- this loop is already awake, and the
# round trip happens where no button is pending.
STATE_REFRESH = 30.0

SWITCHED_LIGHTS = ("Kitchen",)             # dead whenever the switch is off
WELCOME_LIGHTS = ("Kitchen", "Vibe")       # what comes on when you walk in
# Probed only to tell "their mains went" from "our wifi went". Both must be on
# permanently live sockets for the check to mean anything.
REFERENCE_IPS = (LIVING_ROOM_LIGHT_IP, BATHROOM_LIGHT_IP, VIBE_LIGHT_IP)


class House:
    def __init__(self, tapo, sound_player, voice=None,
                 switch_ip=KITCHEN_LIGHT_IP,
                 switched_lights=SWITCHED_LIGHTS,
                 welcome_lights=WELCOME_LIGHTS,
                 reference_ips=REFERENCE_IPS):
        self._tapo = tapo
        # Taken in the constructor for the same reason Timers and System take
        # it: the greeting has to land *before* the settle and the bulb calls,
        # and run_action only speaks once the call has already returned.
        self._sound_player = sound_player
        # Optional so the diagnostic at the bottom of this file can run without
        # building a mic, a wake-word model and a websocket.
        self._voice = voice

        self._switch_ip = switch_ip
        self._switched_lights = tuple(switched_lights)
        self._welcome_lights = tuple(welcome_lights)
        self._reference_ips = tuple(reference_ips)

        self.powered = None    # None until the first probe seeds it
        self.standby = False
        # Startup already warms the cache, so the first refresh can wait.
        self._next_refresh = time.monotonic() + STATE_REFRESH
        # welcome_home/standby_now can arrive from a voice command on another
        # task while the watcher is midway through the opposite transition.
        self._lock = asyncio.Lock()

    # -- probing ---------------------------------------------------------

    async def _probe(self, ip):
        """True if something answers on the tapo port at `ip`.

        Deliberately no bare `except Exception`: a CancelledError on shutdown
        has to propagate rather than be reported as an unreachable bulb.
        """
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(ip, PROBE_PORT), PROBE_TIMEOUT)
        except (OSError, asyncio.TimeoutError):
            return False

        # Closed every time: this runs every couple of seconds on a service that
        # stays up for weeks, and a leaked transport per poll adds up.
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
        return True

    async def _any_reference_reachable(self):
        """True if any always-powered bulb answers -- i.e. our wifi is fine."""
        results = await asyncio.gather(*(self._probe(ip)
                                         for ip in self._reference_ips))
        return any(results)

    # -- the watcher -----------------------------------------------------

    async def watch(self, interval: float = POLL_INTERVAL) -> None:
        """Poll the switch forever, acting on the edges.

        Nothing may escape: main gathers this beside the controller and voice
        loops without return_exceptions, so one bad probe would take the whole
        assistant down. CancelledError is re-raised first so shutdown still
        works -- it is control flow, not a fault.
        """
        while True:
            try:
                # Never probe while a command is in flight. An L530 serves very
                # few concurrent connections to :80 -- often one -- and this
                # probe wants the same socket the command needs. Losing that
                # race makes the command fail, which costs a session refresh or
                # a full handshake, and that is how a 0.3s button press turns
                # into a multi-second one. A skipped tick costs nothing: the
                # switch is not going anywhere in the next two seconds.
                if not self._tapo.busy:
                    await self._observe(await self._probe(self._switch_ip))
                await self._maybe_refresh_states()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"  ⚠ house watcher: {e}")
            # Slept at the end rather than the start, so the first observation
            # seeds the state as soon as the assistant boots.
            await asyncio.sleep(interval)

    async def _maybe_refresh_states(self):
        """Re-read the bulbs' on/off state now and then, to bound cache drift.

        Here rather than on a timer of its own because this loop is already
        awake and, more importantly, already knows not to touch the bulbs while
        a command is in flight.
        """
        now = time.monotonic()
        if self._tapo.busy or now < self._next_refresh:
            return
        self._next_refresh = now + STATE_REFRESH
        await self._tapo.refresh_states()

    async def _observe(self, up):
        """Fold one probe result into the state, firing a transition on an edge."""
        if self.powered is None:
            self._seed(up)
            return

        if up == self.powered:
            return

        if not await self._confirm(up):
            return

        self.powered = up
        async with self._lock:
            await (self.on_power_restored() if up else self.on_power_cut())

    async def _confirm(self, up):
        """Take the extra samples an edge needs, back to back.

        Back to back is the point. These used to be one sample per poll tick,
        which put a whole POLL_INTERVAL between each of them and meant leaving
        the house took the better part of ten seconds to notice -- three
        timeouts and two sleeps. The samples are just as independent taken
        immediately, and the wall switch does not flicker.
        """
        for _ in range((UP_STREAK if up else DOWN_STREAK) - 1):
            if await self._probe(self._switch_ip) != up:
                return False

        if not up and not await self._any_reference_reachable():
            # Every bulb unreachable at once is not how a wall switch behaves.
            # This check, rather than the streak length, is what makes a short
            # streak safe: hold the state and wait for the network to come back.
            print("  House: nothing on the network answers -- treating as our "
                  "wifi, not a power cut")
            return False

        return True

    def _seed(self, up):
        """Adopt the switch's position at startup without acting on it."""
        self.powered = up
        self.standby = not up
        for name in self._switched_lights:
            (self._tapo.mark_powered if up else self._tapo.mark_unpowered)(name)
        # Note what is deliberately *not* done here even when seeding into
        # standby: the voice assistant is left listening. Suspending it is an
        # act, and this is only an observation -- but more practically, a
        # restart with the switch off would otherwise come back deaf, and the
        # only ways out would be the wall switch or the controller.
        #
        # A chime rather than the greeting: audible proof the service came back,
        # without claiming you just walked in.
        self._sound_player.play_startup()
        print(f"House: switch {'on' if up else 'off'} at startup -- assuming "
              f"{'awake' if up else 'standby'}, taking no action")

    # -- transitions -----------------------------------------------------

    async def on_power_restored(self):
        """Welcome someone in: greet, then bring the welcome lights up."""
        self.standby = False
        for name in self._switched_lights:
            self._tapo.mark_powered(name)

        # Spoken before any bulb call, so the greeting is immediate rather than
        # arriving after several seconds of handshaking.
        self._sound_player.say("welcome_home")
        self._resume_voice()

        # Split by whether the bulb has had power all along. The ones that have
        # can come on *now* -- there is nothing to wait for. Only the ones whose
        # mains just came back are still booting. Holding every light back until
        # the slowest one is ready is what made walking in feel slow, when half
        # the answer was available immediately.
        booting = [n for n in self._welcome_lights if n in self._switched_lights]
        ready = [n for n in self._welcome_lights if n not in self._switched_lights]

        results = await asyncio.gather(
            *(self._tapo.turn_on(n) for n in ready),
            *(self._wake(n) for n in booting),
            return_exceptions=True,
        )
        self._report("welcome home", ready + booting, results)
        print("House: awake")
        return "Welcome home"

    async def _wake(self, name):
        """Turn on a bulb whose mains have only just come back."""
        await self._ensure_api(name)
        await self._tapo.turn_on(name)

    async def on_power_cut(self):
        """Drop into standby: lights off, and stop listening."""
        self.standby = True
        # Marked, not commanded. The bulb has no mains -- there is nothing to
        # turn off and nothing to wait for -- and marking it is what makes the
        # rest of the code read it as off.
        for name in self._switched_lights:
            self._tapo.mark_unpowered(name)

        targets = [n for n in self._tapo.light_names()
                   if n not in self._switched_lights]
        results = await asyncio.gather(
            *(self._tapo.turn_off(n) for n in targets),
            return_exceptions=True,
        )
        self._report("standby", targets, results)

        self._suspend_voice()
        # Silent on purpose: the switch is flipped on the way out, and there is
        # nobody left in the room to hear a confirmation.
        print("House: standby")
        return "Standby"

    async def _ensure_api(self, name):
        """Wait for one bulb's login endpoint to come up after a cold boot.

        Polled, not slept. A cold-booting bulb accepts TCP before it will serve
        a handshake, and how long that gap lasts varies -- so try at once, fail
        fast, and retry often. The old fixed settle paid its full length every
        time, including when the bulb was ready immediately.
        """
        deadline = time.monotonic() + API_WAIT
        while True:
            try:
                await self._tapo.reconnect(name, timeout=API_ATTEMPT_TIMEOUT)
                return True
            except Exception as e:
                if time.monotonic() >= deadline:
                    print(f"  ⚠ {name} never came back within {API_WAIT:.0f}s: {e}")
                    return False
                await asyncio.sleep(API_RETRY_DELAY)

    @staticmethod
    def _report(label, names, results):
        """Name the lights a transition could not reach. Never raises."""
        failed = [n for n, r in zip(names, results)
                  if isinstance(r, BaseException)]
        if failed:
            print(f"  ⚠ {label} could not reach: {', '.join(failed)}")

    # -- the voice assistant ---------------------------------------------

    def _suspend_voice(self):
        """Stop the wake word listening while the flat is empty."""
        if self._voice is None:
            return
        print(f"  House: {self._voice.suspend()}")

    def _resume_voice(self):
        if self._voice is None:
            return
        print(f"  House: {self._voice.resume()}")

    # -- actions ---------------------------------------------------------

    async def welcome_home(self):
        """Wake the house by hand, as if the switch had just been flipped."""
        async with self._lock:
            self.powered = True
            return await self.on_power_restored()

    async def standby_now(self):
        """Put the house into standby by hand."""
        async with self._lock:
            # powered is left alone: the switch may well still be on, and
            # claiming otherwise would make the next probe look like an edge
            # and greet you for standing still.
            self.standby = True
            targets = [n for n in self._tapo.light_names()
                       if self._tapo.is_powered(n)]
            results = await asyncio.gather(
                *(self._tapo.turn_off(n) for n in targets),
                return_exceptions=True,
            )
            self._report("standby", targets, results)
            self._suspend_voice()
            print("House: standby")
            return "Going into standby"

    def status(self):
        """Report whether the house is awake and whether the switch has power."""
        if self.powered is None:
            return "I have not checked the light switch yet"

        switch = "on" if self.powered else "off"
        where = "in standby" if self.standby else "awake"
        return f"The house is {where}, and the kitchen switch is {switch}"


if __name__ == "__main__":
    # Probe-only diagnostic: builds no TapoController and touches no light.
    # Run it on the pi and flip the kitchen switch -- it prints how long each
    # edge actually took, which is how the constants at the top of this file
    # should be chosen rather than by the guesses they currently are.
    import time

    async def _demo():
        house = House(tapo=None, sound_player=None)
        print(f"Probing {house._switch_ip}:{PROBE_PORT} every {POLL_INTERVAL}s.")
        print("Flip the kitchen switch; Ctrl-C to stop.\n")

        state = None
        changed = time.monotonic()
        while True:
            up = await house._probe(house._switch_ip)
            if up != state:
                now = time.monotonic()
                if state is not None:
                    print(f"  -> {'UP' if up else 'DOWN'} after "
                          f"{now - changed:.1f}s in the previous state")
                else:
                    print(f"  initial state: {'UP' if up else 'DOWN'}")
                state, changed = up, now
            else:
                print(f"  {'up' if up else 'down'}")
            await asyncio.sleep(POLL_INTERVAL)

    try:
        asyncio.run(_demo())
    except KeyboardInterrupt:
        print("\nStopped.")
