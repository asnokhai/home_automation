#!/usr/bin/env python3
"""Does the fireplace video play at all? Nothing else.

Standalone on purpose: it imports nothing from src/, builds no TapoController,
touches no action registry. If this script puts a fire on the screen then the
assistant can too, and if it does not then nothing in bindings.py was ever the
problem. Run it on the dev kit:

    python3 test/test_fireplace.py

**A player that is still running is not a player that is showing anything.**
mpv does not exit when its video output fails -- it says so, drops the video,
and carries on playing the audio, which is why "audio works but the screen
still shows the login prompt" looks identical to success from the outside. So
each attempt below is judged on what it *said*, not on whether it survived, and
audio-only is reported as the failure it is.

Two controls are in the list on purpose, and they are the useful part when
everything fails:

  --vo=null     decodes the file and draws nothing, deliberately. It is the
                shape every broken attempt collapses into, shown on purpose so
                the others can be compared against it.
  test pattern  a generated source instead of the file. If this works and the
                file does not, the file is too much to decode, not too hard
                to show.

The gst attempts are the ones that do not need X at all: on L4T the nvidia
sinks draw onto a display overlay above whatever X is doing, and decode in
hardware rather than on the CPU -- which matters for a 1724x970 60fps file.

Useful flags:
    --seconds 10        leave each attempt up longer
    --only gst          run just the attempts whose name contains this
    --video other.mp4   try a different file
    --no-cec            skip turning the TV on and switching it to this machine
"""

import argparse
import glob
import os
import shutil
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(HERE)
DEFAULT_VIDEO = os.path.join(PROJECT_ROOT, "resources", "videos", "fireplace.mp4")

CEC_DEVICE = "/dev/tegra_cec"
# Image View On to the TV, then Active Source for HDMI 1 -- the same two frames
# src/tv.py sends, inlined so this script depends on nothing.
CEC_FRAMES = ((b"\x40\x04", "TV on"), (b"\x4f\x82\x10\x00", "switch to HDMI 1"))

# What mpv says when it gave up on video and kept the audio. Any of these means
# the attempt failed no matter how healthy the process looks.
VIDEO_FAILED = (
    "error opening/initializing the selected video_out",
    "video: no video",
    "could not open x display",
    "failed to open x display",
    "could not open display",
    "no video output driver",
)

# Not --no-terminal: its output is the entire point of this script.
MPV_TAIL = ["--no-config", "--no-osc", "--fullscreen", "--loop-file=inf", "{video}"]

# gst-launch says this and then keeps the pipeline up in some configurations,
# so a survivor is not automatically a success here either.
GST_FAILED = ("erroneous pipeline", "no element", "could not link",
              "error: from element")

# Video only: qtdemux has an audio pad too and this links neither of them, so
# there is nothing for the sound to come out of. Kept because it is the one
# that put a picture up, and so the thing every candidate below is measured
# against.
def _gst_video_only(decoder, sink):
    return ["gst-launch-1.0", "filesrc", "location={video}", "!", "qtdemux",
            "!", "h264parse", "!", decoder, "!", sink]


# Both pads linked by hand: video through the nvidia decoder to the overlay,
# audio out to ALSA. `name=d` is what lets the two branches refer back to the
# one demuxer.
def _gst_both(decoder, sink):
    return ["gst-launch-1.0", "filesrc", "location={video}", "!",
            "qtdemux", "name=d",
            "d.video_0", "!", "queue", "!", "h264parse", "!", decoder, "!", sink,
            "d.audio_0", "!", "queue", "!", "aacparse", "!", "avdec_aac", "!",
            "audioconvert", "!", "audioresample", "!", "alsasink"]


