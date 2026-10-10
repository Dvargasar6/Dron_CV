# Drone Vision: Following a Path of Numbered Mylar Balloons

A computer-vision system for a drone controlled by an **ESP32-CAM**. The drone has to find number-shaped Mylar balloons
and hit them **in order** (1 → 2 → 3 → … → `LAST_NUMBER`). The ESP32-CAM streams images over WiFi to a PC. The PC
runs a two-stage pipeline (a **YOLO detector** finds balloons, then a **YOLO-cls classifier** reads the number on each
balloon crop) and publishes where the current target is, so the flight-control system (built by another team) can steer toward it.

> Click any section title below to expand or collapse it.

---

<details open>
<summary><h2>1. Project status</h2></summary>

**Last update:** 2026-10-09

| # | Step | Status | Verification (how we know it is done) |
|---|------|--------|----------------------------------------|
| 1 | README + `requirements.txt` + Conda env `drone_env` | 🟡 Code written, **you need to verify it** | `python -m drone_vision.tools.check_env` prints `environment OK` |
| 2 | ESP32-CAM ↔ PC over WiFi + viewer GUI | 🟡 Code written, **you need to verify it** | Firmware boots and prints its IP; `check_stream` reports ≥ 10 fps at VGA; the GUI shows live video |
| 3 | Pre-trained YOLO (COCO) running on the stream | ⚪ Not started | GUI shows COCO detections (person, bottle…) with boxes; the inference FPS is measured |
| 4 | Balloon detector (Colab training) | ⚪ Not started | mAP50 ≥ 0.85 on the validation set; detects balloons live in the GUI |
| 5 | Number classifier (digits + `none`) | ⚪ Not started | top-1 accuracy ≥ 95 % on the validation crops |
| 6 | Full pipeline + mission logic + drone interface | ⚪ Not started | Ordered sequence 1…N is tracked live; UDP messages are received by a test listener |

Legend: ✅ done & verified · 🟡 written, waiting for your test · 🔵 in progress · ⚪ not started · 🔴 blocked

**Next action for you:** run the commands in **§5 Installation** and **§6 Running**,
then report the results (or errors). After that we move on to Step 3.

</details>

---

<details>
<summary><h2>2. System architecture</h2></summary>

```
 ┌──────────────── DRONE ────────────────┐            ┌──────────────────────── PC (drone_env) ─────────────────────────┐
 │                                        │   WiFi     │                                                                  │
 │  ESP32-CAM (firmware/esp32cam_stream)  │  2.4 GHz   │  sources.MJPEGStreamSource  (thread, keeps only newest frame)    │
 │   ├─ :81/stream  MJPEG  ───────────────┼───────────►│        │                                                         │
 │   ├─ :80/status  JSON                  │            │        ▼                                                         │
 │   ├─ :80/capture JPEG                  │◄───────────┼─ esp32_client (camera settings, LED)                             │
 │   └─ :80/control set camera params     │  HTTP GET  │        │                                                         │
 │                                        │            │        ▼                                                         │
 │  Flight controller (other team)        │            │  [Step 4] YOLO detector ──► balloon boxes                        │
 │   ▲                                    │            │        │ crop ROI                                                │
 │   │ target info (UDP JSON) [Step 6]    │            │        ▼                                                         │
 └───┼────────────────────────────────────┘            │  [Step 5] YOLO-cls ──► number 0-9 / none                         │
     │                                                 │        │                                                         │
     └─────────────────────────────────────────────────┼── [Step 6] Mission state machine (target = 1,2,…,LAST_NUMBER)   │
                                                       │        │                                                         │
                                                       │        ▼                                                         │
                                                       │  viewer.py GUI (live video, overlays, dataset capture)           │
                                                       └──────────────────────────────────────────────────────────────────┘
```

</details>

---

<details>
<summary><h2>3. Design decisions (previously open questions)</h2></summary>

<details>
<summary><b>D1 – Communication protocol ESP32 ↔ PC → <u>HTTP MJPEG</u> (+ HTTP control, + UDP for results)</b></summary>

