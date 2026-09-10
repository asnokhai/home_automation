"""Wake the desktop PC over the network.

Thin wrapper around the `wakeonlan` CLI (apt install wakeonlan), rather than
building the magic packet ourselves: the tool already handles the broadcast
address and the socket options, and it is one apt package on the Pi.

Waking is fire-and-forget. The magic packet is a single broadcast UDP datagram
with no reply, so a successful call means "the packet went out", never "the
desktop is up" -- the machine takes a good few seconds to boot, and if
Wake-on-LAN is disabled in its BIOS or its NIC drops power on shutdown, nothing
here can tell.
"""

from __future__ import annotations

import re
import subprocess

from config import DESKTOP_MAC_ADDRESS

# aa:bb:cc:dd:ee:ff, or with dashes / dots -- wakeonlan accepts all three.
_MAC = re.compile(r"^([0-9A-Fa-f]{2}[:\-.]){5}[0-9A-Fa-f]{2}$")


class DesktopError(RuntimeError):
    """Raised when the magic packet could not be sent."""


class DesktopInterface:
    def __init__(self, mac_address: str = DESKTOP_MAC_ADDRESS,
                 wakeonlan_path: str = "wakeonlan"):
        if not _MAC.match(mac_address or ""):
            raise ValueError(
                f"DESKTOP_MAC_ADDRESS {mac_address!r} is not a MAC address"
            )
        self.mac_address = mac_address
        self.wakeonlan_path = wakeonlan_path

    def wakeonlan(self) -> str:
        """Send the magic packet that wakes the desktop.

        Returns a sentence to speak. Raises DesktopError if the packet could
        not be sent at all, so a missing package or a wrong MAC is heard as an
        error instead of a cheerful confirmation of nothing.
        """
        try:
            proc = subprocess.run(
                [self.wakeonlan_path, self.mac_address],
                capture_output=True, text=True, timeout=10,
            )
        except FileNotFoundError:
            raise DesktopError(
                f"{self.wakeonlan_path!r} not found -- install it with "
                "'sudo apt install wakeonlan'"
            ) from None
        except subprocess.TimeoutExpired:
            raise DesktopError("wakeonlan timed out") from None

        if proc.returncode != 0:
            detail = ((proc.stderr or "") + (proc.stdout or "")).strip()
            raise DesktopError(f"wakeonlan failed: {detail or proc.returncode}")

        return "Waking the desktop"


if __name__ == "__main__":
    print(DesktopInterface().wakeonlan())
