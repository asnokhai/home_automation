"""Control of the Pi this assistant runs on.

Two things live here, one heavy and one light. Both are separate from
`desktop.py` on purpose -- that module wakes a *different* machine over the
network, while everything here acts on the local host and takes the assistant
down with it.

**Rebooting** the Pi has two ways in, tried in order, because neither works
everywhere:

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

**Restarting the service** needs none of that. `systemctl --user restart` acts
on a unit of the same user's own systemd instance, so there is nobody to ask
for permission -- which is why it gets one command and no fallback chain.

What it does share with reboot is that the answer never arrives. The
`systemctl` child we spawn lives inside the service's own cgroup, so systemd
kills it while stopping the unit it was asked to restart. The restart still
happens -- the job is queued with systemd, not held by the child -- but this
process is gone before the return code lands. That is why both actions speak
*first* and treat their return value as a failure-path detail.
"""

from __future__ import annotations

import asyncio
import subprocess

# How long to let the spoken confirmation play before the process goes away.
# The clip is queued on SoundPlayer's worker thread, so without a pause here
# the reboot lands first and the only feedback is silence.
SPEAK_GRACE_SECONDS = 1.5

REBOOT_COMMANDS = (
    ("sudo", "-n", "reboot"),
    ("systemctl", "reboot"),
)

# The unit main.py runs under on the Pi. Kept as a constant because it appears
# twice: in the command and in the error that tells you where to read the logs.
SERVICE_NAME = "home_automation.service"

RESTART_COMMANDS = (
    ("systemctl", "--user", "restart", SERVICE_NAME),
)


class RebootError(RuntimeError):
    """Raised when the reboot could not be requested."""


class ServiceRestartError(RuntimeError):
    """Raised when the service restart could not be requested."""


class System:
    def __init__(self, sound_player, reboot_commands=REBOOT_COMMANDS,
                 restart_commands=RESTART_COMMANDS):
        # The SoundPlayer is injected for the same reason Timers takes one:
        # these actions have to speak *before* they act, and run_action only
        # speaks after the call returns -- by which point there is no process
        # left to speak with. So the confirmation is played here, and the
        # Actions are say=None.
        self._sound_player = sound_player
        self._reboot_commands = [list(cmd) for cmd in reboot_commands]
        self._restart_commands = [list(cmd) for cmd in restart_commands]

    async def _run_first_working(self, commands) -> list[str]:
        """Run each command until one succeeds.

        Returns an empty list on success, or one string per command describing
        why it did not work -- short enough to be spoken, since that is where
        these end up.
        """
        failures = []
        for command in commands:
            try:
                # to_thread, not a bare run: blocking the event loop here would
                # freeze the mic pump and the controller poll with it.
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
                return []

            detail = ((proc.stderr or "") + (proc.stdout or "")).strip()
            # First line only: polkit in particular answers with a paragraph,
            # and this string gets spoken on the way out.
            detail = detail.splitlines()[0] if detail else str(proc.returncode)
            failures.append(f"{command[0]}: {detail}")

        return failures

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

        failures = await self._run_first_working(self._reboot_commands)
        if not failures:
            return "Rebooting"

        raise RebootError(
            "reboot failed (" + "; ".join(failures) + ") -- run "
            "scripts/allow_reboot.sh once on the pi to grant passwordless "
            "sudo for reboot"
        )

    async def restart_service(self) -> str:
        """Restart the assistant's own systemd user service, after announcing it.

        The cheap counterpart to `reboot`: a few seconds of silence rather than
        a minute of dark flat, and enough to pick up a code change or shake off
        a wedged session. Same announce-then-act shape, and for the same reason
        -- see the module docstring on why the return value rarely arrives.
        """
        self._sound_player.say("restarting")
        await asyncio.sleep(SPEAK_GRACE_SECONDS)

        failures = await self._run_first_working(self._restart_commands)
        if not failures:
            return "Restarting"

        raise ServiceRestartError(
            f"restarting {SERVICE_NAME} failed (" + "; ".join(failures) + ") -- "
            f"check 'systemctl --user status {SERVICE_NAME}' on the pi"
        )
