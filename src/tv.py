r"""The TV: power, inputs, and the fireplace on it.

Control goes out over HDMI-CEC, and on this box CEC is a character device --
`/dev/tegra_cec` -- that takes raw frames. Writing two bytes to it is the whole
protocol stack:

    printf '\x40\x04' | sudo tee /dev/tegra_cec > /dev/null

so there is no daemon to keep alive, no libcec to build, and nothing to connect
to. What there is instead is a permission problem and a one-way street.

**The permission problem.** The device node is root-owned, and this assistant
runs as a systemd user service with no terminal, so an interactive sudo prompt
cannot be answered -- it would hang the command until the timeout rather than
fail. Hence `sudo -n`, the same reasoning as `system.py`'s reboot, and hence
`scripts/allow_cec.sh`, which installs the one NOPASSWD rule that makes it
work. The command is `tee` rather than `sh -c 'printf ... > /dev/tegra_cec'` on
purpose: sudoers matches arguments, so the rule can be pinned to *that one
file*. A rule for `sh` would be a rule for a root shell.

**The one-way street.** We only ever write. A successful write means "the frame
went onto the bus", never "the TV did it" -- the set may be unplugged, may have
CEC turned off in a menu named whatever the manufacturer invented for it
(Anynet+, Bravia Sync, SimpLink), or may just ignore an opcode it does not
implement. Nothing here can tell those apart, so every method reports what it
sent, and none of them report what the TV is.

**The fireplace** is a GStreamer `playbin` on a loop, and how it got there is
worth keeping, because the obvious answer does not work on this machine. There
is no /dev/dri, so nothing can draw through DRM; the Xorg that does exist will
not take a connection from the service; and mpv's response to a video output it
cannot open is not to exit but to drop the video and go on playing the audio.
The result looked exactly like a working fireplace from the code's side, while
the TV sat there showing the login screen. nvidia's `nvoverlaysink` has neither
problem -- it draws onto a display plane above X, needing no X connection, and
decodes in hardware, which a 1724x970 60fps file needs.

Two things about that are easy to get wrong. A hand-built pipeline linking only
qtdemux's video pad plays in perfect silence, which is why `playbin` does the
demuxing here; and gst-launch plays the file once and exits, so the loop is a
shell around it rather than a flag -- see `_spawn` for why `|| exit 1` is what
keeps the start check honest.

Everything a player says goes to a file rather than /dev/null, so a failure can
be quoted: "exit code 2" is not something anyone can act on, and the failures
here all look alike from outside. `test/test_fireplace.py` is the standalone
version of this whole search, for when it needs doing again.

Audio goes to the default ALSA device, which is worth knowing about: while the
player holds that device, the `aplay` behind every canned clip and spoken reply
may fail to open the card unless the default is a dmix. If the assistant goes
quiet whenever the fire is lit, that is this.
"""

from __future__ import annotations

import asyncio
import glob
import os
import shlex
import shutil
import signal
import subprocess
import tempfile
import threading

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CEC_DEVICE = "/dev/tegra_cec"

# A CEC frame is a header byte -- (initiator << 4) | destination -- then an
# opcode, then operands. We speak as Playback Device 1 (4), which is what a box
# plugged into an HDMI port is meant to claim. The TV is always 0, and F is the
# broadcast address every device on the bus listens to.
SELF_ADDRESS = 0x4
TV_ADDRESS = 0x0
BROADCAST = 0xF

OP_IMAGE_VIEW_ON = 0x04   # "wake up and show something"
OP_STANDBY = 0x36
OP_ACTIVE_SOURCE = 0x82   # broadcast: the device at this address is now the source

# The two inputs that matter, by HDMI port number. Port n has physical address
# n.0.0.0, which goes on the wire as the two bytes `n0 00`.
INPUTS = {
    "dev kit": 1,   # this machine
    "desktop": 2,   # the desktop PC that desktop.py wakes
}

# What people actually say, squashed to alphanumerics. Without these the model
# saying "the pc" resolves to nothing and the TV stays where it is.
ALIASES = {
    "pc": "desktop",
    "desktoppc": "desktop",
    "computer": "desktop",
    "devkit": "dev kit",
    "jetson": "dev kit",
    "assistant": "dev kit",
}

