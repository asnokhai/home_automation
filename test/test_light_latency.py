#!/usr/bin/env python3
"""
Measure how long a light command actually takes.

    cd src && python ../test/test_light_latency.py
    cd src && python ../test/test_light_latency.py --light Vibe --rounds 10
    cd src && python ../test/test_light_latency.py --probe   # with House's probe running

Run from inside `src/`, like main.py, because the modules import each other flat.

This exists because "the button feels slow" is not a number. One L530 answers in
roughly 320ms (light_show/player.py measured it), and the only thing that really
matters for how a button feels is how many of those a single press spends. The
script reports that directly: elapsed time per action, and the implied number of
round trips.

Expect roughly one round trip for a toggle. If you see three or four, something
has gone back to sending each property as its own request; if you see a multi-
second outlier, that is a session refresh or a full handshake, and `--probe`
is the flag for reproducing the cause.
"""

import argparse
import asyncio
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from tapo_controller import TapoController, CALL_TIMEOUT  # noqa: E402

ROUND_TRIP = 0.32   # light_show/player.py: "the measured round trip"


def report(label, samples):
    if not samples:
        print(f"  {label}: no samples")
        return
    mean = statistics.mean(samples)
    print(f"  {label:22s} mean {mean*1000:6.0f}ms   "
          f"min {min(samples)*1000:5.0f}   max {max(samples)*1000:5.0f}   "
          f"~{mean/ROUND_TRIP:.1f} round trips")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--light", default="Kitchen")
    ap.add_argument("--rounds", type=int, default=5)
    ap.add_argument("--probe", action="store_true",
                    help="run House's switch probe concurrently, to see whether "
                         "it is stealing the bulb's connection slot")
    args = ap.parse_args()

    tapo = TapoController()
    t0 = time.monotonic()
    await tapo.connect_to_lights()
    print(f"\nconnect_to_lights + state warm: {(time.monotonic()-t0)*1000:.0f}ms")
    # A ceiling, not a measurement -- printed so an outlier below can be read
    # against it. Reaching it means a bulb stopped answering, never that a
    # healthy call is slow.
    print(f"giving up on a call after: {CALL_TIMEOUT}s (ceiling, not a timing)\n")

    if args.light not in tapo.light_names():
        print(f"No light called {args.light!r}. Have: {', '.join(tapo.light_names())}")
        return 1

    prober = None
    if args.probe:
        from house import House
        house = House(tapo, sound_player=None)

        async def probe_forever():
            while True:
                await house._probe(house._switch_ip)
                await asyncio.sleep(2.0)

        prober = asyncio.create_task(probe_forever())
        print("House probe running concurrently (every 2s)\n")

    on, off, steps = [], [], []
    try:
        for i in range(args.rounds):
            t = time.monotonic(); await tapo.turn_on(args.light); on.append(time.monotonic()-t)
            await asyncio.sleep(0.4)
            t = time.monotonic(); await tapo.turn_off(args.light); off.append(time.monotonic()-t)
            await asyncio.sleep(0.4)
            print(f"  round {i+1}: on {on[-1]*1000:.0f}ms  off {off[-1]*1000:.0f}ms")

        print("\nbrightness step across every lit light:")
        await tapo.turn_on(args.light)
        await asyncio.sleep(0.4)
        for _ in range(args.rounds):
            t = time.monotonic(); await tapo.decrease_brightness(); steps.append(time.monotonic()-t)
            await asyncio.sleep(0.3)
    finally:
        if prober:
            prober.cancel()

    print()
    report("turn_on", on)
    report("turn_off", off)
    report("brightness step", steps)

    worst = max(on + off + steps)
    if worst > ROUND_TRIP * 2.5:
        print(f"\n  ⚠ worst case {worst*1000:.0f}ms -- more than a couple of round "
              "trips. Re-run with --probe to see if the switch probe is the cause.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
