#!/usr/bin/env python3
"""
Manual test for ControllerBattery.

    python3 test_controller_battery.py
    python3 test_controller_battery.py --watch
    python3 test_controller_battery.py --address C8:3F:26:5D:0F:3C --watch --interval 30
"""

import argparse
import asyncio
import time

from jeepney import DBusAddress, MessageType, new_method_call
from jeepney.io.blocking import open_dbus_connection

from xbox_controller.xbox_controller_battery import (
    BATTERY_IFACE,
    BLUEZ,
    DEVICE_IFACE,
    XboxControllerBattery,
    _unwrap,
)


def list_battery_devices():
    """Dump every BlueZ object exposing Battery1. Diagnostic only."""
    conn = open_dbus_connection(bus="SYSTEM")
    try:
        om = DBusAddress("/", bus_name=BLUEZ,
                         interface="org.freedesktop.DBus.ObjectManager")
        reply = conn.send_and_get_reply(
            new_method_call(om, "GetManagedObjects", "", ()))
        if reply.header.message_type is MessageType.error:
            print("  GetManagedObjects failed:", reply.body)
            return

        found = False
        for path, ifaces in sorted(reply.body[0].items()):
            if BATTERY_IFACE not in ifaces:
                continue
            found = True
            device = ifaces.get(DEVICE_IFACE, {})
            print("  {}\n    name={!r} connected={} battery={}%".format(
                path,
                str(_unwrap(device.get("Name"), "?")),
                _unwrap(device.get("Connected"), False),
                _unwrap(ifaces[BATTERY_IFACE].get("Percentage"), "?"),
            ))
        if not found:
            print("  (no device exposes org.bluez.Battery1)")
    finally:
        conn.close()


def check_blocking_read(battery):
    """Time a single synchronous read. Returns the value, or None."""
    started = time.time()
    value = battery.read()
    print("  read() -> {}   ({:.1f} ms)".format(
        value, (time.time() - started) * 1000))
    return value


def check_cache(battery, loop):
    """Confirm level() caches and that max_age=0 bypasses it."""
    started = time.time()
    first = loop.run_until_complete(battery.level())
    first_ms = (time.time() - started) * 1000

    started = time.time()
    second = loop.run_until_complete(battery.level())
    second_ms = (time.time() - started) * 1000

    print("  level()          -> {}  ({:.1f} ms)".format(first, first_ms))
    print("  level()          -> {}  ({:.1f} ms, cached)".format(second, second_ms))
    print("  cached           ->", battery.cached)
    print("  level(max_age=0) ->",
          loop.run_until_complete(battery.level(max_age=0)))

    if first is not None and second != first:
        print("  ⚠ cached value differs from the first read")


async def watch(battery, interval, count):
    """Poll until Ctrl-C, or until `count` readings if non-zero."""
    seen = 0
    while count == 0 or seen < count:
        level = await battery.level(max_age=0)   # skip cache, real round trip
        print("{}  {}".format(
            time.strftime("%H:%M:%S"),
            "{}%".format(level) if level is not None else "unavailable"))
        seen += 1
        if count == 0 or seen < count:
            await asyncio.sleep(interval)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Test ControllerBattery.")
    parser.add_argument("--address", help="MAC, e.g. C8:3F:26:5D:0F:3C")
    parser.add_argument("--name-hint", default="Xbox",
                        help="substring of the BlueZ device name")
    parser.add_argument("--adapter", default="hci0")
    parser.add_argument("--watch", action="store_true",
                        help="poll continuously instead of reading once")
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--count", type=int, default=0,
                        help="stop after N readings (0 = forever)")
    args = parser.parse_args(argv)

    print("== devices exposing Battery1 ==")
    list_battery_devices()

    battery = XboxControllerBattery(address=args.address,
                                name_hint=args.name_hint,
                                adapter=args.adapter)
    value = None
    try:
        print("\n== blocking read ==")
        value = check_blocking_read(battery)

        loop = asyncio.get_event_loop()
        if args.watch:
            print("\n== watching (Ctrl-C to stop) ==")
            loop.run_until_complete(watch(battery, args.interval, args.count))
        else:
            print("\n== async + cache ==")
            check_cache(battery, loop)
    except KeyboardInterrupt:
        print()
    finally:
        battery.close()

    return 0 if value is not None else 1


if __name__ == "__main__":
    raise SystemExit(main())