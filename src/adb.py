"""Minimal adb wrapper: connect to the phone over wireless adb, toggle Instagram."""

from __future__ import annotations

import subprocess
import time

from config import PHONE_IP, ADB_PORT

ADB_PATH = "/usr/bin/adb"
INSTAGRAM = "com.instagram.android"

# What adb prints when the transport is gone. It exits non-zero, but the message
# is on stderr and the wording varies by version, so match loosely.
_GONE = (
    "not found",              # error: device '10.0.0.5:5555' not found
    "device offline",
    "no devices/emulators found",
    "connection reset",
    "protocol fault",
    "closed",
)


class ADBError(RuntimeError):
    """Raised when an adb command fails."""


class ADBUnavailable(ADBError):
    """Phone isn't reachable right now — probably off the network."""


class ADB:
    def __init__(self, host: str = PHONE_IP, port: int = ADB_PORT,
                 adb_path: str = ADB_PATH, user_id: int = 0,
                 base_backoff: float = 5.0, max_backoff: float = 300.0):
        self.serial = f"{host}:{port}"
        self.adb_path = adb_path
        self.user_id = user_id
        self.base_backoff = base_backoff
        self.max_backoff = max_backoff
        self._fails = 0
        self._retry_after = 0.0
        self.connect()  # best effort; never fatal

    # --- connection management -------------------------------------------

    def state(self) -> str | None:
        """Transport state per `adb devices`: 'device', 'offline',
        'unauthorized', or None if adb doesn't know the serial at all."""
        out, _ = self._run(["devices"], device=False, timeout=5)
        for line in out.splitlines():
            if line.startswith(self.serial):
                return line.split()[-1]
        return None

    def connect(self) -> bool:
        """(Re)establish the transport. Cheap no-op when already up."""
        st = self.state()
        if st == "device":
            self._fails = 0
            return True
        if st == "unauthorized":
            raise ADBError(
                f"{self.serial} is connected but unauthorized — accept the RSA "
                "prompt on the phone and tick 'always allow from this computer'."
            )

        if time.monotonic() < self._retry_after:
            return False

        # A stale 'offline' entry won't be repaired by connect alone.
        self._run(["disconnect", self.serial], device=False, timeout=5)
        self._run(["connect", self.serial], device=False, timeout=10)

        if self.state() == "device":
            self._fails = 0
            self._retry_after = 0.0
            return True

        self._fails += 1
        delay = min(self.base_backoff * 2 ** (self._fails - 1), self.max_backoff)
        self._retry_after = time.monotonic() + delay
        return False

    # --- app control ------------------------------------------------------

    def toggle_instagram(self) -> bool:
        """Flip Instagram between blocked and available.

        Returns True if it is now blocked, False if it is now usable.
        """
        blocked = self._is_blocked()
        verb = "unsuspend" if blocked else "suspend"
        out = self._shell(f"pm {verb} --user {self.user_id} {INSTAGRAM}")
        if "error" in out.lower() or "unknown command" in out.lower():
            raise ADBError(f"Failed to {verb} Instagram: {out}")
        return not blocked

    def _is_blocked(self) -> bool:
        out = self._shell(f"dumpsys package {INSTAGRAM}")
        for line in out.splitlines():
            line = line.strip()
            if line.startswith(f"User {self.user_id}:"):
                return "suspended=true" in line
        raise ADBError(f"Could not read the state of {INSTAGRAM} — is it installed?")

    # --- plumbing ---------------------------------------------------------

    def _shell(self, command: str) -> str:
        return self._adb(["shell", command])

    def _adb(self, args: list[str], timeout: int = 30) -> str:
        out, code = self._run(args, timeout=timeout)
        if self._dropped(out, code):
            if not self.connect():
                raise ADBUnavailable(f"{self.serial} is not reachable — {out}")
            out, code = self._run(args, timeout=timeout)
            if self._dropped(out, code):
                raise ADBUnavailable(f"{self.serial} dropped again — {out}")
        return out

    @staticmethod
    def _dropped(out: str, code: int) -> bool:
        low = out.lower()
        return code != 0 and any(m in low for m in _GONE)

    def _run(self, args: list[str], device: bool = True,
             timeout: int = 30) -> tuple[str, int]:
        cmd = [self.adb_path] + (["-s", self.serial] if device else []) + args
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError:
            raise ADBError(f"adb not found at {self.adb_path!r}") from None
        except subprocess.TimeoutExpired:
            return "", 124
        return ((proc.stdout or "") + (proc.stderr or "")).strip(), proc.returncode


if __name__ == "__main__":
    phone = ADB()
    try:
        print("blocked" if phone.toggle_instagram() else "available")
    except ADBUnavailable as e:
        print("phone away:", e)