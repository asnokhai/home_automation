"""
editor.py — the show editor: a local page you open in a browser.

    python -m light_show.editor [show-name]

Python serves the page, the audio and the show file; the browser does playback,
scrubbing and the timeline. That split is deliberate — a browser seeks audio
perfectly and for free, where doing the same in a desktop toolkit means an audio
dependency and a lot of code.

With preview switched on, the bulbs follow the playhead as you work, so the show
is designed by eye rather than imagined.
"""

import asyncio
import json
import mimetypes
import os
import re
import threading
import webbrowser
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

def _bootstrap():
    """Allow this file to be run directly, not just as `python -m`.

    Executing a file inside a package gives it no parent, so every relative
    import in it fails. Put src/ on the path, import the package properly, and
    hand this module its package back.
    """
    import os
    import sys

    here = os.path.dirname(os.path.abspath(__file__))
    src = os.path.dirname(here)
    if src not in sys.path:
        sys.path.insert(0, src)
    import light_show                      # noqa: F401  (establishes the parent)
    return "light_show"


if __package__ in (None, ""):
    __package__ = _bootstrap()

from .show import Show, PROJECT_ROOT, LIGHTS

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")
PORT = 8080

mimetypes.add_type("audio/ogg", ".egg")   # Beat Saber ships Ogg under .egg


class Bulbs:
    """Live preview, on its own event loop thread.

    The server is threaded and synchronous while the Tapo library is async, so
    one loop runs here and requests are handed to it. Calls are coalesced per
    light: scrubbing generates far more updates than a bulb can accept, and the
    only one that matters is the most recent.
    """

    def __init__(self):
        self._loop = None
        self._player = None
        self._pending = {}
        self._busy = set()
        self._lock = threading.Lock()
        self.error = None
        self.ready = False

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            from tapo_controller import TapoController
            from .player import ShowPlayer

            controller = TapoController()
            self._loop.run_until_complete(controller.connect_to_lights())
            self._player = ShowPlayer(Show(), None, controller)
            self.ready = True
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            print(f"  [warn] live preview unavailable: {self.error}")
        self._loop.run_forever()

    def set(self, light, level, color):
        """Ask for a light to show this. Superseded requests are dropped."""
        if not self.ready:
            return False
        with self._lock:
            self._pending[light] = (level, color)
            if light in self._busy:
                return True                  # the in-flight call will pick it up
            self._busy.add(light)
        asyncio.run_coroutine_threadsafe(self._drain(light), self._loop)
        return True

    async def _drain(self, light):
        try:
            while True:
                with self._lock:
                    if light not in self._pending:
                        self._busy.discard(light)
                        return
                    level, color = self._pending.pop(light)
                try:
                    await self._player.apply(light, level, color)
                except Exception as exc:
                    print(f"  [warn] preview {light}: {exc}")
        finally:
            with self._lock:
                self._busy.discard(light)