CEC_GAP = 0.1        # seconds between two frames; CEC is a slow single-wire bus
CEC_TIMEOUT = 5      # a write to a char device either lands at once or is wedged

# Anchored to __file__, not the working directory: main.py is started from
# inside src/, and a CWD-relative path would resolve to src/resources.
FIREPLACE_PATH = os.path.join(PROJECT_ROOT, "resources", "videos", "fireplace.mp4")

# How to put the fireplace on the panel, tried in order until one works. Each
# is a full command; `{video}` is replaced with the path.
#
# mpv is not the answer here and the reason is worth keeping. This box has no
# /dev/dri at all, so no mpv output can reach the display that way, and the
# Xorg it does have would not accept a connection from the service -- so every
# mpv attempt played the audio, reported that it had given up on video, and
# left the login screen sitting on the TV. nvidia's overlay sink has neither
# problem: it draws onto a display plane above X without an X connection, and
# decodes in hardware, which this file needs -- 1724x970 at 60fps is more than
# this CPU will carry in software.
#
# playbin is what makes it one command instead of two. It demuxes the file and
# connects both the picture and the sound itself; `video-sink` pins the half
# that had to be discovered. A hand-built pipeline linking only the video pad
# plays in silence, which is the shape of the first attempt at this.
PLAYERS = (
    ("playbin", ("gst-launch-1.0", "playbin", "uri=file://{video}",
                 "video-sink=nvoverlaysink")),
    # The same thing spelled out, for when playbin chooses something unhelpful.
    # `name=d` is what lets the two branches refer back to the one demuxer.
    ("pipeline", ("gst-launch-1.0", "filesrc", "location={video}", "!",
                  "qtdemux", "name=d",
                  "d.video_0", "!", "queue", "!", "h264parse", "!",
                  "nvv4l2decoder", "!", "nvoverlaysink",
                  "d.audio_0", "!", "queue", "!", "aacparse", "!", "avdec_aac",
                  "!", "audioconvert", "!", "audioresample", "!", "alsasink")),
    # Last, and expected to fail on this machine. It is what starts working if
    # this is ever run somewhere X behaves normally.
    ("mpv", ("mpv", "--vo=x11", "--loop-file=inf", "--fullscreen",
             "--no-config", "--no-osc", "--no-terminal", "{video}")),
)

# gst-launch says one of these and can then sit there with a pipeline it never
# managed to build, so a survivor is not automatically a success.
GST_FAILED = ("erroneous pipeline", "no element", "could not link",
              "error: from element")

# What mpv says when it gave up on video and kept playing the audio. This is
# the failure that does not look like one: the process stays up, the speakers
# crackle, and the screen goes on showing whatever was already on it. Watching
# `poll()` alone reports that as a lit fireplace, which is how "it plays but
# there is no picture" survived a round of testing.
VIDEO_FAILED = (
    "error opening/initializing the selected video_out",
    "video: no video",
    "could not open x display",
    "failed to open x display",
    "could not open display",
    "no video output driver",
)

# Long enough for mpv to fail *and say so*: it gives up on a video output
# within milliseconds, but the message still has to reach the log before the
# check below reads it.
PLAYER_START_CHECK = 1.0
PLAYER_STOP_GRACE = 3.0

# SIGKILL where there is one. Named rather than used inline so this module
# still imports on a machine that has no such signal.
_HARD = getattr(signal, "SIGKILL", signal.SIGTERM)


class TVError(RuntimeError):
    """Raised when a frame could not be sent, or the fireplace could not play."""


def _frame(destination: int, opcode: int, *operands: int) -> bytes:
    """Build one CEC frame: header, opcode, operands."""
    return bytes(((SELF_ADDRESS << 4) | destination, opcode, *operands))