# The candidates that could do the whole job come first now: the question is no
# longer "can anything draw" -- nvoverlaysink answered that -- but "what draws
# and plays sound at the same time".
ATTEMPTS = (
    ("gst playbin overlay",
     ["gst-launch-1.0", "playbin", "uri=file://{video}",
      "video-sink=nvoverlaysink"]),
    ("gst both branches", _gst_both("nvv4l2decoder", "nvoverlaysink")),
    ("gst both branches omx", _gst_both("omxh264dec", "nvoverlaysink")),
    ("nvgstplayer", ["nvgstplayer-1.0", "-i", "{video}"]),
    ("nvgstplayer loop", ["nvgstplayer-1.0", "-i", "{video}", "--loop-forever"]),

    # The known-good picture-without-sound, for comparison.
    ("gst nvoverlaysink video only",
     _gst_video_only("nvv4l2decoder", "nvoverlaysink")),

    # The mpv family: sound without picture, kept so a fix to the X side can be
    # spotted if one ever lands. Skip them with --only gst.
    ("mpv x11", ["mpv", "--vo=x11"] + MPV_TAIL),
    ("mpv opengl", ["mpv", "--vo=opengl"] + MPV_TAIL),
    ("mpv xv", ["mpv", "--vo=xv"] + MPV_TAIL),
)


def x_environment():
    """DISPLAY and XAUTHORITY, found the way src/tv.py finds them."""
    env = {}
    sockets = sorted(glob.glob("/tmp/.X11-unix/X[0-9]*"))
    if sockets:
        env["DISPLAY"] = ":" + os.path.basename(sockets[0])[1:]
    uid = os.getuid()
    for candidate in (f"/run/user/{uid}/gdm/Xauthority",
                      f"/run/user/{uid}/lightdm/Xauthority",
                      os.path.join(os.path.expanduser("~"), ".Xauthority")):
        if os.path.exists(candidate):
            env["XAUTHORITY"] = candidate
            break
    return env


def report_environment(env, video):
    print("== what is here ==")
    print("  video:    ", video, "" if os.path.exists(video) else "  <-- MISSING")
    for tool in ("mpv", "gst-launch-1.0", "nvgstplayer-1.0"):
        print(f"  {tool:16}", shutil.which(tool) or "not installed")
    print("  /dev/dri: ", sorted(glob.glob("/dev/dri/*")) or
          "does not exist -- no DRM output is possible, X is the only path")

    print()
    print("== the X server, and whether we may talk to it ==")
    print("  DISPLAY:   ", env.get("DISPLAY", "not found"))
    print("  XAUTHORITY:", env.get("XAUTHORITY", "not found"))

    # Who owns that server matters: a greeter's X server keeps its cookie
    # somewhere this user cannot read, and then every X attempt below fails
    # identically -- which is indistinguishable from a video problem unless it
    # is separated out here.
    xorg = subprocess.run(["pgrep", "-a", "Xorg"], capture_output=True, text=True)
    for line in (xorg.stdout or "").splitlines():
        pid = line.split()[0]
        who = subprocess.run(["ps", "-o", "user=", "-p", pid],
                             capture_output=True, text=True)
        print(f"  Xorg pid {pid} runs as {(who.stdout or '?').strip()}")
    cookie = env.get("XAUTHORITY")
    if cookie:
        print(f"  cookie readable by us: {os.access(cookie, os.R_OK)}")

    # Looping is the other half of the job and cannot be seen in a six-second
    # window on a long file, so the flags are read rather than tested. Whatever
    # cannot loop by itself has to be restarted by the caller, which costs a
    # black gap every time the file ends.
    if shutil.which("nvgstplayer-1.0"):
        helped = subprocess.run(["nvgstplayer-1.0", "--help"],
                                capture_output=True, text=True)
        loops = [l.strip() for l in
                 ((helped.stdout or "") + (helped.stderr or "")).splitlines()
                 if "loop" in l.lower()]
        print("  nvgstplayer loop options:", "; ".join(loops) or "none mentioned")

    if "DISPLAY" in env and shutil.which("xdpyinfo"):
        probe = subprocess.run(["xdpyinfo"], capture_output=True, text=True,
                               env={**os.environ, **env})
        if probe.returncode == 0:
            size = [l.strip() for l in probe.stdout.splitlines() if "dimensions" in l]
            print("  X connect: OK ", size[0] if size else "")
        else:
            print("  X connect: FAILED --", (probe.stderr or "").strip()[:120])
            print("             so every mpv x11/opengl/xv attempt will fall back")
            print("             to audio-only. The gst attempts do not use X and")
            print("             are the ones to watch.")
    print()


