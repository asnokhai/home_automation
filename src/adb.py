"""Minimal adb wrapper: connect to the phone over wireless adb, toggle Instagram."""

from __future__ import annotations

import subprocess

from config import PHONE_IP, ADB_PORT

ADB_PATH = "/usr/bin/adb"
INSTAGRAM = "com.instagram.android"


class ADBError(RuntimeError):
    """Raised when an adb command fails."""


class ADB:
    def __init__(self, host: str = PHONE_IP, port: int = ADB_PORT,
                 adb_path: str = ADB_PATH, user_id: int = 0):
        self.serial = f"{host}:{port}"
        self.adb_path = adb_path
        self.user_id = user_id
        self.connect()

    def connect(self) -> str:
        out = self._adb(["connect", self.serial], device=False)
        if "unable to connect" in out.lower() or "failed" in out.lower():
            raise ADBError(f"Could not connect to {self.serial} — {out}")
        return self.serial

    def toggle_instagram(self) -> bool:
        """Flip Instagram between blocked and available.

        Returns True if it is now blocked, False if it is now usable.

        Uses pm suspend/unsuspend rather than disable-user, so the launcher
        icon keeps its position. Swap for "pm disable-user" / "pm enable" if
        you'd rather the app disappear from the drawer entirely.
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

    def _shell(self, command: str) -> str:
        return self._adb(["shell", command])

    def _adb(self, args: list[str], device: bool = True) -> str:
        cmd = [self.adb_path] + (["-s", self.serial] if device else []) + args
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        except FileNotFoundError:
            raise ADBError(f"adb not found at {self.adb_path!r}") from None
        except subprocess.TimeoutExpired:
            raise ADBError(f"Timed out: {' '.join(cmd)}") from None
        return ((proc.stdout or "") + (proc.stderr or "")).strip()


if __name__ == "__main__":
    phone = ADB()
    print("blocked" if phone.toggle_instagram() else "available")