def _x_environment() -> dict:
    """DISPLAY and XAUTHORITY for a process that inherited neither.

    There is an X server on vt1, but nothing that starts mpv can see it: a
    systemd user service inherits no X environment, and an ssh session does not
    either -- `DISPLAY` is simply unset in both. Without these two variables mpv
    fails to connect to a server sitting right there.

    Both are discovered rather than assumed. The display comes from the sockets
    X actually created, so a server on :1 is found as readily as one on :0, and
    the cookie from wherever the display manager put it -- gdm keeps it under
    the user's own runtime directory, not in the home directory where the
    convention would put it.
    """
    env = {}

    sockets = sorted(glob.glob("/tmp/.X11-unix/X[0-9]*"))
    if sockets:
        env["DISPLAY"] = ":" + os.path.basename(sockets[0])[1:]

    uid = os.getuid() if hasattr(os, "getuid") else 0
    for candidate in (f"/run/user/{uid}/gdm/Xauthority",
                      f"/run/user/{uid}/lightdm/Xauthority",
                      os.path.join(os.path.expanduser("~"), ".Xauthority")):
        if os.path.exists(candidate):
            env["XAUTHORITY"] = candidate
            break

    return env


def _tail(path, lines: int = 3) -> str:
    """The last few non-empty lines of a log, as one speakable-ish string."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            kept = [line.strip() for line in f if line.strip()]
    except OSError:
        return ""
    return " / ".join(kept[-lines:])


def _discard(path) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _signal_group(proc, hard: bool) -> None:
    """Signal the player *and* the shell looping it.

    The child is a shell with the player inside it, so signalling the child
    alone would stop the loop and leave the fireplace burning with nothing
    holding its handle. The whole process group goes at once.
    """
    sig = _HARD if hard else signal.SIGTERM
    try:
        os.killpg(os.getpgid(proc.pid), sig)
    except (AttributeError, OSError):
        # No process groups here, or it is already gone. Either way the direct
        # signal is the best that can be done.
        try:
            proc.kill() if hard else proc.terminate()
        except OSError:
            pass


def _terminate(proc, grace: float = PLAYER_STOP_GRACE) -> bool:
    """Ask a player to stop, then insist. Returns whether it was running."""
    if proc is None or proc.poll() is not None:
        return False
    _signal_group(proc, hard=False)
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        _signal_group(proc, hard=True)
    return True


class TV:
    def __init__(self, cec_device: str = CEC_DEVICE, inputs: dict = None,
                 own_input: str = "dev kit", fireplace_path: str = FIREPLACE_PATH,
                 players=PLAYERS):
        self.cec_device = cec_device
        self.inputs = dict(inputs or INPUTS)
        if own_input not in self.inputs:
            raise ValueError(
                f"own_input {own_input!r} is not one of {sorted(self.inputs)}"
            )
        self.own_input = own_input
        self.fireplace_path = fireplace_path
        self.players = [(name, list(template)) for name, template in players]
        self._player = None
        # Where mpv's own complaints go. Kept beside the handle because the
        # only moment they are worth reading is when the process is already
        # gone, and by then a pipe would have died with it.
        self._player_log = None
        # Index into players of the one that last worked. Tried first next
        # time, so the wrong guesses are paid for once per boot.
        self._working_player = 0
        # Guards the swap in _stop_player. The player is started and stopped
        # from the event loop and from to_thread workers both, and a double
        # kill on a reused pid is not worth risking for the price of a lock.
        self._lock = threading.Lock()

    # -- power and inputs -------------------------------------------------

    async def turn_on(self) -> str:
        """Wake the TV. Returns a sentence to speak."""
        await self._send(_frame(TV_ADDRESS, OP_IMAGE_VIEW_ON), "turning the TV on")
        return "TV on"

    async def turn_off(self) -> str:
        """Put the TV into standby, stopping the fireplace first.

        The player goes first because it would otherwise keep decoding video
        into a dark panel for as long as the assistant runs, and still be there
        the next time the TV comes on.
        """
        await asyncio.to_thread(self._stop_player)
        await self._send(_frame(TV_ADDRESS, OP_STANDBY), "turning the TV off")
        return "TV off"

    async def switch_input(self, name) -> str:
        """Switch the TV to one of the inputs in INPUTS."""
        port = self._resolve(name)

        # Image View On first: a TV in standby ignores a broadcast Active
        # Source, so without this, switching inputs silently does nothing
        # whenever the TV happens to be off. It is defined as a no-op on a set
        # that is already awake, so it costs one frame and removes a whole
        # class of "it didn't work".
        await self._send(_frame(TV_ADDRESS, OP_IMAGE_VIEW_ON), "waking the TV")
        await asyncio.sleep(CEC_GAP)
        await self._send(
            _frame(BROADCAST, OP_ACTIVE_SOURCE, port << 4, 0x00),
            f"switching to HDMI {port}")

        return f"Switching to the {self._label_of(port)}"

    # -- the fireplace ----------------------------------------------------

    async def show_fireplace(self) -> str:
        """Put the looping fireplace on the TV, turning it on and switching to us.

        Tries each entry in `players` until one is running, and quiet about it,
        after PLAYER_START_CHECK. If none is, the error carries what the player
        actually said rather than an exit code -- which is the whole point of
        writing its output to a file instead of /dev/null.
        """
        if not os.path.exists(self.fireplace_path):
            raise TVError(f"no fireplace video at {self.fireplace_path}")

        await self.turn_on()
        await asyncio.sleep(CEC_GAP)
        await self.switch_input(self.own_input)

        await asyncio.to_thread(self._stop_player)

        failures = []
        for index in self._player_order():
            name, template = self.players[index]
            if shutil.which(template[0]) is None:
                failures.append(f"{name}: {template[0]} is not installed")
                continue

            proc, log_path = self._spawn(template)

            with self._lock:
                self._player, self._player_log = proc, log_path

            await asyncio.sleep(PLAYER_START_CHECK)

            # Alive is necessary and nowhere near sufficient: mpv answers a
            # video output it cannot open by dropping the video and playing on.
            # So the log is read either way, and what it says decides.
            said = _tail(log_path)
            lost_video = any(m in said.lower()
                             for m in VIDEO_FAILED + GST_FAILED)

            if proc.poll() is None and not lost_video:
                self._working_player = index
                return "Fireplace on"

            # Take the handle back before killing and reading, so a concurrent
            # stop cannot delete the file underneath us.
            with self._lock:
                if self._player is proc:
                    self._player, self._player_log = None, None
            # Which of the two failures this was, reported honestly: a player
            # still running here is one that gave up on video and kept going,
            # and it has to be killed. One that is already gone just exited.
            if proc.poll() is None:
                await asyncio.to_thread(_terminate, proc)
                outcome = "running, but with no picture"
            else:
                outcome = f"exit {proc.returncode}"
            _discard(log_path)
            print(f"  fireplace via {name} -> {outcome}: {said}")
            failures.append(f"{name}: {said or outcome}")

        raise TVError(
            f"no video output worked ({'; '.join(failures)}) -- run "
            "test/test_fireplace.py on the dev kit"
        )

    async def stop_fireplace(self) -> str:
        """Stop the fireplace video. Harmless when nothing is playing."""
        stopped = await asyncio.to_thread(self._stop_player)
        return "Fireplace off" if stopped else "The fireplace was not playing"

    def close(self) -> None:
        """Kill the player on the way out. Safe when it never started."""
        self._stop_player()

    # -- internals --------------------------------------------------------

    async def _send(self, frame: bytes, what: str) -> None:
        """Write one frame to the CEC device, translating every failure."""
        if not os.path.exists(self.cec_device):
            raise TVError(
                f"{self.cec_device} is not there -- the tegra_cec driver is not "
                "loaded, so nothing can reach the TV"
            )

        try:
            # to_thread, not a bare run: blocking the event loop here would
            # freeze the controller poll and the mic pump with it.
            proc = await asyncio.to_thread(
                subprocess.run, ["sudo", "-n", "tee", self.cec_device],
                input=frame, stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE, timeout=CEC_TIMEOUT,
            )
        except FileNotFoundError:
            raise TVError("sudo not found -- cannot write to the CEC device") from None
        except subprocess.TimeoutExpired:
            raise TVError(f"{what}: the CEC device did not accept the write") from None

        if proc.returncode == 0:
            return

        detail = (proc.stderr or b"").decode("utf-8", "replace").strip()
        # First line only: sudo answers a refusal with a paragraph, and this
        # string gets spoken on the way out.
        detail = detail.splitlines()[0] if detail else str(proc.returncode)
        if "password" in detail.lower():
            raise TVError(
                f"{what} failed: sudo wants a password -- run "
                "scripts/allow_cec.sh once on the dev kit"
            )
        raise TVError(f"{what} failed: {detail}")

    def _resolve(self, name) -> int:
        """Turn whatever was said into an HDMI port number."""
        squashed = "".join(ch for ch in str(name).lower() if ch.isalnum())

        # "the pc", "the desktop" -- the model says it more often than not, and
        # no label here starts with those three letters.
        if squashed.startswith("the") and len(squashed) > 3:
            squashed = squashed[3:]
        if squashed.startswith("hdmi"):
            squashed = squashed[4:]
        if squashed.isdigit():
            port = int(squashed)
            if port in self.inputs.values():
                return port
            raise TVError(f"nothing is on HDMI {port}; {self._known()}")

        squashed = ALIASES.get(squashed, squashed)
        for label, port in self.inputs.items():
            if "".join(label.split()) == "".join(squashed.split()):
                return port

        raise TVError(f"I do not know the input {name!r}; {self._known()}")

    def _player_order(self):
        """Indices into players, the one that last worked going first."""
        first = self._working_player
        return [first] + [i for i in range(len(self.players)) if i != first]

    def _spawn(self, template):
        """Start one player on a loop, its complaints going to a file.

        A file rather than a pipe: nobody is reading while the fireplace burns,
        and a full pipe buffer would wedge the player an hour into the evening.

        The loop is a shell rather than something in here, because gst-launch
        plays the file once and exits -- which makes a five minute clip, not a
        fireplace. `|| exit 1` is the part that matters: without it the shell
        would outlive a player that cannot start and would sit there respawning
        it forever, and the start check below would call that success.
        """
        command = [part.replace("{video}", self.fireplace_path)
                   for part in template]
        script = "while :; do %s || exit 1; done" % \
                 " ".join(shlex.quote(part) for part in command)

        env = os.environ.copy()
        # setdefault, not assignment: a DISPLAY that was deliberately set --
        # running this by hand from a desktop session -- should win over
        # anything guessed from the sockets on disk.
        for key, value in _x_environment().items():
            env.setdefault(key, value)

        fd, log_path = tempfile.mkstemp(prefix="fireplace-", suffix=".log")
        try:
            with os.fdopen(fd, "wb") as log:
                # Its own session, so the shell and the player it spawns share
                # a process group that can be signalled as one. Without it,
                # stopping the fireplace would kill the shell and orphan the
                # player still holding the display.
                proc = subprocess.Popen(["sh", "-c", script], stdout=log,
                                        stderr=subprocess.STDOUT, env=env,
                                        start_new_session=True)
        except OSError as e:
            _discard(log_path)
            raise TVError(f"could not start {command[0]}: {e}") from None

        return proc, log_path

    def _label_of(self, port: int) -> str:
        for label, value in self.inputs.items():
            if value == port:
                return label
        return f"HDMI {port}"

    def _known(self) -> str:
        return "the inputs are " + " and ".join(sorted(self.inputs))

    def _stop_player(self) -> bool:
        """Kill the player if it is running. Returns whether it was.

        Swap first, act second: taking the handle out under the lock means a
        concurrent caller cannot terminate the same process twice, and the wait
        happens outside the lock so it cannot hold anyone up.
        """
        with self._lock:
            proc, self._player = self._player, None
            log_path, self._player_log = self._player_log, None

        was_running = _terminate(proc)

        # After the process is gone, not before: it still holds the file open,
        # and there are systems where that alone makes the delete fail.
        if log_path:
            _discard(log_path)

        return was_running


if __name__ == "__main__":
    import sys

    USAGE = "usage: python tv.py on|off|devkit|desktop|fireplace"

    async def _main(argv):
        tv = TV()
        try:
            command = (argv[1] if len(argv) > 1 else "").lower()
            if command == "on":
                print(await tv.turn_on())
            elif command == "off":
                print(await tv.turn_off())
            elif command in ("devkit", "desktop"):
                print(await tv.switch_input(command))
            elif command == "fireplace":
                print(await tv.show_fireplace())
                print("Ctrl-C to stop it.")
                while True:
                    await asyncio.sleep(1)
            else:
                print(USAGE)
        finally:
            # The player belongs to this process, so leaving without it would
            # orphan mpv on the console with no way back to the terminal.
            tv.close()

    try:
        asyncio.run(_main(sys.argv))
    except KeyboardInterrupt:
        pass
