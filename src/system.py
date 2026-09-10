"""Control of the Pi this assistant runs on.

Right now that is one thing: rebooting. Separate from `desktop.py` on purpose --
that module wakes a *different* machine over the network, while everything here
acts on the local host and takes the assistant down with it.

Two ways in, tried in order, because neither works everywhere:

  sudo -n reboot     the direct route, and the reason `-n` is there: this runs
                     as a systemd user service with no tty, so an interactive
                     password prompt cannot be answered and would hang until
                     the timeout. `-n` turns that into an instant, reportable
                     failure instead. It needs a NOPASSWD sudoers rule --
                     `scripts/allow_reboot.sh` installs one for reboot alone.
  systemctl reboot   no sudo at all: logind decides, via polkit, which allows
                     an active local session to reboot. Whether the user
                     service counts as one depends on the login setup, so this
                     is a fallback rather than the first choice.

If both refuse, the error names the script, because the fix is one command on
the Pi and the alternative is guessing at it from a spoken error message.
"""

from __future__ import annotations

import asyncio
import subprocess

# How long to let the spoken confirmation play before the kernel goes down.
# The clip is queued on SoundPlayer's worker thread, so without a pause here
# the reboot lands first and the only feedback is silence.
SPEAK_GRACE_SECONDS = 1.5

REBOOT_COMMANDS = (
    ("sudo", "-n", "reboot"),
    ("systemctl", "reboot"),
)


class RebootError(RuntimeError):
    """Raised when the reboot could not be requested."""


class System:
    def __init__(self, sound_player, reboot_commands=REBOOT_COMMANDS):
        # The SoundPlayer is injected for the same reason Timers takes one: this
        # action has to speak *before* it acts, and run_action only speaks after
        # the call returns -- by which point there is no process left to speak
        # with. So the confirmation is played here, and the Action is say=None.
        self._sound_player = sound_player
        self._reboot_commands = [list(cmd) for cmd in reboot_commands]

    async def reboot(self) -> str:
        """Reboot the Pi, after announcing it.

        Returns a sentence describing what happened, which is what the realtime
        pathway hands back to the model. On success that value almost never
        arrives anywhere -- systemd starts killing this process about the same
        time -- so the pre-announcement above is the real confirmation, and the
        return value matters mostly for the failure path. The flip side of
        announcing first is that a reboot which then fails has already been
        heard as happening -- unavoidable, since there is no 'after' to speak in.
        """
        self._sound_player.say("rebooting")
        # Async, not time.sleep: the speech worker is a thread, but blocking the
        # event loop here would also freeze the mic pump and the controller poll
        # for the grace period.
        await asyncio.sleep(SPEAK_GRACE_SECONDS)

        failures = []
        for command in self._reboot_commands:
            try:
                proc = await asyncio.to_thread(
                    subprocess.run, command,
                    capture_output=True, text=True, timeout=10,
                )
            except FileNotFoundError:
                failures.append(f"{command[0]} not installed")
                continue
            except subprocess.TimeoutExpired:
                failures.append(f"{command[0]} timed out")
                continue

            if proc.returncode == 0:
                return "Rebooting"

            detail = ((proc.stderr or "") + (proc.stdout or "")).strip()
            # First line only: polkit in particular answers with a paragraph,
            # and this string gets spoken on the way out.
            detail = detail.splitlines()[0] if detail else str(proc.returncode)
            failures.append(f"{command[0]}: {detail}")

        raise RebootError(
            "reboot failed (" + "; ".join(failures) + ") -- run "
            "scripts/allow_reboot.sh once on the pi to grant passwordless "
            "sudo for reboot"
        )
