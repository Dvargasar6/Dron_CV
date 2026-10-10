"""Step 1 verification: check that drone_env has every dependency.

Run:  python -m drone_vision.tools.check_env
Exit code 0 = everything OK.
"""

from __future__ import annotations

import importlib
import sys

MODULES = [
    ("numpy", "numpy"),
    ("cv2", "opencv-python"),
    ("PIL", "Pillow"),
    ("requests", "requests"),
    ("yaml", "PyYAML"),
    ("torch", "torch"),
    ("torchvision", "torchvision"),
    ("ultralytics", "ultralytics"),
    ("tkinter", "tkinter (comes with Python)"),
]


def main() -> int:
    print(f"Python {sys.version.split()[0]}  ({sys.executable})")
    ok = True
    if sys.version_info < (3, 10):
        print("  [FAIL] Python >= 3.10 required")
        ok = False

    for module, package in MODULES:
        try:
            m = importlib.import_module(module)
            version = getattr(m, "__version__", None) or getattr(m, "TkVersion", "")
            print(f"  [ OK ] {package:<28} {version}")
        except Exception as e:
            print(f"  [FAIL] {package:<28} {type(e).__name__}: {e}")
            ok = False

    try:
        import torch
        print(f"  [INFO] torch CUDA available: {torch.cuda.is_available()} (False is expected on this PC)")
    except Exception:
        pass

    try:
        import tkinter
        root = tkinter.Tk()
        root.destroy()
        print("  [ OK ] Tk can open a window (GUI will work)")
    except Exception as e:
        print(f"  [WARN] Tk cannot open a window: {e}  (GUI needs a desktop session)")

    print("\nRESULT:", "environment OK" if ok else "some packages are missing -> see README > Installation")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
