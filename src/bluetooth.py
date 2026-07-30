#!/usr/bin/env python3
"""Bluetooth control through BlueZ's D-Bus API.

Talks to ``org.bluez`` directly instead of scraping ``bluetoothctl``, which is
an interactive tool whose output format and argument handling vary between
releases. Properties are read as typed values and ``Connect``/``Disconnect``
are synchronous calls that either return or raise a named error, so there is
nothing to parse and nothing to poll.

Requires ``jeepney`` (pure Python, no system dependencies)::

    pip install jeepney
"""

from __future__ import annotations

import contextlib
import re
import time
from typing import Any, Iterator

from jeepney import DBusAddress, HeaderFields, MessageType, new_method_call
from jeepney.io.blocking import open_dbus_connection

from config import SPEAKERS_MAC_ADDRESS, XBOX_CONTROLLER_MAC_ADDRESS

__all__ = ["Bluetooth", "BluetoothError", "DeviceNotFound", "AdapterBlocked"]

_MAC_PATTERN = re.compile(r"^([0-9A-F]{2}:){5}[0-9A-F]{2}$", re.IGNORECASE)

_BLUEZ = "org.bluez"
_ADAPTER_IFACE = "org.bluez.Adapter1"
_DEVICE_IFACE = "org.bluez.Device1"
_PROPS_IFACE = "org.freedesktop.DBus.Properties"
_OBJECT_MANAGER = "org.freedesktop.DBus.ObjectManager"


class BluetoothError(RuntimeError):
    """A BlueZ operation failed.

    ``name`` carries the D-Bus error name, e.g. ``org.bluez.Error.Failed``.
    """

    def __init__(self, message: str, name: str | None = None) -> None:
        super().__init__(message)
        self.name = name


class DeviceNotFound(BluetoothError):
    """BlueZ has no record of this device - it is probably not paired."""


class AdapterBlocked(BluetoothError):
    """The adapter is rfkill-blocked; software cannot power it on."""


