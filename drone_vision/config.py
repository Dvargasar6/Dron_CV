"""Loading of config/config.yaml and project-relative paths."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    """Read the YAML config. `path` defaults to config/config.yaml."""
    path = Path(path) if path else DEFAULT_CONFIG_PATH
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def project_path(p: str | Path) -> Path:
    """Resolve a path from the config relative to the project root."""
    p = Path(p)
    return p if p.is_absolute() else PROJECT_ROOT / p


def stream_url_from_config(cfg: dict[str, Any]) -> str:
    esp = cfg["esp32"]
    return f"http://{esp['host']}:{esp['stream_port']}/stream"