def send_cec():
    """Turn the TV on and put it on this machine's input, so there is something
    to look at. Best effort -- a failure here is not what is being tested."""
    if not os.path.exists(CEC_DEVICE):
        print(f"  no {CEC_DEVICE}; not touching the TV\n")
        return
    for frame, what in CEC_FRAMES:
        done = subprocess.run(["sudo", "-n", "tee", CEC_DEVICE], input=frame,
                              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        ok = "sent" if done.returncode == 0 else \
             "FAILED: " + (done.stderr or b"").decode(errors="replace").strip()
        print(f"  {what}: {ok}")
    print()


def attempt(name, template, video, seconds, env):
    """Run one player for a while. Returns 'video', 'audio only', or 'failed'."""
    command = [part.replace("{video}", video) for part in template]
    print(f"-- {name} " + "-" * max(0, 56 - len(name)))
    print("   " + " ".join(command))
    print(f"   >>> watch the TV for {seconds}s <<<")

    try:
        done = subprocess.run(command, capture_output=True, text=True,
                              timeout=seconds, stdin=subprocess.DEVNULL,
                              env={**os.environ, **env})
        survived, out, code = False, (done.stdout or "") + (done.stderr or ""), done.returncode
    except subprocess.TimeoutExpired as e:
        # Still running when the time ran out. Necessary for success, nowhere
        # near sufficient -- see the module docstring.
        survived, code = True, None
        out = "".join(_text(part) for part in (e.stdout, e.stderr))
    except FileNotFoundError:
        print("   not installed\n")
        return "failed"

    for line in [l.strip() for l in out.splitlines() if l.strip()][-8:]:
        print("     " + line)

    lowered = out.lower()
    # Two different kinds of "still running but wrong", and they mean opposite
    # things: mpv drops the video and plays on, while gst-launch can sit there
    # after refusing to build the pipeline at all.
    lost_video = any(marker in lowered for marker in VIDEO_FAILED)
    broken_pipeline = any(marker in lowered for marker in GST_FAILED)

    if survived and lost_video:
        verdict = "audio only"
        print("   => AUDIO ONLY. It kept playing, but it told us it could not "
              "open a video output.")
    elif survived and broken_pipeline:
        verdict = "failed"
        print("   => the pipeline errored; whatever is still running is not it")
    elif survived:
        verdict = "video"
        print(f"   => RAN for {seconds}s with no complaint. "
              "PICTURE? SOUND? Both have to be there.")
    else:
        verdict = "failed"
        print(f"   => died, exit {code}")
    print()
    return verdict


def _text(part):
    if part is None:
        return ""
    return part if isinstance(part, str) else part.decode("utf-8", "replace")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", default=DEFAULT_VIDEO)
    parser.add_argument("--seconds", type=float, default=6.0)
    parser.add_argument("--only", default="")
    parser.add_argument("--no-cec", action="store_true")
    args = parser.parse_args()

    env = x_environment()
    report_environment(env, args.video)

    if not args.no_cec:
        print("== putting the TV on this input first ==")
        send_cec()

    results = {}
    for name, template in ATTEMPTS:
        if args.only and args.only.lower() not in name.lower():
            continue
        results[name] = attempt(name, template, args.video, args.seconds, env)

    print("=" * 66)
    showed = [n for n, v in results.items() if v == "video"]
    audio = [n for n, v in results.items() if v == "audio only"]

    if showed:
        print("Ran without complaining:")
        for name in showed:
            print("   ", name)
        print()
        print("For each one, the only thing that settles it is what you saw and")
        print("heard: a fire on the TV AND crackling out of the speakers. A")
        print("pipeline with an unlinked audio pad runs perfectly happily in")
        print("silence, so 'it ran' proves nothing about the sound.")
    else:
        print("Nothing ran cleanly.")
    if audio:
        print()
        print("Audio but no picture:", ", ".join(audio))
    print()
    print("Say which one gave you both, and whether it was smooth. That is the")
    print("one to wire into src/tv.py -- along with how it should loop.")
