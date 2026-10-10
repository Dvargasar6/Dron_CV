"""HTTP client for the control endpoints of the ESP32-CAM firmware
(firmware/esp32cam_stream). The video itself is read by sources.MJPEGStreamSource.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

import cv2
import numpy as np
import requests

FRAME_SIZES = ["QQVGA", "QVGA", "CIF", "VGA", "SVGA", "XGA", "HD", "SXGA", "UXGA"]


class ESP32Client:
    def __init__(self, host: str, control_port: int = 80, stream_port: int = 81, timeout: float = 3.0):
        self.host = host
        self.control_port = control_port
        self.stream_port = stream_port
        self.timeout = timeout

    @classmethod
    def from_stream_url(cls, url: str, control_port: int = 80, timeout: float = 3.0) -> "ESP32Client":
        parsed = urlparse(url)
        return cls(parsed.hostname, control_port, parsed.port or 81, timeout)

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.control_port}"

    @property
    def stream_url(self) -> str:
        return f"http://{self.host}:{self.stream_port}/stream"

    def status(self) -> dict[str, Any]:
        r = requests.get(f"{self.base_url}/status", timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def set_control(self, var: str, val: str | int) -> dict[str, Any]:
        """Change a camera setting; returns the updated status."""
        r = requests.get(f"{self.base_url}/control", params={"var": var, "val": str(val)}, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def capture(self) -> np.ndarray:
        """Grab a single JPEG frame (independent of the stream), as BGR image."""
        r = requests.get(f"{self.base_url}/capture", timeout=self.timeout)
        r.raise_for_status()
        img = cv2.imdecode(np.frombuffer(r.content, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            raise ValueError("ESP32 returned an invalid JPEG")
        return img