class Handler(BaseHTTPRequestHandler):
    show = None
    bulbs = None
    # keep-alive: without it every seek reopens the connection to the audio
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass                                  # the default logs every asset fetch

    # ── helpers ────────────────────────────────────────────────────

    def _send(self, code, body=b"", ctype="application/json", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload, code=200):
        self._send(code, json.dumps(payload).encode("utf-8"))

    def _body(self):
        return json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))

    # ── audio, with range support so seeking is instant ────────────

    def _serve_audio(self):
        path = os.path.join(PROJECT_ROOT, Handler.show.audio)
        if not os.path.isfile(path):
            return self._json({"error": f"no audio at {Handler.show.audio}"}, 404)

        size = os.path.getsize(path)
        ctype = mimetypes.guess_type(path)[0] or "audio/ogg"
        rng = self.headers.get("Range")
        start, end = 0, size - 1

        if rng:
            match = re.match(r"bytes=(\d*)-(\d*)", rng)
            if match:
                first, last = match.groups()
                # a browser asks for the tail of the file when it seeks; without
                # 206 support it re-downloads from zero every time
                start = int(first) if first else size - int(last)
                end = int(last) if first and last else size - 1
        start, end = max(0, start), min(end, size - 1)

        with open(path, "rb") as f:
            f.seek(start)
            chunk = f.read(end - start + 1)

        headers = {"Accept-Ranges": "bytes"}
        if rng:
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        self._send(206 if rng else 200, chunk, ctype, headers)

    # ── routes ─────────────────────────────────────────────────────

    def do_GET(self):
        route = self.path.split("?")[0]

        if route in ("/", "/index.html"):
            return self._file(os.path.join(STATIC, "index.html"))
        if route.startswith("/static/"):
            name = os.path.basename(route)
            return self._file(os.path.join(STATIC, name))
        if route == "/api/show":
            return self._json(Handler.show.to_dict())
        if route == "/api/state":
            return self._json({
                "name": Handler.show.name,
                "lights": Handler.show.lights,
                "preview": Handler.bulbs.ready,
                "preview_error": Handler.bulbs.error,
            })
        if route == "/audio":
            return self._serve_audio()
        self._json({"error": "not found"}, 404)

    def do_PUT(self):
        if self.path == "/api/show":
            data = self._body()
            Handler.show.cues = data.get("cues", [])
            Handler.show.offset = data.get("offset", Handler.show.offset)
            Handler.show.duration = data.get("duration", Handler.show.duration)
            Handler.show.presets = data.get("presets", Handler.show.presets)
            Handler.show.bpm = data.get("bpm", Handler.show.bpm)
            Handler.show.beat_offset = data.get("beatOffset", Handler.show.beat_offset)
            path = Handler.show.save()
            return self._json({"saved": os.path.relpath(path, PROJECT_ROOT),
                               "cues": len(Handler.show.cues)})
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        if self.path == "/api/preview":
            data = self._body()
            color = tuple(data["color"]) if data.get("color") else None
            ok = Handler.bulbs.set(data["light"], data.get("level"), color)
            return self._json({"sent": ok})
        self._json({"error": "not found"}, 404)

    def _file(self, path):
        if not os.path.isfile(path):
            return self._json({"error": "not found"}, 404)
        with open(path, "rb") as f:
            body = f.read()
        self._send(200, body, mimetypes.guess_type(path)[0] or "text/plain")


def serve(name="untitled", port=PORT, open_browser=True, song=None, audio=None,
          track_uri=None):
    path = Show.path_for(name)
    show = Show.load(name) if os.path.isfile(path) else Show(name=name, lights=LIGHTS)
    show.name = name

    # a new show has no song yet, and hand-editing JSON is not "easy to use"
    before = show.to_dict()
    if song:
        show.song = song
    if audio:
        show.audio = os.path.relpath(os.path.abspath(audio), PROJECT_ROOT)             if os.path.isabs(audio) else audio
    if track_uri:
        show.track_uri = track_uri

    # only write when something actually changed: the settings above are passed
    # on every launch, and rewriting a show full of work just to restate them is
    # a needless risk
    if not os.path.isfile(path) or show.to_dict() != before:
        show.save()

    if show.audio and not os.path.isfile(os.path.join(PROJECT_ROOT, show.audio)):
        print(f"  [warn] no audio at {show.audio} — the timeline will have no "
              f"waveform and nothing to play")

    Handler.show = show
    Handler.bulbs = Bulbs()
    Handler.bulbs.start()

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"Show editor for {name!r} — {len(show.cues)} cues")
    print(f"  song  : {show.song or '(not set)'}")
    print(f"  audio : {show.audio or '(not set)'}")
    print(f"  open  : {url}")
    print("  Ctrl-C to stop")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped. Show saved at", Show.path_for(name))


if __name__ == "__main__":
    serve("nightmare",
          song="Nightmare - Avenged Sevenfold",
          audio="resources/audio/nightmare.mp3")
