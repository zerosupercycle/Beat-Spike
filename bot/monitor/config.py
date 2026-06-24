"""Load config/monitor.yaml — separate from bot strategy config."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


class MonitorCaptureConfig(BaseModel):
    before_sec: float = Field(default=120.0, ge=0.0)
    after_sec: float = Field(default=120.0, ge=0.0)
    slug_pattern: str = "updown"


class MonitorStorageConfig(BaseModel):
    events_path: str = "data/monitor_events.jsonl"
    snapshots_dir: str = "data/monitor_snapshots"
    state_path: str = "data/monitor_state.json"


class MonitorConfig(BaseModel):
    enabled: bool = True
    targets: list[str] = Field(default_factory=list)
    server_url: str = "http://127.0.0.1:8788"
    rtds_url: str = "wss://ws-live-data.polymarket.com"
    # Real-time RTDS activity stream (disable if Polymarket returns 429; poll fallback still runs).
    rtds_enabled: bool = True
    rtds_reconnect_min_sec: float = Field(default=5.0, ge=1.0)
    rtds_reconnect_max_sec: float = Field(default=120.0, ge=5.0)
    rtds_rate_limit_backoff_sec: float = Field(default=90.0, ge=10.0)
    poll_fallback_sec: float = Field(default=8.0, ge=2.0)
    capture: MonitorCaptureConfig = Field(default_factory=MonitorCaptureConfig)
    storage: MonitorStorageConfig = Field(default_factory=MonitorStorageConfig)


def _resolve_path(path: str) -> Path:
    p = Path(path)
    return p if p.is_absolute() else _PROJECT_ROOT / p


def load_monitor_config(path: str | Path | None = None) -> MonitorConfig:
    cfg_path = Path(path) if path else _PROJECT_ROOT / "config" / "monitor.yaml"
    if not cfg_path.is_absolute():
        cfg_path = _PROJECT_ROOT / cfg_path
    raw: dict[str, Any] = {}
    if cfg_path.is_file():
        with cfg_path.open(encoding="utf-8") as f:
            loaded = yaml.safe_load(f) or {}
        if isinstance(loaded, dict):
            raw = loaded
    return MonitorConfig(**raw)


def monitor_paths(cfg: MonitorConfig) -> tuple[Path, Path, Path]:
    return (
        _resolve_path(cfg.storage.events_path),
        _resolve_path(cfg.storage.snapshots_dir),
        _resolve_path(cfg.storage.state_path),
    )