| Option | Latency | Robustness | ESP32 effort | PC effort | Verdict |
|---|---|---|---|---|---|
| **HTTP MJPEG** (`multipart/x-mixed-replace`) | Low (~80–150 ms at VGA) | TCP: no corrupted frames, automatic retransmission | Native (`esp_http_server`, the same approach as Espressif's CameraWebServer) | Simple; can also be tested in a browser | ✅ **Chosen** |
| RTSP | Similar or worse | Good | Third-party libs (Micro-RTSP) that are poorly maintained. Video is still MJPEG inside RTP, so no compression benefit. | Needs FFmpeg/GStreamer | ❌ More complexity for no gain |
| Raw UDP sockets | Lowest | Lost packets mean lost frames. A JPEG (10–40 KB) is larger than the MTU (~1.4 KB), so you need custom fragmentation and reassembly. | Custom protocol | Custom protocol | ⏸ Kept as an **optimization** if measured latency turns out too high |

**Why:** The ESP32-CAM compresses JPEG in hardware, so MJPEG costs the MCU almost nothing. The stream is easy to debug
(open `http://<ip>/` in a browser). The PC reader keeps only the newest frame, so TCP buffering doesn't add latency.
Control commands (resolution, quality, LED) use simple `GET /control?var=..&val=..` requests on a **separate server
(port 80)**, so they still work while the stream (port 81) is busy.

**Return channel (PC → drone flight system):** **UDP datagrams carrying JSON**, sent to a configurable `host:port`
(implemented in Step 6). UDP fits here because only the newest target estimate matters. A late packet is worthless,
and each message is tiny (< 300 B). The other team can read it from any language. A Python API is also available (see D8).

**WiFi mode:** Use *Station* mode (join a router) during development. Use *Access Point* mode (the ESP32 creates the `DRONE_CAM`
network, IP `192.168.4.1`) in the field, where there is no router. It is selected in `wifi_config.h`. If the router can't be
reached, the firmware falls back to AP mode automatically.
</details>

<details>
<summary><b>D2 – Where inference runs → <u>on the PC</u> (Step 3 clarification)</b></summary>

The action plan says *"Load a pre-trained YOLO model onto the MCU"*. The ESP32-CAM (classic ESP32, 240 MHz dual core,
4 MB PSRAM, no AI accelerator) **cannot run YOLO**. Even the tiniest YOLO would take several seconds per frame, if
it fits at all. In line with the chosen architecture ("the PC executes the recognition"), Step 3 is interpreted as:
**run a pre-trained COCO YOLO model on the PC, using frames that come from the MCU**. This step validates the whole
inference chain (stream → model → overlay → FPS) before we train our own models.
*If on-board inference is ever required, the path would be an ESP32-S3 + ESP-DL with a very small custom model.
That is out of scope for now.*
</details>

<details>
<summary><b>D3 – GUI toolkit → <u>Tkinter + Pillow</u></b></summary>

Tkinter ships with Python, so there's nothing to install. `ultralytics` pulls in `opencv-python`, which bundles its own Qt
libraries, and these clash with PySide6/PyQt (a well-known *"Could not load the Qt platform plugin xcb"* error). Using
Tkinter avoids that conflict. Tkinter is more than fast enough to show a VGA stream at 25+ fps.
</details>

<details>
<summary><b>D4 – Detection model → <u>Ultralytics YOLO11n</u> (nano), fine-tuned with 1 class <code>balloon</code></b></summary>

- **Why nano:** inference runs on the **PC's CPU**. YOLO11n is about 2.6 M parameters and takes about 30–60 ms per frame at 640 px
  on a modern CPU, which allows real-time steering. YOLO11s is more accurate but about 2.5× slower.
- **Step 3** uses the pre-trained `yolo11n.pt` (COCO, 80 classes) as-is.
- **Step 4** fine-tunes it on Colab (GPU) with one class, `balloon`, using `imgsz=640`. We'll benchmark `imgsz=416/320` on the
  PC for speed.
- **Alternatives to benchmark in Step 4:** `yolo11s` (accuracy), and newer Ultralytics releases (e.g. YOLO26n) if
  they are available in the installed `ultralytics` version. The model name is a config value, so switching costs nothing.
- **CPU speed-up (optional):** export to ONNX / OpenVINO with `yolo export`, which is usually 1.5–3× faster on Intel CPUs.
</details>

<details>
<summary><b>D5 – Number classification → <u>Ultralytics YOLO11n-cls</u> fine-tuned, 11 classes</b></summary>

- **Classes:** `0`,`1`,…,`9`, `none`. `none` covers a balloon with no visible or readable number (seen from the back,
  heavily rotated, a non-numbered balloon, or a false-positive crop).
  Only the digits that actually exist as balloons are needed. Unused digit folders can be omitted.
- **Input:** the balloon crop from the detector (with a 10 % margin), resized to `imgsz=128` (fast, and enough for the
  big shape of a digit). We'll try 224 if accuracy is low.
- **Why not OCR (Tesseract/EasyOCR) or an MNIST model:** a number-shaped Mylar balloon is a **reflective 3D silhouette**,
  not printed text, so OCR and handwritten-digit models perform poorly on it. A classifier trained on our own crops
  learns the real look of the balloons (reflections, wrinkles, lighting).
- **Why two stages and not one detector with 10 classes:** this is your chosen design, and it has real advantages. The
  detector learns a single, very robust "balloon" concept from all images. The classifier works on a high-resolution
  crop, so digits stay legible from farther away. Each stage can be retrained independently.
- **Classifier dataset:** generated automatically from the detector labels (a crop tool will be provided in Step 5).
</details>

<details>
<summary><b>D6 – Datasets & labelling</b></summary>

- **Image source:** mostly the **ESP32-CAM itself** (same sensor, resolution, and JPEG artefacts as in flight). The GUI's
  *Record* button saves 1 of every N frames to `data/raw/`. Add some phone photos for variety.
- **Variety to capture:** every balloon number, distances from 0.5 to 8 m, angles, indoor and outdoor lighting, sun reflections,
  backgrounds, partial occlusions, motion blur, and **negatives** (frames without balloons).
- **Initial size:** about 400–600 detector images (balloon boxes), and ≥ 100 crops per number class plus `none`.
- **Labelling tool:** [Roboflow](https://roboflow.com) (free plan, exports *YOLOv8/YOLO11* format directly and can
  host the dataset for Colab) **or** CVAT / Label Studio if the data must stay offline.
- **Split:** 80 % train / 20 % val, split by recording session to avoid near-duplicate frames leaking across splits.
</details>

<details>
<summary><b>D7 – Mission logic (state machine, Step 6)</b></summary>

```
target = 1
SEARCH(target)   : no balloon classified as `target` → report "searching" (flight team decides how to rotate)
   │ seen with confidence ≥ c_min in K consecutive frames (K≈3, avoids single-frame misreads)
   ▼
TRACK(target)    : publish the target's offset from the image centre (x,y ∈ [-1,1]) and its relative size (area)
   │ box area ≥ HIT_AREA (balloon fills the view)  then  target lost for T frames   → hit assumed
   ▼
target += 1 ; if target > LAST_NUMBER → DONE
```

`LAST_NUMBER`, `K`, `HIT_AREA`, and the confidence thresholds will be config variables. Optionally, the flight team can
send an explicit "hit confirmed" signal (e.g. from an IMU impact detection) that overrides the visual estimate.
</details>

<details>
<summary><b>D8 – Integration interface for the drone team (Step 6)</b></summary>

Two ways to consume the vision system:

1. **Python API:** `VisionSystem(config).process(frame) -> VisionResult`
2. **UDP JSON** (language-independent), one message per processed frame:

```jsonc
{
  "t": 1760000000.123,          // PC timestamp (s)
  "frame": 1532,                // frame index
  "state": "TRACK",             // SEARCH | TRACK | DONE
  "target": 2,                  // number currently being searched/tracked
  "last_number": 5,
  "found": true,
  "cx": -0.12, "cy": 0.05,      // target centre, normalised to [-1, 1] (0,0 = image centre)
  "area": 0.18,                 // box area / image area (proxy for distance)
  "det_conf": 0.91, "cls_conf": 0.97,
  "latency_ms": 95              // frame received → result sent
}
```
</details>

<details>
<summary><b>D9 – Environment</b></summary>

Conda env `drone_env`, **Python 3.11**, packages installed with pip from `requirements.txt`. PyTorch comes from the
**CPU-only** index, because the PC has no NVIDIA GPU (a smaller download with no CUDA). Training runs on **Google Colab** (GPU),
and the trained `.pt` weights are copied to `models/`.
</details>

</details>

---

<details>
<summary><h2>4. Detailed action plan</h2></summary>

<details>
<summary><b>Step 1 – README, requirements, environment</b> 🟡</summary>

- [x] Write `README.md` (this file) with the plan and decisions
- [x] Write `requirements.txt` and `config/config.yaml`
- [x] Write the environment checker `drone_vision/tools/check_env.py`
- [ ] **You:** create `drone_env` and run `check_env` → it must print `environment OK`
</details>

<details>
<summary><b>Step 2 – WiFi communication + viewer GUI</b> 🟡</summary>

- [x] ESP32-CAM firmware: camera init, WiFi STA/AP, MJPEG `/stream` (port 81), `/status`, `/capture`, `/control` (port 80)
- [x] PC stream reader with auto-reconnect and latest-frame-only buffering (`sources.py`)
- [x] ESP32 control client (`esp32_client.py`)
- [x] Tkinter viewer: live video, FPS, frame age, RSSI, resolution/quality/LED controls, snapshot & record for datasets
- [x] Headless check tool (`check_stream.py`)
- [ ] **You:** flash the firmware → see the IP on the serial monitor → open `http://<ip>/` in a browser
- [ ] **You:** `check_stream` → ≥ 10 fps at VGA (report the measured value)
- [ ] **You:** `viewer` → live video, and changing the resolution works
- [ ] (optional) Measure end-to-end latency: point the camera at a stopwatch on the PC screen, then compare the stopwatch with the
      image shown in the GUI
</details>

<details>
<summary><b>Step 3 – Pre-trained YOLO on the stream (PC)</b> ⚪</summary>

- [ ] `detector.py`: a wrapper around `ultralytics.YOLO` (model path, `imgsz`, `conf`, and class filter from the config)
- [ ] Plug it into the GUI through the `processor` hook (`--model yolo11n.pt`), drawing boxes, labels, and inference ms
- [ ] Benchmark CPU inference at `imgsz` 640 / 416 / 320 (and optionally ONNX/OpenVINO)
- [ ] **Verification:** COCO objects (person, bottle, chair…) are detected live, and the inference time is recorded here
</details>

<details>
<summary><b>Step 4 – Balloon detector (Google Colab)</b> ⚪</summary>

- [ ] Collect images with the GUI *Record* button (see D6)
- [ ] Label them in Roboflow with the class `balloon` and export in YOLO format
- [ ] `notebooks/train_detector.ipynb`: mount Drive / download the dataset → train `yolo11n.pt` (≈100 epochs, imgsz 640)
      → validate → export `best.pt` (+ ONNX)
- [ ] Copy the weights to `models/balloon_det.pt`, then test live in the GUI
- [ ] **Verification:** mAP50 ≥ 0.85 on val, and balloons are detected live at the required distances
</details>

<details>
<summary><b>Step 5 – Number classifier</b> ⚪</summary>

- [ ] `tools/make_cls_dataset.py`: crop balloons using the detector labels (or by running the detector) into
      `data/cls/{train,val}/{0..9,none}/`, then manually check and move any wrong crops
- [ ] `notebooks/train_classifier.ipynb`: fine-tune `yolo11n-cls.pt` (imgsz 128, ≈50 epochs) on Colab
- [ ] Copy the weights to `models/number_cls.pt`
- [ ] **Verification:** top-1 accuracy ≥ 95 % on val, with a confusion matrix reviewed (watch 6↔9, 1↔7, and `none`)
</details>

<details>
<summary><b>Step 6 – Full recognition pipeline + mission + drone interface</b> ⚪</summary>

- [ ] `pipeline.py`: detect → crop → classify → `VisionResult` list
- [ ] `mission.py`: the state machine from D7, with `LAST_NUMBER` as a config variable
- [ ] `publisher.py`: UDP JSON sender (D8) + `tools/udp_listener.py` for the drone team to test
- [ ] `run_mission.py`: headless runner (for the field) + GUI overlay mode
- [ ] **Verification:** walking the camera past balloons 1…N in order makes the target advance correctly, and the UDP
      listener receives the messages
</details>

</details>

---

<details>
<summary><h2>5. Installation (Step 1)</h2></summary>

Requirements: Miniconda/Anaconda, plus a desktop session (for the GUI).

```bash
cd /mnt/aux/home/Development/Dron

# 1. Create and activate the environment
conda create -n drone_env python=3.11 -y
conda activate drone_env

# 2. Install the dependencies (PyTorch CPU build comes from the extra index in requirements.txt)
pip install -r requirements.txt

# 3. Verify
python -m drone_vision.tools.check_env
```

Expected result: every line is `[ OK ]`, `torch CUDA available: False`, and finally `RESULT: environment OK`.

> If pip still downloads a huge CUDA build of torch, install torch first explicitly:
> `pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu` and then re-run
> `pip install -r requirements.txt`.

</details>

---

<details>
<summary><h2>6. Running (Step 2): firmware + PC viewer</h2></summary>

<details>
<summary><b>6.1 Flash the ESP32-CAM</b></summary>

**Hardware:** AI-Thinker ESP32-CAM + USB programmer (ESP32-CAM-MB board, or an FTDI adapter at 5 V).
Using FTDI: connect `5V`, `GND`, `U0R←TX`, `U0T→RX`, and bridge **GPIO0 to GND** while flashing.

**1. Edit `firmware/esp32cam_stream/wifi_config.h`:** set `WIFI_SSID` / `WIFI_PASSWORD` (a **2.4 GHz** network,
because the ESP32 doesn't support 5 GHz), or set `USE_AP_MODE 1`.

**2a. Arduino IDE (2.x):**
- *File → Preferences → Additional boards URLs:* `https://espressif.github.io/arduino-esp32/package_esp32_index.json`
- *Boards Manager:* install **esp32 by Espressif Systems** (2.0.x or 3.x)
- Open `firmware/esp32cam_stream/esp32cam_stream.ino`
- *Tools → Board:* **AI Thinker ESP32-CAM**; *Port:* `/dev/ttyUSB0` (or `ttyACM0`)
- Upload. With FTDI: hold GPIO0→GND, press RESET, upload, then remove the bridge and press RESET again.

**2b. arduino-cli (alternative):**
```bash
URL=https://espressif.github.io/arduino-esp32/package_esp32_index.json
arduino-cli core update-index --additional-urls $URL
arduino-cli core install esp32:esp32 --additional-urls $URL
arduino-cli compile --fqbn esp32:esp32:esp32cam firmware/esp32cam_stream
arduino-cli upload  --fqbn esp32:esp32:esp32cam -p /dev/ttyUSB0 firmware/esp32cam_stream
arduino-cli monitor -p /dev/ttyUSB0 -c baudrate=115200
```
> On Arch Linux, the serial port needs your user to be in the `uucp` group: `sudo usermod -aG uucp $USER` (then log out and back in).

**3. Serial monitor (115200 baud).** You should see:
```
=== Drone ESP32-CAM stream server ===
Connecting to 'MyWifi'.....
Ready:
  Browser test : http://192.168.1.50/
  Stream       : http://192.168.1.50:81/stream
  Status       : http://192.168.1.50/status
```

**4. Browser test:** open `http://<ip>/`. You should see the live video.
**Close the browser tab afterwards:** the stream server serves **one client at a time**.
</details>

<details>
<summary><b>6.2 PC side</b></summary>

1. Put the ESP32's IP in `config/config.yaml` → `esp32.host` (or pass `--source`).
   In AP mode, connect the PC to the `DRONE_CAM` WiFi and use `192.168.4.1`.

```bash
conda activate drone_env
cd /mnt/aux/home/Development/Dron

# Headless check: FPS + saves data/raw/check_stream.jpg
python -m drone_vision.tools.check_stream --seconds 10
python -m drone_vision.tools.check_stream --source http://192.168.1.50:81/stream

# GUI viewer
python -m drone_vision.viewer
python -m drone_vision.viewer --source http://192.168.1.50:81/stream

# Testing the GUI without the ESP32 (PC webcam or a video file)
python -m drone_vision.viewer --source 0
python -m drone_vision.viewer --source some_video.mp4
```

**GUI controls:** *Size* (resolution), *JPEG q* (press Enter or use the arrows), *LED* (flash), *Snapshot* (`S`),
*Record* (`R`, saves 1 of every `record_every_n` frames to `data/raw/`), `Q`/`Esc` to quit.
**Status bar:** connection state · resolution · FPS · *age* (time since the newest frame arrived) · WiFi RSSI · frames saved.

</details>

</details>

---

<details>
<summary><h2>7. Code explanation</h2></summary>

<details>
<summary><b>Repository structure</b></summary>

```
Dron/
├── README.md                    ← this file (plan, status, commands, docs)
├── CLAUDE.md                    ← original project brief
├── requirements.txt             ← pip deps for the conda env drone_env
├── .gitignore
├── config/
│   └── config.yaml              ← all tunable parameters (IP, ports, camera, GUI, dataset)
├── firmware/
│   └── esp32cam_stream/
│       ├── esp32cam_stream.ino  ← ESP32-CAM camera + WiFi + HTTP servers
│       ├── camera_pins.h        ← AI-Thinker pin map
│       └── wifi_config.h        ← WiFi credentials / AP mode  (EDIT BEFORE FLASHING)
├── drone_vision/                ← Python package (PC side)
│   ├── __init__.py
│   ├── config.py                ← load config.yaml, project-relative paths
│   ├── esp32_client.py          ← HTTP client for /status, /control, /capture
│   ├── sources.py               ← threaded frame sources (MJPEG stream, webcam, video)
│   ├── viewer.py                ← Tkinter GUI
│   └── tools/
│       ├── check_env.py         ← Step 1 verification
│       └── check_stream.py      ← Step 2 headless verification
└── data/                        ← (created at runtime, git-ignored) raw images, datasets
```
</details>

<details>
<summary><b><code>firmware/esp32cam_stream/esp32cam_stream.ino</code></b></summary>

- `init_camera()` configures the OV2640 sensor in **JPEG mode** (hardware compression). With PSRAM it uses 2 frame
  buffers and `CAMERA_GRAB_LATEST`, so the frame sent is always the newest one (low latency). Without PSRAM it falls back to QVGA.
- `start_wifi()` runs in Station mode with `WiFi.setSleep(false)` (power save adds a lot of latency) and falls back to AP mode
  after `WIFI_CONNECT_TIMEOUT_MS`. It can also run in AP mode directly.
- **Two `esp_http_server` instances.** Port 80 has `/`, `/status`, `/control`, and `/capture`. Port 81 has `/stream`. The
  stream handler loops forever on its connection, which is why control lives on a separate server.
- `stream_handler()` sends each frame as one multipart part: boundary + `Content-Type`/`Content-Length` + JPEG bytes.
  When the client disconnects, the send fails and the loop exits.
- `control_handler()` handles `?var=&val=` → sensor setters. Frame sizes are exchanged **by name** (`VGA`, `QVGA`…), so the PC
  doesn't depend on the numeric `framesize_t` values, which differ between library versions.
  It replies with the `/status` JSON.
- `loop()` re-joins the router if the WiFi link drops (Station mode).
</details>

<details>
<summary><b><code>drone_vision/config.py</code></b></summary>

`load_config()` reads `config/config.yaml`. `project_path()` resolves relative paths from the project root, so scripts
work from any working directory. `stream_url_from_config()` builds `http://host:81/stream`.
</details>

<details>
<summary><b><code>drone_vision/sources.py</code></b></summary>

- `Frame`: a BGR image, the PC reception timestamp, and an increasing index.
- `FrameSource`: the base class. It runs a reader thread and stores **only the latest frame** behind a lock. Consumers never
  wait on the network and never process stale frames. It also keeps a smoothed FPS estimate (EMA).
- `MJPEGStreamSource`: an HTTP streaming request (`requests`). It parses the stream by finding JPEG **SOI (`FFD8`) / EOI
  (`FFD9`)** markers, which works with any MJPEG server. When several complete frames are buffered, only the newest is
  decoded. It **reconnects automatically** after `reconnect_delay_s` if the stream drops, and `state`/`error` show what
  is happening.
- `OpenCVSource`: a webcam index or video file (looped at its native FPS). It lets you develop and test without the ESP32.
- `open_source(spec)` is a factory: an `http…` spec → MJPEG, digits → webcam, an existing path → video.
</details>

<details>
<summary><b><code>drone_vision/esp32_client.py</code></b></summary>

`ESP32Client` wraps the control endpoints: `status()`, `set_control(var, val)`, and `capture()` (a single JPEG decoded to BGR).
`from_stream_url()` derives the host from the stream URL.
</details>

<details>
<summary><b><code>drone_vision/viewer.py</code></b></summary>

- `ViewerApp` builds a Tkinter window: a toolbar (source, connect, camera controls, snapshot, record), the video label, and a status bar.
- `_tick()` runs every `gui.refresh_ms`. It takes the newest frame, saves it if recording, displays it (scaled to
  `max_display_width`), polls `/status` every 2 s, and refreshes the status bar.
- Network calls to the ESP32 run in **worker threads**, and their results come back through a `queue.Queue`, so the GUI never freezes.
- **`processor` hook:** an optional function `BGR frame → annotated BGR frame` applied before display. Steps 3–6 plug
  YOLO here without touching the GUI code.
- Saved images are always the **original, unannotated** frames, so they're ready for labelling.
</details>

<details>
<summary><b><code>drone_vision/tools/check_env.py</code> · <code>check_stream.py</code></b></summary>

- `check_env` imports each dependency, prints its version, reports whether CUDA is available (expected `False`), and checks that
  Tk can open a window. The exit code is 0 when everything is OK.
- `check_stream` queries `/status`, opens the source, waits for the first frame, saves it to `data/raw/check_stream.jpg`, and then
  prints the FPS every second for `--seconds`.
</details>

</details>

---

<details>
<summary><h2>8. Troubleshooting</h2></summary>

| Symptom | Fix |
|---|---|
| `Camera init failed with error 0x105` / `0x20001` | Ribbon cable is not seated, or the board is not AI-Thinker (check the pin map). Power-cycle it. |
| Board reboots in a loop, `Brownout detector was triggered` | Weak 5 V supply. Use a good 5 V/2 A source and short cables. On the drone, use a dedicated regulator. |
| Never connects to WiFi | Only 2.4 GHz is supported. Check SSID/password (case sensitive). The board falls back to AP `DRONE_CAM` after 20 s. |
| PC can't reach the IP | The PC and ESP32 must be on the same subnet. Some routers have *AP/client isolation*, so disable it or use AP mode. |
| `check_stream` works but the browser doesn't (or vice versa) | Only **one** stream client at a time. Close the other one. |
| Low FPS | Lower the resolution (QVGA) or raise the JPEG q value (e.g. 15–20). Move closer to the router, and watch the RSSI (> -70 dBm is good). |
| `TclError: no display name` | The GUI needs a desktop session. Use `check_stream` over SSH. |
| `ModuleNotFoundError: drone_vision` | Run the commands from the project root (`cd .../Dron`) with `python -m ...`. |

</details>

---

<details>
<summary><h2>9. Changelog</h2></summary>

- **2026-10-09**: Step 1 & Step 2 code written: README with plan/decisions D1–D9, `requirements.txt`, `config.yaml`,
  ESP32-CAM firmware (MJPEG + control servers), PC stream reader, ESP32 client, Tkinter viewer, and check tools.
  Waiting for user verification.

</details>
