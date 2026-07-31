"""
Battery level for a Bluetooth game controller, via BlueZ over D-Bus.

The battery is exposed by BlueZ (GATT Battery Service 0x180f), not by the
HID/joystick device, so this is entirely independent of pygame and works
even when no joystick node exists.
"""

import asyncio
import threading
import time

from jeepney import DBusAddress, MessageType, new_method_call
from jeepney.io.blocking import open_dbus_connection

BLUEZ = "org.bluez"
PROPS_IFACE = "org.freedesktop.DBus.Properties"
BATTERY_IFACE = "org.bluez.Battery1"
DEVICE_IFACE = "org.bluez.Device1"


def _unwrap(variant, default=None):
    """jeepney returns variants as (signature, value) tuples."""
    if isinstance(variant, tuple) and len(variant) == 2:
        return variant[1]
    return default if variant is None else variant


class XboxControllerBattery:
    """Reads battery percentage for a connected Bluetooth controller.

    Identify the device either by MAC address or by a substring of its
    BlueZ name. Address wins if both are given.

        battery = ControllerBattery(name_hint="Xbox")
        print(battery.read())              # blocking, 0-100 or None
        print(await battery.level())       # async + cached
    """

    def __init__(self, address=None, name_hint="Xbox",
                 cache_seconds=30, adapter="hci0"):
        self.address = address
        self.name_hint = name_hint
        self.cache_seconds = cache_seconds
        self.adapter = adapter

        self._conn = None
        self._path = None
        self._lock = threading.Lock()
        self._value = None
        self._read_at = 0.0

    # -- connection ------------------------------------------------------

    def _connection(self):
        if self._conn is None:
            self._conn = open_dbus_connection(bus="SYSTEM")
        return self._conn

    def _drop_connection(self):
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
        self._conn = None
        self._path = None

    def close(self):
        with self._lock:
            self._drop_connection()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- device lookup ---------------------------------------------------

    def _candidate_path(self):
        """Path derived from the MAC, when one was given."""
        if not self.address:
            return None
        return "/org/{}/{}/dev_{}".format(
            "bluez", self.adapter, self.address.upper().replace(":", "_"))

    def _find_path(self, conn):
        """Locate the BlueZ object exposing Battery1 for our controller."""
        om = DBusAddress("/", bus_name=BLUEZ,
                         interface="org.freedesktop.DBus.ObjectManager")
        reply = conn.send_and_get_reply(
            new_method_call(om, "GetManagedObjects", "", ()))
        if reply.header.message_type is MessageType.error:
            return None

        wanted = self._candidate_path()
        for path, ifaces in reply.body[0].items():
            if BATTERY_IFACE not in ifaces:
                continue
            device = ifaces.get(DEVICE_IFACE, {})
            # BlueZ keeps objects for paired-but-absent devices; their
            # battery values are stale.
            if not _unwrap(device.get("Connected"), False):
                continue
            if wanted:
                if path == wanted:
                    return path
            else:
                name = str(_unwrap(device.get("Name"), ""))
                if self.name_hint.lower() in name.lower():
                    return path
        return None

    # -- reads -----------------------------------------------------------

    def read(self):
        with self._lock:
            try:
                return self._read_locked()
            except Exception as e:
                import traceback;
                traceback.print_exc()  # TEMP
                self._drop_connection()
                return None

    def _read_locked(self):
        conn = self._connection()
        path = self._path or self._find_path(conn)
        if path is None:
            print("BATTERY: no matching device found")  # TEMP
            self._path = None
            return None

        props = DBusAddress(path, bus_name=BLUEZ, interface=PROPS_IFACE)
        reply = conn.send_and_get_reply(
            new_method_call(props, "Get", "ss",
                            (BATTERY_IFACE, "Percentage")))

        if reply.header.message_type is MessageType.error:
            print("BATTERY: error reply:", reply.body)  # TEMP
            self._path = None
            return None

        self._path = path
        return int(_unwrap(reply.body[0]))

    async def level(self, max_age=None):
        """Cached async read. Pass max_age=0 to force a fresh one.

        BlueZ only refreshes this every few minutes, so caching costs
        nothing in accuracy and keeps D-Bus off a fast poll loop.
        """
        if max_age is None:
            max_age = self.cache_seconds

        now = time.time()
        if self._value is not None and now - self._read_at < max_age:
            return self._value

        loop = asyncio.get_event_loop()
        value = await loop.run_in_executor(None, self.read)

        if value is not None:
            self._value = value
            self._read_at = now
        return value

    async def say_battery_level(self):
        """Return a phrase describing the controller's battery level."""
        level = await self.level()
        if level is None:
            return "Controller battery unavailable"
        return f"{level} percent"

    @property
    def cached(self):
        """Last successful reading, without triggering a read."""
        return self._value