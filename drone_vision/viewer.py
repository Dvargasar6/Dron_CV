"""Live viewer GUI (Tkinter) for the ESP32-CAM stream.

Run:
    python -m drone_vision.viewer                       # URL from config/config.yaml
    python -m drone_vision.viewer --source http://192.168.1.50:81/stream
    python -m drone_vision.viewer --source 0            # PC webcam (test without ESP32)
    python -m drone_vision.viewer --source video.mp4    # video file

Keys: S = snapshot, R = toggle recording, Q / Esc = quit.

The GUI is also the dataset collection tool: snapshots and recorded frames are
saved (full resolution, unannotated) to `dataset.raw_dir`.

`ViewerApp.processor` is the hook for the next steps: a callable that takes a
BGR frame and returns an annotated BGR frame (e.g. YOLO detections).
"""

from __future__ import annotations

import argparse
import queue
import threading
import time
import tkinter as tk
from datetime import datetime
from tkinter import ttk
from typing import Callable

import cv2
import numpy as np
from PIL import Image, ImageTk

from .config import load_config, project_path, stream_url_from_config
from .esp32_client import FRAME_SIZES, ESP32Client
from .sources import FrameSource, open_source

Processor = Callable[[np.ndarray], np.ndarray]


class ViewerApp:
    def __init__(self, root: tk.Tk, cfg: dict, source_spec: str, processor: Processor | None = None):
        self.root = root
        self.cfg = cfg
        self.processor = processor
        self.source: FrameSource | None = None
        self.esp: ESP32Client | None = None
        self.last_index = 0
        self.saved_count = 0
        self.record_counter = 0
        self.raw_dir = project_path(cfg["dataset"]["raw_dir"])
        self.events: queue.Queue = queue.Queue()  # results from worker threads -> GUI thread
        self._photo = None  # keep a reference or Tk garbage-collects the image
        self._last_status_poll = 0.0
        self.esp_status: dict | None = None
        self.message: tuple[str, float] | None = None  # (text, time) shown for a few seconds

        root.title("Drone Vision - Viewer")
        root.protocol("WM_DELETE_WINDOW", self.close)
        self._build_widgets(source_spec)
        root.bind("<KeyPress-s>", self._shortcut(self.snapshot))
        root.bind("<KeyPress-r>", self._shortcut(lambda: self.record_var.set(not self.record_var.get())))
        root.bind("<KeyPress-q>", self._shortcut(self.close))
        root.bind("<Escape>", lambda e: self.close())

        self.connect()
        self._tick()

    # ------------------------------------------------------------------ UI
    @staticmethod
    def _shortcut(action: Callable[[], None]):
        """Key handler that is ignored while typing in a text field (Entry/Spinbox/Combobox)."""
        def handler(event):
            if not isinstance(event.widget, tk.Entry):
                action()
        return handler

    def _build_widgets(self, source_spec: str) -> None:
        top = ttk.Frame(self.root, padding=4)
        top.pack(side=tk.TOP, fill=tk.X)

        ttk.Label(top, text="Source:").pack(side=tk.LEFT)
        self.source_var = tk.StringVar(value=source_spec)
        entry = ttk.Entry(top, textvariable=self.source_var, width=38)
        entry.pack(side=tk.LEFT, padx=2)
        entry.bind("<Return>", lambda e: self.connect())
        self.connect_btn = ttk.Button(top, text="Connect", command=self.connect)
        self.connect_btn.pack(side=tk.LEFT, padx=2)

        ttk.Separator(top, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=6)

        # ESP32 camera controls (disabled for webcam / file sources)
        self.esp_widgets: list[tk.Widget] = []
        ttk.Label(top, text="Size:").pack(side=tk.LEFT)
        self.framesize_var = tk.StringVar(value=self.cfg["camera"]["framesize"])
        fs = ttk.Combobox(top, textvariable=self.framesize_var, values=FRAME_SIZES, width=7, state="readonly")
        fs.bind("<<ComboboxSelected>>", lambda e: self._esp_control("framesize", self.framesize_var.get()))
        fs.pack(side=tk.LEFT, padx=2)

        ttk.Label(top, text="JPEG q:").pack(side=tk.LEFT)
        self.quality_var = tk.IntVar(value=self.cfg["camera"]["quality"])
        q = ttk.Spinbox(top, from_=4, to=63, textvariable=self.quality_var, width=4,
                        command=lambda: self._esp_control("quality", self.quality_var.get()))
        q.bind("<Return>", lambda e: self._esp_control("quality", self.quality_var.get()))
        q.pack(side=tk.LEFT, padx=2)

        self.led_var = tk.BooleanVar(value=False)
        led = ttk.Checkbutton(top, text="LED", variable=self.led_var,
                              command=lambda: self._esp_control("led", int(self.led_var.get())))
        led.pack(side=tk.LEFT, padx=4)
        self.esp_widgets += [fs, q, led]

        ttk.Separator(top, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=6)

        ttk.Button(top, text="Snapshot (S)", command=self.snapshot).pack(side=tk.LEFT, padx=2)
        self.record_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="Record (R)", variable=self.record_var).pack(side=tk.LEFT, padx=2)

        self.video = ttk.Label(self.root, anchor=tk.CENTER, text="waiting for frames...")
        self.video.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        self.status_var = tk.StringVar(value="")
        ttk.Label(self.root, textvariable=self.status_var, relief=tk.SUNKEN, anchor=tk.W, padding=3).pack(
            side=tk.BOTTOM, fill=tk.X)
        self.root.minsize(640, 400)

    # ------------------------------------------------------------ actions
    def connect(self) -> None:
        if self.source:
            self.source.stop()
            self.source = None
        spec = self.source_var.get()
        esp_cfg = self.cfg["esp32"]
        try:
            self.source = open_source(spec, esp_cfg["timeout_s"], esp_cfg["reconnect_delay_s"]).start()
        except ValueError as e:
            self.message = (str(e), time.time())
            return
        self.last_index = 0
        self.esp_status = None

        is_esp = spec.startswith("http")
        self.esp = ESP32Client.from_stream_url(spec, esp_cfg["control_port"]) if is_esp else None
        for w in self.esp_widgets:
            w.configure(state=("readonly" if isinstance(w, ttk.Combobox) else "normal") if is_esp else "disabled")
        if self.esp:  # push initial camera settings from the config
            self._esp_control("framesize", self.framesize_var.get())
            self._esp_control("quality", self.quality_var.get())

    def _esp_control(self, var: str, val) -> None:
        if not self.esp:
            return
        esp = self.esp

        def work():
            try:
                self.events.put(("status", esp.set_control(var, val)))
            except Exception as e:
                self.events.put(("error", f"control {var}={val} failed: {e}"))

        threading.Thread(target=work, daemon=True).start()

    def _poll_status(self) -> None:
        esp = self.esp

        def work():
            try:
                self.events.put(("status", esp.status()))
            except Exception:
                pass  # stream errors are already shown in the status bar

        threading.Thread(target=work, daemon=True).start()

    def snapshot(self) -> None:
        frame = self.source.latest() if self.source else None
        if frame is not None:
            self._save(frame.image, prefix="snap")

    def _save(self, img: np.ndarray, prefix: str) -> None:
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        name = f"{prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.jpg"
        cv2.imwrite(str(self.raw_dir / name), img)
        self.saved_count += 1

    def close(self) -> None:
        if self.source:
            self.source.stop()
        self.root.destroy()

    # ------------------------------------------------------------ main loop
    def _tick(self) -> None:
        self._handle_events()

        src = self.source
        frame = src.latest() if src else None
        if frame is not None and frame.index != self.last_index:
            self.last_index = frame.index
            if self.record_var.get():
                self.record_counter += 1
                if self.record_counter % self.cfg["dataset"]["record_every_n"] == 0:
                    self._save(frame.image, prefix="rec")
            self._show(frame.image)

        if self.esp and time.time() - self._last_status_poll > 2.0:
            self._last_status_poll = time.time()
            self._poll_status()

        self._update_status_bar(frame)
        self.root.after(self.cfg["gui"]["refresh_ms"], self._tick)

    def _show(self, img: np.ndarray) -> None:
        if self.processor is not None:
            img = self.processor(img)
        h, w = img.shape[:2]
        max_w = self.cfg["gui"]["max_display_width"]
        if w > max_w:
            img = cv2.resize(img, (max_w, int(h * max_w / w)), interpolation=cv2.INTER_AREA)
        rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        self._photo = ImageTk.PhotoImage(Image.fromarray(rgb))
        self.video.configure(image=self._photo, text="")

    def _handle_events(self) -> None:
        while True:
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                return
            if kind == "status":
                self.esp_status = payload
            elif kind == "error":
                self.message = (payload, time.time())

    def _update_status_bar(self, frame) -> None:
        src = self.source
        if src is None:
            if self.message:
                self.status_var.set(self.message[0])
            return
        parts = [f"state: {src.state}"]
        if src.state == "error" and src.error:
            parts.append(src.error[:80])
        if frame is not None:
            h, w = frame.image.shape[:2]
            parts += [f"{w}x{h}", f"{src.fps:4.1f} fps", f"age {1000 * (time.time() - frame.timestamp):4.0f} ms"]
        if self.esp and self.esp_status:
            parts.append(f"RSSI {self.esp_status.get('rssi')} dBm")
        parts.append(f"saved: {self.saved_count}" + (" [REC]" if self.record_var.get() else ""))
        if self.message and time.time() - self.message[1] < 5.0:
            parts.append(self.message[0])
        self.status_var.set("  |  ".join(parts))


def main() -> None:
    ap = argparse.ArgumentParser(description="Live viewer for the drone camera")
    ap.add_argument("--config", default=None, help="path to config.yaml")
    ap.add_argument("--source", default=None, help="stream URL, webcam index or video file")
    args = ap.parse_args()

    cfg = load_config(args.config)
    source = args.source or stream_url_from_config(cfg)
    root = tk.Tk()
    ViewerApp(root, cfg, source)
    root.mainloop()


if __name__ == "__main__":
    main()
