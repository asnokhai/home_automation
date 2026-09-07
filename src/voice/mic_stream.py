"""The one owner of the ESP32 mic stream.

Only one socket can bind UDP :5005, so the two voice backends share a single
reader instead of each opening their own. A daemon thread does the blocking
recvfrom and pushes fixed-size frames onto a queue; consumers pull them either
blocking (from a worker thread) or with a cancellable await, which is what lets
the facade tear a backend down mid-frame when the mode changes.
"""

import asyncio
import queue
import socket
import threading

import numpy as np

RATE = 16000
FRAME = 1280      # 80 ms -- the window openWakeWord scores
HEADER = 4        # the ESP32 prefixes a little-endian uint32 sequence counter

# ~16 s of audio. Deep enough that a slow consumer does not lose speech, shallow
# enough that a stalled one cannot grow the queue without bound.
QUEUE_FRAMES = 200


class MicStream:
    """UDP 16 kHz mono int16 in, 80 ms int16 frames out."""

    def __init__(self, port=5005, maxsize=QUEUE_FRAMES):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("0.0.0.0", port))
        self._q = queue.Queue(maxsize=maxsize)
        self._thread = threading.Thread(target=self._reader, daemon=True)
        self._started = False

    def start(self):
        if not self._started:
            self._started = True
            self._thread.start()
        return self

    def _reader(self):
        buf = np.zeros(0, dtype=np.int16)
        while True:
            try:
                pkt, _ = self.sock.recvfrom(2048)
            except OSError:      # socket closed on shutdown
                return
            buf = np.concatenate([buf, np.frombuffer(pkt[HEADER:], dtype="<i2")])
            while len(buf) >= FRAME:
                frame, buf = buf[:FRAME], buf[FRAME:]
                try:
                    self._q.put_nowait(frame)
                except queue.Full:
                    # Drop the oldest frame rather than the newest: a consumer
                    # that fell behind should catch up at the live edge.
                    try:
                        self._q.get_nowait()
                        self._q.put_nowait(frame)
                    except (queue.Empty, queue.Full):
                        pass

    def next_frame(self, timeout=None):
        """Blocking read, for callers already on a worker thread.

        Raises queue.Empty once `timeout` seconds pass with no frame.
        """
        return self._q.get(timeout=timeout)

    async def next_frame_async(self):
        """Cancellable read.

        The short timeout matters: a bare blocking get() inside an executor
        would pin that thread past cancellation and steal the next frame from
        whichever backend takes over.
        """
        loop = asyncio.get_event_loop()
        while True:
            try:
                return await loop.run_in_executor(None, self.next_frame, 0.1)
            except queue.Empty:
                continue

    def clear(self):
        """Drop everything buffered -- called after playback so the assistant
        does not wake itself on its own voice."""
        while True:
            try:
                self._q.get_nowait()
            except queue.Empty:
                return
