"""Frame sources: where images come from.

Every source runs a background thread that keeps ONLY THE LATEST frame.
Consumers (GUI, detector) call `latest()` and never block on the network,
and stale frames are dropped instead of queued -> minimum latency, which
matters more than processing every frame when steering a drone.

    MJPEGStreamSource  - ESP32-CAM HTTP MJPEG stream (the real system)
    OpenCVSource       - PC webcam index or video file (testing without the ESP32)

Use `open_source(spec)` to build the right one from a string.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
import requests

SOI = b"\xff\xd8"  # JPEG start-of-image marker
EOI = b"\xff\xd9"  # JPEG end-of-image marker
MAX_BUFFER_BYTES = 4 * 1024 * 1024


@dataclass
class Frame:
    image: np.ndarray  # BGR, HxWx3
    timestamp: float   # time.time() when the frame was received on the PC
    index: int         # increasing counter, lets consumers detect new frames


class FrameSource:
    """Base class: thread management, latest-frame storage and FPS estimate."""

    def __init__(self, name: str):
        self.name = name
        self.state = "idle"  # idle | connecting | connected | error | stopped
        self.error: str | None = None
        self.fps = 0.0
        self._lock = threading.Lock()
        self._latest: Frame | None = None
        self._count = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_t: float | None = None

    # -- public API ---------------------------------------------------------
    def start(self) -> "FrameSource":
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=f"src-{self.name}", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._interrupt()
        if self._thread:
            self._thread.join(timeout=2.0)
        self.state = "stopped"

    def latest(self) -> Frame | None:
        with self._lock:
            return self._latest

    def wait_first_frame(self, timeout: float) -> Frame | None:
        t0 = time.time()
        while time.time() - t0 < timeout:
            f = self.latest()
            if f is not None:
                return f
            time.sleep(0.02)
        return None

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    # -- for subclasses -----------------------------------------------------
    def _run(self) -> None:
        raise NotImplementedError

    def _interrupt(self) -> None:
        """Unblock the reader thread on stop (optional)."""

    def _publish(self, image: np.ndarray) -> None:
        now = time.time()
        if self._last_t is not None:
            dt = now - self._last_t
            if dt > 0:
                inst = 1.0 / dt
                self.fps = inst if self.fps == 0 else 0.9 * self.fps + 0.1 * inst
        self._last_t = now
        with self._lock:
            self._count += 1
            self._latest = Frame(image, now, self._count)


class MJPEGStreamSource(FrameSource):
    """Reads a multipart MJPEG HTTP stream by scanning JPEG SOI/EOI markers.

    Marker scanning works with any MJPEG server (no dependency on the exact
    multipart headers) and reconnects automatically if the stream drops.
    """

    def __init__(self, url: str, timeout: float = 5.0, reconnect_delay: float = 1.0, chunk_size: int = 8192):
        super().__init__(url)
        self.url = url
        self.timeout = timeout
        self.reconnect_delay = reconnect_delay
        self.chunk_size = chunk_size
        self._response: requests.Response | None = None

    def _interrupt(self) -> None:
        resp = self._response
        if resp is not None:
            try:
                resp.close()
            except Exception:
                pass

    def _run(self) -> None:
        while not self._stop.is_set():
            self.state = "connecting"
            try:
                with requests.get(self.url, stream=True, timeout=self.timeout) as resp:
                    self._response = resp
                    resp.raise_for_status()
                    self.state, self.error = "connected", None
                    buf = bytearray()
                    for chunk in resp.iter_content(chunk_size=self.chunk_size):
                        if self._stop.is_set():
                            break
                        buf += chunk
                        jpg = self._pop_latest_jpeg(buf)
                        if jpg is not None:
                            img = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
                            if img is not None:
                                self._publish(img)
                    if not self._stop.is_set():
                        raise ConnectionError("stream closed by the server")
            except Exception as e:  # network errors, HTTP errors, closed stream
                if self._stop.is_set():
                    break
                self.state, self.error = "error", f"{type(e).__name__}: {e}"
            finally:
                self._response = None
            self._stop.wait(self.reconnect_delay)

    @staticmethod
    def _pop_latest_jpeg(buf: bytearray) -> bytes | None:
        """Remove all complete JPEGs from `buf`; return only the newest one."""
        newest = None
        while True:
            start = buf.find(SOI)
            if start < 0:
                del buf[:-1]  # keep last byte: it may be the first half of a marker
                break
            end = buf.find(EOI, start + 2)
            if end < 0:
                del buf[:start]  # incomplete frame: drop garbage before it, wait for more data
                break
            newest = bytes(buf[start:end + 2])
            del buf[:end + 2]
        if len(buf) > MAX_BUFFER_BYTES:  # corrupted stream protection
            buf.clear()
        return newest


class OpenCVSource(FrameSource):
    """Webcam (integer index) or video file (looped, played at its native FPS)."""

    def __init__(self, spec: int | str):
        super().__init__(str(spec))
        self.spec = spec
        self.is_file = not isinstance(spec, int)
        self._cap: cv2.VideoCapture | None = None

    def _run(self) -> None:
        self.state = "connecting"
        cap = cv2.VideoCapture(self.spec)
        self._cap = cap
        if not cap.isOpened():
            self.state, self.error = "error", f"cannot open {self.spec!r}"
            return
        self.state = "connected"
        period = 1.0 / (cap.get(cv2.CAP_PROP_FPS) or 30.0) if self.is_file else 0.0
        while not self._stop.is_set():
            t0 = time.time()
            ok, img = cap.read()
            if not ok:
                if self.is_file:
                    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)  # loop the video
                    continue
                self.state, self.error = "error", "webcam read failed"
                break
            self._publish(img)
            if period:
                self._stop.wait(max(0.0, period - (time.time() - t0)))
        cap.release()


def open_source(spec: str, timeout: float = 5.0, reconnect_delay: float = 1.0) -> FrameSource:
    """Build a source from a string: 'http://...' URL, webcam index '0', or a video path."""
    spec = spec.strip()
    if spec.startswith(("http://", "https://")):
        return MJPEGStreamSource(spec, timeout=timeout, reconnect_delay=reconnect_delay)
    if spec.isdigit():
        return OpenCVSource(int(spec))
    if Path(spec).exists():
        return OpenCVSource(spec)
    raise ValueError(f"unknown source {spec!r}: use an http URL, a webcam index or an existing video file")
