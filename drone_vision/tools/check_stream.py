"""Step 2 verification (no GUI): connect to a source, measure FPS, save one frame.

Run:
    python -m drone_vision.tools.check_stream                 # URL from config
    python -m drone_vision.tools.check_stream --source http://192.168.1.50:81/stream --seconds 15
    python -m drone_vision.tools.check_stream --source 0      # webcam

Exit code 0 = frames received.
"""

from __future__ import annotations

import argparse
import json
import sys
import time

import cv2

from ..config import load_config, project_path, stream_url_from_config
from ..esp32_client import ESP32Client
from ..sources import open_source


def main() -> int:
    ap = argparse.ArgumentParser(description="Headless stream check")
    ap.add_argument("--config", default=None)
    ap.add_argument("--source", default=None, help="stream URL, webcam index or video file")
    ap.add_argument("--seconds", type=float, default=10.0, help="measurement duration")
    args = ap.parse_args()

    cfg = load_config(args.config)
    spec = args.source or stream_url_from_config(cfg)
    print(f"Source: {spec}")

    if spec.startswith("http"):
        esp = ESP32Client.from_stream_url(spec, cfg["esp32"]["control_port"])
        try:
            print("ESP32 /status:", json.dumps(esp.status()))
        except Exception as e:
            print(f"[WARN] /status failed ({e}); is the IP right and the PC on the same network?")

    src = open_source(spec, cfg["esp32"]["timeout_s"], cfg["esp32"]["reconnect_delay_s"]).start()
    try:
        first = src.wait_first_frame(timeout=cfg["esp32"]["timeout_s"] + 5)
        if first is None:
            print(f"[FAIL] no frame received. state={src.state} error={src.error}")
            return 1
        h, w = first.image.shape[:2]
        out = project_path(cfg["dataset"]["raw_dir"]) / "check_stream.jpg"
        out.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(out), first.image)
        print(f"[ OK ] first frame {w}x{h} saved to {out}")

        t0, start_index, last_index = time.time(), first.index, first.index
        while time.time() - t0 < args.seconds:
            time.sleep(1.0)
            f = src.latest()
            last_index = f.index
            print(f"  t={time.time() - t0:5.1f}s  fps(ema)={src.fps:5.1f}  frames={last_index - start_index}  "
                  f"state={src.state}")
        avg = (last_index - start_index) / (time.time() - t0)
        print(f"[ OK ] average {avg:.1f} fps over {args.seconds:.0f} s")
        return 0
    finally:
        src.stop()


if __name__ == "__main__":
    sys.exit(main())