class Bluetooth:
    """Control a BlueZ adapter and its paired devices.

    ``bus`` exists so tests can point the class at a session bus; leave it as
    ``"SYSTEM"`` for real use.
    """

    def __init__(self, adapter: str = "hci0", bus: str = "SYSTEM",
                 timeout: float = 30.0) -> None:
        self.adapter = adapter
        self.timeout = timeout
        self._path = f"/{_BLUEZ.replace('.', '/')}/{adapter}"
        try:
            self._conn = open_dbus_connection(bus=bus)
        except Exception as exc:                      # noqa: BLE001
            raise BluetoothError(f"could not reach the {bus.lower()} bus: {exc}") from exc

    # ------------------------------------------------------------------
    # plumbing
    # ------------------------------------------------------------------

    @staticmethod
    def normalise(mac: str) -> str:
        """Validate a MAC address and return it as ``AA:BB:CC:DD:EE:FF``."""
        cleaned = mac.strip().replace("-", ":").upper()
        if not _MAC_PATTERN.match(cleaned):
            raise ValueError(f"{mac!r} is not a valid MAC address")
        return cleaned

    def device_path(self, mac: str) -> str:
        """Return the D-Bus object path BlueZ uses for a device."""
        return f"{self._path}/dev_{self.normalise(mac).replace(':', '_')}"

    def _call(self, path: str, interface: str, method: str,
              signature: str = "", body: tuple = (),
              timeout: float | None = None) -> tuple:
        """Send a method call and return its body, translating D-Bus errors."""
        address = DBusAddress(path, bus_name=_BLUEZ, interface=interface)
        message = new_method_call(address, method, signature, body)
        try:
            reply = self._conn.send_and_get_reply(
                message, timeout=self.timeout if timeout is None else timeout
            )
        except TimeoutError as exc:
            raise BluetoothError(f"{method} timed out") from exc

        if reply.header.message_type is MessageType.error:
            name = reply.header.fields.get(HeaderFields.error_name, "unknown")
            detail = reply.body[0] if reply.body else name
            raise self._translate(name, detail)
        return reply.body

    @staticmethod
    def _translate(name: str, detail: str) -> BluetoothError:
        """Map a D-Bus error name onto a useful exception."""
        # A device BlueZ has never seen has no object path at all. Depending on
        # the call and the GDBus version that surfaces as UnknownObject,
        # UnknownMethod or UnknownInterface; the method and interface names
        # here are fixed, so any of them means "no such device".
        if name in ("org.freedesktop.DBus.Error.UnknownObject",
                    "org.freedesktop.DBus.Error.UnknownMethod",
                    "org.freedesktop.DBus.Error.UnknownInterface",
                    "org.bluez.Error.DoesNotExist"):
            return DeviceNotFound(
                "BlueZ does not know this device; pair it first", name
            )
        if name == "org.bluez.Error.Blocked":
            return AdapterBlocked(
                f"{detail} - try 'rfkill unblock bluetooth'", name
            )
        if name == "org.bluez.Error.NotReady":
            return BluetoothError(f"{detail} - the adapter is powered off", name)
        return BluetoothError(detail, name)

    def _get(self, path: str, interface: str, prop: str) -> Any:
        """Read one property, unwrapping the variant BlueZ returns."""
        (variant,) = self._call(
            path, _PROPS_IFACE, "Get", "ss", (interface, prop)
        )
        return variant[1]

    def _set(self, path: str, interface: str, prop: str,
             variant: tuple[str, Any]) -> None:
        self._call(path, _PROPS_IFACE, "Set", "ssv", (interface, prop, variant))

    # ------------------------------------------------------------------
    # adapter
    # ------------------------------------------------------------------

    def enable(self, settle: float = 2.0) -> None:
        """Power the adapter on.

        Raises :class:`AdapterBlocked` immediately if rfkill is in the way,
        rather than appearing to succeed.
        """
        self._power(True, settle)

    def disable(self, settle: float = 2.0) -> None:
        """Power the adapter off."""
        self._power(False, settle)

    def _power(self, state: bool, settle: float) -> None:
        self._set(self._path, _ADAPTER_IFACE, "Powered", ("b", state))
        # The Set call returns once accepted; give the property a moment to
        # reflect reality before believing it.
        deadline = time.monotonic() + settle
        while self.is_enabled() is not state:
            if time.monotonic() >= deadline:
                raise BluetoothError(
                    f"adapter did not turn {'on' if state else 'off'} "
                    f"within {settle:g}s of being asked"
                )
            time.sleep(0.1)

    def is_enabled(self) -> bool:
        """Return ``True`` if the adapter is powered on."""
        return bool(self._get(self._path, _ADAPTER_IFACE, "Powered"))

    # ------------------------------------------------------------------
    # devices
    # ------------------------------------------------------------------

    def connect(self, mac: str, timeout: float | None = None) -> None:
        """Connect to a paired device.

        Blocks until BlueZ reports the connection established or failed. A
        device that is already connected is treated as success.
        """
        print("Attempting to connect to device...")
        try:
            self._call(self.device_path(mac), _DEVICE_IFACE, "Connect",
                       timeout=timeout)
        except BluetoothError as exc:
            if exc.name == "org.bluez.Error.AlreadyConnected":
                return
            raise

    def disconnect(self, mac: str, timeout: float | None = None) -> None:
        """Disconnect a device."""
        print("Attempting to disconnect from device...")
        try:
            self._call(self.device_path(mac), _DEVICE_IFACE, "Disconnect",
                       timeout=timeout)
        except BluetoothError as exc:
            if exc.name == "org.bluez.Error.NotConnected":
                return
            raise

    def is_connected(self, mac: str) -> bool:
        """Return ``True`` if the device is currently connected."""
        return bool(self._get(self.device_path(mac), _DEVICE_IFACE, "Connected"))

    def is_paired(self, mac: str) -> bool:
        """Return ``True`` if the device is paired with this adapter."""
        try:
            return bool(self._get(self.device_path(mac), _DEVICE_IFACE, "Paired"))
        except DeviceNotFound:
            return False

    def devices(self, paired_only: bool = True) -> dict[str, str]:
        """Return a ``{mac: name}`` mapping of the devices BlueZ knows about."""
        (objects,) = self._call("/", _OBJECT_MANAGER, "GetManagedObjects")
        found: dict[str, str] = {}
        for path, interfaces in objects.items():
            props = interfaces.get(_DEVICE_IFACE)
            if props is None or not path.startswith(self._path + "/"):
                continue
            if paired_only and not props.get("Paired", ("b", False))[1]:
                continue
            address = props.get("Address", ("s", ""))[1]
            name = props.get("Alias", props.get("Name", ("s", "")))[1]
            if address:
                found[address.upper()] = name
        return found

    def connect_to_speaker(self):
        self.connect(SPEAKERS_MAC_ADDRESS)
        print("Device is connected: ", self.is_connected(SPEAKERS_MAC_ADDRESS))

    def disconnect_from_speaker(self):
        self.disconnect(SPEAKERS_MAC_ADDRESS)
        print("Device is connected: ", self.is_connected(SPEAKERS_MAC_ADDRESS))

    def disconnect_xbox_controller(self):
        self.disconnect(XBOX_CONTROLLER_MAC_ADDRESS)
        print("XBOX controller is connected: ", self.is_connected(XBOX_CONTROLLER_MAC_ADDRESS))

    # backwards-compatible alias
    paired_devices = devices

    # ------------------------------------------------------------------
    # convenience
    # ------------------------------------------------------------------

    @contextlib.contextmanager
    def connection(self, mac: str, **kwargs) -> Iterator[str]:
        """Connect on entry, disconnect on exit."""
        mac = self.normalise(mac)
        self.connect(mac, **kwargs)
        try:
            yield mac
        finally:
            with contextlib.suppress(BluetoothError):
                self.disconnect(mac)

    def close(self) -> None:
        """Close the D-Bus connection."""
        with contextlib.suppress(Exception):
            self._conn.close()

    def __enter__(self) -> "Bluetooth":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


if __name__ == "__main__":
    with Bluetooth() as bt:
        if not bt.is_enabled():
            bt.enable()
        print(f"adapter powered: {bt.is_enabled()}")
        for address, name in bt.devices().items():
            state = "[connected]" if bt.is_connected(address) else ""
            print(f"  {address}  {name}  {state}")
